# -*- coding: utf-8 -*-
"""区間運賃のExcel（三角形の運賃早見表）を解析し、fare_matrix [{from,to,price}] を得る。

日本のバス運賃表は「対角に停留所名、右上(または左下)の三角に区間運賃」を並べた形式が多い。
さらに『大人』『子供・障がい者』など複数の表が縦に並ぶことがある。ここでは：
  - 値のある行の塊(ブロック)で表を分離
  - 各ブロック内で列ごと/行ごとの停留所名セルを特定し、数値セルを (発→着, 運賃) に変換
  - 区分ラベル(大人/小児/障がい)でブロックの区分を判定
均一(全部同額)でも動くが、非均一(距離で変わる)こそ手入力が大変なので自動化の価値が高い。

出力: {"大人":[{from,to,price}...], "小児":[...], "障がい者":[...]}（存在する区分のみ）
CLI: python parse_fare_matrix_excel.py 料金表.xlsx [--stops "A,B,C"] [-o out.json]
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

# 区分の判定（子供/障がいを先に見る。『子供…大人』のような混在ラベル対策）
CATEGORY = [("小児", r"小児|こども|子供|子ども|小人"),
            ("障がい者", r"障害者|障がい者|障碍者"),
            ("大人", r"大人|おとな|一般")]
# 停留所名でない「ラベル」語（表題・区分名など）。停留所判定から除外。
LABEL_TOKENS = ["料金", "運賃", "運賃表", "別紙", "大人", "小人", "小児", "こども", "子供",
                "子ども", "障害", "障がい", "障碍", "おとな", "一般", "表", "円"]


def _norm(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")))


def _is_label(s):
    s = str(s).strip()
    return (not s) or any(tok in s for tok in LABEL_TOKENS)


def _category_of(cells_in_block):
    for _, v in cells_in_block:
        for cat, pat in CATEGORY:
            if re.search(pat, str(v)):
                return cat
    return "大人"


def parse_fare_matrix(xlsx_path, valid_stops=None):
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    # 全セル収集
    cells = {}
    maxrow = 0
    for row in ws.iter_rows():
        for c in row:
            if c.value is not None and str(c.value).strip() != "":
                cells[(c.row, c.column)] = c.value
                maxrow = max(maxrow, c.row)
    # 値のある行を連続ブロックに分割（表ごとに分ける）
    rows_with = sorted({r for (r, _c) in cells})
    blocks, cur = [], []
    for r in rows_with:
        if cur and r - cur[-1] > 1:
            blocks.append(cur); cur = []
        cur.append(r)
    if cur:
        blocks.append(cur)

    vnorm = {_norm(s) for s in (valid_stops or [])}

    def is_stop(v):
        s = str(v).strip()
        if not s:
            return False
        if re.fullmatch(r"[\d,\.\s円¥￥]+", unicodedata.normalize("NFKC", s)):
            return False   # 数値/金額
        if vnorm:
            return _norm(s) in vnorm
        return not _is_label(s)

    def as_price(v):
        m = re.search(r"(\d{2,4})", unicodedata.normalize("NFKC", str(v)))
        return int(m.group(1)) if m else None

    out = {}
    for blk in blocks:
        rset = set(blk)
        bcells = [((r, c), v) for (r, c), v in cells.items() if r in rset]
        cat = _category_of(bcells)
        # 列ごと・行ごとの停留所名（三角表では各列/各行に停留所名セルが1つ）
        col_stop, row_stop = {}, {}
        for (r, c), v in bcells:
            if is_stop(v):
                col_stop.setdefault(c, str(v).strip())
                if r not in row_stop:
                    row_stop[r] = str(v).strip()
                else:                       # 行に停留所が複数なら左側を採用
                    row_stop[r] = row_stop[r]
        # 行の停留所は「その行で最も左の停留所セル」に統一
        left_stop = {}
        for (r, c), v in sorted(bcells):
            if is_stop(v) and r not in left_stop:
                left_stop[r] = str(v).strip()
        row_stop = left_stop
        pairs, seen = [], set()
        for (r, c), v in bcells:
            if is_stop(v):
                continue
            price = as_price(v)
            if not price:
                continue
            rs, cs = row_stop.get(r), col_stop.get(c)
            if not rs or not cs or rs == cs:
                continue
            key = (rs, cs)
            if key in seen:
                continue
            seen.add(key)
            pairs.append({"from": rs, "to": cs, "price": price})
        if pairs:
            out.setdefault(cat, []).extend(pairs)
    return out


def main():
    ap = argparse.ArgumentParser(description="区間運賃Excel(三角表)→fare_matrix")
    ap.add_argument("input")
    ap.add_argument("--stops", default=None, help="有効な停留所名（カンマ区切り。指定で精度UP）")
    ap.add_argument("-o", "--output", default=None)
    a = ap.parse_args()
    stops = [s.strip() for s in a.stops.split(",")] if a.stops else None
    res = parse_fare_matrix(Path(a.input), stops)
    if a.output:
        Path(a.output).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    for cat, pairs in res.items():
        prices = sorted({p["price"] for p in pairs})
        print(f"[{cat}] {len(pairs)}区間  運賃種={prices}")
        for p in pairs[:4]:
            print(f"    {p['from']} → {p['to']} = {p['price']}円")
    if not res:
        print("運賃マトリクスを検出できませんでした")


if __name__ == "__main__":
    sys.exit(main())
