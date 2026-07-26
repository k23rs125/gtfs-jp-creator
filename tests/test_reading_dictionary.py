# -*- coding: utf-8 -*-
"""reading_dictionary（読みの階層辞書＋監査ログ）の仕様を固定する単体テスト。

このファイルは「実行できる仕様書」を兼ねる。固定する主な仕様:
  1. 階層辞書の優先順位: 全国 < 自治体 < 路線 < 利用者（後の層が上書き）。
     resolve() は各読みの由来層 _layer（＝出典）も返す。
  2. 監査ログ: 追記専用JSONL（既存行は保持）。空値は記録しない。
  3. ファイル名キーのサニタイズ（日本語は保持・パス区切り等は除去）。
  4. 辞書ルート／監査ログ先の解決（環境変数 GTFS_DICT_DIR / GTFS_AUDIT_LOG 対応）。
  5. write_layer_entry: 指定層へ出典つきで追記／更新し、resolve に反映される。

いずれも tmp_path と環境変数の隔離で決定的に検証する（実ディレクトリに触れない）。
"""
import csv
import json

import pytest

import reading_dictionary as rd


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """全テストで GTFS_DICT_DIR / GTFS_AUDIT_LOG を未設定にし、実環境へ影響しないようにする。"""
    monkeypatch.delenv("GTFS_DICT_DIR", raising=False)
    monkeypatch.delenv("GTFS_AUDIT_LOG", raising=False)


