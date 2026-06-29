"""
classify_coord_confidence.py
============================
Step 3.5f（座標の信頼度分類）: 各停留所の最終座標を「確定／要確認／未補完」に分類する。
官公庁提出など誤りが許されない用途で、「推測座標を確定として黙って出さない」ための層。

判定（最終座標が**どの補完源**で付いたかをレポート突合で特定し，さらに経路整合を見る）:
  確定   : 公式/旧フィード再利用，手動，**経路整合を満たすP11完全一致**
  要確認 : P11あいまい一致(fuzzy/前後/部分)，Nominatim，内挿推定，同名候補，
           完全一致でも経路から外れる(同名誤マッチ疑い)，由来不明
  未補完 : 座標なし

経路整合: 便の前後の確定停留所を結ぶ線分への垂直距離が閾値内なら「経路上」とみなす
（reject_geom_outliers と同じ幾何。3.5d2 の大外れ閾値(3km)をすり抜ける中距離誤りも要確認に回す）。

Usage:
  python classify_coord_confidence.py <stops.txt> --stop-times <st.txt> --reports-dir <work>
      [--manual <manual_coords.json>] [--on-route-m 500] [-o 座標_信頼度.csv] [--report r.json]

License: Apache 2.0
"""
from __future__ import annotations

import argparse, csv, json, math, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reject_geom_outliers import _point_seg_dist  # 点→経路線分の距離(m)


def _haversine(a, b):
    """2点(lat,lon)間の距離[m]。"""
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _src_type(s):
    """ソース名を独立geocode種別に正規化（p11_exact/p11_fuzzy → p11）。"""
    if not s:
        return s
    return "p11" if s.startswith("p11") else s


def _round(v):
    try:
        return round(float(v), 5)
    except Exception:
        return None


def load_sources(reports: Path):
    """各 stop_id について [(source, lat5, lon5, detail)] を集める（最終座標の由来特定用）。"""
    src = defaultdict(list)

    def add(fn, source, latk="lat", lonk="lon", listk="matched", detailk=None):
        p = reports / fn
        if not p.exists():
            return
        d = json.loads(p.read_text(encoding="utf-8"))
        for r in d.get(listk, []):
            sid = r.get("stop_id")
            la, lo = _round(r.get(latk)), _round(r.get(lonk))
            if sid and la is not None and lo is not None:
                src[sid].append((source, la, lo, r.get(detailk) if detailk else None))

    add("merge_report.json", "official")                       # 3.5a 公式/旧feed再利用
    # P11 は strategy 付き
    p = reports / "p11_report.json"
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        for r in d.get("matched", []):
            sid = r.get("stop_id"); la, lo = _round(r.get("lat")), _round(r.get("lon"))
            if sid and la is not None:
                src[sid].append((f"p11_{r.get('strategy','?')}", la, lo, r.get("similarity")))
    add("nominatim_report.json", "nominatim")                  # 3.5c
    add("interpolate_report.json", "interpolated", listk="interpolated_estimated")  # 3.5e
    return src


