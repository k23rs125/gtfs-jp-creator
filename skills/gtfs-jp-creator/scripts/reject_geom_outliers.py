"""
reject_geom_outliers.py
=======================

Step 3.5d2（経路ジオメトリ外れ値の棄却）: 座標補完(P11/Nominatim/手動)後、各停留所の
座標を**便の前後停留所(経路)と照合**し、経路から大きく外れる停留所を「同名誤マッチの疑い」
として座標を消す（→後段の内挿/手動に回す）。

動機: 自治体が南北に長いと、自治体bbox内でも同名別地点に誤マッチしうる（例: 築城巡回線の
「八津田」が約10km、「京築恵みの郷」が約8km離れた同名にヒット）。座標が付くため
stop_without_location では捕まらず、公式データと比較しないと気づけない誤りだった。

判定: 停留所 k の前後の既知停留所 j,l を結ぶ線分への**垂直距離(off-route距離)**を
全便で求め、その**最小値**が閾値(既定1500m)を超えるとき外れ値とみなす。
（経路線分上にある停留所は≈0。区間が長くても線分上なら小さい。誤マッチだけが大きく外れる。）

Usage:
  python reject_geom_outliers.py <stops.txt> --stop-times <stop_times.txt>
      [-o out] [--report r.json] [--threshold-m 2000]

License: Apache 2.0
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path


def _to_xy(lat, lon, lat0, lon0):
    """局所平面近似で (lat,lon)→(x,y)メートル。原点 (lat0,lon0)。"""
    x = (lon - lon0) * math.cos(math.radians(lat0)) * 111320.0
    y = (lat - lat0) * 110540.0
    return (x, y)


def _point_seg_dist(c, j, l):
    """点 c から線分 j-l への距離(m)。c,j,l は (lat,lon)。"""
    lat0, lon0 = j
    px, py = _to_xy(c[0], c[1], lat0, lon0)
    ax, ay = 0.0, 0.0                      # j を原点
    bx, by = _to_xy(l[0], l[1], lat0, lon0)
    dx, dy = bx - ax, by - ay
    seg2 = dx*dx + dy*dy
    if seg2 == 0:                          # j==l のときは点間距離
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / seg2))
    cx, cy = ax + t*dx, ay + t*dy
    return math.hypot(px - cx, py - cy)


def main() -> int:
    ap = argparse.ArgumentParser(description="経路ジオメトリで座標の外れ値(同名誤マッチ)を棄却")
    ap.add_argument("input")
    ap.add_argument("--stop-times", required=True)
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--report", default="geom_outlier_report.json")
    ap.add_argument("--threshold-m", type=float, default=3000.0,
                    help="経路線分への垂直距離がこの m を超えたら外れ値として棄却（既定3000）。"
                         "曲がった道路上の正しい停留所を誤検出しないよう、大外れ(同名別自治体)のみ捕捉する値")
    a = ap.parse_args()

    in_path = Path(a.input)
    out_path = Path(a.output) if a.output else in_path.with_suffix(".geom.txt")
    with in_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f); rows = list(reader); fields = list(reader.fieldnames or [])
    by_id = {r.get("stop_id"): r for r in rows}

    def coord(sid):
        r = by_id.get(sid)
        if r and (r.get("stop_lat") or "").strip() and (r.get("stop_lon") or "").strip():
            return (float(r["stop_lat"]), float(r["stop_lon"]))
        return None

    trips = defaultdict(list)
    with Path(a.stop_times).open(encoding="utf-8-sig", newline="") as f:
        seq = [(x["trip_id"], int(x["stop_sequence"]), x["stop_id"]) for x in csv.DictReader(f)]
    for tid, _, sid in sorted(seq, key=lambda x: (x[0], x[1])):
        trips[tid].append(sid)

    # 各停留所の「前後既知からの寄り道超過」最小値
    extras = defaultdict(list)
    for ids in trips.values():
        coords = [(k, sid) for k, sid in enumerate(ids) if coord(sid)]
        for k, sid in enumerate(ids):
            c = coord(sid)
            if not c:
                continue
            pred = max([(kk, ss) for kk, ss in coords if kk < k and ss != sid], default=None)
            succ = min([(kk, ss) for kk, ss in coords if kk > k and ss != sid], default=None)
            if pred and succ:
                cj, cl = coord(pred[1]), coord(succ[1])
                extras[sid].append(_point_seg_dist(c, cj, cl))

    rejected = []
    for sid, exs in extras.items():
        if exs and min(exs) > a.threshold_m:
            r = by_id[sid]
            rejected.append({"stop_id": sid, "stop_name": r.get("stop_name"),
                             "lat": r.get("stop_lat"), "lon": r.get("stop_lon"),
                             "min_extra_m": round(min(exs))})
            r["stop_lat"] = ""; r["stop_lon"] = ""   # 座標を消す（後段の内挿/手動へ）

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\r\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

    Path(a.report).write_text(json.dumps(
        {"threshold_m": a.threshold_m, "rejected": rejected}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print(f"経路ジオメトリ外れ値の棄却: {len(rejected)} 件（閾値 {a.threshold_m:.0f}m）")
    for r in rejected:
        print(f"  ! {r['stop_name']} を棄却（経路から約 {r['min_extra_m']}m 外れ＝同名誤マッチの疑い）"
              f" 旧座標 {r['lat']},{r['lon']}")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
