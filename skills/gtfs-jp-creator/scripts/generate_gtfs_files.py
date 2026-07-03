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
        agency_jp.txt       (GTFS-JP拡張・必須)
        routes.txt          (GTFS必須)
        routes_jp.txt       (GTFS-JP拡張)
        stops.txt           (GTFS必須)
        trips.txt           (GTFS必須)
        stop_times.txt      (GTFS必須)
        calendar.txt        (GTFS必須)
        calendar_dates.txt  (任意・PDFに記載があれば)
        fare_attributes.txt (任意・PDFに記載があれば)
        fare_rules.txt      (任意・PDFに記載があれば)
        feed_info.txt       (GTFS-JP必須)
        office_jp.txt       (GTFS-JP拡張・任意・営業所情報があれば)

Encoding:
    UTF-8 with BOM, CRLF line endings (GTFS仕様で許容される形式)

条件確認画面との連携:
    入力 JSON の `_meta.user_overrides`（"table.field" 形式のキー）に
    ユーザーが条件確認画面で上書きした値があれば、生成前に最終値へ反映する。
    対象テーブル: agency / agency_jp / feed_info。

Status:
    Phase 1.1 ― 国際標準 GTFS に加え、GTFS-JP 拡張 (agency_jp / office_jp /
    routes_jp) と条件確認画面の user_overrides に対応。pattern_jp.txt は将来対応。

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
DEFAULT_START_DATE = "20250401"  # calendar.start_date のデフォルト（年度初め）
DEFAULT_END_DATE = "20991231"    # 究極のフォールバック（実質期限なし）


def compute_default_end_date(start_date_str: str | None) -> str:
    """start_date から日本の年度末（3/31）を計算する。

    例:
      start_date 20260601 → end_date 20270331（FY2026 年度末）
      start_date 20260301 → end_date 20260331（FY2025 年度末）
      start_date None     → DEFAULT_END_DATE

    GTFS Validator の `start_and_end_range_out_of_order` エラー回避が目的。
    end_date が必ず start_date より後になるよう保証する。
    """
    if not start_date_str:
        return DEFAULT_END_DATE
    try:
        sd = datetime.strptime(start_date_str, "%Y%m%d")
    except (ValueError, TypeError):
        return DEFAULT_END_DATE
    # 日本の年度: 4/1 から始まる
    end_year = sd.year + 1 if sd.month >= 4 else sd.year
    return f"{end_year}0331"


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
# 条件確認画面からの上書き反映
# ----------------------------------------------------------------------

# user_overrides を反映できるテーブル（いずれも単一レコードのセクション）
OVERRIDABLE_TABLES = {"agency", "agency_jp", "feed_info"}


def apply_user_overrides(data: dict) -> list[str]:
    """`_meta.user_overrides` を中間表現の最終値に反映する。

    条件確認画面（v2 設計）でユーザーが上書きした値は、
    `_meta.user_overrides` に "table.field" 形式のキーで格納される。
    例: {"agency.agency_url": "https://...", "agency_jp.agency_zip_number": "811-2192"}

    Returns: 反映したキーの一覧（ログ表示用）。
    """
    overrides = (data.get("_meta") or {}).get("user_overrides") or {}
    applied: list[str] = []
    for key, value in overrides.items():
        if "." not in key:
            print(f"[WARN] user_overrides: 不正なキー形式 '{key}' をスキップ", file=sys.stderr)
            continue
        table, field = key.split(".", 1)
        if table not in OVERRIDABLE_TABLES:
            print(f"[WARN] user_overrides: 未対応テーブル '{table}' をスキップ "
                  f"(対応: {sorted(OVERRIDABLE_TABLES)})", file=sys.stderr)
            continue
        section = data.get(table)
        if not isinstance(section, dict):
            section = {}
            data[table] = section
        section[field] = value
        applied.append(key)
    return applied


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


