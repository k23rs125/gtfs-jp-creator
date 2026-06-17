#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_timetable_excel.py
==========================
Step1 (Excel経路): Excel(.xlsx)のバス時刻表から停留所名・時刻を抽出し、
座標方式(extract_timetable_coords.py)と同じ「抽出JSON」形式で出力する。
以降の Step2(構造化)・Step3〜7(run_pipeline) は PDF と完全に共通化できる。

なぜ Excel を直接読むか:
  Excel は機械可読(セルが既にグリッド構造)なので、PDFの座標クラスタリングやOCRが
  不要で、最も確実・高精度に時刻表を読める。元データが Excel/Office にあるなら、
  PDF化せずそのまま読むのが「利用者の負担を減らし正確なデータを作る」設計に合う。

想定する標準レイアウト(よくあるバス時刻表):
  - 1列が「停留所名」（縦に停留所が並ぶ）
  - その右の複数列が「便」（各列=1便、セルが時刻 HH:MM）
  - 便名/行先などの見出しは時刻行の上にあってよい(任意)
  自動検出するが、外れる場合は --name-col / --header-rows / --sheet で上書き可。

設計方針(正しく失敗する):
  - 機械的に決められること(停留所名・時刻・便の並び)だけを行う。
  - 便名・方向・循環の解釈は行わない(Step2のLLM判断に委ねる)。
  - 構造が読めない/曖昧なときは推測せず warnings/needs_confirmation に明記。

Usage:
  python extract_timetable_excel.py <input.xlsx> -o <out.json>
        [--sheet <name>] [--name-col <Aや3>] [--header-rows <N>]

