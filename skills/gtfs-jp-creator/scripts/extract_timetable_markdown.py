# -*- coding: utf-8 -*-
"""OCR(MinerU)等で得た Markdown の時刻表テーブルを extract.json（blocks/便/cells）へ変換する。

画像化PDF → pdf_to_markdown.py(--engine mineru) → 本スクリプト → 既存パイプライン(Step2構造化以降)。
MinerU は時刻表を <table> として書き出すので、その表を解釈して
extract_timetable_coords.py / extract_timetable_excel.py と同じ extract.json 形式に落とす。

判定ルール（時刻表テーブルの見分け方）:
  - ヘッダ行（最初の行）の2列目以降に「便」を含むセルが複数 → 時刻表テーブル。
  - 以降の各行は「停留所名 | 時刻 | 時刻 | ...」。`||` `‖` 空欄 `通過` は通過（その便はその停留所に停まらない）。
方向見出し(direction_hint)は直前の ■... / ⇒ / → を含む行から拾う。

OCRは誤読が起きるため、生成物は needs_confirmation でOCR照合を促す（＝正しく失敗）。

使い方:
  python extract_timetable_markdown.py <input.md> -o extract.json
"""
import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
PASS_TOKENS = {"||", "‖", "ǁ", "│", "|", "", "‐", "-", "ー", "通過", "レ", "↓", "…"}
TRIP_NUM_RE = re.compile(r"(\d+)\s*便")


class _Table(HTMLParser):
    """<table> を 行×セル の二次元リストに分解する簡易パーサ。"""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._row = None
        self._cell = None
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._in_cell = False
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in_cell and self._cell is not None:
            self._cell.append(data)


def _parse_tables(md: str):
    """markdown中の各 <table> を (直前見出し, 行リスト) のタプルで返す。"""
    out = []
    for m in re.finditer(r"<table>.*?</table>", md, re.S):
        # 直前の非空テキスト行を見出し候補に
        head = ""
        pre = md[:m.start()].rstrip().split("\n")
        for line in reversed(pre):
            s = re.sub(r"[#>*`]", "", line).strip()
            if s and not s.startswith("!["):
                head = s
                break
        p = _Table()
        p.feed(m.group(0))
        out.append((head, p.rows))
    return out


def _norm_time(s: str):
    s = s.strip()
    m = TIME_RE.match(s)
    if not m:
        return None
    h, mi, se = m.group(1), m.group(2), m.group(3) or "00"
    return f"{int(h):02d}:{mi}:{se}"


def _is_timetable(rows):
    if len(rows) < 3:
        return False
    header = rows[0]
    bin_cells = sum(1 for c in header[1:] if "便" in c)
    if bin_cells >= 2:
        return True
    # 「便」表記が無くても、データ行に時刻が多数あれば時刻表とみなす
    timey = 0
    for r in rows[1:]:
        timey += sum(1 for c in r[1:] if _norm_time(c))
    return timey >= 4 and len(header) >= 3


def _trip_number(label: str):
    m = TRIP_NUM_RE.search(label)
    return int(m.group(1)) if m else None


def route_title_from_texts(texts):
    """文字列群（見出し行など）から路線名「○○線/系統/ルート」を1つ返す。
    曜日付記・付随語を落とし、全文字二重描画を畳む。JR等の鉄道路線・「系統番号」は除外。"""
    for text in texts:
        if not text:
            continue
        for tok in re.split(r"[\s　（）()【】\[\]／/｜|,、。「」#*>]+", str(text)):
            tok = tok.strip()
            if len(tok) >= 4 and len(tok) % 2 == 0 and tok[0::2] == tok[1::2]:
                tok = tok[0::2]
            tok = re.sub(r"(時刻表|時刻|ダイヤ|運行表|一覧表|表)$", "", tok)
            if not (2 <= len(tok) <= 20):
                continue
            if any(x in tok for x in ("新幹線", "ゆたか線", "福北", "鉄道", "番号", "種類")):
                continue   # 鉄道路線・「系統番号」等は除外（JR古賀線等のバス路線名は許可）
            if tok.endswith(("線", "系統", "ルート")):
                return tok
    return None


def md_to_extract(md: str, source: str = "") -> dict:
    _doc_rt = route_title_from_texts(md.splitlines()[:15])   # 文書上部の見出しから路線名
    blocks = []
    for bi, (head, rows) in enumerate(t for t in _parse_tables(md) if _is_timetable(t[1])):
        header = rows[0]
        n_cols = max(len(r) for r in rows)
        trip_labels = [(header[j] if j < len(header) else f"便{j}") for j in range(1, n_cols)]
        # マスタ停留所（行順）
        data_rows = [r for r in rows[1:] if r and r[0].strip()]
        stops = [{"num": None, "name": r[0].strip(), "row": i, "reserve": False}
                 for i, r in enumerate(data_rows)]
        # 便（列）ごとに cells を作る。通過/空は除外。
        trips = []
        for j in range(1, n_cols):
            cells = []
            for r in data_rows:
                name = r[0].strip()
                val = r[j].strip() if j < len(r) else ""
                if val in PASS_TOKENS:
                    continue
                tm = _norm_time(val)
                if tm is None:
                    continue
                cells.append({"seq": len(cells) + 1, "num": None, "name": name,
                              "time": tm, "reserve": False})
            if len(cells) >= 2:
                trips.append({"col": j, "trip_number": _trip_number(trip_labels[j - 1]),
                              "label": trip_labels[j - 1], "n_stops": len(cells),
                              "monotonic": True, "cells": cells})
        if not trips:
            continue
        dh = head if (head and ("⇒" in head or "→" in head or head.startswith("■") or "行" in head)) else None
        if dh:
            dh = dh.lstrip("■").strip()
        _rt = route_title_from_texts([head]) or _doc_rt   # 表の見出し→無ければ文書上部
        _blk = {"block_index": len(blocks), "name_col": 0, "section_row": 0,
                "direction_hint": dh, "stops": stops, "trips": trips, "warnings": []}
        if _rt:
            _blk["route_title"] = _rt
        blocks.append(_blk)

    needs = [{"type": "ocr_review",
              "message": "OCR(画像PDF)からの抽出です。停留所名・時刻・通過(||)に誤読の可能性があるため、"
                         "必ず原典と目視照合してください。"}]
    if not blocks:
        needs.append({"type": "no_timetable_table",
                      "message": "Markdown内に時刻表テーブルを検出できませんでした。OCR結果(.md)を確認してください。"})
    return {"source": source, "sheet": None, "blocks": blocks,
            "warnings": [], "needs_confirmation": needs}


def main():
    ap = argparse.ArgumentParser(description="OCR Markdown の時刻表を extract.json に変換")
    ap.add_argument("input", help="入力 Markdown (.md)")
    ap.add_argument("-o", "--output", required=True, help="出力 extract.json")
    a = ap.parse_args()
    md = Path(a.input).read_text(encoding="utf-8")
    ext = md_to_extract(md, source=str(a.input))
    Path(a.output).write_text(json.dumps(ext, ensure_ascii=False, indent=2), encoding="utf-8")
    nb = len(ext["blocks"])
    nt = sum(len(b["trips"]) for b in ext["blocks"])
    ns = sum(len(b["stops"]) for b in ext["blocks"])
    print(f"[OK] {a.output}: ブロック{nb} / 便{nt} / 停留所(延べ){ns}")
    for b in ext["blocks"]:
        print(f"  block{b['block_index']} dir={b['direction_hint']!r} 便{len(b['trips'])} 停{len(b['stops'])}")


if __name__ == "__main__":
    sys.exit(main())
