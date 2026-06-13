#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_shape_coverage.py  ―  ルート線(shape)が各便の停留所を正しく通っているかを機械チェックする。

検証ツールの一つ（「機械チェック＋目視」の流儀）。公式データ不要、手元のGTFSだけで動く。
標準ライブラリのみ・GPU不要・決定的処理。推測しない。決まらない/おかしい点は要確認として出す。

何を見るか（便ごと・停留所ごと）:
  1) 停留所から shape 線までの最短距離。しきい値(既定50m)以上なら「離れすぎ＝要確認」。
     → OSRMがその停留所を通っていない/別の道に引いた疑い。
  2) 停留所を shape 上に射影した沿線距離が stop_sequence 順に増えているか。
     減っていれば順序逆転＝逆走の疑い。

入力:
  stops.txt / shapes.txt / trips(_with_shapes).txt / stop_times.txt
出力:
  CSV（便ごと・停留所ごとに 距離・順序・判定 を1行）。Excelで要確認行だけ見れば済む。

使い方:
  python check_shape_coverage.py GTFSフォルダ
  python check_shape_coverage.py GTFSフォルダ --trips trips.with_shapes.txt --threshold 50 --out shape_coverage.csv
"""

import argparse
import csv
import math
import os
import sys
from collections import defaultdict, OrderedDict


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def need(path, what):
    if not os.path.exists(path):
        sys.exit("ERROR: %s が見つかりません: %s" % (what, path))
    return path


# --- 平面近似でメートル換算（この緯度では誤差わずか・決定的） ---
def make_projector(lat0):
    # 1度あたりのメートル（緯度lat0近傍の局所平面近似）
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    def to_xy(lat, lon):
        return (lon * m_per_deg_lon, lat * m_per_deg_lat)
    return to_xy


def point_seg_dist_and_t(px, py, ax, ay, bx, by):
    """点P から線分AB への最短距離と、AB上の射影パラメータt(0..1)、
       および線分始点Aからの射影点までの距離を返す。"""
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0.0:
        d = math.hypot(px - ax, py - ay)
        return d, 0.0, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
    t_clamped = max(0.0, min(1.0, t))
    projx, projy = ax + t_clamped * dx, ay + t_clamped * dy
    dist = math.hypot(px - projx, py - projy)
    along = t_clamped * math.hypot(dx, dy)  # この線分内での進み
    return dist, t_clamped, along


def project_point_to_polyline(px, py, poly_xy, seg_cumlen):
    """点を折れ線に射影。最短距離 min_dist と、折れ線始点からの沿線距離 along_total を返す。"""
    best = None
    for i in range(len(poly_xy) - 1):
        ax, ay = poly_xy[i]
        bx, by = poly_xy[i + 1]
        dist, t, along = point_seg_dist_and_t(px, py, ax, ay, bx, by)
        along_total = seg_cumlen[i] + along
        if best is None or dist < best[0]:
            best = (dist, along_total)
    return best  # (min_dist, along_total)


def main():
    ap = argparse.ArgumentParser(description="ルート線(shape)が各便の停留所を正しく通っているか機械チェックしCSV出力")
    ap.add_argument("gtfs_dir", help="GTFSファイル一式が入ったフォルダ")
    ap.add_argument("--trips", default=None,
                    help="便→shape対応ファイル名（既定: trips.with_shapes.txt があればそれ、無ければ trips.txt）")
    ap.add_argument("--threshold", type=float, default=50.0, help="離れすぎ判定の距離しきい値(m)。既定50")
    ap.add_argument("--out", default="shape_coverage.csv", help="出力CSVパス（既定: shape_coverage.csv）")
    args = ap.parse_args()

    d = args.gtfs_dir
    stops_rows = read_csv(need(os.path.join(d, "stops.txt"), "stops.txt"))
    shapes_rows = read_csv(need(os.path.join(d, "shapes.txt"), "shapes.txt"))
    st_rows = read_csv(need(os.path.join(d, "stop_times.txt"), "stop_times.txt"))

    # trips ファイルの決定（shape_idを持つ方）
    if args.trips:
        trips_path = os.path.join(d, args.trips)
    else:
        cand = os.path.join(d, "trips.with_shapes.txt")
        trips_path = cand if os.path.exists(cand) else os.path.join(d, "trips.txt")
    trips_rows = read_csv(need(trips_path, "trips(_with_shapes).txt"))
    print("便→shape対応に使うファイル: %s" % os.path.basename(trips_path), file=sys.stderr)

    # 停留所座標
    stop_coord = {}
    for r in stops_rows:
        sid = r["stop_id"].strip()
        la, lo = r.get("stop_lat", "").strip(), r.get("stop_lon", "").strip()
        if la == "" or lo == "":
            stop_coord[sid] = None  # 座標未補完
        else:
            try:
                stop_coord[sid] = (float(la), float(lo))
            except ValueError:
                stop_coord[sid] = None
    stop_name = {r["stop_id"].strip(): r.get("stop_name", "").strip() for r in stops_rows}

    # shape点（sequence順）
    shp = defaultdict(list)
    for r in shapes_rows:
        try:
            seq = int(r["shape_pt_sequence"])
            la = float(r["shape_pt_lat"]); lo = float(r["shape_pt_lon"])
        except (ValueError, KeyError):
            continue
        shp[r["shape_id"].strip()].append((seq, la, lo))
    for sid in shp:
        shp[sid].sort(key=lambda x: x[0])

    # 便→shape_id
    trip_shape = {}
    for r in trips_rows:
        sid = (r.get("shape_id") or "").strip()
        trip_shape[r["trip_id"].strip()] = sid if sid else None

    # 便→[(stop_sequence, stop_id)]
    trip_stops = defaultdict(list)
    for r in st_rows:
        try:
            seq = int(r["stop_sequence"])
        except (ValueError, KeyError):
            continue
        trip_stops[r["trip_id"].strip()].append((seq, r["stop_id"].strip()))
    for t in trip_stops:
        trip_stops[t].sort(key=lambda x: x[0])

    # 緯度の代表値（投影用）：全停留所の平均緯度
    lats = [c[0] for c in stop_coord.values() if c]
    lat0 = sum(lats) / len(lats) if lats else 35.0
    to_xy = make_projector(lat0)

    rows_out = []
    summary = OrderedDict()
    issues_far = 0
    issues_order = 0
    skipped_no_shape = []
    skipped_no_coord = 0

    for trip_id in sorted(trip_stops):
        shape_id = trip_shape.get(trip_id)
        seq_stops = trip_stops[trip_id]
        if not shape_id or shape_id not in shp:
            skipped_no_shape.append(trip_id)
            continue
        # shapeを平面座標へ＋線分累積長
        poly_ll = [(la, lo) for _, la, lo in shp[shape_id]]
        poly_xy = [to_xy(la, lo) for la, lo in poly_ll]
        seg_cumlen = [0.0]
        for i in range(len(poly_xy) - 1):
            ax, ay = poly_xy[i]; bx, by = poly_xy[i + 1]
            seg_cumlen.append(seg_cumlen[-1] + math.hypot(bx - ax, by - ay))

        prev_along = None
        for (stop_seq, stop_id) in seq_stops:
            coord = stop_coord.get(stop_id)
            name = stop_name.get(stop_id, "")
            if coord is None:
                rows_out.append([trip_id, shape_id, stop_seq, stop_id, name,
                                 "", "", "", "座標未補完(スキップ)"])
                skipped_no_coord += 1
                continue
            px, py = to_xy(coord[0], coord[1])
            min_dist, along = project_point_to_polyline(px, py, poly_xy, seg_cumlen)

            order_ok = ""
            if prev_along is not None:
                order_ok = "OK" if along >= prev_along - 1.0 else "逆順"  # 1m余裕
            prev_along = along

            verdict = "OK"
            if min_dist > args.threshold:
                verdict = "要確認(離れすぎ)"; issues_far += 1
            if order_ok == "逆順":
                verdict = "要確認(順序逆転)" if verdict == "OK" else verdict + "+順序逆転"
                issues_order += 1

            rows_out.append([trip_id, shape_id, stop_seq, stop_id, name,
                             "%.1f" % min_dist, "%.0f" % along, order_ok, verdict])

        summary[trip_id] = shape_id

    # CSV出力
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trip_id", "shape_id", "stop_sequence", "stop_id", "stop_name",
                    "距離m(shape線まで)", "沿線距離m", "順序", "判定"])
        w.writerows(rows_out)

    # 集計をstderrに
    print("出力しました: %s" % args.out, file=sys.stderr)
    print("  検証した便: %d / 行(停留所×便): %d" % (len(summary), len(rows_out)), file=sys.stderr)
    print("  しきい値: %.0fm" % args.threshold, file=sys.stderr)
    print("  要確認(離れすぎ): %d 件" % issues_far, file=sys.stderr)
    print("  要確認(順序逆転): %d 件" % issues_order, file=sys.stderr)
    if skipped_no_coord:
        print("  座標未補完でスキップした停留所行: %d" % skipped_no_coord, file=sys.stderr)
    if skipped_no_shape:
        print("  shape未割当でスキップした便: %d (%s)"
              % (len(skipped_no_shape), ", ".join(skipped_no_shape[:5]) + ("..." if len(skipped_no_shape) > 5 else "")),
              file=sys.stderr)


if __name__ == "__main__":
    main()
