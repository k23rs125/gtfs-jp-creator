# -*- coding: utf-8 -*-
"""停留所名ふりがな(読み)と出典(監査)関連のヘルパー（app.py 分割・保守性向上）。

責務:
    - 読みの自動生成（階層辞書を最優先→SudachiPy＋補正規則）
    - 階層辞書（全国<自治体<路線<利用者）のマージ・キャッシュ
    - 出典判定（AI／各辞書層／自動／手動）＝説明責任のための表示
    - 修正の層選択保存＋監査ログ記録

app 状態は app_context 経由（ss/work_dir）で最小限に扱い、読みロジックは
reading_dictionary / generate_translations を用いる。app.py からは従来名（先頭 _）で
エイリアス import して呼ぶ＝挙動不変。
License: Apache 2.0
"""
import datetime
import json
import re

from app_context import ss, work_dir

try:
    from generate_translations import to_hiragana as _to_hira
    import reading_dictionary as _rdict_mod
except Exception:                       # 読みエンジン未導入でもアプリは動く（読みは空/要確認）
    _to_hira, _rdict_mod = None, None

# 読みの出典（層）を日本語ラベルで表示するための対応表。
LAYER_JA = {"national": "全国共通", "municipality": "自治体", "route": "路線",
            "user": "利用者", "project": "プロジェクト"}


def reading_overrides():
    """現在の自治体/路線/利用者から階層辞書（全国<自治体<路線<利用者）をマージした
    {stop_name: spec(+_layer)} を返す。識別子が変わった時だけ再読込してキャッシュする。
    spec['_layer'] が読みの出典（監査証跡・出典表示の基盤）。"""
    if _rdict_mod is None:
        return {}
    muni = str(ss().get("muni_input", "") or "").strip()
    user = str(ss().get("user_name", "") or "").strip()
    routes = ",".join(
        (r.get("route_long_name") or "").strip()
        for r in (ss().get("decision_spec", {}) or {}).get("routes", [])
        if (r.get("route_long_name") or "").strip())
    key = (muni, routes, user)
    if ss().get("_rovr_key") != key:
        try:
            ss()["_rovr"] = _rdict_mod.resolve(municipality=muni or None,
                                               routes=routes or None, user=user or None)
        except Exception:
            ss()["_rovr"] = {}
        ss()["_rovr_key"] = key
    return ss().get("_rovr", {})


def auto_reading(name):
    """停留所名からふりがな(ja-Hrkt)を自動生成。階層辞書を最優先→SudachiPy＋補正規則。"""
    name = (name or "").strip()
    ov = reading_overrides().get(name)
    if ov and ov.get("ja-Hrkt"):
        return ov["ja-Hrkt"]
    return _to_hira(name) if _to_hira else ""


def reading_source(name, reading):
    """現在の読み reading の出典（AI／プロジェクト／利用者／路線／自治体／全国／自動）をラベルで返す。
    説明責任のための「その読みがどこ由来か」の表示。判定は上位（具体的）から順に行う。"""
    reading = (reading or "").strip()
    if not reading:
        return ""
    # 当セッションで層を明示保存した場合はそれを尊重（保存直後の表示を正確に）。
    _sl = (ss().get("_reading_layer") or {}).get(name)
    if _sl:
        return LAYER_JA.get(_sl, _sl)
    aiap = ss().get("ai_applied") or {}
    if name in aiap and (aiap[name].get("yomi") or "").strip() == reading:
        return "AI"
    try:
        _mr = work_dir() / "manual_readings.json"
        if _mr.exists():
            _m = json.loads(_mr.read_text(encoding="utf-8")).get("by_stop_name", {})
            if name in _m and (_m[name].get("ja-Hrkt") or "").strip() == reading:
                return "プロジェクト"
    except Exception:
        pass
    ov = reading_overrides().get(name)
    if ov and (ov.get("ja-Hrkt") or "").strip() == reading:
        return LAYER_JA.get(ov.get("_layer"), ov.get("_layer") or "辞書")
    if _to_hira and _to_hira(name) == reading:
        return "自動"
    return "手動"


