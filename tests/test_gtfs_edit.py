# -*- coding: utf-8 -*-
"""gtfs_edit（GTFS/CSV 編集ユーティリティ）の仕様を固定する単体テスト。

実行できる仕様書を兼ねる。重点的に固定する仕様:
  - rewrite_csv_field : BOM・改行コードを入力どおり維持／only_table 絞り込み／
                        欠損ファイル・空 rename_map・列なしは no-op（副作用なし）。
  - update_translations_rows : 既存更新／空文字で言語行を削除／無ければ新規追加。
  - write_shape : 連続重複点の除去／shape_dist_traveled の累積再計算（Haversine）／
                  他 shape の保持／緯度経度6桁・距離2桁の書式。
  - assign_trip_shape : route+direction 一致行へ shape_id 付与／列が無ければ追加。
すべて tmp_path 上で決定的に検証する。
"""
import csv

import pytest

import gtfs_edit as ge


# ---- 小道具 ----
def _read_rows(path, encoding="utf-8-sig"):
    with open(path, encoding=encoding, newline="") as f:
        return list(csv.DictReader(f))


def _write_bytes(path, text, bom=False, newline="\n"):
    """BOM・改行を厳密に指定して生CSVを書く（維持テスト用）。"""
    data = text.replace("\n", newline)
    raw = (b"\xef\xbb\xbf" if bom else b"") + data.encode("utf-8")
    path.write_bytes(raw)


# ============================================================
# rewrite_csv_field: 値の置換＋ BOM/改行の維持
# ============================================================
class TestRewriteCsvField:
    def test_basic_rename(self, tmp_path):
        p = tmp_path / "stops.txt"
        _write_bytes(p, "stop_id,stop_name\nS1,駅東口\nS2,呉\n")
        ge.rewrite_csv_field(p, "stop_name", {"駅東口": "東口"})
        rows = _read_rows(p)
        assert rows[0]["stop_name"] == "東口" and rows[1]["stop_name"] == "呉"

    def test_only_table_filter(self, tmp_path):
        p = tmp_path / "t.txt"
        _write_bytes(p, "table_name,field_value\nstops,本町\nroutes,本町\n")
        ge.rewrite_csv_field(p, "field_value", {"本町": "ほんまち"}, only_table="stops")
        rows = _read_rows(p)
        assert rows[0]["field_value"] == "ほんまち"   # stops 行だけ置換
        assert rows[1]["field_value"] == "本町"        # routes 行は不変

    def test_preserves_bom_and_crlf(self, tmp_path):
        p = tmp_path / "b.txt"
        _write_bytes(p, "stop_id,stop_name\nS1,駅\n", bom=True, newline="\r\n")
        ge.rewrite_csv_field(p, "stop_name", {"駅": "えき"})
        raw = p.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")            # BOM 維持
        assert b"\r\n" in raw                             # CRLF 維持
        assert "えき".encode("utf-8") in raw              # 置換済み

    def test_preserves_no_bom_and_lf(self, tmp_path):
        p = tmp_path / "n.txt"
        _write_bytes(p, "stop_id,stop_name\nS1,駅\n", bom=False, newline="\n")
        ge.rewrite_csv_field(p, "stop_name", {"駅": "えき"})
        raw = p.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")   # BOM を付けない
        assert b"\r\n" not in raw                    # LF のまま

    def test_missing_file_is_noop(self, tmp_path):
        p = tmp_path / "none.txt"
        ge.rewrite_csv_field(p, "stop_name", {"a": "b"})
        assert not p.exists()   # ファイルを作らない

    def test_empty_rename_map_leaves_file_unchanged(self, tmp_path):
        p = tmp_path / "s.txt"
        _write_bytes(p, "stop_id,stop_name\nS1,駅\n")
        before = p.read_bytes()
        ge.rewrite_csv_field(p, "stop_name", {})
        assert p.read_bytes() == before

    def test_field_not_in_header_leaves_file_unchanged(self, tmp_path):
        p = tmp_path / "s.txt"
        _write_bytes(p, "stop_id,stop_name\nS1,駅\n")
        before = p.read_bytes()
        ge.rewrite_csv_field(p, "no_such_col", {"駅": "えき"})
        assert p.read_bytes() == before

    def test_value_not_in_map_unchanged(self, tmp_path):
        p = tmp_path / "s.txt"
        _write_bytes(p, "stop_id,stop_name\nS1,駅\n")
        ge.rewrite_csv_field(p, "stop_name", {"別の名前": "x"})
        assert _read_rows(p)[0]["stop_name"] == "駅"


