# -*- coding: utf-8 -*-
"""GTFS-JP 半自動生成アプリ（Streamlit MVP）。
時刻表(PDF/Excel)アップロード → 抽出 → Claudeで構造化 → 条件確認フォーム →
生成(apply_decisions + run_pipeline) → 検証結果・地図・GTFS-JP ダウンロード。

設計: 正確さの源は決定的スクリプト。LLM(Claude API)は構造化(Step2)の判断のみ。
PDF/Excelに無いメタ情報(事業者・運行日・運賃)は推測せず条件確認フォームで人が入力する。

起動: streamlit run app/app.py
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
import folium
import pandas as pd
from streamlit_folium import st_folium

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "gtfs-jp-creator" / "scripts"
APPLY_DECISIONS = REPO / "apply_decisions.py"
PY = sys.executable
sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_structure  # noqa: E402
sys.path.insert(0, str(SCRIPTS))
try:
    from detect_time_anomalies import detect_anomalies  # 編集後の疑いをライブ再計算
except Exception:
    detect_anomalies = None
try:
    from stop_name_merge import detect_variants, apply_merges, all_stop_names
except Exception:
    detect_variants = None

st.set_page_config(page_title="GTFS-JP メーカー", page_icon="🚌", layout="wide")

# --- 見た目（官公庁向けの信頼感ある青系テーマ。CSS注入なのでサーバ/ローカル問わず効く） ---
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700;800&display=swap');
  :root { --brand:#1E5FA8; --brand-dark:#123E70; --brand-2:#2E86C8; --brand-light:#E8F0FA;
          --ink:#16202B; --bg:#F4F7FC; --card:#FFFFFF; --border:#E1E8F2; }
  html, body, .stApp, [class*="css"], input, textarea, button {
    font-family:'Noto Sans JP','Inter',system-ui,-apple-system,sans-serif; }
  .stApp { background:
     radial-gradient(1200px 520px at 82% -8%, #E7F0FB 0%, rgba(231,240,251,0) 55%),
     radial-gradient(900px 440px at -6% 0%, #ECF3FC 0%, rgba(236,243,252,0) 50%),
     var(--bg); }
  [data-testid="stHeader"] { background: transparent; }
  .block-container { max-width:1120px; padding-top:1.0rem; }
  h1,h2,h3 { color:var(--brand-dark); font-weight:800; letter-spacing:.01em; }
  /* セクション見出し：短いグラデーションのアクセント下線でモダンに */
  h2 { border-bottom:none; padding-bottom:.1rem; margin-top:1.6rem; }
  h2::after { content:''; display:block; width:52px; height:3px; margin-top:.4rem;
    background:linear-gradient(90deg,var(--brand),var(--brand-2)); border-radius:3px; }
  /* ブランドヘッダー */
  @keyframes heroIn { from{opacity:0; transform:translateY(-8px)} to{opacity:1; transform:none} }
  .app-hero { position:relative; overflow:hidden; display:flex; align-items:center; gap:18px;
    background: linear-gradient(120deg,#0E3B6E 0%,#1E5FA8 55%,#2E86C8 100%);
    color:#fff; border-radius:18px; padding:22px 26px; margin:.1rem 0 1.1rem;
    box-shadow:0 12px 30px rgba(18,62,112,.28); animation:heroIn .5s ease; }
  .app-hero::after { content:''; position:absolute; right:-50px; top:-60px; width:240px; height:240px;
    background:radial-gradient(circle, rgba(255,255,255,.16), rgba(255,255,255,0) 62%); pointer-events:none; }
  .app-hero::before { content:''; position:absolute; left:-30px; bottom:-72px; width:170px; height:170px;
    background:radial-gradient(circle, rgba(255,255,255,.10), rgba(255,255,255,0) 60%); pointer-events:none; }
  .app-hero-icon { font-size:42px; line-height:1; background:rgba(255,255,255,.18);
    border-radius:14px; padding:8px 13px; box-shadow:inset 0 0 0 1px rgba(255,255,255,.22); }
  .app-hero-title { font-size:1.8rem; font-weight:800; letter-spacing:.02em; line-height:1.15; }
  .app-hero-title small { font-weight:600; font-size:.78rem; opacity:.9; margin-left:.6rem;
    padding:.16rem .55rem; border:1px solid rgba(255,255,255,.38); border-radius:999px; vertical-align:middle; }
  .app-hero-sub { font-size:.96rem; opacity:.94; margin-top:5px; }
  /* ボタン（通常＝青枠、主要＝グラデ塗り） */
  .stButton>button {
    border-radius:10px; border:1px solid var(--brand); color:var(--brand); background:#fff;
    font-weight:600; padding:.44rem 1.1rem; transition:all .16s ease; }
  .stButton>button:hover {
    background:var(--brand-light); color:var(--brand-dark); border-color:var(--brand);
    transform:translateY(-1px); box-shadow:0 4px 12px rgba(30,95,168,.18); }
  .stButton>button[kind="primary"], [data-testid="stFormSubmitButton"]>button, .stDownloadButton>button {
    background:linear-gradient(120deg,var(--brand),var(--brand-2)); color:#fff; border:none;
    font-weight:700; border-radius:10px; padding:.46rem 1.15rem;
    box-shadow:0 5px 15px rgba(30,95,168,.30); transition:all .16s ease; }
  .stButton>button[kind="primary"]:hover, [data-testid="stFormSubmitButton"]>button:hover,
  .stDownloadButton>button:hover { filter:brightness(1.07); transform:translateY(-1px); color:#fff; }
  /* カード群 */
  [data-testid="stFileUploaderDropzone"] { border-radius:12px; border:1.5px dashed #B9C9DF; background:#FBFDFF; }
  div[data-testid="stExpander"] { border:1px solid var(--border); border-radius:12px; background:var(--card);
    box-shadow:0 1px 2px rgba(20,50,90,.04); }
  [data-testid="stForm"] { border:1px solid var(--border); border-radius:14px; background:var(--card);
    padding:1.1rem 1.2rem; box-shadow:0 2px 10px rgba(20,50,90,.06); }
  [data-testid="stMetric"] { background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:.7rem .95rem; box-shadow:0 1px 3px rgba(20,50,90,.05); }
  [data-testid="stAlert"] { border-radius:12px; box-shadow:0 1px 4px rgba(20,50,90,.05); }
  [data-testid="stCaptionContainer"], .stCaption { color:#5B6B7C; }
  a, a:visited { color:var(--brand); }
  /* 入力欄を白背景でも枠が分かるように（クリックしないと欄が見えない問題の対策） */
  div[data-baseweb="input"], div[data-baseweb="base-input"],
  div[data-baseweb="select"] > div, div[data-baseweb="textarea"] {
    background:#F5F9FF !important; border:1.5px solid #B7C9E2 !important; border-radius:8px !important; }
  div[data-baseweb="input"]:focus-within, div[data-baseweb="select"] > div:focus-within,
  div[data-baseweb="textarea"]:focus-within {
    border-color:var(--brand) !important; box-shadow:0 0 0 3px rgba(30,95,168,.16) !important; }
  .stTextInput input, .stNumberInput input, .stDateInput input, .stTextArea textarea { background:transparent; }
  .stNumberInput button { background:#E9F0FB; border-color:#B7C9E2; }
  .stDateInput div[data-baseweb="input"] { background:#F5F9FF !important; }
</style>
""", unsafe_allow_html=True)
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run(cmd, cwd=None):
    r = subprocess.run([PY] + [str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=ENV, cwd=cwd)
    return r.returncode, r.stdout, r.stderr


def ss():  # session_state ショートカット
    return st.session_state


if "work" not in ss():
    ss().work = tempfile.mkdtemp(prefix="gtfsapp_")
WORK = Path(ss().work)

# マルチユーザ隔離: 利用者ごとにURLトークン(sid)を割り当て、保存も作業領域も分ける。
# 共有サーバで複数人が同時に使っても衝突しない。URL(?sid=...)を保つとリロードでも
# 自分の作業だけ復元できる（別の人には別のsid＝別ファイル）。
if "sid" not in ss():
    _sid = st.query_params.get("sid")
    if not _sid:
        import secrets
        _sid = secrets.token_hex(6)
        try:
            st.query_params["sid"] = _sid
        except Exception:
            pass
    ss()["sid"] = _sid
SID = ss()["sid"]

# 一時保存（自動）＋復元。保存先はサーバ/PCの安定フォルダ（セッション用tempとは別）、
# かつ sid ごとに分離。復元は「開いたとき1クリック」で事故を防ぐ（折衷案）。
AUTOSAVE_DIR = Path.home() / ".gtfs_jp_app"
AUTOSAVE_FILE = AUTOSAVE_DIR / f"session_{SID}.json"
# 保存する作業一式（費用の高い手作業＝抽出・時刻修正・路線割当・確定座標・検出・原本）。
# ③の入力欄(事業者/運賃/曜日)はウィジェット値なので復元対象外＝再入力（軽い）。
SAVE_KEYS = ["extract", "extract_token", "decision_spec", "detected", "confirmed",
             "source_display", "fare_matrix_doc"]


def autosave():
    if not ss().get("extract"):
        return
    try:
        payload = {k: ss().get(k) for k in SAVE_KEYS if ss().get(k) is not None}
        AUTOSAVE_DIR.mkdir(parents=True, exist_ok=True)
        AUTOSAVE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def restore_prompt():
    """起動時、前回の自動保存があれば『続きから復元/新規』を出す（extract未読込のときのみ）。"""
    if ss().get("extract") or ss().get("_restore_dismissed"):
        return
    if not AUTOSAVE_FILE.exists():
        return
    try:
        mt = datetime.datetime.fromtimestamp(AUTOSAVE_FILE.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        mt = ""
    st.info(f"💾 前回の作業（{mt}）が自動保存されています。続きから再開できます"
            "（③の運賃・曜日・事業者などの入力欄は再入力になります）。")
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("前回の続きから復元する", type="primary"):
        try:
            data = json.loads(AUTOSAVE_FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                ss()[k] = v
            ss()["_restore_dismissed"] = True
            st.rerun()
        except Exception as e:
            st.error("復元に失敗しました: " + str(e))
    if c2.button("新規で始める"):
        ss()["_restore_dismissed"] = True
        st.rerun()


st.markdown("""
<div class="app-hero">
  <div class="app-hero-icon">🚌</div>
  <div>
    <div class="app-hero-title">GTFS-JP メーカー<small>バス時刻表 → 標準フォーマット</small></div>
    <div class="app-hero-sub">バス時刻表（PDF・Excel）から、正確な GTFS-JP を半自動で生成します</div>
  </div>
</div>
""", unsafe_allow_html=True)
st.caption("正確さの源は決定的スクリプト、LLMは構造化の判断のみ、"
           "無い情報は推測せず確認フォームで入力します（誤りを作らない＝正しく失敗）。")

restore_prompt()   # 前回の自動保存があれば「続きから復元/新規」を提示
if ss().get("extract"):
    st.caption("💾 作業は自動保存されています。**このページのURLをブックマーク**しておくと、"
               "タブを閉じても同じURLを開けば『続きから復元』できます（他の人の作業とは分離）。")

# =====================================================================
# Step 1: アップロード → 抽出
# =====================================================================
st.header("① 時刻表をアップロード")
up = st.file_uploader("バス時刻表（.xlsx / PDF / OCR後の .md）", type=["xlsx", "pdf", "md"])
st.caption("📄 文字が選べるPDF・Excelはそのまま抽出。**画像化PDF（スキャン）**は、"
           "抽出するとアプリ内で**OCRして続行するボタン**が出ます（ターミナル不要）。")


def render_ocr_panel():
    """画像化PDFが検出されたとき、アプリ内でOCR(MinerU)を実行して続行できるパネル。
    ターミナル作業なしで『画像PDF→OCR→抽出』を一気通貫にする。"""
    src = ss().get("ocr_pending")
    if not src:
        return
    st.warning("この時刻表は**画像化PDF（文字情報なし）**でした。"
               "下のボタンで**アプリ内でOCR（文字起こし）して続行**できます。")
    st.caption("⏳ OCRはCPUだと数分〜数十分かかります（GPUなら数分）。"
               "MinerU pipeline（数字に強い）で実行します。OCRは誤読が起きるので、"
               "取り込み後に**時刻表の確認・修正**で原典と照合してください。")
    if st.button("🔎 アプリ内でOCRして続行する", type="primary"):
        md_out = WORK / "ocr.md"
        with st.spinner("OCR実行中…（画像PDFの文字起こし。時間がかかります）"):
            rc, so, se = run([SCRIPTS / "pdf_to_markdown.py", src,
                              "--engine", "mineru", "--lang", "japan", "-o", md_out])
        if rc == 0 and md_out.exists():
            ss().pop("ocr_pending", None)
            do_extract(md_out)              # OCR結果の .md から抽出して続行
            st.rerun()
        else:
            st.error("OCRに失敗しました。MinerU未導入の可能性があります"
                     "（`pip install -U \"mineru[core]\"`）。\n" + (se or "")[-800:])
            with st.expander("手動でOCRする場合のコマンド"):
                st.code(f'python skills/gtfs-jp-creator/scripts/pdf_to_markdown.py "{src}" '
                        f'--engine mineru --lang japan -o out.md', language="bash")
                st.caption("できた out.md を①に再アップロードしてください。")


def render_source_panel(where=""):
    """アップロードした原本（PDF/画像）を編集画面の隣で見られる開閉パネル。
    時刻・停留所・運賃を原典と横並びで照合できるようにし、誤読・誤りの見落としを減らす。"""
    sp = ss().get("source_display")
    if not sp or not Path(sp).exists():
        return
    low = sp.lower()
    with st.expander("📄 原本（アップロードした資料）を見ながら確認する", expanded=False):
        if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")):
            zoom = st.slider("拡大", 0.6, 2.5, 1.2, 0.2, key=f"imgzoom_{where}")
            st.image(sp, width=int(760 * zoom))
            return
        if not low.endswith(".pdf"):
            st.caption("原本プレビューはPDF・画像のみ対応です（Excel/md は元ファイルを直接ご参照ください）。")
            return
        try:
            import pymupdf
            doc = pymupdf.open(sp)
            npages = doc.page_count
        except Exception as e:
            st.caption("PDFを開けませんでした: " + str(e))
            return
        cc = st.columns([1, 2])
        page = int(cc[0].number_input("ページ", 1, npages, 1, key=f"srcpage_{where}")) if npages > 1 else 1
        zoom = cc[1].slider("拡大", 0.6, 2.5, 1.2, 0.2, key=f"srczoom_{where}")
        cache = WORK / f"srcpage_{page}.png"
        if not cache.exists():
            try:
                pix = doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(2.0, 2.0))
                pix.save(str(cache))
            except Exception as e:
                st.caption("ページを描画できませんでした: " + str(e))
                return
        st.image(str(cache), width=int(760 * zoom))
        st.caption("原典と**時刻・停留所名・運賃**を見比べてください。OCRは誤読があります。"
                   "違う所は上の表で直せます。")


def do_extract(src):
    ext_out = WORK / "extract.json"
    low = str(src).lower()
    # 原本プレビュー用に元ファイルを記録（OCR後の .md では上書きせず、元のPDF/画像を保持）。
    if not low.endswith(".md"):
        ss()["source_display"] = str(src)
    if low.endswith(".xlsx"):
        rc, so, se = run([SCRIPTS / "extract_timetable_excel.py", src, "-o", ext_out])
    elif low.endswith(".md"):
        rc, so, se = run([SCRIPTS / "extract_timetable_markdown.py", src, "-o", ext_out])
    else:
        rc, so, se = run([SCRIPTS / "extract_timetable_coords.py", src, "-o", ext_out])
    ss().pop("ocr_pending", None)   # 新しい抽出のたびに前回のOCR待ちを消す
    if rc == 0 and ext_out.exists():
        ex = json.loads(ext_out.read_text(encoding="utf-8"))
        # 画像化PDFで0停留所 → アプリ内OCRへ誘導（空のまま進めない）
        if not ex.get("blocks") and any(n.get("type") == "image_pdf_use_ocr"
                                        for n in ex.get("needs_confirmation", [])):
            ss()["ocr_pending"] = str(src)   # 下のOCRパネルで実行する
            return
        ss().extract = ex
        ss().extract_token = str(src)
        for k in ("decision_spec", "result", "confirmed"):
            ss().pop(k, None)
        # PDF/Excelに「書かれている」条件を検出し、③に候補として初期入力する（要確認）。
        cond_out = WORK / "conditions.json"
        run([SCRIPTS / "detect_conditions.py", src, "-o", cond_out])
        ss().detected = json.loads(cond_out.read_text(encoding="utf-8")) if cond_out.exists() else {}
        st.success("抽出しました。")
    else:
        st.error("抽出に失敗しました。\n" + se[-800:])


def apply_conditions_doc(path, tk, routes):
    """運賃・運行条件の資料(path)を検出し、③の入力に反映する。
    ③の各ウィジェットは key付きで session_state が優先されるため、ss().detected の更新に
    加えて**ウィジェットの session_state を直接書き換える**（そうしないと欄に反映されない）。
    反映したら True。呼び出し側で st.rerun() すること。"""
    co = WORK / "conditions2.json"
    run([SCRIPTS / "detect_conditions.py", path, "-o", co])
    if not co.exists():
        return False
    d2 = json.loads(co.read_text(encoding="utf-8"))
    merged = dict(ss().get("detected", {}) or {})
    for k, v in d2.items():
        if k == "_evidence":
            merged.setdefault("_evidence", {}).update(v or {})
        elif v not in (None, "", []):
            merged[k] = v
    ss()["detected"] = merged
    # ③の各ウィジェットは key付きで session_state が優先される。キー削除では Streamlit の
    # 内部状態が残り value= を読み直さないため、検出値を session_state に直接書き込む
    # （value= との二重指定でログ警告が出るが機能は正しい・利用者には無害）。
    _st = st.session_state
    fa, fc, fd = merged.get("fare_adult"), merged.get("fare_child"), merged.get("fare_disabled")
    if fa is not None:
        _st[f"fa_{tk}"] = int(fa)
        for r in routes:
            _st[f"rfa_{r['route_id']}_{tk}"] = int(fa)
    if fc is not None:
        _st[f"fc_{tk}"] = int(fc)
        for r in routes:
            _st[f"rfc_{r['route_id']}_{tk}"] = int(fc)
    if fd is not None:
        _st[f"fd_{tk}"] = int(fd)
        for r in routes:
            _st[f"rfd_{r['route_id']}_{tk}"] = int(fd)
    if merged.get("phone"):
        _st[f"tel_{tk}"] = merged["phone"]
    if merged.get("url"):
        _st[f"url_{tk}"] = merged["url"]
    if merged.get("start_date"):
        _st[f"st_{tk}"] = merged["start_date"]
    if merged.get("end_date"):
        _st[f"en_{tk}"] = merged["end_date"]
    if merged.get("holiday_syukujitsu"):
        _st[f"hs_{tk}"] = True
    if merged.get("holiday_nenmatsu"):
        _st[f"hn_{tk}"] = True
    if merged.get("holiday_obon"):
        _st[f"ho_{tk}"] = True
    if merged.get("days") and len(merged["days"]) == 7:
        for i in range(7):
            _st[f"day{i}_{tk}"] = bool(merged["days"][i])
    # 事業者情報（運行主体者資料などから）
    if merged.get("agency_name"):
        _st[f"agn_{tk}"] = merged["agency_name"]
    if merged.get("agency_official_name"):
        _st[f"agof_{tk}"] = merged["agency_official_name"]
    if merged.get("agency_id"):
        _st[f"agid_{tk}"] = merged["agency_id"]
    if merged.get("agency_zip"):
        _st[f"agz_{tk}"] = merged["agency_zip"]
    if merged.get("agency_address"):
        _st[f"aga_{tk}"] = merged["agency_address"]
    if merged.get("agency_president_name"):
        _st[f"agpn_{tk}"] = merged["agency_president_name"]
    # 区間運賃のExcel（三角の運賃早見表）→ 非均一なら区間運賃表に自動取り込み。
    # 均一（全区間同額）は手入力の方が速いので取り込まない（利用者の方針）。
    if str(path).lower().endswith(".xlsx"):
        _stops = []
        for _b in (ss().get("extract") or {}).get("blocks", []):
            for _s in _b.get("stops", []):
                nm = _s.get("name")
                if nm and nm not in _stops:
                    _stops.append(nm)
        fmj = WORK / "fare_matrix_doc.json"
        if fmj.exists():
            fmj.unlink()
        _args = [SCRIPTS / "parse_fare_matrix_excel.py", path, "-o", fmj]
        if _stops:
            _args += ["--stops", ",".join(_stops)]
        run(_args)
        fm = {}
        if fmj.exists():
            try:
                fm = json.loads(fmj.read_text(encoding="utf-8"))
            except Exception:
                fm = {}
        adult = fm.get("大人") or []
        prices = {p["price"] for p in adult}
        nm = Path(path).name
        if adult and len(prices) > 1:            # 非均一のみ自動取り込み
            ss()["fare_matrix_doc"] = adult
            _st[f"zonechk_{tk}"] = True
            _st.pop(f"zonedf_{tk}", None)         # 表を取り込み値で作り直す
            ss()["fare_matrix_doc_msg"] = (
                f"料金表『{nm}』から区間運賃 {len(adult)}区間を取り込みました"
                f"（{min(prices)}〜{max(prices)}円・③の表で要確認）。")
        elif adult and len(prices) == 1:
            ss()["fare_matrix_doc_msg"] = (
                f"料金表『{nm}』は均一運賃（{next(iter(prices))}円）のようです。"
                "③の運賃欄に手入力してください（均一は入力の方が速いため自動取り込みしません）。")
        elif not adult and _stops:
            # フィルタ無しでは表が読めるのに停留所一致が0＝表記ゆれの可能性を通知
            _args2 = [SCRIPTS / "parse_fare_matrix_excel.py", path, "-o", fmj]
            run(_args2)
            try:
                fm2 = json.loads(fmj.read_text(encoding="utf-8")) if fmj.exists() else {}
            except Exception:
                fm2 = {}
            if fm2.get("大人"):
                ss()["fare_matrix_doc_msg"] = (
                    f"料金表『{nm}』を読めましたが、停留所名が時刻表と一致しませんでした"
                    "（表記ゆれの可能性）。③の区間運賃表に手入力するか、名称をそろえてください。")
    return True


def run_generation(spec, muni, use_nom, hol):
    """spec から GTFS-JP を生成（apply_decisions→run_pipeline）。ss().result に結果を入れる。"""
    # 都道府県だけだと P11 の市域bboxが効かず、同名バス停を県内別所に誤マッチしやすい。
    if muni and not any(k in muni for k in ("市", "町", "村", "区")):
        st.warning(f"⚠ 対象自治体が「{muni}」（都道府県のみ）です。**市区町村まで**入れると"
                   "同名停留所の座標精度が大きく上がります（例: 福岡県築上町）。"
                   "このまま生成すると同名バス停の誤マッチが増える可能性があります。")
    (WORK / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    (WORK / "extract.json").write_text(json.dumps(ss().extract, ensure_ascii=False), encoding="utf-8")
    with st.spinner("構造化 → 生成 → 座標補完 → 検証 を実行中..."):
        rc, so, se = run([APPLY_DECISIONS, "--extract", WORK / "extract.json",
                          "--decisions", WORK / "spec.json", "--out", WORK / "structured.json"])
        if rc != 0:
            st.error("構造化(apply_decisions)に失敗:\n" + se[-800:]); return
        pref = muni
        for k in ("県", "都", "府", "道"):
            if k in muni:
                pref = muni[:muni.index(k) + 1]; break
        cfg = {"feed_name": "app_feed", "input_json": str(WORK / "structured.json"),
               "extract_json": str(WORK / "extract.json"), "output_dir": str(WORK / "out"),
               "context": muni, "p11_prefecture": pref, "use_nominatim": bool(use_nom),
               "interpolate_coords": True, "validate": True}
        if hol.get("syuku"):
            cfg["holiday_syukujitsu"] = str(SCRIPTS.parent / "references" / "data" / "syukujitsu.csv")
        if hol.get("nenmatsu"):
            cfg["holiday_nenmatsu"] = "12-29:01-03"
        if hol.get("obon"):
            cfg["holiday_obon"] = "08-13:08-15"
        (WORK / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        rc, so, se = run([SCRIPTS / "run_pipeline.py", "--config", WORK / "config.json"], cwd=REPO)
    ss().result = {"rc": rc, "log": se}
    st.success("完了しました。" if rc == 0 else "完了（警告/エラーあり）。")


SAMPLES = Path(__file__).resolve().parent / "samples"
if st.button("抽出する", type="primary", disabled=(up is None)) and up:
    src = WORK / up.name
    src.write_bytes(up.getbuffer())
    do_extract(src)
st.caption("サンプルで試す:")
c_b, c_c, c_d = st.columns([1, 1, 1])
if c_b.button("太宰府まほろば号（往復）"):
    do_extract(SAMPLES / "sample_dazaifu_mahoroba.xlsx")
if c_c.button("築城巡回線（循環・変則便）"):
    do_extract(SAMPLES / "sample_tsuiki_junkai.xlsx")
if c_d.button("こがバス（画像PDF→OCR）"):
    do_extract(SAMPLES / "sample_koga_ocr.md")

# 画像化PDFが検出されたら、アプリ内でOCRして続行できるパネルを出す
render_ocr_panel()

if "extract" in ss():
    ex = ss().extract
    blocks = ex.get("blocks", [])
    total_trips = sum(len(b.get("trips", [])) for b in blocks)
    st.info(f"ブロック {len(blocks)} / 便 計 {total_trips}")
    for b in blocks:
        trips = b.get("trips", [])
        # 便ごとに停留所数が異なる（循環・区間便）ため、代表は便[0]でなく全体の停留所列を使う
        full = [s.get("name") for s in b.get("stops", [])]
        if not full and trips:
            full = max(([c["name"] for c in t["cells"]] for t in trips), key=len, default=[])
        loop = bool(full) and full[0] == full[-1]
        tag = f"（始点=終点「{full[0]}」→循環とみられます）" if loop else ""
        st.write(f"- block {b.get('block_index')}（{b.get('direction_hint') or '方向見出しなし'}）"
                 f": 便 {len(trips)} / 停留所 {len(full)}{tag}")
        st.caption("　順: " + " → ".join(full))

    # ---- 停留所の名寄せ（表記ゆれの統合）----
    # OCR/原本のゆれで同じ停留所が別名に割れると別 stop_id になり網・座標・運賃が崩れる。
    # 検出して人が確定（似ていて別物もあるため自動統合はしない＝正しく失敗）。
    if detect_variants:
        _groups = detect_variants(all_stop_names(ex))
        if _groups:
            st.subheader("🔗 停留所の名寄せ（表記ゆれの確認）")
            st.caption("同じ停留所が別表記で分かれている可能性があります。**同じなら統合**してください"
                       "（別物ならチェックを外す）。統合すると1つの停留所にまとまります。")
            _tk = ss().get("extract_token", "")
            merge_map = {}
            for gi, g in enumerate(_groups):
                cols = st.columns([3, 2])
                on = cols[0].checkbox("統合する：" + " ／ ".join(g["names"]),
                                      value=True, key=f"mg_{_tk}_{gi}", help=g["reason"])
                canon = cols[1].selectbox("正規名（残す名前）", g["names"],
                                          key=f"mgc_{_tk}_{gi}", disabled=not on)
                if on:
                    for nm in g["names"]:
                        if nm != canon:
                            merge_map[nm] = canon
            if st.button("この内容で名寄せを反映", type="primary", disabled=not merge_map):
                n = apply_merges(ss().extract, merge_map)
                for k in ("decision_spec", "result", "confirmed", "anomalies_token"):
                    ss().pop(k, None)
                st.success(f"名寄せを反映しました（{n}箇所を統合）。②以降が新しい停留所で組み直されます。")
                st.rerun()

# =====================================================================
# Step 2: 路線の割り当て（多路線対応の構造化）
# =====================================================================
def _auto_route_rows(bs):
    """停留所集合が近いブロック＝同一路線(往復)とみなし、路線名・方向を自動割当（要確認・編集可）。

    OCR由来の表記ゆれ（濁点誤読など）で完全一致しないことがあるため、Jaccard類似度で判定する。
    """
    def _names(b):
        return set(s.get("name") for s in b.get("stops", []))
    grouped = []  # [[代表stop集合, [block index...]], ...]
    for i, b in enumerate(bs):
        ns = _names(b)
        placed = False
        for g in grouped:
            inter = len(ns & g[0]); uni = len(ns | g[0]) or 1
            if inter / uni >= 0.6:  # 6割以上の停留所を共有 → 同一路線の別方向
                g[1].append(i); placed = True
                break
        if not placed:
            grouped.append([ns, [i]])
    rows = []
    for gi, (_rep, members) in enumerate(grouped):
        # 路線名はグループで1つ（往復は同じ路線名・方向0/1）。代表ブロックの端点から作る。
        nm0 = [s.get("name") for s in bs[members[0]].get("stops", [])]
        rname = f"{nm0[0]}～{nm0[-1]}" if nm0 else f"路線{gi + 1}"
        for d, bi in enumerate(members):
            nm = [s.get("name") for s in bs[bi].get("stops", [])]
            rows.append({"ブロック": bi, "見出し": bs[bi].get("direction_hint") or "",
                         "停留所数": len(nm), "路線名": rname, "方向(0/1)": d % 2,
                         "運行日": "③の曜日"})
    return rows


# 運行日パターン → 曜日フラグ（平日/土日で時刻が違う時刻表は、ブロックごとに変える）
DAY_PATTERNS = {
    "平日(月〜金)": dict(mon=1, tue=1, wed=1, thu=1, fri=1, sat=0, sun=0),
    "土日祝": dict(mon=0, tue=0, wed=0, thu=0, fri=0, sat=1, sun=1),
    "毎日": dict(mon=1, tue=1, wed=1, thu=1, fri=1, sat=1, sun=1),
}
PATTERN_SID = {"平日(月〜金)": "WD", "土日祝": "WE", "毎日": "ALL"}


if "extract" in ss():
    st.header("② 路線の割り当て（どのブロックがどの路線・方向か）")
    st.caption("停留所の並びが同じブロックを自動で**同じ路線**にまとめ、方向(0/1)を割り振りました（要確認）。"
               "複数路線・往復の対応づけが違うときは表を編集してください。路線名も変更できます。")
    blocks_e = ex.get("blocks", [])
    base_df = pd.DataFrame(_auto_route_rows(blocks_e))
    edited = st.data_editor(
        base_df, hide_index=True, use_container_width=True,
        key=f"route_editor_{ss().get('extract_token', '')}",
        column_config={
            "ブロック": st.column_config.NumberColumn("ブロック", disabled=True),
            "見出し": st.column_config.TextColumn("見出し(参考)", disabled=True),
            "停留所数": st.column_config.NumberColumn("停留所数", disabled=True),
            "路線名": st.column_config.TextColumn("路線名", help="同じ路線名のブロックが1つの路線にまとまる"),
            "方向(0/1)": st.column_config.SelectboxColumn("方向(0/1)", options=[0, 1], required=True),
            "運行日": st.column_config.SelectboxColumn(
                "運行日", options=["③の曜日"] + list(DAY_PATTERNS), required=True,
                help="平日/土日で時刻が違う時刻表は、便のブロックごとに運行日を変える（別ダイヤとして出力）"),
        },
    )
    st.caption("⚠ **平日と土日で時刻が違う**時刻表は、該当ブロックの『運行日』を変えてください"
               "（別カレンダーで出力されます）。同じなら『③の曜日』のままでOK。")
    # 割り当て表から decision_spec を構築（同じ路線名のブロックを1路線にまとめる）
    name_blocks, block_dir, headsign, block_pattern = {}, {}, {}, {}
    for _, r in edited.iterrows():
        bi = int(r["ブロック"]); nm = str(r["路線名"]).strip() or f"路線{bi}"
        name_blocks.setdefault(nm, []).append(bi)
        block_dir[str(bi)] = int(r["方向(0/1)"])
        block_pattern[str(bi)] = str(r.get("運行日") or "③の曜日")
        dh = blocks_e[bi].get("direction_hint")
        if dh:
            headsign[str(bi)] = dh
    routes = [{"route_id": f"R{i + 1:02d}", "route_long_name": nm, "blocks": bidx, "circular": False}
              for i, (nm, bidx) in enumerate(name_blocks.items())]
    ss().decision_spec = {"routes": routes, "block_direction": block_dir, "block_headsign": headsign,
                          "block_pattern": block_pattern,
                          "exclude_reserve": True, "exclude_unnumbered": False, "stop_key": "name"}
    if len(routes) > 1:
        st.info(f"{len(routes)} 路線として構成します: " + " / ".join(r["route_long_name"] for r in routes))
    with st.expander("詳細（任意：Claude構造化 / decision-spec の確認・上書き）"):
        key = st.text_input("ANTHROPIC_API_KEY（環境変数があれば空でOK）", type="password", value="")
        api_key = key or os.environ.get("ANTHROPIC_API_KEY", "")
        if st.button("Claudeで構造化（上書き）"):
            if not api_key:
                st.warning("APIキーが未設定です。上の表での割り当てをそのまま使えます。")
            else:
                try:
                    with st.spinner("Claude が構造を判断中..."):
                        ss().decision_spec = claude_structure.structure(ss().extract, api_key)
                    st.success("構造化しました（上の表より優先）。")
                except Exception as e:
                    st.error(f"Claude 呼び出し失敗: {e}")
        st.caption("現在の decision-spec:")
        st.code(json.dumps(ss().get("decision_spec", {}), ensure_ascii=False, indent=2), language="json")

# =====================================================================
# 自動確認: システム側でまず時刻表から読み取れた内容を提示する。
# （＝先に全部を質問しない。ここに無い＝PDFに無い情報だけを下の③で後から質問する）
# =====================================================================
if ss().get("decision_spec"):
    spec0 = ss()["decision_spec"]
    blocks0 = ss().extract.get("blocks", [])
    total0 = sum(len(b.get("trips", [])) for b in blocks0)
    st.subheader("自動確認の結果（時刻表から読み取れたこと）")
    st.caption("システムがまず確認した内容です。ここに出ていない情報は PDF/Excel に"
               "書かれていないため、推測せず下の③で質問します。")
    auto = [f"便・停留所: ブロック {len(blocks0)} / 便 計 {total0}（停留所順は上に表示）"]
    for b in blocks0:
        dh = b.get("direction_hint")
        if dh:
            auto.append(f"方向（block {b.get('block_index')}）: 見出しから「{dh}」を自動検出")
    # 循環は「始点=終点」のときだけ"とみられる"と提示（表からは確定不可なので断定しない／③で確認）。
    for b in blocks0:
        names = [s.get("name") for s in b.get("stops", [])]
        if names and names[0] == names[-1]:
            auto.append(f"循環の可能性（block {b.get('block_index')}）: 始点と終点が同じ"
                        f"「{names[0]}」→循環路線とみられます（③で確認）")
    st.success("自動で分かったこと:\n" + "\n".join("・" + a for a in auto))
    nc = list(ss().extract.get("needs_confirmation", []))
    nc += [{"message": w} for w in ss().extract.get("warnings", [])]
    if nc:
        st.warning("要確認（原典と照合してください）:\n"
                   + "\n".join("・" + (x.get("message") if isinstance(x, dict) else str(x)) for x in nc))
    st.info("PDF/Excel に無いので③で質問します: 路線名 / 事業者名・法人番号・URL・電話 / "
            "運賃 / 運行する曜日 / 有効期間 / 対象自治体（座標補完用）")

# =====================================================================
# 時刻表の確認・修正: 全便・全停留所の時刻を表で出し、原典と目視照合して直接編集する。
# OCR誤読の疑いはヒントとして併記（検出は detect_time_anomalies）。自動では書き換えない。
# =====================================================================
if "extract" in ss():
    tok = ss().get("extract_token", "")
    if ss().get("anomalies_token") != tok:
        (WORK / "_ext_check.json").write_text(json.dumps(ss().extract, ensure_ascii=False), encoding="utf-8")
        run([SCRIPTS / "detect_time_anomalies.py", WORK / "_ext_check.json", "-o", WORK / "anomalies.json"])
        ap = WORK / "anomalies.json"
        ss().anomalies = json.loads(ap.read_text(encoding="utf-8")) if ap.exists() else []
        ss().anomalies_token = tok
    anomalies = ss().get("anomalies", [])
    blocks_t = ss().extract.get("blocks", [])
    if blocks_t:
        st.subheader("⏰ 時刻表の確認・修正（全便・全停留所）")
        render_source_panel("tt")   # 原本（PDF/画像）を並べて照合できるパネル
        n_an = len(anomalies)
        st.caption("抽出した**全時刻**です。原典（紙やPDF）と見比べて、違うセルを直接直してください。"
                   "空欄＝通過。**停留所名も直接編集**でき、**行を選んで削除**もできます"
                   "（「待機時間」「○○出発」など停留所でない行を消す／表記を直す）。"
                   + (f"OCR誤読の疑い **{n_an}件** は各表の下に列挙しています。" if n_an else "")
                   + "直したら『この時刻表で確定して反映』を押してください（自動では書き換えません）。")
        edited_blocks = {}
        issue_tot = {"rev": 0, "inval": 0, "an": 0}
        for b in blocks_t:
            bi = b.get("block_index")
            stops = [s.get("name") for s in b.get("stops", [])]
            trips = b.get("trips", [])
            labels = []
            for j, t in enumerate(trips):
                _tn = t.get("trip_number")
                if t.get("label"):
                    lab = str(t["label"])
                elif _tn:
                    _tn = str(_tn).strip()
                    lab = _tn if "便" in _tn else f"{_tn}便"   # 「第1便」は二重付与しない
                else:
                    lab = f"便{j + 1}"
                labels.append(f"{lab}#{j}")   # 重複ラベル対策に内部で一意化
            # 各便の時刻を master停留所(行)に揃える（便は master の部分列）
            per_trip = []
            for t in trips:
                cells = t.get("cells", []); k = 0; mp = {}
                for i, sn in enumerate(stops):
                    if k < len(cells) and cells[k].get("name") == sn:
                        _tt = cells[k].get("time") or ""
                        _mt = re.match(r"(\d{1,2}):(\d{2})", _tt)
                        mp[i] = f"{int(_mt.group(1)):02d}:{_mt.group(2)}" if _mt else _tt[:5]
                        k += 1
                per_trip.append(mp)
            rows = []
            for i, sn in enumerate(stops):
                row = {"停留所": sn}
                for j, lab in enumerate(labels):
                    row[lab] = per_trip[j].get(i, "")
                rows.append(row)
            df = pd.DataFrame(rows)
            dh = b.get("direction_hint")
            st.markdown(f"**block {bi}**" + (f"（{dh}）" if dh else ""))
            colcfg = {"停留所": st.column_config.TextColumn(
                "停留所", help="停留所名を直接直せます。行の左端で選ぶと削除でき、"
                "『待機時間』『渡船場出発』のような停留所でない行を消せます。")}
            for lab in labels:
                colcfg[lab] = st.column_config.TextColumn(lab.split("#")[0])
            ed = st.data_editor(df, hide_index=True, use_container_width=True,
                                key=f"tt_{tok}_{bi}", column_config=colcfg, num_rows="dynamic")

            def _enm(i):   # 編集後の停留所名（削除/改名を反映）
                try:
                    return str(ed.iloc[i].get("停留所", "") or "").strip()
                except Exception:
                    return ""
            edited_blocks[bi] = (ed, labels, stops)
            # ---- 編集内容をその場で再チェック（逆行・不正値・OCR疑い）----
            edited_cells = []   # 便ごとの [{i,name,min,time}]
            inval = []          # 非時刻の入力（誤入力の疑い）
            for j, lab in enumerate(labels):
                cs = []
                for i in range(len(ed)):
                    if not _enm(i):
                        continue   # 削除された（空の）停留所行はスキップ
                    v = str(ed.iloc[i][lab]).strip()
                    if not v or v.lower() == "nan":
                        continue
                    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", v)
                    if not m:
                        inval.append((lab.split("#")[0], _enm(i), v)); continue
                    cs.append({"i": i, "name": _enm(i),
                               "min": int(m.group(1)) * 60 + int(m.group(2)),
                               "time": f"{int(m.group(1)):02d}:{m.group(2)}:00"})
                edited_cells.append(cs)
            css = pd.DataFrame("", index=ed.index, columns=ed.columns)
            rev = []            # 逆行セル（誤りの疑い＝赤）
            nextday = []        # 日跨ぎ（夜→翌朝。逆行でない＝青。GTFSは24時超で表す）
            for j, cs in enumerate(edited_cells):
                prev = None
                for c in cs:
                    m = c["min"]
                    if prev is not None and m < prev:
                        # 日跨ぎ判定: 翌日(+24h)にすると前から自然に続く(3時間以内で増える)なら日跨ぎ。
                        # 待機時間(0:11等)は翌日にしても差が大きく、逆行(赤)として残す。
                        if 0 <= (m + 1440) - prev <= 180:
                            m += 1440
                            nextday.append((labels[j].split("#")[0], c["name"], c["time"][:5]))
                            css.iloc[c["i"], ed.columns.get_loc(labels[j])] = \
                                "background-color:#1565c0;color:#ffffff"   # 青＝日跨ぎ（翌日）
                        else:
                            rev.append((labels[j].split("#")[0], c["name"], c["time"][:5]))
                            css.iloc[c["i"], ed.columns.get_loc(labels[j])] = \
                                "background-color:#c62828;color:#ffffff;font-weight:700"   # 赤＝逆行
                    prev = m
            live_an = []        # OCR誤読の疑い（編集後の値で再計算）
            if detect_anomalies:
                tmpb = {"block_index": bi, "trips": [
                    {"cells": [{"name": c["name"], "time": c["time"]} for c in cs]} for cs in edited_cells]}
                try:
                    live_an = detect_anomalies({"blocks": [tmpb]})
                except Exception:
                    live_an = []
            issue_tot["rev"] += len(rev)
            issue_tot["inval"] += len(inval)
            issue_tot["an"] += len(live_an)
            issue_tot["nd"] = issue_tot.get("nd", 0) + len(nextday)
            msgs = []
            if rev: msgs.append(f"🔴 時刻の逆行 {len(rev)}件")
            if inval: msgs.append(f"⚠ 時刻でない値 {len(inval)}件")
            if live_an: msgs.append(f"🟠 OCR誤読の疑い {len(live_an)}件")
            if nextday: msgs.append(f"🔵 日跨ぎ（翌日） {len(nextday)}件")
            if msgs:
                _sev = rev or inval or live_an   # 赤/非時刻/OCRは要対応、青(日跨ぎ)だけなら情報
                (st.warning if _sev else st.info)(
                    "　／　".join(msgs)
                    + ("　🔴赤=要修正の逆行　🔵青=日跨ぎ（翌日・誤りではない。GTFSは24時超で出力）"
                       if (rev or nextday) else "")
                    + "　— 原典と照合して直すと自動で再チェックします。")
                if rev:
                    st.caption("逆行: " + " ／ ".join(f"{l} {s} {t}" for l, s, t in rev[:8])
                               + (" ほか" if len(rev) > 8 else ""))
                if nextday:
                    st.caption("日跨ぎ(翌日): " + " ／ ".join(f"{l} {s} {t}" for l, s, t in nextday[:8]))
                if inval:
                    st.caption("非時刻: " + " ／ ".join(f"{l} {s}「{v}」" for l, s, v in inval[:8]))
                if live_an:
                    st.caption("疑い: " + " ／ ".join(
                        f"{a['stop_name']} {a['current'][:5]}→"
                        f"{(a['suggested'][:5] if a.get('suggested') else '要確認')}" for a in live_an[:8]))
                if rev or nextday:
                    with st.expander("色付きで表示（🔴逆行=要修正／🔵日跨ぎ=翌日）"):
                        st.dataframe(ed.style.apply(lambda _x: css, axis=None),
                                     hide_index=True, use_container_width=True)
            else:
                st.caption("✅ 逆行なし・全セルが妥当な時刻です。")
        if issue_tot["rev"] or issue_tot["inval"] or issue_tot["an"]:
            st.info("反映前チェック: " + "　".join(filter(None, [
                f"🔴 逆行 {issue_tot['rev']}件" if issue_tot["rev"] else "",
                f"⚠ 非時刻 {issue_tot['inval']}件" if issue_tot["inval"] else "",
                f"🟠 OCR疑い {issue_tot['an']}件" if issue_tot["an"] else ""]))
                + " が残っています。直してから反映するのがおすすめです（このまま反映も可）。")
        if st.button("この時刻表で確定して反映", type="primary"):
            for b in blocks_t:
                bi = b.get("block_index")
                if bi not in edited_blocks:
                    continue
                ed, labels, stops = edited_blocks[bi]
                # 編集後の停留所名（空欄＝削除された行）。行ごとに1つ。
                new_names = [str(ed.iloc[i].get("停留所", "") or "").strip() for i in range(len(ed))]
                # ブロックの master 停留所を編集後で更新（空行＝待機時間等は除外）
                b["stops"] = [{"name": nm} for nm in new_names if nm]
                for j, t in enumerate(b.get("trips", [])):
                    lab = labels[j] if j < len(labels) else None
                    newcells = []
                    for i in range(len(ed)):
                        nm = new_names[i]
                        if not nm or lab is None or lab not in ed.columns:
                            continue   # 削除された停留所行はスキップ
                        val = str(ed.iloc[i][lab]).strip()
                        m = re.match(r"^(\d{1,2}):(\d{2})", val)
                        if not m:
                            continue
                        newcells.append({"seq": len(newcells) + 1, "num": None, "name": nm,
                                         "time": f"{int(m.group(1)):02d}:{m.group(2)}:00", "reserve": False})
                    t["cells"] = newcells; t["n_stops"] = len(newcells)
            for k in ("decision_spec", "result", "confirmed", "anomalies_token"):
                ss().pop(k, None)
            st.success("時刻表を反映しました。③で条件を入れて生成してください。")
            st.rerun()

# =====================================================================
# Step 3: PDF/Excelに無い項目だけを後から質問（自動確認の後）
# =====================================================================
if ss().get("decision_spec"):
    st.header("③ PDF/Excel に無い項目を入力（不足分の質問）")
    st.caption("上の②でシステムが確認した結果、時刻表に書かれていない項目です。"
               "推測せず入力してください（不明は空欄でOK＝暫定/要確認として入る。ただし路線名は必須）。"
               "下の『生成する』を押すと入力が一括で反映されます。")
    # 循環の自動検出（既定値の提案に使う。始点=終点なら循環とみられる）
    _loop = False
    for b in ss().extract.get("blocks", []):
        _n = [s.get("name") for s in b.get("stops", [])]
        if _n and _n[0] == _n[-1]:
            _loop = True
            break
    _routes_now = ss()["decision_spec"]["routes"]
    det = ss().get("detected", {}) or {}
    tk = ss().get("extract_token", "")
    _ev = det.get("_evidence", {})
    if _ev:
        labels = {"fare_adult": "大人運賃", "fare_child": "小児運賃", "fare_disabled": "障がい者運賃",
                  "days": "運行曜日", "holiday_syukujitsu": "祝日運休", "holiday_nenmatsu": "年末年始運休",
                  "holiday_obon": "お盆運休", "start_date": "有効期間開始", "end_date": "有効期間終了",
                  "phone": "電話", "url": "URL"}
        _fill_ev = {k: v for k, v in _ev.items() if k != "date_stale"}
        if _fill_ev:
            st.info("🔎 PDF/Excel から検出した項目を下に**初期入力（要確認）**しました。原典と照合してください: "
                    + " ／ ".join(f"{labels.get(k, k)}「{_fill_ev[k]}」" for k in _fill_ev))
    if det.get("date_stale"):
        st.warning("📅 古い日付（" + str(det["date_stale"]) + "）を検出しました。古い資料の改正日の可能性が高いため、"
                   "**有効期間には自動入力していません**。正しい有効期間を下の欄に入力してください。")
    # 複数の異なる運賃を検出（路線で違う可能性）→ 単一自動入力せず、候補を提示して路線ごとに割り当てさせる
    _multi_fare = det.get("fare_multiple") and len(_routes_now) > 1
    if det.get("fare_candidates"):
        st.warning("💴 PDF に複数の運賃候補がありました（路線で異なる可能性）。"
                   + "／".join(f"{c['category']}{c['price']}円" for c in det["fare_candidates"])
                   + " — 下の**路線ごとの運賃**で割り当ててください（勝手に1つを全路線に適用しません）。")
    # 運賃・運行条件などが別資料(Excel/Word/PDF)にある場合、それをアップロードすると
    # 検出して③に初期入力する（時刻表と同じ発想。値は利用者が確認）。フォーム外に置く。
    cond_doc = st.file_uploader("運賃・運行条件などの資料があれば（任意・PDF/Excel/Word/テキスト）",
                                type=["pdf", "xlsx", "md", "txt", "docx"], key=f"conddoc_{tk}",
                                help="運賃・運行日・事業者情報などを検出して③に初期入力します。"
                                     "区間ごとに運賃が違う『運賃早見表（三角の表）』のExcelは、区間運賃の表へ"
                                     "自動取り込みします（均一運賃は入力の方が速いので手入力のまま）。値は必ず要確認。")
    if cond_doc is not None and ss().get("conddoc_name") != cond_doc.name:
        _cp = WORK / ("cond_" + cond_doc.name)
        _cp.write_bytes(cond_doc.getbuffer())
        if apply_conditions_doc(_cp, tk, _routes_now):
            ss()["conddoc_name"] = cond_doc.name
            st.success(f"資料『{cond_doc.name}』から運賃・運行条件を検出し、③に初期入力しました（要確認）。")
            st.rerun()
    _days_def = det.get("days") or [1, 1, 1, 1, 1, 0, 0]
    # 区間運賃（停留所ごと・区間ごとに運賃が違う）。st.form内ではトグルで表示を
    # 切り替えられないため、トグルはフォーム外に置き、ON時だけフォーム内に表を出す。
    _stops_all = []
    for _b in ss().extract.get("blocks", []):
        for _s in _b.get("stops", []):
            _nm = _s.get("name")
            if _nm and _nm not in _stops_all:
                _stops_all.append(_nm)
    if ss().get("fare_matrix_doc_msg"):
        st.info(ss()["fare_matrix_doc_msg"])
    zone_fare = st.checkbox("区間運賃にする（停留所ごと・区間ごとに運賃が違う）", key=f"zonechk_{tk}",
                            help="チェックすると③の中に『区間運賃の表（発×着）』が出ます。区間ごとに金額を入れます。")
    with st.form("conditions"):
        c1, c2, c3 = st.columns(3)
        if len(_routes_now) == 1:
            route_name = c1.text_input("路線名", value=_routes_now[0].get("route_long_name", ""))
        else:
            route_name = ""  # 多路線は②の割り当てで路線名を設定
            c1.caption("路線名は②で設定済み: " + " / ".join(r["route_long_name"] for r in _routes_now))
        muni = c1.text_input("対象自治体（都道府県＋市区町村）", value="福岡県",
                             help="P11の都道府県/市域制約に使用。市区町村まで入れると同名停留所の精度が大きく上がります")
        c1.caption("⚠ **市区町村まで**入れてください（例: 福岡県築上町）。"
                   "都道府県だけだと同名のバス停を県内の別の場所に誤って合わせる恐れがあります。")
        if len(_routes_now) == 1:
            c1.write("運賃（区分別・円。0は未設定。PDF記載は検出して初期入力）")
            fc1, fc2, fc3 = c1.columns(3)
            fare_adult = fc1.number_input("大人", min_value=0, value=int(det.get("fare_adult") or 0), step=10, key=f"fa_{tk}")
            fare_child = fc2.number_input("小児", min_value=0, value=int(det.get("fare_child") or 0), step=10, key=f"fc_{tk}")
            fare_disabled = fc3.number_input("障がい者", min_value=0, value=int(det.get("fare_disabled") or 0), step=10, key=f"fd_{tk}")
        else:
            fare_adult = fare_child = fare_disabled = 0
            c1.caption("運賃は下の『路線ごとの運賃』で入力（路線で違う場合に対応）")
        zone_df = None
        zone_symmetric = False
        if zone_fare and _stops_all:
            st.markdown("**区間運賃の表（行＝発／列＝着。セルに金額。空欄＝設定なし）**　"
                        "表の右上と左下がそれぞれ**上り・下り**にあたり、別々の金額を入れられます。")
            zone_symmetric = st.checkbox("上り・下りを同額にする（外すと方向で別料金にできる）",
                                         value=True, key=f"zsym_{tk}",
                                         help="ON: 片方向だけ入れれば逆向きも同額に自動補完。"
                                              "OFF: A→B と B→A（＝上り/下り）を別々の金額で入力できます。")
            _doc_fm = ss().get("fare_matrix_doc") or []
            _fm_lookup = {(m["from"], m["to"]): m["price"] for m in _doc_fm}
            _zbase = pd.DataFrame(
                [[_fm_lookup.get((_o, _d)) for _d in _stops_all] for _o in _stops_all],
                index=_stops_all, columns=_stops_all)
            _zbase.insert(0, "発／着", _stops_all)
            _zcfg = {"発／着": st.column_config.TextColumn("発／着", disabled=True)}
            for _c in _stops_all:
                _zcfg[_c] = st.column_config.NumberColumn(_c, min_value=0, step=10, format="%d")
            zone_df = st.data_editor(_zbase, hide_index=True, key=f"zonedf_{tk}",
                                     column_config=_zcfg, use_container_width=False)
            st.caption(f"{len(_stops_all)}停留所。対角（同一停留所）は空欄でOK。"
                       "**上り・下りで運賃が違う場合は上のチェックを外し、両方向のセルに入力**してください。"
                       "Excelの表をコピー＆貼り付けも可。乗れる区間だけの入力でも構いません。")
        # 路線別運賃（多路線で運賃が違う場合）。検出が単一区分はその値を各路線の既定に。
        rfares_in = {}
        if len(_routes_now) > 1:
            st.markdown("**路線ごとの運賃（円・0は未設定）**")
            for r in _routes_now:
                rid = r["route_id"]; rnm = r.get("route_long_name", rid)
                pcols = st.columns([3, 1, 1, 1])
                pcols[0].markdown(f"<div style='padding-top:8px'>{rnm}</div>", unsafe_allow_html=True)
                ra = pcols[1].number_input("大人", min_value=0, value=int(det.get("fare_adult") or 0), step=10, key=f"rfa_{rid}_{tk}")
                rch = pcols[2].number_input("小児", min_value=0, value=int(det.get("fare_child") or 0), step=10, key=f"rfc_{rid}_{tk}")
                rdi = pcols[3].number_input("障がい者", min_value=0, value=int(det.get("fare_disabled") or 0), step=10, key=f"rfd_{rid}_{tk}")
                rfares_in[rid] = (ra, rch, rdi)
        c2.caption("**事業者情報**（正式提出に必要。自治体はご自身の情報を入力）")
        ag_name = c2.text_input("事業者名", value="", key=f"agn_{tk}")
        ag_official = c2.text_input("正式名称（登記名。空なら事業者名を使用）", value="", key=f"agof_{tk}")
        ag_id = c2.text_input("法人番号（13桁・不明なら空）", value="", key=f"agid_{tk}")
        ag_zip = c2.text_input("郵便番号（例 811-2192）", value="", key=f"agz_{tk}")
        ag_addr = c2.text_input("住所", value="", key=f"aga_{tk}")
        agp1, agp2 = c2.columns(2)
        ag_pres_pos = agp1.text_input("代表者 役職", value="", key=f"agpp_{tk}", help="例: 町長・市長・社長")
        ag_pres_name = agp2.text_input("代表者 氏名", value="", key=f"agpn_{tk}")
        ag_url = c2.text_input("URL", value=det.get("url", ""), key=f"url_{tk}")
        ag_phone = c2.text_input("電話", value=det.get("phone", ""), key=f"tel_{tk}")
        is_circular = c3.checkbox("循環路線（始点に戻る）", value=_loop,
                                  help="始点=終点を検出すると自動でチェック。違えば外してください。")
        headsign = c3.text_input("行き先表示（方向名）", value="",
                                 placeholder="例: 太宰府市役所 方面",
                                 help="バスの前面に出る行き先。空なら自動で行先ベースの『○○方面』にします"
                                      "（右回り/左回り/循環も分かりやすい『○○方面』に変換）。"
                                      "特定の表示にしたいときだけ入力。例:『太宰府市役所 方面』")
        st.write("運行する曜日")
        d = st.columns(7)
        days = [d[i].checkbox(x, value=bool(_days_def[i]), key=f"day{i}_{tk}")
                for i, x in enumerate(["月", "火", "水", "木", "金", "土", "日"])]
        c4, c5 = st.columns(2)
        start = c4.text_input("有効期間 開始 (YYYYMMDD)", value=det.get("start_date", ""), key=f"st_{tk}")
        end = c5.text_input("有効期間 終了 (YYYYMMDD)", value=det.get("end_date", ""), key=f"en_{tk}")
        st.write("運休日（祝日・年末年始・お盆。該当する場合のみチェック）")
        h1, h2, h3 = st.columns(3)
        hol_syuku = h1.checkbox("祝日は運休", value=bool(det.get("holiday_syukujitsu")), key=f"hs_{tk}",
                                help="内閣府の祝日データ（同梱・〜2027年）で祝日を運休に展開")
        hol_nenmatsu = h2.checkbox("年末年始運休", value=bool(det.get("holiday_nenmatsu")), key=f"hn_{tk}",
                                   help="12/29〜1/3 を運休に展開")
        hol_obon = h3.checkbox("お盆運休", value=bool(det.get("holiday_obon")), key=f"ho_{tk}",
                               help="8/13〜8/15 を運休に展開")
        # 個別の運行日・運休日（臨時運休・特別運行）。calendar_dates(2=運休/1=臨時運行)に積む。
        st.write("個別の運行日・運休日（臨時運休・特別運行がある日。無ければ空でOK）")
        _cd_base = pd.DataFrame({"日付": pd.Series([], dtype="datetime64[ns]"),
                                 "種別": pd.Series([], dtype="object")})
        cd_editor = st.data_editor(
            _cd_base, num_rows="dynamic", key=f"cd_{tk}", use_container_width=False,
            column_config={
                "日付": st.column_config.DateColumn("日付", format="YYYY-MM-DD"),
                "種別": st.column_config.SelectboxColumn("種別", options=["運休", "臨時運行"],
                                                         default="運休", required=True)})
        cdp1, cdp2, cdp3 = st.columns([1, 1.4, 1.4])
        cd_use_period = cdp1.checkbox("期間で運休", value=False, key=f"cdp_{tk}",
                                      help="下の開始〜終了を毎日運休に（季節運休など）")
        cd_ps = cdp2.text_input("運休期間 開始(YYYYMMDD)", value="", key=f"cdps_{tk}")
        cd_pe = cdp3.text_input("運休期間 終了(YYYYMMDD)", value="", key=f"cdpe_{tk}")
        # 乗降制約（降車専用＝乗車不可）。抽出でマーカーを検出したブロックのみ提示。
        # 範囲は自動で決めず、検出した停留所を初期選択にして人が確定する（正しく失敗）。
        board_sel = {}
        _bhints = []
        for _b in ss().get("extract", {}).get("blocks", []):
            _names = [s["name"] for s in _b.get("stops", [])]
            _hint = [s["name"] for s in _b.get("stops", []) if s.get("boarding_hint") == "drop_off_only"]
            if _hint:
                _bhints.append((_b["block_index"], _b.get("direction_hint"), _names, _hint))
        if _bhints:
            st.markdown("**降車専用（乗車不可）にする停留所**　"
                        "※原典に「降車専用区間」等の記載あり。**範囲は自動判定していません**——"
                        "どの停留所が乗車不可かを確認して選んでください。")
            for bi, dirh, names, hint in _bhints:
                sel = st.multiselect(f"block{bi}（{dirh or '方向なし'}）の降車専用停留所",
                                     options=names, default=hint, key=f"board_{bi}_{tk}")
                if sel:
                    board_sel[bi] = sel
        use_nom = st.checkbox("Nominatim 補完を使う（POI多い路線向け・遅い）", value=False)
        submitted = st.form_submit_button("GTFS-JP を生成する", type="primary")

    if submitted:
        # 空抽出ガード: 停留所が1つも取れていないなら、空のfeedを黙って作らず停止する。
        # 画像化PDF（テキストレイヤなし）が主因。原典・抽出方法の見直しを促す（＝正しく失敗）。
        _blocks = ss().extract.get("blocks", [])
        _nstops = sum(len(b.get("stops", [])) for b in _blocks)
        _img_pdf = any((n.get("type") == "image_pdf_use_ocr")
                       for n in ss().extract.get("needs_confirmation", []))
        if _nstops == 0:
            if _img_pdf:
                st.error("この時刻表は画像化PDF（文字情報なし）のため、停留所を1つも抽出できませんでした。"
                         "テキストが選択できるPDF版・Excel版を使うか、OCR(MinerU)で文字起こししてから"
                         "取り込んでください。空のGTFSは生成しません。")
            else:
                st.error("停留所が抽出できませんでした（0件）。時刻表の形式が想定外の可能性があります。"
                         "別の時刻表で試すか、抽出結果（①の表示）をご確認ください。空のGTFSは生成しません。")
            st.stop()
        # 必須チェック: 官公庁提出物が黙って Validator ERROR にならないよう、
        # 全路線に名前があるか確認（GTFS仕様: route_short_name か route_long_name のどちらか必須）。
        _rts = ss()["decision_spec"]["routes"]
        _eff = [(route_name if (len(_rts) == 1 and route_name) else (r.get("route_long_name") or "")).strip()
                for r in _rts]
        if any(not e for e in _eff):
            if len(_rts) == 1:
                st.error("路線名が空です。GTFS仕様では route_short_name / route_long_name の"
                         "いずれかが必須で、空のまま生成すると Validator ERROR "
                         "（route_both_short_and_long_name_missing）になります。③で路線名を入力してください。")
            else:
                st.error("路線名が空の路線があります。②の割り当て表で各路線に名前を付けてください。"
                         "空のまま生成すると Validator ERROR になります。")
            st.stop()
        # 事業者名は暫定運用を許容（＝止めない）が、空なら明示警告。
        if not ag_name.strip():
            st.warning("事業者名が空です。agency は暫定値（agency_id=AGENCY_TBD／『未定（自治体が記入）』）"
                       "で出力されます。正式提出前に事業者名・法人番号を記入してください。")
        # 法人番号は13桁の数字。桁が違えば注意（止めはしない）。
        if ag_id.strip() and not re.fullmatch(r"\d{13}", ag_id.strip()):
            st.warning("法人番号は**13桁の数字**です。桁数をご確認ください（不明なら空でOK）。")
        spec = dict(ss()["decision_spec"])
        if route_name:
            spec["routes"][0]["route_long_name"] = route_name
        # 循環フラグ（意図の記録）と、行き先表示の上書き（方向見出しが無いブロックに適用）
        for r in spec.get("routes", []):
            r["circular"] = bool(is_circular)
        if headsign.strip():
            bh = dict(spec.get("block_headsign", {}))
            for b in ss().extract.get("blocks", []):
                if not b.get("direction_hint"):
                    bh[str(b.get("block_index"))] = headsign.strip()
            spec["block_headsign"] = bh
        period = {"start_date": start or "20250401", "end_date": end or "20271231"}
        form_days = {"mon": int(days[0]), "tue": int(days[1]), "wed": int(days[2]),
                     "thu": int(days[3]), "fri": int(days[4]), "sat": int(days[5]), "sun": int(days[6])}
        spec["service"] = {"service_id": "SVC", **form_days, **period}
        # 複数ダイヤ: ②の運行日割当(block_pattern)から services と block_service を構築。
        bpat = ss()["decision_spec"].get("block_pattern", {})
        services = {"SVC": {"service_id": "SVC", **form_days, **period}}
        block_service = {}
        for bi, pat in bpat.items():
            if pat in DAY_PATTERNS:
                sidp = PATTERN_SID[pat]
                services[sidp] = {"service_id": sidp, **DAY_PATTERNS[pat], **period}
                block_service[bi] = sidp
            else:
                block_service[bi] = "SVC"
        spec["services"] = list(services.values())
        spec["block_service"] = block_service
        # 個別の運行日・運休日 → calendar_dates。運休(2)は全service、臨時運行(1)は基本SVCに付与。
        _svc_ids = [s["service_id"] for s in spec["services"]] or ["SVC"]
        _cal_dates, _seen_cd = [], set()

        def _add_cd(ymd, etype, sids):
            for sid in sids:
                k = (sid, ymd, etype)
                if k not in _seen_cd:
                    _seen_cd.add(k)
                    _cal_dates.append({"service_id": sid, "date": ymd, "exception_type": etype})
        try:
            for _, _r in cd_editor.iterrows():
                _dv, _kind = _r.get("日付"), _r.get("種別")
                if pd.isna(_dv) or not _kind:
                    continue
                _ymd = pd.Timestamp(_dv).strftime("%Y%m%d")
                if _kind == "運休":
                    _add_cd(_ymd, 2, _svc_ids)
                elif _kind == "臨時運行":
                    _add_cd(_ymd, 1, _svc_ids[:1])
        except Exception:
            pass
        if cd_use_period and cd_ps.strip().isdigit() and cd_pe.strip().isdigit():
            try:
                _d0 = datetime.datetime.strptime(cd_ps.strip(), "%Y%m%d").date()
                _d1 = datetime.datetime.strptime(cd_pe.strip(), "%Y%m%d").date()
                _cur = _d0
                while _cur <= _d1:
                    _add_cd(_cur.strftime("%Y%m%d"), 2, _svc_ids)
                    _cur += datetime.timedelta(days=1)
            except Exception:
                pass
        if _cal_dates:
            spec["calendar_dates"] = _cal_dates
        fare_matrix = []
        if zone_fare and zone_df is not None:
            for i, orig in enumerate(_stops_all):
                for dest in _stops_all:
                    if dest == orig:
                        continue
                    v = zone_df.iloc[i][dest]
                    sv = str(v).strip()
                    if sv in ("", "nan", "None", "<NA>"):
                        continue
                    try:
                        pr = int(float(sv))
                    except ValueError:
                        continue
                    if pr > 0:
                        fare_matrix.append({"from": orig, "to": dest, "price": pr})
            if zone_symmetric:   # 対称補完: 片方向だけ入っていれば逆向きも同額で足す
                have = {(m["from"], m["to"]): m["price"] for m in fare_matrix}
                for (a, b), pr in list(have.items()):
                    if (b, a) not in have:
                        fare_matrix.append({"from": b, "to": a, "price": pr})
        # 路線別運賃（多路線で各路線に1つでも運賃が入っていれば採用）
        route_fares = {}
        for rid, (ra, rch, rdi) in rfares_in.items():
            lst = [{"category": c, "price": int(p)} for c, p in
                   (("大人", ra), ("小児", rch), ("障がい者", rdi)) if p > 0]
            if lst:
                route_fares[rid] = lst
        # 優先順位: 区間運賃 > 路線別運賃 > 全路線一律(区分別)
        if fare_matrix:
            spec["fare_matrix"] = fare_matrix
        elif route_fares:
            spec["route_fares"] = route_fares
        else:
            fares = [{"category": c, "price": int(p)} for c, p in
                     (("大人", fare_adult), ("小児", fare_child), ("障がい者", fare_disabled)) if p > 0]
            if fares:
                spec["fares"] = fares
        # 乗降制約（降車専用＝乗車不可）。ブロック単位で限定（往路は影響しない）。
        boarding = [{"type": "drop_off_only", "block": bi, "stops": sel}
                    for bi, sel in board_sel.items()]
        if boarding:
            spec["boarding"] = boarding
        aid = ag_id.strip() or "AGENCY_TBD"
        _zip = re.sub(r"[^0-9]", "", ag_zip)   # 郵便番号は数字（7桁）に正規化
        spec["agency"] = {"agency_id": aid, "agency_name": ag_name or "未定（自治体が記入）",
                          "agency_url": ag_url or None, "agency_phone": ag_phone or None}
        spec["agency_jp"] = {"agency_official_name": (ag_official.strip() or ag_name) or None,
                             "agency_zip_number": _zip or None,
                             "agency_address": ag_addr.strip() or None,
                             "agency_president_pos": ag_pres_pos.strip() or None,
                             "agency_president_name": ag_pres_name.strip() or None}
        hol = {"syuku": hol_syuku, "nenmatsu": hol_nenmatsu, "obon": hol_obon}
        # 路線名以外がほぼ未入力なら、暫定の既定値（捏造なし）で生成してよいか確認してから生成。
        minimal = (not ag_name.strip() and not ag_id.strip() and not ag_url.strip()
                   and not ag_phone.strip() and not ag_official.strip() and not ag_zip.strip()
                   and not ag_addr.strip() and not ag_pres_pos.strip() and not ag_pres_name.strip()
                   and fare_adult == 0 and fare_child == 0
                   and fare_disabled == 0 and not route_fares
                   and not (zone_fare and fare_matrix)
                   and not start.strip() and not end.strip()
                   and not (hol_syuku or hol_nenmatsu or hol_obon) and not _cal_dates)
        if minimal:
            ss().pending_gen = {"spec": spec, "muni": muni, "use_nom": bool(use_nom), "hol": hol}
            ss().awaiting_confirm = True
            st.rerun()
        else:
            run_generation(spec, muni, bool(use_nom), hol)

    # 路線名のみ入力 → 暫定既定値での生成確認（捏造せず、要確認として入れる）
    if ss().get("awaiting_confirm"):
        pg = ss().get("pending_gen", {})
        sp = pg.get("spec", {})
        names = " / ".join(r.get("route_long_name", "") for r in sp.get("routes", []))
        sv = sp.get("service", {})
        st.warning("路線名以外がほぼ未入力です。下の**暫定の既定値**で生成します"
                   "（事実は捏造しません。各項目は『要確認』として入ります）。よろしいですか？")
        st.markdown(
            f"- 路線名: **{names}**\n"
            f"- 運行曜日: 平日（月〜金）　／　有効期間: {sv.get('start_date')}〜{sv.get('end_date')}\n"
            f"- 運賃: 未設定（0）　／　事業者: 未定（自治体が記入）・法人番号 空\n"
            f"- 対象自治体: {pg.get('muni')}（座標補完に使用）\n"
            "- 公式GTFS(BODIK等)があれば、対象自治体名で照合して座標を再利用できます（URLが分かれば②で指定）。")
        cc1, cc2 = st.columns(2)
        if cc1.button("この暫定内容で生成する", type="primary"):
            ss().pop("awaiting_confirm", None)
            run_generation(sp, pg.get("muni", "福岡県"), pg.get("use_nom", False), pg.get("hol", {}))
            st.rerun()
        if cc2.button("入力に戻る"):
            ss().pop("awaiting_confirm", None); ss().pop("pending_gen", None); st.rerun()

def render_submission_checklist(out):
    """提出前チェックリスト。生成物を根拠に『そのまま提出してよいか』を判定して見せる。
    正確さの総仕上げ＝官公庁が『なぜ提出可か』を確認できるようにする。"""
    import csv as _csv

    def _rows(p):
        return list(_csv.DictReader(p.open(encoding="utf-8-sig"))) if p.exists() else []
    items = []  # (level, ok, label, detail)  level: block(必須) / must(重要) / info(参考)
    # 1) 公式Validator ERROR=0（未実行なら「OK」と誤表示しない）
    rep = out / "validation" / "report.json"
    if not rep.exists():
        items.append(("block", False, "公式Validatorのエラーが0",
                      "検証が未実行です（Java/Validator未設定の可能性）→ 検証を実行してください"))
    else:
        n_err = 0
        try:
            n_err = sum(n.get("totalNotices", 0)
                        for n in json.loads(rep.read_text(encoding="utf-8")).get("notices", [])
                        if n.get("severity") == "ERROR")
        except Exception:
            pass
        items.append(("block", n_err == 0, "公式Validatorのエラーが0",
                      "問題なし" if n_err == 0 else f"ERROR {n_err}件 → ④下部の内容と対処を確認"))
    # 1b) GTFS-JP 拡張検証 ERROR=0（標準Validatorが見ない agency_jp/office_jp 等）
    jr = out / "jp_ext_report.json"
    if not jr.exists():
        items.append(("info", True, "GTFS-JP拡張検証", "レポート未生成（スキップ）"))
    else:
        try:
            jd = json.loads(jr.read_text(encoding="utf-8"))
        except Exception:
            jd = {}
        je = int(jd.get("error_count", 0) or 0)
        jw = int(jd.get("warning_count", 0) or 0)
        items.append(("block", je == 0, "GTFS-JP拡張検証のエラーが0",
                      ("問題なし" + (f"（警告{jw}件）" if jw else "")) if je == 0
                      else f"拡張ERROR {je}件 → " + " ／ ".join(jd.get("errors", [])[:2])))
    # 2) 全停留所の座標が確定
    confirmed = ss().get("confirmed", {}) or {}
    n_ok = n_rev = n_non = 0
    for rr in _rows(out / "座標_信頼度.csv"):
        c = "確定" if rr.get("stop_id") in confirmed else rr.get("confidence", "")
        if c == "確定":
            n_ok += 1
        elif c == "要確認":
            n_rev += 1
        else:
            n_non += 1
    tot = n_ok + n_rev + n_non
    items.append(("block", tot > 0 and n_rev == 0 and n_non == 0, "全停留所の座標が確定",
                  f"確定{n_ok} / 要確認{n_rev} / 未補完{n_non}"
                  + ("" if (n_rev == 0 and n_non == 0) else " → ⑤の地図で確定してください")))
    # 3) 全路線に路線名
    n_no = nr = 0
    for rr in _rows(out / "gtfs" / "routes.txt"):
        nr += 1
        if not (str(rr.get("route_short_name", "")).strip() or str(rr.get("route_long_name", "")).strip()):
            n_no += 1
    items.append(("block", nr > 0 and n_no == 0, "全路線に路線名がある",
                  f"{nr}路線すべてOK" if n_no == 0 else f"名前なし {n_no}路線 → ③で入力"))
    # 4) 事業者情報（GTFS-JP正式提出に必要）
    ag = _rows(out / "gtfs" / "agency.txt")
    ajp = _rows(out / "gtfs" / "agency_jp.txt")
    aid = (ag[0].get("agency_id", "") if ag else "")
    a0 = ajp[0] if ajp else {}
    miss = []
    if not aid or aid == "AGENCY_TBD":
        miss.append("法人番号")
    for key, nm in (("agency_official_name", "正式名称"), ("agency_zip_number", "郵便番号"),
                    ("agency_address", "住所")):
        if not str(a0.get(key, "") or "").strip():
            miss.append(nm)
    items.append(("must", not miss, "事業者情報（法人番号・正式名称・郵便番号・住所）",
                  "入力済み" if not miss else "未入力: " + "・".join(miss) + " → ③で入力"))
    # 4b) 内部整合（抽出時刻↔stop_times）— 座標方式/Excel経路のときだけ生成される
    svp = out / "stoptimes_verify.json"
    if svp.exists():
        try:
            svs = json.loads(svp.read_text(encoding="utf-8")).get("summary", {})
        except Exception:
            svs = {}
        mm = int(svs.get("time_mismatch", 0) or 0)
        oe = len(svs.get("only_in_extract", []) or [])
        oo = len(svs.get("only_in_stop_times", []) or [])
        pct = svs.get("time_match_pct", "-")
        okv = (mm == 0 and oe == 0 and oo == 0)
        items.append(("must", okv, "内部整合（抽出した時刻がstop_timesに保たれている）",
                      f"一致率 {pct}％・時刻不一致{mm}・便の欠落{oe}・余分{oo}"
                      + ("" if okv else " → 原典と生成を確認（生成漏れ・時刻改変の疑い）")))
    # 4c) 区間速度チェック（座標/時刻の誤りを炙り出す。「確定」でも別地点座標を捕捉）
    spd = _rows(out / "速度_check.csv")
    if spd:
        n_fast = sum(1 for r in spd if r.get("判定") == "速すぎ")
        n_zero = sum(1 for r in spd if r.get("判定") == "時間0")
        n_slow = sum(1 for r in spd if r.get("判定") == "遅すぎ")
        bad = n_fast + n_zero
        items.append(("must", bad == 0, "区間速度が現実的（座標・時刻に飛びがない）",
                      f"速すぎ{n_fast}・時間0 {n_zero}・遅すぎ{n_slow}"
                      + ("" if bad == 0 else " → 該当区間の座標/時刻を確認（別地点への誤マッチや時刻誤りの疑い）")))
    # 4d) 経路(shape)が停留所を通っているか（参考）
    shp = _rows(out / "shape_coverage.csv")
    if shp:
        n_far = sum(1 for r in shp if "離れすぎ" in (r.get("判定") or ""))
        n_rev = sum(1 for r in shp if "順序逆転" in (r.get("判定") or ""))
        items.append(("info", n_far == 0 and n_rev == 0, "経路(shape)が停留所を通っている",
                      f"離れすぎ{n_far}・順序逆転{n_rev}"
                      + ("" if (n_far == 0 and n_rev == 0) else " — 経路生成/座標を確認（参考）")))
    # 5) 時刻の原典照合（参考）
    n_anom = 0
    ap = out / "時刻アノマリ.json"
    if ap.exists():
        try:
            n_anom = len(json.loads(ap.read_text(encoding="utf-8")))
        except Exception:
            pass
    items.append(("info", n_anom == 0, "時刻の原典照合",
                  "疑いなし" if n_anom == 0 else f"OCR誤読の疑い {n_anom}件 — 原典と見比べて確認を"))
    # 6) 有効期間（参考）
    fi = _rows(out / "gtfs" / "feed_info.txt")
    end = (fi[0].get("feed_end_date", "") if fi else "")
    today = datetime.date.today().strftime("%Y%m%d")
    ok_p = bool(end) and end >= today
    items.append(("info", ok_p, "有効期間が現在以降",
                  (f"終了 {end}" if end else "未設定") + ("" if ok_p else " → ③で有効期間を確認")))

    block_ng = [i for i in items if i[0] == "block" and not i[1]]
    must_ng = [i for i in items if i[0] == "must" and not i[1]]
    st.subheader("✅ 提出前チェック")
    if not block_ng and not must_ng:
        st.success("提出できる状態です（必須項目クリア）。")
    elif not block_ng:
        st.warning("提出は可能ですが、事業者情報などに未入力があります（下記）。")
    else:
        st.error("まだ提出しない方がよいです。下記の必須項目を対応してください。")
    for level, ok, label, detail in items:
        icon = "✅" if ok else ("⛔" if level == "block" else ("⚠" if level == "must" else "🔸"))
        st.markdown(f"{icon} **{label}** — {detail}")


# =====================================================================
# Step 4: 結果（検証・地図・ダウンロード）
# =====================================================================
if ss().get("result"):
    st.header("④ 結果")
    out = WORK / "out"
    render_submission_checklist(out)
    # 内部整合
    sv = out / "stoptimes_verify.json"
    if sv.exists():
        s = json.loads(sv.read_text(encoding="utf-8")).get("summary", {})
        st.metric("内部整合（抽出時刻↔stop_times）", f"{s.get('time_match','-')}/{s.get('rows_compared','-')}",
                  s.get("verdict", ""))
    # Validator（ERROR は件数だけでなく内容と対処を出す＝官公庁が原因を追える）
    ERR_HELP = {
        "route_both_short_and_long_name_missing":
            ("路線名（route_short_name/long_name）が両方空", "③で路線名を入力してください。"),
        "stop_time_with_arrival_before_previous_departure_time":
            ("便の中で時刻が前の停留所より早い（時刻の逆行）",
             "原典の時刻を確認してください。要予約の寄り道・折り返し・誤記が原因のことが多い。"
             "上の『要確認』で該当便が指摘されています。"),
        "stop_time_with_only_arrival_or_departure_time":
            ("到着/出発の一方しか時刻が無い", "原典で時刻を補ってください。"),
        "decreasing_or_equal_stop_time_distance":
            ("shapeの距離が単調増加でない", "経路(shapes)の生成を確認してください。"),
    }
    rep = out / "validation" / "report.json"
    if rep.exists():
        try:
            notices = json.loads(rep.read_text(encoding="utf-8")).get("notices", [])
            err_notices = [n for n in notices if n.get("severity") == "ERROR"]
            errs = sum(n.get("totalNotices", 0) for n in err_notices)
            st.metric("MobilityData Validator ERROR", errs)
            if errs:
                lines = []
                for n in err_notices:
                    code = n.get("code", "")
                    meaning, how = ERR_HELP.get(code, ("", "原典・生成結果を確認してください。"))
                    head = f"**{code}** × {n.get('totalNotices')}"
                    if meaning:
                        head += f" — {meaning}"
                    lines.append(head + f"\n　→ 対処: {how}")
                    sample = (n.get("sampleNotices") or [])[:1]
                    if sample:
                        ids = {k: v for k, v in sample[0].items()
                               if k in ("tripId", "stopId", "stopSequence", "csvRowNumber", "routeId")}
                        if ids:
                            lines.append("　該当例: " + ", ".join(f"{k}={v}" for k, v in ids.items()))
                st.error("Validator ERROR の内容（このままでは公式提出に不適）:\n\n" + "\n\n".join(lines))
            else:
                st.success("Validator ERROR は 0 件です。")
        except Exception:
            pass
    # 座標カバレッジ
    stops = out / "gtfs" / "stops.txt"
    if stops.exists():
        import csv
        rows = list(csv.DictReader(open(stops, encoding="utf-8-sig")))
        have = sum(1 for r in rows if (r.get("stop_lat") or "").strip())
        st.write(f"座標カバレッジ: {have}/{len(rows)}")
    # 地図
    mv = out / "map_view.html"
    if mv.exists():
        st.subheader("地図プレビュー（停留所・経路）")
        components.html(mv.read_text(encoding="utf-8"), height=520, scrolling=True)
    # 要確認CSV
    rc_csv = out / "座標_要確認.csv"
    if rc_csv.exists():
        st.warning("同名複数候補の要確認リストがあります（地図で確認を）。")
        st.download_button("座標_要確認.csv をDL", rc_csv.read_bytes(), "座標_要確認.csv")
    # ふりがな・英語の確認・修正（GTFS-JP必須。pykakasiの難読地名の誤読を人が直す）
    _trans = out / "gtfs" / "translations.txt"
    if _trans.exists():
        import csv as _csvr
        _trows = list(_csvr.DictReader(_trans.open(encoding="utf-8-sig")))
        _cur, _order = {}, []
        for _r in _trows:
            if (_r.get("table_name") or "").strip() != "stops":
                continue
            _nm = (_r.get("field_value") or "").strip()
            _lang = (_r.get("language") or "").strip()
            if _nm and _nm not in _cur:
                _cur[_nm] = {}; _order.append(_nm)
            if _lang in ("ja-Hrkt", "en"):
                _cur[_nm][_lang] = _r.get("translation", "")
        _has_en = any("en" in v for v in _cur.values())
        with st.expander("🈁 ふりがな・英語の確認・修正（難読地名の誤読をここで直す）"):
            st.caption("pykakasi が自動生成した読みです。難読地名は誤読することがあります"
                       "（例: 壱町原「いちまちはら」→ 正しくは「いっちょうばる」）。"
                       "直したい行だけ書き換えて反映してください（GTFS-JP の必須項目）。")
            _rows = [{"停留所名": _nm, "ふりがな(ja-Hrkt)": _cur[_nm].get("ja-Hrkt", ""),
                      **({"英語(en)": _cur[_nm].get("en", "")} if _has_en else {})} for _nm in _order]
            with st.form("readings_form"):
                _cfg = {"停留所名": st.column_config.TextColumn("停留所名", disabled=True)}
                _edited = st.data_editor(pd.DataFrame(_rows), hide_index=True,
                                         key="readings_editor", column_config=_cfg,
                                         use_container_width=True)
                if st.form_submit_button("この読みで反映（zipを更新）"):
                    _by = {}
                    for _i, _nm in enumerate(_order):
                        _spec = {}
                        _nh = str(_edited.iloc[_i]["ふりがな(ja-Hrkt)"]).strip()
                        if _nh and _nh != _cur[_nm].get("ja-Hrkt", ""):
                            _spec["ja-Hrkt"] = _nh
                        if _has_en:
                            _ne = str(_edited.iloc[_i].get("英語(en)", "")).strip()
                            if _ne and _ne != _cur[_nm].get("en", ""):
                                _spec["en"] = _ne
                        if _spec:
                            _by[_nm] = _spec
                    if not _by:
                        st.info("変更がありませんでした。")
                    else:
                        _mr = WORK / "manual_readings.json"
                        _mr.write_text(json.dumps({"by_stop_name": _by}, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
                        run([SCRIPTS / "apply_manual_readings.py", _trans, "--readings", _mr])
                        _zz = list(out.glob("*_gtfs-jp.zip"))
                        if _zz:
                            run([SCRIPTS / "package_gtfs_zip.py", out / "gtfs", "-o", _zz[0]])
                        st.success(f"{len(_by)}件の読みを反映し、GTFS-JP(zip)を更新しました。"
                                   "下のボタンで再ダウンロードしてください。")
                        st.rerun()
    # zip ダウンロード
    zips = list(out.glob("*_gtfs-jp.zip"))
    if zips:
        st.download_button("⬇ GTFS-JP (zip) をダウンロード", zips[0].read_bytes(), zips[0].name,
                           type="primary")
    with st.expander("実行ログ"):
        st.code(ss()["result"]["log"][-3000:])

# =====================================================================
# Step 5: 座標の確認（地図で要確認を確定）— 推測座標を人が確認するまで正式採用しない
# =====================================================================
if ss().get("result"):
    conf_csv = WORK / "out" / "座標_信頼度.csv"
    if conf_csv.exists():
        st.header("⑤ 座標の確認（地図）")
        st.caption("確定=緑／要確認=橙／未補完=赤。要確認・未補完は地図クリックか座標入力で確定する。"
                   "**全部が確定になるまで「公式提出可」にしない**（＝推測座標を黙って出さない）。"
                   "**行き・帰りは別々の停留所**として表示されます（多くは反対車線＝別座標。"
                   "終点・敷地内で同じ場所なら『同じ場所にする』で揃えられます）。")
        import csv as _csv
        crows = list(_csv.DictReader(conf_csv.open(encoding="utf-8-sig")))
        # ★行き/帰りを反対側へ自動推定配置した停留所は、必ず確認してもらう（推定なので）
        _n_est = sum(1 for r in crows if "反対側へ自動配置" in (r.get("reason") or ""))
        if _n_est:
            st.error(f"⚠ 行き・帰りを自動で **反対側（反対車線）に推定配置** した停留所が **{_n_est} 件** あります。"
                     "これは経路からの**推定**であり、正しい位置とは限りません。"
                     "**必ず地図で1件ずつ正しい位置を確認して『確定』にしてください**"
                     "（確認が終わるまで公式提出はできません。同じ場所なら『同じ場所にする』で戻せます）。")
        # stop_desc(方面) を stops.txt から補う（行き/帰りの区別表示に使う）
        _descmap = {}
        _stpath = WORK / "out" / "gtfs" / "stops.txt"
        if _stpath.exists():
            for _s in _csv.DictReader(_stpath.open(encoding="utf-8-sig")):
                _descmap[_s.get("stop_id", "")] = (_s.get("stop_desc") or "").strip()
        confirmed = ss().setdefault("confirmed", {})  # stop_id -> (lat,lon)

        def _sid(r):
            return r.get("stop_id", "")

        def _label(r):
            d = _descmap.get(_sid(r), "")
            return f"{r['stop_name']}（{d}）" if d else r["stop_name"]

        def eff_conf(r):
            return "確定" if _sid(r) in confirmed else r["confidence"]

        n_ok = sum(1 for r in crows if eff_conf(r) == "確定")
        n_rev = sum(1 for r in crows if eff_conf(r) == "要確認")
        n_non = sum(1 for r in crows if eff_conf(r) == "未補完")
        m1, m2, m3 = st.columns(3)
        m1.metric("確定", n_ok); m2.metric("要確認", n_rev); m3.metric("未補完", n_non)

        # 地図。tooltipは一意化（同名の行き/帰りを方面で区別、万一重複ならID付与）。
        pts, tip2id = [], {}
        for r in crows:
            sid = _sid(r)
            if sid in confirmed:
                la, lo, conf = confirmed[sid][0], confirmed[sid][1], "確定"
            elif (r.get("stop_lat") or "").strip():
                la, lo, conf = float(r["stop_lat"]), float(r["stop_lon"]), r["confidence"]
            else:
                continue
            tip = _label(r)
            if tip in tip2id:
                tip = f"{tip}[{sid}]"
            tip2id[tip] = sid
            pts.append((tip, la, lo, conf, r.get("reason", "")))
        center = ([sum(p[1] for p in pts) / len(pts), sum(p[2] for p in pts) / len(pts)]
                  if pts else [35.0, 138.0])
        fmap = folium.Map(location=center, zoom_start=14)
        col = {"確定": "green", "要確認": "orange", "未補完": "red"}
        for tip, la, lo, conf, reason in pts:
            folium.Marker([la, lo], tooltip=tip, draggable=True,
                          icon=folium.Icon(color=col.get(conf, "gray")),
                          popup=f"{tip}（{conf}）{reason}").add_to(fmap)
        st.caption("📍 ピンを**ドラッグ**して正しい位置へ動かし、そのピンを**クリック**すると、"
                   "下に『この位置で確定』ボタンが出ます（地図の空き場所クリックで座標を拾うこともできます）。")
        state = st_folium(fmap, width=900, height=460, key="confmap",
                          returned_objects=["last_clicked", "last_object_clicked",
                                            "last_object_clicked_tooltip"])
        clicked = state.get("last_clicked") if state else None
        obj = state.get("last_object_clicked") if state else None
        obj_tip = state.get("last_object_clicked_tooltip") if state else None
        # ドラッグ→ピンをクリック で、その移動後の位置を確定できる
        if obj and obj_tip and obj_tip in tip2id:
            sid = tip2id[obj_tip]
            la2, lo2 = round(obj["lat"], 6), round(obj["lng"], 6)
            already = confirmed.get(sid)
            moved = (not already) or abs(already[0] - la2) > 1e-6 or abs(already[1] - lo2) > 1e-6
            st.success(f"選択中のピン『{obj_tip}』: {la2:.6f}, {lo2:.6f}")
            if st.button(f"『{obj_tip}』をこのピン位置で確定する", disabled=not moved):
                confirmed[sid] = (la2, lo2); st.rerun()
        if clicked:
            st.info(f"地図クリック位置: {clicked['lat']:.6f}, {clicked['lng']:.6f}"
                    "（下で停留所を選び『地図クリック位置を使う』）")

        todo = [r for r in crows if eff_conf(r) != "確定"]
        if todo:
            st.subheader(f"要確認・未補完を確定する（残り {len(todo)} 件）")
            _todo_ids = [_sid(r) for r in todo]
            sel = st.selectbox("停留所", _todo_ids,
                               format_func=lambda s: next((_label(r) for r in crows if _sid(r) == s), s))
            cur = next((r for r in crows if _sid(r) == sel), {})
            st.write(f"現在の座標: {cur.get('stop_lat','')}, {cur.get('stop_lon','')} ／ "
                     f"理由: {cur.get('reason','')}")
            # 同じ場所（終点・敷地内）: 同名で反対方向の停留所と同座標にする
            _sibs = [r for r in crows if r["stop_name"] == cur.get("stop_name") and _sid(r) != sel]
            for _sb in _sibs[:1]:
                _sbid = _sid(_sb)
                _sbc = confirmed.get(_sbid) or ((float(_sb["stop_lat"]), float(_sb["stop_lon"]))
                                                if (_sb.get("stop_lat") or "").strip() else None)
                if _sbc and st.button(f"『{_label(_sb)}』と同じ場所にする（敷地内・終点向け）"):
                    confirmed[sel] = (round(_sbc[0], 6), round(_sbc[1], 6)); st.rerun()
            a1, a2, a3 = st.columns([1, 1, 1])
            if a1.button("地図クリック位置を使う", disabled=not clicked):
                confirmed[sel] = (round(clicked["lat"], 6), round(clicked["lng"], 6)); st.rerun()
            lat_in = a2.number_input("緯度", value=float(cur.get("stop_lat") or center[0]), format="%.6f")
            lon_in = a3.number_input("経度", value=float(cur.get("stop_lon") or center[1]), format="%.6f")
            if st.button("この停留所を確定にする"):
                confirmed[sel] = (round(lat_in, 6), round(lon_in, 6)); st.rerun()
        else:
            st.success("✅ すべての座標が確定しました。**公式提出可** です。")

        if confirmed:
            st.write(f"確認済み（手動確定）: {len(confirmed)} 件")
            if st.button("確定座標で再生成する", type="primary"):
                mc = {"by_stop_id": {sid: {"lat": la, "lon": lo}
                                     for sid, (la, lo) in confirmed.items()}}
                (WORK / "manual_coords.json").write_text(json.dumps(mc, ensure_ascii=False), encoding="utf-8")
                cfg = json.loads((WORK / "config.json").read_text(encoding="utf-8"))
                cfg["manual_coords"] = str(WORK / "manual_coords.json")
                (WORK / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
                with st.spinner("確定座標で再生成中..."):
                    rc, so, se = run([SCRIPTS / "run_pipeline.py", "--config", WORK / "config.json"], cwd=REPO)
                ss().result = {"rc": rc, "log": se}
                st.success("再生成しました（確定座標を反映）。"); st.rerun()

# =====================================================================
# Step 6: GTFSビューア（作成した feed を 7タブで閲覧）
# =====================================================================
if ss().get("result"):
    viewer = WORK / "out" / "gtfs_viewer.html"
    if viewer.exists():
        st.header("⑥ GTFSビューア（路線一覧・時刻表・運賃・路線図・運行カレンダー・バス停・点検）")
        st.caption("作成した GTFS をブラウザで閲覧（📋路線一覧 / 🕐時刻表 / 💴運賃表 / 🗺️路線図 / "
                   "📅運行カレンダー / 🚏バス停一覧 / ✓データチェック結果）。"
                   "単一HTMLなのでDLしてそのままブラウザで開けます（サーバ不要）。")
        html = viewer.read_text(encoding="utf-8")
        components.html(html, height=820, scrolling=True)
        st.download_button("⬇ GTFSビューア(HTML)をダウンロード", html.encode("utf-8"),
                           "gtfs_viewer.html", mime="text/html")

# 画面描画の最後に、現在の作業状態を自動保存（節目ごと＝実質ほぼ毎回の確定状態）。
autosave()
