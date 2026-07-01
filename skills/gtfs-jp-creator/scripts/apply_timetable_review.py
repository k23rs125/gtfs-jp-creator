# -*- coding: utf-8 -*-
"""修正済みの時刻CSV(export_timetable_review.py が出力)を extract.json に決定的に反映する。

- ブロックごとの CSV を、ファイル名の block番号で対応づける。
- 列(便)は位置で、行(停留所)は位置で対応づける（export と同じ順序前提）。
- セルが HH:MM なら時刻、空欄なら通過。各便の cells を作り直す（アプリのエディタと同じロジック）。
- 変更点を差分として報告。形式不正・行数/便数不一致のブロックは適用せず警告（正しく失敗）。

使い方: python apply_timetable_review.py extract.json review_dir/ -o extract_fixed.json
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})")


def _norm(v):
    m = _TIME.match(str(v or ""))
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}:00"


def _hhmm(v):
    """比較・表示用 'HH:MM'（ゼロ埋め正規化）。時刻でなければ空。"""
    m = _TIME.match(str(v or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def _read_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return None, []
    return rows[0], rows[1:]


def apply_reviews(ext, review_dir):
    blocks = {int(b.get("block_index")): b for b in ext.get("blocks", [])}
    changes, warnings = [], []
    files = sorted(Path(review_dir).glob("block*.csv"))
    for path in files:
        m = re.search(r"block0*(\d+)", path.name)
        if not m:
            continue
        bi = int(m.group(1))
        b = blocks.get(bi)
        if b is None:
            warnings.append(f"{path.name}: block{bi} が extract に無いためスキップ")
            continue
        header, data = _read_csv(path)
        if not header or header[0].strip() not in ("停留所", "﻿停留所"):
            warnings.append(f"{path.name}: 先頭列が『停留所』でないためスキップ")
            continue
        trips = b.get("trips", [])
        n_trip_cols = len(header) - 1
        if n_trip_cols != len(trips):
            warnings.append(f"{path.name}: 便の列数({n_trip_cols})が抽出の便数({len(trips)})と不一致→スキップ")
            continue
        stops = [s.get("name") for s in b.get("stops", [])]
        if len(data) != len(stops):
            warnings.append(f"{path.name}: 行数({len(data)})が停留所数({len(stops)})と不一致→スキップ")
            continue
        # 便ごとに cells を作り直す（列位置 j → trips[j]、行位置 i → stops[i]）
        for j, t in enumerate(trips):
            old = {c.get("name"): (c.get("time") or "") for c in t.get("cells", [])}
            newcells = []
            for i, sn in enumerate(stops):
                val = data[i][j + 1] if j + 1 < len(data[i]) else ""
                tm = _norm(val)
                if tm is None:
                    if str(val).strip():   # 非空だが時刻(HH:MM)でない＝誤入力の疑い→通過扱いだが警告
                        warnings.append(f"{path.name}: {header[j + 1]} {sn} の値『{val}』は"
                                        "時刻(HH:MM)でないため通過扱い→要確認")
                    continue
                newcells.append({"seq": len(newcells) + 1, "num": None,
                                 "name": sn, "time": tm,
                                 "reserve": "要予約" in sn})
            # 差分検出
            new = {c["name"]: c["time"] for c in newcells}
            for sn in set(old) | set(new):
                o, nv = _hhmm(old.get(sn, "")), _hhmm(new.get(sn, ""))
                if o != nv:   # ゼロ埋め等の見かけ差は無視、実質変更のみ報告
                    changes.append({"block": bi, "trip": header[j + 1], "stop": sn,
                                    "before": o or "(通過)", "after": nv or "(通過)"})
            t["cells"] = newcells
            t["n_stops"] = len(newcells)
    return changes, warnings


def main():
    ap = argparse.ArgumentParser(description="修正済みCSVを extract.json に反映")
    ap.add_argument("extract", help="元の extract.json")
    ap.add_argument("review_dir", help="修正済みCSVのフォルダ")
    ap.add_argument("-o", "--output", required=True, help="出力 extract.json")
    a = ap.parse_args()

    ext = json.loads(Path(a.extract).read_text(encoding="utf-8"))
    changes, warnings = apply_reviews(ext, a.review_dir)
    Path(a.output).write_text(json.dumps(ext, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] 反映しました: {a.output}")
    print(f"[INFO] 変更セル {len(changes)} 件")
    for c in changes[:40]:
        print(f"  block{c['block']} {c['trip']} {c['stop']}: {c['before']} → {c['after']}")
    if len(changes) > 40:
        print(f"  ...ほか {len(changes) - 40} 件")
    for w in warnings:
        print(f"[警告] {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
