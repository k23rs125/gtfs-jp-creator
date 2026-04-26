"""
validate_gtfs_jp_extensions.py
==============================

Step 5b: GTFS-JP 拡張部 (agency_jp.txt / office_jp.txt / pattern_jp.txt /
        routes_jp.txt) の独自検証。

MobilityData GTFS Validator は GTFS-JP拡張に対応していないため、
本スクリプトで以下を確認する:

    1. ファイルの存在チェック
    2. 必須カラムの有無
    3. agency_id / office_id / route_id 等の参照整合性
    4. GTFS-JP仕様で許容される値域チェック

Usage:
    python validate_gtfs_jp_extensions.py <gtfs_dir>

Status: STUB (skeleton only - implementation TBD)

License: Apache 2.0
"""

import argparse
import csv
import sys
from pathlib import Path


# GTFS-JP拡張ファイルとその必須カラム
JP_EXTENSION_SCHEMA = {
    "agency_jp.txt": {
        "required": ["agency_id", "agency_official_name", "agency_zip_number", "agency_address"],
        "optional": ["agency_president_pos", "agency_president_name"],
    },
    "office_jp.txt": {
        "required": ["office_id", "office_name"],
        "optional": ["office_url", "office_phone"],
    },
    "pattern_jp.txt": {
        "required": ["pattern_id", "route_id", "direction_id"],
        "optional": ["pattern_name"],
    },
    "routes_jp.txt": {
        "required": ["route_id"],
        "optional": ["route_update_date", "origin_stop", "via_stop", "destination_stop"],
    },
}


def check_file_exists(gtfs_dir: Path, filename: str) -> bool:
    return (gtfs_dir / filename).exists()


def check_required_columns(file_path: Path, required: list[str]) -> list[str]:
    """必須カラムが揃っているかチェック。不足カラム名のリストを返す。"""
    with file_path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        existing = set(reader.fieldnames or [])
    return [col for col in required if col not in existing]


def check_referential_integrity(gtfs_dir: Path) -> list[str]:
    """参照整合性チェック。違反メッセージのリストを返す。"""
    errors = []
    # TODO:
    # - agency_jp.agency_id は agency.agency_id に存在するか
    # - office_jp.office_id は trips.office_id 等から参照されているか
    # - pattern_jp.route_id は routes.route_id に存在するか
    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate GTFS-JP extension files")
    parser.add_argument("gtfs_dir", help="Path to extracted GTFS-JP directory")
    args = parser.parse_args()

    gtfs_dir = Path(args.gtfs_dir)
    if not gtfs_dir.is_dir():
        print(f"Error: not a directory: {gtfs_dir}", file=sys.stderr)
        sys.exit(1)

    all_errors = []
    all_warnings = []

    for filename, schema in JP_EXTENSION_SCHEMA.items():
        if not check_file_exists(gtfs_dir, filename):
            all_warnings.append(f"[WARN] {filename} not found (optional in some feeds)")
            continue

        missing_cols = check_required_columns(gtfs_dir / filename, schema["required"])
        if missing_cols:
            all_errors.append(f"[ERROR] {filename}: missing required columns: {missing_cols}")

    all_errors.extend(check_referential_integrity(gtfs_dir))

    if all_warnings:
        print("\n".join(all_warnings))
    if all_errors:
        print("\n".join(all_errors), file=sys.stderr)
        sys.exit(1)

    print("[OK] GTFS-JP extension validation passed")


if __name__ == "__main__":
    main()
