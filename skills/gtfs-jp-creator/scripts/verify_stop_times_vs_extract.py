#!/usr/bin/env python3
"""
verify_stop_times_vs_extract.py
===============================
Step1 の座標抽出JSON <-> 生成された stop_times.txt の時刻を照合する内部整合性検証ツール。

目的:
  座標方式(Step1)はPDFに対し誤差ゼロで時刻を抽出する。その後 Step2(LLM構造化)・
  Step3(CSV生成) を経て stop_times.txt ができる。本ツールは両者の時刻が便ごとに
  一致するかを照合し、LLM・生成段階での取りこぼし/改変を検出する。
  「PDFに忠実に抽出した時刻」が「最終成果物」まで保たれているかの保証になる。

  公式feedと比較する analyze_stop_times_diff.py（外部の真値・ダイヤ改正の版差に依存）と
  異なり、本ツールは「自分が抽出したもの」と「自分が出力したもの」の整合だけを見るため、
  公式feed不要・版差の影響を受けない。役割を分けて併用する。

マッチング:
  便を「停留所名の並び（時刻を除いた正規化名のタプル）」で対応付け、対応した便を
  行ごとに時刻比較する（既存 analyze_stop_times_diff.py の手法を流用）。
  要予約バス停は既定の生成で除外されるため、抽出側でも既定で除外する（--keep-reserve で含む）。

Usage:
  python verify_stop_times_vs_extract.py <extract.json> --gtfs <gtfs_dir>
        [-o report.md] [--json report.json] [--keep-reserve]

License: Apache 2.0
"""
import argparse
import json
import os
import sys
from pathlib import Path

# 同ディレクトリの analyze_stop_times_diff のマッチング/比較ロジックを流用する。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_stop_times_diff as A


def build_extract_sequences(extract: dict, keep_reserve: bool = False) -> dict:
    """抽出JSON → {trip_key: [(seq, norm_name, norm_time), ...]}
    （analyze_stop_times_diff.build_trip_sequences と同じ形式）。
    要予約バス停は既定で除外（生成側の既定挙動に合わせる）。"""
    seqs = {}
    for b in extract.get("blocks", []):
        bi = b.get("block_index", "?")
        for ti, t in enumerate(b.get("trips", [])):
            # PDFは col_x、Excelは col。indexも併用して便キーを一意にする。
            col = t.get("col_x", t.get("col"))
            key = f"b{bi}_t{ti}_x{col}"
            rows = []
            s = 0
            for c in t.get("cells", []):
                if c.get("reserve") and not keep_reserve:
                    continue
                s += 1
                rows.append((s, A.normalize_name(c.get("name", "")),
                             A.normalize_time(c.get("time", ""))))
            if rows:
                seqs[key] = rows
    return seqs


