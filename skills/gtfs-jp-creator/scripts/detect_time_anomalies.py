# -*- coding: utf-8 -*-
"""抽出した時刻表(extract.json)から、OCR誤読が疑われる時刻セルを検出し修正候補を出す。

OCR(MinerU等)は数字を読み違える（例: 08:28 → 09:33）。これを「漏れなく見つけて直しやすく」する。
- 便内の時刻逆行（monotonic 違反）
- 便間パターンからの外れ: 同じ停留所区間(A→B)の所要分は各便でほぼ一定のはず。
  前後の停留所と区間中央値から各セルの「期待時刻」を求め、大きく外れるセルを疑う。
自動では書き換えない。修正候補(suggested)を出して人が確認・確定する（＝推測せず確認）。

出力: [{block, trip_label, trip_col, seq, stop_name, current, suggested, reason}, ...]
使い方: python detect_time_anomalies.py extract.json -o anomalies.json
"""
import argparse
import json
import statistics
import sys
from pathlib import Path


def _to_min(t: str):
    p = (t or "").split(":")
    if len(p) < 2:
        return None
    try:
        return int(p[0]) * 60 + int(p[1])
    except ValueError:
        return None


def _fmt(minute: int):
    h, m = divmod(int(round(minute)), 60)
    return f"{h:02d}:{m:02d}:00"


def detect_anomalies(extract: dict, dev_threshold: int = 5):
    out = []
    for b in extract.get("blocks", []):
        trips = b.get("trips", [])
        # 区間(A→B)ごとの所要分の中央値（2便以上で観測できたもののみ）
        pair_intervals = {}
        for t in trips:
            cells = t.get("cells", [])
            for i in range(1, len(cells)):
                a, c = cells[i - 1], cells[i]
                ma, mc = _to_min(a.get("time")), _to_min(c.get("time"))
                if ma is None or mc is None:
                    continue
                pair_intervals.setdefault((a.get("name"), c.get("name")), []).append(mc - ma)
        median_iv = {k: statistics.median(v) for k, v in pair_intervals.items() if len(v) >= 2}

        for t in trips:
            cells = t.get("cells", [])
            mins = [_to_min(c.get("time")) for c in cells]
            for i, c in enumerate(cells):
                if mins[i] is None:
                    continue
                exp = []
                if i > 0 and mins[i - 1] is not None:
                    k = (cells[i - 1].get("name"), c.get("name"))
                    if k in median_iv:
                        exp.append(mins[i - 1] + median_iv[k])
                if i < len(cells) - 1 and mins[i + 1] is not None:
                    k = (c.get("name"), cells[i + 1].get("name"))
                    if k in median_iv:
                        exp.append(mins[i + 1] - median_iv[k])
                backward = (i > 0 and mins[i - 1] is not None and mins[i] < mins[i - 1]) or \
                           (i < len(cells) - 1 and mins[i + 1] is not None and mins[i] > mins[i + 1])
                if not exp:
                    # パターン無しでも逆行は必ず拾う（候補は出せない）
                    if backward:
                        out.append(_rec(b, t, i, c, None, "時刻逆行", "low"))
                    continue
                two_sided = len(exp) == 2
                agree = (not two_sided) or abs(exp[0] - exp[1]) <= 3
                expv = statistics.median(exp)
                dev = abs(mins[i] - expv)
                # 確度の高い候補: 前後の予測が一致(両側)し大きく外れる、または逆行で両側が一致
                if two_sided and agree and (dev >= dev_threshold or backward):
                    out.append(_rec(b, t, i, c, _fmt(expv),
                                    "便間パターンから外れ" + ("・時刻逆行" if backward else ""), "high"))
                elif backward:
                    # 逆行だが両側予測が無い/食い違う → 要確認(候補は参考値)
                    out.append(_rec(b, t, i, c, _fmt(expv) if agree else None, "時刻逆行", "low"))
    return out


def _rec(b, t, i, c, suggested, reason, confidence):
    return {
        "block": b.get("block_index"),
        "trip_label": t.get("label") or (f"{t.get('trip_number')}便" if t.get("trip_number") else None),
        "trip_col": t.get("col"),
        "seq": i + 1,
        "stop_name": c.get("name"),
        "current": c.get("time"),
        "suggested": suggested,
        "confidence": confidence,
        "reason": reason,
    }


def main():
    ap = argparse.ArgumentParser(description="extract.json の時刻アノマリ検出＋修正候補")
    ap.add_argument("input", help="extract.json")
    ap.add_argument("-o", "--output", help="出力 anomalies.json（省略時は標準出力に要約）")
    ap.add_argument("--threshold", type=int, default=5, help="期待時刻からの逸脱しきい値(分)")
    a = ap.parse_args()
    ext = json.loads(Path(a.input).read_text(encoding="utf-8"))
    an = detect_anomalies(ext, a.threshold)
    if a.output:
        Path(a.output).write_text(json.dumps(an, ensure_ascii=False, indent=2), encoding="utf-8")
    hi = sum(1 for x in an if x.get("confidence") == "high")
    print(f"[OK] 時刻アノマリ {len(an)} 件（確度高 {hi} / 要確認 {len(an) - hi}）")
    for x in an[:30]:
        sug = f"→ 候補 {x['suggested']}" if x.get("suggested") else "→ 候補なし(要確認)"
        print(f"  [{x.get('confidence')}] block{x['block']} {x['trip_label']} seq{x['seq']} "
              f"{x['stop_name']}: {x['current']} {sug} ({x['reason']})")


if __name__ == "__main__":
    sys.exit(main())
