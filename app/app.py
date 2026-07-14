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
from contextlib import nullcontext
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
# ふりがな(ja-Hrkt)の再計算に生成側と同じロジックを使う（NFKC正規化＋難読地名辞書）。
try:
    from generate_translations import (init_kakasi as _init_kks, to_hiragana as _to_hira,
                                        load_reading_dict as _load_rdict)
    _KKS = _init_kks()
    _RDICT = _load_rdict(SCRIPTS.parent / "references" / "data" / "stop_readings.csv")
except Exception:
    _KKS, _RDICT, _to_hira = None, {}, None


def _auto_reading(name):
    """停留所名からふりがな(ja-Hrkt)を自動生成。辞書優先→pykakasi(NFKC)。"""
    name = (name or "").strip()
    if name in _RDICT and _RDICT[name].get("ja-Hrkt"):
        return _RDICT[name]["ja-Hrkt"]
    return _to_hira(name, _KKS) if _to_hira else ""


def _reading_suspicious(reading):
    """自動読みが怪しい（漢字が残る・半角カナ・濁点の文字化け）なら True。要確認の目印。"""
    r = reading or ""
    return bool(re.search(r"[一-鿿｡-ﾟ゚゜]", r)) or not r.strip()


def _rewrite_csv_field(path, field, rename_map, only_table=None):
    """CSV(path)の列 field の値を rename_map(old→new)で置換して書き戻す。
    only_table 指定時は table_name==only_table の行だけ対象。BOM/改行は踏襲。"""
    import csv as _c
    p = Path(path)
    if not p.exists() or not rename_map:
        return
    raw = p.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    with p.open(encoding="utf-8-sig", newline="") as f:
        rd = _c.DictReader(f)
        fns = rd.fieldnames or []
        rows = list(rd)
    if field not in fns:
        return
    for r in rows:
        if only_table and (r.get("table_name") or "").strip() != only_table:
            continue
        if (r.get(field) or "").strip() in rename_map:
            r[field] = rename_map[(r.get(field) or "").strip()]
    with p.open("w", encoding=("utf-8-sig" if has_bom else "utf-8"), newline="") as f:
        w = _c.DictWriter(f, fieldnames=fns, lineterminator=newline)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fns})


def _update_translations_rows(trans_path, updates):
    """translations.txt の (table,field,value) 行の言語別 translation を更新/追加/削除する。
    updates: [(table_name, field_name, field_value, {"ja-Hrkt":..,"en":..}), ...]
    翻訳が空文字ならその言語行を削除、値があれば無ければ新規追加する。"""
    import csv as _c
    p = Path(trans_path)
    if not p.exists() or not updates:
        return
    with p.open(encoding="utf-8-sig", newline="") as f:
        rd = _c.DictReader(f)
        fns = rd.fieldnames or ["table_name", "field_name", "language", "translation", "field_value"]
        rows = list(rd)
    idx = {}   # (table,field,value,lang) -> row index
    for i, r in enumerate(rows):
        k = ((r.get("table_name") or "").strip(), (r.get("field_name") or "").strip(),
             (r.get("field_value") or "").strip(), (r.get("language") or "").strip())
        idx[k] = i
    _drop = set()
    for tb, fd, val, chg in updates:
        for lang, tr in chg.items():
            k = (tb, fd, val, lang)
            if k in idx:
                if tr:
                    rows[idx[k]]["translation"] = tr
                else:
                    _drop.add(idx[k])   # 空＝その言語行を削除
            elif tr:
                nr = {c: "" for c in fns}
                nr["table_name"], nr["field_name"] = tb, fd
                nr["language"], nr["field_value"], nr["translation"] = lang, val, tr
                rows.append(nr); idx[k] = len(rows) - 1
    rows = [r for i, r in enumerate(rows) if i not in _drop]
    with p.open("w", encoding="utf-8", newline="") as f:
        w = _c.DictWriter(f, fieldnames=fns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in fns})


def _write_shape(shp_path, shape_id, coords):
    """shapes.txt の shape_id の点を coords[(lat,lon)] で置き換える（他shapeは保持）。距離も再計算。"""
    import csv as _c
    from math import radians, sin, cos, asin, sqrt
    p = Path(shp_path)

    def _hav(a, b):
        la1, lo1, la2, lo2 = map(radians, [a[0], a[1], b[0], b[1]])
        h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
        return 2 * 6371000 * asin(sqrt(h))

    # 連続する重複点を除去（区間差し替えの継ぎ目で同一座標が並ぶと距離が増えず、
    # shape_dist_traveled が単調増加にならない＝検証で警告になるため）。
    _cc = []
    for c in coords:
        if not _cc or abs(_cc[-1][0] - c[0]) > 1e-7 or abs(_cc[-1][1] - c[1]) > 1e-7:
            _cc.append(c)
    coords = _cc
    fns = ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence", "shape_dist_traveled"]
    rows = []
    if p.exists():
        rows = [r for r in _c.DictReader(p.open(encoding="utf-8-sig")) if r.get("shape_id") != shape_id]
    d = 0.0
    for i, (la, lo) in enumerate(coords):
        if i > 0:
            d += _hav(coords[i - 1], coords[i])
        rows.append({"shape_id": shape_id, "shape_pt_lat": f"{la:.6f}", "shape_pt_lon": f"{lo:.6f}",
                     "shape_pt_sequence": str(i), "shape_dist_traveled": f"{d:.2f}"})
    with p.open("w", encoding="utf-8", newline="") as f:
        w = _c.DictWriter(f, fieldnames=fns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fns})


def _assign_trip_shape(trp_path, rid, did, shape_id):
    """trips(.with_shapes).txt の route+direction の行に shape_id を割り当てる。"""
    import csv as _c
    p = Path(trp_path)
    rd = _c.DictReader(p.open(encoding="utf-8-sig"))
    fns = rd.fieldnames or []
    rows = list(rd)
    if "shape_id" not in fns:
        fns = fns + ["shape_id"]
    for r in rows:
        if r.get("route_id") == rid and (r.get("direction_id") or "0") == did:
            r["shape_id"] = shape_id
    with p.open("w", encoding="utf-8", newline="") as f:
        w = _c.DictWriter(f, fieldnames=fns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fns})


# 地図の描画ツールバー(Leaflet.draw)の英語ラベルを日本語化する MacroElement。
# ※ streamlit-folium は header の <script> を innerHTML で挿入する＝ブラウザが実行しない。
#    そのため header ではなく「実行される地図初期化スクリプト(script macro)」に載せる。
#    ツールバーは非同期生成＋操作で動的に増えるので、DOM置換＋MutationObserver(characterData含む)で追随。
from jinja2 import Template as _JTemplate


class _DrawJPLabels(folium.MacroElement):
    _name = "DrawJPLabels"

    def __init__(self):
        super().__init__()
        self._template = _JTemplate("""
        {% macro script(this, kwargs) %}
        {% raw %}
        (function(){
          var TITLE={'Draw a polyline':'線を描く（道に沿って点を打つ）','Edit layers':'線を編集（点をドラッグ）',
            'Delete layers':'線を削除','No layers to edit':'編集できる線がありません',
            'No layers to delete':'削除できる線がありません','Finish drawing':'この点で線を確定',
            'Delete last point drawn':'直前の点を削除','Cancel drawing':'描画をやめる','Save changes':'変更を保存',
            'Cancel editing, discards all changes':'編集をやめる（変更を破棄）','Clear all layers':'すべて消す'};
          var TEXT={'Finish':'完了','Delete last point':'最後の点を削除','Cancel':'キャンセル','Save':'保存','Clear All':'全消去'};
          var TIP=[['Click to start drawing line.','クリックで線を描き始めます。'],
            ['Click to continue drawing line.','クリックで点を追加します。'],
            ['Click last point to finish line.','最後の点をダブルクリックで確定します。'],
            ['Click and drag to edit features.','点をドラッグして形を直します。'],
            ['Click cancel to undo changes.','キャンセルで元に戻せます。'],
            ['Click on a feature to remove.','消したい線をクリックします。']];
          function relabel(){
            document.querySelectorAll('.leaflet-draw a[title]').forEach(function(a){if(TITLE[a.title])a.title=TITLE[a.title];});
            document.querySelectorAll('.leaflet-draw-actions a').forEach(function(a){
              var t=(a.textContent||'').trim(); if(TEXT[t])a.textContent=TEXT[t]; if(TITLE[a.title])a.title=TITLE[a.title];});
            document.querySelectorAll('.leaflet-draw-tooltip').forEach(function(el){
              TIP.forEach(function(p){if(el.innerHTML.indexOf(p[0])>=0)el.innerHTML=el.innerHTML.split(p[0]).join(p[1]);});});
          }
          function boot(){ if(!document.querySelector('.leaflet-draw')){setTimeout(boot,60);return;}
            relabel();
            new MutationObserver(relabel).observe(document.body,{childList:true,subtree:true,attributes:true,characterData:true});}
          setTimeout(boot,0);
        })();
        {% endraw %}
        {% endmacro %}
        """)


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
  /* 「✏️ クリックで編集」ヒントは常時表示せず、表にカーソルが乗ったときだけ出す（外れると消える）。
     ※data_editor は canvas 描画でセル単位のDOMが無いため、表単位のホバーで表示する。
     ③区間運賃表(zonedf)には付けない。 */
  [class*="st-key-tt_"], [class*="st-key-route_editor_"] { position:relative; padding-bottom:18px; }
  [class*="st-key-tt_"]::after, [class*="st-key-route_editor_"]::after {
    content:"✏️ クリックで編集";
    position:absolute; right:2px; bottom:1px; z-index:5; pointer-events:none;
    font-size:.72rem; line-height:1; color:#5B6B7C;
    opacity:0; transition:opacity .14s ease; }
  [class*="st-key-tt_"]:hover::after, [class*="st-key-route_editor_"]:hover::after { opacity:1; }
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
# 保存する作業一式（費用の高い手作業＝抽出・時刻修正・路線割当・確定座標・検出・原本・生成結果）。
SAVE_KEYS = ["extract", "extract_token", "decision_spec", "detected", "confirmed",
             "source_display", "sources_all", "fare_matrix_doc", "result"]


def _out_persist_dir(sid):
    return AUTOSAVE_DIR / f"out_{sid}"


_WORK_FILES = ["config.json", "structured.json", "extract.json", "spec.json"]   # 再生成に要る作業ファイル


def _persist_out():
    """生成物 out/ と再生成用の作業ファイルを sid ごとの安定フォルダに保存
    （再起動後も④⑤⑥・ダウンロード・再生成を復元するため）。"""
    import shutil
    src = WORK / "out"
    if not src.exists():
        return
    dst = _out_persist_dir(SID)
    try:
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst / "out")
        for _wf in _WORK_FILES:
            if (WORK / _wf).exists():
                shutil.copy2(WORK / _wf, dst / _wf)
    except Exception:
        pass


def _restore_out(sid):
    """保存した生成物・作業ファイルを現在の作業フォルダ(WORK)へ戻す。"""
    import shutil
    src = _out_persist_dir(sid)
    if not src.exists():
        return
    try:
        if (src / "out").exists():
            _dst = WORK / "out"
            if _dst.exists():
                shutil.rmtree(_dst, ignore_errors=True)
            shutil.copytree(src / "out", _dst)
        for _wf in _WORK_FILES:
            if (src / _wf).exists():
                shutil.copy2(src / _wf, WORK / _wf)
    except Exception:
        pass


def autosave():
    if not ss().get("extract"):
        return
    try:
        payload = {k: ss().get(k) for k in SAVE_KEYS if ss().get(k) is not None}
        # ③の入力欄（事業者/運賃/有効期間 等。キーは *_<extract_token>）も保存して
        # 再起動後に再入力せず続けられるようにする。単純値のみ（data_editor等の複雑値は除外）。
        tk = ss().get("extract_token", "")
        if tk:
            forms = {k: v for k, v in ss().items()
                     if isinstance(k, str) and k.endswith("_" + tk)
                     and isinstance(v, (str, int, float, bool))}
            if forms:
                payload["form_inputs"] = forms
        AUTOSAVE_DIR.mkdir(parents=True, exist_ok=True)
        AUTOSAVE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # 生成物(out/)は変更があった時だけ保存（毎回コピーは重いのでフラグで制御）。
        if ss().pop("_out_dirty", False):
            _persist_out()
    except Exception:
        pass


def _restore_label(data, f):
    """保存ファイルを利用者が識別できる見出しにする（事業者名/路線名・停留所数・保存時刻）。"""
    try:
        mt = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%m/%d %H:%M")
    except Exception:
        mt = ""
    ex = data.get("extract", {}) or {}
    blocks = ex.get("blocks", []) or []
    nstops = sum(len(b.get("stops", [])) for b in blocks)
    fi = data.get("form_inputs", {}) or {}
    agn = next((v for k, v in fi.items() if k.startswith("agn_") and str(v).strip()), "")
    routes = [r.get("route_long_name", "") for r in (data.get("decision_spec", {}) or {}).get("routes", [])]
    src = data.get("source_display", "")
    srcname = Path(src).name if src else ""
    title = agn or "／".join([r for r in routes if r][:2]) or srcname or "作業データ"
    return f"**{title}**　（{len(blocks)}まとまり／停留所{nstops}／保存 {mt}）"


