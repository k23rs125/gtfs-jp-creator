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
TIME_RE = re.compile(r'^\d{1,2}[:：]\d{2}$')   # 全角コロン（5：50）の時刻表にも対応
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
    # 注記（※…）・方向見出し（佐屋⇒西鉄新宮駅 等）は停留所ではない。長さでは切らない
    # （実在の停留所に「ﾌｧﾐﾘｰﾏｰﾄ城島店（中町整骨院）」＝18字のような長い名称があるため）。
    if nm.startswith("※") or "⇒" in nm or "⇨" in nm or "→" in nm:
        return True
    # ルート・方向見出し（「第１ルート（時計回り）」等）。これらの語は停留所名には現れない。
    # 文字が二重に描画される PDF（第第１１ルルーートト…）でも「回り」は部分一致で拾える。
    if "ルート" in nm or "回り" in nm or "循環" in nm:
        return True
    # 「待機時間」は待ち時間(例 0:11)を記した注記で停留所ではない（相島渡船場↔渡船場出発の
    # 間に入り時刻を逆行させる）。停留所名に「待機」は現れないため部分一致で除外する。
    for kw in ("到着", "発車", "乗車時刻", "利用前日", "予約", "時刻表", "改正版",
               "待機", "運行になります", "ダイヤでの運行"):
        # 「予約」を含むが「要予約【…】」は停留所なので別扱い（下で判定）
        if kw in nm and not nm.startswith("要予約"):
            return True
    return False

# 末尾の方向付記（（西行き）（東行き）（上り）（下り）等）。乗り場違いでも
# 番号が同じなら同一停留所として扱う方針(案ア)に基づき、名称からは除去する。
DIRECTION_SUFFIX = re.compile(r'[（(](?:[東西南北]行き?|上り|下り|のりば\d*|\d+番のりば)[）)]\s*$')

