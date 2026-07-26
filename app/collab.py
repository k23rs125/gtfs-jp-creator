# -*- coding: utf-8 -*-
"""協調同期・永続化エンジン（app.py から分離・保守性向上）。

責務:
    - 自動保存/復元: 作業一式(SAVE_KEYS)と生成物(out/)を、再起動後も戻せるよう
      sid ごとの安定フォルダへ保存/復元する（persist_out / restore_out / load_by_code）。
    - 同時並行の協調: 共有プロジェクト(SID)を担当(role)ごとに部分マージ保存し、担当は
      同時に一人（ソフトロック＋ハートビート）。各自は毎回他担当の最新を取り込む。

app 状態は app_context 経由（ss / work_dir / sid / user_id / autosave_dir / autosave_file）
で最小限に扱う。app.py からは従来名（先頭 _）でエイリアス import して呼ぶ＝挙動不変。
UI 表示（進捗・復元プロンプト）は app.py 側に残し、本モジュールはエンジンに徹する。
License: Apache 2.0
"""
from __future__ import annotations

import json
import os
import time

import streamlit as st

from app_context import (autosave_dir, autosave_file, sid, ss, user_id, work_dir)

# 保存する作業一式（費用の高い手作業＝抽出・時刻修正・路線割当・確定座標・検出・原本・生成結果）。
SAVE_KEYS = ["extract", "extract_token", "decision_spec", "detected", "confirmed",
             "source_display", "sources_all", "fare_matrix_doc", "result"]
# 担当(role)ごとに「所有する保存キー」。別担当のキーには触れない＝部分マージで衝突回避。
SECTION_KEYS = {
    "tt": ["extract", "extract_token", "decision_spec", "detected",
           "source_display", "sources_all"],
    "q": ["fare_matrix_doc"],                      # ＋ form_inputs(③入力) は別途
    "coord": ["confirmed"],
}
GEN_KEYS = ["result"]           # 生成物（生成した人が書く共有物）
LOCK_TTL = 90                   # 秒。これを超えてハートビートが無い担当ロックは解放可能。
_WORK_FILES = ["config.json", "structured.json", "extract.json", "spec.json"]  # 再生成に要る作業ファイル


# ---------------------------------------------------------------------------
# 生成物・作業ファイルの永続化（out/ の保存・復元）
# ---------------------------------------------------------------------------

def out_persist_dir(_sid):
    return autosave_dir() / f"out_{_sid}"


def persist_out():
    """生成物 out/ と再生成用の作業ファイルを sid ごとの安定フォルダに保存
    （再起動後も④⑤⑥・ダウンロード・再生成を復元するため）。"""
    import shutil
    src = work_dir() / "out"
    if not src.exists():
        return
    dst = out_persist_dir(sid())
    try:
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst / "out")
        for _wf in _WORK_FILES:
            if (work_dir() / _wf).exists():
                shutil.copy2(work_dir() / _wf, dst / _wf)
    except Exception:
        pass


def restore_out(_sid):
    """保存した生成物・作業ファイルを現在の作業フォルダ(WORK)へ戻す。"""
    import shutil
    src = out_persist_dir(_sid)
    if not src.exists():
        return
    try:
        if (src / "out").exists():
            _dst = work_dir() / "out"
            if _dst.exists():
                shutil.rmtree(_dst, ignore_errors=True)
            shutil.copytree(src / "out", _dst)
        for _wf in _WORK_FILES:
            if (src / _wf).exists():
                shutil.copy2(src / _wf, work_dir() / _wf)
    except Exception:
        pass


def load_by_code(code):
    """共有コード(sid)を指定して、そのプロジェクトの保存を現在のセッションへ読み込む。
    『最新を拾う』復元と違い、コードで“そのプロジェクト”を確実に引き継ぐ（複数人の受け渡し用）。"""
    code = (code or "").strip().lower()
    if not code:
        return False, "共有コードを入力してください。"
    f = autosave_dir() / f"session_{code}.json"
    if not f.exists():
        return False, f"コード「{code}」の保存が見つかりません。コードを確認してください。"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        forms = data.pop("form_inputs", {}) or {}
        for k, v in data.items():
            ss()[k] = v
        for k, v in forms.items():   # ③の入力欄も復元
            ss()[k] = v
        ss()["sid"] = code
        try:
            st.query_params["sid"] = code
        except Exception:
            pass
        restore_out(code)           # 生成物(out/)・作業ファイルも戻す
        ss()["_restore_dismissed"] = True
        return True, ""
    except Exception as e:
        return False, f"読み込みに失敗しました: {e}"


# ---------------------------------------------------------------------------
# 同時並行（複数人が同じプロジェクトを担当ごとに並行編集）の土台
#   ・共有プロジェクト＝固定コード(SID)。全員が同じコードで同じ保存を共有。
#   ・担当ごとに「自分の担当部分だけ」保存（部分マージ）＝別担当は衝突しない。
#   ・担当は同時に一人（ソフトロック）。ロックはハートビートで生存確認、放置は自動解放。
#   ・各自は毎回、他担当の最新を読み込む＝常に最新から作業。
# ---------------------------------------------------------------------------

def owned_keys(role):
    if role == "solo":
        return list(SAVE_KEYS)                       # 一人モードは全部所有
    return SECTION_KEYS.get(role, [])


def shared_lock_path():
    return autosave_dir() / f"session_{sid()}.lock"


