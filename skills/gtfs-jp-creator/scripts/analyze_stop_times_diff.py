"""
analyze_stop_times_diff.py
==========================

EVAL-03: trip 単位で対応付けた stop_times 差分解析。

eval_compare.py は (停留所名, 時刻) ペアの集合比較を行う。
この手法は停留所名の表記揺れや時刻フォーマットの差で大きく過小評価される
（古賀市実証: 集合比較 71.9% vs 本ツール 99.7%）。

本スクリプトは:
    1. 公式と当方の trips を「最初の停留所の (name, time)」で1対1対応付け
    2. 対応付いた trip ごとに、stop_sequence の各行で時刻と停留所名を比較
    3. 真の時刻誤差・名前不一致・余分/欠落を分けて集計

なぜこの方法か:
    バス時刻表は便（trip）単位で意味を持つ。便ごとに「ある時刻に出発する」
    という確定的なシグネチャがあり、表記揺れに左右されない。
    集合比較は数値だけ見ると低くなりがちだが、便対応付けで見ると
    Step 2 LLM の真の精度が定量化できる。

Usage:
    python analyze_stop_times_diff.py
        --official <gtfs_zip_or_dir>
        --ours <gtfs_zip_or_dir>
        [-o <report.md>]
        [--json <report.json>]
        [--max-details N]      # 詳細表示する差分の最大件数（既定 50）

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory


# ---------------------------------------------------------------------------
# 正規化
# ---------------------------------------------------------------------------

# 停留所名のマッチング正規化（canonicalize_stops.py と同じ思想）
_NOISE_PATTERNS = [
    "JR", "ＪＲ",
    "(駅前広場)", "（駅前広場）",
    "(東口)", "（東口）", "(西口)", "（西口）",
    "(南口)", "（南口）", "(北口)", "（北口）",
    "「", "」", "『", "』",
]


def normalize_name(s: str) -> str:
    """停留所名・路線名の正規化（NFKC + 全角/半角空白 + ノイズ除去）。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.replace("　", " ")
    for noise in _NOISE_PATTERNS:
        s = s.replace(noise, "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_time(t: str) -> str:
    """時刻フォーマットを HH:MM:SS に統一する（8:10:00 → 08:10:00）。"""
    if not t:
        return ""
    s = str(t).strip()
    if ":" not in s:
        return s
    parts = s.split(":")
    try:
        if len(parts) == 2:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"
        if len(parts) >= 3:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
    except (ValueError, TypeError):
        return s
    return s


# ---------------------------------------------------------------------------
# CSV 読み込み
# ---------------------------------------------------------------------------

def read_csv_text(text: str) -> list[dict]:
    if text.startswith("﻿"):
        text = text[1:]
    return list(csv.DictReader(io.StringIO(text)))


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def find_gtfs_files_in_zip(zip_path: Path) -> Path:
    """zip を展開して GTFS ファイルがあるディレクトリを返す。"""
    tmp = TemporaryDirectory()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmp.name)
    base = Path(tmp.name)
    # ルート直下に stops.txt があれば base、無ければ最初に見つかったサブディレクトリ
    if (base / "stops.txt").exists():
        return base, tmp
    for sub in base.iterdir():
        if sub.is_dir() and (sub / "stops.txt").exists():
            return sub, tmp
    return base, tmp


# ---------------------------------------------------------------------------
# trip → stop sequence の構築
# ---------------------------------------------------------------------------

def build_trip_sequences(gtfs_dir: Path) -> dict[str, list[tuple[int, str, str]]]:
    """trip_id → [(stop_seq, normalized_stop_name, normalized_arrival_time), ...]"""
    stops = read_csv(gtfs_dir / "stops.txt")
    stop_times = read_csv(gtfs_dir / "stop_times.txt")

    id_to_name = {s["stop_id"]: normalize_name(s.get("stop_name", "")) for s in stops}

    by_trip: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for st in stop_times:
        tid = st.get("trip_id", "")
        sid = st.get("stop_id", "")
        try:
            seq = int(st.get("stop_sequence") or 0)
        except (ValueError, TypeError):
            seq = 0
        time = normalize_time(st.get("arrival_time", ""))
        if not tid or not sid:
            continue
        name = id_to_name.get(sid, "")
        by_trip[tid].append((seq, name, time))

    # 各 trip の stop_sequence でソート
    for tid in by_trip:
        by_trip[tid].sort(key=lambda x: x[0])
    return dict(by_trip)


