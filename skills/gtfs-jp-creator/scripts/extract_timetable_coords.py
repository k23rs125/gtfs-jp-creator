#!/usr/bin/env python3
"""
extract_timetable_coords.py
===========================
Step 1 (座標方式): PDF の時刻表ページから、停留所名・番号・時刻を
pdfplumber の座標情報を使って決定的に抽出する。

pymupdf4llm（表が崩れる）や MinerU（GPU非搭載でタイムアウト）が
機能しない高密度・画像主体の自治体バス時刻表 PDF に対する第三の経路。

設計方針 (正しく失敗する):
  - 座標から機械的に決められることだけを行う。
  - 便名の確定・方向の割り当て・循環の展開・複数列が1便かの判断など、
    人の較正が要る解釈は行わない（生の抽出結果を座標つきで返す）。
  - 日本語トークンが極端に少ない等で座標抽出が効かない場合は、
    推測で埋めず warnings に明記して取れたぶんだけ返す。

確立した取りこぼし対策 (課題1):
  - 停留所名列の x0 を固定値で決め打ちせず、トークンの x0 分布の
    最頻値を「列の代表位置」として自動検出する。
  - アイコン文字 (店/駅/病院 等) が頭に付く停留所は文字幅ぶん左へ
    ずれるため、代表位置から左に余裕・右にも余裕を持たせて拾う。
  - 校区ラベル(縦書き1文字)や番号・乗継などのラベルは更に左にあるため除外。
  - 拾った後、先頭のアイコン文字を正規化で除去する。

Usage:
  python extract_timetable_coords.py <input.pdf> -o <out.json> [--page N]
        [--name-x-margin-left 15] [--name-x-margin-right 40]
        [--row-thr 7] [--col-gap 20]

License: Apache 2.0
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("Error: pdfplumber が必要です (pip install pdfplumber)", file=sys.stderr)
    sys.exit(1)

# 停留所名の先頭に紛れ込むアイコン由来文字・全角空白
ICON_PREFIX = re.compile(r'^[店駅\u3000\s]+')
TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')
NUM_RE = re.compile(r'^\d{1,3}$')
JP_RE = re.compile(r'[ぁ-んァ-ヶ一-龠]')

# 停留所名ではない見出し・案内文の断片
NOISE_NAMES = {
    "から", "バス停名", "行先", "便名", "平日",
    "乗", "継", "番", "号", "校", "区",
}
def is_noise_name(nm: str) -> bool:
    if not nm or nm in NOISE_NAMES:
        return True
    if nm.endswith("方面") or nm.endswith("」"):
        return True
    for kw in ("到着", "発車", "乗車時刻", "利用前日", "予約", "時刻表", "改正版"):
        # 「予約」を含むが「要予約【…】」は停留所なので別扱い（下で判定）
        if kw in nm and not nm.startswith("要予約"):
            return True
    if nm.startswith("⇒"):
        return True
    return False

# 末尾の方向付記（（西行き）（東行き）（上り）（下り）等）。乗り場違いでも
# 番号が同じなら同一停留所として扱う方針(案ア)に基づき、名称からは除去する。
DIRECTION_SUFFIX = re.compile(r'[（(](?:[東西南北]行き?|上り|下り|のりば\d*|\d+番のりば)[）)]\s*$')

def normalize_name(s: str) -> str:
    """停留所名の正規化。
    1) 先頭のアイコン文字(店/駅 等)・全角空白を除去
    2) 末尾の方向付記（（西行き）等）を除去（案ア: stopは番号単位で1つ）
    """
    s = ICON_PREFIX.sub('', s).strip()
    s = DIRECTION_SUFFIX.sub('', s).strip()
    return s

def cluster_rows(items, thr):
    """top 座標で行クラスタリング。"""
    items = sorted(items, key=lambda w: w['top'])
    rows = []
    for w in items:
        if not rows or w['top'] - rows[-1][-1]['top'] > thr:
            rows.append([w])
        else:
            rows[-1].append(w)
    return rows

def cluster_cols(xs, gap):
    """x0 の集合を列にクラスタリングし、各列の代表x0を返す。"""
    xs = sorted(xs)
    cols = []
    for x in xs:
        if not cols or x - cols[-1][-1] > gap:
            cols.append([x])
        else:
            cols[-1].append(x)
    return [sum(c) / len(c) for c in cols]

def detect_name_x0(name_tokens):
    """停留所名列の代表x0（最頻値）を自動検出。"""
    if not name_tokens:
        return None
    return Counter(round(w['x0']) for w in name_tokens).most_common(1)[0][0]

# 方面/方向セクションの見出し行（縦ブロック分割の境界）。
# コスモス号 弓削線のように、同じ時刻列を共有して方面ごとに縦2セクション積層する
# 時刻表で、セクションの区切りを与える。詳細: references/notes/kitano_yuge_direction_split_design_v1.md
# 行内のどこかに時刻があれば（括弧付き (9:02) 含む）その行は見出しではない。
# 「久留米方面への発車時刻(9:02)…」のような注記行を見出しと誤検出しないため。
TIME_ANYWHERE = re.compile(r'\d{1,2}:\d{2}')
# 方面/方向/循環 が括弧（【】（））の内側にある＝方向見出しとみなす。
# 「久留米方面への発車時刻」(括弧外) のような散文は見出しにしない。
BRACKET_DIR = re.compile(r'[（(【][^）)】]*(?:方面|方向|循環)')
HEADING_EXCLUDE_KW = ("問い合わせ", "運行に関する", "お問", "について")

def detect_section_headings(words):
    """『方面見出し』のトークンを検出して返す（top昇順）。
    pdfplumber は「【弓削線（古賀茶屋駅方面）】」のような見出しを1トークンに
    まとめるため、行クラスタせずトークン単位で判定する（同じtopに別ブロックの
    時刻行が並んでも混ざらない）。
    条件: (1)トークン内に時刻(括弧付き含む)が無い (2)括弧内に方面/方向/循環
    (3)案内文でない。縦に積層した方面（例: コスモス号弓削線）を分ける境界に使う。
    返り値: list of {top, x0, x1, text}"""
    heads = []
    for w in words:
        norm = w['text'].replace("　", "").replace(" ", "")
        if TIME_ANYWHERE.search(norm):
            continue  # 時刻(括弧付き含む)を含むトークンは見出しではない
        if not BRACKET_DIR.search(norm):
            continue  # 括弧内の方面/方向/循環 でなければ見出しではない
        if any(k in norm for k in HEADING_EXCLUDE_KW):
            continue  # 案内文(footer)等を除外
        heads.append({"top": round(w['top'], 1), "x0": round(w['x0'], 1),
                      "x1": round(w['x1'], 1), "text": w['text'].strip()})
    return sorted(heads, key=lambda h: h['top'])

def extract_block(words, x_lo, x_hi, name_margin_left, name_margin_right,
                  num_x_lo, num_x_hi, time_x_lo, time_x_hi, row_thr, col_gap,
                  y_lo=float("-inf"), y_hi=float("inf"), skip_tops=()):
    """1つの方向ブロックを抽出する。

    x_lo, x_hi: このブロックの停留所名トークンを大まかに含むx範囲（粗い枠）
    y_lo, y_hi: 縦セクションのy(top)範囲（既定 ±∞＝ページ全体。縦分割時のみ指定）。
    skip_tops: 停留所として扱わない行のtop（セクション見出し行の除外用）。
    既定（y_lo=-∞, y_hi=+∞, skip_tops=()）では従来と完全に同じ挙動。
    返り値: dict(stops=[{num,name,top}], trips=[{col_x, cells=[{seq,num,name,time}]}], warnings=[])
    """
    warnings_list = []
    # --- 停留所名列 ---
    jp_in_block = [w for w in words
                   if x_lo <= w['x0'] <= x_hi and JP_RE.search(w['text'])
                   and y_lo <= w['top'] <= y_hi]
    if not jp_in_block:
        return {"stops": [], "uncertain": [], "trips": [], "warnings": ["停留所名(日本語)トークンが取得できません。座標抽出は不適。"]}
    xmode = detect_name_x0(jp_in_block)
    lo, hi = xmode - name_margin_left, xmode + name_margin_right
    name_tokens = [w for w in jp_in_block if lo <= w['x0'] <= hi and w['x0'] >= xmode - name_margin_left]

    # --- 番号列（停留所名列の少し左の数字） ---
    nums = [w for w in words if num_x_lo <= w['x0'] <= num_x_hi and NUM_RE.match(w['text'])
            and y_lo <= w['top'] <= y_hi]
    numlist = sorted([(sum(t['top'] for t in r) / len(r),
                       "".join(x['text'] for x in sorted(r, key=lambda w: w['x0'])))
                      for r in cluster_rows(nums, row_thr)])

    stops = []
    uncertain = []  # 番号が取れず、停留所か不確実なもの(要確認)
    for r in cluster_rows(name_tokens, row_thr):
        top = sum(t['top'] for t in r) / len(r)
        if skip_tops and any(abs(top - h) < row_thr for h in skip_tops):
            continue  # セクション見出し行は停留所として扱わない
        nm = normalize_name("".join(x['text'] for x in sorted(r, key=lambda w: w['x0'])))
        if is_noise_name(nm):
            continue
        near = min(numlist, key=lambda x: abs(x[0] - top)) if numlist else (1e9, "")
        num = int(near[1]) if abs(near[0] - top) < row_thr + 1 and near[1].isdigit() else None
        is_reserve = nm.startswith("要予約")
        rec = {"num": num, "name": nm, "top": round(top, 1), "reserve": is_reserve}
        if num is None:
            rec["uncertain"] = True
            uncertain.append(rec)
        stops.append(rec)

    # --- 時刻列（便） ---
    times = [w for w in words if time_x_lo <= w['x0'] <= time_x_hi and TIME_RE.match(w['text'])
             and y_lo <= w['top'] <= y_hi]
    if not times:
        warnings_list.append("時刻トークンが取得できません。")
        return {"stops": stops, "uncertain": uncertain, "trips": [], "warnings": warnings_list}
    colx = cluster_cols([round(w['x0']) for w in times], col_gap)
    tops = [s["top"] for s in stops]
    def nearest(v, arr):
        return min(range(len(arr)), key=lambda i: abs(arr[i] - v))
    grid = {}
    for w in times:
        if not tops:
            break
        ri = nearest(w['top'], tops)
        ci = nearest(w['x0'], colx)
        if abs(tops[ri] - w['top']) < row_thr + 1:
            grid[(ri, ci)] = w['text']
    trips = []
    for ci in range(len(colx)):
        cells = []
        seq = 0
        for ri in range(len(stops)):
            if (ri, ci) in grid:
                seq += 1
                cells.append({"seq": seq, "num": stops[ri]["num"],
                              "name": stops[ri]["name"], "time": grid[(ri, ci)] + ":00",
                              "reserve": stops[ri]["reserve"]})
        if cells:
            # 単調性チェック（参考情報。要予約・折り返しで逆行はありうるので警告のみ）
            def _m(t):
                h, m, *_ = t.split(":")
                return int(h) * 60 + int(m)
            mins = [_m(c["time"]) for c in cells]
            mono = all(mins[i] <= mins[i + 1] for i in range(len(mins) - 1))
            trips.append({"col_x": round(colx[ci], 1), "n_stops": len(cells),
                          "monotonic": mono, "cells": cells})
    return {"stops": stops, "uncertain": uncertain, "trips": trips, "warnings": warnings_list}

def main():
    ap = argparse.ArgumentParser(description="座標方式でPDF時刻表から停留所・時刻を抽出 (Step1 第三経路)")
    ap.add_argument("input", help="入力PDF")
    ap.add_argument("-o", "--output", required=True, help="出力JSON")
    ap.add_argument("--page", type=int, default=None, help="時刻表ページ(1始まり)。未指定なら時刻トークン最多のページを自動選択")
    ap.add_argument("--name-x-margin-left", type=int, default=15)
    ap.add_argument("--name-x-margin-right", type=int, default=40)
    ap.add_argument("--row-thr", type=int, default=7)
    ap.add_argument("--col-gap", type=int, default=20)
    ap.add_argument("--blocks", type=int, default=0,
                    help="方向ブロック数(0=自動: 名前列の塊を検出)。手動指定も可")
    ap.add_argument("--multi-pattern-min", type=int, default=30,
                    help="便内でこの分数以上の時刻逆行があれば『複数方面/パターン混在の可能性』として要確認に挙げる(既定30分)")
    args = ap.parse_args()

    pdf = pdfplumber.open(args.input)
    # ページ自動選択（時刻トークンが最も多いページ）
    if args.page:
        page = pdf.pages[args.page - 1]
    else:
        best, bestn = pdf.pages[0], -1
        for pg in pdf.pages:
            n = sum(1 for w in pg.extract_words() if TIME_RE.match(w['text']))
            if n > bestn:
                best, bestn = pg, n
        page = best
    words = page.extract_words()
    page_no = pdf.pages.index(page) + 1
    # --- 画像PDF検出 (課題: 画像化された時刻表は座標方式が使えない) ---
    # 選択ページの文字オブジェクトが極端に少ない場合、時刻表が画像として
    # 貼り付けられている(テキストレイヤなし)と判定する。座標方式はpdfplumberの
    # 文字座標に依存するため、この場合は原理的に機能しない。OCR経路が必要。
    n_chars = len(page.chars)
    if n_chars < 20:
        # 画像PDF: 座標方式は文字座標に依存するため使えない。拒否せず「精度低下を明示して
        # OCR(MinerU)経路へ誘導」する。OCRは誤読が起きるため、抽出結果は必ず原典と目視照合
        # する前提（"正しく失敗"の精神は強い警告と要確認フラグで担保する）。
        ocr_msg = (f"ページ{page_no}の文字オブジェクトが{n_chars}個しかなく、時刻表が画像化"
                   "(テキストレイヤなし)と判定しました。座標方式はこのページには使えません。"
                   "代わりに OCR 経路で抽出してください（既定の pipeline バックエンドが数字に正確で速い）: "
                   "`python scripts/pdf_to_markdown.py <PDF> --engine mineru --lang japan -o out.md` "
                   "→ Markdown を extract_timetable_markdown.py で extract.json 化し Step2(構造化)へ。"
                   "OCRは読み違いが残るため、生成後は detect_time_anomalies.py / 時刻アノマリ で確認し"
                   "必ず原典と目視照合してください。"
                   "元データ(Excel)があれば extract_timetable_excel.py で直接読むのが最も確実です。")
        result = {"source": str(args.input), "page": page_no, "blocks": [],
                  "warnings": [
                      f"ページ{page_no}の文字オブジェクトが{n_chars}個しかありません(画像化PDF)。"
                      "座標方式は使えないため、OCR(MinerU)経路での抽出に切り替えてください"
                      "（精度低下・要目視確認）。"],
                  "needs_confirmation": [{
                      "type": "image_pdf_use_ocr", "page": page_no, "n_chars": n_chars,
                      "recommended_engine": "mineru",
                      "accuracy_warning": "OCRのため精度が下がります。抽出結果は必ず原典と目視照合してください。",
                      "message": ocr_msg}]}
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[INFO] ページ {page_no} を使用", file=sys.stderr)
        print(f"[警告] 文字オブジェクト{n_chars}個 → 画像化PDFと判定。座標方式は使えません。", file=sys.stderr)
        print(f"[誘導] OCR経路(MinerU)で抽出してください: "
              f"pdf_to_markdown.py --engine mineru → Markdown → Step2(構造化)", file=sys.stderr)
        print(f"[注意] OCRは精度が下がります。抽出結果は必ず原典と目視照合してください。", file=sys.stderr)
        print(f"[INFO] 元データ(Excel)があれば extract_timetable_excel.py で直接読むのが最も確実です。",
              file=sys.stderr)
        print(f"[OK] 出力: {args.output}", file=sys.stderr)
        return

    # 名前列(日本語)の x0 分布から「ブロック(方向)」を検出
    # 縦書き1文字ラベル(校区名 等)を除くため、幅のある(>=2文字相当)トークンに限定
    jp = [w for w in words if JP_RE.search(w['text']) and (w['x1'] - w['x0']) >= 18]
    x0c = Counter(round(w['x0']) for w in jp)
    # 出現10件以上の x0 を名前列候補とみなす
    name_x0s = sorted([x for x, c in x0c.items() if c >= 10])
    # 近接するものをまとめてブロック中心に
    block_centers = cluster_cols(name_x0s, 50) if name_x0s else []
    # --blocks 手動指定: 自動検出と異なる場合、gap を調整して指定数に近づける。
    # 名前列が物理的に指定数ぶん立たない場合は割れない（推測で増やさない=正しく失敗）。
    # 実際に割れたか否かの判定は、便を持つ最終ブロック(real_blocks)確定後に行う。
    if args.blocks and name_x0s and len(block_centers) != args.blocks:
        target = args.blocks
        chosen = block_centers
        for g in range(2, 201):  # gap を狭めて分割数を増やす方向に探索
            c = cluster_cols(name_x0s, g)
            if len(c) == target:
                chosen = c
                break
            if len(c) < target:
                # これ以上 gap を狭めても増えないところまで来たら、最も近いものを採用
                if abs(len(c) - target) < abs(len(chosen) - target):
                    chosen = c
        block_centers = chosen

    result = {"source": str(args.input), "page": page_no,
              "blocks": [], "warnings": []}
    if not block_centers:
        result["warnings"].append("名前列を検出できません。座標抽出が効かないPDFの可能性（停留所名が画像化など）。")
    page_w = page.width
    # ページ全体から方面見出しトークンを検出（各見出しは後で最も近い名前列ブロックへ割り当て）
    all_headings = detect_section_headings(words)
    needs = []  # 要確認項目（利用者に質問を投げる材料）
    raw_blocks = []
    for bi, cx in enumerate(block_centers):
        x_lo, x_hi = cx - 20, cx + 120
        num_x_lo, num_x_hi = cx - 58, cx - 12  # 左端を-45→-58に拡大: 3桁番号(名前の左約46pt)を取りこぼさないため
        next_cx = block_centers[bi + 1] if bi + 1 < len(block_centers) else page_w
        time_x_lo, time_x_hi = cx + 120, next_cx - 60 if next_cx < page_w else page_w

        # --- 方面見出しによる縦セクション分割 ---
        # このブロックの水平範囲内の見出し行を検出し、時刻帯の「中段」にある見出し
        # （= 同じ列を共有して縦積みされた別方面の境界）でブロックを縦に分割する。
        # 中段見出しが無ければ従来どおり 1 ブロック（下の else 節は従来と完全に同一）。
        # この名前列ブロックに属する見出し（x0が最も近い名前列ブロックに割り当て）
        headings = [h for h in all_headings
                    if min(range(len(block_centers)),
                           key=lambda j: abs(h['x0'] - block_centers[j])) == bi]
        block_time_tops = [w['top'] for w in words
                           if time_x_lo <= w['x0'] <= time_x_hi and TIME_RE.match(w['text'])]
        inbody = []
        if block_time_tops:
            tmin, tmax = min(block_time_tops), max(block_time_tops)
            inbody = sorted(h['top'] for h in headings if tmin < h['top'] < tmax)

        if not inbody:
            # 従来パス（中段見出しなし）。jojima 等はここを通り、出力は従来と完全一致。
            blk = extract_block(words, x_lo, x_hi,
                                args.name_x_margin_left, args.name_x_margin_right,
                                num_x_lo, num_x_hi, time_x_lo, time_x_hi,
                                args.row_thr, args.col_gap)
            blk["name_col_x"] = round(cx, 1)
            raw_blocks.append(blk)
        else:
            # 中段見出しで縦分割。各セクションを別ブロックとして抽出し、方面ラベルを付与。
            section_bounds = [float("-inf")] + inbody + [float("inf")]
            for si in range(len(section_bounds) - 1):
                lo, hi = section_bounds[si], section_bounds[si + 1]
                if si == 0:
                    above = [h for h in headings if h['top'] < inbody[0]]
                    label = above[-1]['text'] if above else None
                else:
                    at = [h for h in headings if abs(h['top'] - lo) < 1]
                    label = at[0]['text'] if at else None
                blk = extract_block(words, x_lo, x_hi,
                                    args.name_x_margin_left, args.name_x_margin_right,
                                    num_x_lo, num_x_hi, time_x_lo, time_x_hi,
                                    args.row_thr, args.col_gap,
                                    y_lo=lo, y_hi=hi, skip_tops=inbody)
                blk["name_col_x"] = round(cx, 1)
                blk["direction_heading"] = label
                raw_blocks.append(blk)

    # 便を持つブロックのみ「方向ブロック」として採用（校区ラベル等の誤検出を除外）
    real_blocks = [b for b in raw_blocks if b["trips"]]
    for i, b in enumerate(real_blocks):
        b["block_index"] = i
    result["blocks"] = real_blocks

    # --blocks 手動指定の充足判定（便を持つ最終ブロック数で評価）
    if args.blocks and len(real_blocks) != args.blocks:
        result["warnings"].append(
            f"--blocks={args.blocks} が指定されましたが、座標からは {len(real_blocks)} "
            "ブロックしか分離できませんでした。停留所名列が複数方面で共有されている等の理由で、"
            "座標だけでは指定数に分割できません（推測では分けません）。原典で方面ごとに確認してください。")

    if not real_blocks:
        result["warnings"].append("便(時刻列)を持つブロックがありません。座標抽出が効かないPDFの可能性。")
        needs.append({"type": "extraction_failed",
                      "message": "座標方式で時刻が抽出できませんでした。MinerU等の別方式か、手動確認が必要です。"})

    for b in real_blocks:
        # (1) 番号なし停留所 → 停留所かノイズか要確認
        for u in b.get("uncertain", []):
            needs.append({"type": "uncertain_stop", "block": b["block_index"],
                          "name": u["name"], "top": u["top"],
                          "message": f"番号が取得できない停留所候補『{u['name']}』。実在の停留所か、表の見出し/案内文か確認してください。"})
        # (2) 時刻逆行のある便 → 要予約・折り返しの可能性
        for t in b["trips"]:
            if not t["monotonic"]:
                needs.append({"type": "time_nonmonotonic", "block": b["block_index"],
                              "col_x": t["col_x"],
                              "message": f"便(列x={t['col_x']})で時刻が逆行しています。要予約バス停への寄り道や折り返しの可能性。便の向き・経路順を確認してください。"})
        # (2b) 便内に「大きな」時刻逆行(既定30分以上)がある便 → 複数方面/パターンの
        #      混在の可能性。1つの名前列ブロックに別方面の便列が同居していると、
        #      前方面の終わり→次方面の始まりで時刻が大きく戻る。座標だけでは分離
        #      できないため、原典で方面ごとに分けて確認するよう促す（正しく失敗）。
        big_jump_cols = []
        for t in b["trips"]:
            def _mm(tt):
                h, m, *_ = tt.split(":")
                return int(h) * 60 + int(m)
            ms = [_mm(c["time"]) for c in t["cells"]]
            worst_back = max((ms[i] - ms[i + 1] for i in range(len(ms) - 1)), default=0)
            if worst_back >= args.multi_pattern_min:
                big_jump_cols.append((t["col_x"], worst_back))
        if big_jump_cols:
            cols_desc = "、".join(f"列x={cx}(最大{w}分戻り)" for cx, w in big_jump_cols)
            needs.append({"type": "multi_pattern_suspected", "block": b["block_index"],
                          "threshold_min": args.multi_pattern_min,
                          "cols": [cx for cx, _ in big_jump_cols],
                          "message": (f"ブロック{b['block_index']}に、便内で{args.multi_pattern_min}分以上の"
                                      f"大きな時刻逆行を含む便があります（{cols_desc}）。"
                                      "1つの名前列に複数方面/パターンの便が同居している可能性があります。"
                                      "座標だけでは方面を分離できないため、原典の時刻表で方面ごとに分けて"
                                      "（行先・経路別に）確認・割り当ててください。")})
        # (3) 便ごとの停留所数のばらつき → 取りこぼし/区間便の確認
    # (4) 便名・方向は座標から確定できない → 必ず確認
    if real_blocks:
        needs.append({"type": "assign_required",
                      "message": "便名(A1便等)・方向(direction_id)・循環の展開は座標から確定できません。原典と照合して割り当ててください。"})
    result["needs_confirmation"] = needs

    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    # サマリ
    print(f"[INFO] ページ {page_no} を使用", file=sys.stderr)
    print(f"[INFO] 検出ブロック数: {len(result['blocks'])}", file=sys.stderr)
    for b in result["blocks"]:
        print(f"  block{b['block_index']} (name_x={b['name_col_x']}): "
              f"停留所{len(b['stops'])} 便{len(b['trips'])} "
              f"warnings={b['warnings']}", file=sys.stderr)
    print(f"[INFO] 要確認項目(needs_confirmation): {len(result.get('needs_confirmation', []))}件", file=sys.stderr)
    print(f"[OK] 出力: {args.output}", file=sys.stderr)

if __name__ == "__main__":
    main()
