#!/usr/bin/env python3
"""
apply_manual_coords.py
======================
Step 3.5 (手動座標オーバーライド): stops.txt の停留所座標を、人が確認した
手動座標で上書きする。座標補完の自動経路（P11 / Nominatim 等）でマッチしない、
あるいは誤マッチする停留所（医療機関名など）に対し、地図実測などで確定した
正しい座標を最優先で適用するための独立ステップ。

設計方針 (正しく失敗する):
  - 手動座標は最も信頼できる情報源として、自動補完より優先して適用する。
  - 指定された停留所が stops.txt に見つからない場合、黙って無視せず警告する
    （手動座標ファイルの typo や stop_id 変更を検出するため）。
  - 推測で座標を作らない。手動ファイルに書かれた値のみを適用する。

手動座標ファイル(JSON)の形式:
  {
    "by_stop_id":   { "S045": {"lat": 33.252459, "lon": 130.420398, "note": "..."} },
    "by_stop_name": { "池田クリニック": {"lat": 33.252459, "lon": 130.420398} }
  }
  - by_stop_id を優先し、無ければ by_stop_name でマッチする。
  - note は任意（出所メモ。適用には影響しない）。

Usage:
  python apply_manual_coords.py <stops.txt> --coords <manual_coords.json> [-o <out.txt>]
        [--only-empty]
  -o 省略時は入力 stops.txt を上書き。
  --only-empty 指定時は、座標が空(または0)の停留所だけ補完し、既存値は変更しない。

License: Apache 2.0
"""
import argparse
import csv
import json
import sys
from pathlib import Path


def is_empty_coord(v: str) -> bool:
    v = (v or "").strip()
    return v in ("", "0", "0.0")


def fmt_coord(v) -> str:
    """既存 stops.txt の慣例（小数6桁）に合わせて整形する。"""
    return f"{float(v):.6f}"


def main():
    ap = argparse.ArgumentParser(
        description="stops.txt の停留所座標を手動座標で上書きする (Step3.5 手動オーバーライド)")
    ap.add_argument("stops", help="入力 stops.txt")
    ap.add_argument("--coords", required=True, help="手動座標ファイル(JSON)")
    ap.add_argument("-o", "--output", default=None,
                    help="出力 stops.txt（省略時は入力を上書き）")
    ap.add_argument("--only-empty", action="store_true",
                    help="座標が空(または0)の停留所だけ補完し、既存値は変更しない")
    args = ap.parse_args()

    stops_path = Path(args.stops)
    out_path = Path(args.output) if args.output else stops_path

    # --- 手動座標ファイル読み込み ---
    try:
        manual = json.loads(Path(args.coords).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ERROR] 手動座標ファイルを読めません: {e}", file=sys.stderr)
        sys.exit(1)
    by_id = manual.get("by_stop_id", {}) or {}
    by_name = manual.get("by_stop_name", {}) or {}
    if not by_id and not by_name:
        print("[WARN] 手動座標ファイルに by_stop_id も by_stop_name もありません。何も適用しません。",
              file=sys.stderr)

    # --- stops.txt 読み込み ---
    with open(stops_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    if not fieldnames or "stop_lat" not in fieldnames or "stop_lon" not in fieldnames:
        print(f"[ERROR] stops.txt に stop_lat/stop_lon 列がありません: {fieldnames}", file=sys.stderr)
        sys.exit(1)

    # --- 適用 ---
    applied = []        # (stop_id, stop_name, lat, lon, by)
    skipped_nonempty = []  # only-empty 指定で既存値があり据え置いた
    used_id_keys = set()
    used_name_keys = set()
    for r in rows:
        sid = (r.get("stop_id") or "").strip()
        sname = (r.get("stop_name") or "").strip()
        spec = None
        by = None
        if sid in by_id:
            spec = by_id[sid]; by = "stop_id"; used_id_keys.add(sid)
        elif sname in by_name:
            spec = by_name[sname]; by = "stop_name"; used_name_keys.add(sname)
        if spec is None:
            continue
        if args.only_empty and not (is_empty_coord(r.get("stop_lat")) or is_empty_coord(r.get("stop_lon"))):
            skipped_nonempty.append((sid, sname))
            continue
        try:
            r["stop_lat"] = fmt_coord(spec["lat"])
            r["stop_lon"] = fmt_coord(spec["lon"])
        except (KeyError, ValueError, TypeError) as e:
            print(f"[ERROR] {by}={sid or sname} の座標値が不正です: {spec} ({e})", file=sys.stderr)
            sys.exit(1)
        applied.append((sid, sname, r["stop_lat"], r["stop_lon"], by))

    # --- マッチしなかった手動指定の検出（typo / stop_id変更の検出。正しく失敗）---
    unmatched_id = [k for k in by_id if k not in used_id_keys]
    unmatched_name = [k for k in by_name if k not in used_name_keys]

    # --- 書き戻し（BOMなしUTF-8、LF）---
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # --- レポート ---
    print(f"[OK] 手動座標を適用: {len(applied)}件 → {out_path}", file=sys.stderr)
    for sid, sname, lat, lon, by in applied:
        print(f"  - {sid} {sname}: lat={lat} lon={lon}  (matched by {by})", file=sys.stderr)
    if skipped_nonempty:
        print(f"[INFO] --only-empty のため既存座標を据え置き: {len(skipped_nonempty)}件", file=sys.stderr)
        for sid, sname in skipped_nonempty:
            print(f"  - {sid} {sname}", file=sys.stderr)
    if unmatched_id or unmatched_name:
        print(f"[WARN] 手動座標ファイルの指定のうち stops.txt に見つからないものがあります"
              f"（typo か stop_id 変更の可能性。確認してください）:", file=sys.stderr)
        for k in unmatched_id:
            print(f"  - by_stop_id: {k}", file=sys.stderr)
        for k in unmatched_name:
            print(f"  - by_stop_name: {k}", file=sys.stderr)

    # 残った空座標の件数（参考）
    still_empty = [r for r in rows if is_empty_coord(r.get("stop_lat")) or is_empty_coord(r.get("stop_lon"))]
    print(f"[INFO] 適用後も座標が空の停留所: {len(still_empty)}件", file=sys.stderr)
    for r in still_empty:
        print(f"  - {r.get('stop_id')} {r.get('stop_name')}", file=sys.stderr)


if __name__ == "__main__":
    main()
