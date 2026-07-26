# -*- coding: utf-8 -*-
"""地図（folium）ヘルパー（app.py から分離・保守性向上）。

責務: 座標確認・経路編集の地図生成と、基盤タイルURLの決定。
基盤タイルは既定オフライン（外部通信なし＝閉域網・自治体運用対応）。
参照するのは st.session_state と環境変数 GTFS_TILE_URL のみ（WORK/SCRIPTS 非依存）。
app.py からは従来名（先頭 _）でエイリアス import して呼ぶ＝挙動不変。
License: Apache 2.0
"""
import os

import streamlit as st


def ss():
    return st.session_state


def map_tile_url():
    """地図の基盤タイルURLを返す。既定は None（オフライン＝タイル取得しない）。
    閉域網・自治体運用のため、外部タイル(OpenStreetMap等)は既定で取得しない。
    背景地図が必要な場合のみ、環境変数 GTFS_TILE_URL か ⑤ の設定欄で
    タイルURL（例: 自前タイルサーバや https://tile.openstreetmap.org/{z}/{x}/{y}.png）を指定する。"""
    url = (str(ss().get("tile_url", "")) or os.environ.get("GTFS_TILE_URL", "")).strip()
    return url or None


def folium_map(**kwargs):
    """基盤タイルを既定オフライン(None=灰背景＋マーカーのみ)にした folium.Map を返す。
    タイルURLが設定されている時だけ、その基盤地図を表示する（外部通信は利用者の明示指定時のみ）。"""
    import folium as _fl
    url = map_tile_url()
    if url:
        return _fl.Map(tiles=url, attr="(指定タイル)", **kwargs)
    return _fl.Map(tiles=None, **kwargs)


def build_map_points(crows, confirmed, sid_of, label_of):
    """⑤座標確認マップのマーカー点リストと tooltip→stop_id の対応を組み立てる。

    戻り値: (pts, tip2id)。pts = [(tip, lat, lon, conf, reason), ...]。
    確定座標(confirmed)があればそれを優先、無ければ stop_lat/lon を使う（座標なしは除外）。
    tooltip は一意化する（同名の行き/帰り等が重複する時は末尾に [stop_id] を付与）。
    sid_of(r): 行の stop_id を返す / label_of(r): 表示ラベルを返す /
    confirmed: {stop_id: (lat, lon, ...)}。純関数（副作用なし・データ整形と表示の分離）。
    """
    pts, tip2id = [], {}
    for r in crows:
        sid = sid_of(r)
        if sid in confirmed:
            la, lo, conf = confirmed[sid][0], confirmed[sid][1], "確定"
        elif (r.get("stop_lat") or "").strip():
            la, lo, conf = float(r["stop_lat"]), float(r["stop_lon"]), r["confidence"]
        else:
            continue
        tip = label_of(r)
        if tip in tip2id:
            tip = f"{tip}[{sid}]"
        tip2id[tip] = sid
        pts.append((tip, la, lo, conf, r.get("reason", "")))
    return pts, tip2id


def count_confidence(crows, confirmed, sid_of):
    """⑤の信頼度メトリクス（確定・要確認・未補完 の件数）を集計する。

    確定座標(confirmed)にある停留所は『確定』、それ以外は行の confidence を用いる
    （app.py 側の eff_conf と同一ロジック）。戻り値: (n_ok, n_rev, n_non)。
    純関数（副作用なし・集計処理と UI 表示処理の分離）。
    sid_of(r): 行の stop_id を返す / confirmed: {stop_id: (...)}。
    """
    def eff(r):
        return "確定" if sid_of(r) in confirmed else r["confidence"]
    n_ok = sum(1 for r in crows if eff(r) == "確定")
    n_rev = sum(1 for r in crows if eff(r) == "要確認")
    n_non = sum(1 for r in crows if eff(r) == "未補完")
    return n_ok, n_rev, n_non


def marker_popup_html(tip, conf, reason=""):
    """座標確認マップ(⑤)のピンのポップアップ HTML を組み立てる（純粋な表示ロジック・副作用なし）。

    ポップアップは幅を明示する（既定だと日本語の長文が1文字ずつ改行されて読めないため）。
    conf は 確定／要確認／未補完 のいずれかで、色分け表示に使う。
    """
    from html import escape as _esc
    _cbadge = {"確定": "#2e7d32", "要確認": "#e08a1e", "未補完": "#c62828"}
    return (
        "<div style='width:230px;white-space:normal;font-size:12px;line-height:1.55;"
        "word-break:break-word'>"
        f"<div style='font-weight:700;margin-bottom:3px'>{_esc(tip)}</div>"
        f"<span style='color:{_cbadge.get(conf, '#555')};font-weight:700'>{_esc(conf)}</span>"
        + (f"<div style='color:#333;margin-top:2px'>{_esc(reason)}</div>" if reason else "")
        + "</div>")