License: Apache 2.0
"""
import argparse
import datetime
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import openpyxl
except ImportError:
    print("Error: openpyxl が必要です (pip install openpyxl)", file=sys.stderr)
    sys.exit(1)

TIME_RE = re.compile(r'^\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*$')
ICON_PREFIX = re.compile(r'^[店駅　\s]+')
DIRECTION_SUFFIX = re.compile(r'[（(](?:[東西南北]行き?|上り|下り|のりば\d*|\d+番のりば)[）)]\s*$')


def normalize_name(s: str) -> str:
    s = ICON_PREFIX.sub('', str(s)).strip()
    s = DIRECTION_SUFFIX.sub('', s).strip()
    return s


def cell_time(v):
    """セル値を 'H:MM:00' に正規化。時刻でなければ None。"""
    if isinstance(v, datetime.datetime):
        return f"{v.hour}:{v.minute:02d}:00"
    if isinstance(v, datetime.time):
        return f"{v.hour}:{v.minute:02d}:00"
    if isinstance(v, str):
        m = TIME_RE.match(v)
        if m:
            return f"{int(m.group(1))}:{m.group(2)}:00"
    return None


def is_name_cell(v) -> bool:
    """停留所名らしいセルか（文字列・時刻でない・純数値でない）。"""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not s or cell_time(s) or s.isdigit():
        return False
    return True


def col_letter_to_idx(s: str) -> int:
    """'A'->1, 'B'->2 ... 数字ならそのまま int。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    n = 0
    for ch in s.upper():
        n = n * 26 + (ord(ch) - ord('A') + 1)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Excel時刻表から停留所・時刻を抽出（PDF座標方式と同じJSON形式で出力）")
    ap.add_argument("input", help="入力 .xlsx")
    ap.add_argument("-o", "--output", required=True, help="出力JSON")
    ap.add_argument("--sheet", default=None, help="シート名（既定: 先頭シート）")
    ap.add_argument("--name-col", default=None, help="停留所名の列（A や 3）。未指定で自動検出")
    ap.add_argument("--header-rows", type=int, default=None,
                    help="先頭の見出し行数（未指定で自動）")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"Error: 入力が見つかりません: {in_path}", file=sys.stderr)
        return 1

    wb = openpyxl.load_workbook(in_path, data_only=True)
    ws = wb[args.sheet] if args.sheet else wb[wb.sheetnames[0]]

    # --- 全セルを読む ---
    cells = {}
    for row in ws.iter_rows():
        for c in row:
            if c.value is not None and str(c.value).strip() != "":
                cells[(c.row, c.column)] = c.value
    if not cells:
        result = {"source": str(in_path), "sheet": ws.title, "blocks": [],
                  "warnings": ["シートが空です。"], "needs_confirmation": []}
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        print("[警告] シートが空です。", file=sys.stderr)
        return 0

    # --- スケジュール時刻セルと便(列) ---
    # 時刻型セルのうち「実時刻(hour>=4)」だけを便の時刻とみなす。所要時間列
    # (例: I/J/L/M列の 0:01, 0:05 など hour=0)を便の時刻と誤検出しないため。
    sched_cells = {}
    for (r, c), v in cells.items():
        t = cell_time(v)
        if t and int(t.split(":")[0]) >= 4:
            sched_cells[(r, c)] = t
    if not sched_cells:
        result = {"source": str(in_path), "sheet": ws.title, "blocks": [],
                  "warnings": ["実時刻(4:00以降)のセルが見つかりません。レイアウト/シートを確認してください。"],
                  "needs_confirmation": [{"type": "no_time_cells",
                      "message": "便の時刻セル(4:00以降のHH:MM)が無いため抽出できません。--sheet で正しいシートを指定するか、時刻が HH:MM 形式か確認してください。"}]}
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        print("[警告] 便の時刻セルが見つかりません。", file=sys.stderr)
        return 0

    col_sched_count = Counter(c for (r, c) in sched_cells)
    trip_cols = sorted([c for c, n in col_sched_count.items() if n >= 2])
    if not trip_cols:
        trip_cols = sorted(col_sched_count)  # 各列1便しか無い場合も拾う

    # --- 停留所名の列（自動検出 or 指定） ---
    if args.name_col:
        name_col = col_letter_to_idx(args.name_col)
    else:
        # 便列より左で、停留所名セルが最も多い列
        left_name_counts = Counter(c for (r, c), v in cells.items()
                                   if is_name_cell(v) and c < min(trip_cols))
        if not left_name_counts:
            # 便列の左に名前列が無い → 全体で最多の名前列
            left_name_counts = Counter(c for (r, c), v in cells.items() if is_name_cell(v))
        name_col = left_name_counts.most_common(1)[0][0]

    # --- 停留所行（name_col に名前があり、時刻行と対応する行） ---
    row_name = {}
    for (r, c), v in cells.items():
        if c == name_col and is_name_cell(v):
            row_name[r] = normalize_name(v)
    stop_rows = sorted(row_name)

    warnings_list = []

    # --- セクション(縦表)検出: 便番号ヘッダ行で分割 ---
    # 便列の位置に小整数(便番号)が並び、実時刻が無い行を「便番号ヘッダ行」とみなす。
    # その行を境に、同じ列を共有して縦に積まれた別方向の表(例: 上り/下り)を分割する。
    def _is_int_cell(v):
        if isinstance(v, bool):
            return False
        if isinstance(v, int):
            return 0 < v < 1000
        if isinstance(v, str) and v.strip().isdigit():
            return 0 < int(v.strip()) < 1000
        return False

    header_rows = []
    for r in range(1, ws.max_row + 1):
        ints = sum(1 for tc in trip_cols
                   if _is_int_cell(cells.get((r, tc))) and (r, tc) not in sched_cells)
        has_sched = any((r, tc) in sched_cells for tc in trip_cols)
        if ints >= 2 and not has_sched:
            header_rows.append(r)
    header_rows.sort()

    # 各セクションの範囲: (ヘッダ行, 開始行, 終了行)。ヘッダが無ければ単一表。
    if header_rows:
        bounds = header_rows + [ws.max_row + 1]
        sections = [(header_rows[i], header_rows[i], bounds[i + 1])
                    for i in range(len(header_rows))]
    else:
        sections = [(None, 0, ws.max_row + 1)]

    def _m(t):
        h, m, *_ = t.split(":")
        return int(h) * 60 + int(m)

    blocks = []
    needs = []
    all_served = set()
    for hdr, r_lo, r_hi in sections:
        sec_rows = sorted(r for r in row_name
                          if r_lo <= r < r_hi and (hdr is None or r > hdr))
        trips = []
        for tc in trip_cols:
            cell_list = []
            seq = 0
            for r in sec_rows:
                if (r, tc) in sched_cells:
                    seq += 1
                    nm = row_name[r]
                    cell_list.append({"seq": seq, "num": None, "name": nm,
                                      "time": sched_cells[(r, tc)],
                                      "reserve": "要予約" in nm})
            if cell_list:
                mins = [_m(c["time"]) for c in cell_list]
                mono = all(mins[i] <= mins[i + 1] for i in range(len(mins) - 1))
                tnum = cells.get((hdr, tc)) if hdr is not None else None
                trips.append({"col": tc,
                              "trip_number": str(tnum) if tnum is not None else None,
                              "n_stops": len(cell_list), "monotonic": mono,
                              "cells": cell_list})
        served = sorted({r for r in sec_rows
                         if any((r, tc) in sched_cells for tc in trip_cols)})
        all_served |= set(served)
        if not trips:
            continue
        stops = [{"num": None, "name": row_name[r], "row": r,
                  "reserve": "要予約" in row_name[r]} for r in served]
        bi = len(blocks)
        blocks.append({"block_index": bi, "name_col": name_col, "section_row": hdr,
                       "stops": stops, "trips": trips, "warnings": []})
        for t in trips:
            if not t["monotonic"]:
                needs.append({"type": "time_nonmonotonic", "block": bi, "col": t["col"],
                              "message": f"ブロック{bi} 便(列{t['col']})で時刻が逆行しています。"
                                         "要予約への寄り道や折り返しの可能性。原典で確認してください。"})
    needs.append({"type": "assign_required",
                  "message": "便名・方向(direction_id)・循環の展開は表構造から確定できません。原典と照合して割り当ててください(Step2)。"})

    result = {"source": str(in_path), "sheet": ws.title,
              "blocks": blocks, "warnings": warnings_list,
              "needs_confirmation": needs}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

    # サマリ（cp932安全・絵文字なし）
    total_trips = sum(len(b["trips"]) for b in blocks)
    print(f"[INFO] シート: {ws.title}", file=sys.stderr)
    print(f"[INFO] 停留所名の列: {name_col}列  便(時刻列): {len(trip_cols)}列  "
          f"セクション(表): {len(blocks)}", file=sys.stderr)
    print(f"[INFO] 停留所 {len(all_served)} / 便 {total_trips} を抽出", file=sys.stderr)
    if needs:
        print(f"[INFO] 要確認: {len(needs)}件", file=sys.stderr)
    print(f"[OK] 出力: {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