class file_guard:
    """共有保存ファイルの read-modify-write を直列化する簡易ロック（.lock を排他生成）。
    3人程度・低頻度なら十分。取得できなくてもタイムアウトで諦めて処理は続ける。"""
    def __init__(self, timeout=5.0):
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        autosave_dir().mkdir(parents=True, exist_ok=True)
        p = shared_lock_path()
        deadline = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return self
            except FileExistsError:
                # 古いロック（>15秒）は奪う（クラッシュ残骸対策）
                try:
                    if time.time() - p.stat().st_mtime > 15:
                        os.unlink(str(p)); continue
                except OSError:
                    pass
                if time.time() > deadline:
                    return self          # 諦めてロック無しで続行
                time.sleep(0.05)

    def __exit__(self, *a):
        try:
            if self.fd is not None:
                os.close(self.fd)
            os.unlink(str(shared_lock_path()))
        except OSError:
            pass


def read_shared():
    try:
        return json.loads(autosave_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_shared_atomic(data):
    tmp = autosave_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(autosave_file()))


def heartbeat_and_locks(role):
    """自分の担当ロックを更新（ハートビート）し、現在の全ロック状況を返す。
    role が None/solo のときはロックしない。返り値: {role: {owner,name,ts}}。"""
    with file_guard():
        shared = read_shared()
        locks = shared.get("_locks", {})
        now = time.time()
        # 期限切れロックを掃除
        for r in list(locks):
            if now - locks[r].get("ts", 0) > LOCK_TTL:
                del locks[r]
        if role in ("tt", "q", "coord"):
            cur = locks.get(role)
            if (not cur) or cur.get("owner") == user_id():
                locks[role] = {"owner": user_id(),
                               "name": ss().get("user_name", "") or "(名前未設定)",
                               "ts": now}
        shared["_locks"] = locks
        write_shared_atomic(shared)
        return locks


def release_lock(role):
    with file_guard():
        shared = read_shared()
        locks = shared.get("_locks", {})
        if locks.get(role, {}).get("owner") == user_id():
            del locks[role]
            shared["_locks"] = locks
            write_shared_atomic(shared)


def reload_others(role):
    """他担当が保存した最新データを session_state に取り込む（自分の所有キーは触らない）。
    ＝常に最新から作業する（点3対策）。フォーム入力(form_inputs)も自分の担当以外を反映。"""
    if not autosave_file().exists():
        return
    shared = read_shared()
    owned = set(owned_keys(role))
    for k, v in shared.items():
        if k in ("_locks", "form_inputs", "_out_ver"):
            continue
        if k in owned:
            continue
        # 生成物は生成した最新を全員が見る
        if v is not None:
            ss()[k] = v
    # ③フォーム入力: q担当以外は反映（q担当は自分が編集中なので上書きしない）
    if role != "q":
        for k, v in (shared.get("form_inputs") or {}).items():
            ss()[k] = v
    # 生成物(out/)が他者により更新されていれば取り込む
    _ver = shared.get("_out_ver")
    if _ver and ss().get("_seen_out_ver") != _ver:
        restore_out(sid())
        ss()["_seen_out_ver"] = _ver


def autosave():
    """担当ごとの部分マージ保存。自分の担当が所有するキーだけを共有ファイルへ書き込み、
    別担当のキーは保持する＝同時並行でも別担当は衝突しない。"""
    if not ss().get("extract"):
        return
    if ss().get("_readonly_role"):     # 他の人が編集中の担当は保存しない（上書き防止）
        return
    role = ss().get("work_mode") or "solo"
    try:
        with file_guard():
            shared = read_shared()
            for k in owned_keys(role):        # 自分の所有キーだけ更新
                v = ss().get(k)
                if v is not None:
                    shared[k] = v
            # ③の入力欄（キーは *_<extract_token>）は q担当 or solo が所有。単純値のみ。
            tk = ss().get("extract_token", "")
            if tk and role in ("q", "solo"):
                forms = {k: v for k, v in ss().items()
                         if isinstance(k, str) and k.endswith("_" + tk)
                         and isinstance(v, (str, int, float, bool))}
                base = shared.get("form_inputs") or {}
                base.update(forms)
                shared["form_inputs"] = base
            # 生成物(result)は生成した人(q/coord/solo)が書く共有物。
            if role in ("q", "coord", "solo") and ss().get("result") is not None:
                shared["result"] = ss()["result"]
            shared["_data_ver"] = time.time()      # データ版数（同期の変化検知に使う）
            write_shared_atomic(shared)
        # 生成物ファイル(out/)は変更時だけ保存。バージョンを上げて他担当へ通知。
        if ss().pop("_out_dirty", False):
            persist_out()
            with file_guard():
                shared = read_shared()
                shared["_out_ver"] = time.time()
                write_shared_atomic(shared)
                ss()["_seen_out_ver"] = shared["_out_ver"]
        # 自分の保存直後は「変化なし」と記録して、ライブ同期が自分の保存で再実行しないように。
        ss()["_last_sync_sig"] = sync_signature(shared)
    except Exception:
        pass


def sync_signature(shared):
    """同期の変化検知シグネチャ。データ版数・生成物版数・『どの担当を誰が持つか』で決まる
    （ロックのハートビート時刻は含めない＝在席更新だけでは再同期しない）。"""
    locks = shared.get("_locks") or {}
    return (shared.get("_data_ver"), shared.get("_out_ver"),
            tuple(sorted((r, l.get("owner", "")) for r, l in locks.items())))
