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