def generate_agency_jp(data: dict, output_dir: Path, default_agency_id: str) -> None:
    """agency_jp.txt を生成 (GTFS-JP 拡張・必須)。

    事業者の日本固有情報（正式名称・住所・代表者など）。
    PDF からは取得できない値が多く、条件確認画面でユーザーが入力する。
    未入力の項目は空欄のまま出力する（GTFS-JP ファイル自体は必ず生成）。
    """
    aj = data.get("agency_jp") or {}
    ag = data.get("agency") or {}

    # 後方互換: 旧スキーマでは agency_jp 系の項目が agency に直接入っていた
    def pick(key: str) -> str:
        return aj.get(key) or ag.get(key) or ""

    row = {
        # JP拡張レコードは必ずフィード本体の事業者を指す。spec が AGENCY_TBD 等の
        # プレースホルダや異なる値を持っていても agency.txt の agency_id に統一する。
        "agency_id": default_agency_id,
        "agency_official_name": pick("agency_official_name"),
        "agency_zip_number": pick("agency_zip_number"),
        "agency_address": pick("agency_address"),
        "agency_president_pos": pick("agency_president_pos"),
        "agency_president_name": pick("agency_president_name"),
    }
    fieldnames = [
        "agency_id", "agency_official_name", "agency_zip_number",
        "agency_address", "agency_president_pos", "agency_president_name",
    ]
    write_csv(output_dir / "agency_jp.txt", [row], fieldnames)


def generate_office_jp(data: dict, output_dir: Path) -> bool:
    """office_jp.txt を生成 (GTFS-JP 拡張・任意)。

    営業所情報。`office_jp` セクションがある場合のみ生成する。
    単一の dict でも、複数営業所の list でも受け付ける。

    Returns: 生成したら True、営業所情報が無くスキップしたら False。
    """
    offices = data.get("office_jp")
    if not offices:
        return False
    if isinstance(offices, dict):
        offices = [offices]
    rows = []
    for i, o in enumerate(offices, start=1):
        if not isinstance(o, dict):
            continue
        rows.append({
            "office_id": o.get("office_id") or f"OFFICE{i:02d}",
            "office_name": o.get("office_name") or "",
            "office_url": o.get("office_url") or "",
            "office_phone": o.get("office_phone") or "",
        })
    if not rows:
        return False
    fieldnames = ["office_id", "office_name", "office_url", "office_phone"]
    write_csv(output_dir / "office_jp.txt", rows, fieldnames)
    return True


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
    has_zone = any(s.get("zone_id") for s in data["stops"])
    has_desc = any((s.get("stop_desc") or "").strip() for s in data["stops"])
    for s in data["stops"]:
        row = {
            "stop_id": s["stop_id"],
            "stop_name": s["stop_name"],
            "stop_lat": s.get("stop_lat"),
            "stop_lon": s.get("stop_lon"),
        }
        if has_desc:
            row["stop_desc"] = (s.get("stop_desc") or "").strip()   # 方面（行き/帰りの向き）
        if has_zone:
            row["zone_id"] = s.get("zone_id") or ""
        rows.append(row)
    fieldnames = ["stop_id", "stop_name"]
    if has_desc:
        fieldnames.append("stop_desc")
    fieldnames += ["stop_lat", "stop_lon"]
    if has_zone:
        fieldnames.append("zone_id")   # 区間運賃(zone制)のとき出力
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
    """stop_times.txt を生成。

    要予約バス停（stop_name に「要予約」を含む）を通る行には
    pickup_type / drop_off_type = 2（電話で営業所に連絡）を付与し、
    デマンド型停留所であることを GTFS 標準の方法で表現する。
    中間JSONに pickup_type / drop_off_type の明示指定があればそれを優先する。
    """
    id_to_name = {s["stop_id"]: s.get("stop_name", "") for s in data.get("stops", [])}
    rows = []
    for st in data["stop_times"]:
        sid = st["stop_id"]
        name = id_to_name.get(sid, "")
        is_reserve = "要予約" in name
        pickup = st.get("pickup_type")
        dropoff = st.get("drop_off_type")
        if pickup is None:
            pickup = 2 if is_reserve else 0
        if dropoff is None:
            dropoff = 2 if is_reserve else 0
        rows.append({
            "trip_id": st["trip_id"],
            "arrival_time": st["arrival_time"],
            "departure_time": st["departure_time"],
            "stop_id": sid,
            "stop_sequence": st["stop_sequence"],
            "pickup_type": pickup,
            "drop_off_type": dropoff,
        })
    fieldnames = [
        "trip_id", "arrival_time", "departure_time",
        "stop_id", "stop_sequence", "pickup_type", "drop_off_type",
    ]
    write_csv(output_dir / "stop_times.txt", rows, fieldnames)


