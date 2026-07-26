# -*- coding: utf-8 -*-
"""collab（協調同期・永続化）の「純関数」の仕様を固定する単体テスト。

対象は session_state / ファイル非依存の純関数のみ:
  - sync_signature : 共有データから「同期の変化検知シグネチャ」を作る
                     （データ版数・生成物版数・担当と持ち主。ハートビート時刻は含めない）。
  - owned_keys     : 担当(role)ごとの所有キー（solo は全キー、他は担当分のみ）。

※ read_shared / write_shared_atomic / heartbeat_and_locks / autosave / reload_others /
   load_by_code / restore_out などは session_state と実ファイルに依存するため対象外
   （AppTest 側で自動保存・復元・協調ロックを別途検証済み）。
"""
import collab


# ============================================================
# sync_signature: 変化検知シグネチャ（在席更新では変わらない）
# ============================================================
class TestSyncSignature:
    def test_extracts_versions_and_lock_owners(self):
        shared = {"_data_ver": 100, "_out_ver": 200,
                  "_locks": {"tt": {"owner": "u1", "ts": 5},
                             "q": {"owner": "u2", "ts": 9}}}
        # 担当はソートされる（q < tt）。ハートビート時刻(ts)は含めない。
        assert collab.sync_signature(shared) == (100, 200, (("q", "u2"), ("tt", "u1")))

    def test_heartbeat_ts_does_not_change_signature(self):
        # ts（在席更新）だけが違っても、シグネチャは同じ＝在席更新では再同期しない
        a = {"_data_ver": 1, "_locks": {"tt": {"owner": "u1", "ts": 5}}}
        b = {"_data_ver": 1, "_locks": {"tt": {"owner": "u1", "ts": 999}}}
        assert collab.sync_signature(a) == collab.sync_signature(b)

    def test_owner_change_changes_signature(self):
        a = {"_locks": {"tt": {"owner": "u1"}}}
        b = {"_locks": {"tt": {"owner": "u2"}}}
        assert collab.sync_signature(a) != collab.sync_signature(b)

    def test_empty_shared(self):
        assert collab.sync_signature({}) == (None, None, ())

    def test_missing_owner_defaults_to_empty(self):
        assert collab.sync_signature({"_locks": {"tt": {}}}) == (None, None, (("tt", ""),))


# ============================================================
# owned_keys: 担当ごとの所有キー
# ============================================================
class TestOwnedKeys:
    def test_solo_owns_all_save_keys(self):
        got = collab.owned_keys("solo")
        assert got == collab.SAVE_KEYS
        assert got is not collab.SAVE_KEYS   # コピーを返す（元を書き換えない）

    def test_section_roles(self):
        assert collab.owned_keys("tt") == collab.SECTION_KEYS["tt"]
        assert collab.owned_keys("q") == collab.SECTION_KEYS["q"]
        assert collab.owned_keys("coord") == collab.SECTION_KEYS["coord"]

    def test_unknown_role_owns_nothing(self):
        assert collab.owned_keys("unknown") == []

    def test_section_roles_are_disjoint_subsets_of_save_keys(self):
        # 各担当の所有キーは互いに素で、全体(SAVE_KEYS)の部分集合であること（部分マージの前提）
        all_keys = set(collab.SAVE_KEYS)
        seen = set()
        for role in ("tt", "q", "coord"):
            keys = set(collab.owned_keys(role))
            assert keys.isdisjoint(seen)          # 担当同士は衝突しない
            assert keys <= all_keys               # 全体(SAVE_KEYS)の部分集合
            seen |= keys
