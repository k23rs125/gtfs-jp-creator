# -*- coding: utf-8 -*-
"""抽出した時刻表(extract.json)を、利用者が原典と見比べて修正できる CSV に書き出す。

目的: 時刻が合っていない場合に、利用者が Excel 等で直接直せるようにする（正しく失敗）。
- ブロックごとに「停留所(行) × 便(列)」のグリッド CSV を出力。セル=HH:MM、空欄=通過。
- OCR誤読の疑い(detect_time_anomalies)は _疑い一覧.csv に列挙して、どこを見ればよいか示す。
- 停留所の列・順序は編集しない前提（apply 時は行の位置で対応づける）。

修正した CSV は apply_timetable_review.py で extract.json に決定的に反映する。

使い方: python export_timetable_review.py extract.json -o review_dir/
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

try:
    from detect_time_anomalies import detect_anomalies
except Exception:
    detect_anomalies = None

_BADCHARS = re.compile(r'[\\/:*?"<>|\s]+')
_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


def _safe(s: str) -> str:
    return _BADCHARS.sub("_", (s or "").strip()).strip("_")


def _hhmm(t: str) -> str:
    """'8:43:00' や '08:43' → 'HH:MM'。時刻でなければ空。"""
    m = _HHMM.match(str(t or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def trip_labels(trips):
    """便ラベル（表示用・重複は連番で一意化）。apply は位置で対応づけるので順序が命。"""
    base = []
    for j, t in enumerate(trips):
        tn = t.get("trip_number")
        if t.get("label"):
            lab = str(t["label"])
        elif tn:
            tn = str(tn).strip()
            lab = tn if "便" in tn else f"{tn}便"   # 「第1便」は既に便を含む→二重付与しない
        else:
            lab = f"便{j + 1}"
        base.append(lab)
    seen, out = {}, []
    for lab in base:
        if lab in seen:
            seen[lab] += 1
            out.append(f"{lab}({seen[lab]})")
        else:
            seen[lab] = 1
            out.append(lab)
    return out


def block_grid(block):
    """(labels, rows) を返す。rows[i] = {停留所, label1, ...}。便セルは停留所名で順に整列。"""
    stops = [s.get("name") for s in block.get("stops", [])]
    trips = block.get("trips", [])
    labels = trip_labels(trips)
    per_trip = []
    for t in trips:
        cells = t.get("cells", [])
        k, mp = 0, {}
        for i, sn in enumerate(stops):
            if k < len(cells) and cells[k].get("name") == sn:
                mp[i] = _hhmm(cells[k].get("time"))
                k += 1
        per_trip.append(mp)
    rows = []
    for i, sn in enumerate(stops):
        row = {"停留所": sn}
        for j, lab in enumerate(labels):
            row[lab] = per_trip[j].get(i, "")
        rows.append(row)
    return labels, rows


def main():
    ap = argparse.ArgumentParser(description="extract.json を修正用CSV(停留所×便)に書き出す")
    ap.add_argument("input", help="extract.json")
    ap.add_argument("-o", "--output-dir", required=True, help="出力フォルダ")
    ap.add_argument("--threshold", type=int, default=5, help="疑い検出の逸脱しきい値(分)")
    a = ap.parse_args()

    ext = json.loads(Path(a.input).read_text(encoding="utf-8"))
    outdir = Path(a.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    blocks = ext.get("blocks", [])

    written = []
    for b in blocks:
        bi = b.get("block_index")
        if not b.get("trips"):
            continue
        labels, rows = block_grid(b)
        dh = _safe(b.get("direction_hint") or "")
        fn = f"block{int(bi):02d}" + (f"_{dh}" if dh else "") + ".csv"
        path = outdir / fn
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["停留所"] + labels)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        written.append((fn, len(rows), len(labels)))

    # 疑い一覧（OCR誤読の可能性）。どこを重点的に見ればよいかの手がかり。
    n_an = 0
    if detect_anomalies is not None:
        an = detect_anomalies(ext, a.threshold)
        n_an = len(an)
        with open(outdir / "_疑い一覧.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["block", "便", "停留所", "現在の時刻", "候補", "確度", "理由"])
            for x in an:
                w.writerow([x.get("block"), x.get("trip_label") or "", x.get("stop_name"),
                            (x.get("current") or "")[:5],
                            (x.get("suggested") or "")[:5] if x.get("suggested") else "",
                            x.get("confidence"), x.get("reason")])

    readme = (
        "【時刻表の確認・修正手順】\n"
        "1. block*.csv を Excel 等で開き、原典（紙・PDF）と見比べてください。\n"
        "2. 間違っている時刻だけ直します。形式は HH:MM（例 08:05）。空欄＝通過（その便は通らない）。\n"
        "3. 1行目のヘッダ、左端『停留所』列、行の順序・数は変えないでください（列＝便の順序も固定）。\n"
        "4. CSVのまま保存してください（文字コードはそのまま）。\n"
        "5. apply_timetable_review.py でこのフォルダを指定すると extract.json に反映されます。\n"
        f"\n・_疑い一覧.csv（{n_an}件）: OCR誤読の可能性がある時刻です。まずここを確認してください。\n"
        "・自動では書き換えません。確定するのは利用者です（正しく失敗の原則）。\n"
    )
    (outdir / "_README.txt").write_text(readme, encoding="utf-8")

    print(f"[OK] {len(written)} ブロックを書き出し: {outdir}")
    for fn, nr, nc in written:
        print(f"  {fn}  停留所{nr} × 便{nc}")
    print(f"[INFO] 疑い一覧: {n_an}件  → {outdir/'_疑い一覧.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
