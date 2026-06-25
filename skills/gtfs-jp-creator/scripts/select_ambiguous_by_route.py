"""
select_ambiguous_by_route.py
============================
Step 3.5b2（同名複数候補の経路位置選択）: P11照合で同名候補が市域bbox内に複数あり
「別地点の疑い（要確認）」となった停留所を、**便の経路上のあるべき位置**に最も近い候補へ
自動選択する。

位置づけ（要確認 option2 の一歩先）:
    option2（enrich_stops_p11 --review-csv）は「黙って先頭採用せず要確認リストに出す」。
    本スクリプトはその一歩先で、**実在するP11候補の中から経路に最も合うものを決定的に選ぶ**。
    内挿(interpolate_coords)が「推定座標」を作るのに対し、本処理は推定位置を手がかりに
    **実在候補へスナップ**するので、座標は推定値でなく実在のP11座標になる。

仕組み:
    1. 各停留所の便内の前後の「確定（非あいまい）停留所」座標から、停留所順インデックスで
       あるべき位置を内挿推定（interpolate_coords と同じ式）。複数便の中央値を採る。
    2. 推定位置に最も近い同名候補を選び、その座標を採用する。
    3. 前後に確定座標が無く推定できない停留所は変更しない（要確認のまま）。

入力:
    <stops.txt>            P11補完後の stops.txt（あいまい停留所は先頭候補が入っている）
    --stop-times <path>    便の停留所順
    --p11-report <path>    enrich_stops_p11 のレポート（ambiguous_matches に候補一覧）
    -o <output>            出力 stops.txt（既定 <input>.ambsel.txt）
    --report <path>        選択レポート

License: Apache 2.0
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def _haversine_m(a, b):
    R = 6371000.0
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _has_coord(r):
    return bool((r.get("stop_lat") or "").strip()) and bool((r.get("stop_lon") or "").strip())


def main() -> int:
    ap = argparse.ArgumentParser(description="同名複数候補を経路位置で自動選択する")
    ap.add_argument("input")
    ap.add_argument("--stop-times", required=True)
    ap.add_argument("--p11-report", required=True)
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--report", default="ambiguous_select_report.json")
    a = ap.parse_args()

    in_path = Path(a.input)
    out_path = Path(a.output) if a.output else in_path.with_suffix(".ambsel.txt")
    with in_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    by_id = {r.get("stop_id"): r for r in rows}

    # あいまい停留所と候補一覧
    rep = json.loads(Path(a.p11_report).read_text(encoding="utf-8"))
    amb = {}   # stop_id -> [(lat,lon), ...]
    for m in rep.get("ambiguous_matches", []):
        sid = m.get("stop_id")
        cands = [(c["lat"], c["lon"]) for c in m.get("candidates", [])]
        if sid and cands:
            amb[sid] = cands
    if not amb:
        # あいまい無し：そのままコピー出力
        with out_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator="\r\n")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})
        Path(a.report).write_text(json.dumps({"resolved": [], "note": "あいまい候補なし"},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
        print("同名複数候補の経路選択: 対象0件")
        print(f"Output: {out_path}")
        return 0

    # 確定アンカー（あいまい停留所は除外＝座標が不確かなので推定の足場にしない）
    known = {sid: (float(r["stop_lat"]), float(r["stop_lon"]))
             for sid, r in by_id.items() if _has_coord(r) and sid not in amb}

    # 便の停留所順
    trips = defaultdict(list)
    seqk = []
    with Path(a.stop_times).open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            seqk.append((r["trip_id"], int(r["stop_sequence"]), r["stop_id"]))
    for tid, _, sid in sorted(seqk, key=lambda x: (x[0], x[1])):
        trips[tid].append(sid)

    # 各あいまい停留所の経路位置推定（前後の確定アンカーからインデックス内挿・複数便で中央値）
    est = defaultdict(list)
    for seq in trips.values():
        anchors = [(k, sid) for k, sid in enumerate(seq) if sid in known]
        for k, sid in enumerate(seq):
            if sid not in amb:
                continue
            pred = max([(kk, ss) for kk, ss in anchors if kk < k], default=None)
            succ = min([(kk, ss) for kk, ss in anchors if kk > k], default=None)
            if pred and succ:
                (kj, sj), (kl, sl) = pred, succ
                la_j, lo_j = known[sj]
                la_l, lo_l = known[sl]
                fr = (k - kj) / (kl - kj)
                est[sid].append((la_j + fr * (la_l - la_j), lo_j + fr * (lo_l - lo_j)))

    resolved, skipped = [], []
    for sid, cands in amb.items():
        r = by_id.get(sid)
        if not r:
            continue
        if sid not in est:
            skipped.append({"stop_id": sid, "stop_name": r.get("stop_name"),
                            "reason": "前後に確定座標が無く推定できない（要確認のまま）"})
            continue
        ela = statistics.median(p[0] for p in est[sid])
        elo = statistics.median(p[1] for p in est[sid])
        # 推定位置に最も近い候補
        dists = [( _haversine_m((ela, elo), c), c) for c in cands]
        dists.sort(key=lambda x: x[0])
        best_d, best = dists[0]
        cur = (float(r["stop_lat"]), float(r["stop_lon"])) if _has_coord(r) else None
        changed = (cur is None) or (round(best[0], 6) != round(cur[0], 6)
                                    or round(best[1], 6) != round(cur[1], 6))
        r["stop_lat"] = f"{best[0]:.6f}"
        r["stop_lon"] = f"{best[1]:.6f}"
        resolved.append({
            "stop_id": sid, "stop_name": r.get("stop_name"),
            "n_candidates": len(cands),
            "chosen": {"lat": round(best[0], 6), "lon": round(best[1], 6)},
            "dist_to_route_estimate_m": round(best_d),
            "second_nearest_m": round(dists[1][0]) if len(dists) > 1 else None,
            "changed_from_first_pick": changed,
        })

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\r\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

    Path(a.report).write_text(json.dumps(
        {"resolved": resolved, "skipped": skipped,
         "note": "推定位置に最も近い実在P11候補を採用（座標は推定値でなく実在座標）。"},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"同名複数候補の経路選択: 解決 {len(resolved)}件 / 推定不可 {len(skipped)}件")
    for it in resolved[:15]:
        mark = "（先頭採用から変更）" if it["changed_from_first_pick"] else "（先頭採用と一致）"
        print(f"  ○ {it['stop_name']}: 候補{it['n_candidates']}件→経路位置に最近 "
              f"{it['chosen']['lat']},{it['chosen']['lon']} "
              f"(推定から{it['dist_to_route_estimate_m']}m, 次点{it['second_nearest_m']}m){mark}")
    for it in skipped[:10]:
        print(f"  ? {it['stop_name']}: {it['reason']}")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
