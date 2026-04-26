"""
generate_gtfs_files.py
======================

Step 3: 構造化された中間表現（JSON/dict）から、
        GTFS-JP v4.0 の各CSVファイルを生成する。

生成対象:
    - 必須: agency.txt, stops.txt, routes.txt, trips.txt, stop_times.txt,
            calendar.txt, fare_attributes.txt, feed_info.txt
    - GTFS-JP拡張: agency_jp.txt, routes_jp.txt, office_jp.txt, pattern_jp.txt
    - 任意: calendar_dates.txt, fare_rules.txt, translations.txt

Usage:
    python generate_gtfs_files.py <input.json> -o <output_dir>

Status: STUB (skeleton only - implementation TBD)

License: Apache 2.0
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


# GTFS-JP v4.0 で生成する全ファイル名
GTFS_FILES = [
    # 必須
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "calendar.txt",
    "fare_attributes.txt",
    "feed_info.txt",
    # GTFS-JP拡張
    "agency_jp.txt",
    "routes_jp.txt",
    "office_jp.txt",
    "pattern_jp.txt",
    # 任意
    "calendar_dates.txt",
    "fare_rules.txt",
    "translations.txt",
    # shapes.txt は generate_shapes.py で別生成
]


def write_csv(output_path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """1ファイル分のCSVを書き出す（UTF-8 with BOM, CRLF）。

    GTFS仕様では LF/CRLF どちらでもよいが、Windows互換性のためCRLF推奨。
    """
    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)


def generate_agency(data: dict, output_dir: Path) -> None:
    """agency.txt を生成。"""
    # TODO: data から agency情報を取り出してCSV化
    raise NotImplementedError("generate_agency")


def generate_stops(data: dict, output_dir: Path) -> None:
    """stops.txt を生成。"""
    raise NotImplementedError("generate_stops")


def generate_routes(data: dict, output_dir: Path) -> None:
    """routes.txt を生成。"""
    raise NotImplementedError("generate_routes")


def generate_trips_and_stop_times(data: dict, output_dir: Path) -> None:
    """trips.txt と stop_times.txt を生成。"""
    raise NotImplementedError("generate_trips_and_stop_times")


def generate_calendar(data: dict, output_dir: Path) -> None:
    """calendar.txt と calendar_dates.txt を生成。"""
    raise NotImplementedError("generate_calendar")


def generate_jp_extensions(data: dict, output_dir: Path) -> None:
    """agency_jp.txt / office_jp.txt / pattern_jp.txt / routes_jp.txt を生成。"""
    raise NotImplementedError("generate_jp_extensions")


def main():
    parser = argparse.ArgumentParser(description="Generate GTFS-JP CSV files from intermediate JSON")
    parser.add_argument("input", help="Input JSON file (intermediate representation)")
    parser.add_argument("-o", "--output", required=True, help="Output directory")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = json.loads(input_path.read_text(encoding="utf-8"))

    generate_agency(data, output_dir)
    generate_stops(data, output_dir)
    generate_routes(data, output_dir)
    generate_trips_and_stop_times(data, output_dir)
    generate_calendar(data, output_dir)
    generate_jp_extensions(data, output_dir)

    print(f"[OK] Generated GTFS-JP files in {output_dir}")


if __name__ == "__main__":
    main()