def main() -> int:
    ap = argparse.ArgumentParser(description="座標の信頼度を確定/要確認/未補完に分類")
    ap.add_argument("input")
    ap.add_argument("--stop-times", required=True)
    ap.add_argument("--reports-dir", required=True)
    ap.add_argument("--manual", default=None, help="手動座標JSON(by_stop_name)。確定扱い")
    ap.add_argument("--on-route-m", type=float, default=500.0,
                    help="P11完全一致でも経路からこのm以上外れたら要確認(同名誤マッチ疑い)")
    ap.add_argument("--agree-m", type=float, default=100.0,
                    help="独立な複数ソースがこのm以内で一致したら確定に昇格(第3相)。0で無効")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--report", default="coord_confidence_report.json")
    a = ap.parse_args()

    in_path = Path(a.input)
    out_path = Path(a.output) if a.output else in_path.parent / "座標_信頼度.csv"
    rows = list(csv.DictReader(in_path.open(encoding="utf-8-sig")))
    by_id = {r["stop_id"]: r for r in rows}

    def coord(sid):
        r = by_id.get(sid)
        if r and (r.get("stop_lat") or "").strip() and (r.get("stop_lon") or "").strip():
            return (float(r["stop_lat"]), float(r["stop_lon"]))
        return None

    # 経路整合: 各停留所の便前後の既知からの off-route 最小値
    trips = defaultdict(list)
    with Path(a.stop_times).open(encoding="utf-8-sig") as f:
        seq = [(x["trip_id"], int(x["stop_sequence"]), x["stop_id"]) for x in csv.DictReader(f)]
    for tid, _, sid in sorted(seq, key=lambda x: (x[0], x[1])):
        trips[tid].append(sid)
    offroute = {}
    for ids in trips.values():
        known = [(k, s) for k, s in enumerate(ids) if coord(s)]
        for k, sid in enumerate(ids):
            c = coord(sid)
            if not c:
                continue
            pred = max([(kk, ss) for kk, ss in known if kk < k and ss != sid], default=None)
            succ = min([(kk, ss) for kk, ss in known if kk > k and ss != sid], default=None)
            if pred and succ:
                dd = _point_seg_dist(c, coord(pred[1]), coord(succ[1]))
                offroute[sid] = min(offroute.get(sid, dd), dd)

    sources = load_sources(Path(a.reports_dir))
    manual_names = set()
    if a.manual and Path(a.manual).exists():
        manual_names = set(json.loads(Path(a.manual).read_text(encoding="utf-8")).get("by_stop_name", {}))

    def final_source(sid):
        """最終座標(lat,lon)に一致する由来を返す。"""
        c = coord(sid)
        if not c:
            return None
        la, lo = round(c[0], 5), round(c[1], 5)
        cands = sources.get(sid, [])
        for s, sla, slo, det in cands:
            if abs(sla - la) < 1e-4 and abs(slo - lo) < 1e-4:
                return s
        return cands[-1][0] if cands else "unknown"

    def agree_types(sid, c):
        """最終座標cの周辺agree_m以内にある独立geocode種別の集合（内挿は除外）。"""
        if not c or a.agree_m <= 0:
            return set()
        types = set()
        for s, sla, slo, det in sources.get(sid, []):
            if s == "interpolated":   # 内挿は隣接からの推定で独立geocodeでない
                continue
            if _haversine(c, (sla, slo)) <= a.agree_m:
                types.add(_src_type(s))
        return types

    out, counts = [], defaultdict(int)
    for r in rows:
        sid = r["stop_id"]; nm = r.get("stop_name", "")
        c = coord(sid)
        if not c:
            conf, src, reason = "未補完", "-", "座標なし"
        else:
            src = "manual" if nm in manual_names else final_source(sid)
            orm = offroute.get(sid)
            if src in ("official", "manual"):
                conf, reason = "確定", ("公式/旧feed再利用" if src == "official" else "手動指定")
            elif src == "p11_exact":
                if orm is None or orm <= a.on_route_m:
                    conf, reason = "確定", "P11完全一致(経路整合)"
                else:
                    conf, reason = "要確認", f"P11完全一致だが経路から{round(orm)}m外れ(同名誤マッチ疑い)"
            elif src and src.startswith("p11_"):
                conf, reason = "要確認", f"P11あいまい一致({src[4:]})"
            elif src == "nominatim":
                conf, reason = "要確認", "Nominatim(OSM)補完"
            elif src == "interpolated":
                conf, reason = "要確認", "経路内挿の推定座標"
            else:
                conf, reason = "要確認", "補完源不明"
            # 第3相: 独立な複数ソースが近接一致したら確定へ昇格（精度を落とさず要確認を減らす）。
            # ただし経路から外れている場合(同名誤マッチ疑い)は安全側で据え置く。
            if conf == "要確認" and "経路から" not in reason:
                orm = offroute.get(sid)
                if orm is None or orm <= a.on_route_m:
                    types = agree_types(sid, c)
                    if len(types) >= 2:
                        conf = "確定"
                        reason = f"複数ソース一致({'+'.join(sorted(types))}, ≤{int(a.agree_m)}m)"
        counts[conf] += 1
        out.append({"stop_id": sid, "stop_name": nm,
                    "stop_lat": r.get("stop_lat", ""), "stop_lon": r.get("stop_lon", ""),
                    "confidence": conf, "source": src,
                    "off_route_m": round(offroute[sid]) if sid in offroute else "",
                    "reason": reason})

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["stop_id", "stop_name", "stop_lat", "stop_lon",
                                          "confidence", "source", "off_route_m", "reason"],
                           lineterminator="\r\n")
        w.writeheader()
        for x in out:
            w.writerow(x)
    Path(a.report).write_text(json.dumps(
        {"counts": dict(counts), "total": len(rows), "rows": out}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print(f"座標信頼度: 確定 {counts['確定']} / 要確認 {counts['要確認']} / 未補完 {counts['未補完']}（計{len(rows)}）")
    for x in out:
        if x["confidence"] != "確定":
            print(f"  [{x['confidence']}] {x['stop_name']}: {x['reason']}")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
