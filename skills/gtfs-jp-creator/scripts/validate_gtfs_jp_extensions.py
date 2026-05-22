"""
validate_gtfs_jp_extensions.py
==============================

Step 7b: GTFS-JP 拡張部 (agency_jp.txt / office_jp.txt / pattern_jp.txt /
        routes_jp.txt) の独自検証。

MobilityData GTFS Validator は GTFS-JP 拡張に対応していないため、
本スクリプトで以下を確認する:

    1. ファイルの存在チェック
    2. 必須カラムの有無
    3. agency_id / office_id / route_id 等の参照整合性
    4. GTFS-JP 仕様で許容される値域チェック（郵便番号形式・direction_id 等）

検出結果は ERROR / WARNING に分類して集計表示する。
ERROR が 1 件以上のとき終了コード 1。WARNING のみなら 0。

Usage:
    python validate_gtfs_jp_extensions.py <gtfs_dir>

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


# GTFS-JP 拡張ファイルとその必須／任意カラム
JP_EXTENSION_SCHEMA = {
    "agency_jp.txt": {
        "required": ["agency_id", "agency_official_name",
                     "agency_zip_number", "agency_address"],
        "optional": ["agency_president_pos", "agency_president_name"],
        "mandatory_file": True,   # GTFS-JP 必須ファイル
    },
    "office_jp.txt": {
        "required": ["office_id", "office_name"],
        "optional": ["office_url", "office_phone"],
        "mandatory_file": False,  # 任意ファイル
    },
    "pattern_jp.txt": {
        "required": ["pattern_id", "route_id", "direction_id"],
        "optional": ["pattern_name"],
        "mandatory_file": False,
    },
    "routes_jp.txt": {
        "required": ["route_id"],
        "optional": ["route_update_date", "origin_stop",
                     "via_stop", "destination_stop"],
        "mandatory_file": False,
    },
}

ZIP_RE = re.compile(r"^\d{3}-?\d{4}$")


# ----------------------------------------------------------------------
# ヘルパ
# ----------------------------------------------------------------------

def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    """CSV を (ヘッダー, 行リスト) で返す。"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def column_values(gtfs_dir: Path, filename: str, col: str) -> set[str]:
    """指定ファイルの 1 カラムの値集合を返す（ファイルが無ければ空集合）。"""
    path = gtfs_dir / filename
    if not path.exists():
        return set()
    _, rows = read_rows(path)
    return {(r.get(col) or "").strip() for r in rows
            if (r.get(col) or "").strip()}


# ----------------------------------------------------------------------
# チェック本体
# ----------------------------------------------------------------------

def check_columns(gtfs_dir: Path, errors: list, warnings: list) -> None:
    """ファイル存在と必須カラムの有無をチェック。"""
    for filename, schema in JP_EXTENSION_SCHEMA.items():
        path = gtfs_dir / filename
        if not path.exists():
            if schema["mandatory_file"]:
                errors.append(f"{filename}: GTFS-JP 必須ファイルが存在しません。")
            else:
                warnings.append(f"{filename}: 任意ファイルのため未生成（問題なし）。")
            continue
        header, _ = read_rows(path)
        missing = [c for c in schema["required"] if c not in header]
        if missing:
            errors.append(f"{filename}: 必須カラムが不足しています: {missing}")


