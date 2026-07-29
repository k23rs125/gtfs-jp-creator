# -*- coding: utf-8 -*-
"""app_context の「純関数」の仕様を固定する単体テスト。

対象:
  - build_subprocess_env : ⑤「背景地図の設定」のタイルURL(ss.tile_url)を、地図生成の
    子プロセスへ渡す環境に GTFS_TILE_URL として注入する純関数。未入力なら base_env を
    そのまま返す（＝既定オフライン・外部通信なし）。app.py の run() が呼び、生成HTML
    (map_view.html)の背景地図をUI設定に一致させる「glue」の中核。

※ ss / work_dir / autosave_* は Streamlit セッション状態に依存するため対象外。
"""
import app_context as C


class TestBuildSubprocessEnv:
    def test_tile_url_injected_as_gtfs_tile_url(self):
        base = {"PATH": "x", "PYTHONIOENCODING": "utf-8"}
        got = C.build_subprocess_env("https://t/{z}/{x}/{y}.png", base)
        assert got["GTFS_TILE_URL"] == "https://t/{z}/{x}/{y}.png"
        assert got["PATH"] == "x"                 # 既存の環境は保持

    def test_empty_returns_base_unchanged(self):
        base = {"PATH": "x"}
        # 空・空白・None はいずれも「未設定」＝ base をそのまま返す（既定オフライン）
        assert C.build_subprocess_env("", base) is base
        assert C.build_subprocess_env("   ", base) is base
        assert C.build_subprocess_env(None, base) is base

    def test_base_env_not_mutated(self):
        base = {"PATH": "x"}
        C.build_subprocess_env("https://t/{z}/{x}/{y}.png", base)
        assert "GTFS_TILE_URL" not in base       # 元 dict は変更しない（副作用なし）

    def test_whitespace_is_trimmed(self):
        got = C.build_subprocess_env("  https://t/{z}/{x}/{y}.png  ", {})
        assert got["GTFS_TILE_URL"] == "https://t/{z}/{x}/{y}.png"

    def test_existing_gtfs_tile_url_overridden_when_set(self):
        # 起動時 env に既定があっても、UI入力があればそれで上書き（UIの明示指定を優先）
        base = {"GTFS_TILE_URL": "https://old/{z}/{x}/{y}.png"}
        got = C.build_subprocess_env("https://new/{z}/{x}/{y}.png", base)
        assert got["GTFS_TILE_URL"] == "https://new/{z}/{x}/{y}.png"

    def test_empty_keeps_existing_env_default(self):
        # UI未入力なら、起動時 env の GTFS_TILE_URL（環境変数指定）をそのまま活かす
        base = {"GTFS_TILE_URL": "https://env/{z}/{x}/{y}.png"}
        assert C.build_subprocess_env("", base) is base