def generate_calendar(data: dict, output_dir: Path) -> None:
    """calendar.txt を生成。

    GTFS必須項目として start_date / end_date を要求するため、
    JSONで null の場合はデフォルト値を補う。
    end_date が null の場合は start_date から日本の年度末を自動計算する。
    （GTFS Validator の start_and_end_range_out_of_order エラー対策）
    """
    rows = []
    for c in data["calendar"]:
        start_date = c.get("start_date") or DEFAULT_START_DATE
        # end_date が無ければ start_date から年度末を計算
        end_date = c.get("end_date") or compute_default_end_date(start_date)
        rows.append({
            "service_id": c["service_id"],
            "monday": c.get("monday", 0),
            "tuesday": c.get("tuesday", 0),
            "wednesday": c.get("wednesday", 0),
            "thursday": c.get("thursday", 0),
            "friday": c.get("friday", 0),
            "saturday": c.get("saturday", 0),
            "sunday": c.get("sunday", 0),
            "start_date": start_date,
            "end_date": end_date,
        })
    fieldnames = [
        "service_id",
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
        "start_date", "end_date",
    ]
    write_csv(output_dir / "calendar.txt", rows, fieldnames)


def generate_calendar_dates(data: dict, output_dir: Path) -> None:
    """calendar_dates.txt を生成 (例外日: 運休日・祝日・年末年始など)。

    入力 JSON に calendar_dates 配列が無いか空なら、ファイルを生成しない。
    """
    cdates = data.get("calendar_dates") or []
    if not cdates:
        return  # 例外日が無いならファイル作らない
    rows = []
    for c in cdates:
        rows.append({
            "service_id": c["service_id"],
            "date": c["date"],
            "exception_type": c.get("exception_type", 2),
            "comment": c.get("comment", "") or "",
        })
    fieldnames = ["service_id", "date", "exception_type", "comment"]
    write_csv(output_dir / "calendar_dates.txt", rows, fieldnames)


def generate_fare_attributes(data: dict, output_dir: Path, default_agency_id: str) -> None:
    """fare_attributes.txt を生成 (運賃情報)。

    入力 JSON に fare_attributes 配列が無いか空なら、ファイルを生成しない。
    """
    fares = data.get("fare_attributes") or []
    if not fares:
        return
    rows = []
    for f in fares:
        # fare は必ずフィードの事業者に属する。spec が AGENCY_TBD 等のプレースホルダや
        # agency と異なる値を持っていても agency.txt の agency_id に強制統一し、
        # fare_attributes.agency_id の foreign_key_violation 再発を決定的に防ぐ。
        spec_aid = f.get("agency_id")
        if spec_aid and spec_aid != default_agency_id:
            print(f"[WARN] fare '{f.get('fare_id')}' の agency_id '{spec_aid}' を "
                  f"agency '{default_agency_id}' に統一しました", file=sys.stderr)
        rows.append({
            "agency_id": default_agency_id,
            "fare_id": f["fare_id"],
            "price": f["price"],
            "currency_type": f.get("currency_type") or "JPY",
            "payment_method": f.get("payment_method", 0),
            "transfers": f.get("transfers", 0),
        })
    fieldnames = [
        "agency_id", "fare_id", "price",
        "currency_type", "payment_method", "transfers",
    ]
    write_csv(output_dir / "fare_attributes.txt", rows, fieldnames)