# ============================================================
# update_translations_rows: 更新／削除（空文字）／新規追加
# ============================================================
_TRANS_HEADER = "table_name,field_name,language,translation,field_value\n"


class TestUpdateTranslationsRows:
    def _base(self, tmp_path):
        p = tmp_path / "translations.txt"
        _write_bytes(p, _TRANS_HEADER
                     + "stops,stop_name,ja-Hrkt,かわら,香春\n"
                     + "stops,stop_name,en,Kawara,香春\n")
        return p

    def test_update_existing(self, tmp_path):
        p = self._base(tmp_path)
        ge.update_translations_rows(p, [("stops", "stop_name", "香春", {"ja-Hrkt": "かはる"})])
        rows = _read_rows(p)
        hrkt = [r for r in rows if r["language"] == "ja-Hrkt"][0]
        assert hrkt["translation"] == "かはる"

    def test_empty_translation_deletes_row(self, tmp_path):
        p = self._base(tmp_path)
        ge.update_translations_rows(p, [("stops", "stop_name", "香春", {"en": ""})])
        rows = _read_rows(p)
        assert all(r["language"] != "en" for r in rows)   # en 行が削除された
        assert any(r["language"] == "ja-Hrkt" for r in rows)  # 他は残る

    def test_adds_new_row_when_missing(self, tmp_path):
        p = self._base(tmp_path)
        ge.update_translations_rows(p, [("routes", "route_long_name", "香春線", {"ja-Hrkt": "かわらせん"})])
        rows = _read_rows(p)
        added = [r for r in rows if r["table_name"] == "routes"]
        assert len(added) == 1
        assert added[0]["translation"] == "かわらせん" and added[0]["field_value"] == "香春線"

    def test_missing_file_is_noop(self, tmp_path):
        p = tmp_path / "none.txt"
        ge.update_translations_rows(p, [("stops", "stop_name", "香春", {"ja-Hrkt": "x"})])
        assert not p.exists()

    def test_empty_updates_is_noop(self, tmp_path):
        p = self._base(tmp_path)
        before = p.read_bytes()
        ge.update_translations_rows(p, [])
        assert p.read_bytes() == before

    def test_multiple_langs_in_one_update(self, tmp_path):
        p = self._base(tmp_path)
        ge.update_translations_rows(p, [("stops", "stop_name", "香春",
                                         {"ja-Hrkt": "かはる", "en": ""})])
        rows = _read_rows(p)
        assert [r for r in rows if r["language"] == "ja-Hrkt"][0]["translation"] == "かはる"
        assert all(r["language"] != "en" for r in rows)   # en は削除


