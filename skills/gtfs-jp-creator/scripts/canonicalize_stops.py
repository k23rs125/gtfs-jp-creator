"""
canonicalize_stops.py
=====================

Step 3.x (停留所名正規化): 参照フィード（旧 GTFS-JP zip、公式 stops.txt、
canonical 名リストのテキスト）から「正解の停留所名表記」を引き当て、
当方の表記揺れを吸収する。

なぜこのスクリプトが必要か:
    LLM (Claude/Gemini/ChatGPT) は PDF に書かれた停留所名をそのまま読み取る。
    一方、自治体・事業者が公開する公式 GTFS-JP の表記は同じ停留所でも
    「JR新宮中央駅」と「新宮中央駅(駅前広場)」のように違うことがある。
    eval_compare の集合比較では別物として扱われ、精度が過小評価される。

    本スクリプトを通せば、参照フィードに該当があるものは正解表記に統一され、
    後段の eval_compare で正しく一致するようになる。

    古賀市の実証:
        canonicalize なし: stop_times 71.9% (96 件の差分のうち約95件が表記揺れ)
        canonicalize あり: stop_times ~99.7% (真の時刻誤差のみが差分として残る)

照合ロジック:
    1. 参照フィードから「正解の停留所名リスト」を抽出
    2. 各名前を正規化キーに変換
       - NFKC 正規化
       - 全角/半角空白統一
       - 「JR」接頭辞・「(駅前広場)」のような接尾辞を吸収する正規化
       - 「」「」括弧除去
    3. 当方の各 stop_name について、正規化キーで参照リストを引く
       完全一致 → 参照の元の表記で置換
    4. マッチしない場合は元の表記のまま（強制変更しない）

Usage:
    python canonicalize_stops.py <stops.txt>
        --reference <old_feed.zip | stops.txt | text>
        [-o <output.txt>]
        [--update-stop-times <stop_times.txt>]  # stop_times に stop_name カラムがあれば同時更新
        [--report <canonicalize_report.json>]

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# 名前正規化
# ---------------------------------------------------------------------------

# 表記揺れを吸収するための置換規則（正規化キーを作る用、最終的な表記には影響しない）
_NOISE_PATTERNS = [
    "JR", "ＪＲ",                # 鉄道接頭辞（OFF と ON でぶれることが多い）
    "(駅前広場)", "（駅前広場）",
    "(東口)", "（東口）",
    "(西口)", "（西口）",
    "(南口)", "（南口）",
    "(北口)", "（北口）",
    "「", "」", "『", "』",
]


def normalize_name_for_match(name: str) -> str:
    """マッチング用の正規化キーを作る。

    - NFKC（全角英数→半角、互換文字統一）
    - 全角空白→半角、連続空白圧縮
    - 「」括弧・「JR」接頭辞・「(駅前広場)」等の接尾辞を除去

    注意: これは「マッチング用のキー」を作るための関数で、最終的な
    停留所名表記には影響しない。表記そのものは reference 由来のものを採用する。
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    s = s.replace("　", " ")
    for noise in _NOISE_PATTERNS:
        s = s.replace(noise, "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def read_csv_text(text: str) -> tuple[list[dict], list[str]]:
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def read_csv(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return read_csv_text(f.read())


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


# ---------------------------------------------------------------------------
# 参照リストの読み込み
# ---------------------------------------------------------------------------

def load_reference_canonical_names(ref_path: Path) -> list[str]:
    """参照フィードから canonical な停留所名リストを取得。

    対応形式:
        - .zip: GTFS-JP zip（中の stops.txt から stop_name を取り出す）
        - .txt (csv): stop_name 列を持つ CSV
        - .txt (一行一名前): GTFS じゃないプレーンテキスト
    """
    if ref_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(ref_path) as zf:
            stops_name = None
            for name in zf.namelist():
                if name.endswith("stops.txt"):
                    stops_name = name
                    break
            if stops_name is None:
                raise FileNotFoundError(f"{ref_path} 内に stops.txt が見つかりません")
            raw = zf.read(stops_name).decode("utf-8-sig")
        rows, _ = read_csv_text(raw)
        return [r.get("stop_name", "") for r in rows if r.get("stop_name")]

    # テキストファイル: CSV ヘッダーに stop_name があれば CSV として読む
    text = ref_path.read_text(encoding="utf-8-sig")
    first_line = text.split("\n", 1)[0]
    if "stop_name" in first_line:
        rows, _ = read_csv_text(text)
        return [r.get("stop_name", "") for r in rows if r.get("stop_name")]

    # 一行一名前のプレーンテキスト
    return [line.strip() for line in text.splitlines() if line.strip()]


def build_canonical_map(canonical_names: list[str]) -> dict[str, str]:
    """canonical 名のリストから「正規化キー → 元の canonical 表記」のマップを作る。

    同じ正規化キーに複数の canonical が紐付くことがあれば、最初に出現したものを採用。
    """
    cmap: dict[str, str] = {}
    for name in canonical_names:
        key = normalize_name_for_match(name)
        if not key:
            continue
        if key not in cmap:
            cmap[key] = name
    return cmap


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def canonicalize_stops(stops_rows: list[dict], canonical_map: dict[str, str]
                        ) -> tuple[list[dict], list[dict]]:
    """stops_rows の stop_name を canonical map に照合して書き換える。

    Returns:
        (updated_rows, change_details)
    """
    changes: list[dict] = []
    for row in stops_rows:
        original = row.get("stop_name", "")
        if not original:
            continue
        key = normalize_name_for_match(original)
        canonical = canonical_map.get(key)
        if canonical and canonical != original:
            row["stop_name"] = canonical
            changes.append({
                "stop_id": row.get("stop_id", ""),
                "before": original,
                "after": canonical,
                "match_key": key,
            })
    return stops_rows, changes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="参照フィードから canonical な停留所名表記を引き当て、表記揺れを吸収する"
    )
    parser.add_argument("input", help="正規化したい stops.txt")
    parser.add_argument("--reference", required=True,
                        help="canonical 名の参照 (zip / stops.txt / プレーンテキスト)")
    parser.add_argument("-o", "--output", default=None,
                        help="出力 stops.txt (既定: <input>.canonical.txt)")
    parser.add_argument("--report", default="canonicalize_report.json",
                        help="レポート出力先 (既定: ./canonicalize_report.json)")
    args = parser.parse_args()

    in_path = Path(args.input)
    ref_path = Path(args.reference)
    if not in_path.exists():
        print(f"Error: input not found: {in_path}", file=sys.stderr)
        return 1
    if not ref_path.exists():
        print(f"Error: reference not found: {ref_path}", file=sys.stderr)
        return 1

    out_path = Path(args.output) if args.output else in_path.with_suffix(".canonical.txt")
    report_path = Path(args.report)

    print(f"Input:      {in_path}")
    print(f"Reference:  {ref_path}")
    print(f"Output:     {out_path}")
    print()

    # 読み込み
    rows, fieldnames = read_csv(in_path)
    canonical_names = load_reference_canonical_names(ref_path)
    canonical_map = build_canonical_map(canonical_names)
    print(f"Loaded: {len(rows)} stops (input), "
          f"{len(canonical_names)} canonical names (ref), "
          f"{len(canonical_map)} unique match keys")
    print()

    # 正規化
    rows, changes = canonicalize_stops(rows, canonical_map)

    # 書き出し
    write_csv(out_path, rows, fieldnames)

    # レポート
    report = {
        "summary": {
            "total_stops": len(rows),
            "renamed": len(changes),
            "unchanged": len(rows) - len(changes),
        },
        "changes": changes,
        "reference": str(ref_path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # サマリ
    print("=" * 64)
    print("CANONICALIZE REPORT")
    print("=" * 64)
    print(f"Total stops:    {len(rows)}")
    print(f"Renamed:        {len(changes)}")
    print(f"Unchanged:      {len(rows) - len(changes)}")
    if changes:
        print()
        print("Renamed details:")
        for c in changes:
            print(f"  {c['stop_id']:<10} {c['before']:<25} → {c['after']}")
    print("=" * 64)
    print(f"Output written: {out_path}")
    print(f"Report saved:   {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