def build_markdown(summary: dict, trip_reports: list, args) -> str:
    L = []
    L.append("# stop_times 照合レポート（抽出JSON <-> stop_times.txt）")
    L.append("")
    L.append(f"- 抽出JSON: `{args.extract}`")
    L.append(f"- GTFS: `{args.gtfs}`")
    L.append(f"- 要予約バス停: {'照合に含める' if args.keep_reserve else '除外（生成と同条件）'}")
    L.append("")
    L.append("## サマリ")
    L.append("")
    L.append(f"- 抽出便: {summary['extract_trips']} / stop_times便: {summary['stop_times_trips']} "
             f"/ マッチ: {summary['matched_trips']}")
    L.append(f"- 比較行数: {summary['rows_compared']}")
    L.append(f"- **時刻一致: {summary['time_match']} / {summary['rows_compared']} "
             f"= {summary['time_match_pct']}%**")
    L.append(f"- 時刻不一致: {summary['time_mismatch']}")
    L.append(f"- 抽出のみ便（stop_timesに無い）: {len(summary['only_in_extract'])}")
    L.append(f"- stop_timesのみ便（抽出に無い）: {len(summary['only_in_stop_times'])}")
    L.append(f"- 判定: **{summary['verdict']}**")
    L.append("")
    if summary['only_in_extract']:
        L.append("## 抽出のみ便（生成で落ちた可能性）")
        for k in summary['only_in_extract']:
            L.append(f"- {k}")
        L.append("")
    if summary['only_in_stop_times']:
        L.append("## stop_timesのみ便（抽出に無い＝生成で増えた可能性）")
        for k in summary['only_in_stop_times']:
            L.append(f"- {k}")
        L.append("")
    if trip_reports:
        L.append("## 便ごとの差異")
        for ext_tid, our_tid, d in trip_reports:
            L.append(f"### {our_tid}  (抽出: {ext_tid})")
            if d["extra_official"] or d["extra_ours"]:
                L.append(f"- 行数差: 抽出のみ {d['extra_official']} 行 / stop_timesのみ {d['extra_ours']} 行")
            for m in d["time_mismatch"]:
                L.append(f"- 順{m['seq']} {m['name']}: 抽出 {m['off_time']} → stop_times {m['our_time']}")
            L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Step1抽出JSON と生成 stop_times.txt の時刻を照合する内部整合性検証")
    ap.add_argument("extract", help="Step1 座標抽出JSON")
    ap.add_argument("--gtfs", required=True,
                    help="生成された GTFS-JP ディレクトリ（stops.txt/stop_times.txt を含む）")
    ap.add_argument("-o", "--output", default="stop_times_verify_report.md",
                    help="Markdown レポート出力先")
    ap.add_argument("--json", default=None, help="JSON レポート出力先（任意）")
    ap.add_argument("--keep-reserve", action="store_true",
                    help="要予約バス停も照合に含める（既定は除外＝生成と同条件）")
    args = ap.parse_args()

    ext_path = Path(args.extract)
    gtfs_dir = Path(args.gtfs)
    if not ext_path.exists():
        print(f"[ERROR] 抽出JSONが見つかりません: {ext_path}", file=sys.stderr)
        return 1
    if not (gtfs_dir / "stop_times.txt").exists():
        print(f"[ERROR] stop_times.txt が見つかりません: {gtfs_dir}", file=sys.stderr)
        return 1

    extract = json.loads(ext_path.read_text(encoding="utf-8"))
    ext_seq = build_extract_sequences(extract, args.keep_reserve)
    our_seq = A.build_trip_sequences(gtfs_dir)

    matched, only_ext, only_our = A.match_trips(ext_seq, our_seq)

    total_compared = 0
    total_match = 0
    mismatches = []
    trip_reports = []
    for ext_tid, our_tid in matched:
        d = A.diff_trip_pair(ext_seq[ext_tid], our_seq[our_tid])
        total_compared += d["compared"]
        total_match += d["time_match"]
        if d["time_mismatch"] or d["extra_official"] or d["extra_ours"]:
            trip_reports.append((ext_tid, our_tid, d))
        for m in d["time_mismatch"]:
            mismatches.append({"trip": our_tid, **m})

    rate = (total_match / total_compared * 100) if total_compared else 0.0
    verdict_ok = (not only_ext and not only_our and not mismatches)
    verdict = ("[PASS] 抽出した時刻が stop_times.txt に完全に保たれています"
               if verdict_ok else "[要確認] 抽出と stop_times に差異があります")
    summary = {
        "extract_trips": len(ext_seq),
        "stop_times_trips": len(our_seq),
        "matched_trips": len(matched),
        "only_in_extract": only_ext,
        "only_in_stop_times": only_our,
        "rows_compared": total_compared,
        "time_match": total_match,
        "time_match_pct": round(rate, 2),
        "time_mismatch": len(mismatches),
        "verdict": verdict,
    }

    md = build_markdown(summary, trip_reports, args)
    Path(args.output).write_text(md, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"summary": summary, "mismatches": mismatches}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    # サマリ（cp932安全・絵文字なし）
    print("=" * 64, file=sys.stderr)
    print("STOP_TIMES 照合 (抽出JSON <-> stop_times.txt)", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    print(f"  抽出便: {summary['extract_trips']}  stop_times便: {summary['stop_times_trips']}  "
          f"マッチ: {summary['matched_trips']}", file=sys.stderr)
    print(f"  時刻一致: {total_match} / {total_compared} = {rate:.2f}%", file=sys.stderr)
    print(f"  時刻不一致: {len(mismatches)}", file=sys.stderr)
    print(f"  抽出のみ便: {len(only_ext)}  stop_timesのみ便: {len(only_our)}", file=sys.stderr)
    print(f"  判定: {verdict}", file=sys.stderr)
    if mismatches:
        print("  時刻不一致の例:", file=sys.stderr)
        for m in mismatches[:10]:
            print(f"    {m['trip']} 順{m['seq']} {m['name']}: "
                  f"抽出 {m['off_time']} → stop_times {m['our_time']}", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    print(f"[OK] Markdown: {args.output}", file=sys.stderr)
    if args.json:
        print(f"[OK] JSON: {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
