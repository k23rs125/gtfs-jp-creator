"""
validate_gtfs.py
================

Step 5a: MobilityData GTFS Validator (Java製) を呼び出して
         生成された GTFS-JP データをバリデーションし、
         エラー/警告のサマリを表示する。

Validator JAR:
    https://github.com/MobilityData/gtfs-validator/releases
    Apache License 2.0

事前準備:
    1. Java 11+ JRE をインストール
       Windows: https://adoptium.net/ から Temurin JRE 11+ を入手
    2. gtfs-validator-cli.jar を以下のいずれかに配置
       - skills/gtfs-jp-creator/tools/gtfs-validator-cli.jar （既定）
       - --jar オプションでパス指定

Usage:
    python validate_gtfs.py <gtfs_zip_or_dir>
        [-o <report_dir>]              # 既定: ./validation_report
        [--jar <jar_path>]             # 既定: ../tools/gtfs-validator-cli.jar
        [--country-code JP]            # 国コード (推奨: JP)
        [--no-summary]                 # report.json サマリ表示を抑制

Status:
    v0.1 (本実装): 基本機能完成、report.json パースしてサマリ出力

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


DEFAULT_VALIDATOR_JAR = Path(__file__).parent.parent / "tools" / "gtfs-validator-cli.jar"
SEVERITY_ORDER = ["ERROR", "WARNING", "INFO"]


# ---------------------------------------------------------------------------
# 環境チェック
# ---------------------------------------------------------------------------

def check_java_installed() -> str | None:
    """Java の実行ファイルパスを返す。なければ None。"""
    return shutil.which("java")


def get_java_version() -> str:
    """`java -version` の出力を返す。"""
    try:
        proc = subprocess.run(
            ["java", "-version"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        # java -version は stderr に出る
        return (proc.stderr or proc.stdout).strip().split("\n")[0]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "(unknown)"


# ---------------------------------------------------------------------------
# Validator 実行
# ---------------------------------------------------------------------------

def run_validator(input_path: Path, output_dir: Path, jar_path: Path,
                   country_code: str = "JP", timeout_sec: int = 300) -> int:
    """gtfs-validator を Java で実行する。

    Args:
        input_path: GTFS の zip ファイルまたはディレクトリ
        output_dir: validator 出力先（report.json などが入る）
        jar_path:   gtfs-validator-cli.jar のパス
        country_code: 国コード（推奨: "JP"）
        timeout_sec: タイムアウト秒

    Returns:
        Validator の exit code (0 = 成功、>=1 = 何らかのエラー)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "java", "-jar", str(jar_path),
        "--input", str(input_path),
        "--output_base", str(output_dir),
        "--country_code", country_code,
    ]
    print(f"[INFO] 実行: {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(cmd, check=False, timeout=timeout_sec)
        return proc.returncode
    except subprocess.TimeoutExpired:
        print(f"Error: Validator がタイムアウト ({timeout_sec} 秒)", file=sys.stderr)
        return 124


# ---------------------------------------------------------------------------
# report.json パース
# ---------------------------------------------------------------------------

def parse_report(report_path: Path) -> dict:
    """Validator の report.json をパースして summary 用 dict を返す。

    Returns:
        {
            "by_severity": {"ERROR": 5, "WARNING": 12, "INFO": 3},
            "by_code": [{"code": "...", "severity": "...", "count": N}, ...],
            "raw": <report.json の元データ>
        }
    """
    with report_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    notices = data.get("notices", [])
    by_severity: dict[str, int] = defaultdict(int)
    by_code: list[dict] = []

    for n in notices:
        code = n.get("code", "?")
        severity = (n.get("severity") or "INFO").upper()
        total = n.get("totalNotices", n.get("noticeCount", 0))
        by_severity[severity] += total
        by_code.append({"code": code, "severity": severity, "count": total})

    # severity 順、件数降順でソート
    by_code.sort(key=lambda x: (SEVERITY_ORDER.index(x["severity"])
                                if x["severity"] in SEVERITY_ORDER else 99,
                                -x["count"]))

    return {
        "by_severity": dict(by_severity),
        "by_code": by_code,
        "raw": data,
    }


def print_summary(report_summary: dict, report_path: Path,
                   html_path: Path | None = None) -> None:
    """report.json サマリを表示する。"""
    by_sev = report_summary["by_severity"]
    by_code = report_summary["by_code"]

    err = by_sev.get("ERROR", 0)
    warn = by_sev.get("WARNING", 0)
    info = by_sev.get("INFO", 0)

    # 全体判定
    if err == 0 and warn == 0:
        verdict = "[PASS] エラー・警告ともゼロ"
    elif err == 0:
        verdict = f"[WARNING] {warn} 件の警告あり、エラーなし"
    else:
        verdict = f"[FAIL] {err} 件のエラーあり"

    print()
    print("=" * 64)
    print("GTFS VALIDATION REPORT")
    print("=" * 64)
    print(f"  {verdict}")
    print(f"  ERROR:    {err}")
    print(f"  WARNING:  {warn}")
    print(f"  INFO:     {info}")
    print("=" * 64)

    if by_code:
        print()
        print("コード別の内訳（先頭 20 件）:")
        print(f"  {'SEVERITY':<10} {'COUNT':>6}  CODE")
        print(f"  {'-' * 10} {'-' * 6}  {'-' * 30}")
        for entry in by_code[:20]:
            print(f"  {entry['severity']:<10} {entry['count']:>6}  {entry['code']}")
        if len(by_code) > 20:
            print(f"  ... ほか {len(by_code) - 20} 種類のコード")

    print()
    print(f"report.json:  {report_path}")
    if html_path and html_path.exists():
        print(f"report.html:  {html_path}")
        print("  → ブラウザで開いて詳細を確認可能")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate GTFS-JP feed using MobilityData GTFS Validator",
    )
    parser.add_argument("input", help="GTFS zip ファイルまたはディレクトリ")
    parser.add_argument("-o", "--output", default="./validation_report",
                        help="検証レポート出力先 (既定: ./validation_report)")
    parser.add_argument("--jar", default=str(DEFAULT_VALIDATOR_JAR),
                        help=f"gtfs-validator JAR のパス (既定: {DEFAULT_VALIDATOR_JAR})")
    parser.add_argument("--country-code", default="JP",
                        help="国コード (既定: JP)")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Validator タイムアウト秒 (既定: 300)")
    parser.add_argument("--no-summary", action="store_true",
                        help="report.json サマリを表示しない")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)
    jar_path = Path(args.jar)

    # 入力存在チェック
    if not input_path.exists():
        print(f"Error: 入力が存在しません: {input_path}", file=sys.stderr)
        return 1

    # Java チェック
    if check_java_installed() is None:
        print("Error: Java が PATH に見つかりません。", file=sys.stderr)
        print("  Adoptium Temurin JRE 11+ をインストール: https://adoptium.net/",
              file=sys.stderr)
        return 1

    # JAR チェック
    if not jar_path.exists():
        print(f"Error: Validator JAR が見つかりません: {jar_path}", file=sys.stderr)
        print("  以下からダウンロード:", file=sys.stderr)
        print("    https://github.com/MobilityData/gtfs-validator/releases",
              file=sys.stderr)
        print(f"  ファイル名は gtfs-validator-X.X.X-cli.jar （X.X.X は最新版）",
              file=sys.stderr)
        print(f"  保存先（既定）: {DEFAULT_VALIDATOR_JAR.parent}/", file=sys.stderr)
        return 1

    print(f"[INFO] Java:   {get_java_version()}", file=sys.stderr)
    print(f"[INFO] JAR:    {jar_path}", file=sys.stderr)
    print(f"[INFO] 入力:   {input_path}", file=sys.stderr)
    print(f"[INFO] 出力:   {output_dir}", file=sys.stderr)
    print(f"[INFO] 国:     {args.country_code}", file=sys.stderr)
    print(file=sys.stderr)

    # Validator 実行
    exit_code = run_validator(input_path, output_dir, jar_path,
                               country_code=args.country_code,
                               timeout_sec=args.timeout)

    if exit_code != 0:
        print(f"[WARN] Validator が exit code {exit_code} で終了しました。"
              " 出力ファイルを確認してください。", file=sys.stderr)

    # report.json があればサマリ表示
    report_json = output_dir / "report.json"
    report_html = output_dir / "report.html"
    if report_json.exists() and not args.no_summary:
        summary = parse_report(report_json)
        print_summary(summary, report_json, report_html)
    elif not report_json.exists():
        print(f"[WARN] report.json が出力されませんでした: {report_json}",
              file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