def generate_fare_rules(data: dict, output_dir: Path) -> None:
    """fare_rules.txt を生成 (運賃と route の対応規則)。

    入力 JSON に fare_rules 配列が無いか空なら、ファイルを生成しない。
    """
    rules = data.get("fare_rules") or []
    if not rules:
        return
    rows = []
    for r in rules:
        rows.append({
            "fare_id": r["fare_id"],
            "route_id": r.get("route_id") or "",
            "origin_id": r.get("origin_id") or "",
            "destination_id": r.get("destination_id") or "",
            "contains_id": r.get("contains_id") or "",
        })
    fieldnames = ["fare_id", "route_id", "origin_id", "destination_id", "contains_id"]
    write_csv(output_dir / "fare_rules.txt", rows, fieldnames)


def generate_feed_info(data: dict, output_dir: Path) -> None:
    """feed_info.txt を生成 (GTFS-JP では必須扱い)。

    `feed_info` セクション（条件確認画面でユーザーが入力・上書き可能）が
    あればその値を優先し、無ければ agency 情報から既定値を導出する。
    """
    agency = data["agency"]
    fi = data.get("feed_info") or {}
    today = datetime.now().strftime("%Y%m%d")
    row = {
        "feed_publisher_name": fi.get("feed_publisher_name")
        or agency.get("agency_name") or "Unknown Publisher",
        "feed_publisher_url": fi.get("feed_publisher_url")
        or agency.get("agency_url") or PLACEHOLDER_URL,
        "feed_lang": DEFAULT_LANG,
        "feed_start_date": fi.get("feed_start_date") or "",
        "feed_end_date": fi.get("feed_end_date") or "",
        "feed_version": fi.get("feed_version") or today,
    }
    fieldnames = [
        "feed_publisher_name", "feed_publisher_url", "feed_lang",
        "feed_start_date", "feed_end_date", "feed_version",
    ]
    # 連絡先（GTFS/GTFS-JP 推奨。feed_info か agency.email から補完して出力する。
    # 無いと Validator が missing_feed_contact_email_and_url を出すため）。
    contact_email = fi.get("feed_contact_email") or agency.get("agency_email") or ""
    contact_url = fi.get("feed_contact_url") or ""
    if contact_email:
        row["feed_contact_email"] = contact_email
        fieldnames.append("feed_contact_email")
    if contact_url:
        row["feed_contact_url"] = contact_url
        fieldnames.append("feed_contact_url")
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

    # 条件確認画面でユーザーが上書きした値を反映
    applied = apply_user_overrides(data)
    if applied:
        print(f"[INFO] 条件確認の上書きを {len(applied)} 件反映: "
              f"{', '.join(applied)}", file=sys.stderr)

    # ファイル生成
    agency_id = generate_agency(data, output_dir)
    generate_agency_jp(data, output_dir, agency_id)     # GTFS-JP拡張・必須
    generate_routes(data, output_dir, agency_id)
    generate_routes_jp(data, output_dir)
    generate_stops(data, output_dir)
    generate_trips(data, output_dir)
    generate_stop_times(data, output_dir)
    generate_calendar(data, output_dir)
    generate_calendar_dates(data, output_dir)          # 例外日
    generate_fare_attributes(data, output_dir, agency_id)  # 運賃
    generate_fare_rules(data, output_dir)              # 運賃ルール
    generate_feed_info(data, output_dir)
    if generate_office_jp(data, output_dir):           # GTFS-JP拡張・任意
        print("[INFO] office_jp.txt を生成しました（営業所情報あり）", file=sys.stderr)

    print_stats(output_dir)


if __name__ == "__main__":
    main()
