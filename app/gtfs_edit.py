# -*- coding: utf-8 -*-
"""GTFS/CSV ファイルの編集ユーティリティ（app.py から分離）。

責務: 出力済みの GTFS-JP ファイル（stops.txt / translations.txt / shapes.txt /
trips.txt 等）を、パスを受け取って直接書き換える純粋関数群。

設計方針（app.py 分割・保守性向上）:
    - Streamlit のセッション状態や UI に一切依存しない（st/ss/WORK/SCRIPTS 不使用）。
    - 入力はファイルパス、副作用はそのファイルの書き換えのみ＝単体テスト・再利用が容易。
    - app.py からは従来名（先頭アンダースコア）でエイリアス import して呼び出す
      （呼び出し側を変えない＝挙動不変）。
License: Apache 2.0
"""
from __future__ import annotations

import csv
from math import asin, cos, radians, sin, sqrt
from pathlib import Path


def rewrite_csv_field(path, field, rename_map, only_table=None):
    """CSV(path)の列 field の値を rename_map(old→new)で置換して書き戻す。
    only_table 指定時は table_name==only_table の行だけ対象。BOM/改行は踏襲。"""
    p = Path(path)
    if not p.exists() or not rename_map:
        return
    raw = p.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    with p.open(encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
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
        w = csv.DictWriter(f, fieldnames=fns, lineterminator=newline)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fns})


def update_translations_rows(trans_path, updates):
    """translations.txt の (table,field,value) 行の言語別 translation を更新/追加/削除する。
    updates: [(table_name, field_name, field_value, {"ja-Hrkt":..,"en":..}), ...]
    翻訳が空文字ならその言語行を削除、値があれば無ければ新規追加する。"""
    p = Path(trans_path)
    if not p.exists() or not updates:
        return
    with p.open(encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
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
        w = csv.DictWriter(f, fieldnames=fns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in fns})


def write_shape(shp_path, shape_id, coords):
    """shapes.txt の shape_id の点を coords[(lat,lon)] で置き換える（他shapeは保持）。距離も再計算。"""
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
        rows = [r for r in csv.DictReader(p.open(encoding="utf-8-sig")) if r.get("shape_id") != shape_id]
    d = 0.0
    for i, (la, lo) in enumerate(coords):
        if i > 0:
            d += _hav(coords[i - 1], coords[i])
        rows.append({"shape_id": shape_id, "shape_pt_lat": f"{la:.6f}", "shape_pt_lon": f"{lo:.6f}",
                     "shape_pt_sequence": str(i), "shape_dist_traveled": f"{d:.2f}"})
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fns})


def assign_trip_shape(trp_path, rid, did, shape_id):
    """trips(.with_shapes).txt の route+direction の行に shape_id を割り当てる。"""
    p = Path(trp_path)
    rd = csv.DictReader(p.open(encoding="utf-8-sig"))
    fns = rd.fieldnames or []
    rows = list(rd)
    if "shape_id" not in fns:
        fns = fns + ["shape_id"]
    for r in rows:
        if r.get("route_id") == rid and (r.get("direction_id") or "0") == did:
            r["shape_id"] = shape_id
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fns})