def _write_csv(path, rows, header=("stop_name", "ja-Hrkt", "en")):
    """テスト用に辞書CSVを書く小道具。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(header))
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _setup_layers(root):
    """全国/自治体/路線/利用者の各層に、同じ停留所「本町」を別読みで用意する
    （優先順位の検証用）。全国のみの「壱町原」も置く。"""
    _write_csv(root / "national.csv",
               [{"stop_name": "壱町原", "ja-Hrkt": "いっちょうばる", "en": ""},
                {"stop_name": "本町", "ja-Hrkt": "ほんちょう", "en": ""}])
    _write_csv(root / "municipality" / "福岡県香春町.csv",
               [{"stop_name": "本町", "ja-Hrkt": "ほんまち-muni", "en": ""}])
    _write_csv(root / "route" / "香春線.csv",
               [{"stop_name": "本町", "ja-Hrkt": "ほんまち-route", "en": ""}])
    _write_csv(root / "user" / "佐藤.csv",
               [{"stop_name": "本町", "ja-Hrkt": "ほんまち-user", "en": ""}])


# ============================================================
# _safe_key: ファイル名に使えない文字だけを除去、日本語は保持
# ============================================================
class TestSafeKey:
    def test_keeps_japanese(self):
        assert rd._safe_key("福岡県香春町") == "福岡県香春町"

    @pytest.mark.parametrize("bad", ["a/b", "a\\b", "a:b", "a*b", "a?b",
                                     'a"b', "a<b", "a>b", "a|b"])
    def test_removes_path_dangerous_chars(self, bad):
        got = rd._safe_key(bad)
        assert "/" not in got and "\\" not in got
        assert got == "a_b"

    def test_empty_whitespace_none(self):
        assert rd._safe_key("") == ""
        assert rd._safe_key("   ") == ""
        assert rd._safe_key(None) == ""

    def test_strips_surrounding_space(self):
        assert rd._safe_key("  香春線  ") == "香春線"


# ============================================================
# load_csv_layer: CSV → {stop_name: {ja-Hrkt, en, source, ...}}
# ============================================================
class TestLoadCsvLayer:
    def test_reads_basic(self, tmp_path):
        p = tmp_path / "d.csv"
        _write_csv(p, [{"stop_name": "香春", "ja-Hrkt": "かわら", "en": "Kawara"}])
        assert rd.load_csv_layer(p) == {"香春": {"ja-Hrkt": "かわら", "en": "Kawara"}}

    def test_missing_file_returns_empty(self, tmp_path):
        assert rd.load_csv_layer(tmp_path / "none.csv") == {}

    def test_skips_empty_stop_name(self, tmp_path):
        p = tmp_path / "d.csv"
        _write_csv(p, [{"stop_name": "", "ja-Hrkt": "x"},
                       {"stop_name": "香春", "ja-Hrkt": "かわら"}])
        assert list(rd.load_csv_layer(p)) == ["香春"]

    def test_empty_values_are_dropped(self, tmp_path):
        p = tmp_path / "d.csv"
        _write_csv(p, [{"stop_name": "香春", "ja-Hrkt": "かわら", "en": ""}])
        assert "en" not in rd.load_csv_layer(p)["香春"]  # 空欄は入れない

    def test_reads_provenance_columns(self, tmp_path):
        p = tmp_path / "d.csv"
        header = ("stop_name", "ja-Hrkt", "en", "source", "updated_by", "updated_at", "note")
        _write_csv(p, [{"stop_name": "長木", "ja-Hrkt": "おさぎ", "en": "",
                        "source": "human", "updated_by": "佐藤",
                        "updated_at": "2026-07-25", "note": "現地確認"}], header=header)
        d = rd.load_csv_layer(p)["長木"]
        assert d["source"] == "human" and d["updated_by"] == "佐藤"
        assert d["note"] == "現地確認"


# ============================================================
# resolve: 階層の優先順位（全国 < 自治体 < 路線 < 利用者）
# ============================================================
class TestResolvePriority:
    def test_national_only(self, tmp_path):
        _setup_layers(tmp_path)
        m = rd.resolve(dict_dir=str(tmp_path))
        assert m["壱町原"]["ja-Hrkt"] == "いっちょうばる"
        assert m["壱町原"]["_layer"] == "national"
        assert m["本町"]["ja-Hrkt"] == "ほんちょう"       # 全国の読み
        assert m["本町"]["_layer"] == "national"

    def test_municipality_overrides_national(self, tmp_path):
        _setup_layers(tmp_path)
        m = rd.resolve(dict_dir=str(tmp_path), municipality="福岡県香春町")
        assert m["本町"]["ja-Hrkt"] == "ほんまち-muni"
        assert m["本町"]["_layer"] == "municipality"
        assert m["壱町原"]["_layer"] == "national"        # 全国のみの停留所は残る

    def test_route_over_municipality(self, tmp_path):
        _setup_layers(tmp_path)
        m = rd.resolve(dict_dir=str(tmp_path), municipality="福岡県香春町", routes="香春線")
        assert m["本町"]["ja-Hrkt"] == "ほんまち-route"
        assert m["本町"]["_layer"] == "route"

    def test_user_is_top_priority(self, tmp_path):
        _setup_layers(tmp_path)
        m = rd.resolve(dict_dir=str(tmp_path), municipality="福岡県香春町",
                       routes="香春線", user="佐藤")
        assert m["本町"]["ja-Hrkt"] == "ほんまち-user"    # 最上位＝利用者
        assert m["本町"]["_layer"] == "user"

    def test_routes_accepts_string_and_list(self, tmp_path):
        _setup_layers(tmp_path)
        assert rd.resolve(dict_dir=str(tmp_path), routes="香春線") == \
               rd.resolve(dict_dir=str(tmp_path), routes=["香春線"])

    def test_missing_key_skips_layer(self, tmp_path):
        _setup_layers(tmp_path)
        # 自治体キー未指定なら自治体層は読まれず、全国の読みが残る
        m = rd.resolve(dict_dir=str(tmp_path), municipality=None)
        assert m["本町"]["_layer"] == "national"

    def test_national_path_override(self, tmp_path):
        alt = tmp_path / "alt_national.csv"
        _write_csv(alt, [{"stop_name": "香春", "ja-Hrkt": "かわら-alt", "en": ""}])
        m = rd.resolve(dict_dir=str(tmp_path / "empty"), national_path=str(alt))
        assert m["香春"]["ja-Hrkt"] == "かわら-alt"

    def test_multiple_routes_merged(self, tmp_path):
        _write_csv(tmp_path / "national.csv", [{"stop_name": "A", "ja-Hrkt": "a0", "en": ""}])
        _write_csv(tmp_path / "route" / "路線1.csv", [{"stop_name": "A", "ja-Hrkt": "a1", "en": ""}])
        _write_csv(tmp_path / "route" / "路線2.csv", [{"stop_name": "B", "ja-Hrkt": "b2", "en": ""}])
        m = rd.resolve(dict_dir=str(tmp_path), routes="路線1,路線2")
        assert m["A"]["ja-Hrkt"] == "a1" and m["B"]["ja-Hrkt"] == "b2"


# ============================================================
# dict_root / audit_log_path: パス解決（環境変数対応）
# ============================================================
class TestPathResolution:
    def test_dict_root_explicit_wins(self, tmp_path):
        assert rd.dict_root(str(tmp_path)) == tmp_path

    def test_dict_root_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GTFS_DICT_DIR", str(tmp_path))
        assert rd.dict_root() == tmp_path

    def test_dict_root_default_is_dictionaries(self):
        assert rd.dict_root().name == "dictionaries"

    def test_audit_log_default(self, tmp_path):
        assert rd.audit_log_path(str(tmp_path)) == tmp_path / "audit_log.jsonl"

    def test_audit_log_env_override(self, tmp_path, monkeypatch):
        target = tmp_path / "custom.jsonl"
        monkeypatch.setenv("GTFS_AUDIT_LOG", str(target))
        assert rd.audit_log_path(str(tmp_path)) == target


# ============================================================
# append_audit: 追記専用JSONL（改ざん防止・空値は記録しない）
# ============================================================
class TestAppendAudit:
    def test_appends_one_line_with_ts(self, tmp_path):
        rd.append_audit({"user": "佐藤", "stop_name": "長木", "new": "おさぎ"},
                        dict_dir=str(tmp_path), ts="2026-07-25T00:00:00")
        lines = rd.audit_log_path(str(tmp_path)).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["ts"] == "2026-07-25T00:00:00"
        assert rec["user"] == "佐藤" and rec["new"] == "おさぎ"

    def test_list_and_append_only(self, tmp_path):
        rd.append_audit({"stop_name": "A", "new": "a"}, dict_dir=str(tmp_path), ts="t1")
        rd.append_audit([{"stop_name": "B", "new": "b"}, {"stop_name": "C", "new": "c"}],
                        dict_dir=str(tmp_path), ts="t2")
        lines = rd.audit_log_path(str(tmp_path)).read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(x)["stop_name"] for x in lines] == ["A", "B", "C"]  # 既存行を保持

    def test_empty_entries_noop(self, tmp_path):
        rd.append_audit([], dict_dir=str(tmp_path))
        assert not rd.audit_log_path(str(tmp_path)).exists()

    def test_omits_empty_and_none_values(self, tmp_path):
        rd.append_audit({"user": "佐藤", "note": "", "ai_model": None, "new": "x"},
                        dict_dir=str(tmp_path), ts="t")
        rec = json.loads(rd.audit_log_path(str(tmp_path)).read_text(encoding="utf-8").strip())
        assert "note" not in rec and "ai_model" not in rec   # 空/Noneは記録しない
        assert rec["user"] == "佐藤" and rec["new"] == "x"


# ============================================================
# write_layer_entry: 層へ出典つきで追記／更新し resolve に反映
# ============================================================
class TestWriteLayerEntry:
    def test_writes_with_provenance(self, tmp_path):
        rd.write_layer_entry(str(tmp_path), "user", "佐藤", "長木",
                             ja_hrkt="おさぎ", source="human", updated_by="佐藤",
                             updated_at="2026-07-25", note="現地確認")
        d = rd.load_csv_layer(tmp_path / "user" / "佐藤.csv")["長木"]
        assert d["ja-Hrkt"] == "おさぎ" and d["updated_by"] == "佐藤"
        assert d["note"] == "現地確認" and d["source"] == "human"

    def test_national_writes_to_national_csv(self, tmp_path):
        rd.write_layer_entry(str(tmp_path), "national", None, "壱町原", ja_hrkt="いっちょうばる")
        assert (tmp_path / "national.csv").exists()

    def test_missing_key_raises(self, tmp_path):
        with pytest.raises(ValueError):
            rd.write_layer_entry(str(tmp_path), "user", "", "長木", ja_hrkt="おさぎ")

    def test_update_preserves_other_entries(self, tmp_path):
        rd.write_layer_entry(str(tmp_path), "user", "佐藤", "A", ja_hrkt="a1")
        rd.write_layer_entry(str(tmp_path), "user", "佐藤", "B", ja_hrkt="b1")
        rd.write_layer_entry(str(tmp_path), "user", "佐藤", "A", ja_hrkt="a2")   # 上書き更新
        d = rd.load_csv_layer(tmp_path / "user" / "佐藤.csv")
        assert d["A"]["ja-Hrkt"] == "a2" and d["B"]["ja-Hrkt"] == "b1"

    def test_roundtrip_reflected_in_resolve(self, tmp_path):
        # 全国では「ながき」だが、利用者層に「おさぎ」を書くと resolve は利用者を優先する
        _write_csv(tmp_path / "national.csv", [{"stop_name": "長木", "ja-Hrkt": "ながき", "en": ""}])
        rd.write_layer_entry(str(tmp_path), "user", "佐藤", "長木", ja_hrkt="おさぎ")
        m = rd.resolve(dict_dir=str(tmp_path), user="佐藤")
        assert m["長木"]["ja-Hrkt"] == "おさぎ" and m["長木"]["_layer"] == "user"
