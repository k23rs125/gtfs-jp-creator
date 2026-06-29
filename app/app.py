# -*- coding: utf-8 -*-
"""GTFS-JP 半自動生成アプリ（Streamlit MVP）。
時刻表(PDF/Excel)アップロード → 抽出 → Claudeで構造化 → 条件確認フォーム →
生成(apply_decisions + run_pipeline) → 検証結果・地図・GTFS-JP ダウンロード。

設計: 正確さの源は決定的スクリプト。LLM(Claude API)は構造化(Step2)の判断のみ。
PDF/Excelに無いメタ情報(事業者・運行日・運賃)は推測せず条件確認フォームで人が入力する。

起動: streamlit run app/app.py
"""
import json
import os
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

st.set_page_config(page_title="GTFS-JP 半自動生成", page_icon="🚌", layout="wide")
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

st.title("🚌 GTFS-JP 半自動生成アプリ")
st.caption("バス時刻表(PDF/Excel) → GTFS-JP。正確さの源は決定的スクリプト、"
           "LLMは構造化の判断のみ、無い情報は推測せず確認フォームで入力（正しく失敗）。")

# =====================================================================
# Step 1: アップロード → 抽出
# =====================================================================
st.header("① 時刻表をアップロード")
up = st.file_uploader("バス時刻表（.xlsx / テキストPDF / OCR後の .md）", type=["xlsx", "pdf", "md"])
st.caption("📄 文字が選べるPDF・Excelはそのまま。**画像化PDF（スキャン）は文字が無いので抽出できません** → "
           "下のコマンドでOCRし、できた .md をアップロードしてください。")


def _ocr_hint(src):
    cmd = (f'python skills/gtfs-jp-creator/scripts/pdf_to_markdown.py "{src}" '
           f'--engine mineru --lang japan -o out.md')
    st.warning("この時刻表は**画像化PDF（文字情報なし）**でした。テキスト方式では抽出できません。\n\n"
               "**OCRで文字起こし**してから、できた `out.md` を①に再アップロードしてください"
               "（OCRはCPUだと数分〜数十分かかるため、アプリ外で実行します）:")
    st.code(cmd, language="bash")
    st.caption("OCRは誤読が起きます。取り込み後は必ず原典と目視照合してください。")