# ============================================================
# write_shape: 重複除去・距離再計算・他shape保持・書式
# ============================================================
class TestWriteShape:
    def test_first_distance_zero_and_monotonic(self, tmp_path):
        p = tmp_path / "shapes.txt"
        ge.write_shape(p, "S1", [(35.0, 135.0), (35.001, 135.0), (35.002, 135.0)])
        rows = _read_rows(p)
        dists = [float(r["shape_dist_traveled"]) for r in rows]
        assert dists[0] == 0.0
        assert dists == sorted(dists)        # 単調非減少
        assert all(a <= b for a, b in zip(dists, dists[1:]))

    def test_distance_is_haversine_meters(self, tmp_path):
        p = tmp_path / "shapes.txt"
        ge.write_shape(p, "S1", [(35.0, 135.0), (36.0, 135.0)])   # 緯度1度≒111.19km
        d = float(_read_rows(p)[1]["shape_dist_traveled"])
        assert 111000 < d < 111400

    def test_dedup_consecutive_duplicates(self, tmp_path):
        p = tmp_path / "shapes.txt"
        ge.write_shape(p, "S1", [(35.0, 135.0), (35.0, 135.0), (35.001, 135.0)])
        rows = _read_rows(p)
        assert len(rows) == 2   # 連続重複は1点に畳まれる

    def test_preserves_other_shapes(self, tmp_path):
        p = tmp_path / "shapes.txt"
        _write_bytes(p, "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled\n"
                     + "OTHER,10.0,20.0,0,0.00\n")
        ge.write_shape(p, "S1", [(35.0, 135.0), (35.001, 135.0)])
        ids = {r["shape_id"] for r in _read_rows(p)}
        assert ids == {"OTHER", "S1"}   # 別shapeは保持

    def test_formatting_lat_lon_seq(self, tmp_path):
        p = tmp_path / "shapes.txt"
        ge.write_shape(p, "S1", [(35.123456789, 135.987654321)])
        r = _read_rows(p)[0]
        assert r["shape_pt_lat"] == "35.123457"     # 6桁
        assert r["shape_pt_lon"] == "135.987654"
        assert r["shape_pt_sequence"] == "0" and r["shape_dist_traveled"] == "0.00"

    def test_creates_file_when_absent(self, tmp_path):
        p = tmp_path / "new_shapes.txt"
        ge.write_shape(p, "S1", [(35.0, 135.0)])
        assert p.exists() and _read_rows(p)[0]["shape_id"] == "S1"


# ============================================================
# assign_trip_shape: route+direction 一致行へ shape_id 付与
# ============================================================
class TestAssignTripShape:
    def test_assigns_to_matching_route_and_direction(self, tmp_path):
        p = tmp_path / "trips.txt"
        _write_bytes(p, "trip_id,route_id,direction_id,shape_id\n"
                     + "T1,R01,0,\nT2,R01,1,\nT3,R02,0,\n")
        ge.assign_trip_shape(p, "R01", "0", "SHP_R01_0")
        rows = _read_rows(p)
        assert rows[0]["shape_id"] == "SHP_R01_0"   # R01/0 に付与
        assert rows[1]["shape_id"] == ""            # R01/1 は対象外
        assert rows[2]["shape_id"] == ""            # R02/0 は対象外

    def test_adds_shape_id_column_when_missing(self, tmp_path):
        p = tmp_path / "trips.txt"
        _write_bytes(p, "trip_id,route_id,direction_id\nT1,R01,0\n")
        ge.assign_trip_shape(p, "R01", "0", "SHP")
        rows = _read_rows(p)
        assert "shape_id" in rows[0] and rows[0]["shape_id"] == "SHP"

    def test_direction_defaults_to_zero(self, tmp_path):
        # direction_id が空欄の行は "0" 扱い
        p = tmp_path / "trips.txt"
        _write_bytes(p, "trip_id,route_id,direction_id,shape_id\nT1,R01,,\n")
        ge.assign_trip_shape(p, "R01", "0", "SHP")
        assert _read_rows(p)[0]["shape_id"] == "SHP"

    def test_other_rows_unchanged(self, tmp_path):
        p = tmp_path / "trips.txt"
        _write_bytes(p, "trip_id,route_id,direction_id,shape_id\n"
                     + "T1,R01,0,OLD\nT2,R02,0,KEEP\n")
        ge.assign_trip_shape(p, "R01", "0", "NEW")
        rows = _read_rows(p)
        assert rows[0]["shape_id"] == "NEW" and rows[1]["shape_id"] == "KEEP"
