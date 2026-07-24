# -*- coding: utf-8 -*-
"""日付入力ウィジェット（app.py から分離・保守性向上）。

責務: 「年・月・日」「月・日」を分かりやすい別欄で入力させる Streamlit ウィジェット。
セッション状態や WORK/SCRIPTS には依存しない（streamlit のみ）＝再利用・テストが容易。
app.py からは従来名（先頭 _）でエイリアス import して呼ぶ＝挙動不変。
License: Apache 2.0
"""
import datetime

import streamlit as st


def ymd_date_input(label, prefill, key, help_text=""):
    """「年・月・日」を別々の欄で入力させ、内部で使う "YYYYMMDD" の文字列を返す。

    YYYYMMDD や YYYY/MM/DD といった表記は Y/M/D が何を指すか伝わらないため、
    画面には「年」「月」「日」としか出さない。保存は従来どおり8桁文字列に揃えるので、
    後続の処理（GTFSのcalendar等）は変更しない。
    prefill は検出済みの "YYYYMMDD"（空・不正なら未入力で出す）。
    """
    _y = _m = _d = None
    _s = str(prefill or "").strip()
    if len(_s) == 8 and _s.isdigit():
        try:
            _dt = datetime.datetime.strptime(_s, "%Y%m%d").date()
            _y, _m, _d = _dt.year, _dt.month, _dt.day
        except ValueError:
            _y = _m = _d = None
    st.markdown(f"**{label}**" + (f"　<span style='color:#5b6b7b;font-size:0.85em'>{help_text}</span>"
                                  if help_text else ""), unsafe_allow_html=True)
    _c1, _c2, _c3, _c4 = st.columns([2, 1.4, 1.4, 4])
    _yy = _c1.number_input("年", min_value=2000, max_value=2040, value=_y, step=1,
                           key=f"{key}_y", placeholder="2026")
    _mm = _c2.number_input("月", min_value=1, max_value=12, value=_m, step=1,
                           key=f"{key}_m", placeholder="4")
    _dd = _c3.number_input("日", min_value=1, max_value=31, value=_d, step=1,
                           key=f"{key}_d", placeholder="1")
    if _yy is None and _mm is None and _dd is None:
        return ""
    if _yy is None or _mm is None or _dd is None:
        st.warning(f"{label}：**年・月・日をすべて**入れてください。")
        return ""
    try:                                  # 2月30日のような存在しない日は受け付けない
        datetime.date(int(_yy), int(_mm), int(_dd))
    except ValueError:
        st.error(f"{label}：{int(_yy)}年{int(_mm)}月{int(_dd)}日 は存在しない日付です。")
        return ""
    return f"{int(_yy):04d}{int(_mm):02d}{int(_dd):02d}"


def md_input(label, default_m, default_d, key):
    """「月・日」だけの入力（年末年始・お盆の範囲用）。"MM-DD" の文字列を返す。"""
    _c1, _c2, _c3 = st.columns([1.4, 1.4, 5])
    _c1.markdown(f"<div style='padding-top:6px'>{label}</div>", unsafe_allow_html=True)
    _mm = _c2.number_input("月", min_value=1, max_value=12, value=default_m, step=1, key=f"{key}_m")
    _dd = _c3.number_input("日", min_value=1, max_value=31, value=default_d, step=1, key=f"{key}_d")
    return f"{int(_mm):02d}-{int(_dd):02d}"
