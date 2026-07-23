# -*- coding: utf-8 -*-
"""区間運賃のPDF（三角形の運賃早見表）を座標から解析し、fare_matrix [{from,to,price}] を得る。

日本のバス運賃表でよくある「三角の区間運賃早見表」をテキストPDFから読み取る。
Excel版(parse_fare_matrix_excel.py)と**同じ出力形式**を返し、③の区間運賃取り込みに合流する。

対応する形式（太陽交通・香春線などの標準的な三角表）:
  - 停留所名が階段状（対角）に並び、左端の列にも各行の発停留所名が入る。
  - 下三角（左下）に区間運賃の数字（円記号なしの裸の数字）が並ぶ。
  - k 番目の発停留所の行には、着 s0..s(k-1) への運賃が左から順に k 個並ぶ（完全な下三角）。

解析方針（決定的・自己検証つき）:
  1) 数字トークンを y でクラスタして「運賃行」を作る（上から順に発 s1, s2, ...）。
  2) 数字トークンを x でクラスタして「運賃列」を作る（左から順に着 s0, s1, ...）。
  3) 左端列の停留所名（＋最上段の停留所名）を y 順に並べ、停留所列 S を得る。
  4) 各運賃セルを (発=行の停留所, 着=列の停留所, 運賃) に変換する。
  5) **完全な下三角（行 i が i+1 個の運賃を持つ）でなければ取り込まない**（誤った運賃を
     黙って出さない＝「正しく失敗」）。壊れた表は警告して {} を返し、手入力を促す。

座標抽出は PyMuPDF(fitz) を用いる。pdfplumber は本形式で名前と数字を結合したり
一部を落とすため、トークン数が正確な fitz を採用した。CJK部首・互換文字は NFKC＋
確認済みの安全な写像で補正する（extract_timetable_coords.py と同方針）。

出力: {"大人":[{from,to,price}...]}（本形式は大人1表が基本。区分ラベルがあれば拾う）
CLI: python parse_fare_matrix_pdf.py 料金表.pdf [--stops "A,B,C"] [-o out.json]
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

# CJK部首・康煕部首など、pdfplumber/fitz が返しうる紛らわしい符号の安全な写像。
# extract_timetable_coords.py と同じ「確認済みのみ」方針（推測で増やさない）。
_CJK_RADICAL_MAP = {
    "⼀": "一",  # ⼀ -> 一
    "⻞": "西", "西": "西",  # ⻞ 系の西
    "⾍": "西",  # 互換
    "⻨": "西",  # ⻨ 近傍
    "⻛": "西",
    "⻓": "西",
    "⽅": "西",
    "⻄": "西",  # ⻄ -> 西
    "⼦": "長",  # ⾦ 近傍の長
    "⻡": "長",  # ⻡
    "⻑": "長",  # ⻑ -> 長
}
# 停留所名でない「ラベル」語（表題・区分名など）。停留所判定から除外。
LABEL_TOKENS = ["料金", "運賃", "運賃表", "別紙", "大人", "小人", "小児", "こども", "子供",
                "子ども", "障害", "障がい", "障碍", "おとな", "一般", "表", "円", "線", "系統",
                "時刻", "早見"]
CATEGORY = [("小児", r"小児|こども|子供|子ども|小人"),
            ("障がい者", r"障害者|障がい者|障碍者"),
            ("大人", r"大人|おとな|一般")]


def normalize_cjk(text):
    """NFKC 正規化＋確認済み部首写像。名前の文字化けを補正する。"""
    if not text:
        return text
    t = unicodedata.normalize("NFKC", text)
    if any(ch in _CJK_RADICAL_MAP for ch in t):
        t = "".join(_CJK_RADICAL_MAP.get(ch, ch) for ch in t)
    return t


def _is_label(s):
    s = str(s).strip()
    return (not s) or any(tok in s for tok in LABEL_TOKENS)


def _cluster(values, gap):
    """1次元の値をギャップで塊に分け、各塊の代表値(平均)のリストを昇順で返す。
       戻り値: [(center, [members...]), ...]（center 昇順）。"""
    if not values:
        return []
    xs = sorted(values)
    groups, cur = [], [xs[0]]
    for v in xs[1:]:
        if v - cur[-1] > gap:
            groups.append(cur); cur = [v]
        else:
            cur.append(v)
    groups.append(cur)
    return [(sum(g) / len(g), g) for g in groups]


def _assign(value, centers):
    """value を最も近い center の index に割り当てる。"""
    best, bi = None, -1
    for i, c in enumerate(centers):
        d = abs(value - c)
        if best is None or d < best:
            best, bi = d, i
    return bi


def _page_words(page):
    """fitz ページから (x0, top, x1, bottom, text) を正規化して返す。"""
    out = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], normalize_cjk(w[4])
        if text.strip():
            out.append((x0, y0, x1, y1, text))
    return out


def parse_fare_matrix_pdf(pdf_path, valid_stops=None):
    import fitz
    doc = fitz.open(str(pdf_path))
    # 運賃表は「数字が最も多いページ」にあるとみなす（表紙などを避ける）。
    best_page, best_words, best_nums = None, None, -1
    for pg in doc:
        ws = _page_words(pg)
        nnum = sum(1 for w in ws if re.fullmatch(r"\d{2,4}", w[4]))
        if nnum > best_nums:
            best_page, best_words, best_nums = pg, ws, nnum
    if not best_words or best_nums < 3:
        return {}, ["数字（運賃）が十分に見つからず、運賃表として解析できませんでした。"]
    words = best_words
    page_w = float(best_page.rect.width)

    vnorm = {re.sub(r"\s+", "", normalize_cjk(s)) for s in (valid_stops or [])}

    def is_stop(text):
        s = text.strip()
        if not s or re.fullmatch(r"[\d,\.\s円¥￥]+", unicodedata.normalize("NFKC", s)):
            return False
        if vnorm:
            return re.sub(r"\s+", "", s) in vnorm
        return not _is_label(s)

    num_tokens = [(x0, y0, x1, y1, t) for (x0, y0, x1, y1, t) in words
                  if re.fullmatch(r"\d{2,4}", t)]
    name_tokens = [(x0, y0, x1, y1, t) for (x0, y0, x1, y1, t) in words if is_stop(t)]
    if not num_tokens or not name_tokens:
        return {}, ["運賃または停留所名が見つからず、解析できませんでした。"]

    # 左端列の停留所名（発ヘッダ）: 名前 x0 の最小クラスタ。
    name_x0 = [w[0] for w in name_tokens]
    x0_clusters = _cluster(name_x0, gap=10)
    left_x0 = x0_clusters[0][0]
    left_names = sorted([(w[1], w[4]) for w in name_tokens if abs(w[0] - left_x0) <= 12])
    # 最上段の停留所名（S0）: 左端列より上・左端でない名前の最上段（対角の起点）。
    top_y = left_names[0][0] if left_names else min(w[1] for w in name_tokens)
    top_candidates = sorted(
        [(w[1], w[0], w[4]) for w in name_tokens
         if w[1] < top_y - 2 and abs(w[0] - left_x0) > 12],
        key=lambda t: (t[0], t[1]))
    top_name = top_candidates[0][2] if top_candidates else None

    # 停留所列 S（着 s0..sn / 発 s1..sn）
    S = ([top_name] if top_name else []) + [nm for _, nm in left_names]
    if len(S) < 3:
        return {}, ["停留所名の並び（対角）を復元できませんでした。"]

    # 運賃行: 数字を y でクラスタ（行の高さ ~12px。gap=7 で隣接行を分離）。
    row_clusters = _cluster([w[1] for w in num_tokens], gap=7)
    # 運賃列: 数字を x でクラスタ（列の幅 ~16px。gap=8 で分離）。
    col_centers = [c for c, _ in _cluster([(w[0] + w[2]) / 2 for w in num_tokens], gap=8)]

    row_centers = [c for c, _ in row_clusters]
    # 各数字トークンを (行index, 列index) に割り当て、行ごとに (col, price) を集約。
    rows = {i: [] for i in range(len(row_centers))}
    for (x0, y0, x1, y1, t) in num_tokens:
        ri = _assign(y0, row_centers)
        ci = _assign((x0 + x1) / 2, col_centers)
        rows[ri].append((ci, int(unicodedata.normalize("NFKC", t))))

    warnings = []
    # 自己検証: 完全な下三角か（行 i が i+1 個、列は 0..i）。
    # S1..Sn（発）が上から順に row 0..n-1 に対応する前提。
    n_rows = len(row_centers)
    if n_rows != len(S) - 1:
        warnings.append(
            f"停留所数（{len(S)}）と運賃行数（{n_rows}）が一致しません。"
            "三角表の構造を正しく認識できなかったため、取り込みを見送りました。")
        return {}, warnings

    pairs, seen = [], set()
    ok_triangle = True
    for i in range(n_rows):
        cells = sorted(rows[i])                      # (col, price) を列順に
        origin = S[i + 1]                            # 発 = S1..Sn
        # 期待: 行 i は列 0..i の i+1 セル。
        cols_here = [c for c, _ in cells]
        if len(cells) != i + 1 or cols_here != list(range(i + 1)):
            ok_triangle = False
        for c, price in cells:
            if c >= len(S):
                continue
            dest = S[c]                              # 着 = S0..
            if not origin or not dest or origin == dest or (origin, dest) in seen:
                continue
            seen.add((origin, dest))
            pairs.append({"from": origin, "to": dest, "price": price})

    if not ok_triangle:
        warnings.append(
            "運賃表が完全な三角形として読み取れませんでした（セルの欠けや列ずれ）。"
            "誤った運賃を出さないため取り込みを見送りました。③の表で手入力してください。")
        return {}, warnings

    # 区分（大人/小児/障がい）: 本形式は大人1表が基本。ラベルがあれば拾う。
    cat = "大人"
    for w in name_tokens:
        for cname, pat in CATEGORY:
            if re.search(pat, w[4]):
                cat = cname
    return {cat: pairs}, warnings


def main():
    ap = argparse.ArgumentParser(description="区間運賃PDF(三角表)→fare_matrix")
    ap.add_argument("input")
    ap.add_argument("--stops", default=None, help="有効な停留所名（カンマ区切り。指定で精度UP）")
    ap.add_argument("-o", "--output", default=None)
    a = ap.parse_args()
    stops = [s.strip() for s in a.stops.split(",")] if a.stops else None
    res, warns = parse_fare_matrix_pdf(Path(a.input), stops)
    if a.output:
        Path(a.output).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    for w in warns:
        print("[warn]", w)
    for cat, pairs in res.items():
        prices = sorted({p["price"] for p in pairs})
        print(f"[{cat}] {len(pairs)}区間  運賃種={prices}")
        for p in pairs[:4]:
            print(f"    {p['from']} -> {p['to']} = {p['price']}円")
    if not res:
        print("運賃マトリクスを検出できませんでした")


if __name__ == "__main__":
    sys.exit(main())