_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def normalize_name(s: str) -> str:
    """停留所名の正規化。
    1) 先頭のアイコン文字(店/駅 等)・全角空白を除去
    2) 末尾の方向付記（（西行き）等）を除去（案ア: stopは番号単位で1つ）
    """
    s = ICON_PREFIX.sub('', s).strip()
    s = DIRECTION_SUFFIX.sub('', s).strip()
    s = s.translate(_FW_DIGITS)   # 全角数字→半角（「湊坂２丁目」→「湊坂2丁目」。公式GTFSは半角）
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
    """停留所名列の左端x0を自動検出。
    均等割り付けで停留所名が横に広がる時刻表では、最頻値だと各行の最後の文字（右端）を
    拾ってしまい名前が先頭側で切れる（例: 新宮町マリンクス土曜ページ）。そこで、最頻に
    近い頻度で現れる x0 のうち最も左（＝名前の先頭文字位置）を採用する。コンパクトな
    名前（1トークン）では先頭＝最頻なので従来と同じ結果になる。"""
    if not name_tokens:
        return None
    c = Counter(round(w['x0']) for w in name_tokens)
    mode, top = c.most_common(1)[0]
    # mode と同程度に頻出し（>=0.7×最頻）、mode より左で名前の広がり(~110pt)以内にある x0 の
    # 最も左を名前の先頭とみなす。左に離れた別列（校区・番号など、行ごとに現れず頻度が低い）は
    # しきい値で自然に除外される。コンパクトな名前では mode 自身が返り従来と同じ。
    cands = [x for x, n in c.items() if n >= top * 0.7 and mode - 110 <= x <= mode]
    return min(cands) if cands else mode

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
    # 名前列の右端は、ブロック範囲 x_hi（＝時刻列の手前。呼び出し側でデータ駆動に決定）まで
    # 広げる。均等割り付けで横に広がる停留所名（例「的 野 公 民 館 前」）を途中で切らないため。
    # 左端は番号列を避ける margin。従来より広いが、x_hi が時刻列手前なので時刻は混ざらない。
    lo = xmode - name_margin_left
    hi = max(xmode + name_margin_right, x_hi)
    name_tokens = [w for w in jp_in_block if lo <= w['x0'] <= hi]

    # --- 番号列（停留所名列の少し左の数字） ---
    nums = [w for w in words if num_x_lo <= w['x0'] <= num_x_hi and NUM_RE.match(w['text'])
            and y_lo <= w['top'] <= y_hi]
    numlist = sorted([(sum(t['top'] for t in r) / len(r),
                       "".join(x['text'] for x in sorted(r, key=lambda w: w['x0'])))
                      for r in cluster_rows(nums, row_thr)])

    stops = []
    uncertain = []  # 番号が取れず、停留所か不確実なもの(要確認)
    # 名前列内の「日本語でないトークン」（例「緑ケ浜３丁目」の３、「下府１丁目」の１）。
    # 名前は日本語トークンから作るため、名前の途中の数字が落ちる。行ごとに、その行の
    # 日本語名の x範囲内にあるものだけを取り込む（左の番号列はスパン外なので混ざらない）。
    band_extra = [w for w in words
                  if lo <= w['x0'] and w['x1'] <= x_hi + 2
                  and y_lo <= w['top'] <= y_hi and not JP_RE.search(w['text'])]

    for r in cluster_rows(name_tokens, row_thr):
        top = sum(t['top'] for t in r) / len(r)
        if skip_tops and any(abs(top - h) < row_thr for h in skip_tops):
            continue  # セクション見出し行は停留所として扱わない
        rmin = min(t['x0'] for t in r); rmax = max(t['x1'] for t in r)
        _row_extra = [w for w in band_extra if abs(w['top'] - top) < row_thr + 1]
        # (1) JP名のx範囲内にある非JP（中間の数字。例「緑ケ浜３丁目」の３、「下府１丁目」の１）
        inline = [w for w in _row_extra if w['x0'] >= rmin - 2 and w['x1'] <= rmax + 2]
        # (2) JP名の左に連続する非JP（接頭辞。例「ＪＲ福工大前駅」の J R）。gap<=8 で連結し、
        #     30pt以上離れた左の番号列/校区列は連結されず混ざらない。
        prefix = []; _edge = rmin
        for w in sorted([w for w in _row_extra if w['x1'] <= rmin + 2],
                        key=lambda w: w['x1'], reverse=True):
            if _edge - w['x1'] <= 8:
                prefix.append(w); _edge = w['x0']
            else:
                break
        nm = normalize_name("".join(x['text'] for x in
                                    sorted(list(r) + inline + prefix, key=lambda w: w['x0'])))
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
            grid[(ri, ci)] = w['text'].replace('：', ':')   # 全角コロンを半角に正規化
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

    # 名前列(日本語)の x0 分布から「ブロック(方向)」を検出。
    # 均等割り付けで1文字ずつに割れる停留所名（例「的 野 公 民 館 前」「佐　　屋」）に対応するため、
    # 同一行の日本語トークンを、大きな水平ギャップ（=別方向の列）でだけ区切って結合し、
    # 結合後の幅で判定する。縦1文字ラベル(校区名等)は行が別なので結合されず、幅フィルタで除外される。
    def _merge_row_names(row_tokens, gap=100):
        xs = sorted(row_tokens, key=lambda w: w['x0'])
        groups, cur = [], [xs[0]]
        for w in xs[1:]:
            if w['x0'] - cur[-1]['x1'] <= gap:
                cur.append(w)
            else:
                groups.append(cur); cur = [w]
        groups.append(cur)
        return [{'x0': g[0]['x0'], 'x1': g[-1]['x1'],
                 'text': "".join(t['text'] for t in g)} for g in groups]
    jp_raw = [w for w in words if JP_RE.search(w['text'])]
    jp = []
    for _r in cluster_rows(jp_raw, args.row_thr):
        jp.extend(m for m in _merge_row_names(_r) if (m['x1'] - m['x0']) >= 18)
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
        num_x_lo, num_x_hi = cx - 58, cx - 12  # 左端を-45→-58に拡大: 3桁番号(名前の左約46pt)を取りこぼさないため
        next_cx = block_centers[bi + 1] if bi + 1 < len(block_centers) else page_w
        # 名前/時刻の境界をデータ駆動で決める（固定 cx+120 だと名前列が狭く時刻が近い時刻表で
        # 先頭便を取りこぼす。例: マリンクス）。最初の時刻列 x0 を基準に、その左の停留所名
        # トークンの右端との中間を境界にする。ヘッダ「N便」・注記(※…)・方向見出し(⇒)は名前右端の
        # 計算から除外する（時刻列付近まで伸びて境界を右に押し、先頭便を落とすため）。
        _times_x = sorted(w['x0'] for w in words
                          if cx - 25 < w['x0'] < next_cx and TIME_RE.match(w['text']))
        _first_time = _times_x[0] if _times_x else cx + 120
        _names = [w for w in words
                  if cx - 25 <= w['x0'] < _first_time - 3 and JP_RE.search(w['text'])
                  and not re.match(r'^[\d０-９]{1,2}便$', w['text'])
                  and '⇒' not in w['text'] and not w['text'].startswith('※')
                  and len(w['text']) <= 16]
        _name_right = max((w['x1'] for w in _names), default=cx + 40)
        boundary = min((_name_right + _first_time) / 2, _first_time - 2) if _times_x else cx + 120
        x_lo, x_hi = cx - 20, boundary
        time_x_lo, time_x_hi = boundary, next_cx - 60 if next_cx < page_w else page_w

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

    # ページ上部の見出しから路線名「○○線／○○系統／○○ルート」を拾い、x位置が近いブロックへ
    # 割り当てる（路線名が停留所名にもファイル名にも無く、見出しにだけある例＝マリンクス等に対応）。
    # 「山らいず線（平日）」のような曜日付記は括弧分割で落ち「山らいず線」になる。線/系統を優先し、
    # 「第１ルート」等のサブ表記は 線/系統 が無い時だけ使う。
    _ph = page.height
    _titles = []   # (x0, name)
    for w in words:
        if w.get("top", 9999) > _ph * 0.16:
            continue
        for _t in re.split(r"[\s　（）()【】\[\]／/｜|,、]+", w.get("text", "")):
            _t = _t.strip()
            # 全文字が二重描画されたPDF（「山山ららいいずず線線」）を「山らいず線」に畳む
            if len(_t) >= 4 and len(_t) % 2 == 0 and _t[0::2] == _t[1::2]:
                _t = _t[0::2]
            _t = re.sub(r"(時刻表|時刻|ダイヤ|運行表|一覧表|表)$", "", _t)
            if not (2 <= len(_t) <= 20):
                continue
            if any(x in _t for x in ("新幹線", "ゆたか線", "福北", "鉄道", "番号", "種類")):
                continue   # 鉄道路線・「系統番号」等は除外（JR古賀線等のバス路線名は許可）
            if _t.endswith(("線", "系統", "ルート")):
                _titles.append((w.get("x0", 0), _t))
    if _titles:
        _primary = [(x, t) for x, t in _titles if t.endswith(("線", "系統"))]
        _pool = _primary if _primary else _titles
        for b in real_blocks:
            b["route_title"] = min(_pool, key=lambda t: abs(t[0] - b.get("name_col_x", 0)))[1]

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
    # (3b) 【汎用の整合チェック】どの便にも割り当てられなかった「時刻の列」を検知する。
    #   座標方式は便列の x 範囲を絞るため、範囲の外側にある便列が丸ごと落ちることがある
    #   （例：最終便の列が next_cx-60 の内側ぎりぎりだと欠落）。落ちた便を黙って捨てず、
    #   要確認として報告する（＝正しく失敗）。特定の時刻表に依存しない汎用処理。
    if real_blocks:
        page_times = [(w['x0'], w['top']) for w in words if TIME_RE.match(w['text'])]
        assigned_colx = [t['col_x'] for b in real_blocks for t in b['trips']]
        if page_times:
            for cxx in cluster_cols([round(x) for x, _ in page_times], args.col_gap):
                n_here = sum(1 for x, _ in page_times if abs(x - cxx) <= args.col_gap)
                covered = any(abs(cxx - acx) <= args.col_gap + 10 for acx in assigned_colx)
                if not covered and n_here >= 3:   # 3件以上まとまった未割当列＝便1本ぶんの可能性
                    needs.append({"type": "unassigned_time_column", "col_x": round(cxx, 1),
                                  "count": n_here,
                                  "message": (f"時刻が {n_here} 件まとまっている列(x≈{round(cxx)})が"
                                              "どの便にも割り当てられていません。便が1本まるごと"
                                              "抜けている可能性があります。原典と便数を照合してください。")})
            n_assigned_cells = sum(len(t['cells']) for b in real_blocks for t in b['trips'])
            # 抽出できた時刻セル数がページ上の時刻トークン数より大幅に少ない＝取りこぼしの目安
            if len(page_times) - n_assigned_cells >= max(5, int(len(page_times) * 0.1)):
                result["warnings"].append(
                    f"ページ上の時刻 {len(page_times)} 件に対し、便に割り当てられたのは "
                    f"{n_assigned_cells} 件でした。差が大きい場合は便や停留所の取りこぼしの"
                    "可能性があるため、原典と照合してください。")

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
