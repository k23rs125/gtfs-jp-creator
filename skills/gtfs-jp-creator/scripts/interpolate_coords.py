"""
interpolate_coords.py
=====================

Step 3.5e (経路順内挿による「推定座標」補完): 旧feed/P11/Nominatim/手動でも埋まらなかった
停留所を、便の停留所順で前後の既知座標から内挿して埋める。

設計方針（「正しく失敗」の延長）:
    - 内挿値は **推定（要確認）** であり、正確な座標ではない（誤差は隣接間隔の約半分・
      中央値146m・9割が500m以内という実測に基づく）。レポートで明示し、利用者の確認に回す。
    - 対象自治体の範囲(bbox)外に出た内挿は **外れ値** として採用せず未補完のまま残す
      （誤った種が伝播した可能性。座標補完評価.tex 参照）。
    - 既定では空座標のみを埋める（既存座標は変更しない）。

Usage:
    python interpolate_coords.py <stops.txt> --stop-times <stop_times.txt>
        [-o <output.txt>] [--report <report.json>]
        [--municipality "沖縄県うるま市"] [--bbox lon_min,lat_min,lon_max,lat_max]

License: Apache 2.0
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

# 同ディレクトリの enrich_stops_p11 から自治体bbox取得を流用
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from enrich_stops_p11 import fetch_municipality_bbox
except Exception:  # noqa: BLE001
    fetch_municipality_bbox = None


def read_stops(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    return rows, fields


def write_stops(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\r\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def has_coord(r: dict) -> bool:
    return bool((r.get("stop_lat") or "").strip()) and bool((r.get("stop_lon") or "").strip())


def main() -> int:
    ap = argparse.ArgumentParser(description="経路順内挿で未補完停留所を推定座標で埋める")
    ap.add_argument("input", help="座標を埋めたい stops.txt")
    ap.add_argument("--stop-times", required=True, help="便の停留所順を持つ stop_times.txt")
    ap.add_argument("-o", "--output", default=None, help="出力 stops.txt（既定: <input>.interp.txt）")
    ap.add_argument("--report", default="interpolate_report.json", help="レポート出力先")
    ap.add_argument("--municipality", default=None,
                    help="自治体名。範囲bboxを取得し、市域外に出た内挿を外れ値として棄却")
    ap.add_argument("--bbox", default=None, help="外れ値ガード範囲 (lon_min,lat_min,lon_max,lat_max)")
    ap.add_argument("--municipality-margin", type=float, default=0.04)
    args = ap.parse_args()

    in_path = Path(args.input)
    st_path = Path(args.stop_times)
    out_path = Path(args.output) if args.output else in_path.with_suffix(".interp.txt")
    if not in_path.exists() or not st_path.exists():
        print("Error: input / stop_times が見つかりません", file=sys.stderr)
        return 1

    rows, fields = read_stops(in_path)
    by_id = {r.get("stop_id"): r for r in rows}
    known = {sid: (float(r["stop_lat"]), float(r["stop_lon"]))
             for sid, r in by_id.items() if has_coord(r)}

    # 外れ値ガード bbox
    bbox = None
    if args.bbox:
        p = [float(x) for x in args.bbox.split(",")]
        if len(p) == 4:
            bbox = tuple(p)
    if bbox is None and args.municipality and fetch_municipality_bbox:
        bbox = fetch_municipality_bbox(args.municipality, args.municipality_margin)
        if bbox:
            print(f"外れ値ガード bbox({args.municipality}): "
                  f"({bbox[0]:.4f},{bbox[1]:.4f},{bbox[2]:.4f},{bbox[3]:.4f})")

    def in_bbox(lat, lon):
        if not bbox:
            return True
        return bbox[1] <= lat <= bbox[3] and bbox[0] <= lon <= bbox[2]

    # 便ごとの停留所順
    trips: dict[str, list[str]] = defaultdict(list)
    seqkey = []
    with st_path.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            seqkey.append((r["trip_id"], int(r["stop_sequence"]), r["stop_id"]))
    for tid, _, sid in sorted(seqkey, key=lambda x: (x[0], x[1])):
        trips[tid].append(sid)

    # 内挿（各便で最も近い既知の前後から、停留所順インデックスで内挿）
    est: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for seq in trips.values():
        coords = [(k, sid) for k, sid in enumerate(seq) if sid in known]
        for k, sid in enumerate(seq):
            if sid in known:
                continue
            pred = max([(kk, ss) for kk, ss in coords if kk < k], default=None)
            succ = min([(kk, ss) for kk, ss in coords if kk > k], default=None)
            if pred and succ:
                (kj, sj), (kk2, sk) = pred, succ
                la_j, lo_j = known[sj]
                la_k, lo_k = known[sk]
                fr = (k - kj) / (kk2 - kj)
                est[sid].append((la_j + fr * (la_k - la_j), lo_j + fr * (lo_k - lo_j)))

    interpolated, flagged, still_empty = [], [], []
    for sid, r in by_id.items():
        if has_coord(r):
            continue
        if sid in est:
            la = round(statistics.median(p[0] for p in est[sid]), 6)
            lo = round(statistics.median(p[1] for p in est[sid]), 6)
            if in_bbox(la, lo):
                r["stop_lat"] = f"{la:.6f}"
                r["stop_lon"] = f"{lo:.6f}"
                interpolated.append({"stop_id": sid, "stop_name": r.get("stop_name"),
                                     "lat": la, "lon": lo, "n_trips": len(est[sid])})
            else:
                flagged.append({"stop_id": sid, "stop_name": r.get("stop_name"),
                                "lat": la, "lon": lo, "reason": "市域外（外れ値）"})
                still_empty.append(sid)
        else:
            still_empty.append(sid)

    write_stops(out_path, rows, fields)

    report = {
        "summary": {
            "total": len(rows), "known_before": len(known),
            "interpolated_estimated": len(interpolated),
            "flagged_outlier": len(flagged),
            "still_empty": len([s for s in still_empty if s not in [f["stop_id"] for f in flagged]]),
        },
        "note": "interpolated_estimated は推定座標（要確認）。誤差は中央値約146m・9割が500m以内。",
        "interpolated_estimated": interpolated,
        "flagged_outlier": flagged,
        "bbox": list(bbox) if bbox else None,
    }
    Path(args.report).write_text(__import__("json").dumps(report, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

    print("=" * 60)
    print("経路内挿（推定座標）レポート")
    print("=" * 60)
    print(f"  既知（前段まで）: {len(known)}")
    print(f"  内挿で補完（推定・要確認）: {len(interpolated)}")
    print(f"  外れ値で棄却（市域外）: {len(flagged)}")
    print(f"  なお未補完: {report['summary']['still_empty']}")
    if interpolated:
        print("  [推定・要確認] 内挿で埋めた停留所（原典/地図で確認を）:")
        for it in interpolated[:15]:
            print(f"    ~ {it['stop_name']} -> {it['lat']},{it['lon']}")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