def do_extract(src):
    ext_out = WORK / "extract.json"
    low = str(src).lower()
    if low.endswith(".xlsx"):
        rc, so, se = run([SCRIPTS / "extract_timetable_excel.py", src, "-o", ext_out])
    elif low.endswith(".md"):
        rc, so, se = run([SCRIPTS / "extract_timetable_markdown.py", src, "-o", ext_out])
    else:
        rc, so, se = run([SCRIPTS / "extract_timetable_coords.py", src, "-o", ext_out])
    if rc == 0 and ext_out.exists():
        ex = json.loads(ext_out.read_text(encoding="utf-8"))
        # 画像化PDFで0停留所 → OCR経路へ誘導（空のまま進めない）
        if not ex.get("blocks") and any(n.get("type") == "image_pdf_use_ocr"
                                        for n in ex.get("needs_confirmation", [])):
            _ocr_hint(src)
            return
        ss().extract = ex
        ss().extract_token = str(src)
        for k in ("decision_spec", "result", "confirmed"):
            ss().pop(k, None)
        st.success("抽出しました。")
    else:
        st.error("抽出に失敗しました。\n" + se[-800:])


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
                         "停留所数": len(nm), "路線名": rname, "方向(0/1)": d % 2})
    return rows


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
        },
    )
    # 割り当て表から decision_spec を構築（同じ路線名のブロックを1路線にまとめる）
    name_blocks, block_dir, headsign = {}, {}, {}
    for _, r in edited.iterrows():
        bi = int(r["ブロック"]); nm = str(r["路線名"]).strip() or f"路線{bi}"
        name_blocks.setdefault(nm, []).append(bi)
        block_dir[str(bi)] = int(r["方向(0/1)"])
        dh = blocks_e[bi].get("direction_hint")
        if dh:
            headsign[str(bi)] = dh
    routes = [{"route_id": f"R{i + 1:02d}", "route_long_name": nm, "blocks": bidx, "circular": False}
              for i, (nm, bidx) in enumerate(name_blocks.items())]
    ss().decision_spec = {"routes": routes, "block_direction": block_dir, "block_headsign": headsign,
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
# 時刻チェック: OCR誤読が疑われる時刻を検出し、修正候補を出す（人が確認・確定）。
# 主に画像PDF→OCR(MinerU)で起きる数字の読み違いを拾う。自動では書き換えない。
# =====================================================================
if "extract" in ss():
    tok = ss().get("extract_token", "")
    if ss().get("anomalies_token") != tok:
        (WORK / "_ext_check.json").write_text(json.dumps(ss().extract, ensure_ascii=False), encoding="utf-8")
        rc, so, se = run([SCRIPTS / "detect_time_anomalies.py", WORK / "_ext_check.json",
                          "-o", WORK / "anomalies.json"])
        ap = WORK / "anomalies.json"
        ss().anomalies = json.loads(ap.read_text(encoding="utf-8")) if ap.exists() else []
        ss().anomalies_token = tok
    anomalies = ss().get("anomalies", [])
    if anomalies:
        st.subheader(f"⏰ 時刻チェック（OCR誤読の疑い {len(anomalies)} 件）")
        st.caption("時刻の逆行や便間パターンからの外れを検出しました（主にOCRの数字読み違い）。"
                   "**原典と照合**し、必要なら『採用時刻』を直して『時刻を修正して反映』を押してください。"
                   "空欄/現在値のままなら変更しません（自動では書き換えません）。")
        adf = pd.DataFrame([{
            "block": a["block"], "便": a.get("trip_label") or a.get("trip_col"),
            "停留所": a["stop_name"], "現在": a["current"],
            "候補": a.get("suggested") or "", "確度": a.get("confidence"),
            "理由": a["reason"], "採用時刻": a.get("suggested") or a["current"],
            "_col": a.get("trip_col"), "_seq": a.get("seq"),
        } for a in anomalies])
        ed = st.data_editor(
            adf, hide_index=True, use_container_width=True, key=f"anom_{tok}",
            column_config={
                "block": st.column_config.NumberColumn(disabled=True),
                "便": st.column_config.TextColumn(disabled=True),
                "停留所": st.column_config.TextColumn(disabled=True),
                "現在": st.column_config.TextColumn(disabled=True),
                "候補": st.column_config.TextColumn(disabled=True),
                "確度": st.column_config.TextColumn(disabled=True),
                "理由": st.column_config.TextColumn(disabled=True),
                "採用時刻": st.column_config.TextColumn("採用時刻(HH:MM)", help="ここを原典どおりに直す"),
                "_col": None, "_seq": None,
            },
        )
        if st.button("時刻を修正して反映"):
            import re as _re
            n_fix = 0
            for _, r in ed.iterrows():
                val = str(r["採用時刻"]).strip()
                cur = str(r["現在"]).strip()
                if not val or val == cur:
                    continue
                m = _re.match(r"^(\d{1,2}):(\d{2})", val)
                if not m:
                    continue
                hhmmss = f"{int(m.group(1)):02d}:{m.group(2)}:00"
                for b in ss().extract.get("blocks", []):
                    if b.get("block_index") != int(r["block"]):
                        continue
                    for t in b.get("trips", []):
                        if t.get("col") != r["_col"]:
                            continue
                        idx = int(r["_seq"]) - 1
                        if 0 <= idx < len(t.get("cells", [])):
                            t["cells"][idx]["time"] = hhmmss
                            n_fix += 1
            if n_fix:
                for k in ("decision_spec", "result", "confirmed", "anomalies_token"):
                    ss().pop(k, None)
                st.success(f"{n_fix} 件の時刻を修正しました。③で条件を入れて再生成してください。")
                st.rerun()
            else:
                st.info("変更がありませんでした。")

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
    with st.form("conditions"):
        c1, c2, c3 = st.columns(3)
        if len(_routes_now) == 1:
            route_name = c1.text_input("路線名", value=_routes_now[0].get("route_long_name", ""))
        else:
            route_name = ""  # 多路線は②の割り当てで路線名を設定
            c1.caption("路線名は②で設定済み: " + " / ".join(r["route_long_name"] for r in _routes_now))
        muni = c1.text_input("対象自治体（都道府県＋市区町村）", value="福岡県", help="P11の都道府県/市域制約に使用")
        fare = c1.number_input("運賃（円・0なら無料/未設定）", min_value=0, value=0, step=10)
        ag_name = c2.text_input("事業者名", value="")
        ag_id = c2.text_input("法人番号（不明なら空）", value="")
        ag_url = c2.text_input("URL", value="")
        ag_phone = c2.text_input("電話", value="")
        is_circular = c3.checkbox("循環路線（始点に戻る）", value=_loop,
                                  help="始点=終点を検出すると自動でチェック。違えば外してください。")
        headsign = c3.text_input("行き先表示（方向名）", value="",
                                 help="空なら自動（方向見出し→無ければ終点名）。"
                                      "方向見出しが無い路線（循環など）に適用。例『循環』")
        st.write("運行する曜日")
        d = st.columns(7)
        days = [d[i].checkbox(x, value=(i < 5)) for i, x in enumerate(["月", "火", "水", "木", "金", "土", "日"])]
        c4, c5 = st.columns(2)
        start = c4.text_input("有効期間 開始 (YYYYMMDD)", value="")
        end = c5.text_input("有効期間 終了 (YYYYMMDD)", value="")
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
        spec["service"] = {"service_id": "SVC",
                           "mon": int(days[0]), "tue": int(days[1]), "wed": int(days[2]),
                           "thu": int(days[3]), "fri": int(days[4]), "sat": int(days[5]), "sun": int(days[6]),
                           "start_date": start or "20250401", "end_date": end or "20271231"}
        if fare > 0:
            spec["fare_price"] = int(fare)
        aid = ag_id or "AGENCY_TBD"
        spec["agency"] = {"agency_id": aid, "agency_name": ag_name or "未定（自治体が記入）",
                          "agency_url": ag_url or None, "agency_phone": ag_phone or None}
        spec["agency_jp"] = {"agency_official_name": ag_name or None, "agency_zip_number": None,
                             "agency_address": None, "agency_president_pos": None, "agency_president_name": None}
        (WORK / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        (WORK / "extract.json").write_text(json.dumps(ss().extract, ensure_ascii=False), encoding="utf-8")

        with st.spinner("構造化 → 生成 → 座標補完 → 検証 を実行中..."):
            rc, so, se = run([APPLY_DECISIONS, "--extract", WORK / "extract.json",
                              "--decisions", WORK / "spec.json", "--out", WORK / "structured.json"])
            if rc != 0:
                st.error("構造化(apply_decisions)に失敗:\n" + se[-800:]); st.stop()
            pref = muni
            for k in ("県", "都", "府", "道"):
                if k in muni:
                    pref = muni[:muni.index(k) + 1]; break
            cfg = {"feed_name": "app_feed", "input_json": str(WORK / "structured.json"),
                   "extract_json": str(WORK / "extract.json"), "output_dir": str(WORK / "out"),
                   "context": muni, "p11_prefecture": pref, "use_nominatim": bool(use_nom),
                   "interpolate_coords": True, "validate": True}
            (WORK / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            rc, so, se = run([SCRIPTS / "run_pipeline.py", "--config", WORK / "config.json"], cwd=REPO)
        ss().result = {"rc": rc, "log": se}
        st.success("完了しました。" if rc == 0 else "完了（警告/エラーあり）。")

# =====================================================================
# Step 4: 結果（検証・地図・ダウンロード）
# =====================================================================
if ss().get("result"):
    st.header("④ 結果")
    out = WORK / "out"
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
                   "**全部が確定になるまで「公式提出可」にしない**（＝推測座標を黙って出さない）。")
        import csv as _csv
        crows = list(_csv.DictReader(conf_csv.open(encoding="utf-8-sig")))
        confirmed = ss().setdefault("confirmed", {})  # stop_name -> (lat,lon)

        def eff_conf(r):
            return "確定" if r["stop_name"] in confirmed else r["confidence"]

        n_ok = sum(1 for r in crows if eff_conf(r) == "確定")
        n_rev = sum(1 for r in crows if eff_conf(r) == "要確認")
        n_non = sum(1 for r in crows if eff_conf(r) == "未補完")
        m1, m2, m3 = st.columns(3)
        m1.metric("確定", n_ok); m2.metric("要確認", n_rev); m3.metric("未補完", n_non)

        # 地図（確定=緑/要確認=橙）。確認済み(session)は確定扱い。
        pts = []
        for r in crows:
            nm = r["stop_name"]
            if nm in confirmed:
                la, lo, conf = confirmed[nm][0], confirmed[nm][1], "確定"
            elif (r.get("stop_lat") or "").strip():
                la, lo, conf = float(r["stop_lat"]), float(r["stop_lon"]), r["confidence"]
            else:
                continue
            pts.append((nm, la, lo, conf, r.get("reason", "")))
        center = ([sum(p[1] for p in pts) / len(pts), sum(p[2] for p in pts) / len(pts)]
                  if pts else [35.0, 138.0])
        fmap = folium.Map(location=center, zoom_start=14)
        col = {"確定": "green", "要確認": "orange", "未補完": "red"}
        for nm, la, lo, conf, reason in pts:
            folium.CircleMarker([la, lo], radius=6, color=col.get(conf, "gray"),
                                fill=True, fill_opacity=0.9,
                                popup=f"{nm}（{conf}）{reason}").add_to(fmap)
        state = st_folium(fmap, width=900, height=460, key="confmap")
        clicked = state.get("last_clicked") if state else None
        if clicked:
            st.info(f"地図クリック位置: {clicked['lat']:.6f}, {clicked['lng']:.6f}"
                    "（下で停留所を選び『地図クリック位置を使う』）")

        todo = [r["stop_name"] for r in crows if eff_conf(r) != "確定"]
        if todo:
            st.subheader(f"要確認・未補完を確定する（残り {len(todo)} 件）")
            sel = st.selectbox("停留所", todo)
            cur = next((r for r in crows if r["stop_name"] == sel), {})
            st.write(f"現在の座標: {cur.get('stop_lat','')}, {cur.get('stop_lon','')} ／ "
                     f"理由: {cur.get('reason','')}")
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
                mc = {"by_stop_name": {nm: {"lat": la, "lon": lo}
                                       for nm, (la, lo) in confirmed.items()}}
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