# ---------------------------------------------------------------------------
# trip マッチング
# ---------------------------------------------------------------------------

def trip_signature(seq_list: list[tuple[int, str, str]]) -> tuple[str, ...] | None:
    """trip の「停留所名の並び（時刻を除いた正規化名のタプル）」をシグネチャとする。"""
    if not seq_list:
        return None
    return tuple(name for _, name, _ in seq_list)


def _first_time(seq_list: list[tuple[int, str, str]]) -> str:
    return seq_list[0][2] if seq_list else ""


def match_trips(off_seq: dict, our_seq: dict) -> tuple[list[tuple[str, str]],
                                                         list[str], list[str]]:
    """trip を「停留所列シグネチャ」で対応付ける。

    同一の停留所列を持つ便が複数ある場合は、先頭発車時刻が近い順に1対1対応。
    """
    off_sig: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for tid, seq in off_seq.items():
        sig = trip_signature(seq)
        if sig:
            off_sig[sig].append(tid)

    our_sig: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for tid, seq in our_seq.items():
        sig = trip_signature(seq)
        if sig:
            our_sig[sig].append(tid)

    matched: list[tuple[str, str]] = []
    only_official: list[str] = []
    only_ours: list[str] = []

    for sig in set(off_sig.keys()) | set(our_sig.keys()):
        off_tids = sorted(off_sig.get(sig, []), key=lambda t: _first_time(off_seq[t]))
        our_tids = sorted(our_sig.get(sig, []), key=lambda t: _first_time(our_seq[t]))
        n = min(len(off_tids), len(our_tids))
        for i in range(n):
            matched.append((off_tids[i], our_tids[i]))
        only_official.extend(off_tids[n:])
        only_ours.extend(our_tids[n:])

    return matched, only_official, only_ours


# ---------------------------------------------------------------------------
# 比較ロジック
# ---------------------------------------------------------------------------

def diff_trip_pair(off_seq: list[tuple[int, str, str]],
                    our_seq: list[tuple[int, str, str]]) -> dict:
    """1対の trip を行ごとに比較する。

    Returns:
        {
            "compared":       N,  # 比較した行数
            "time_match":     N,  # 時刻一致した行数
            "time_mismatch":  [{"seq", "name", "off_time", "our_time"}, ...],
            "name_mismatch":  [{"seq", "off_name", "our_name", "off_time", "our_time"}, ...],
            "extra_official": N,  # 公式にあって当方にない行数
            "extra_ours":     N,  # 当方にあって公式にない行数
        }
    """
    compared = 0
    time_match = 0
    time_mismatch: list[dict] = []
    name_mismatch: list[dict] = []
    extra_official = 0
    extra_ours = 0

    max_len = max(len(off_seq), len(our_seq))
    for i in range(max_len):
        if i < len(off_seq) and i < len(our_seq):
            off = off_seq[i]
            our = our_seq[i]
            compared += 1
            if off[2] == our[2]:
                time_match += 1
            else:
                time_mismatch.append({
                    "seq": i + 1,
                    "name": our[1] or off[1],
                    "off_time": off[2],
                    "our_time": our[2],
                })
            if off[1] != our[1]:
                name_mismatch.append({
                    "seq": i + 1,
                    "off_name": off[1],
                    "our_name": our[1],
                    "off_time": off[2],
                    "our_time": our[2],
                })
        elif i < len(off_seq):
            extra_official += 1
        else:
            extra_ours += 1

    return {
        "compared": compared,
        "time_match": time_match,
        "time_mismatch": time_mismatch,
        "name_mismatch": name_mismatch,
        "extra_official": extra_official,
        "extra_ours": extra_ours,
    }


# ---------------------------------------------------------------------------
# レポート生成
# ---------------------------------------------------------------------------