def restore_prompt():
    """起動時、前回の作業（このPCの最新の保存）があれば『続きから復元／新規』を出す
    （extract未読込のときのみ）。生成結果(out/)も含めて復元する。"""
    if ss().get("extract") or ss().get("_restore_dismissed"):
        return
    files = []
    if AUTOSAVE_DIR.exists():
        files = sorted((p for p in AUTOSAVE_DIR.glob("session_*.json") if p.stat().st_size > 200),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return
    f = files[0]   # 前回の作業＝このPCの最新の保存
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return
    st.info("💾 前回の作業が保存されています：" + _restore_label(data, f)
            + "　**続きから再開**できます（抽出・路線の割り当て・時刻表の修正・③の入力・"
              "生成結果まで復元）。")
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("前回の続きから復元する", type="primary"):
        try:
            forms = data.pop("form_inputs", {}) or {}
            for k, v in data.items():
                ss()[k] = v
            for k, v in forms.items():   # ③の入力欄も復元（ウィジェットkeyへ直接）
                ss()[k] = v
            _sid = f.stem.replace("session_", "")   # このファイルのsidを引き継ぐ
            ss()["sid"] = _sid
            try:
                st.query_params["sid"] = _sid
            except Exception:
                pass
            _restore_out(_sid)   # 生成物(out/)・作業ファイルを戻す（④⑤⑥・DL・再生成を復元）
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

with st.expander("📖 使い方ガイド（はじめての方はここを開いてください）",
                 expanded=not ss().get("extract")):
    st.markdown("""
**この道具でできること**：バス時刻表（PDF・Excel）を読み込み、**①→⑥の順に確認・入力するだけ**で、
標準フォーマット **GTFS-JP** を作れます。難しい処理は自動で行い、**人の判断が要る所だけ**をあなたに尋ねます。
所要は 15〜30 分ほどです。

---

**① 時刻表をアップロード** — 時刻表ファイルを選びます。
文字を選択できる PDF・Excel はそのまま抽出、スキャン画像の PDF は抽出後に「OCRして続行」ボタンが出ます。
> 💡 **こんな時は**：元の Excel が手元にあれば **Excel が最も正確**です。画像 PDF は OCR するため
> 数字の誤読が出ることがあり、後の⏰で必ず見比べてください。

**② 路線の割り当て** — どのページ（＝**便のまとまり**）が **どの路線・方向** かを表で確認します。
**行き先表示** と **運行する曜日（月〜日のチェック）** もこの表で直せます（月水金など任意の組合せOK）。
> 💡 **こんな時は**：行きと帰りは **同じ路線名** にして方向を 0／1 に。平日と土日で時刻が違う時刻表は、
> そのページの**曜日チェック**を 月〜金／土・日 に分けます（別々のダイヤとして出力されます）。

**⏰ 時刻表の確認・修正** — 取り込んだ時刻を原典と見比べます。
**<span style="color:#c62828">赤＝時刻の逆行（要修正）</span>**、**<span style="color:#1565c0">青＝日跨ぎ（翌日・正常）</span>**。
停留所名もこの画面で直せます。
> 💡 **こんな時は**：「待機時間」などの逆行は自動で除外されますが、赤が残ったら原典を見て正しい時刻に直します。
> 赤いセルの下に **専用の修正欄** が出るので、そこに正しい時刻を入れられます。

**③ PDF/Excel に無い項目を入力** — 時刻表に **書かれていない** 情報
（事業者名・運賃・有効期間・運休日など）を入れます（**運行する曜日は②の『運行日』で設定**）。
**分からない項目は空欄のままで OK**（推測で埋めない＝誤りを作らない）。
> 💡 **こんな時は**：運賃がどの路線も同じなら **「全路線を同じ運賃にする」** で1回入力。
> 区間で違うなら **「区間運賃にする」** で表に入力。事業者名や法人番号が不明なら空欄のままで進められます。

**生成** — 「GTFS-JP を生成する」を押すと、座標補完・路線図・翻訳・検証まで **自動で** 走ります。

**④ 結果（ふりがな・停留所名の確認）** — 読み（ふりがな）や英語、停留所名の誤りを直します。
**⚠印** が付いた行（漢字が残る等）は特に確認してください。
> 💡 **こんな時は**：難読地名（例：相島＝あいのしま）は自動では誤読しがちです。原典を見て正しい読みに直すと、
> zip と地図が更新されます。**「🔎 AIで読みをチェック（任意・要確認）」** を押すと、自動読みと違う所を
> AIが洗い出します（採用は自分で選ぶ。APIキーが要ります）。

**⑤ 座標の確認（地図）** — 地図で停留所の位置を確認します。**<span style="color:#e08a1e">橙＝要確認</span>** の停留所を、
**地図の点をクリックして選び**、**ピンをドラッグして正しい位置へ動かして**確定します（動かした先の緯度経度が入ります）。
> 💡 **こんな時は**：地図の点を押すと、下の一覧でその停留所が自動で選ばれ、緯度・経度が表示されます。
> ピンをドラッグ→クリックでその位置に確定。同じ名前のバス停が県内に複数あると位置を誤ることがあるので、
> 橙が残っている間は「公式提出可」にせず、必ず地図で確認してください。

**⑥ ビューアで確認 → ダウンロード** — 完成した内容をブラウザで確認し、
**ビューアの下にある大きなボタンから GTFS-JP 一式（zip）をダウンロード** します。

---

**この道具の約束**：分からない事は推測せず「要確認」に上げます。
**赤・⚠・橙（要確認）が出たら必ず確認**してから提出してください。
""", unsafe_allow_html=True)

# 用語と仕組みのヘルプ（循環路線・有効期間/運行期間・Nominatim・保存先・内部コード）
with st.expander("❓ 用語と仕組み（ヘルプ）— 循環路線 / 有効期間と運行期間 / Nominatim / 保存先 など"):
    st.markdown(
        "**循環路線とは**　始点＝終点で一周して戻ってくる路線です。方向は **0 のまま**、"
        "行き先表示は「右回り／左回り」等でOK。始点と終点が同じ停留所だと自動で検出し、③で"
        "「循環路線」のチェックを提案します。\n\n"
        "**有効期間と運行期間の違い**　現在アプリで入力する **有効期間（開始〜終了）** は、"
        "そのダイヤ（サービス）が有効な期間＝カレンダーの期間です。GTFSでは本来、"
        "**feed全体の有効期間**（データそのものの有効期限）と、**各サービスの運行期間**を別々に持てます。"
        "多くの場合は同じでよいので1つにまとめていますが、分けたい場合は対応できます。\n\n"
        "**Nominatim とは**　OpenStreetMap の**住所→座標**変換サービスです。"
        "国土数値情報(P11)で座標が埋まらなかった停留所を補う時に使います（**任意ON・やや遅い・"
        "POIの多い路線向け**）。まずP11で埋め、それでも残った所にだけ使うのがおすすめです。\n\n"
        "**保存先はどこ？**　① 作業中のファイルはPCの一時フォルダ。"
        "② 途中経過は自動保存され、**ホームの `.gtfs_jp_app` フォルダ**に置かれます"
        "（このページのURLをブックマークすれば『続きから復元』できます）。"
        "③ 完成した **GTFS-JP 一式(zip)** は、⑥のボタンからブラウザのダウンロード先に保存されます。\n\n"
        "**内部コード（仕組み）**　抽出→構造化→座標補完→検証まで、判断が要る所だけ人に尋ね、"
        "残りは決定的なスクリプトが自動で行う三層構成です（同じ入力なら同じ出力＝再現性あり）。")

restore_prompt()   # 前回の自動保存があれば「続きから復元/新規」を提示
if ss().get("extract"):
    st.caption("💾 作業は自動保存されています。**このページのURLをブックマーク**しておくと、"
               "タブを閉じても同じURLを開けば『続きから復元』できます（他の人の作業とは分離）。")

# ── 作業の選び方（最初に1画面で選ぶ）→ 一人は3作業を切替、複数人は担当の作業だけ表示 ──
WORK_AREAS = [("tt", "🕐 時刻表・路線の割り当て"),
              ("q", "📝 不足分の入力（PDF/Excelに無い項目）"),
              ("coord", "🗺 結果・座標の確認")]
_area_label = dict(WORK_AREAS)
_mode = ss().get("work_mode")
if _mode not in ("solo", "tt", "q", "coord"):
    _mode = None

if not _mode:
    # 選択画面（1画面）。ここで進め方を選ぶまで下の作業は表示しない。
    st.markdown("### まず、作業の進め方を選んでください")
    st.caption("一人で全部進めるか、複数人で分担するかを選びます。あとで「◀ 選び直す」で変更できます。")
    if st.button("🧑 一人で全部進める（時刻表 → 入力 → 座標の順）", type="primary",
                 use_container_width=True):
        ss()["work_mode"] = "solo"; st.rerun()
    st.markdown("**または、複数人で分担する場合は担当を選択：**")
    _pcols = st.columns(3)
    for _i, (_k, _lbl) in enumerate(WORK_AREAS):
        if _pcols[_i].button(_lbl, key=f"pick_{_k}", use_container_width=True):
            ss()["work_mode"] = _k; st.rerun()
    st.caption("分担のときは、選んだ担当の作業画面だけが表示されます。作業は自動保存され、"
               "同じURLを開けば別の担当が続きから作業できます。")
    st.stop()

# モード決定後：上部に「選び直す」。一人＝3タブを自動作成、複数人＝担当の作業だけ表示。
_top1, _top2 = st.columns([1, 3])
if _top1.button("◀ 選び直す"):
    ss().pop("work_mode", None); st.rerun()
if _mode == "solo":
    _top2.caption("一人モード：3つのタブを順に進めてください（時刻表 → 入力 → 座標）。")
    tab_tt, tab_q, tab_coord = st.tabs([_l for _, _l in WORK_AREAS])
    _show_tt = _show_q = _show_coord = True
else:
    tab_tt = tab_q = tab_coord = nullcontext()
    _show_tt = (_mode == "tt")
    _show_q = (_mode == "q")
    _show_coord = (_mode == "coord")
    _top2.markdown(f"**担当：{_area_label.get(_mode, '')}**（複数人で分担）")
    # 前工程が未完了の担当を開いたときは、空画面にせず案内を出す（分担の待ち合わせ）。
    if _show_q and not ss().get("decision_spec"):
        st.info("まだ「時刻表・路線の割り当て」が終わっていません。"
                "時刻表担当が①②を終えると、ここで不足分を入力できます。")
    if _show_coord and not ss().get("result"):
        st.info("まだ生成されていません。「不足分の入力」で『GTFS-JP を生成する』を押すと、"
                "ここに結果・地図（座標の確認）が表示されます。")

if _show_tt:
    with tab_tt:
        # =====================================================================
        # Step 1: アップロード → 抽出
        # =====================================================================
        st.header("① 時刻表をアップロード")
        up = st.file_uploader("バス時刻表（.xlsx / PDF / Word(.docx) / PowerPoint(.pptx) / OCR後の .md）— **複数選択できます**",
                              type=["xlsx", "pdf", "docx", "pptx", "md"], accept_multiple_files=True)
        st.caption("📄 文字が選べるPDF・Excelはそのまま抽出。**Word/PowerPoint**は中の**表**をそのまま読み取り"
                   "（時刻表が画像で貼られている場合はOCRに回します）。**画像化PDF（スキャン）**は、"
                   "抽出するとアプリ内で**OCRして続行するボタン**が出ます（ターミナル不要）。"
                   "**複数のファイル**（路線ごとに分かれた時刻表など）を選ぶと、まとめて1つのGTFS-JPにできます"
                   "（②で全路線を割り当て）。")


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


        def _open_source_new_tab_button(sp, low, where, label="📄 原典を別タブで開く（別画面に並べて見比べる）"):
            """原典(PDF/画像)を別タブで開くボタン。別ウィンドウ/別モニタに並べて見比べやすくする。
    ファイルを Blob 化して window.open するため、ローカルファイルでもブラウザで開ける。
    複数原典を並べて置けるよう、要素IDは where で一意化し、ボタン文言は label で差し替える。"""
            import base64
            import re as _re
            import html as _html
            mime = ("application/pdf" if low.endswith(".pdf")
                    else "image/jpeg" if low.endswith((".jpg", ".jpeg"))
                    else "image/gif" if low.endswith(".gif")
                    else "image/webp" if low.endswith(".webp")
                    else "image/bmp" if low.endswith(".bmp") else "image/png")
            try:
                b64 = base64.b64encode(Path(sp).read_bytes()).decode()
            except Exception:
                return
            _bid = "op_" + (_re.sub(r"[^0-9A-Za-z_]", "", str(where)) or "src")
            _tmpl = """
        <button id="__BID__" style="width:100%;padding:9px 14px;border:1px solid #0e5c6b;
          background:#e6f0f1;color:#0a4552;border-radius:6px;font-weight:700;font-size:14px;
          cursor:pointer;font-family:sans-serif">__LABEL__</button>
        <script>
        const b64=__B64__, mime=__MIME__;
        document.getElementById("__BID__").onclick=function(){
          const bin=atob(b64), arr=new Uint8Array(bin.length);
          for(let i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i);
          const url=URL.createObjectURL(new Blob([arr],{type:mime}));
          window.open(url,"_blank");
        };
        </script>
        """
            components.html(
                _tmpl.replace("__BID__", _bid).replace("__LABEL__", _html.escape(label))
                .replace("__B64__", json.dumps(b64)).replace("__MIME__", json.dumps(mime)),
                height=46)


        def render_source_panel(where=""):
            """アップロードした原本（PDF/画像）を編集画面の隣で見られる開閉パネル。
    時刻・停留所・運賃を原典と横並びで照合できるようにし、誤読・誤りの見落としを減らす。
    複数ファイルを取り込んだ時は、各原典を個別に別タブで開けるようにする。"""
            _IMG = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
            srcs = ss().get("sources_all") or ([ss().get("source_display")] if ss().get("source_display") else [])
            srcs = [s for s in srcs if s and Path(s).exists() and str(s).lower().endswith(_IMG)]
            if not srcs:
                return
            multi = len(srcs) > 1
            for i, sp in enumerate(srcs):
                _lbl = (f"📄『{Path(sp).name}』を別タブで開く" if multi
                        else "📄 原典を別タブで開く（別画面に並べて見比べる）")
                _open_source_new_tab_button(sp, str(sp).lower(), f"{where}{i}", _lbl)
            st.caption("↑ 別タブで開いて、ウィンドウを横に並べる（または別モニタに移す）と、下の表と見比べやすくなります。"
                       + ("（複数ファイルはそれぞれ開けます）" if multi else ""))
            # インラインのプレビュー枠は廃止（別タブで開いて見比べる方式に統一＝画面をすっきり）。


        def _pdf_time_pages(src):
            """時刻トークンを持つページ番号(1始まり)一覧。複数あれば全ページ抽出の対象。
    pdfplumberが使えない/PDFでない場合は None（従来の単一ページ抽出にフォールバック）。"""
            try:
                import pdfplumber
            except Exception:
                return None
            tre = re.compile(r'^\d{1,2}[:：]\d{2}')
            pages = []
            try:
                with pdfplumber.open(src) as pdf:
                    for idx, pg in enumerate(pdf.pages):
                        try:
                            n = sum(1 for w in pg.extract_words() if tre.match(w['text']))
                        except Exception:
                            n = 0
                        if n >= 3:          # 時刻がごく少ないページ(表紙/凡例/連絡先)は除外
                            pages.append(idx + 1)
            except Exception:
                return None
            return pages


        def _extract_merge_pages(src, pages, ext_out):
            """時刻のある各ページを個別抽出し、blocks を1つの extract.json に統合する。
    block_index は全ページ通しで振り直し、needs の block 参照も付け替える。
    複数路線が別ページに分かれた時刻表(例: マリンクス)で全ルートを取り込むための処理。"""
            merged = {"source": str(src), "page": pages[0], "pages": pages,
                      "blocks": [], "warnings": [], "needs_confirmation": []}
            tmp = WORK / "extract_page.json"
            last_se, n_img = "", 0
            for pno in pages:
                rc, so, se = run([SCRIPTS / "extract_timetable_coords.py", src,
                                  "-o", tmp, "--page", str(pno)])
                last_se = se or last_se
                if rc != 0 or not tmp.exists():
                    merged["warnings"].append(f"p{pno}: 抽出に失敗（スキップ）")
                    continue
                pj = json.loads(tmp.read_text(encoding="utf-8"))
                if not pj.get("blocks") and any(n.get("type") == "image_pdf_use_ocr"
                                                for n in pj.get("needs_confirmation", [])):
                    n_img += 1
                off = len(merged["blocks"])          # このページのブロックを通し番号へ
                idx_map = {}
                for b in pj.get("blocks", []):
                    old = b.get("block_index")
                    b["page"] = pno
                    b["block_index"] = off + (old if isinstance(old, int) else 0)
                    idx_map[old] = b["block_index"]
                    merged["blocks"].append(b)
                for nd in pj.get("needs_confirmation", []):
                    if nd.get("type") == "image_pdf_use_ocr":
                        continue             # 個別ページのOCR誘導は全体では出さない
                    if "block" in nd and nd["block"] in idx_map:
                        nd = dict(nd); nd["block"] = idx_map[nd["block"]]
                    merged["needs_confirmation"].append(nd)
                for w in pj.get("warnings", []):
                    merged["warnings"].append(f"p{pno}: {w}")
            # 全ページ画像化でブロックが1つも取れない → OCR経路へ誘導（単一ページ時と同じ挙動）
            if not merged["blocks"] and n_img:
                merged["needs_confirmation"].append({
                    "type": "image_pdf_use_ocr", "page": pages[0],
                    "message": "全ページが画像化(テキストレイヤなし)と判定しました。OCR(MinerU)経路で抽出してください。"})
            ext_out.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0, "", last_se


        def _extract_one(src, ext_out):
            """1ファイルを種別に応じて抽出し ext_out(extract.json形式)に書く。(rc,so,se)を返す。"""
            low = str(src).lower()
            if low.endswith(".xlsx"):
                return run([SCRIPTS / "extract_timetable_excel.py", src, "-o", ext_out])
            if low.endswith(".md"):
                return run([SCRIPTS / "extract_timetable_markdown.py", src, "-o", ext_out])
            # PDF: 路線が別ページに分かれていれば全ページ抽出して統合。1ページ/判定不可は従来抽出。
            _pages = _pdf_time_pages(src)
            if _pages and len(_pages) > 1:
                return _extract_merge_pages(src, _pages, ext_out)
            return run([SCRIPTS / "extract_timetable_coords.py", src, "-o", ext_out])


        def _expand_office(src):
            """Word(.docx)/PowerPoint(.pptx) を既存経路に乗る中間ファイルへ展開して返す。
    ・時刻表の『表』→ .xlsx（表ごと1ファイル＝複数路線は複数ファイル統合にそのまま乗る）
    ・表が無く画像貼付のみ → 画像を束ねた .pdf（画像PDF→OCR経路へ）
    office 以外はそのまま [src]。取り出せなければ [] を返し警告を表示（正しく失敗）。"""
            low = str(src).lower()
            if not low.endswith((".docx", ".pptx")):
                return [src]
            outdir = WORK / ("office_" + Path(src).stem)
            rc, so, se = run([SCRIPTS / "office_to_intermediate.py", src, "--outdir", outdir])
            info = {}
            try:
                info = json.loads((so or "").strip().splitlines()[-1])
            except Exception:
                info = {}
            if rc == 0 and info.get("kind") == "xlsx" and info.get("paths"):
                return [Path(p) for p in info["paths"]]
            if rc == 0 and info.get("kind") == "pdf" and info.get("path"):
                return [Path(info["path"])]
            st.warning(f"『{Path(src).name}』から時刻表を取り出せませんでした。"
                       + (info.get("message") or "")
                       + "（Word/PowerPoint内に時刻表を『表』か『画像』として入れてください）")
            return []


        def _is_image_pdf_result(ex):
            """抽出結果が「画像化PDF＝OCRが必要」で0ブロックか。"""
            return (not ex.get("blocks")) and any(
                n.get("type") == "image_pdf_use_ocr" for n in ex.get("needs_confirmation", []))


        def do_extract(src):
            ext_out = WORK / "extract.json"
            low = str(src).lower()
            # 原本プレビュー用に元ファイルを記録（OCR後の .md では上書きせず、元のPDF/画像を保持）。
            if not low.endswith(".md"):
                ss()["source_display"] = str(src)
            ss()["sources_all"] = [str(src)]
            rc, so, se = _extract_one(src, ext_out)
            ss().pop("ocr_pending", None)   # 新しい抽出のたびに前回のOCR待ちを消す
            if rc == 0 and ext_out.exists():
                ex = json.loads(ext_out.read_text(encoding="utf-8"))
                # 画像化PDFで0停留所 → アプリ内OCRへ誘導（空のまま進めない）
                if _is_image_pdf_result(ex):
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


        def do_extract_multi(srcs):
            """複数ファイルを個別抽出し、blocks を1つの extract.json に統合する（②でまとめて路線割当）。
    各 block に元ファイル(source_file/source_name)を記録。block_index は全ファイル通しで振り直す。
    画像化PDF(要OCR)や失敗ファイルはスキップして警告する（＝正しく失敗）。"""
            if len(srcs) == 1:
                return do_extract(srcs[0])
            ext_out = WORK / "extract.json"
            tmp = WORK / "extract_file.json"
            merged = {"source": None, "sources": [str(s) for s in srcs],
                      "blocks": [], "warnings": [], "needs_confirmation": []}
            detected_all = {}
            img_files, fail_files = [], []
            for src in srcs:
                name = Path(src).name
                rc, so, se = _extract_one(src, tmp)
                if rc != 0 or not tmp.exists():
                    fail_files.append(name)
                    continue
                pj = json.loads(tmp.read_text(encoding="utf-8"))
                if _is_image_pdf_result(pj):
                    img_files.append(name)   # 画像PDFは1つずつOCRが要るのでここではスキップ
                    continue
                off = len(merged["blocks"])
                idx_map = {}
                for b in pj.get("blocks", []):
                    old = b.get("block_index")
                    b["source_file"] = str(src)
                    b["source_name"] = name
                    b["block_index"] = off + (old if isinstance(old, int) else 0)
                    idx_map[old] = b["block_index"]
                    merged["blocks"].append(b)
                for nd in pj.get("needs_confirmation", []):
                    if nd.get("type") == "image_pdf_use_ocr":
                        continue
                    nd = dict(nd)
                    if "block" in nd and nd["block"] in idx_map:
                        nd["block"] = idx_map[nd["block"]]
                    nd["file"] = name
                    merged["needs_confirmation"].append(nd)
                for w in pj.get("warnings", []):
                    merged["warnings"].append(f"{name}: {w}")
                # 運賃・事業者などの条件検出をマージ（先に見つかった非空値を採用）
                cond_tmp = WORK / "conditions_file.json"
                run([SCRIPTS / "detect_conditions.py", src, "-o", cond_tmp])
                if cond_tmp.exists():
                    d2 = json.loads(cond_tmp.read_text(encoding="utf-8"))
                    for k, v in d2.items():
                        if k == "_evidence":
                            detected_all.setdefault("_evidence", {}).update(v or {})
                        elif v not in (None, "", []) and k not in detected_all:
                            detected_all[k] = v
            if img_files:
                merged["warnings"].append("画像化PDF（OCRが必要）はスキップ: " + "・".join(img_files)
                                          + "。画像PDFは1つずつ取り込み→OCRしてから使ってください。")
            if fail_files:
                merged["warnings"].append("抽出に失敗（スキップ）: " + "・".join(fail_files))
            ext_out.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            ss().pop("ocr_pending", None)
            if not merged["blocks"]:
                st.error("どのファイルからも時刻表を抽出できませんでした。"
                         + ("画像化PDFは1つずつOCRしてから取り込んでください。" if img_files else ""))
                return
            ss().extract = merged
            ss().extract_token = "+".join(Path(s).name for s in srcs)
            ss()["sources_all"] = [str(s) for s in srcs]
            ss()["source_display"] = next((str(s) for s in srcs if not str(s).lower().endswith(".md")),
                                          str(srcs[0]))
            for k in ("decision_spec", "result", "confirmed"):
                ss().pop(k, None)
            ss().detected = detected_all
            _msg = (f"{len(srcs)} 個のファイルから 計 {len(merged['blocks'])} ブロックを取り込みました。"
                    "②でまとめて路線を割り当てできます。")
            if img_files or fail_files:
                _msg += "（一部スキップ：下の警告を確認）"
            st.success(_msg)


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


        def _ai_readings_apply(ai_key, ai_ctx):
            """生成後の translations.txt に対し、AIが探索した読みを『自動読みと違う所だけ』既定値に反映。
    自動確定ではなく“既定値”＝④で必ず人が確認する前提。反映した停留所を ss()['ai_applied'] に記録。"""
            ss().pop("ai_applied", None)
            _tp = WORK / "out" / "gtfs" / "translations.txt"
            if not _tp.exists():
                return
            import csv as _c2
            _cur = {}
            for _r in _c2.DictReader(_tp.open(encoding="utf-8-sig")):
                if (_r.get("table_name") or "").strip() == "stops" and (_r.get("language") or "").strip() == "ja-Hrkt":
                    _cur[(_r.get("field_value") or "").strip()] = _r.get("translation", "")
            if not _cur:
                return
            with st.spinner("AIで読みを探索中..."):
                _sug = claude_structure.suggest_readings(list(_cur.keys()), ai_key, context=ai_ctx)
            _by, _applied = {}, {}
            for _nm, _s in (_sug or {}).items():
                _y = (_s.get("yomi") or "").strip()
                if _y and _nm in _cur and _y != _cur[_nm]:   # 自動読みと違う所だけ既定値に
                    _by[_nm] = {"ja-Hrkt": _y}
                    _applied[_nm] = {"before": _cur[_nm], "yomi": _y,
                                     "confidence": _s.get("confidence", ""), "note": _s.get("note", "")}
            if _by:
                _mr = WORK / "manual_readings.json"
                _mr.write_text(json.dumps({"by_stop_name": _by}, ensure_ascii=False, indent=2), encoding="utf-8")
                run([SCRIPTS / "apply_manual_readings.py", _tp, "--readings", _mr])
                _zz = list((WORK / "out").glob("*_gtfs-jp.zip"))
                if _zz:
                    run([SCRIPTS / "package_gtfs_zip.py", WORK / "out" / "gtfs", "-o", _zz[0]])
            ss()["ai_applied"] = _applied
            st.info(f"AIが読みを探索し、自動読みと違う **{len(_applied)} 件**を既定値に反映しました。"
                    "**④で必ず確認**してください（AI由来＝要確認）。")


        def run_generation(spec, muni, use_nom, hol, ai_read=False, ai_key="", ai_ctx=""):
            """spec から GTFS-JP を生成（apply_decisions→run_pipeline）。ss().result に結果を入れる。"""
            ss().pop("ai_applied", None)   # 再生成のたびに前回のAI探索マークを消す
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
                    cfg["holiday_nenmatsu"] = hol.get("nenmatsu_range") or "12-29:01-03"
                if hol.get("obon"):
                    cfg["holiday_obon"] = hol.get("obon_range") or "08-13:08-15"
                (WORK / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
                rc, so, se = run([SCRIPTS / "run_pipeline.py", "--config", WORK / "config.json"], cwd=REPO)
            ss().result = {"rc": rc, "log": se}
            ss()["_out_dirty"] = True   # 生成物(out/)を保存対象にする（再起動後に④⑤⑥を復元）
            # 生成後、任意でAIが読みを探索して既定値に反映（④で確認）。失敗しても生成物は無事。
            if rc == 0 and ai_read and ai_key:
                try:
                    _ai_readings_apply(ai_key, ai_ctx or muni)
                except Exception as _e:
                    st.warning(f"AI読み探索はスキップしました（{_e}）。④で個別に実行できます。")
            st.success("完了しました。" if rc == 0 else "完了（警告/エラーあり）。")


        def _with_overlay(fn, msg="時刻表を読み取っています…"):
            """処理中は画面中央に大きなローディングを出す（右上の小さな印だと気づきにくいため）。"""
            _ph = st.empty()
            _ph.markdown(
                "<div style='position:fixed;inset:0;background:rgba(246,248,252,.9);z-index:99999;"
                "display:flex;flex-direction:column;align-items:center;justify-content:center;gap:22px'>"
                "<div style='width:70px;height:70px;border:7px solid #cfe0e3;border-top-color:#0e5c6b;"
                "border-radius:50%;animation:gjspin 1s linear infinite'></div>"
                f"<div style='font-size:23px;font-weight:700;color:#0a4552'>⏳ {msg}</div>"
                "<div style='font-size:14px;color:#4c5663'>少しお待ちください（数秒〜数分）</div></div>"
                "<style>@keyframes gjspin{to{transform:rotate(360deg)}}</style>",
                unsafe_allow_html=True)
            try:
                fn()
            finally:
                _ph.empty()


        def _extract_with_overlay(src, msg="時刻表を読み取っています…"):
            _with_overlay(lambda: do_extract(src), msg)


        SAMPLES = Path(__file__).resolve().parent / "samples"
        _ups = up if isinstance(up, list) else ([up] if up else [])   # 単一/複数どちらでもリスト化
        if st.button("抽出する", type="primary", disabled=(not _ups)) and _ups:
            _saved = []
            for _f in _ups:
                _p = WORK / _f.name
                _p.write_bytes(_f.getbuffer())
                _saved.append(_p)
            # Word/PowerPoint は中間ファイル(.xlsx/.pdf)へ展開してから既存経路に乗せる。
            _srcs = []
            for _p in _saved:
                _srcs.extend(_expand_office(_p))
            if not _srcs:
                st.stop()   # 展開できる時刻表が無い（警告は _expand_office が表示済み）
            if len(_srcs) == 1:
                _extract_with_overlay(_srcs[0])
            else:
                _with_overlay(lambda: do_extract_multi(_srcs),
                              msg=f"{len(_srcs)} 個の時刻表を読み取っています…")
        st.caption("サンプルで試す:")
        c_b, c_c, c_d = st.columns([1, 1, 1])
        if c_b.button("太宰府まほろば号（往復）"):
            _extract_with_overlay(SAMPLES / "sample_dazaifu_mahoroba.xlsx")
        if c_c.button("築城巡回線（循環・変則便）"):
            _extract_with_overlay(SAMPLES / "sample_tsuiki_junkai.xlsx")
        if c_d.button("こがバス（画像PDF→OCR）"):
            _extract_with_overlay(SAMPLES / "sample_koga_ocr.md")

        # 画像化PDFが検出されたら、アプリ内でOCRして続行できるパネルを出す
        render_ocr_panel()

        if "extract" in ss():
            ex = ss().extract
            blocks = ex.get("blocks", [])
            total_trips = sum(len(b.get("trips", [])) for b in blocks)
            st.info(f"便のまとまり {len(blocks)} 組 / 便 計 {total_trips}"
                    "（便のまとまり＝時刻表のひとかたまり。PDFなら1ページ分・往復なら片道分）")
            for b in blocks:
                trips = b.get("trips", [])
                # 便ごとに停留所数が異なる（循環・区間便）ため、代表は便[0]でなく全体の停留所列を使う
                full = [s.get("name") for s in b.get("stops", [])]
                if not full and trips:
                    full = max(([c["name"] for c in t["cells"]] for t in trips), key=len, default=[])
                loop = bool(full) and full[0] == full[-1]
                tag = f"（始点=終点「{full[0]}」→循環とみられます）" if loop else ""
                st.write(f"- 便のまとまり {b.get('block_index')}（{b.get('direction_hint') or '方向見出しなし'}）"
                         f": 便 {len(trips)} 本 / 停留所 {len(full)} 個{tag}")
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
        def _route_name_from(text):
            """文字列から路線名らしい語（○○線 / ○○系統 / ○○ルート）を1つ取り出す。
    区切りで分割し、末尾の付随語（時刻表・ダイヤ等）を落として末尾が 線/系統/ルート の語を返す。
    JR等の鉄道路線・「系統番号」等のラベルは除外。見つからなければ None。"""
            if not text:
                return None
            for tok in re.split(r"[\s　_\-\[\]【】（）()、,。/／|｜:：]+", str(text)):
                tok = re.sub(r"(時刻表|時刻|ダイヤ|運行表|一覧表|表)$", "", tok.strip())
                if not tok or not (2 <= len(tok) <= 20):
                    continue
                if any(x in tok for x in ("新幹線", "ゆたか線", "福北", "鉄道", "番号", "種類")):
                    continue   # 鉄道の路線・「系統番号」等のラベルは除外（JR古賀線等のバス路線名は許可）
                if tok.endswith(("線", "系統", "ルート")):
                    return tok
            return None


        def _auto_route_rows(bs, source=""):
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
            # ファイル名からの路線名候補は、まとまり(グループ)ごとに元ファイル(source_file)から拾う
            # （複数ファイル取り込み時は各路線が別ファイル由来のため。単一時は共通の source を使う）。
            from pathlib import Path as _P
            rows = []
            for gi, (_rep, members) in enumerate(grouped):
                # 路線名はグループで1つ（往復は同じ路線名・方向0/1）。「○○線／○○系統／○○ルート」を
                # ①このまとまりの停留所名・方向見出し ②ファイル名 の順で自動取得（線/系統/ルートがトリガー）。
                # どれも無ければ端点「始点～終点」で作る（従来どおり）。
                nm0 = [s.get("name") for s in bs[members[0]].get("stops", [])]
                _line = None
                # ① 見出し(ページ上部のタイトル route_title)を最優先（例: 山らいず線・相らんど線）
                for _mbi in members:
                    if bs[_mbi].get("route_title"):
                        _line = bs[_mbi]["route_title"]
                        break
                # ② 停留所名・方向見出しから拾う
                if not _line:
                    for _mbi in members:
                        for _s in bs[_mbi].get("stops", []):
                            _line = _route_name_from(_s.get("name"))
                            if _line:
                                break
                        if not _line and bs[_mbi].get("direction_hint"):
                            _line = _route_name_from(bs[_mbi]["direction_hint"])
                        if _line:
                            break
                # ③ ファイル名の候補（このまとまりの元ファイル → 無ければ共通 source）
                if not _line:
                    _grp_src = next((bs[m].get("source_file") for m in members if bs[m].get("source_file")),
                                    source)
                    _line = _route_name_from(_P(str(_grp_src)).stem) if _grp_src else None
                rname = _line or (f"{nm0[0]}～{nm0[-1]}" if nm0 else f"路線{gi + 1}")
                for d, bi in enumerate(members):
                    nm = [s.get("name") for s in bs[bi].get("stops", [])]
                    # 行き先の既定: 方向見出し(direction_hint)があれば入れる。無ければ空にして、
                    # 生成時に終点名/循環判定から自動で「○○方面」にさせる（循環の起点手前も正しく扱う）。
                    dh = bs[bi].get("direction_hint")
                    _row = {"ブロック": bi, "見出し": dh or "",
                            "停留所数": len(nm), "路線名": rname,
                            "方向(0/1)": "0（行き）" if d % 2 == 0 else "1（帰り）",
                            "行き先表示": dh or "",
                            "月": True, "火": True, "水": True, "木": True, "金": True, "土": False, "日": False}
                    if bs[bi].get("source_name"):        # 複数ファイル取り込み時のみ元ファイル名を表示
                        _row["ファイル"] = bs[bi]["source_name"]
                    rows.append(_row)
            return rows


        # 運行する曜日は便のまとまりごとに 月〜日 の7チェックで指定する（月水金など任意の組合せ可）。
        DAY_COLS = ["月", "火", "水", "木", "金", "土", "日"]
        DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        DAY_DEFAULT = [True, True, True, True, True, False, False]   # 既定＝平日(月〜金)
        # 旧保存データ（プリセット名）→7曜日 の読み替え表（後方互換）。
        _OLD_PATTERN_DAYS = {
            "平日(月〜金)": [1, 1, 1, 1, 1, 0, 0], "土曜": [0, 0, 0, 0, 0, 1, 0],
            "日曜・祝日": [0, 0, 0, 0, 0, 0, 1], "土日祝": [0, 0, 0, 0, 0, 1, 1],
            "毎日": [1, 1, 1, 1, 1, 1, 1], "平日（既定）": [1, 1, 1, 1, 1, 0, 0],
        }


        def _days_label(bits):
            """7曜日ビット[0/1]×7 → 分かりやすいダイヤ名（例 平日(月〜金)/月・水・金/土日/毎日）。"""
            b = [int(x) for x in bits] if (isinstance(bits, (list, tuple)) and len(bits) == 7) else [1, 1, 1, 1, 1, 0, 0]
            if b == [1, 1, 1, 1, 1, 0, 0]:
                return "平日(月〜金)"
            if b == [1, 1, 1, 1, 1, 1, 1]:
                return "毎日"
            if b == [0, 0, 0, 0, 0, 1, 1]:
                return "土日"
            _n = [DAY_COLS[i] for i in range(7) if b[i]]
            return "・".join(_n) if _n else "（曜日なし）"


        def _svc_id_from_days(bits):
            b = [int(x) for x in bits] if (isinstance(bits, (list, tuple)) and len(bits) == 7) else [1, 1, 1, 1, 1, 0, 0]
            if not any(b):
                b = [1, 1, 1, 1, 1, 0, 0]
            return "SVC_" + "".join(str(x) for x in b)


        def _service_labels_map():
            """②の運行日(block_days)から {ダイヤ名: service_id} を作る（同一組合せは1つに集約）。"""
            m = {}
            for _d in (ss().get("decision_spec", {}) or {}).get("block_days", {}).values():
                m[_days_label(_d)] = _svc_id_from_days(_d)
            return m


        if "extract" in ss():
            st.header("② 路線の割り当て（どの路線・方向か）")
            st.caption("停留所の並びが同じ**便のまとまり**を自動で**同じ路線**にまとめ、"
                       "方向（行き=0/帰り=1）を割り振りました（要確認）。複数路線・往復の対応づけが違うときは"
                       "表を編集してください。路線名も変更できます。"
                       "（便のまとまり＝時刻表のひとかたまり／PDFなら1ページ分）")
            st.markdown("<span style='color:#16202B;font-weight:700'>✏️ 表のセルはクリック（ダブルクリック）で"
                        "編集できます</span><span style='color:#16202B'>（路線名・方向・行き先・運行日）。</span>",
                        unsafe_allow_html=True)
            blocks_e = ex.get("blocks", [])
            _rows0 = _auto_route_rows(blocks_e, ex.get("source", ""))
            # 復元直後（route_editor の編集stateがまだ無い時）だけ、保存済みの割り当て(decision_spec)を
            # 初期表示に反映して「続きから」を実現する。以降はユーザーの編集が優先される。
            _ds0 = ss().get("decision_spec")
            if _ds0 and f"route_editor_{ss().get('extract_token','')}" not in ss():
                _b2r = {int(b): r.get("route_long_name", "")
                        for r in _ds0.get("routes", []) for b in r.get("blocks", [])}
                _bd0 = _ds0.get("block_direction", {})
                _bh0 = _ds0.get("block_headsign", {})
                _bday0 = _ds0.get("block_days", {})     # 新仕様（7曜日）
                _bp0 = _ds0.get("block_pattern", {})    # 旧仕様（プリセット名・後方互換）
                for _row in _rows0:
                    _bi = _row["ブロック"]
                    if _b2r.get(_bi):
                        _row["路線名"] = _b2r[_bi]
                    if str(_bi) in _bd0:
                        _row["方向(0/1)"] = "0（行き）" if int(_bd0[str(_bi)]) == 0 else "1（帰り）"
                    if _bh0.get(str(_bi)):
                        _row["行き先表示"] = _bh0[str(_bi)]
                    # 運行日の復元: 新仕様(7曜日)を優先。無ければ旧仕様(プリセット名)を7曜日へ読み替え。
                    _days = _bday0.get(str(_bi))
                    if not _days and _bp0.get(str(_bi)):
                        _days = _OLD_PATTERN_DAYS.get(_bp0[str(_bi)])
                    if _days and len(_days) == 7:
                        for _k, _c in enumerate(DAY_COLS):
                            _row[_c] = bool(_days[_k])
            base_df = pd.DataFrame(_rows0)
            if "ブロック" in base_df.columns:      # 便のまとまりの番号順に並べて表示
                base_df = base_df.sort_values("ブロック").reset_index(drop=True)
            edited = st.data_editor(
                base_df, hide_index=True, use_container_width=True,
                key=f"route_editor_{ss().get('extract_token', '')}",
                column_config={
                    "ブロック": st.column_config.NumberColumn(
                        "便のまとまり", disabled=True,
                        help="時刻表のひとかたまり（PDFなら1ページ分／往復なら片道分）。この単位で路線・方向・行き先を割り当てます。"),
                    "見出し": st.column_config.TextColumn("見出し(参考)", disabled=True),
                    "ファイル": st.column_config.TextColumn(  # 複数ファイル取り込み時のみ表示（列があれば）
                        "元ファイル", disabled=True, help="この便のまとまりが、どのアップロードfile由来か"),
                    "停留所数": st.column_config.NumberColumn("停留所数", disabled=True),
                    "路線名": st.column_config.TextColumn("路線名", help="同じ路線名の便のまとまりが1つの路線にまとまる"),
                    "方向(0/1)": st.column_config.SelectboxColumn(
                        "方向（行き/帰り）", options=["0（行き）", "1（帰り）"], required=True,
                        help="同じ路線の往復を分ける番号です。行き=0／帰り=1（どちらを0にするかは決めでOK）。"
                             "循環路線は0のまま。GTFSデータには 0/1 の数字で出力されます。"),
                    "行き先表示": st.column_config.TextColumn(
                        "行き先表示", help="バス前面に出る行き先。便のまとまりごとに指定できます。"
                        "空なら終点名から『○○方面』。循環は『右回り/左回り』等でもOK。"),
                    "月": st.column_config.CheckboxColumn(
                        "月", help="運行する曜日にチェック。初期値は平日(月〜金)。月水金など任意の組合せもOK"),
                    "火": st.column_config.CheckboxColumn("火"),
                    "水": st.column_config.CheckboxColumn("水"),
                    "木": st.column_config.CheckboxColumn("木"),
                    "金": st.column_config.CheckboxColumn("金"),
                    "土": st.column_config.CheckboxColumn("土"),
                    "日": st.column_config.CheckboxColumn(
                        "日", help="日曜にチェックすると祝日も運行扱い（日祝ダイヤ）になります"),
                },
            )
            st.caption("**運行する曜日**は**月〜日のチェック**で指定（初期値＝平日：月〜金／**月水金**など任意の組合せOK）。"
                       "**日曜にチェック＝祝日も運行**（日祝ダイヤ）扱いです。祝日・年末年始などの運休は"
                       "**下の『運休日』**でまとめて設定します。曜日で時刻が違う時刻表は便のまとまりを分けてください。")
            st.info("💡 **こんな時は（例）**\n\n"
                    "- **往復の路線** → 行き（佐屋→駅）と帰り（駅→佐屋）を **同じ路線名** にし、方向を 0 と 1 に。\n"
                    "- **平日と土日で時刻が違う** → 平日ページは 月〜金、土日ページは 土・日 にチェックを分ける。\n"
                    "- **月水金だけ運行** → 月・水・金だけチェック（祝日を休むなら下の**『祝日は運休』**）。\n"
                    "- **日祝ダイヤ** → **日** にチェック（日曜と祝日に運行）。\n"
                    "- **循環路線** → 方向は 0 のまま、行き先は『右回り／左回り』でもOK。")

            # ── 運休日（②の表のすぐ下に配置）：祝日・年末年始・お盆・個別の運休日をここでまとめて設定 ──
            _htk = ss().get("extract_token", "")
            _hdet = ss().get("detected", {}) or {}
            st.markdown("**運休日（全路線に一律で適用。該当する場合のみチェック）**")
            st.caption("上の表で運行する曜日を決め、ここで**祝日などの運休**を設定します。"
                       "『祝日は運休』は 平日/土曜ダイヤを祝日に休みにします（**日**にチェックした日祝ダイヤはその日も運行）。")
            _h1, _h2, _h3 = st.columns(3)
            hol_syuku = _h1.checkbox("祝日は運休", value=bool(_hdet.get("holiday_syukujitsu")), key=f"hs_{_htk}",
                                     help="内閣府の祝日データ（同梱・〜2027年）で祝日を運休に展開")
            hol_nenmatsu = _h2.checkbox("年末年始運休", value=bool(_hdet.get("holiday_nenmatsu")), key=f"hn_{_htk}",
                                        help="年末年始を運休に展開（下で期間を変えられます）")
            hol_obon = _h3.checkbox("お盆運休", value=bool(_hdet.get("holiday_obon")), key=f"ho_{_htk}",
                                    help="お盆を運休に展開（下で期間を変えられます）")
            # 年末年始・お盆は「どこまでか」を可変に（既定＝12/29〜1/3、8/13〜8/15）。MM-DD で入力。
            nenmatsu_range, obon_range = "12-29:01-03", "08-13:08-15"
            if hol_nenmatsu:
                _n1, _n2 = st.columns(2)
                _ns = _n1.text_input("年末年始 開始 (MM-DD)", value="12-29", key=f"nns_{_htk}")
                _ne = _n2.text_input("年末年始 終了 (MM-DD)", value="01-03", key=f"nne_{_htk}",
                                     help="年をまたぐ場合も、開始＞終了の表記でOK（例 12-29〜01-03）")
                nenmatsu_range = f"{_ns.strip() or '12-29'}:{_ne.strip() or '01-03'}"
            if hol_obon:
                _o1, _o2 = st.columns(2)
                _os = _o1.text_input("お盆 開始 (MM-DD)", value="08-13", key=f"obs_{_htk}")
                _oe = _o2.text_input("お盆 終了 (MM-DD)", value="08-15", key=f"obe_{_htk}")
                obon_range = f"{_os.strip() or '08-13'}:{_oe.strip() or '08-15'}"
            st.caption("個別の運行日・運休日（臨時運休・特別運行がある日。無ければ空でOK）。"
                       "**対象ダイヤ**で、その日を『どの運行日（②の曜日）に効かせるか』を選べます"
                       "（『全ダイヤ』＝全部。特別運行はどのダイヤの便を動かすか指定できます）。")
            _svc_opts = ["全ダイヤ"] + list(_service_labels_map())   # 全ダイヤ ＋ ②の各ダイヤ名
            _cd_base = pd.DataFrame({"日付": pd.Series([], dtype="datetime64[ns]"),
                                     "種別": pd.Series([], dtype="object"),
                                     "対象ダイヤ": pd.Series([], dtype="object")})
            cd_editor = st.data_editor(
                _cd_base, num_rows="dynamic", key=f"cd_{_htk}", use_container_width=True,
                column_config={
                    "日付": st.column_config.DateColumn("日付", format="YYYY-MM-DD"),
                    "種別": st.column_config.SelectboxColumn("種別", options=["運休", "臨時運行"],
                                                             default="運休", required=True),
                    "対象ダイヤ": st.column_config.SelectboxColumn(
                        "対象ダイヤ", options=_svc_opts, default="全ダイヤ",
                        help="この日を効かせる運行日（②の曜日）。全ダイヤ＝すべてのダイヤに適用")})
            cd_use_period = st.checkbox("期間で運休", value=False, key=f"cdp_{_htk}",
                                        help="連続した期間をまるごと運休に（工事・季節運休など）。"
                                             "チェックすると開始・終了の入力欄が出ます。")
            cd_ps, cd_pe = "", ""
            if cd_use_period:
                _cdp2, _cdp3 = st.columns(2)
                cd_ps = _cdp2.text_input("運休期間 開始(YYYYMMDD)", value="", key=f"cdps_{_htk}",
                                         help="西暦8桁（例 20260901）")
                cd_pe = _cdp3.text_input("運休期間 終了(YYYYMMDD)", value="", key=f"cdpe_{_htk}",
                                         help="西暦8桁（例 20260907）")

            # 割り当て表から decision_spec を構築（同じ路線名のブロックを1路線にまとめる）
            name_blocks, block_dir, headsign, block_days = {}, {}, {}, {}
            for _, r in edited.iterrows():
                bi = int(r["ブロック"]); nm = str(r["路線名"]).strip() or f"路線{bi}"
                name_blocks.setdefault(nm, []).append(bi)
                # 表示は「0（行き）/1（帰り）」だが、生成データは 0/1 の数字に戻す。
                block_dir[str(bi)] = 0 if str(r["方向(0/1)"]).strip().startswith("0") else 1
                # 運行する曜日: 7チェック(月〜日)を [0/1]×7 で保存。1つも選ばれてなければ既定(平日)。
                _days = [1 if bool(r.get(_c, False)) else 0 for _c in DAY_COLS]
                if not any(_days):
                    _days = [int(x) for x in DAY_DEFAULT]
                block_days[str(bi)] = _days
                # 行き先表示: 表で編集された値を優先。空なら方向見出し(direction_hint)を使う。
                _head = str(r.get("行き先表示") or "").strip()
                if _head:
                    headsign[str(bi)] = _head
                elif blocks_e[bi].get("direction_hint"):
                    headsign[str(bi)] = blocks_e[bi]["direction_hint"]
            routes = [{"route_id": f"R{i + 1:02d}", "route_long_name": nm, "blocks": bidx, "circular": False}
                      for i, (nm, bidx) in enumerate(name_blocks.items())]
            ss().decision_spec = {"routes": routes, "block_direction": block_dir, "block_headsign": headsign,
                                  "block_days": block_days,
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
            auto = [f"便・停留所: 便のまとまり {len(blocks0)} 組 / 便 計 {total0}（停留所順は上に表示）"]
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
            st.info("PDF/Excel に無いので③で質問します: 路線名 / 事業者名・法人番号・URL・電話 / "
                    "運賃 / 有効期間 / 対象自治体（座標補完用）　※運行する曜日は②の『運行日』で設定")

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
                st.markdown("<span style='color:#16202B;font-weight:700'>✏️ 時刻・停留所名のセルはクリック"
                            "（ダブルクリック）で編集できます。</span>", unsafe_allow_html=True)
                st.caption("抽出した**全時刻**です。原典（紙やPDF）と見比べて、違うセルを直接直してください。"
                           "空欄＝通過。**停留所名も直接編集**でき、**行を選んで削除**もできます"
                           "（「待機時間」「○○出発」など停留所でない行を消す／表記を直す）。"
                           + (f"OCR誤読の疑い **{n_an}件** は各表の下に列挙しています。" if n_an else "")
                           + "直したら**修正欄で Enter**（または『この時刻表で確定して反映』ボタン）で反映します"
                             "（自動では書き換えません）。")
                st.markdown("<span style='color:#c62828;font-weight:700'>⚠ 時刻は必ず原典と1つずつ見比べて"
                            "確認してください。「📄 原典を別タブで開く」で並べて照合できます。"
                            "</span>", unsafe_allow_html=True)
                # ⏰の見出しは②で入力した路線名で表示する（利用者は block 番号が分からないため）
                _ds = ss().get("decision_spec", {}) or {}
                _bi2name = {}
                for _r in _ds.get("routes", []):
                    for _b in _r.get("blocks", []):
                        _bi2name[int(_b)] = _r.get("route_long_name", "")
                _bdir = _ds.get("block_direction", {})
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
                    _rname = _bi2name.get(bi, "") or f"便のまとまり {bi}"
                    _dv = _bdir.get(str(bi))
                    _dtag = "（行き）" if _dv == 0 else ("（帰り）" if _dv == 1 else (f"（{dh}）" if dh else ""))
                    st.markdown(f"**{_rname}**{_dtag}")
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
                    rev_cells = []      # 赤セルの位置(i, 列ラベル, 停留所名, 時刻) 直接修正用
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
                                    rev_cells.append((c["i"], labels[j], c["name"], c["time"][:5]))
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
                            with st.expander("色付きで表示（🔴逆行=要修正／🔵日跨ぎ=翌日）", expanded=bool(rev)):
                                st.dataframe(ed.style.apply(lambda _x: css, axis=None),
                                             hide_index=True, use_container_width=True)
                                if rev_cells:
                                    st.markdown("**🔴 逆行セルをここで直す**"
                                                "（正しい時刻を入れると上書き。空欄なら上の表のまま）")
                                    for (ci, lab_raw, nm, tm) in rev_cells:
                                        cA, cB = st.columns([3, 2])
                                        cA.markdown(f"便 **{lab_raw.split('#')[0]}**｜{nm}"
                                                    f"　<span style='color:#c62828'>現在 {tm}</span>",
                                                    unsafe_allow_html=True)
                                        cB.text_input("正しい時刻", value="", placeholder=tm,
                                                      key=f"fix_{tok}_{bi}_{ci}_{lab_raw}",
                                                      label_visibility="collapsed",
                                                      on_change=lambda: ss().__setitem__("_tt_apply_req", True))
                                    st.caption("例）7:30 と入力し **Enter** で確定・反映（下のボタンでもOK）。"
                                               "停留所（行）ごと消したい待機時間は上の表の左端で行を削除してください。")
                    else:
                        st.caption("✅ 逆行なし・全セルが妥当な時刻です。")
                if issue_tot["rev"] or issue_tot["inval"] or issue_tot["an"]:
                    st.info("反映前チェック: " + "　".join(filter(None, [
                        f"🔴 逆行 {issue_tot['rev']}件" if issue_tot["rev"] else "",
                        f"⚠ 非時刻 {issue_tot['inval']}件" if issue_tot["inval"] else "",
                        f"🟠 OCR疑い {issue_tot['an']}件" if issue_tot["an"] else ""]))
                        + " が残っています。直してから反映するのがおすすめです（このまま反映も可）。")
                # 修正欄で Enter を押すと on_change で _tt_apply_req が立つ → ボタンと同じ反映を実行。
                _apply_req = ss().pop("_tt_apply_req", False)
                if st.button("この時刻表で確定して反映", type="primary") or _apply_req:
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
                                # 赤セル修正欄に入力があれば優先（空欄なら表の値）
                                _fix = str(ss().get(f"fix_{tok}_{bi}_{i}_{lab}", "") or "").strip()
                                val = _fix if _fix else str(ed.iloc[i][lab]).strip()
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

if _show_q:
    with tab_q:
        # =====================================================================
        # Step 3: PDF/Excelに無い項目だけを後から質問（自動確認の後）
        # =====================================================================
        if ss().get("decision_spec"):
            st.header("③ PDF/Excel に無い項目を入力（不足分の質問）")
            st.caption("上の②でシステムが確認した結果、時刻表に書かれていない項目です。"
                       "推測せず入力してください（不明は空欄でOK＝暫定/要確認として入る。ただし路線名は必須）。"
                       "下の『生成する』を押すと入力が一括で反映されます。")
            st.info("💡 **こんな時は（例）**\n\n"
                    "- **運賃が均一（例：100円）** → 金額を1つ入れるだけ。\n"
                    "- **区間で運賃が違う** → 「均一運賃」のチェックを外し、区間の表に金額を入力。\n"
                    "- **事業者名・法人番号が不明** → 空欄のままで進めてOK（後から直せます／暫定値で出ます）。\n"
                    "- **祝日は運休** → 『祝日は運休』にチェック（日祝ダイヤがあればその日は日祝ダイヤで運行）。\n"
                    "- **有効期間が分からない** → 空欄でも生成できます（提出前に正しい期間を入れてください）。")
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
            st.markdown("**運賃の入力方法**")
            _fcol = st.columns(2)
            uniform_fare = False
            if len(_routes_now) > 1:
                uniform_fare = _fcol[0].checkbox("全路線を同じ運賃にする（一律・おすすめ）", value=True,
                                                 key=f"unifare_{tk}",
                                                 help="ON: 大人/小児/障がい者を1回入れるだけで全路線に適用（路線ごとの入力が不要）。"
                                                      "路線で運賃が違う時だけOFFにして路線ごとに入力。")
            zone_fare = _fcol[1].checkbox("区間運賃にする（停留所・区間ごとに違う）", key=f"zonechk_{tk}",
                                          help="チェックすると③の中に『区間運賃の表（発×着）』が出ます。区間ごとに金額を入れます。")
            with st.form("conditions"):
                # 運賃（入力方法は上の「運賃の入力方法」で選択済み）。金額をここ＝方法選択の直後に入力する。
                # 区分別テーブル（単一路線 or 全路線一律）: 区分を自由に追加・削除でき、区分ごとに金額と支払い方法を入れる。
                fare_cat_df = None
                _use_cat_table = (len(_routes_now) == 1) or (len(_routes_now) > 1 and uniform_fare)
                if _use_cat_table:
                    if len(_routes_now) == 1:
                        st.markdown("**運賃（区分別・円。区分は自由に追加・削除できます）**")
                    else:
                        st.markdown("**運賃（全路線一律・区分別・円。区分は自由に追加・削除できます）**")
                    st.caption("末尾の「＋」で区分を追加、行を選んで削除できます（大人／小児のほか シルバー・学生 なども）。"
                               "**支払い方法**は GTFS標準の payment_method です。"
                               "『車内で支払う』＝乗車中に支払う（**乗車時の前払い・降車時の後払いはどちらもこちら**。"
                               "日本のバスはほぼこれ）／『乗車前に支払う』＝改札・事前購入など乗る前に支払う場合。")
                    st.markdown(":red[**現金とICで金額が違うとき**は、GTFS標準に現金/IC別の欄がないため、"
                                "区分名を分けて（例：大人 ／ 大人(IC)）それぞれの金額を入れてください。]")
                    _fare_base = pd.DataFrame([
                        {"区分": "大人", "金額(円)": int(det.get("fare_adult") or 0), "支払い方法": "車内で支払う（乗車時／降車時）"},
                        {"区分": "小児", "金額(円)": int(det.get("fare_child") or 0), "支払い方法": "車内で支払う（乗車時／降車時）"},
                        {"区分": "障がい者", "金額(円)": int(det.get("fare_disabled") or 0), "支払い方法": "車内で支払う（乗車時／降車時）"},
                    ])
                    fare_cat_df = st.data_editor(
                        _fare_base, hide_index=True, num_rows="dynamic", key=f"farecat_{tk}",
                        use_container_width=True,
                        column_config={
                            "区分": st.column_config.TextColumn(
                                "区分", help="例：大人／小児／障がい者／大人(IC)／シルバー", width="medium"),
                            "金額(円)": st.column_config.NumberColumn("金額(円)", min_value=0, step=10, format="%d"),
                            "支払い方法": st.column_config.SelectboxColumn(
                                "支払い方法",
                                options=["車内で支払う（乗車時／降車時）", "乗車前に支払う（改札・事前購入）"], width="medium"),
                        })
                # 路線別運賃（多路線・一律OFF）は路線ごとに固定区分で入力。
                rfares_in = {}
                if len(_routes_now) > 1 and not uniform_fare:
                    st.markdown("**路線ごとの運賃（円・0は未設定）**")
                    for r in _routes_now:
                        rid = r["route_id"]; rnm = r.get("route_long_name", rid)
                        pcols = st.columns([3, 1, 1, 1])
                        pcols[0].markdown(f"<div style='padding-top:8px'>{rnm}</div>", unsafe_allow_html=True)
                        ra = pcols[1].number_input("大人", min_value=0, value=int(det.get("fare_adult") or 0), step=10, key=f"rfa_{rid}_{tk}")
                        rch = pcols[2].number_input("小児", min_value=0, value=int(det.get("fare_child") or 0), step=10, key=f"rfc_{rid}_{tk}")
                        rdi = pcols[3].number_input("障がい者", min_value=0, value=int(det.get("fare_disabled") or 0), step=10, key=f"rfd_{rid}_{tk}")
                        rfares_in[rid] = (ra, rch, rdi)
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
                    st.caption(f"{len(_stops_all)}停留所。対角（同一停留所）は**通常は空欄でOK**。"
                               "ただし**循環路線で一周して同じ停留所で降りる**場合は、その**対角セルに運賃を入れてください**"
                               "（対角に入力があればその運賃も出力します）。"
                               "**上り・下りで運賃が違う場合は上のチェックを外し、両方向のセルに入力**してください。"
                               "Excelの表をコピー＆貼り付けも可。乗れる区間だけの入力でも構いません。")
                # 運賃の下: 路線名・対象自治体・事業者情報
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
                c3.caption("🚌 **行き先表示は②の割り当て表**で便のまとまりごとに設定できます"
                           "（複数の行き先に対応）。")
                # 運行する曜日は②の『運行日』で便のまとまりごとに決めるため、ここでは入力しない。
                # ②でパターン未割当の便が万一残った場合の予備サービス(SVC)にだけ既定曜日を使う。
                c4, c5 = st.columns(2)
                start = c4.text_input("有効期間 開始 (YYYYMMDD)", value=det.get("start_date", ""), key=f"st_{tk}",
                                      help="このダイヤ(サービス)が有効な期間＝カレンダーの期間です。"
                                           "GTFSでは『feed全体の有効期限』と『各サービスの運行期間』を分けられますが、"
                                           "通常はこの1つでOK（❓用語ヘルプ参照）。")
                end = c5.text_input("有効期間 終了 (YYYYMMDD)", value=det.get("end_date", ""), key=f"en_{tk}")
                # ※運休日（祝日・年末年始・お盆・個別の運休日）は②の表の下へ移動済み。
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
                        sel = st.multiselect(f"便のまとまり{bi}（{dirh or '方向なし'}）の降車専用停留所",
                                             options=names, default=hint, key=f"board_{bi}_{tk}")
                        if sel:
                            board_sel[bi] = sel
                use_nom = st.checkbox(
                    "Nominatim 補完を使う（P11で埋まらなかった停留所だけに使う・POI多い路線向け・遅い）",
                    value=False,
                    help="まず国土数値情報(P11)で座標を埋め、それでも残った停留所だけを OpenStreetMap の"
                         "住所→座標変換(Nominatim)で補います。任意（既定OFF）・処理は遅めです。")
                ai_read_gen = st.checkbox("🔎 生成時にAIで読み(ふりがな)を探索して既定値にする（任意・要確認）",
                                          value=False,
                                          help="生成後、Claudeが停留所名の読みを探索し、自動読み(pykakasi)と違う所だけを"
                                               "既定値に反映します。④で必ず確認してください（AI由来＝要確認・推測を鵜呑みにしない）。")
                ai_gen_key = st.text_input("ANTHROPIC_API_KEY（上のAIチェックを使う時。環境変数があれば空でOK）",
                                           type="password", value="", key=f"aigk_{tk}")
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
                        st.error("路線名が空です。GTFS仕様では路線名が必須で、空のまま生成すると"
                                 "**データ点検（Validator）でエラー**になります。③で路線名を入力してください。")
                    else:
                        st.error("路線名が空の路線があります。②の割り当て表で各路線に名前を付けてください。"
                                 "空のまま生成すると**データ点検（Validator）でエラー**になります。")
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
                period = {"start_date": start or "20250401", "end_date": end or "20271231"}
                # 予備サービスSVCの曜日: ②でパターン未割当の便が残ったときの保険。
                # 運行曜日は②で決めるので、ここは検出/既定値(_days_def=平日)をそのまま使う。
                _dd = _days_def if (isinstance(_days_def, (list, tuple)) and len(_days_def) == 7) else [1, 1, 1, 1, 1, 0, 0]
                form_days = {"mon": int(_dd[0]), "tue": int(_dd[1]), "wed": int(_dd[2]),
                             "thu": int(_dd[3]), "fri": int(_dd[4]), "sat": int(_dd[5]), "sun": int(_dd[6])}
                spec["service"] = {"service_id": "SVC", **form_days, **period}
                # 複数ダイヤ: ②の運行日(block_days=7曜日)から services と block_service を構築。
                # 同じ曜日組合せの便は1サービスを共有。service_idは曜日ビットで作る(例 月水金=SVC_1010100)。
                # 祝日運行は「日曜にチェック(sun=1)＝日祝ダイヤ」で判定（下の運休日設定と連動）。
                bdays = ss()["decision_spec"].get("block_days", {})

                def _svc_from_days(_d):
                    if not (isinstance(_d, (list, tuple)) and len(_d) == 7):
                        _d = [int(form_days[k]) for k in DAY_KEYS]
                    _d = [int(bool(x)) for x in _d]
                    if not any(_d):                       # 曜日未選択の保険＝平日
                        _d = [1, 1, 1, 1, 1, 0, 0]
                    sid = "SVC_" + "".join(str(x) for x in _d)
                    return sid, {"service_id": sid, **{DAY_KEYS[i]: _d[i] for i in range(7)}, **period}

                services = {}
                block_service = {}
                for bi, days in bdays.items():
                    sid, svc = _svc_from_days(days)
                    services.setdefault(sid, svc)
                    block_service[bi] = sid
                if not services:   # 便が無い等の保険（最低1サービスは必要）
                    sid, svc = _svc_from_days(None)
                    services[sid] = svc
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
                    _svc_label2sid = _service_labels_map()   # ダイヤ名 → service_id
                    for _, _r in cd_editor.iterrows():
                        _dv, _kind = _r.get("日付"), _r.get("種別")
                        if pd.isna(_dv) or not _kind:
                            continue
                        _ymd = pd.Timestamp(_dv).strftime("%Y%m%d")
                        # 対象ダイヤ: 「全ダイヤ」or未指定→全service、特定ダイヤ→その1つ（無効なら全）。
                        _tgt = str(_r.get("対象ダイヤ") or "全ダイヤ").strip()
                        if _tgt in ("", "全ダイヤ"):
                            _tsids = _svc_ids
                        else:
                            _one = _svc_label2sid.get(_tgt)
                            _tsids = [_one] if _one in _svc_ids else _svc_ids
                        if _kind == "運休":
                            _add_cd(_ymd, 2, _tsids)
                        elif _kind == "臨時運行":
                            _add_cd(_ymd, 1, _tsids)
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
                # 祝日: 「日曜・祝日／土日祝／毎日」ダイヤは“祝日も運行”がパターンの意味なので、
                # 祝日運休チェックの有無に関わらず、その祝日に運行(1)を追加する（＝一般的な祝日を反映）。
                # 平日/土曜ダイヤを祝日に運休(2)にするのは「祝日は運休」チェックON時だけ（利用者の選択）。
                # run_pipeline の一律運休は複数ダイヤで日祝サービスの日曜まで消すため、ここで決定的に展開。
                # 「日曜にチェックがある便＝祝日も運行(日祝ダイヤ)」とみなす（sun=1 で判定）。
                # 平日/土曜(sun=0)は下の「祝日は運休」がONのとき、その曜日に当たる祝日を運休にする。
                _has_hol_pattern = any(int(s.get("sun", 0)) == 1 for s in spec["services"])
                _syuku_inapp_ok = False
                if hol_syuku or _has_hol_pattern:
                    try:
                        from generate_calendar_dates import load_syukujitsu
                        _holidays = load_syukujitsu(
                            str(SCRIPTS.parent / "references" / "data" / "syukujitsu.csv"))
                        _p0 = datetime.datetime.strptime(period["start_date"], "%Y%m%d").date()
                        _p1 = datetime.datetime.strptime(period["end_date"], "%Y%m%d").date()
                        _daykeys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
                        for _d in _holidays:
                            if not (_p0 <= _d <= _p1):
                                continue
                            _ymd = _d.strftime("%Y%m%d"); _dk = _daykeys[_d.weekday()]
                            for _s in spec["services"]:
                                _sid = _s["service_id"]
                                _runs_hol = int(_s.get("sun", 0)) == 1   # 日曜含む便＝祝日も運行
                                _covers = int(_s.get(_dk, 0)) == 1
                                if _runs_hol and not _covers:
                                    _add_cd(_ymd, 1, [_sid])   # 祝日はこのダイヤで運行（常に）
                                elif (not _runs_hol) and _covers and hol_syuku:
                                    _add_cd(_ymd, 2, [_sid])   # 平日/土曜は祝日運休（チェックON時のみ）
                        _syuku_inapp_ok = hol_syuku   # 一律運休の抑制は祝日運休ON時のみ意味を持つ
                    except Exception:
                        _syuku_inapp_ok = False   # 失敗時は後段の一律運休へフォールバック
                if _cal_dates:
                    spec["calendar_dates"] = _cal_dates
                fare_matrix = []
                if zone_fare and zone_df is not None:
                    for i, orig in enumerate(_stops_all):
                        for dest in _stops_all:
                            # 対角(同じ停留所どうし)も、入力があれば運賃にする＝循環の一周乗車に対応。
                            # 非循環では対角は空欄のままなので、従来どおり運賃は付かない。
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
                # 区分別運賃（単一路線 or 全路線一律）: テーブルから (区分, 金額, 支払い方法) を組む。
                # 区分名で一意化（重複名は最初の1行を採用＝重複 fare_id を出さない＝不正GTFS防止）。
                cat_fares = []
                if fare_cat_df is not None:
                    _PAY = {"車内で支払う（乗車時／降車時）": 0, "乗車前に支払う（改札・事前購入）": 1}
                    _seen_cat = set()
                    for _, _row in fare_cat_df.iterrows():
                        _cat = str(_row.get("区分") or "").strip()
                        try:
                            _pr = int(float(_row.get("金額(円)") or 0))
                        except (ValueError, TypeError):
                            _pr = 0
                        if _cat and _pr > 0 and _cat not in _seen_cat:
                            _seen_cat.add(_cat)
                            cat_fares.append({"category": _cat, "price": _pr,
                                              "payment_method": _PAY.get(str(_row.get("支払い方法") or ""), 0)})
                # 優先順位: 区間運賃 > 路線別運賃 > 区分別（単一/一律）
                if fare_matrix:
                    spec["fare_matrix"] = fare_matrix
                elif route_fares:
                    spec["route_fares"] = route_fares
                elif cat_fares:
                    spec["fares"] = cat_fares
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
                # 祝日をアプリ側で正しく展開できたら後段の一律運休は使わない（二重・誤運休を防ぐ）。
                hol = {"syuku": hol_syuku and not _syuku_inapp_ok,
                       "nenmatsu": hol_nenmatsu, "obon": hol_obon,
                       "nenmatsu_range": nenmatsu_range, "obon_range": obon_range}
                # 路線名以外がほぼ未入力なら、暫定の既定値（捏造なし）で生成してよいか確認してから生成。
                minimal = (not ag_name.strip() and not ag_id.strip() and not ag_url.strip()
                           and not ag_phone.strip() and not ag_official.strip() and not ag_zip.strip()
                           and not ag_addr.strip() and not ag_pres_pos.strip() and not ag_pres_name.strip()
                           and not cat_fares and not route_fares
                           and not (zone_fare and fare_matrix)
                           and not start.strip() and not end.strip()
                           and not (hol_syuku or hol_nenmatsu or hol_obon) and not _cal_dates)
                _aikey = ai_gen_key or os.environ.get("ANTHROPIC_API_KEY", "")
                _aion = bool(ai_read_gen)
                if minimal:
                    ss().pending_gen = {"spec": spec, "muni": muni, "use_nom": bool(use_nom), "hol": hol,
                                        "ai_read": _aion, "ai_key": _aikey}
                    ss().awaiting_confirm = True
                    st.rerun()
                else:
                    run_generation(spec, muni, bool(use_nom), hol,
                                   ai_read=_aion, ai_key=_aikey, ai_ctx=muni)

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
                    run_generation(sp, pg.get("muni", "福岡県"), pg.get("use_nom", False), pg.get("hol", {}),
                                   ai_read=pg.get("ai_read", False), ai_key=pg.get("ai_key", ""),
                                   ai_ctx=pg.get("muni", ""))
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
                items.append(("block", False, "データ点検（公式Validator）のエラーが0",
                              "点検が未実行です（Java/点検ツール未設定の可能性）→ 点検を実行してください"))
            else:
                n_err = 0
                try:
                    n_err = sum(n.get("totalNotices", 0)
                                for n in json.loads(rep.read_text(encoding="utf-8")).get("notices", [])
                                if n.get("severity") == "ERROR")
                except Exception:
                    pass
                items.append(("block", n_err == 0, "データ点検（公式Validator）のエラーが0",
                              "問題なし" if n_err == 0 else f"エラー {n_err}件 → ④下部の内容と対処を確認"))
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


if _show_coord:
    with tab_coord:
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
                    st.metric("データ点検（Validator）のエラー", errs,
                              help="GTFSが標準仕様に合っているかを自動点検した結果。0 なら仕様上の重大エラーなし。"
                                   "MobilityData の公式点検ツール（Validator）を使っています。")
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
                        st.error("データ点検（Validator）のエラー内容（このままでは公式提出に不適）:\n\n" + "\n\n".join(lines))
                    else:
                        st.success("データ点検（Validator）のエラーは 0 件です。")
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
                _aiap = ss().get("ai_applied") or {}   # 生成時にAIが探索して既定化した読み（要確認）
                _n_susp = sum(1 for _nm in _order if _reading_suspicious(_cur[_nm].get("ja-Hrkt", "")))
                _hdr = (f' / ⚠要確認 {_n_susp}件' if _n_susp else '') + (f' / 🔎AI {len(_aiap)}件' if _aiap else '')
                with st.expander(f"🈁 ふりがな・英語・停留所名の確認・修正"
                                 f"（難読地名の誤読をここで直す{_hdr}）",
                                 expanded=bool(_n_susp) or bool(_aiap) or bool(ss().get("ai_readings"))):
                    st.caption("✏️ **読み・停留所名のセルはクリック（ダブルクリック）で編集**できます。"
                               "読みは辞書付き解析（SudachiPy）で自動生成します（半角カナはNFKCで正規化、"
                               "全国共通の難読は辞書で補正）。それでも難読地名は誤読が残ることがあります"
                               "（例: 相島「あいじま」→正しくは「あいのしま」）。"
                               "**⚠印**の行（漢字が残る等）は特に確認を。**停留所名そのものも直せます**"
                               "（OCR誤りの修正など）。名前を変えると読みは自動で作り直します"
                               "（読み欄も直せばそちらが優先）。GTFS-JP の必須項目です。")
                    if _aiap:
                        st.info(f"🔎 **AIが探索して既定にした読み {len(_aiap)}件**（表の🔎AI印）。"
                                "AI由来なので**原典で確認**してください。下の表で 元の自動読み→AIの読み・確度を確認できます。")
                        _aidf = [{"停留所名": _k, "元の自動読み": _v.get("before", ""),
                                  "AIが入れた読み": _v.get("yomi", ""), "確度": _v.get("confidence", ""),
                                  "根拠(AI)": _v.get("note", "")} for _k, _v in _aiap.items()]
                        st.dataframe(pd.DataFrame(_aidf), hide_index=True, use_container_width=True)
                    _rows = []
                    for _nm in _order:
                        _h = _cur[_nm].get("ja-Hrkt", "")
                        _mk = "⚠" if _reading_suspicious(_h) else ""
                        if _nm in _aiap:
                            _mk = (_mk + " 🔎AI").strip()
                        _row = {"要確認": _mk, "停留所名": _nm, "ふりがな(ja-Hrkt)": _h,
                                "英語(en)": _cur[_nm].get("en", "")}   # 英語は常に編集可（空でOK）
                        _rows.append(_row)
                    with st.form("readings_form"):
                        _cfg = {
                            "要確認": st.column_config.TextColumn("⚠", disabled=True, width="small",
                                                                  help="漢字が残る等、読みが怪しい行の目印"),
                            "停留所名": st.column_config.TextColumn(
                                "停留所名", help="停留所名そのものを直せます（OCR誤りの修正など）。"
                                "変更すると stops.txt と読みが更新されます。"),
                            "ふりがな(ja-Hrkt)": st.column_config.TextColumn("ふりがな(ja-Hrkt)"),
                            "英語(en)": st.column_config.TextColumn(
                                "英語(en)", help="英語名（任意）。空でもOK。入れると translations に en として出力されます。"),
                        }
                        _edited = st.data_editor(pd.DataFrame(_rows), hide_index=True,
                                                 key="readings_editor", column_config=_cfg,
                                                 use_container_width=True)
                        if st.form_submit_button("この内容で反映（zip・地図を更新）"):
                            _renames, _by = {}, {}   # old→new名 / new名→{ja-Hrkt?,en?}
                            for _i, _nm in enumerate(_order):
                                _new = str(_edited.iloc[_i]["停留所名"]).strip()
                                _nh = str(_edited.iloc[_i]["ふりがな(ja-Hrkt)"]).strip()
                                _ne = str(_edited.iloc[_i].get("英語(en)", "")).strip()
                                _read_edited = bool(_nh) and _nh != _cur[_nm].get("ja-Hrkt", "")
                                _en_edited = bool(_ne) and _ne != _cur[_nm].get("en", "")
                                _key = _new or _nm
                                if _new and _new != _nm:
                                    _renames[_nm] = _new
                                    if not _read_edited:   # 読み未編集なら新名から自動再計算
                                        _auto = _auto_reading(_new)
                                        if _auto:
                                            _by.setdefault(_key, {})["ja-Hrkt"] = _auto
                                if _read_edited:
                                    _by.setdefault(_key, {})["ja-Hrkt"] = _nh
                                if _en_edited:
                                    _by.setdefault(_key, {})["en"] = _ne
                            if not _renames and not _by:
                                st.info("変更がありませんでした。")
                            else:
                                # (1) 停留所名の変更を stops.txt / translations.txt(field_value) に反映
                                if _renames:
                                    _stops_p = out / "gtfs" / "stops.txt"
                                    _rewrite_csv_field(_stops_p, "stop_name", _renames)
                                    _rewrite_csv_field(_trans, "field_value", _renames,
                                                       only_table="stops")
                                # (2) 読み・英語の手動/自動値を translations.txt に上書き（新名キー）
                                if _by:
                                    _mr = WORK / "manual_readings.json"
                                    _mr.write_text(json.dumps({"by_stop_name": _by}, ensure_ascii=False,
                                                              indent=2), encoding="utf-8")
                                    run([SCRIPTS / "apply_manual_readings.py", _trans, "--readings", _mr])
                                # (3) zip 再梱包 & 地図/ビューア再生成（名前変更を反映）
                                _zz = list(out.glob("*_gtfs-jp.zip"))
                                if _zz:
                                    run([SCRIPTS / "package_gtfs_zip.py", out / "gtfs", "-o", _zz[0]])
                                if _renames:
                                    run([SCRIPTS / "make_map_view.py", out / "gtfs" / "stops.txt",
                                         "--out", out / "map_view.html", "--title", "app_feed"])
                                    run([SCRIPTS / "make_gtfs_viewer.py", "--feed", out / "gtfs",
                                         "-o", out / "gtfs_viewer.html"])
                                ss()["_out_dirty"] = True
                                st.success(
                                    f"反映しました（停留所名 {len(_renames)}件 / 読み・英語 {len(_by)}件）。"
                                    "GTFS-JP(zip)と地図を更新しました。下のボタンで再ダウンロードしてください。")
                                st.rerun()

                    # ---- 路線名・事業者名・行き先表示 の 読み(ja-Hrkt)・英語(en) ----
                    _TBL_LABEL = {"routes": "路線", "agency": "事業者", "trips": "行き先"}
                    _FLD_LABEL = {"route_long_name": "路線名", "route_short_name": "路線略称",
                                  "agency_name": "事業者名", "trip_headsign": "行き先表示"}
                    _other, _oorder = {}, []
                    for _r in _trows:
                        _tb = (_r.get("table_name") or "").strip()
                        if _tb not in _TBL_LABEL:
                            continue
                        _kk = (_tb, (_r.get("field_name") or "").strip(), (_r.get("field_value") or "").strip())
                        _lg = (_r.get("language") or "").strip()
                        if _kk[2] and _kk not in _other:
                            _other[_kk] = {}; _oorder.append(_kk)
                        if _lg in ("ja-Hrkt", "en"):
                            _other[_kk][_lg] = _r.get("translation", "")
                    if _oorder:
                        st.markdown("---")
                        st.markdown("**路線名・事業者名・行き先表示の 読み・英語**")
                        st.caption("停留所以外（路線・事業者・行き先）の**読み(ja-Hrkt)と英語**もここで直せます。"
                                   "読みは辞書解析(SudachiPy)で自動生成。**名前そのものは②③で**直してください。")
                        _orows = [{"種類": _TBL_LABEL.get(_k[0], _k[0]), "項目": _FLD_LABEL.get(_k[1], _k[1]),
                                   "名前": _k[2], "ふりがな(ja-Hrkt)": _other[_k].get("ja-Hrkt", ""),
                                   "英語(en)": _other[_k].get("en", "")} for _k in _oorder]
                        with st.form("readings_other_form"):
                            _oed = st.data_editor(
                                pd.DataFrame(_orows), hide_index=True, key="readings_other_editor",
                                use_container_width=True,
                                column_config={
                                    "種類": st.column_config.TextColumn("種類", disabled=True, width="small"),
                                    "項目": st.column_config.TextColumn("項目", disabled=True, width="small"),
                                    "名前": st.column_config.TextColumn("名前", disabled=True),
                                    "ふりがな(ja-Hrkt)": st.column_config.TextColumn("ふりがな(ja-Hrkt)"),
                                    "英語(en)": st.column_config.TextColumn(
                                        "英語(en)", help="英語名（任意・空でOK）")})
                            if st.form_submit_button("この内容で反映（読み・英語）"):
                                _updates = []
                                for _i, _k in enumerate(_oorder):
                                    _nh = str(_oed.iloc[_i]["ふりがな(ja-Hrkt)"]).strip()
                                    _ne = str(_oed.iloc[_i]["英語(en)"]).strip()
                                    _chg = {}
                                    if _nh != (_other[_k].get("ja-Hrkt", "") or ""):
                                        _chg["ja-Hrkt"] = _nh
                                    if _ne != (_other[_k].get("en", "") or ""):
                                        _chg["en"] = _ne
                                    if _chg:
                                        _updates.append((_k[0], _k[1], _k[2], _chg))
                                if not _updates:
                                    st.info("変更がありませんでした。")
                                else:
                                    _update_translations_rows(_trans, _updates)
                                    _zz2 = list(out.glob("*_gtfs-jp.zip"))
                                    if _zz2:
                                        run([SCRIPTS / "package_gtfs_zip.py", out / "gtfs", "-o", _zz2[0]])
                                    run([SCRIPTS / "make_gtfs_viewer.py", "--feed", out / "gtfs",
                                         "-o", out / "gtfs_viewer.html"])
                                    ss()["_out_dirty"] = True
                                    st.success(f"路線・事業者・行き先の読み・英語を {len(_updates)}件 反映しました。")
                                    st.rerun()

                    # ---- 🔎 AIで読みをチェック（任意・要確認）----
                    # 難読地名は自動読み(pykakasi)が“静かに”誤ることがある(⚠も付かない)。
                    # Claude に読み候補を尋ね、自動読みと食い違う所を洗い出す＝第二の意見。
                    # 候補は必ず人が原典で確認してから採用する（推測を鵜呑みにしない＝正しく失敗）。
                    st.markdown("---")
                    st.markdown("**🔎 AIで読みをチェック（任意・要確認）**")
                    st.caption("Claude に読みの候補を尋ね、**自動読みと食い違う所**を洗い出します（難読地名の"
                               "静かな誤読対策）。**AIの候補も必ず原典で確認**してから採用してください。")
                    _ak = st.text_input("ANTHROPIC_API_KEY（環境変数があれば空でOK）", type="password",
                                        value="", key="ai_read_key")
                    _akey = _ak or os.environ.get("ANTHROPIC_API_KEY", "")
                    st.text_input("地域（文脈・任意。読みの曖昧さを減らす）", key="ai_read_ctx",
                                  placeholder="例: 福岡県古賀市")
                    if st.button("AIに読みを提案させる", key="ai_read_btn"):
                        if not _akey:
                            st.warning("APIキーが未設定です。環境変数 ANTHROPIC_API_KEY を設定するか入力してください。")
                        else:
                            try:
                                with st.spinner("Claude に読みを問い合わせ中..."):
                                    ss()["ai_readings"] = claude_structure.suggest_readings(
                                        _order, _akey, context=ss().get("ai_read_ctx", ""))
                                st.rerun()   # 取得結果を、展開を保ったまま表示する
                            except Exception as _e:
                                st.error(f"読み候補の取得に失敗しました: {_e}")
                    _air = ss().get("ai_readings") or {}
                    if _air:
                        _cmp = []
                        for _nm in _order:
                            _cur_y = _cur[_nm].get("ja-Hrkt", "")
                            _sug = _air.get(_nm) or {}
                            _ay = (_sug.get("yomi") or "").strip()
                            _diff = bool(_ay) and _ay != _cur_y
                            _cmp.append({"停留所名": _nm, "現在の読み": _cur_y, "AI候補": _ay,
                                         "判定": "" if not _ay else ("○ 一致" if not _diff else "✗ 違う"),
                                         "確度": _sug.get("confidence", ""), "根拠(AI)": _sug.get("note", "")})
                        _mis = [r for r in _cmp if str(r["判定"]).startswith("✗")]
                        st.caption(f"AIと自動読みが**食い違う停留所：{len(_mis)}件**（ここが確認の要。"
                                   "低確度は特に慎重に）。")
                        st.dataframe(pd.DataFrame(_cmp), hide_index=True, use_container_width=True)
                        if _mis:
                            _sel = st.multiselect("AI候補を採用する停留所（原典で確認して選ぶ）",
                                                  [r["停留所名"] for r in _mis], default=[], key="ai_read_sel")
                            if st.button("選んだAI候補を反映（zip更新）", key="ai_read_apply"):
                                if not _sel:
                                    st.info("採用する停留所を選んでください。")
                                else:
                                    _byai = {}
                                    for _nm in _sel:
                                        _sug = _air.get(_nm) or {}
                                        _spec = {}
                                        if (_sug.get("yomi") or "").strip():
                                            _spec["ja-Hrkt"] = _sug["yomi"].strip()
                                        if _has_en and (_sug.get("romaji") or "").strip():
                                            _spec["en"] = _sug["romaji"].strip()
                                        if _spec:
                                            _byai[_nm] = _spec
                                    _mr = WORK / "manual_readings.json"
                                    _mr.write_text(json.dumps({"by_stop_name": _byai}, ensure_ascii=False,
                                                              indent=2), encoding="utf-8")
                                    run([SCRIPTS / "apply_manual_readings.py", _trans, "--readings", _mr])
                                    _zz = list(out.glob("*_gtfs-jp.zip"))
                                    if _zz:
                                        run([SCRIPTS / "package_gtfs_zip.py", out / "gtfs", "-o", _zz[0]])
                                    ss()["_out_dirty"] = True
                                    st.success(f"{len(_byai)}件のAI候補を反映しました（要確認）。zipを更新しました。")
                                    st.rerun()
            # zip ダウンロード（完成物の主ボタンは下の⑥ビューア直下。ここは修正後の再取得用）
            zips = list(out.glob("*_gtfs-jp.zip"))
            if zips:
                st.caption("💡 完成した **GTFS-JP 一式（zip）** のダウンロードは、"
                           "内容を確認できる **下の『⑥ ビューア』の直下に大きなボタン** があります。"
                           "（読み・停留所名を直した後は、ここでも最新版を取得できます↓）")
                st.download_button("GTFS-JP (zip) をダウンロード", zips[0].read_bytes(), zips[0].name,
                                   key="dl_zip_step4")
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

                # ── 行き↔帰りの反転（地図で向きが逆と分かった便のまとまりを反転＋その場で再生成）──
                with st.expander("↔ 行き／帰りが逆のとき反転する（停留所の順・時刻を逆にして再生成）"):
                    st.caption("地図で行き・帰りが逆（終点→始点で読まれている等）と分かった便のまとまりを選んで反転すると、"
                               "**停留所の並びと時刻を逆順**にして、その場で**全体を再生成**します。"
                               "行先(方面)は終点から自動で付け直され、確定済みの座標はそのまま保たれます。")
                    _rvb = {b.get("block_index"): b for b in ss().get("extract", {}).get("blocks", [])}
                    if _rvb:
                        _rvbi = st.selectbox(
                            "反転する便のまとまり", list(_rvb), key="rev5sel",
                            format_func=lambda bi: f"便のまとまり {bi}"
                            + (f"（{_rvb[bi].get('direction_hint')}）" if _rvb[bi].get('direction_hint') else ""))
                        _rvnm = [s.get("name") for s in _rvb.get(_rvbi, {}).get("stops", [])]
                        if _rvnm:
                            st.caption("現在の順： " + "  →  ".join(_rvnm[:6]) + (" …" if len(_rvnm) > 6 else ""))
                        if st.button("↔ この便のまとまりを反転して再生成する", key="rev5btn", type="primary"):
                            if not ((WORK / "spec.json").exists() and (WORK / "config.json").exists()):
                                st.error("再生成に必要なファイル(spec.json / config.json)が見つかりません。"
                                         "③から一度生成し直してから使ってください。")
                            else:
                                _tb = next((b for b in ss().extract.get("blocks", [])
                                            if b.get("block_index") == _rvbi), None)
                                if _tb is not None:
                                    _tb["stops"] = list(reversed(_tb.get("stops", [])))
                                    for _t in _tb.get("trips", []):
                                        _t["cells"] = list(reversed(_t.get("cells", [])))
                                        for _i, _c in enumerate(_t["cells"], 1):
                                            _c["seq"] = _i
                                    (WORK / "extract.json").write_text(
                                        json.dumps(ss().extract, ensure_ascii=False, indent=2), encoding="utf-8")
                                    with st.spinner("反転して再生成中…（構造化→生成→座標補完→検証）"):
                                        rc, so, se = run([APPLY_DECISIONS, "--extract", WORK / "extract.json",
                                                          "--decisions", WORK / "spec.json",
                                                          "--out", WORK / "structured.json"])
                                        if rc == 0:
                                            rc, so, se = run([SCRIPTS / "run_pipeline.py",
                                                              "--config", WORK / "config.json"], cwd=REPO)
                                    ss().result = {"rc": rc, "log": se}
                                    ss()["_out_dirty"] = True
                                    ss().pop("anomalies_token", None)
                                    if rc == 0:
                                        st.success(f"便のまとまり {_rvbi} を反転して再生成しました。地図で確認してください。")
                                    else:
                                        st.error("再生成でエラーが出ました。\n" + (se or "")[-800:])
                                    st.rerun()

                import csv as _csv
                crows = list(_csv.DictReader(conf_csv.open(encoding="utf-8-sig")))
                # ★行き/帰りを反対側へ自動推定配置した停留所は、必ず確認してもらう（推定なので）
                _est_names = [r["stop_name"] for r in crows if "反対側へ自動配置" in (r.get("reason") or "")]
                if _est_names:
                    st.warning("⚠ 反対側へ**自動配置（推定）**した停留所（地図で確認してください）： "
                               + "、".join(_est_names[:20]) + ("　…ほか" if len(_est_names) > 20 else ""))
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
                _cbadge = {"確定": "#2e7d32", "要確認": "#e08a1e", "未補完": "#c62828"}
                from html import escape as _esc
                for tip, la, lo, conf, reason in pts:
                    # ポップアップは幅を明示（既定だと日本語の長文が1文字ずつ改行されて読めない）。
                    _pop_html = (
                        "<div style='width:230px;white-space:normal;font-size:12px;line-height:1.55;"
                        "word-break:break-word'>"
                        f"<div style='font-weight:700;margin-bottom:3px'>{_esc(tip)}</div>"
                        f"<span style='color:{_cbadge.get(conf, '#555')};font-weight:700'>{_esc(conf)}</span>"
                        + (f"<div style='color:#333;margin-top:2px'>{_esc(reason)}</div>" if reason else "")
                        + "</div>")
                    folium.Marker([la, lo], tooltip=tip, draggable=True,
                                  icon=folium.Icon(color=col.get(conf, "gray")),
                                  popup=folium.Popup(_pop_html, max_width=260)).add_to(fmap)
                st.caption("📍 ピンを**ドラッグ**して正しい位置へ動かし、そのピンを**クリック**すると、"
                           "下に『この位置で確定』ボタンが出ます（地図の空き場所クリックで座標を拾うこともできます）。")
                state = st_folium(fmap, width=900, height=460, key="confmap",
                                  returned_objects=["last_clicked", "last_object_clicked",
                                                    "last_object_clicked_tooltip"])
                clicked = state.get("last_clicked") if state else None
                obj = state.get("last_object_clicked") if state else None
                obj_tip = state.get("last_object_clicked_tooltip") if state else None
                # クリック/ドラッグされたピンの停留所と、その地図上の現在位置（ドラッグ後の座標）
                obj_sid = tip2id.get(obj_tip) if (obj and obj_tip) else None
                obj_pos = (round(obj["lat"], 6), round(obj["lng"], 6)) if obj else None
                if clicked:
                    st.info(f"地図クリック位置: {clicked['lat']:.6f}, {clicked['lng']:.6f}"
                            "（下で停留所を選び『地図クリック位置で確定』）")

                todo = [r for r in crows if eff_conf(r) != "確定"]
                if todo:
                    st.subheader(f"要確認・未補完を確定する（残り {len(todo)} 件）")
                    _todo_ids = [_sid(r) for r in todo]
                    # 選択肢は未確定の停留所。確定済みのピンを「ドラッグして動かした」ときだけ、
                    # 再調整できるよう一時的に加える（ただ確定しただけなら加えず、次の未確定へ自動で進む）。
                    _readjust = (obj_sid in confirmed and obj_pos is not None
                                 and (abs(confirmed[obj_sid][0] - obj_pos[0]) > 1e-6
                                      or abs(confirmed[obj_sid][1] - obj_pos[1]) > 1e-6))
                    _opts = list(_todo_ids)
                    if _readjust and obj_sid not in _opts:
                        _opts.append(obj_sid)
                    # 地図の点をクリック（未確定）／確定済みをドラッグ したら、その停留所を一覧で自動選択する
                    if obj_sid in _opts:
                        ss()["conf_sel"] = obj_sid
                    if ss().get("conf_sel") not in _opts:
                        ss()["conf_sel"] = _opts[0]
                    sel = st.selectbox(
                        "停留所（地図の点をクリックでも選べます）", _opts, key="conf_sel",
                        format_func=lambda s: (next((_label(r) for r in crows if _sid(r) == s), s)
                                               + ("（確定済み）" if s in confirmed else "")))
                    cur = next((r for r in crows if _sid(r) == sel), {})
                    _cc = confirmed.get(sel)
                    if _cc:
                        _sla, _slo = _cc[0], _cc[1]
                    elif (cur.get("stop_lat") or "").strip():
                        _sla, _slo = float(cur["stop_lat"]), float(cur["stop_lon"])
                    else:
                        _sla = _slo = None
                    # 選択中の停留所のピンをドラッグしたら、その位置を採用（座標表示・確定ボタンを1か所に集約）
                    _dragged = (obj_sid == sel) and obj_pos is not None and (
                        _sla is None or abs(_sla - obj_pos[0]) > 1e-6 or abs(_slo - obj_pos[1]) > 1e-6)
                    _nla, _nlo = (obj_pos if _dragged else (_sla, _slo))
                    _ctag = "（確定済み）" if _cc else ""
                    if _nla is None:
                        st.write(f"**{_label(cur)}**{_ctag} の座標: まだありません"
                                 "（地図クリックかピンのドラッグで決めてください）")
                    else:
                        _mv = "　🟢 ドラッグで移動中（この位置で確定できます）" if _dragged else ""
                        st.write(f"**{_label(cur)}**{_ctag} の座標: 緯度 {_nla:.6f} ／ 経度 {_nlo:.6f}{_mv}")
                    st.caption("この停留所の**ピンを地図でドラッグ**すると位置が変わります。"
                               "『この位置で確定』で確定します（動かさなければ今の位置のまま確定）。"
                               "地図の空き場所をクリックした座標も使えます。")
                    # 同じ場所（終点・敷地内）: 同名で反対方向の停留所と同座標にする
                    _sibs = [r for r in crows if r["stop_name"] == cur.get("stop_name") and _sid(r) != sel]
                    for _sb in _sibs[:1]:
                        _sbid = _sid(_sb)
                        _sbc = confirmed.get(_sbid) or ((float(_sb["stop_lat"]), float(_sb["stop_lon"]))
                                                        if (_sb.get("stop_lat") or "").strip() else None)
                        if _sbc and st.button(f"『{_label(_sb)}』と同じ場所にする（敷地内・終点向け）"):
                            confirmed[sel] = (round(_sbc[0], 6), round(_sbc[1], 6)); st.rerun()
                    b1, b2 = st.columns(2)
                    if b1.button("この位置で確定", type="primary", disabled=(_nla is None)):
                        confirmed[sel] = (round(_nla, 6), round(_nlo, 6)); st.rerun()
                    if b2.button("📍 地図クリック位置で確定", disabled=not clicked):
                        confirmed[sel] = (round(clicked["lat"], 6), round(clicked["lng"], 6)); st.rerun()
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
                        ss()["_out_dirty"] = True
                        st.success("再生成しました（確定座標を反映）。"); st.rerun()

        # =====================================================================
        # Step 5b: 路線図を手で描き直す（shapes 編集）— 地図に点を打って正しい経路にする
        # =====================================================================
        if ss().get("result"):
            _gd = WORK / "out" / "gtfs"
            _shp = _gd / "shapes.txt"
            _trp = _gd / "trips.with_shapes.txt"
            _trp = _trp if _trp.exists() else (_gd / "trips.txt")
            _stp = _gd / "stops.txt"
            _stt = _gd / "stop_times.txt"
            if all(p.exists() for p in (_trp, _stp, _stt)):
                with st.expander("🖊 路線図を手で描き直す（経路 shapes を作り直す・任意）"):
                    st.caption("自動生成の経路が実際と違う時、地図に点を打って正しい経路に描き直せます。"
                               "路線・方向を選び、**『区間だけ直す』**（停留所を選び、その前後どちらかの区間だけ描き直す）か "
                               "**『全体を描き直す』**を選択。地図左上の**ペン（線のアイコン）**で道に沿って点を打ち"
                               "→ダブルクリックで確定 → 下のボタンで反映。描いた線で shapes を上書きします（推定より優先）。")
                    st.markdown(
                        "<div style='background:#eef4f5;border:1px solid #cfe0e3;border-radius:8px;"
                        "padding:8px 12px;font-size:12.5px;color:#0a4552;margin:2px 0 10px'>"
                        "🌐 <b>地図左上のボタンの意味</b>（英語表示のことがあります）：<br>"
                        "✏️<b>線のアイコン</b>＝線を描く（クリックで点を打つ／ダブルクリックで確定）　｜　"
                        "<b>Finish</b>＝完了　｜　<b>Delete last point</b>＝直前の点を消す　｜　<b>Cancel</b>＝やめる<br>"
                        "<b>鉛筆＋レイヤ</b>＝線を編集（点をドラッグ）　｜　<b>ゴミ箱</b>＝線を削除　｜　"
                        "<b>Save</b>＝保存　｜　<b>Clear All</b>＝全消去</div>",
                        unsafe_allow_html=True)
                    import csv as _c5
                    _trips5 = list(_c5.DictReader(_trp.open(encoding="utf-8-sig")))
                    _stops5 = {r["stop_id"]: r for r in _c5.DictReader(_stp.open(encoding="utf-8-sig"))}
                    _sts5 = list(_c5.DictReader(_stt.open(encoding="utf-8-sig")))
                    _rmeta = {r["route_id"]: r for r in _c5.DictReader((_gd / "routes.txt").open(encoding="utf-8-sig"))} \
                        if (_gd / "routes.txt").exists() else {}
                    _rd5 = sorted({(t["route_id"], (t.get("direction_id") or "0")) for t in _trips5})

                    def _rdlab(rd):
                        rid, dv = rd
                        return f"{_rmeta.get(rid, {}).get('route_long_name', rid)}（方向{dv}）"

                    _sel5 = st.selectbox("路線・方向", _rd5, format_func=_rdlab, key="shpedit_rd")
                    _rid5, _dir5 = _sel5
                    _rt5 = [t for t in _trips5 if t["route_id"] == _rid5 and (t.get("direction_id") or "0") == _dir5]
                    _shid = next((t.get("shape_id") for t in _rt5 if t.get("shape_id")), None)
                    _rep = _rt5[0]["trip_id"] if _rt5 else None
                    _seq5 = sorted([r for r in _sts5 if r["trip_id"] == _rep], key=lambda r: int(r["stop_sequence"]))
                    _spts = []
                    for r in _seq5:
                        s = _stops5.get(r["stop_id"], {})
                        if (s.get("stop_lat") or "").strip():
                            _spts.append((s.get("stop_name", ""), float(s["stop_lat"]), float(s["stop_lon"])))
                    if len(_spts) < 2:
                        st.warning("この路線・方向は停留所座標が足りません。先に⑤で座標を確定してください。")
                    else:
                        from folium.plugins import Draw
                        # 現在の経路点(shapes)を seq 順に。無ければ「停留所を直線で結んだ線」を土台にする。
                        _cur_latlon = []
                        if _shid and _shp.exists():
                            _tmp = []
                            for r in _c5.DictReader(_shp.open(encoding="utf-8-sig")):
                                if r.get("shape_id") == _shid:
                                    _tmp.append((int(r["shape_pt_sequence"]),
                                                 float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))
                            _tmp.sort()
                            _cur_latlon = [(la, lo) for _, la, lo in _tmp]
                        if not _cur_latlon:
                            _cur_latlon = [(p[1], p[2]) for p in _spts]   # 土台＝停留所直結
                        _octr = [sum(p[1] for p in _spts) / len(_spts),
                                 sum(p[2] for p in _spts) / len(_spts)]

                        def _num_icon(i, bg="#0e5c6b"):
                            return folium.DivIcon(html=(
                                f"<div style='background:{bg};color:#fff;border-radius:50%;width:22px;"
                                "height:22px;line-height:22px;text-align:center;font-size:11px;font-weight:700'>"
                                f"{i}</div>"))

                        def _draw_opts(m):
                            # 描く線は赤（#c62828）にする＝描いた/確定した線が赤で分かりやすい。
                            Draw(export=False, edit_options={"edit": True},
                                 draw_options={"polyline": {"shapeOptions": {"color": "#c62828", "weight": 5}},
                                               "polygon": False, "rectangle": False,
                                               "circle": False, "marker": False, "circlemarker": False}).add_to(m)
                            m.add_child(_DrawJPLabels())   # ツールバーを日本語化（実行される初期化スクリプトに載せる）

                        def _drawn_line(state):
                            """st_folium の戻りから描画ポリラインを (lat,lon) 列で取り出す。"""
                            d = None
                            if state:
                                d = state.get("last_active_drawing") or ((state.get("all_drawings") or [None])[-1])
                            if d and (d.get("geometry") or {}).get("type") == "LineString":
                                return [(c[1], c[0]) for c in d["geometry"]["coordinates"]]  # [lng,lat]→(lat,lon)
                            return []

                        def _regen_after_shape():
                            _zz = list((WORK / "out").glob("*_gtfs-jp.zip"))
                            if _zz:
                                run([SCRIPTS / "package_gtfs_zip.py", _gd, "-o", _zz[0],
                                     "--substitute", "trips.with_shapes.txt=trips.txt"])
                            run([SCRIPTS / "make_map_view.py", _stp, "--out", WORK / "out" / "map_view.html",
                                 "--title", "app_feed"])
                            run([SCRIPTS / "make_gtfs_viewer.py", "--feed", _gd,
                                 "-o", WORK / "out" / "gtfs_viewer.html"])
                            ss()["_out_dirty"] = True

                        _mode5 = st.radio(
                            "編集のしかた", ["区間だけ直す（一部を修正）", "全体を描き直す"],
                            horizontal=True, key="shpedit_mode",
                            help="『区間だけ直す』＝停留所を選び、その前後どちらかの区間だけを描き直して差し替え。"
                                 "『全体を描き直す』＝始点から終点まで一気に描き直し（従来）。")

                        if _mode5 == "全体を描き直す":
                            _em = folium.Map(location=_octr, zoom_start=13)
                            _draw_opts(_em)
                            for i, (nm, la, lo) in enumerate(_spts, 1):
                                folium.Marker([la, lo], tooltip=f"{i}. {nm}", icon=_num_icon(i)).add_to(_em)
                            folium.PolyLine(_cur_latlon, color="#1E5FA8", weight=4, opacity=0.85,
                                            tooltip="現在の経路").add_to(_em)
                            _emst = st_folium(_em, width=900, height=460, key="shpeditmap",
                                              returned_objects=["last_active_drawing", "all_drawings"])
                            _coords5 = _drawn_line(_emst)
                            if _coords5:
                                st.info(f"描いた線：{len(_coords5)} 点。この線で経路(shapes)を更新できます。")
                                if st.button("この線で経路(shapes)を更新する", type="primary", key="shpeditsave"):
                                    if not _shid:
                                        _shid = f"shape_{_rid5}_{_dir5}_manual"
                                        _assign_trip_shape(_trp, _rid5, _dir5, _shid)
                                    _write_shape(_shp, _shid, _coords5)
                                    _regen_after_shape()
                                    st.success("経路(shapes)を更新しました。地図・ビューア・zip を更新しました。")
                                    st.rerun()
                        else:
                            # 停留所番号を選び、その「前の区間(n-1→n)」か「次の区間(n→n+1)」だけ描き直す。
                            _N = len(_spts)
                            _pick = st.selectbox(
                                "基準にする停留所（番号）", list(range(1, _N + 1)),
                                format_func=lambda k: f"{k}. {_spts[k - 1][0]}", key="shpedit_pick")
                            _side_opts = []
                            if _pick > 1:
                                _side_opts.append((f"前の区間（{_pick - 1}→{_pick}）", _pick - 1, _pick))
                            if _pick < _N:
                                _side_opts.append((f"次の区間（{_pick}→{_pick + 1}）", _pick, _pick + 1))
                            _side = st.radio("直す区間（この区間を消して描き直す）", _side_opts,
                                             format_func=lambda o: o[0], key="shpedit_side")
                            _si, _sj = _side[1], _side[2]           # 1始まりの連続する2停留所
                            _a, _b = _spts[_si - 1], _spts[_sj - 1]

                            def _nrst(pt):   # 現在経路の中でこの座標に最も近い点の index
                                return min(range(len(_cur_latlon)),
                                           key=lambda k: (_cur_latlon[k][0] - pt[0]) ** 2
                                           + (_cur_latlon[k][1] - pt[1]) ** 2)
                            _result = ss().pop("_seg_result", None)   # 直前に差し替えたら結果(全体)を表示
                            if _result:
                                st.success(f"『{_result[0]}』〜『{_result[1]}』の区間を差し替えました。"
                                           "下が **できあがった経路（全体）** です（編集した区間以外もそのまま残っています）。")
                                _rm = folium.Map(location=_octr, zoom_start=14)
                                folium.PolyLine(_cur_latlon, color="#1E5FA8", weight=5, opacity=0.95,
                                                tooltip="できあがった経路（全体）").add_to(_rm)
                                for i, (nm, la, lo) in enumerate(_spts, 1):
                                    folium.Marker([la, lo], tooltip=f"{i}. {nm}", icon=_num_icon(i)).add_to(_rm)
                                st_folium(_rm, width=900, height=460, key="shpeditmap_result",
                                          returned_objects=[])
                                st.button("続けて別の区間を直す", key="shpedit_cont")
                            else:
                                _ia, _ib = _nrst((_a[1], _a[2])), _nrst((_b[1], _b[2]))
                                _lo, _hi = (_ia, _ib) if _ia <= _ib else (_ib, _ia)
                                _em = folium.Map(location=[(_a[1] + _b[1]) / 2, (_a[2] + _b[2]) / 2], zoom_start=15)
                                _draw_opts(_em)
                                # 直す区間は線を消して「ギャップ（線がつながっていない状態）」にする。
                                # 区間の前後の現在経路(グレー)だけ残し、赤い2停留所の間は線を描かない
                                # ＝利用者はこの空いた区間にペンで道を描く（第1区間①→②でも線が残らない）。
                                _before, _after = _cur_latlon[:_lo + 1], _cur_latlon[_hi:]
                                if len(_before) >= 2:
                                    folium.PolyLine(_before, color="#1E5FA8", weight=4, opacity=0.85,
                                                    tooltip="現在の経路（ここは変えません）").add_to(_em)
                                if len(_after) >= 2:
                                    folium.PolyLine(_after, color="#1E5FA8", weight=4, opacity=0.85,
                                                    tooltip="現在の経路（ここは変えません）").add_to(_em)
                                # 編集前の区間（差し替え対象）をグレーの点線で参考表示＝描き直しの目安。
                                if _hi > _lo:
                                    folium.PolyLine(_cur_latlon[_lo:_hi + 1], color="#8a949e", weight=3,
                                                    opacity=0.75, dash_array="4,10",
                                                    tooltip="編集前の経路（この区間を描き直す・参考）").add_to(_em)
                                for i, (nm, la, lo) in enumerate(_spts, 1):
                                    folium.Marker([la, lo], tooltip=f"{i}. {nm}",
                                                  icon=_num_icon(i, "#c62828" if i in (_si, _sj) else "#0e5c6b")).add_to(_em)
                                st.caption(f"**グレーの点線＝編集前の経路**（『{_a[0]}』↔『{_b[0]}』・差し替え対象）。これを目安に、"
                                           "地図左上の**ペン**でこの区間の道に沿って点を打ち→ダブルクリックで確定してください。"
                                           "**描いた線は赤**で表示され、この区間の新しい経路になります（青い他の区間はそのまま残ります）。")
                                _emst = st_folium(_em, width=900, height=460, key="shpeditmap_seg",
                                                  returned_objects=["last_active_drawing", "all_drawings"])
                                _seg = _drawn_line(_emst)
                                if _seg:
                                    _plo, _phi = _cur_latlon[_lo], _cur_latlon[_hi]

                                    def _d2(u, v):
                                        return (u[0] - v[0]) ** 2 + (u[1] - v[1]) ** 2
                                    # 描いた線の向きを、置き換える両端に合わせる（逆向きに描いても正しくつなぐ）
                                    if _d2(_seg[0], _plo) + _d2(_seg[-1], _phi) > _d2(_seg[0], _phi) + _d2(_seg[-1], _plo):
                                        _seg = list(reversed(_seg))
                                    _new_pts = _cur_latlon[:_lo + 1] + _seg + _cur_latlon[_hi:]
                                    st.info(f"描いた線：{len(_seg)} 点。『{_a[0]}』〜『{_b[0]}』の区間を差し替えます"
                                            f"（経路は全体で {len(_new_pts)} 点になります）。")
                                    if st.button("この区間を差し替える", type="primary", key="shpeditsave_seg"):
                                        if not _shid:
                                            _shid = f"shape_{_rid5}_{_dir5}_manual"
                                            _assign_trip_shape(_trp, _rid5, _dir5, _shid)
                                        _write_shape(_shp, _shid, _new_pts)
                                        _regen_after_shape()
                                        ss()["_seg_result"] = (_a[0], _b[0])   # 差し替え結果(全体)を表示するフラグ
                                        st.rerun()

# =====================================================================
# Step 6: GTFSビューア（作成した feed を 7タブで閲覧）
# =====================================================================
if ss().get("result"):
    viewer = WORK / "out" / "gtfs_viewer.html"
    if viewer.exists():
        st.header("⑥ ビューアで確認 → GTFS-JP をダウンロード")
        st.caption("作成した GTFS をブラウザで確認（📋路線一覧 / 🕐時刻表 / 💴運賃表 / 🗺️路線図 / "
                   "📅運行カレンダー / 🚏バス停一覧 / ✓データチェック結果）。"
                   "内容を確認したら、**下の大きなボタンから GTFS-JP 一式（zip）をダウンロード**してください。")
        html = viewer.read_text(encoding="utf-8")
        components.html(html, height=820, scrolling=True)
        # ★ 完成物 = GTFS-JP 一式(zip) を、ビューアの直下に目立つ大ボタンで配置
        _vz = list((WORK / "out").glob("*_gtfs-jp.zip"))
        if _vz:
            st.download_button("⬇  完成した GTFS-JP 一式（zip）をダウンロード",
                               _vz[0].read_bytes(), _vz[0].name, type="primary",
                               use_container_width=True, key="dl_zip_main")
        # ビューア(HTML)のDLは補助 → 小さく右下に
        _vc1, _vc2 = st.columns([3, 1])
        _vc2.download_button("ビューア(HTML)を保存", html.encode("utf-8"),
                             "gtfs_viewer.html", mime="text/html", key="dl_viewer_html")

# 画面描画の最後に、現在の作業状態を自動保存（節目ごと＝実質ほぼ毎回の確定状態）。
autosave()
