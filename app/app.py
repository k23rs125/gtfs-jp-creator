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
up = st.file_uploader("バス時刻表（.xlsx / .pdf）", type=["xlsx", "pdf"])


def do_extract(src):
    ext_out = WORK / "extract.json"
    if str(src).lower().endswith(".xlsx"):
        rc, so, se = run([SCRIPTS / "extract_timetable_excel.py", src, "-o", ext_out])
    else:
        rc, so, se = run([SCRIPTS / "extract_timetable_coords.py", src, "-o", ext_out])
    if rc == 0 and ext_out.exists():
        ss().extract = json.loads(ext_out.read_text(encoding="utf-8"))
        for k in ("decision_spec", "result", "confirmed"):
            ss().pop(k, None)
        st.success("抽出しました。")
    else:
        st.error("抽出に失敗しました。\n" + se[-800:])


c_a, c_b = st.columns([1, 1])
if c_a.button("抽出する", type="primary", disabled=(up is None)) and up:
    src = WORK / up.name
    src.write_bytes(up.getbuffer())
    do_extract(src)
if c_b.button("サンプルで試す（太宰府まほろば号）"):
    do_extract(Path(__file__).resolve().parent / "samples" / "sample_dazaifu_mahoroba.xlsx")

if "extract" in ss():
    ex = ss().extract
    blocks = ex.get("blocks", [])
    total_trips = sum(len(b.get("trips", [])) for b in blocks)
    st.info(f"ブロック {len(blocks)} / 便 計 {total_trips}")
    for b in blocks:
        trips = b.get("trips", [])
        names = [c["name"] for c in trips[0]["cells"]] if trips else []
        st.write(f"- block {b.get('block_index')}（{b.get('direction_hint') or '方向見出しなし'}）"
                 f": 便 {len(trips)} / 停留所 {len(names)}")
        st.caption("　順: " + " → ".join(names))

# =====================================================================
# Step 2: Claude で構造化（decision-spec）
# =====================================================================
if "extract" in ss():
    st.header("② 自動構造化（路線・方向・循環の判断）")
    st.caption("抽出結果から路線・方向・循環をシステムが自動で割り当てます。"
               "見出しが曖昧な時刻表だけ、下の詳細で Claude 構造化や手動調整ができます。")
    # 既定: 抽出の direction_hint から自動構造化（APIキー無しでも成立）
    default_spec = json.dumps(ss().get("decision_spec", {
        "routes": [{"route_id": "R01", "route_long_name": "", "blocks": list(range(len(ex.get("blocks", [])))), "circular": False}],
        "block_direction": {str(i): i for i in range(len(ex.get("blocks", [])))},
        "block_headsign": {str(i): (ex["blocks"][i].get("direction_hint") or "") for i in range(len(ex.get("blocks", [])))},
        "exclude_reserve": True, "exclude_unnumbered": False, "stop_key": "name"
    }), ensure_ascii=False, indent=2)
    with st.expander("詳細を調整（任意：Claude構造化 / decision-spec の手動編集）"):
        key = st.text_input("ANTHROPIC_API_KEY（環境変数があれば空でOK）", type="password", value="")
        api_key = key or os.environ.get("ANTHROPIC_API_KEY", "")
        if st.button("Claudeで構造化"):
            if not api_key:
                st.warning("APIキーが未設定です。下の欄に decision-spec を貼り付けても進められます。")
            else:
                try:
                    with st.spinner("Claude が構造を判断中..."):
                        ss().decision_spec = claude_structure.structure(ss().extract, api_key)
                    st.success("構造化しました。")
                except Exception as e:
                    st.error(f"Claude 呼び出し失敗: {e}")
        spec_text = st.text_area("decision-spec（自動生成・編集可）", value=default_spec, height=260)
        try:
            ss().decision_spec = json.loads(spec_text)
            st.caption("✓ JSONとして妥当")
        except Exception:
            st.caption("⚠ JSONが不正です")
    # expander を開かなくても decision_spec を確定（既定 or 既存）させる
    if not ss().get("decision_spec"):
        ss().decision_spec = json.loads(default_spec)

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
    circ = any(r.get("circular") for r in spec0.get("routes", []))
    auto.append(f"循環/方向: {'循環あり' if circ else '往復（循環なし）'}（構造化の判断）")
    st.success("自動で分かったこと:\n" + "\n".join("・" + a for a in auto))
    nc = list(ss().extract.get("needs_confirmation", []))
    nc += [{"message": w} for w in ss().extract.get("warnings", [])]
    if nc:
        st.warning("要確認（原典と照合してください）:\n"
                   + "\n".join("・" + (x.get("message") if isinstance(x, dict) else str(x)) for x in nc))
    st.info("PDF/Excel に無いので③で質問します: 路線名 / 事業者名・法人番号・URL・電話 / "
            "運賃 / 運行する曜日 / 有効期間 / 対象自治体（座標補完用）")

