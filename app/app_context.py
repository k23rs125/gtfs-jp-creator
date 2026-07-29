# -*- coding: utf-8 -*-
"""アプリ共有ランタイム状態のアクセサ（app.py 分割・グローバル依存の低減）。

責務: Streamlit のセッション状態と作業ディレクトリへの「最小限」のアクセサを提供する。
分離したヘルパーモジュール（reading_ui など）が、app.py のモジュールグローバル
（ss()/WORK）へ直接依存せず、この薄い層を経由して同じ実体へアクセスできるようにする。

※ st.session_state はセッション単位のシングルトンなので、どのモジュールから参照しても
   app.py と同一の実体。したがって本モジュールと app.py の ss()/WORK は常に一致する。
License: Apache 2.0
"""
import tempfile
from pathlib import Path

import streamlit as st


def ss():
    """Streamlit セッション状態（app.py の ss() と同一実体）。"""
    return st.session_state


def work_dir() -> Path:
    """このセッションの作業ディレクトリ（app.py の WORK と同一）。
    app.py 起動時に ss().work を設定済み。未設定時は一時ディレクトリにフォールバックする。"""
    w = st.session_state.get("work")
    return Path(w) if w else Path(tempfile.gettempdir())


# --- 協調同期・永続化(collab.py)で使う共有ランタイム値のアクセサ（最小限）---
# いずれも app.py のグローバル(SID/USER_ID/AUTOSAVE_DIR/AUTOSAVE_FILE)と同一値を返す。
def sid() -> str:
    """共有プロジェクトコード（app.py の SID と同一）。全員が同じコードで保存を共有。"""
    return str(st.session_state.get("sid", "") or "")


def user_id() -> str:
    """セッションごとの利用者ID（app.py の USER_ID と同一）。担当ロックの持ち主判定に使う。"""
    return str(st.session_state.get("user_id", "") or "")


def autosave_dir() -> Path:
    """自動保存の安定フォルダ（app.py の AUTOSAVE_DIR と同一）。セッション用tempとは別。"""
    return Path.home() / ".gtfs_jp_app"


def autosave_file() -> Path:
    """このプロジェクト(sid)の共有保存ファイル（app.py の AUTOSAVE_FILE と同一）。"""
    return autosave_dir() / f"session_{sid()}.json"


# --- 地図生成の子プロセスへ渡す環境の構築（背景地図のUI設定を生成HTMLへ反映する）---
def build_subprocess_env(tile_url, base_env):
    """地図生成(make_map_view / ビューア)の子プロセスに渡す環境を作る純関数。

    ⑤「背景地図の設定」で入力したタイルURL(tile_url)があれば、それを GTFS_TILE_URL として
    base_env に上書き注入する。未入力(空/空白/None)なら base_env をそのまま返す＝既定オフライン
    （閉域網では外部通信なし）。UI設定と生成HTML(map_view.html)の背景地図を一致させるための土台。
    副作用なし・Streamlit非依存（base_env は変更せず、必要時のみ新しい dict を返す）。
    """
    t = str(tile_url or "").strip()
    if t:
        return {**base_env, "GTFS_TILE_URL": t}
    return base_env
