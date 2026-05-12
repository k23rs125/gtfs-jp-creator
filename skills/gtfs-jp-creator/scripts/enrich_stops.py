"""
enrich_stops.py
================

Step 3.5 (緯度経度補完): stops.txt の空欄 stop_lat/stop_lon を外部APIで埋める。

主バックエンド: Nominatim (OpenStreetMap)
将来拡張:       国土地理院 AddressSearch (fallback)、国土数値情報 P11 (offline)

Usage:
    python enrich_stops.py <stops.txt> [-o <output.txt>]
                           [--context "福岡県須恵町"]
                           [--agency-name "須恵町コミュニティバス"]
                           [--cache stops_geocache.json]
                           [--force-refresh]
                           [--rate 1.1]
                           [--report enrichment_report.json]

Encoding:
    UTF-8 with BOM (utf-8-sig) で stops.txt を読み書き。
    GTFS-JP の標準エンコーディングに準拠。

設計の根拠:
    緯度経度補完設計書_v1.md を参照。
    なぜ Nominatim を一次にしたか、4戦略クエリ、キャッシュ動作などの説明あり。

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "gtfs-jp-creator/0.1 (research; https://github.com/k23rs125/gtfs-jp-creator)"
DEFAULT_RATE_SEC = 1.1
DEFAULT_TIMEOUT_SEC = 30
QUERY_STRATEGIES = ["full_address", "agency_name", "prefecture_city", "name_only"]


# ---------------------------------------------------------------------------
# データクラス相当の dict ヘルパ
# ---------------------------------------------------------------------------

def make_cache_entry_success(lat: float, lon: float, source: str, query: str,
                              osm_class: str | None = None,
                              osm_type: str | None = None) -> dict:
    return {
        "lat": lat,
        "lon": lon,
        "source": source,
        "query": query,
        "osm_class": osm_class,
        "osm_type": osm_type,
        "cached_at": datetime.now().isoformat(timespec="seconds"),
    }


def make_cache_entry_failure(attempted_queries: list[str]) -> dict:
    return {
        "status": "not_found",
        "attempted_queries": attempted_queries,
        "cached_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# キャッシュ I/O
# ---------------------------------------------------------------------------

def load_cache(cache_path: Path) -> dict:
    if not cache_path.exists():
        return {"version": 1, "created": datetime.now().isoformat(), "entries": {}}
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if "entries" not in data:
            data["entries"] = {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: cache file unreadable ({e}); starting fresh", file=sys.stderr)
        return {"version": 1, "created": datetime.now().isoformat(), "entries": {}}


def save_cache(cache: dict, cache_path: Path) -> None:
    cache["updated"] = datetime.now().isoformat(timespec="seconds")
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# クエリ戦略生成
# ---------------------------------------------------------------------------

def build_queries(stop_name: str, context: str | None,
                  agency_name: str | None) -> list[tuple[str, str]]:
    """4戦略のクエリ列を生成する。

    Returns:
        list of (strategy_label, query_string)
    """
    queries: list[tuple[str, str]] = []
    if context:
        queries.append(("full_address", f"{stop_name} バス停 {context}"))
    if agency_name:
        queries.append(("agency_name", f"{stop_name} バス停 {agency_name}"))
    if context:
        # 県・市レベルだけ抜き出す簡易処理
        queries.append(("prefecture_city", f"{stop_name} {context.split('郡')[0] if '郡' in context else context}"))
    queries.append(("name_only", stop_name))
    # 重複排除
    seen = set()
    deduped: list[tuple[str, str]] = []
    for label, q in queries:
        if q not in seen:
            seen.add(q)
            deduped.append((label, q))
    return deduped


# ---------------------------------------------------------------------------
# Nominatim API 呼び出し
# ---------------------------------------------------------------------------

def call_nominatim(query: str, bbox: tuple[float, float, float, float] | None = None,
                    timeout: int = DEFAULT_TIMEOUT_SEC) -> list[dict] | None:
    """Nominatim search API を呼び出す。

    Args:
        query:   検索文字列
        bbox:    (lon_min, lat_min, lon_max, lat_max) — 指定すると viewbox + bounded=1 を付ける
        timeout: HTTP タイムアウト秒

    Returns:
        ヒットのリスト（最大10件）、HTTP/ネットワークエラー時は None
    """
    params = {
        "q": query,
        "format": "json",
        "addressdetails": "1",
        "limit": "10",
        "accept-language": "ja",
        "countrycodes": "jp",  # 日本のみ
    }
    if bbox is not None:
        lon_min, lat_min, lon_max, lat_max = bbox
        # Nominatim の viewbox 順序: lon_left, lat_top, lon_right, lat_bottom
        params["viewbox"] = f"{lon_min},{lat_max},{lon_max},{lat_min}"
        params["bounded"] = "1"
    url = f"{NOMINATIM_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            if status == 429:
                # Rate limited - caller should backoff
                print("  ! HTTP 429 Too Many Requests; backing off 30s", file=sys.stderr)
                time.sleep(30)
                return None
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("  ! HTTP 429 Too Many Requests; backing off 30s", file=sys.stderr)
            time.sleep(30)
        else:
            print(f"  ! HTTP error {e.code} for query: {query}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"  ! Network/parse error ({type(e).__name__}) for query: {query}", file=sys.stderr)
        return None


def matches_prefecture(result: dict, expected_prefecture: str | None) -> bool:
    """結果の address フィールドに期待される県/都/府/道が含まれるかを判定。

    expected_prefecture が None の場合は常に True。
    """
    if not expected_prefecture:
        return True
    addr = result.get("address") or {}
    # Nominatim の address は state / county / city / town / village など複数フィールドにわたる
    haystack_fields = ["state", "province", "region", "county", "city", "town", "village",
                       "municipality", "suburb", "neighbourhood", "ISO3166-2-lvl4"]
    for f in haystack_fields:
        val = addr.get(f, "")
        if val and expected_prefecture in val:
            return True
    # display_name でも検索（フォールバック）
    if expected_prefecture in (result.get("display_name") or ""):
        return True
    return False


def matches_municipality(result: dict, expected_municipality: str | None) -> bool:
    """結果の address フィールドに期待される市町村名が含まれるかを判定。

    隣町の同名施設や、bbox 内の無関係な場所を棄却するための二次フィルタ。
    expected_municipality が None の場合は常に True。
    """
    if not expected_municipality:
        return True
    addr = result.get("address") or {}
    # 市町村系フィールド優先
    municipality_fields = ["city", "town", "village", "municipality", "suburb",
                           "city_district", "town_district"]
    for f in municipality_fields:
        val = addr.get(f, "")
        if val and expected_municipality in val:
            return True
    # display_name にも市町村名が含まれていれば許容（フォールバック）
    # ただし完全一致に近い形を要求（例: "須恵町" で福岡市と区別したい）
    display_name = result.get("display_name") or ""
    if expected_municipality in display_name:
        # display_name は ", " 区切りなので、市町村名がトークンとして現れているか確認
        tokens = [t.strip() for t in display_name.split(",")]
        if any(expected_municipality in t and len(t) <= len(expected_municipality) + 5 for t in tokens):
            return True
    return False


def pick_best_candidate(results: list[dict],
                         expected_prefecture: str | None = None,
                         expected_municipality: str | None = None) -> dict | None:
    """Nominatim応答リストから最良候補を選ぶ。

    Args:
        results:               Nominatim 応答リスト
        expected_prefecture:   期待される県/都/府/道（例: "福岡県"）。指定するとこの県外は除外。
        expected_municipality: 期待される市町村名（例: "須恵町"）。指定するとこの市町村外は除外。

    優先順位:
        1. 県＆市町村と一致 AND class=highway/type=bus_stop
        2. 県＆市町村と一致 AND class=place
        3. 県＆市町村と一致 AND importance 最大
        4. （無一致なら None）
    """
    if not results:
        return None

    # 県名フィルタ
    filtered = [r for r in results if matches_prefecture(r, expected_prefecture)]
    # 市町村フィルタ
    filtered = [r for r in filtered if matches_municipality(r, expected_municipality)]

    if not filtered:
        return None

    bus_stops = [r for r in filtered if r.get("class") == "highway" and r.get("type") == "bus_stop"]
    if bus_stops:
        return bus_stops[0]

    places = [r for r in filtered if r.get("class") == "place"]
    if places:
        return places[0]

    # importance 最大
    def imp(r: dict) -> float:
        try:
            return float(r.get("importance", 0))
        except (ValueError, TypeError):
            return 0.0

    return max(filtered, key=imp)


# ---------------------------------------------------------------------------
# 1停留所の補完
# ---------------------------------------------------------------------------

def enrich_one_stop(stop_name: str, context: str | None, agency_name: str | None,
                     cache: dict, rate_sec: float, force_refresh: bool,
                     bbox: tuple[float, float, float, float] | None = None,
                     expected_prefecture: str | None = None,
                     expected_municipality: str | None = None) -> tuple[dict, int]:
    """1停留所を補完する。

    Args:
        bbox:                  Nominatim viewbox (lon_min, lat_min, lon_max, lat_max)
        expected_prefecture:   結果がこの県/都/府/道に属さない場合は棄却
        expected_municipality: 結果がこの市町村に属さない場合は棄却（隣町誤マッチ防止）

    Returns:
        (cache_entry, api_calls_made)
        cache_entry は make_cache_entry_success/failure の戻り値形式
    """
    entries = cache["entries"]

    # キャッシュヒット
    if not force_refresh and stop_name in entries:
        return entries[stop_name], 0

    api_calls = 0
    queries = build_queries(stop_name, context, agency_name)
    attempted: list[str] = []

    for strategy_label, query in queries:
        attempted.append(query)
        print(f"  [{strategy_label}] querying: {query}")
        time.sleep(rate_sec)  # レート制限遵守
        results = call_nominatim(query, bbox=bbox)
        api_calls += 1

        if results is None:
            # ネットワークエラー: 次戦略へ
            continue

        candidate = pick_best_candidate(
            results,
            expected_prefecture=expected_prefecture,
            expected_municipality=expected_municipality,
        )
        if candidate:
            try:
                lat = float(candidate["lat"])
                lon = float(candidate["lon"])
            except (KeyError, ValueError, TypeError):
                continue

            entry = make_cache_entry_success(
                lat=lat, lon=lon,
                source="nominatim",
                query=query,
                osm_class=candidate.get("class"),
                osm_type=candidate.get("type"),
            )
            # display_name も保存しておくと検証時に便利
            entry["display_name"] = candidate.get("display_name", "")
            entries[stop_name] = entry
            print(f"    ✓ found: ({lat:.6f}, {lon:.6f}) [class={candidate.get('class')}, type={candidate.get('type')}]")
            print(f"      display_name: {candidate.get('display_name', '')[:90]}")
            return entry, api_calls
        else:
            # 結果はあったが県名/市町村フィルタや候補選定で落とされた
            if results:
                n_rejected = len(results)
                filters_applied = []
                if expected_prefecture:
                    filters_applied.append(expected_prefecture)
                if expected_municipality:
                    filters_applied.append(expected_municipality)
                filter_desc = " / ".join(filters_applied) if filters_applied else "n/a"
                print(f"    ・ {n_rejected} 件ヒットしたが [{filter_desc}] と一致せず棄却")

    # 全戦略失敗
    entry = make_cache_entry_failure(attempted)
    entries[stop_name] = entry
    print(f"    ✗ not found (tried {len(attempted)} queries)")
    return entry, api_calls


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def read_stops_csv(path: Path) -> tuple[list[dict], list[str]]:
    """stops.txt を読み込み、行リストとフィールド名リストを返す。"""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def write_stops_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """stops.txt を UTF-8 BOM + CRLF で書き出す。"""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def has_existing_coords(row: dict) -> bool:
    lat = (row.get("stop_lat") or "").strip()
    lon = (row.get("stop_lon") or "").strip()
    return bool(lat) and bool(lon)


# ---------------------------------------------------------------------------
# レポート出力
# ---------------------------------------------------------------------------

def make_report(rows: list[dict], result_map: dict, total_api_calls: int,
                elapsed_sec: float, cache_hits: int) -> dict:
    total = len(rows)
    already = sum(1 for r in rows if has_existing_coords(r))
    enriched = 0
    failed = 0
    failed_details: list[dict] = []

    for row in rows:
        name = row.get("stop_name", "")
        entry = result_map.get(name)
        if entry is None:
            continue
        if "lat" in entry and "lon" in entry:
            enriched += 1
        else:
            failed += 1
            failed_details.append({
                "stop_id": row.get("stop_id"),
                "stop_name": name,
                "attempted_queries": entry.get("attempted_queries", []),
            })

    pct_success = (enriched / max(total - already, 1)) * 100

    return {
        "summary": {
            "total_stops": total,
            "already_had_coords": already,
            "newly_enriched": enriched,
            "failed": failed,
            "cache_hits": cache_hits,
            "api_calls": total_api_calls,
            "elapsed_sec": round(elapsed_sec, 1),
            "success_rate_pct": round(pct_success, 1),
        },
        "failed_stops": failed_details,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def print_summary(report: dict) -> None:
    s = report["summary"]
    print()
    print("=" * 64)
    print("ENRICHMENT REPORT")
    print("=" * 64)
    print(f"Total stops:           {s['total_stops']}")
    print(f"Already had coords:    {s['already_had_coords']}")
    print(f"Newly enriched:        {s['newly_enriched']} ({s['success_rate_pct']}%)")
    print(f"Failed:                {s['failed']}")
    print(f"Cache hits:            {s['cache_hits']}")
    print(f"API calls made:        {s['api_calls']}")
    print(f"Total time:            {s['elapsed_sec']} sec")
    if report["failed_stops"]:
        print()
        print("Failed stops:")
        for fs in report["failed_stops"][:10]:
            print(f"  {fs['stop_id']}  {fs['stop_name']}")
        if len(report["failed_stops"]) > 10:
            print(f"  ... and {len(report['failed_stops']) - 10} more (see report file)")
    print("=" * 64)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="stops.txt の空欄 stop_lat/stop_lon を Nominatim で補完する"
    )
    parser.add_argument("input", help="入力 stops.txt")
    parser.add_argument("-o", "--output", default=None,
                        help="出力 stops.txt (デフォルト: <input>.enriched.txt)")
    parser.add_argument("--context", default=None,
                        help="クエリのコンテキスト（例: '福岡県糟屋郡須恵町'）")
    parser.add_argument("--agency-name", default=None,
                        help="事業者名（例: '須恵町コミュニティバス'）")
    parser.add_argument("--cache", default="stops_geocache.json",
                        help="キャッシュファイル (デフォルト: ./stops_geocache.json)")
    parser.add_argument("--force-refresh", action="store_true",
                        help="キャッシュを無視して再取得")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_SEC,
                        help=f"レート制限の sleep 秒 (デフォルト: {DEFAULT_RATE_SEC})")
    parser.add_argument("--report", default="enrichment_report.json",
                        help="レポート出力先 (デフォルト: ./enrichment_report.json)")
    parser.add_argument("--limit", type=int, default=0,
                        help="先頭N件のみ処理（デバッグ用、0=全件）")
    parser.add_argument("--bbox", default=None,
                        help="Nominatim 検索範囲を絞る (lon_min,lat_min,lon_max,lat_max)。"
                             " 例: 130.43,33.48,130.55,33.58 で須恵町周辺。"
                             " 推奨: 同名地名の誤マッチ防止のため必ず指定。")
    parser.add_argument("--prefecture", default=None,
                        help="期待される都道府県名（例: '福岡県'）。"
                             " 指定するとこの県外の結果は棄却。"
                             " --context に県名が含まれていれば自動推定も可。")
    parser.add_argument("--municipality", default=None,
                        help="期待される市町村名（例: '須恵町'）。"
                             " 指定するとこの市町村外の結果は棄却（隣町誤マッチ防止）。"
                             " --context に市町村名が含まれていれば自動推定も可。")
    args = parser.parse_args()

    # --bbox のパース
    bbox = None
    if args.bbox:
        try:
            parts = [float(x.strip()) for x in args.bbox.split(",")]
            if len(parts) != 4:
                raise ValueError("bbox は 4 つの値が必要")
            lon_min, lat_min, lon_max, lat_max = parts
            if lon_min >= lon_max or lat_min >= lat_max:
                raise ValueError("bbox: lon_min < lon_max, lat_min < lat_max を満たす必要")
            bbox = (lon_min, lat_min, lon_max, lat_max)
        except ValueError as e:
            print(f"Error: --bbox parse failed: {e}", file=sys.stderr)
            return 1

    # --prefecture: 明示指定がなければ --context から推定
    expected_prefecture = args.prefecture
    if not expected_prefecture and args.context:
        for suffix in ("都", "道", "府", "県"):
            idx = args.context.find(suffix)
            if 0 < idx < 5:  # 先頭4文字以内に県名末尾がある
                expected_prefecture = args.context[: idx + 1]
                break

    # --municipality: 明示指定がなければ --context から推定
    # 「福岡県糟屋郡須恵町」のような形式から末尾の「須恵町」を取り出す
    expected_municipality = args.municipality
    if not expected_municipality and args.context:
        # 県名以降を切り出し
        remainder = args.context
        if expected_prefecture and remainder.startswith(expected_prefecture):
            remainder = remainder[len(expected_prefecture):]
        # 「郡」があれば、その後ろを市町村候補とする
        if "郡" in remainder:
            remainder = remainder.split("郡", 1)[1]
        # 末尾が「市」「町」「村」「区」で終わる部分を取り出す
        for suffix in ("市", "町", "村", "区"):
            idx = remainder.rfind(suffix)
            if idx > 0:
                expected_municipality = remainder[: idx + 1]
                break

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"Error: input not found: {in_path}", file=sys.stderr)
        return 1

    out_path = Path(args.output) if args.output else in_path.with_suffix(".enriched.txt")
    cache_path = Path(args.cache)
    report_path = Path(args.report)

    print(f"Input:        {in_path}")
    print(f"Output:       {out_path}")
    print(f"Cache:        {cache_path}")
    print(f"Context:      {args.context or '(none)'}")
    print(f"Prefecture:   {expected_prefecture or '(none — no filter)'}")
    print(f"Municipality: {expected_municipality or '(none — no filter)'}")
    print(f"BBox:         {bbox if bbox else '(none — global JP)'}")
    print(f"Rate:         {args.rate} sec/req")
    print()

    rows, fieldnames = read_stops_csv(in_path)
    print(f"Loaded {len(rows)} stops")

    # stop_lat/lon カラムがなければ追加
    if "stop_lat" not in fieldnames:
        fieldnames.append("stop_lat")
    if "stop_lon" not in fieldnames:
        fieldnames.append("stop_lon")

    cache = load_cache(cache_path)
    initial_cache_size = len(cache["entries"])

    total_api_calls = 0
    cache_hits = 0
    result_map: dict[str, dict] = {}

    start = time.time()
    target_rows = rows[: args.limit] if args.limit > 0 else rows

    for i, row in enumerate(target_rows, 1):
        name = (row.get("stop_name") or "").strip()
        if not name:
            continue

        # 既に座標があるならスキップ
        if has_existing_coords(row) and not args.force_refresh:
            print(f"[{i}/{len(target_rows)}] {name}: skip (already has coords)")
            continue

        print(f"[{i}/{len(target_rows)}] {name}")
        # キャッシュ確認
        was_cached = not args.force_refresh and name in cache["entries"]
        entry, api_calls = enrich_one_stop(
            stop_name=name,
            context=args.context,
            agency_name=args.agency_name,
            cache=cache,
            rate_sec=args.rate,
            force_refresh=args.force_refresh,
            bbox=bbox,
            expected_prefecture=expected_prefecture,
            expected_municipality=expected_municipality,
        )
        if was_cached and api_calls == 0:
            cache_hits += 1
        total_api_calls += api_calls
        result_map[name] = entry

        # 行に反映
        if "lat" in entry and "lon" in entry:
            row["stop_lat"] = f"{entry['lat']:.6f}"
            row["stop_lon"] = f"{entry['lon']:.6f}"

        # 10件ごとにキャッシュをセーブ（クラッシュ耐性）
        if i % 10 == 0:
            save_cache(cache, cache_path)

    elapsed = time.time() - start

    # 最終保存
    save_cache(cache, cache_path)
    write_stops_csv(out_path, rows, fieldnames)

    report = make_report(rows, result_map, total_api_calls, elapsed, cache_hits)
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print_summary(report)
    print(f"\nOutput written:  {out_path}")
    print(f"Cache updated:   {cache_path}  ({len(cache['entries']) - initial_cache_size} new entries)")
    print(f"Report saved:    {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
