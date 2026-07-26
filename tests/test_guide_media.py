# -*- coding: utf-8 -*-
"""guide_media（使い方ガイド動画）の純関数の仕様を固定する単体テスト。

対象:
  - norm_video_name : ファイル名の表記ゆれを吸収した比較用文字列（拡張子除去・
                      「の」/空白/記号の除去・小文字化）。撮り直しで名前が揺れても
                      同じ動画として扱えるようにするための正規化。
  - guide_video     : 動画パスの探索（完全一致→表記ゆれ一致→無ければ None）。
                      探索先 GUIDE_VIDEO_DIRS は monkeypatch で一時フォルダに差し替える。
  - video_length_label : 読めない/存在しないファイルは "" を返す（graceful）。

render_guide_video は st に依存するため対象外。
"""
import guide_media as gm


# ============================================================
# norm_video_name: 表記ゆれ吸収
# ============================================================
class TestNormVideoName:
    def test_strips_extension(self):
        assert gm.norm_video_name("時刻表.mp4") == gm.norm_video_name("時刻表")

    def test_removes_no_and_spaces(self):
        # 「時刻表の読み取り」と「時刻表読み取り」は同じ扱い
        assert gm.norm_video_name("時刻表の読み取り.mp4") == \
               gm.norm_video_name("時刻表読み取り.mp4")
        assert gm.norm_video_name("時刻表の読み取り.mp4") == "時刻表読み取り"

    def test_removes_symbols_and_underscores(self):
        assert gm.norm_video_name("a_b-c・d.mp4") == "abcd"

    def test_lowercases_ascii(self):
        assert gm.norm_video_name("Guide.MP4") == "guide"

    def test_fullwidth_and_halfwidth_space(self):
        assert gm.norm_video_name("あ い　う.mp4") == "あいう"


# ============================================================
# guide_video: 完全一致 → 表記ゆれ一致 → None
# ============================================================
class TestGuideVideo:
    def _setup(self, tmp_path, monkeypatch, files):
        d = tmp_path / "videos"
        d.mkdir()
        for name in files:
            (d / name).write_bytes(b"x")   # 中身は問わない
        monkeypatch.setattr(gm, "GUIDE_VIDEO_DIRS", [d])
        return d

    def test_exact_match(self, tmp_path, monkeypatch):
        d = self._setup(tmp_path, monkeypatch, ["①時刻表.mp4"])
        assert gm.guide_video("①時刻表.mp4") == d / "①時刻表.mp4"

    def test_fuzzy_match_by_normalized_name(self, tmp_path, monkeypatch):
        # 探しているのは「の」入り、実ファイルは「の」なし → 表記ゆれで一致
        d = self._setup(tmp_path, monkeypatch, ["時刻表読み取り.mp4"])
        assert gm.guide_video("時刻表の読み取り.mp4") == d / "時刻表読み取り.mp4"

    def test_not_found_returns_none(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, ["①時刻表.mp4"])
        assert gm.guide_video("存在しない動画.mp4") is None

    def test_missing_directory_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gm, "GUIDE_VIDEO_DIRS", [tmp_path / "no_such_dir"])
        assert gm.guide_video("なんでも.mp4") is None


# ============================================================
# video_length_label: 読めないファイルは "" を返す（graceful）
# ============================================================
class TestVideoLengthLabel:
    def test_missing_file_returns_empty(self, tmp_path):
        assert gm.video_length_label(tmp_path / "none.mp4") == ""

    def test_non_mp4_content_returns_empty(self, tmp_path):
        p = tmp_path / "notvideo.mp4"
        p.write_bytes(b"this is not an mp4")   # mvhd ボックスが無い
        assert gm.video_length_label(p) == ""
