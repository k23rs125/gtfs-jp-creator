# -*- coding: utf-8 -*-
"""停留所名ふりがな(・英訳)の階層辞書。

5階層（優先度・上書きルール／低→高で後が上書き）:
    1. national     全国共通辞書   … 全国で安全な難読地名（同梱・開発側が監査して追加）
    2. municipality 自治体辞書     … その市区町村の慣用読み（例: 本町=ほんまち）
    3. route        路線辞書       … 事業者/路線単位の読み（複数路線はカンマ区切りで併合）
    4. user         利用者辞書     … 担当者が持ち回る個人の補正（案件をまたいで再利用）
    5. project      プロジェクト辞書 … その案件の最終確定（manual_readings.json）。
                     本モジュールは1〜4を解決し、5は生成後に apply_manual_readings.py が最優先で適用。

保存場所（名前キー・環境変数 GTFS_DICT_DIR で移設可能）:
    <dict_dir>/national.csv
    <dict_dir>/municipality/<自治体名>.csv
    <dict_dir>/route/<路線名>.csv
    <dict_dir>/user/<利用者名>.csv
    既定 <dict_dir> = skills/gtfs-jp-creator/references/dictionaries/
    national.csv が無ければ旧 references/data/stop_readings.csv を使う（後方互換）。

各CSVの列（後半は任意＝将来の監査証跡・出典管理の器）:
    stop_name, ja-Hrkt, en, source, updated_by, updated_at, note

resolve() は {stop_name: {ja-Hrkt, en, ..., _layer}} を返す。_layer は由来した層の名前
（＝その読みの「出典」）で、監査証跡・出典表示の基盤になる。

License: Apache 2.0
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import re
from pathlib import Path

# 低優先 → 高優先（後の層が前の層を上書きする）。project(manual_readings)は生成後に別途適用。
LAYER_ORDER = ["national", "municipality", "route", "user"]
# CSVで受け付ける列（stop_name とふりがな/英訳＋出典メタ）
SPEC_FIELDS = ("ja-Hrkt", "en", "source", "updated_by", "updated_at", "note")


def dict_root(explicit: str | None = None) -> Path:
    """辞書ルートを返す。優先: 引数 > 環境変数 GTFS_DICT_DIR > 既定(references/dictionaries)。"""
    if explicit:
        return Path(explicit)
    env = os.environ.get("GTFS_DICT_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "references" / "dictionaries"


def _legacy_national() -> Path:
    """旧・全国辞書（後方互換）。national.csv が無いときに使う。"""
    return Path(__file__).resolve().parent.parent / "references" / "data" / "stop_readings.csv"


def _safe_key(k: str | None) -> str:
    """自治体名/路線名/利用者名をファイル名に使うため、パス区切り等の危険文字だけを除去する。
    日本語はそのまま使える（現代のファイルシステムはUTF-8対応）。"""
    return re.sub(r'[\\/:*?"<>|]+', "_", (k or "").strip()).strip()


def load_csv_layer(path: str | Path) -> dict[str, dict]:
    """1つの辞書CSVを読み、{stop_name: {ja-Hrkt, en, source, ...}} を返す。無ければ空。"""
    d: dict[str, dict] = {}
    p = Path(path)
    if not p.exists():
        return d
    try:
        with p.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                nm = (r.get("stop_name") or "").strip()
                if not nm:
                    continue
                spec = {}
                for k in SPEC_FIELDS:
                    v = (r.get(k) or "").strip()
                    if v:
                        spec[k] = v
                if spec:
                    d[nm] = spec
    except Exception:
        # 壊れたCSVでも他の層は使えるように、読めない層は黙って空にする（正しく失敗）。
        return {}
    return d


def _layer_paths(root: Path, layer: str, keys: list, national_path: str | None) -> list[Path]:
    """ある層の読み込み対象CSVパス群を返す。"""
    if layer == "national":
        if national_path:
            return [Path(national_path)]
        p = root / "national.csv"
        return [p if p.exists() else _legacy_national()]
    out = []
    for k in keys:
        sk = _safe_key(k)
        if sk:
            out.append(root / layer / f"{sk}.csv")
    return out


def resolve(dict_dir: str | None = None, municipality: str | None = None,
            routes=None, user: str | None = None,
            national_path: str | None = None) -> dict[str, dict]:
    """階層辞書をマージして {stop_name: spec(+_layer)} を返す。
    低優先(national)→高優先(user)の順に上書きする。
    routes は文字列（カンマ区切り可）またはリスト。"""
    root = dict_root(dict_dir)
    if isinstance(routes, str):
        routes = [r.strip() for r in routes.split(",") if r.strip()]
    keymap = {
        "national": [None],
        "municipality": [municipality] if municipality else [],
        "route": list(routes or []),
        "user": [user] if user else [],
    }
    merged: dict[str, dict] = {}
    for layer in LAYER_ORDER:
        for path in _layer_paths(root, layer, keymap.get(layer, []), national_path):
            for nm, spec in load_csv_layer(path).items():
                s = dict(spec)
                s["_layer"] = layer          # 由来層＝出典（監査証跡の基盤）
                merged[nm] = s               # 後の層が上書き＝高優先
    return merged


def audit_log_path(dict_dir: str | None = None) -> Path:
    """監査ログ（変更履歴）の保存先。優先: 環境変数 GTFS_AUDIT_LOG > <dict_root>/audit_log.jsonl。
    追記専用JSONL（1行1変更）で、自治体が全案件の変更を一元的に監査できる。"""
    env = os.environ.get("GTFS_AUDIT_LOG")
    if env:
        return Path(env)
    return dict_root(dict_dir) / "audit_log.jsonl"


def append_audit(entries, dict_dir: str | None = None, ts: str | None = None) -> Path:
    """監査ログに変更を追記する（追記専用JSONL）。entries は dict または dict のリスト。
    各行の推奨キー: user, stop_name, field(ja-Hrkt/en), old, new, layer, source(human/ai),
    ai_model, note。ts 未指定なら現在時刻(ISO8601)を付与する。既存行は書き換えない（改ざん防止）。"""
    if isinstance(entries, dict):
        entries = [entries]
    if not entries:
        return audit_log_path(dict_dir)
    if ts is None:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
    path = audit_log_path(dict_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for e in entries:
            rec = {"ts": ts}
            rec.update({k: v for k, v in e.items() if v not in (None, "")})
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def write_layer_entry(dict_dir: str | None, layer: str, key: str | None,
                      stop_name: str, ja_hrkt: str = "", en: str = "",
                      updated_by: str = "", updated_at: str = "",
                      source: str = "human", note: str = "") -> Path:
    """指定した層の辞書CSVに1停留所分を追記/更新する（監査証跡つき）。
    将来の『修正の保存先を層で選ぶ』UI・出典管理の土台。既存の他行は保持する。"""
    root = dict_root(dict_dir)
    if layer == "national":
        path = (root / "national.csv")
    else:
        sk = _safe_key(key)
        if not sk:
            raise ValueError(f"{layer} 層には key（{layer}名）が必要です")
        path = root / layer / f"{sk}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = load_csv_layer(path)              # 既存を読み込み
    entry = {}
    if ja_hrkt:
        entry["ja-Hrkt"] = ja_hrkt
    if en:
        entry["en"] = en
    entry["source"] = source
    if updated_by:
        entry["updated_by"] = updated_by
    if updated_at:
        entry["updated_at"] = updated_at
    if note:
        entry["note"] = note
    rows[stop_name.strip()] = entry
    header = ["stop_name", *SPEC_FIELDS]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, lineterminator="\r\n")
        w.writeheader()
        for nm in sorted(rows):
            row = {"stop_name": nm}
            row.update({k: rows[nm].get(k, "") for k in SPEC_FIELDS})
            w.writerow(row)
    return path
