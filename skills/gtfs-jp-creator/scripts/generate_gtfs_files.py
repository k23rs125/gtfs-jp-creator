"""
generate_gtfs_files.py
======================

Step 3: 構造化された中間表現 (Step 2 の出力 JSON) から、
        GTFS-JP v4.0 の CSV ファイル群を生成する。

Input:
    JSON ファイル。スキーマは references/prompts/02_structured_extraction.md を参照。

Output:
    指定ディレクトリに以下の CSV ファイルを生成:
        agency.txt          (GTFS必須)
        routes.txt          (GTFS必須)
        routes_jp.txt       (GTFS-JP拡張)
        stops.txt           (GTFS必須)
        trips.txt           (GTFS必須)
        stop_times.txt      (GTFS必須)
        calendar.txt        (GTFS必須)
        feed_info.txt       (GTFS-JP必須)

Encoding:
    UTF-8 with BOM, CRLF line endings (GTFS仕様で許容される形式)

Status:
    Phase 1 — 国際標準 GTFS と GTFS-JP の最低限の拡張に対応。
    agency_jp.txt / office_jp.txt / pattern_jp.txt は将来対応。

Usage:
    python generate_gtfs_files.py <input.json> -o <output_dir>

License: Apache 2.0
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


# GTFS-JP のデフォルト値
DEFAULT_TIMEZONE = "Asia/Tokyo"
DEFAULT_LANG = "ja"
PLACEHOLDER_URL = "https://example.com/"  # agency_url 等が無い場合のプレースホルダ
DEFAULT_START_DATE = "20250401"  # calendar.start_date のデフォルト
DEFAULT_END_DATE = "20260331"    # calendar.end_date のデフォルト


def _none_to_empty(value: Any) -> str:
    """None / 数値 / その他を CSV 用文字列に変換。"""
    if value is None:
        return ""
    return str(value)


def write_csv(output_path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """1ファイル分のCSVを書き出す（UTF-8 with BOM, CRLF）。

    GTFS仕様では UTF-8 と CRLF 改行を推奨。
    BOM (utf-8-sig) は Excel 等で正しく日本語が読めるようにするため。
    """
    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            normalized = {k: _none_to_empty(row.get(k)) for k in fieldnames}
            writer.writerow(normalized)


# ----------------------------------------------------------------------
# 各ファイル生成関数
# ----------------------------------------------------------------------

def generate_agency(data: dict, output_dir: Path) -> str:
    """agency.txt を生成。

    Returns: agency_id (routes.txt 等での参照に使用)
    """
    agency = data["agency"]
    agency_id = agency.get("agency_id") or "DEFAULT"
    row = {
        "agency_id": agency_id,
        "agency_name": agency.get("agency_name") or "Unknown Agency",
        "agency_url": agency.get("agency_url") or PLACEHOLDER_URL,
        "agency_timezone": DEFAULT_TIMEZONE,
        "agency_lang": DEFAULT_LANG,
        "agency_phone": agency.get("agency_phone") or "",
    }
    fieldnames = [
        "agency_id", "agency_name", "agency_url", "agency_timezone",
        "agency_lang", "agency_phone",
    ]
    write_csv(output_dir / "agency.txt", [row], fieldnames)
    return agency_id


def generate_routes(data: dict, output_dir: Path, default_agency_id: str) -> None:
    """routes.txt を生成 (GTFS国際標準部分)。"""
    rows = []
    for r in data["routes"]:
        rows.append({
            "route_id": r["route_id"],
            "agency_id": default_agency_id,
            "route_short_name": r.get("route_short_name") or "",
            "route_long_name": r.get("route_long_name") or "",
            "route_type": r.get("route_type", 3),  # 3 = Bus
            "route_color": r.get("route_color") or "",
        })
    fieldnames = [
        "route_id", "agency_id", "route_short_name", "route_long_name",
        "route_type", "route_color",
    ]
    write_csv(output_dir / "routes.txt", rows, fieldnames)


def generate_routes_jp(data: dict, output_dir: Path) -> None:
    """routes_jp.txt を生成 (GTFS-JP 拡張部分)。

    route_origin_stop, route_via_stop, route_destination_stop を含む。
    """
    rows = []
    today = datetime.now().strftime("%Y%m%d")
    for r in data["routes"]:
        rows.append({
            "route_id": r["route_id"],
            "route_update_date": today,  # 仮: 生成日を使う
            "origin_stop": r.get("route_origin_stop") or "",
            "via_stop": r.get("route_via_stop") or "",
            "destination_stop": r.get("route_destination_stop") or "",
        })
    fieldnames = [
        "route_id", "route_update_date",
        "origin_stop", "via_stop", "destination_stop",
    ]
    write_csv(output_dir / "routes_jp.txt", rows, fieldnames)


def generate_stops(data: dict, output_dir: Path) -> None:
    """stops.txt を生成。

    GTFS仕様では stop_lat / stop_lon は必須だが、
    GTFS-JP では位置情報が無い場合に空欄を許容するケースがある。
    """
    rows = []
    for s in data["stops"]:
        rows.append({
            "stop_id": s["stop_id"],
            "stop_name": s["stop_name"],
            "stop_lat": s.get("stop_lat"),
            "stop_lon": s.get("stop_lon"),
        })
    fieldnames = ["stop_id", "stop_name", "stop_lat", "stop_lon"]
    write_csv(output_dir / "stops.txt", rows, fieldnames)


def generate_trips(data: dict, output_dir: Path) -> None:
    """trips.txt を生成。"""
    rows = []
    for t in data["trips"]:
        rows.append({
            "route_id": t["route_id"],
            "service_id": t["service_id"],
            "trip_id": t["trip_id"],
            "trip_headsign": t.get("trip_headsign") or "",
            "direction_id": t.get("direction_id", 0),
            "shape_id": t.get("shape_id") or "",
        })
    fieldnames = [
        "route_id", "service_id", "trip_id", "trip_headsign",
        "direction_id", "shape_id",
    ]
    write_csv(output_dir / "trips.txt", rows, fieldnames)


def generate_stop_times(data: dict, output_dir: Path) -> None:
    """stop_times.txt を生成。"""
    rows = []
    for st in data["stop_times"]:
        rows.append({
            "trip_id": st["trip_id"],
            "arrival_time": st["arrival_time"],
            "departure_time": st["departure_time"],
            "stop_id": st["stop_id"],
            "stop_sequence": st["stop_sequence"],
        })
    fieldnames = [
        "trip_id", "arrival_time", "departure_time",
        "stop_id", "stop_sequence",
    ]
    write_csv(output_dir / "stop_times.txt", rows, fieldnames)


def generate_calendar(data: dict, output_dir: Path) -> None:
    """calendar.txt を生成。

    GTFS必須項目として start_date / end_date を要求するため、
    JSONで null の場合はデフォルト値を補う。
    """
    rows = []
    for c in data["calendar"]:
        rows.append({
            "service_id": c["service_id"],
            "monday": c.get("monday", 0),
            "tuesday": c.get("tuesday", 0),
            "wednesday": c.get("wednesday", 0),
            "thursday": c.get("thursday", 0),
            "friday": c.get("friday", 0),
            "saturday": c.get("saturday", 0),
            "sunday": c.get("sunday", 0),
            "start_date": c.get("start_date") or DEFAULT_START_DATE,
            "end_date": c.get("end_date") or DEFAULT_END_DATE,
        })
    fieldnames = [
        "service_id",
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
        "start_date", "end_date",
    ]
    write_csv(output_dir / "calendar.txt", rows, fieldnames)


def generate_feed_info(data: dict, output_dir: Path) -> None:
    """feed_info.txt を生成 (GTFS-JP では必須扱い)。"""
    agency = data["agency"]
    today = datetime.now().strftime("%Y%m%d")
    row = {
        "feed_publisher_name": agency.get("agency_name") or "Unknown Publisher",
        "feed_publisher_url": agency.get("agency_url") or PLACEHOLDER_URL,
        "feed_lang": DEFAULT_LANG,
        "feed_start_date": "",
        "feed_end_date": "",
        "feed_version": today,
    }
    fieldnames = [
        "feed_publisher_name", "feed_publisher_url", "feed_lang",
        "feed_start_date", "feed_end_date", "feed_version",
    ]
    write_csv(output_dir / "feed_info.txt", [row], fieldnames)


# ----------------------------------------------------------------------
# 統計表示
# ----------------------------------------------------------------------

def print_stats(output_dir: Path) -> None:
    """生成された各ファイルの行数とサイズを表示。"""
    print("\n[OK] 生成完了:", file=sys.stderr)
    for f in sorted(output_dir.glob("*.txt")):
        size = f.stat().st_size
        with f.open("r", encoding="utf-8-sig") as ff:
            line_count = sum(1 for _ in ff)
        # ヘッダー1行を除いたデータ行数
        data_rows = max(0, line_count - 1)
        print(f"  {f.name:<25} {data_rows:>5} rows  {size:>7,} bytes", file=sys.stderr)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate GTFS-JP CSV files from intermediate JSON (Step 3)",
    )
    parser.add_argument("input", help="Input JSON file (Step 2 output)")
    parser.add_argument("-o", "--output", required=True, help="Output directory for CSV files")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] 入力: {input_path}", file=sys.stderr)
    print(f"[INFO] 出力先: {output_dir}", file=sys.stderr)

    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: JSON のパース失敗: {e}", file=sys.stderr)
        sys.exit(2)

    # 必須キーの存在チェック
    required_keys = ["agency", "routes", "stops", "trips", "stop_times", "calendar"]
    missing = [k for k in required_keys if k not in data]
    if missing:
        print(f"Error: 必須キーが不足: {missing}", file=sys.stderr)
        sys.exit(3)

    # ファイル生成
    agency_id = generate_agency(data, output_dir)
    generate_routes(data, output_dir, agency_id)
    generate_routes_jp(data, output_dir)
    generate_stops(data, output_dir)
    generate_trips(data, output_dir)
    generate_stop_times(data, output_dir)
    generate_calendar(data, output_dir)
    generate_feed_info(data, output_dir)

    print_stats(output_dir)


if __name__ == "__main__":
    main()
