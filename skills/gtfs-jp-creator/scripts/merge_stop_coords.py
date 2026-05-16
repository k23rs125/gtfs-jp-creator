"""
merge_stop_coords.py
====================

Step 3.5a (既知座標の再利用): 既存の GTFS-JP フィードから停留所座標を引き継ぐ。

用途:
    自治体が「既存の GTFS-JP を改めて作り直したい」というケースでは、
    停留所の物理的な位置は変わっていないことが多い（変わるのは時刻表）。
    その場合、Nominatim でゼロから引くより、検証済みの旧フィードの
    stop_lat / stop_lon を停留所名でマッチして再利用する方が
    圧倒的に正確かつ高速。

    Step 3.5 (enrich_stops.py / Nominatim) の前段として実行すると、
    旧フィードにある停留所は確実な座標で埋まり、
    旧フィードに無い新規停留所だけが enrich_stops.py の対象になる。

マッチング:
    停留所名で照合。NFKC 正規化 + 全角/半角スペース統一で表記揺れを吸収。
    旧フィードに同名で複数エントリ（方向別など）がある場合は最初の1件を採用。

Usage:
    python merge_stop_coords.py <new_stops.txt>
        --reference <old_gtfs.zip | old_stops.txt>
        [-o <output_stops.txt>]
        [--report <merge_report.json>]
        [--overwrite]            # 既に座標がある stop も上書きする

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# 名前正規化
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """停留所名を比較用に正規化する。

    - NFKC 正規化（全角英数 → 半角、互換文字の統一）
    - 全角スペース → 半角スペース
    - 前後の空白除去
    - 連続スペースを1つに
    """
    if name is None:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    s = s.replace("　", " ")  # 全角スペース → 半角
    s = " ".join(s.split())       # 連続スペース圧縮 + trim
    return s


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def read_stops_csv_text(text: str) -> tuple[list[dict], list[str]]:
    """stops.txt のテキストから (rows, fieldnames) を返す。"""
    # BOM 除去
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def read_stops_csv(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        text = f.read()
    return read_stops_csv_text(text)


def write_stops_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """UTF-8 with BOM + CRLF で書き出す（GTFS 仕様）。"""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


# ---------------------------------------------------------------------------
# リファレンス（旧フィード）の読み込み
# ---------------------------------------------------------------------------

def load_reference_stops(ref_path: Path) -> list[dict]:
    """旧 GTFS-JP フィード（.zip または stops.txt）から stops 行を読み込む。"""
    if ref_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(ref_path) as zf:
            # zip 内の stops.txt を探す（ルート or サブフォルダ）
            stops_name = None
            for name in zf.namelist():
                if name.endswith("stops.txt"):
                    stops_name = name
                    break
            if stops_name is None:
                raise FileNotFoundError(f"{ref_path} 内に stops.txt が見つかりません")
            raw = zf.read(stops_name).decode("utf-8-sig")
        rows, _ = read_stops_csv_text(raw)
        return rows
    else:
        rows, _ = read_stops_csv(ref_path)
        return rows


def build_name_coord_map(ref_rows: list[dict]) -> tuple[dict[str, tuple[str, str]], dict[str, int]]:
    """旧フィードの stops 行から「正規化名 → (lat, lon)」マップを作る。

    Returns:
        (coord_map, name_count)
        coord_map: 正規化名 → (lat_str, lon_str)。最初に出現したものを採用。
        name_count: 正規化名 → 旧フィードでの出現回数（複数=方向別など）
    """
    coord_map: dict[str, tuple[str, str]] = {}
    name_count: dict[str, int] = {}
    for r in ref_rows:
        name = normalize_name(r.get("stop_name", ""))
        if not name:
            continue
        name_count[name] = name_count.get(name, 0) + 1
        lat = (r.get("stop_lat") or "").strip()
        lon = (r.get("stop_lon") or "").strip()
        if not lat or not lon:
            continue
        if name not in coord_map:  # 最初の1件を採用
            coord_map[name] = (lat, lon)
    return coord_map, name_count


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def has_coords(row: dict) -> bool:
    lat = (row.get("stop_lat") or "").strip()
    lon = (row.get("stop_lon") or "").strip()
    return bool(lat) and bool(lon)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="既存の GTFS-JP フィードから停留所座標を停留所名マッチで再利用する"
    )
    parser.add_argument("input", help="座標を埋めたい新しい stops.txt")
    parser.add_argument("--reference", required=True,
                        help="旧 GTFS-JP フィード（.zip）または旧 stops.txt")
    parser.add_argument("-o", "--output", default=None,
                        help="出力 stops.txt（デフォルト: <input>.enriched.txt）")
    parser.add_argument("--report", default="merge_coords_report.json",
                        help="レポート出力先（デフォルト: ./merge_coords_report.json）")
    parser.add_argument("--overwrite", action="store_true",
                        help="既に座標がある stop も旧フィードの値で上書きする")
    args = parser.parse_args()

    in_path = Path(args.input)
    ref_path = Path(args.reference)
    if not in_path.exists():
        print(f"Error: input not found: {in_path}", file=sys.stderr)
        return 1
    if not ref_path.exists():
        print(f"Error: reference not found: {ref_path}", file=sys.stderr)
        return 1

    out_path = Path(args.output) if args.output else in_path.with_suffix(".enriched.txt")
    report_path = Path(args.report)

    print(f"Input:      {in_path}")
    print(f"Reference:  {ref_path}")
    print(f"Output:     {out_path}")
    print(f"Overwrite:  {args.overwrite}")
    print()

    # --- 読み込み ---
    rows, fieldnames = read_stops_csv(in_path)
    if "stop_lat" not in fieldnames:
        fieldnames.append("stop_lat")
    if "stop_lon" not in fieldnames:
        fieldnames.append("stop_lon")

    ref_rows = load_reference_stops(ref_path)
    coord_map, name_count = build_name_coord_map(ref_rows)

    print(f"Loaded: {len(rows)} stops (new), {len(ref_rows)} stops (reference)")
    print(f"Reference unique names with coords: {len(coord_map)}")
    print()

    # --- マッチング ---
    n_matched = 0
    n_already = 0
    n_unmatched = 0
    matched_details = []
    unmatched_details = []

    for row in rows:
        raw_name = row.get("stop_name", "")
        norm = normalize_name(raw_name)

        if has_coords(row) and not args.overwrite:
            n_already += 1
            continue

        if norm in coord_map:
            lat, lon = coord_map[norm]
            row["stop_lat"] = lat
            row["stop_lon"] = lon
            n_matched += 1
            multi = name_count.get(norm, 1)
            matched_details.append({
                "stop_id": row.get("stop_id"),
                "stop_name": raw_name,
                "lat": lat,
                "lon": lon,
                "reference_entries": multi,
            })
            note = f" (旧フィードに {multi} 件あり、先頭を採用)" if multi > 1 else ""
            print(f"  ✓ {raw_name}: ({lat}, {lon}){note}")
        else:
            n_unmatched += 1
            unmatched_details.append({
                "stop_id": row.get("stop_id"),
                "stop_name": raw_name,
                "normalized": norm,
            })
            print(f"  ✗ {raw_name}: 旧フィードに該当なし")

    # --- 書き出し ---
    write_stops_csv(out_path, rows, fieldnames)

    # --- レポート ---
    total = len(rows)
    coverage = (n_matched + n_already) / total * 100 if total else 0.0
    report = {
        "summary": {
            "total_stops": total,
            "matched_from_reference": n_matched,
            "already_had_coords": n_already,
            "unmatched": n_unmatched,
            "coverage_pct": round(coverage, 1),
        },
        "matched": matched_details,
        "unmatched": unmatched_details,
        "reference_file": str(ref_path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # --- サマリ ---
    print()
    print("=" * 64)
    print("MERGE COORDS REPORT")
    print("=" * 64)
    print(f"Total stops:              {total}")
    print(f"Matched from reference:   {n_matched}")
    print(f"Already had coords:       {n_already}")
    print(f"Unmatched (要 Step 3.5):  {n_unmatched}")
    print(f"Coverage:                 {report['summary']['coverage_pct']}%")
    if unmatched_details:
        print()
        print("Unmatched stops (enrich_stops.py で補完が必要):")
        for u in unmatched_details[:10]:
            print(f"  {u['stop_id']}  {u['stop_name']}")
        if len(unmatched_details) > 10:
            print(f"  ... and {len(unmatched_details) - 10} more (see report)")
    print("=" * 64)
    print(f"Output written:  {out_path}")
    print(f"Report saved:    {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
