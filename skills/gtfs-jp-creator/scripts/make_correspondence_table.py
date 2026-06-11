#!/usr/bin/env python3
"""
make_correspondence_table.py
============================
PDFと抽出結果の対応表を作る (目視検証支援ツール)。

座標抽出結果(extract JSON)の「便・停留所・時刻」と、
座標補完後のstops.txtの「緯度経度」を突き合わせ、
便ごとに 番号・名前・時刻・緯度経度 を並べたCSVを出力する。

人がPDFと照らし合わせ、抽出がPDF通りか、座標が妥当かを目視確認するための表。
"""
import argparse, json, csv, sys
from pathlib import Path

def load_stops_coords(stops_txt):
    """stops.txt から名前→(lat,lon) の辞書を作る。"""
    name2coord = {}
    with open(stops_txt, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            name = r.get("stop_name", "").strip()
            lat = r.get("stop_lat", "").strip()
            lon = r.get("stop_lon", "").strip()
            name2coord[name] = (lat, lon)
    return name2coord

def main():
    ap = argparse.ArgumentParser(description="PDFと抽出結果の対応表(目視検証支援)")
    ap.add_argument("extract", help="座標抽出結果 JSON (extract)")
    ap.add_argument("--stops", required=True, help="座標補完後の stops.txt")
    ap.add_argument("-o", "--output", required=True, help="出力CSV")
    ap.add_argument("--bbox", default=None,
                    help="対象地域の範囲 lon_min,lat_min,lon_max,lat_max (範囲外を検出)")
    args = ap.parse_args()

    ext = json.load(open(args.extract, encoding="utf-8"))
    name2coord = load_stops_coords(args.stops)

    bbox = None
    if args.bbox:
        try:
            lon_min, lat_min, lon_max, lat_max = [float(x) for x in args.bbox.split(",")]
            bbox = (lon_min, lat_min, lon_max, lat_max)
        except ValueError:
            print(f"[警告] --bbox の形式が不正です: {args.bbox}", file=sys.stderr)

    def judge(lat, lon):
        """座標の妥当性を判定。'座標なし' / '範囲外' / '' (OK)"""
        if lat in ("", "(座標なし)", None) or lon in ("", "(座標なし)", None):
            return "座標なし"
        if bbox:
            try:
                la, lo = float(lat), float(lon)
            except ValueError:
                return "座標不正"
            lon_min, lat_min, lon_max, lat_max = bbox
            if not (lon_min <= lo <= lon_max and lat_min <= la <= lat_max):
                return "範囲外"
        return ""

    rows = []
    for bi, b in enumerate(ext.get("blocks", [])):
        for ti, t in enumerate(b["trips"]):
            trip_label = f"block{bi}_便{ti+1}"
            for c in t["cells"]:
                nm = c["name"]
                lat, lon = name2coord.get(nm, ("(座標なし)", "(座標なし)"))
                rows.append({
                    "便": trip_label,
                    "順": c["seq"],
                    "番号": c["num"] if c["num"] is not None else "",
                    "停留所名": nm,
                    "時刻": c["time"],
                    "緯度": lat,
                    "経度": lon,
                    "要予約": "○" if c.get("reserve") else "",
                    "判定": judge(lat, lon),
                })

    with open(args.output, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["便","順","番号","停留所名","時刻","緯度","経度","要予約","判定"])
        w.writeheader()
        w.writerows(rows)

    # サマリ
    n_trips = sum(len(b["trips"]) for b in ext.get("blocks", []))
    n_nocoord = sum(1 for r in rows if r["判定"] == "座標なし")
    n_outside = sum(1 for r in rows if r["判定"] == "範囲外")
    print(f"[OK] {args.output}", file=sys.stderr)
    print(f"  便{n_trips} 行{len(rows)} 座標なし{n_nocoord}行 範囲外{n_outside}行", file=sys.stderr)
    if n_outside:
        print(f"  [注意] 範囲外の座標が {n_outside}行 あります。同名の遠方バス停を誤って拾った可能性。", file=sys.stderr)

if __name__ == "__main__":
    main()
