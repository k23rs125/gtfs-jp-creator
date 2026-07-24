# -*- coding: utf-8 -*-
"""使い方ガイドの操作動画ヘルパー（app.py から分離・保守性向上）。

責務: ガイド動画の探索（表記ゆれ吸収）・再生時間の表示・折りたたみ埋め込み。
参照するのは streamlit と同梱の動画フォルダのみ（WORK/SCRIPTS/セッション状態 非依存）。
app.py からは従来名（先頭 _）でエイリアス import して呼ぶ＝挙動不変。
License: Apache 2.0
"""
import re
from pathlib import Path

import streamlit as st

# 使い方ガイドの操作動画。リポジトリ内 app/guide_videos/ を優先し、無ければ
# 稲ゼミ直下の「使い方ガイド」フォルダを見る（動画は容量が大きくGit管理しないため）。
_REPO = Path(__file__).resolve().parent.parent
GUIDE_VIDEO_DIRS = [_REPO / "app" / "guide_videos", _REPO.parent / "使い方ガイド"]

_VIDEO_LEN_CACHE = {}    # (パス, 更新時刻, サイズ) -> 表示用の長さ。再実行のたびに読み直さない


def norm_video_name(name):
    """動画ファイル名の表記ゆれを吸収した比較用の文字列を返す。

    「時刻表の読み取り」と「時刻表読み取り」のように、助詞や記号の有無だけが違う
    ファイル名を同じものとして扱う（撮り直しで名前が揺れても動画が消えないように）。
    """
    return re.sub(r"[の\s　_＿\-ー、,．.･・]+", "", str(name).rsplit(".", 1)[0]).lower()


def guide_video(filename):
    """使い方ガイドの動画パスを返す。見つからなければ None（ガイド本文はそのまま出す）。

    撮り直しでファイル名が少し変わっても動画が消えないよう、完全一致で見つからなければ
    「の」や空白を無視した名前で照合する。
    """
    for _d in GUIDE_VIDEO_DIRS:
        _p = _d / filename
        if _p.exists():
            return _p
    _key = norm_video_name(filename)
    for _d in GUIDE_VIDEO_DIRS:
        if not _d.is_dir():
            continue
        for _f in sorted(_d.glob("*.mp4")):
            if norm_video_name(_f.name) == _key:
                return _f
    return None


def video_length_label(path):
    """mp4 の再生時間を「20秒」「1分24秒」の形で返す。読めなければ空文字。

    長さを手で書くと動画を撮り直すたびにずれるため、ファイルから読む。
    mvhd ボックスは moov の位置により先頭にも末尾にもあるので前後1MBずつ見る。
    差し替えを検知できるよう、更新時刻とサイズも含めた鍵でキャッシュする。
    """
    try:
        import struct
        _stt = path.stat()
        _ck = (str(path), _stt.st_mtime_ns, _stt.st_size)
        if _ck in _VIDEO_LEN_CACHE:
            return _VIDEO_LEN_CACHE[_ck]
        _sz = _stt.st_size
        with path.open("rb") as _f:
            _bufs = [_f.read(min(_sz, 1 << 20))]
            if _sz > (1 << 20):
                _f.seek(_sz - (1 << 20))
                _bufs.append(_f.read())
        for _b in _bufs:
            _i = _b.find(b"mvhd")
            if _i < 0:
                continue
            if _b[_i + 4] == 0:
                _ts, _dur = struct.unpack(">II", _b[_i + 16:_i + 24])
            else:
                _ts, _dur = struct.unpack(">IQ", _b[_i + 24:_i + 36])
            if _ts:
                _sec = int(_dur / _ts)          # 再生器の表示に合わせて切り捨て
                _m, _s = divmod(_sec, 60)
                _VIDEO_LEN_CACHE[_ck] = f"{_m}分{_s}秒" if _m else f"{_s}秒"
                return _VIDEO_LEN_CACHE[_ck]
        _VIDEO_LEN_CACHE[_ck] = ""      # 読めないファイルを毎回読み直さない
    except Exception:
        pass
    return ""


def render_guide_video(*filenames):
    """操作動画を折りたたみで出す。開くまで読み込まれないので初期表示が重くならない。

    候補を複数渡すと、見つかった最初のものを使う。⑤⑥のように統合動画から
    個別動画へ差し替える途中でも表示が途切れないよう、優先順で候補を並べる。
    """
    _p = next((v for v in (guide_video(f) for f in filenames) if v), None)
    if not _p:
        return
    _len = video_length_label(_p)
    with st.expander("▶️ 動画で見る" + (f"（{_len}）" if _len else "")):
        st.video(str(_p))