def save_reading_layer(layer, stop_name, ja_hrkt="", en="", source="human",
                       ai_model="", note="", old=""):
    """読み修正を指定した辞書層へ保存し、監査ログへ1件追記する（出典・変更者・日時つき）。
    project 層は manual_readings.json（呼び出し側で適用）なので辞書CSVへは書かない。
    user/municipality/route/national は該当CSVへ write_layer_entry で永続化する。
    戻り値: 実際に保存した層（キー未設定で保存できない時は 'project' に落とす）。"""
    user = str(ss().get("user_name", "") or "").strip()
    today = datetime.date.today().isoformat()
    key = {"user": user, "municipality": str(ss().get("muni_input", "") or "").strip(),
           "route": next((r.get("route_long_name", "") for r in
                          (ss().get("decision_spec", {}) or {}).get("routes", [])), ""),
           "national": "", "project": ""}.get(layer, "")
    eff_layer = layer
    if layer in ("user", "municipality", "route") and not key:
        eff_layer = "project"      # キー（担当者名/自治体名/路線名）が無ければ共有層に書けない
    if eff_layer in ("user", "municipality", "route", "national") and _rdict_mod is not None:
        try:
            _rdict_mod.write_layer_entry(None, eff_layer, key, stop_name,
                                         ja_hrkt=ja_hrkt, en=en, source=source,
                                         updated_by=user, updated_at=today, note=note)
        except Exception:
            eff_layer = "project"
    # 監査ログ（追記専用JSONL・改ざん防止）
    if _rdict_mod is not None:
        try:
            _rdict_mod.append_audit({
                "user": user or "(未設定)", "stop_name": stop_name,
                "field": "ja-Hrkt" if ja_hrkt else ("en" if en else ""),
                "old": old, "new": ja_hrkt or en, "layer": eff_layer,
                "source": source, "ai_model": ai_model, "note": note})
        except Exception:
            pass
    ss().setdefault("_reading_layer", {})[stop_name] = eff_layer
    return eff_layer


def reading_suspicious(reading):
    """自動読みが怪しい（漢字が残る・半角カナ・濁点の文字化け）なら True。要確認の目印。"""
    r = reading or ""
    return bool(re.search(r"[一-鿿｡-ﾟ゚゜]", r)) or not r.strip()


def build_reading_rows(order, cur, ai_applied=None):
    """停留所の読み確認表(④・停留所名の表)の表示行(dict のリスト)を組み立てる。

    order: 停留所名の並び / cur: {name: {ja-Hrkt, en}} / ai_applied: AIが既定化した読み(印用)。
    要確認印(⚠)は reading_suspicious、出典は reading_source で判定する（app.py 側と同一挙動）。
    UI表示処理とデータ整形処理の分離。
    """
    ai_applied = ai_applied or {}
    rows = []
    for nm in order:
        h = cur[nm].get("ja-Hrkt", "")
        mk = "⚠" if reading_suspicious(h) else ""
        if nm in ai_applied:
            mk = (mk + " 🔎AI").strip()
        rows.append({"要確認": mk, "出典": reading_source(nm, h),
                     "停留所名": nm, "ふりがな(ja-Hrkt)": h,
                     "英語(en)": cur[nm].get("en", "")})   # 英語は常に編集可（空でOK）
    return rows


def build_audit_rows(entries):
    """監査ログのエントリ列(entries)を、④変更履歴ビューア用の表示行(dict のリスト)へ変換する。
    純関数（副作用なし）＝UI表示とデータ整形の分離。新しい変更が上に来るよう逆順で返す。
    保存層(layer)は日本語ラベル(LAYER_JA)に変換する。"""
    return [{
        "日時": e.get("ts", ""), "担当": e.get("user", ""),
        "停留所": e.get("stop_name", ""), "項目": e.get("field", ""),
        "変更前": e.get("old", ""), "変更後": e.get("new", ""),
        "保存層": LAYER_JA.get(e.get("layer", ""), e.get("layer", "")),
        "出典": e.get("source", ""), "AIモデル": e.get("ai_model", "")}
        for e in reversed(entries)]