def check_referential_integrity(gtfs_dir: Path, errors: list,
                                warnings: list) -> None:
    """agency_id / route_id 等の参照整合性をチェック。"""
    agency_ids = column_values(gtfs_dir, "agency.txt", "agency_id")
    route_ids = column_values(gtfs_dir, "routes.txt", "route_id")

    # agency_jp.agency_id は agency.txt に存在するか
    aj = gtfs_dir / "agency_jp.txt"
    if aj.exists() and agency_ids:
        _, rows = read_rows(aj)
        for r in rows:
            aid = (r.get("agency_id") or "").strip()
            if aid and aid not in agency_ids:
                errors.append(
                    f"agency_jp.txt: agency_id '{aid}' が agency.txt に"
                    f"存在しません。")

    # routes_jp.route_id は routes.txt に存在するか
    rj = gtfs_dir / "routes_jp.txt"
    if rj.exists() and route_ids:
        _, rows = read_rows(rj)
        for r in rows:
            rid = (r.get("route_id") or "").strip()
            if rid and rid not in route_ids:
                errors.append(
                    f"routes_jp.txt: route_id '{rid}' が routes.txt に"
                    f"存在しません。")

    # pattern_jp.route_id は routes.txt に存在するか
    pj = gtfs_dir / "pattern_jp.txt"
    if pj.exists() and route_ids:
        _, rows = read_rows(pj)
        for r in rows:
            rid = (r.get("route_id") or "").strip()
            if rid and rid not in route_ids:
                errors.append(
                    f"pattern_jp.txt: route_id '{rid}' が routes.txt に"
                    f"存在しません。")


def check_values(gtfs_dir: Path, errors: list, warnings: list) -> None:
    """値域チェック（郵便番号形式・direction_id・office_id 重複）。"""
    # agency_jp: 郵便番号の形式
    aj = gtfs_dir / "agency_jp.txt"
    if aj.exists():
        _, rows = read_rows(aj)
        for r in rows:
            zc = (r.get("agency_zip_number") or "").strip()
            if zc and not ZIP_RE.match(zc):
                warnings.append(
                    f"agency_jp.txt: agency_zip_number '{zc}' が "
                    f"郵便番号形式（NNN-NNNN）ではありません。")
            if not (r.get("agency_official_name") or "").strip():
                warnings.append(
                    "agency_jp.txt: agency_official_name が空です"
                    "（条件確認画面で入力を推奨）。")

    # pattern_jp: direction_id は 0 / 1
    pj = gtfs_dir / "pattern_jp.txt"
    if pj.exists():
        _, rows = read_rows(pj)
        for r in rows:
            d = (r.get("direction_id") or "").strip()
            if d and d not in ("0", "1"):
                errors.append(
                    f"pattern_jp.txt: direction_id '{d}' は 0 または 1 で"
                    f"なければなりません。")

    # office_jp: office_id の非空・重複
    oj = gtfs_dir / "office_jp.txt"
    if oj.exists():
        _, rows = read_rows(oj)
        seen: set[str] = set()
        for i, r in enumerate(rows, start=1):
            oid = (r.get("office_id") or "").strip()
            if not oid:
                errors.append(f"office_jp.txt: {i} 行目の office_id が空です。")
            elif oid in seen:
                errors.append(f"office_jp.txt: office_id '{oid}' が重複しています。")
            else:
                seen.add(oid)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate GTFS-JP extension files")
    parser.add_argument("gtfs_dir", help="展開済み GTFS-JP ディレクトリ")
    args = parser.parse_args()

    gtfs_dir = Path(args.gtfs_dir)
    if not gtfs_dir.is_dir():
        print(f"Error: ディレクトリではありません: {gtfs_dir}", file=sys.stderr)
        return 2

    errors: list[str] = []
    warnings: list[str] = []

    check_columns(gtfs_dir, errors, warnings)
    check_referential_integrity(gtfs_dir, errors, warnings)
    check_values(gtfs_dir, errors, warnings)

    print("=" * 64)
    print("GTFS-JP 拡張検証レポート")
    print("=" * 64)
    for w in warnings:
        print(f"  [WARNING] {w}")
    for e in errors:
        print(f"  [ERROR]   {e}", file=sys.stderr)
    print("-" * 64)
    print(f"  ERROR: {len(errors)} 件   WARNING: {len(warnings)} 件")
    print("=" * 64)

    if errors:
        print("[NG] GTFS-JP 拡張検証でエラーが見つかりました。", file=sys.stderr)
        return 1
    print("[OK] GTFS-JP 拡張検証に合格しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