# =====================================================================
# Step 3: PDF/Excelに無い項目だけを後から質問（自動確認の後）
# =====================================================================
if ss().get("decision_spec"):
    st.header("③ PDF/Excel に無い項目を入力（不足分の質問）")
    st.caption("上の②でシステムが確認した結果、時刻表に書かれていない項目です。"
               "推測せず入力してください（不明は空欄でOK＝暫定/要確認として入る。ただし路線名は必須）。")
    c1, c2, c3 = st.columns(3)
    route_name = c1.text_input("路線名", value=ss()["decision_spec"]["routes"][0].get("route_long_name", ""))
    muni = c1.text_input("対象自治体（都道府県＋市区町村）", value="福岡県", help="P11の都道府県/市域制約に使用")
    fare = c1.number_input("運賃（円・0なら無料/未設定）", min_value=0, value=0, step=10)
    ag_name = c2.text_input("事業者名", value="")
    ag_id = c2.text_input("法人番号（不明なら空）", value="")
    ag_url = c2.text_input("URL", value="")
    ag_phone = c2.text_input("電話", value="")
    st.write("運行する曜日")
    d = st.columns(7)
    days = [d[i].checkbox(x, value=(i < 5)) for i, x in enumerate(["月", "火", "水", "木", "金", "土", "日"])]
    c4, c5 = st.columns(2)
    start = c4.text_input("有効期間 開始 (YYYYMMDD)", value="")
    end = c5.text_input("有効期間 終了 (YYYYMMDD)", value="")
    use_nom = st.checkbox("Nominatim 補完を使う（POI多い路線向け・遅い）", value=False)

    if st.button("GTFS-JP を生成する", type="primary"):
        # 必須チェック: 官公庁提出物が黙って Validator ERROR にならないよう、
        # 路線名が空なら生成しない（GTFS仕様: route_short_name か route_long_name のどちらか必須）。
        eff_route = (route_name or ss()["decision_spec"]["routes"][0].get("route_long_name") or "").strip()
        if not eff_route:
            st.error("路線名が空です。GTFS仕様では route_short_name / route_long_name の"
                     "いずれかが必須で、空のまま生成すると Validator ERROR "
                     "（route_both_short_and_long_name_missing）になります。③で路線名を入力してください。")
            st.stop()
        # 事業者名は暫定運用を許容（＝止めない）が、空なら明示警告。
        if not ag_name.strip():
            st.warning("事業者名が空です。agency は暫定値（agency_id=AGENCY_TBD／『未定（自治体が記入）』）"
                       "で出力されます。正式提出前に事業者名・法人番号を記入してください。")
        spec = dict(ss()["decision_spec"])
        if route_name:
            spec["routes"][0]["route_long_name"] = route_name
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
    # Validator
    rep = out / "validation" / "report.json"
    if rep.exists():
        try:
            notices = json.loads(rep.read_text(encoding="utf-8")).get("notices", [])
            errs = sum(n.get("totalNotices", 0) for n in notices if n.get("severity") == "ERROR")
            st.metric("MobilityData Validator ERROR", errs)
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
