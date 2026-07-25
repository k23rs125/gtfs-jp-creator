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
