#!/usr/bin/env python3
"""
check_speed.py
==============
stop_times の時刻の妥当性を「速度」の面から検査する (目視検証支援ツール)。

各便の連続する停留所間について、直線距離(緯度経度から)と所要時間(時刻差)から
区間の速度(km/h)を計算し、バスとして異常な速度を検出する。
公式データが無くても、生成データ単体で時刻の妥当性を測れる。

判定:
  速すぎ … 上限(既定60km/h)超。時刻か座標の誤りの疑い。
  遅すぎ … 下限(既定2km/h)未満かつ距離あり。時刻の誤り/取りこぼしの疑い。
  時間0  … 所要0分だが距離あり。速すぎの極端な形。
  OK     … 正常範囲。
"""
import argparse, csv, sys, math
from collections import defaultdict

def haversine_km(lat1, lon1, lat2, lon2):
    """2点の緯度経度から直線距離(km)を返す。"""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def to_sec(t):
    """HH:MM:SS を秒に。25時超(翌日)も扱える。"""
    h, m, s = (int(x) for x in t.split(":"))
    return h*3600 + m*60 + s

def load_stops(stops_txt):
    d = {}
    with open(stops_txt, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = r["stop_id"]
            try:
                d[sid] = (r["stop_name"], float(r["stop_lat"]), float(r["stop_lon"]))
            except (ValueError, KeyError):
                d[sid] = (r.get("stop_name",""), None, None)
    return d

def main():
    ap = argparse.ArgumentParser(description="stop_timesの時刻を速度面で検査(目視検証支援)")
    ap.add_argument("--stops", required=True, help="stops.txt")
    ap.add_argument("--stop-times", required=True, help="stop_times.txt")
    ap.add_argument("-o", "--output", required=True, help="出力CSV")
    ap.add_argument("--max-kmh", type=float, default=60.0, help="速すぎ判定の上限(既定60)")
    ap.add_argument("--min-kmh", type=float, default=2.0, help="遅すぎ判定の下限(既定2)")
    args = ap.parse_args()

    stops = load_stops(args.stops)

    # trip_id ごとに (stop_sequence, stop_id, departure, arrival) を集める
    trips = defaultdict(list)
    with open(args.stop_times, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trips[r["trip_id"]].append((
                int(r["stop_sequence"]), r["stop_id"],
                r.get("departure_time","").strip(), r.get("arrival_time","").strip()
            ))

    rows = []
    for tid, seq in trips.items():
        seq.sort()
        for i in range(len(seq)-1):
            _, sid_a, dep_a, _ = seq[i]
            _, sid_b, _, arr_b = seq[i+1]
            name_a, la, lo = stops.get(sid_a, ("?",None,None))
            name_b, lb, lo2 = stops.get(sid_b, ("?",None,None))
            # 距離
            if None in (la, lo, lb, lo2):
                dist_km = None
            else:
                dist_km = haversine_km(la, lo, lb, lo2)
            # 所要時間
            try:
                sec = to_sec(arr_b) - to_sec(dep_a)
            except ValueError:
                sec = None
            # 速度と判定
            kmh = None; judge = ""
            if dist_km is None or sec is None:
                judge = "座標/時刻なし"
            elif sec <= 0:
                judge = "時間0" if dist_km > 0.5 else ""  # 0.5km以下は分単位時刻表で起こりうるため許容
            else:
                kmh = dist_km / (sec/3600.0)
                if kmh > args.max_kmh:
                    judge = "速すぎ"
                elif kmh < args.min_kmh and dist_km > 0.5:
                    judge = "遅すぎ"
            rows.append({
                "便": tid,
                "出発": name_a,
                "到着": name_b,
                "距離km": f"{dist_km:.3f}" if dist_km is not None else "",
                "所要分": f"{sec/60:.1f}" if sec is not None else "",
                "時速kmh": f"{kmh:.1f}" if kmh is not None else "",
                "判定": judge,
            })

    with open(args.output, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["便","出発","到着","距離km","所要分","時速kmh","判定"])
        w.writeheader()
        w.writerows(rows)

    n_fast = sum(1 for r in rows if r["判定"]=="速すぎ")
    n_slow = sum(1 for r in rows if r["判定"]=="遅すぎ")
    n_zero = sum(1 for r in rows if r["判定"]=="時間0")
    print(f"[OK] {args.output}", file=sys.stderr)
    print(f"  区間{len(rows)} 速すぎ{n_fast} 遅すぎ{n_slow} 時間0{n_zero}", file=sys.stderr)
    if n_fast or n_zero:
        print(f"  [注意] 速すぎ/時間0 の区間あり。時刻か座標の誤りの可能性。", file=sys.stderr)

if __name__ == "__main__":
    main()
