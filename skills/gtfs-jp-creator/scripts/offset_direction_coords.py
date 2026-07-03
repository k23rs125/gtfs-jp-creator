#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
offset_direction_coords.py
==========================
行き/帰りで分割した「同名・同座標」の停留所を、進行方向の反対側へ推定オフセットする。

日本は左側通行なのでバス停は進行方向の左側にある。行き(方向0)と帰り(方向1)は
互いに逆向きに走るため、各方向の停留所を「その進行方向の左」へ寄せると、
物理的に反対側（≒反対車線）に分かれる。国土数値情報P11は上り/下りの別座標を
ほぼ持たない（同名2件の多くは0m重複）ため、経路(停留所の並び)から幾何学的に推定する。

★これは推定であり、必ず利用者が地図で確認する前提。動かした停留所は report に
  列挙し、classify_coord_confidence で「要確認」に落として提出前チェックでブロックする。

入力: stops.txt(座標補完済) / stop_times.txt / trips.txt
出力: stops.txt を上書き（対象の座標を反対側へ）＋ report(JSON: 推定した stop_id 一覧)
使い方:
  python offset_direction_coords.py <gtfs_dir> [--offset 8] [--same-thresh 5] [--report r.json]
License: Apache 2.0
"""
import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

M_PER_DEG_LAT = 111320.0


def _read(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r), r.fieldnames


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def offset_left(lat, lon, east, north, meters):
    """進行方向ベクトル(east,north)の左90°へ meters だけずらした座標を返す。"""
    n = math.hypot(east, north)
    if n == 0:
        return None
    ux, uy = east / n, north / n
    lx, ly = -uy, ux                       # 左法線（+90°回転）
    m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(lat))
    return (lat + (ly * meters) / M_PER_DEG_LAT,
            lon + (lx * meters) / m_per_deg_lon)


def main():
    ap = argparse.ArgumentParser(description="行き/帰りの同座標停留所を進行方向の反対側へ推定オフセット")
    ap.add_argument("gtfs_dir")
    ap.add_argument("--offset", type=float, default=15.0, help="帰りを反対側へずらす距離(m)。既定15")
    ap.add_argument("--same-thresh", type=float, default=5.0, help="同座標とみなす距離(m)。これ以内の分割ペアを対象")
    ap.add_argument("--manual", default=None, help="手動座標JSON。ここで確定済みの stop_id は動かさない")
    ap.add_argument("--report", default=None, help="推定した stop_id 一覧の出力先(JSON)")
    a = ap.parse_args()

    manual_ids = set()
    if a.manual and Path(a.manual).exists():
        try:
            _mj = json.loads(Path(a.manual).read_text(encoding="utf-8"))
            manual_ids = set(_mj.get("by_stop_id", {}))
        except Exception:
            manual_ids = set()

    d = Path(a.gtfs_dir)
    stops, sfields = _read(d / "stops.txt")
    st_rows, _ = _read(d / "stop_times.txt")
    trips, _ = _read(d / "trips.txt")

    coord = {}
    for s in stops:
        la, lo = _num(s.get("stop_lat")), _num(s.get("stop_lon"))
        coord[s["stop_id"]] = (la, lo) if la is not None and lo is not None else None
    name_of = {s["stop_id"]: (s.get("stop_name") or "").strip() for s in stops}
    trip_dir = {t["trip_id"]: str(t.get("direction_id") or "0") for t in trips}
    stop_dir = {}
    for st in st_rows:
        stop_dir.setdefault(st["stop_id"], trip_dir.get(st["trip_id"], "0"))

    # 便ごとの停車順（進行方向の推定用）
    seqs = defaultdict(list)
    for st in st_rows:
        try:
            seqs[st["trip_id"]].append((int(st["stop_sequence"]), st["stop_id"]))
        except (ValueError, KeyError):
            continue
    # 各 stop_id の進行方向ベクトル（east,north）を全便で平均（前→次を使う）
    tvec = defaultdict(lambda: [0.0, 0.0])
    for tid, seq in seqs.items():
        seq.sort()
        for i, (_sq, sid) in enumerate(seq):
            base = coord.get(sid)
            if not base:
                continue
            prev = coord.get(seq[i - 1][1]) if i > 0 else None
            nxt = coord.get(seq[i + 1][1]) if i < len(seq) - 1 else None
            a_pt = prev or base            # 端は自分を代用
            b_pt = nxt or base
            if a_pt == b_pt:
                continue
            m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(base[0]))
            east = (b_pt[1] - a_pt[1]) * m_per_deg_lon
            north = (b_pt[0] - a_pt[0]) * M_PER_DEG_LAT
            nrm = math.hypot(east, north)
            if nrm > 0:
                tvec[sid][0] += east / nrm
                tvec[sid][1] += north / nrm

    # 同名で「2方向・同座標」の分割ペアを対象にする
    byname = defaultdict(list)      # name -> [stop_id]
    for s in stops:
        byname[name_of[s["stop_id"]]].append(s["stop_id"])

    estimated = []                  # 動かした/確認が要る stop_id
    def hav(a_, b_):
        R = 6371000.0
        p1, p2 = math.radians(a_[0]), math.radians(b_[0])
        dp = math.radians(b_[0] - a_[0]); dl = math.radians(b_[1] - a_[1])
        x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * R * math.asin(math.sqrt(x))

    new_coord = {}
    for nm, ids in byname.items():
        dirs = {stop_dir.get(i, "0") for i in ids}
        if len(ids) != 2 or len(dirs) < 2:
            continue                # 2方向の分割ペアだけ
        if any(i in manual_ids for i in ids):
            continue                # 手動確定を含むペアは動かさない（手動最優先）
        c0, c1 = coord.get(ids[0]), coord.get(ids[1])
        if not c0 or not c1 or hav(c0, c1) > a.same_thresh:
            continue                # 既に別座標なら尊重（手動等）
        # 行き(方向0)はP11座標のまま信頼度を保ち、帰り(方向1)だけ反対側へ寄せて要確認にする
        for sid in ids:
            if stop_dir.get(sid, "0") != "1":
                continue
            base = coord.get(sid)
            ev, nv = tvec.get(sid, [0.0, 0.0])
            moved = offset_left(base[0], base[1], ev, nv, a.offset) if (ev or nv) else None
            if moved:
                new_coord[sid] = moved
            estimated.append({"stop_id": sid, "stop_name": nm,
                              "direction": stop_dir.get(sid, "0"), "moved": bool(moved)})

    # stops.txt を書き戻す（動かした座標のみ更新）
    for s in stops:
        if s["stop_id"] in new_coord:
            la, lo = new_coord[s["stop_id"]]
            s["stop_lat"] = f"{la:.6f}"
            s["stop_lon"] = f"{lo:.6f}"
    with open(d / "stops.txt", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sfields, lineterminator="\r\n")
        w.writeheader()
        for s in stops:
            w.writerow({k: s.get(k, "") for k in sfields})

    if a.report:
        Path(a.report).write_text(json.dumps(
            {"estimated_ids": [e["stop_id"] for e in estimated], "detail": estimated,
             "count": len(estimated), "moved": sum(1 for e in estimated if e["moved"])},
            ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] 反対側へ推定オフセット: 対象{len(estimated)}停留所 / 実移動{len(new_coord)}"
          f"（すべて要確認）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
