# -*- coding: utf-8 -*-
"""map_ui（⑤座標確認マップの純関数）の仕様を固定する単体テスト。

実行できる仕様書を兼ねる。固定する仕様:
  - build_map_points : 確定座標(confirmed)を優先→無ければ stop_lat/lon→座標なしは除外。
                       tooltip は重複時に [stop_id] で一意化。戻り値 (pts, tip2id)。
  - count_confidence : 確定・要確認・未補完 の件数集計（confirmed にあれば『確定』）。
  - marker_popup_html: ピンのポップアップ HTML（HTMLエスケープ・信頼度色分け・幅指定）。

build_map_points / count_confidence は stop_id・ラベルの取り出しを呼び出し側から
コールバック(sid_of / label_of)で受け取る純関数。テストでは lambda で注入する。
"""
import map_ui


def _sid(r):
    return r.get("stop_id", "")


def _label(r):
    return r.get("stop_name", "")


# ============================================================
# build_map_points: 確定優先・フォールバック・除外・一意化
# ============================================================
class TestBuildMapPoints:
    def test_confirmed_takes_priority_over_row(self):
        # confirmed にある停留所は、行の座標/信頼度に関わらず confirmed 座標＋『確定』
        crows = [{"stop_id": "S1", "stop_name": "駅", "stop_lat": "1.0", "stop_lon": "2.0",
                  "confidence": "要確認", "reason": "r1"}]
        confirmed = {"S1": (33.5, 130.9, "x")}
        pts, tip2id = map_ui.build_map_points(crows, confirmed, _sid, _label)
        assert pts == [("駅", 33.5, 130.9, "確定", "r1")]
        assert tip2id == {"駅": "S1"}

    def test_falls_back_to_stop_lat_lon(self):
        crows = [{"stop_id": "S2", "stop_name": "呉", "stop_lat": "34.0", "stop_lon": "131.0",
                  "confidence": "要確認", "reason": ""}]
        pts, _ = map_ui.build_map_points(crows, {}, _sid, _label)
        assert pts == [("呉", 34.0, 131.0, "要確認", "")]

    def test_lat_lon_are_floats(self):
        crows = [{"stop_id": "S", "stop_name": "A", "stop_lat": "34.123456",
                  "stop_lon": "131.7", "confidence": "確定"}]
        pts, _ = map_ui.build_map_points(crows, {}, _sid, _label)
        assert isinstance(pts[0][1], float) and isinstance(pts[0][2], float)
        assert pts[0][1] == 34.123456 and pts[0][2] == 131.7

    def test_excludes_stops_without_coords(self):
        crows = [{"stop_id": "S3", "stop_name": "空", "stop_lat": "", "stop_lon": "",
                  "confidence": "未補完"}]
        pts, tip2id = map_ui.build_map_points(crows, {}, _sid, _label)
        assert pts == [] and tip2id == {}

    def test_tooltip_uniquified_on_duplicate(self):
        # 同名(呉)が2つ → 2つ目は tooltip に [stop_id] が付く
        crows = [
            {"stop_id": "S4", "stop_name": "呉", "stop_lat": "34.0", "stop_lon": "131.0", "confidence": "確定"},
            {"stop_id": "S5", "stop_name": "呉", "stop_lat": "34.1", "stop_lon": "131.1", "confidence": "確定"},
        ]
        pts, tip2id = map_ui.build_map_points(crows, {}, _sid, _label)
        tips = [p[0] for p in pts]
        assert tips == ["呉", "呉[S5]"]
        assert tip2id == {"呉": "S4", "呉[S5]": "S5"}

    def test_reason_defaults_to_empty(self):
        crows = [{"stop_id": "S", "stop_name": "A", "stop_lat": "1.0", "stop_lon": "2.0",
                  "confidence": "確定"}]   # reason キーなし
        pts, _ = map_ui.build_map_points(crows, {}, _sid, _label)
        assert pts[0][4] == ""

    def test_empty_crows(self):
        assert map_ui.build_map_points([], {}, _sid, _label) == ([], {})

    def test_mixed_confirmed_fallback_and_excluded(self):
        crows = [
            {"stop_id": "A", "stop_name": "a", "stop_lat": "", "stop_lon": "", "confidence": "要確認"},   # confirmed
            {"stop_id": "B", "stop_name": "b", "stop_lat": "35.0", "stop_lon": "135.0", "confidence": "要確認"},
            {"stop_id": "C", "stop_name": "c", "stop_lat": "", "stop_lon": "", "confidence": "未補完"},   # 除外
        ]
        confirmed = {"A": (10.0, 20.0)}
        pts, tip2id = map_ui.build_map_points(crows, confirmed, _sid, _label)
        assert [p[0] for p in pts] == ["a", "b"]        # C は除外
        assert pts[0][3] == "確定" and pts[1][3] == "要確認"


# ============================================================
# count_confidence: 確定/要確認/未補完 の件数集計
# ============================================================
class TestCountConfidence:
    def test_basic_counts(self):
        crows = [{"stop_id": s, "confidence": c} for s, c in
                 [("1", "確定"), ("2", "確定"), ("3", "要確認"), ("4", "未補完")]]
        assert map_ui.count_confidence(crows, {}, _sid) == (2, 1, 1)

    def test_confirmed_counts_as_kakutei(self):
        # confidence が「要確認」でも confirmed にあれば『確定』に数える
        crows = [{"stop_id": "S1", "confidence": "要確認"},
                 {"stop_id": "S2", "confidence": "未補完"}]
        assert map_ui.count_confidence(crows, {"S1": (1, 2)}, _sid) == (1, 0, 1)

    def test_unknown_confidence_not_counted(self):
        crows = [{"stop_id": "1", "confidence": "確定"},
                 {"stop_id": "2", "confidence": ""},
                 {"stop_id": "3", "confidence": "その他"}]
        assert map_ui.count_confidence(crows, {}, _sid) == (1, 0, 0)

    def test_empty_crows(self):
        assert map_ui.count_confidence([], {}, _sid) == (0, 0, 0)


# ============================================================
# marker_popup_html: HTML生成・エスケープ・信頼度色分け
# ============================================================
class TestMarkerPopupHtml:
    def test_contains_tip_and_conf(self):
        html = map_ui.marker_popup_html("香春役場", "確定", "P11一致")
        assert "香春役場" in html and "確定" in html and "P11一致" in html

    def test_escapes_html_special_chars(self):
        html = map_ui.marker_popup_html("<script>", "要確認", "a&b")
        assert "&lt;script&gt;" in html         # tip がエスケープされる
        assert "<script>" not in html           # 生の危険タグは残らない
        assert "a&amp;b" in html                # reason もエスケープ

    def test_color_by_confidence(self):
        assert "#2e7d32" in map_ui.marker_popup_html("A", "確定")
        assert "#e08a1e" in map_ui.marker_popup_html("A", "要確認")
        assert "#c62828" in map_ui.marker_popup_html("A", "未補完")

    def test_unknown_confidence_uses_default_color(self):
        assert "#555" in map_ui.marker_popup_html("A", "その他")

    def test_reason_omitted_when_empty(self):
        with_reason = map_ui.marker_popup_html("A", "確定", "理由あり")
        no_reason = map_ui.marker_popup_html("A", "確定", "")
        assert "理由あり" in with_reason
        assert "margin-top:2px" in with_reason      # reason 用の div がある
        assert "margin-top:2px" not in no_reason     # 空なら reason div を出さない

    def test_has_width_style(self):
        # 日本語長文が1文字ずつ改行されないよう幅を明示している
        assert "width:230px" in map_ui.marker_popup_html("A", "確定")
