# -*- coding: utf-8 -*-
"""reading_ui の「純関数」の仕様を固定する単体テスト。

対象は session_state 非依存の純関数のみ:
  - parse_route_reading_entries : translations 行 → (other, oorder)（対象=routes/agency/trips）
  - build_route_reading_rows    : (order, other) → 表示行（種類/項目のラベル変換）
  - build_audit_rows            : 監査ログ列 → 表示行（逆順・保存層の日本語化）
  - reading_suspicious          : 読みが怪しい（漢字残り等）か

※ reading_overrides / auto_reading / reading_source / save_reading_layer / build_reading_rows は
   session_state（Streamlit）に依存するため、本ファイルの対象外（AppTest 側で別途検証済み）。
"""
import reading_ui as R


# ============================================================
# parse_route_reading_entries: 対象テーブルの読みを (other, oorder) へ
# ============================================================
class TestParseRouteReadingEntries:
    def test_captures_target_tables_only(self):
        trows = [
            {"table_name": "routes", "field_name": "route_long_name",
             "field_value": "香春線", "language": "ja-Hrkt", "translation": "かわらせん"},
            {"table_name": "agency", "field_name": "agency_name",
             "field_value": "太陽交通", "language": "ja-Hrkt", "translation": "たいようこうつう"},
            {"table_name": "trips", "field_name": "trip_headsign",
             "field_value": "役場ゆき", "language": "ja-Hrkt", "translation": "やくばゆき"},
            {"table_name": "stops", "field_name": "stop_name",   # 対象外
             "field_value": "香春", "language": "ja-Hrkt", "translation": "かわら"},
        ]
        other, oorder = R.parse_route_reading_entries(trows)
        assert [k[2] for k in oorder] == ["香春線", "太陽交通", "役場ゆき"]   # stops は除外・出現順
        assert other[("routes", "route_long_name", "香春線")]["ja-Hrkt"] == "かわらせん"

    def test_merges_ja_hrkt_and_en_for_same_key(self):
        key = ("routes", "route_long_name", "香春線")
        trows = [
            {"table_name": "routes", "field_name": "route_long_name",
             "field_value": "香春線", "language": "ja-Hrkt", "translation": "かわらせん"},
            {"table_name": "routes", "field_name": "route_long_name",
             "field_value": "香春線", "language": "en", "translation": "Kawara Line"},
        ]
        other, oorder = R.parse_route_reading_entries(trows)
        assert oorder == [key]   # 同じ値は1キーに集約
        assert other[key] == {"ja-Hrkt": "かわらせん", "en": "Kawara Line"}

    def test_other_language_creates_key_without_reading(self):
        # ja(原文)行はキーは作るが ja-Hrkt/en は入れない
        trows = [{"table_name": "routes", "field_name": "route_long_name",
                  "field_value": "香春線", "language": "ja", "translation": "香春線"}]
        other, oorder = R.parse_route_reading_entries(trows)
        assert oorder == [("routes", "route_long_name", "香春線")]
        assert other[("routes", "route_long_name", "香春線")] == {}

    def test_empty_input(self):
        assert R.parse_route_reading_entries([]) == ({}, [])


# ============================================================
# build_route_reading_rows: (order, other) → 表示行（ラベル変換）
# ============================================================
class TestBuildRouteReadingRows:
    def test_label_translation(self):
        order = [("routes", "route_long_name", "香春線"),
                 ("agency", "agency_name", "太陽交通"),
                 ("trips", "trip_headsign", "役場ゆき")]
        other = {order[0]: {"ja-Hrkt": "かわらせん", "en": "Kawara Line"},
                 order[1]: {"ja-Hrkt": "たいようこうつう"},
                 order[2]: {}}
        rows = R.build_route_reading_rows(order, other)
        assert rows[0] == {"種類": "路線", "項目": "路線名", "名前": "香春線",
                           "ふりがな(ja-Hrkt)": "かわらせん", "英語(en)": "Kawara Line"}
        assert rows[1]["種類"] == "事業者" and rows[1]["項目"] == "事業者名"
        assert rows[2]["種類"] == "行き先" and rows[2]["項目"] == "行き先表示"
        assert rows[2]["ふりがな(ja-Hrkt)"] == "" and rows[2]["英語(en)"] == ""   # 欠損は空

    def test_unknown_table_field_passthrough(self):
        order = [("unknown_tbl", "unknown_fld", "X")]
        other = {order[0]: {"ja-Hrkt": "えっくす"}}
        rows = R.build_route_reading_rows(order, other)
        assert rows[0]["種類"] == "unknown_tbl" and rows[0]["項目"] == "unknown_fld"

    def test_empty_order(self):
        assert R.build_route_reading_rows([], {}) == []


# ============================================================
# build_audit_rows: 監査ログ列 → 表示行（逆順・保存層の日本語化）
# ============================================================
class TestBuildAuditRows:
    def test_reversed_order_newest_first(self):
        entries = [{"stop_name": "A"}, {"stop_name": "B"}, {"stop_name": "C"}]
        rows = R.build_audit_rows(entries)
        assert [r["停留所"] for r in rows] == ["C", "B", "A"]   # 新しい変更が上

    def test_layer_label_and_fields(self):
        entries = [{"ts": "2026-07-25T00:00", "user": "佐藤", "stop_name": "長木",
                    "field": "ja-Hrkt", "old": "ちょうき", "new": "おさぎ",
                    "layer": "municipality", "source": "ai", "ai_model": "claude-sonnet-4-6"}]
        r = R.build_audit_rows(entries)[0]
        assert r == {"日時": "2026-07-25T00:00", "担当": "佐藤", "停留所": "長木",
                     "項目": "ja-Hrkt", "変更前": "ちょうき", "変更後": "おさぎ",
                     "保存層": "自治体", "出典": "ai", "AIモデル": "claude-sonnet-4-6"}

    def test_missing_fields_become_empty(self):
        r = R.build_audit_rows([{"stop_name": "X"}])[0]
        assert r["担当"] == "" and r["変更前"] == "" and r["AIモデル"] == ""
        assert r["保存層"] == ""   # layer 未指定は空

    def test_unknown_layer_passthrough(self):
        r = R.build_audit_rows([{"layer": "project"}])[0]
        assert r["保存層"] == "プロジェクト"   # LAYER_JA で変換

    def test_empty_input(self):
        assert R.build_audit_rows([]) == []


# ============================================================
# reading_suspicious: 読みが怪しい（漢字残り・空・半角カナ等）か
# ============================================================
class TestReadingSuspicious:
    def test_clean_hiragana_is_ok(self):
        assert R.reading_suspicious("かわら") is False

    def test_kanji_remaining_is_suspicious(self):
        assert R.reading_suspicious("香春のこり") is True

    def test_empty_and_whitespace_are_suspicious(self):
        assert R.reading_suspicious("") is True
        assert R.reading_suspicious("   ") is True
        assert R.reading_suspicious(None) is True

    def test_halfwidth_kana_is_suspicious(self):
        assert R.reading_suspicious("ｶﾜﾗ") is True