def make_report(off_seq: dict, our_seq: dict, off_label: str, our_label: str,
                 max_details: int) -> dict:
    """総合的なレポートを構築。"""
    matched, only_off, only_ours = match_trips(off_seq, our_seq)

    total_off_trips = len(off_seq)
    total_our_trips = len(our_seq)
    matched_count = len(matched)

    # 全マッチした trip ペアで行単位 diff を取る
    total_compared = 0
    total_time_match = 0
    total_name_mismatch_rows = 0
    total_extra_off = 0
    total_extra_ours = 0
    time_diff_details: list[dict] = []
    name_diff_details: list[dict] = []

    per_trip = []
    for off_tid, our_tid in matched:
        diff = diff_trip_pair(off_seq[off_tid], our_seq[our_tid])
        total_compared += diff["compared"]
        total_time_match += diff["time_match"]
        total_name_mismatch_rows += len(diff["name_mismatch"])
        total_extra_off += diff["extra_official"]
        total_extra_ours += diff["extra_ours"]
        # 詳細
        for tm in diff["time_mismatch"]:
            time_diff_details.append({**tm, "off_trip": off_tid, "our_trip": our_tid})
        for nm in diff["name_mismatch"]:
            name_diff_details.append({**nm, "off_trip": off_tid, "our_trip": our_tid})
        per_trip.append({
            "off_trip": off_tid,
            "our_trip": our_tid,
            "compared": diff["compared"],
            "time_match": diff["time_match"],
            "time_mismatch": len(diff["time_mismatch"]),
            "name_mismatch": len(diff["name_mismatch"]),
        })

    time_match_rate = (total_time_match / total_compared * 100) if total_compared else 0.0

    return {
        "summary": {
            "off_trips": total_off_trips,
            "our_trips": total_our_trips,
            "matched_trips": matched_count,
            "only_in_official": len(only_off),
            "only_in_ours": len(only_ours),
            "total_rows_compared": total_compared,
            "time_match_rows": total_time_match,
            "time_mismatch_rows": total_compared - total_time_match,
            "time_match_rate_pct": round(time_match_rate, 2),
            "name_mismatch_rows": total_name_mismatch_rows,
            "extra_in_official": total_extra_off,
            "extra_in_ours": total_extra_ours,
        },
        "labels": {"official": off_label, "ours": our_label},
        "matched_trips": matched,
        "only_in_official": only_off,
        "only_in_ours": only_ours,
        "time_diff_details": time_diff_details[:max_details],
        "name_diff_details": name_diff_details[:max_details],
        "per_trip": per_trip,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def generate_markdown(report: dict) -> str:
    s = report["summary"]
    md = []
    md.append("# trip-aligned stop_times diff レポート")
    md.append("")
    md.append(f"**生成日時**: {report['generated_at']}")
    md.append(f"**公式**: `{report['labels']['official']}`")
    md.append(f"**当方**: `{report['labels']['ours']}`")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## サマリ")
    md.append("")
    md.append(f"- 公式 trips: **{s['off_trips']}** 件")
    md.append(f"- 当方 trips: **{s['our_trips']}** 件")
    md.append(f"- マッチした trip ペア: **{s['matched_trips']}** 件")
    md.append(f"- 公式のみ: {s['only_in_official']} 件、当方のみ: {s['only_in_ours']} 件")
    md.append("")
    md.append(f"- 行ごとの比較数: **{s['total_rows_compared']}**")
    md.append(f"- 時刻一致: **{s['time_match_rows']} / {s['total_rows_compared']} "
              f"= {s['time_match_rate_pct']}%** ")
    md.append(f"- 時刻不一致: {s['time_mismatch_rows']} 件")
    md.append(f"- 停留所名（正規化後）不一致: {s['name_mismatch_rows']} 件")
    md.append(f"- 公式のみ余分行: {s['extra_in_official']}")
    md.append(f"- 当方のみ余分行: {s['extra_in_ours']}")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 時刻不一致の詳細（先頭サンプル）")
    md.append("")
    if report["time_diff_details"]:
        md.append("| 公式trip | 当方trip | seq | 停留所 | 公式時刻 | 当方時刻 |")
        md.append("|---|---|---|---|---|---|")
        for d in report["time_diff_details"]:
            md.append(f"| {d['off_trip']} | {d['our_trip']} | {d['seq']} | "
                      f"{d['name']} | {d['off_time']} | {d['our_time']} |")
    else:
        md.append("（時刻不一致なし）")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 停留所名不一致の詳細（正規化後）")
    md.append("")
    if report["name_diff_details"]:
        md.append("| 公式trip | seq | 公式名 | 当方名 | 時刻 |")
        md.append("|---|---|---|---|---|")
        for d in report["name_diff_details"]:
            md.append(f"| {d['off_trip']} | {d['seq']} | {d['off_name']} | "
                      f"{d['our_name']} | {d['off_time']} |")
    else:
        md.append("（停留所名不一致なし ― canonicalize と正規化が完璧）")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## マッチング方法論メモ")
    md.append("")
    md.append("- 各 trip の「最初の停留所の (正規化名, 正規化時刻)」をシグネチャとして使用")
    md.append("- 同じシグネチャの trip 同士を1対1で対応付け")
    md.append("- 対応付いた trip 内で stop_sequence の各行を比較")
    md.append("- 停留所名は NFKC + 全角/半角空白 + 「」JR (駅前広場) を正規化")
    md.append("- 時刻は HH:MM:SS にゼロパディング正規化")
    md.append("")
    md.append("→ eval_compare.py の集合比較で過小評価される真の精度を正確に測れる。")

    return "\n".join(md)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="trip 単位対応付けで stop_times の真の精度を測る (EVAL-03)"
    )
    parser.add_argument("--official", required=True,
                        help="公式 GTFS feed (zip またはディレクトリ)")
    parser.add_argument("--ours", required=True,
                        help="本Skill 出力 GTFS feed (zip またはディレクトリ)")
    parser.add_argument("-o", "--output", default="stop_times_diff_report.md",
                        help="Markdown レポート出力先")
    parser.add_argument("--json", default=None, help="JSON レポート出力先")
    parser.add_argument("--max-details", type=int, default=50,
                        help="詳細差分表示の最大件数 (既定 50)")
    args = parser.parse_args()

    off_path = Path(args.official)
    our_path = Path(args.ours)
    if not off_path.exists():
        print(f"Error: --official が存在しません: {off_path}", file=sys.stderr)
        return 1
    if not our_path.exists():
        print(f"Error: --ours が存在しません: {our_path}", file=sys.stderr)
        return 1

    # zip なら展開
    off_holder = None
    our_holder = None
    if off_path.is_file() and off_path.suffix.lower() == ".zip":
        off_dir, off_holder = find_gtfs_files_in_zip(off_path)
    elif off_path.is_dir():
        off_dir = off_path
    else:
        print(f"Error: --official が zip/dir でない: {off_path}", file=sys.stderr)
        return 1

    if our_path.is_file() and our_path.suffix.lower() == ".zip":
        our_dir, our_holder = find_gtfs_files_in_zip(our_path)
    elif our_path.is_dir():
        our_dir = our_path
    else:
        print(f"Error: --ours が zip/dir でない: {our_path}", file=sys.stderr)
        return 1

    print(f"[INFO] 公式: {off_path}", file=sys.stderr)
    print(f"[INFO] 当方: {our_path}", file=sys.stderr)
    print(f"[INFO] trip-aligned diff 解析中...", file=sys.stderr)

    off_seq = build_trip_sequences(off_dir)
    our_seq = build_trip_sequences(our_dir)
    report = make_report(off_seq, our_seq, str(off_path), str(our_path), args.max_details)

    # Markdown 出力
    md = generate_markdown(report)
    Path(args.output).write_text(md, encoding="utf-8")
    print(f"[OK] Markdown: {args.output}", file=sys.stderr)

    # JSON 出力
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[OK] JSON: {args.json}", file=sys.stderr)

    # コンソールサマリ
    s = report["summary"]
    print()
    print("=" * 64)
    print("TRIP-ALIGNED STOP_TIMES DIFF")
    print("=" * 64)
    print(f"  公式 trips:                {s['off_trips']}")
    print(f"  当方 trips:                {s['our_trips']}")
    print(f"  マッチした trip ペア:       {s['matched_trips']}")
    print(f"  公式のみ trip:              {s['only_in_official']}")
    print(f"  当方のみ trip:              {s['only_in_ours']}")
    print(f"  比較した行数:               {s['total_rows_compared']}")
    print(f"  時刻一致:                  {s['time_match_rows']} / "
          f"{s['total_rows_compared']} = {s['time_match_rate_pct']}%")
    print(f"  時刻不一致:                 {s['time_mismatch_rows']}")
    print(f"  停留所名不一致:             {s['name_mismatch_rows']}")
    print("=" * 64)

    return 0


if __name__ == "__main__":
    sys.exit(main())
