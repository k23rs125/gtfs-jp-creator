"""
generate_shapes.py
==================

Step 4: stop_times.txt の停留所列から shapes.txt を生成する。

Strategy (主):
    OSRM (Open Source Routing Machine) の map-matching API を使い、
    停留所の lat/lon を「実走行経路」にマッピングする。

Strategy (フォールバック):
    map-matching が失敗 or 不可能 (座標欠落多数) の場合、
    停留所を直線で結んだ簡易 shape を生成。

設計の根拠:
    shapes生成設計書_v1.md を参照。

Usage:
    python generate_shapes.py <stops.txt> <stop_times.txt> <trips.txt> -o <shapes.txt>
        [--cache shapes_cache.json]
        [--rate 1.1]
        [--no-osrm]                   # OSRM を呼ばず直線のみ
        [--update-trips trips_out.txt] # shape_id 付与済み trips を別ファイルに書き出す
        [--report shapes_report.json]

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
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

OSRM_BASE = "https://router.project-osrm.org/match/v1/driving"
USER_AGENT = "gtfs-jp-creator/0.1 (research; https://github.com/k23rs125/gtfs-jp-creator)"
DEFAULT_RATE_SEC = 1.1
DEFAULT_TIMEOUT_SEC = 30
EARTH_RADIUS_M = 6_371_000
OSRM_MAX_COORDS = 100  # OSRM の上限


# ---------------------------------------------------------------------------
# 数学ヘルパ
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の直線距離（メートル）を Haversine 公式で計算。"""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_M * c


def cumulative_distances(points: list[tuple[float, float]]) -> list[float]:
    """点列の累積距離（メートル）を返す。先頭は 0.0。"""
    if not points:
        return []
    dists = [0.0]
    for i in range(1, len(points)):
        prev_lat, prev_lon = points[i - 1]
        cur_lat, cur_lon = points[i]
        dists.append(dists[-1] + haversine_m(prev_lat, prev_lon, cur_lat, cur_lon))
    return dists


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def read_csv_dict(path: Path) -> tuple[list[dict], list[str]]:
    """CSV を読んで (rows, fieldnames) を返す。UTF-8 BOM 対応。"""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def write_csv_dict(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    """UTF-8 BOM + CRLF で書き出す（GTFS 仕様）。"""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


# ---------------------------------------------------------------------------
# OSRM API 呼び出し
# ---------------------------------------------------------------------------

def call_osrm_match(coords_lat_lon: list[tuple[float, float]],
                     timeout: int = DEFAULT_TIMEOUT_SEC,
                     osrm_base: str = OSRM_BASE) -> list[tuple[float, float]] | None:
    """OSRM map-matching を呼んで、最尤経路の点列 [(lat, lon), ...] を返す。

    Args:
        coords_lat_lon: 停留所の (lat, lon) 列。順序は trip 通り。
        timeout:        HTTP タイムアウト秒
        osrm_base:      OSRM サーバーのベース URL

    Returns:
        マッチング成功時: 経路上の点列 [(lat, lon), ...]
        失敗時 (HTTP/JSON エラー、code != "Ok"): None
    """
    if len(coords_lat_lon) < 2:
        return None
    if len(coords_lat_lon) > OSRM_MAX_COORDS:
        # OSRM の上限を超えるので、先頭から OSRM_MAX_COORDS 点に絞る
        coords_lat_lon = coords_lat_lon[:OSRM_MAX_COORDS]

    # OSRM は lon,lat 順
    coord_str = ";".join(f"{lon},{lat}" for lat, lon in coords_lat_lon)
    params = {
        "geometries": "geojson",
        "overview": "full",
        "radiuses": ";".join(["50"] * len(coords_lat_lon)),  # 50m 探索半径
    }
    url = f"{osrm_base}/{coord_str}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("  ! OSRM HTTP 429 Too Many Requests; backing off 30s", file=sys.stderr)
            time.sleep(30)
        else:
            print(f"  ! OSRM HTTP error {e.code}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"  ! OSRM network/parse error: {type(e).__name__}", file=sys.stderr)
        return None

    if data.get("code") != "Ok":
        print(f"  ! OSRM code={data.get('code')}", file=sys.stderr)
        return None

    matchings = data.get("matchings", [])
    if not matchings:
        return None

    # 複数の matching が返る場合があるが、最初を使う
    geom = matchings[0].get("geometry", {})
    coords = geom.get("coordinates", [])
    # GeoJSON は [lon, lat]、本ツール内では (lat, lon) に揃える
    return [(lat, lon) for lon, lat in coords]


def fallback_straight_lines(coords_lat_lon: list[tuple[float, float]]
                              ) -> list[tuple[float, float]]:
    """OSRM 失敗時のフォールバック。停留所を順に直線結合した点列をそのまま返す。"""
    return list(coords_lat_lon)


# ---------------------------------------------------------------------------
# trip → stop_id 列
# ---------------------------------------------------------------------------

def build_trip_stop_sequences(stop_times: list[dict]) -> dict[str, list[str]]:
    """stop_times を trip_id 別に集約して、stop_sequence 順の stop_id 列を返す。"""
    by_trip: dict[str, list[tuple[int, str]]] = {}
    for row in stop_times:
        trip_id = row.get("trip_id")
        stop_id = row.get("stop_id")
        try:
            seq = int(row.get("stop_sequence") or 0)
        except (ValueError, TypeError):
            seq = 0
        if not trip_id or not stop_id:
            continue
        by_trip.setdefault(trip_id, []).append((seq, stop_id))

    result: dict[str, list[str]] = {}
    for trip_id, pairs in by_trip.items():
        pairs.sort(key=lambda x: x[0])
        result[trip_id] = [stop_id for _, stop_id in pairs]
    return result


def pattern_key(stop_ids: list[str]) -> str:
    """stop_id 列のパターンキー（キャッシュ用）。"""
    return "|".join(stop_ids)


def short_hash(text: str) -> str:
    """SHA-1 の先頭 8 文字を返す（shape_id 命名用）。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# shape 生成（1パターン分）
# ---------------------------------------------------------------------------

def coords_for_stop_ids(stop_ids: list[str], stop_coord_map: dict[str, tuple[float, float]]
                          ) -> tuple[list[tuple[float, float]], int]:
    """stop_id 列を (lat, lon) 列に変換。座標欠落分はスキップして件数を返す。

    Returns:
        (coords, skipped_count)
    """
    coords: list[tuple[float, float]] = []
    skipped = 0
    for sid in stop_ids:
        c = stop_coord_map.get(sid)
        if c is None:
            skipped += 1
            continue
        coords.append(c)
    return coords, skipped


def make_shape_rows(shape_id: str, points: list[tuple[float, float]]) -> list[dict]:
    """shape の点列から shapes.txt の行を生成する。"""
    dists = cumulative_distances(points)
    rows: list[dict] = []
    for i, (lat, lon) in enumerate(points):
        rows.append({
            "shape_id": shape_id,
            "shape_pt_lat": f"{lat:.6f}",
            "shape_pt_lon": f"{lon:.6f}",
            "shape_pt_sequence": str(i),
            "shape_dist_traveled": f"{dists[i]:.2f}",
        })
    return rows


# ---------------------------------------------------------------------------
# キャッシュ I/O
# ---------------------------------------------------------------------------

def load_cache(cache_path: Path) -> dict:
    if not cache_path.exists():
        return {"version": 1, "created": datetime.now().isoformat(timespec="seconds"),
                "patterns": {}}
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("patterns", {})
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: cache unreadable ({e}); starting fresh", file=sys.stderr)
        return {"version": 1, "created": datetime.now().isoformat(timespec="seconds"),
                "patterns": {}}


def save_cache(cache: dict, cache_path: Path) -> None:
    cache["updated"] = datetime.now().isoformat(timespec="seconds")
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate shapes.txt via OSRM map-matching (with straight-line fallback)"
    )
    parser.add_argument("stops_txt", help="入力 stops.txt（緯度経度付き）")
    parser.add_argument("stop_times_txt", help="入力 stop_times.txt")
    parser.add_argument("trips_txt", help="入力 trips.txt")
    parser.add_argument("-o", "--output", required=True, help="出力 shapes.txt")
    parser.add_argument("--cache", default="shapes_cache.json",
                        help="キャッシュ JSON (デフォルト ./shapes_cache.json)")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_SEC,
                        help=f"OSRM 呼び出し間隔 秒 (デフォルト {DEFAULT_RATE_SEC})")
    parser.add_argument("--no-osrm", action="store_true",
                        help="OSRM を呼ばず直線フォールバックのみ使用")
    parser.add_argument("--update-trips", default=None,
                        help="shape_id を付与した trips.txt を別ファイルに書き出す")
    parser.add_argument("--report", default="shapes_report.json",
                        help="レポート出力先 (デフォルト ./shapes_report.json)")
    parser.add_argument("--osrm-url", default=OSRM_BASE,
                        help=f"OSRM ベース URL (デフォルト {OSRM_BASE})")
    args = parser.parse_args()

    stops_path = Path(args.stops_txt)
    stop_times_path = Path(args.stop_times_txt)
    trips_path = Path(args.trips_txt)
    out_path = Path(args.output)
    cache_path = Path(args.cache)
    report_path = Path(args.report)

    for p in [stops_path, stop_times_path, trips_path]:
        if not p.exists():
            print(f"Error: input not found: {p}", file=sys.stderr)
            return 1

    print(f"Stops:       {stops_path}")
    print(f"Stop times:  {stop_times_path}")
    print(f"Trips:       {trips_path}")
    print(f"Output:      {out_path}")
    print(f"Cache:       {cache_path}")
    print(f"OSRM:        {'disabled (--no-osrm)' if args.no_osrm else args.osrm_url}")
    print()

    # --- 入力読み込み ---
    stops, _ = read_csv_dict(stops_path)
    stop_times, _ = read_csv_dict(stop_times_path)
    trips, trip_fields = read_csv_dict(trips_path)

    # stop_id → (lat, lon) マップ作成
    stop_coord_map: dict[str, tuple[float, float]] = {}
    for s in stops:
        lat_str = (s.get("stop_lat") or "").strip()
        lon_str = (s.get("stop_lon") or "").strip()
        if not lat_str or not lon_str:
            continue
        try:
            stop_coord_map[s["stop_id"]] = (float(lat_str), float(lon_str))
        except (ValueError, KeyError):
            continue

    print(f"Loaded: {len(stops)} stops ({len(stop_coord_map)} with coords),"
          f" {len(stop_times)} stop_times, {len(trips)} trips")

    if not stop_coord_map:
        print("Error: 座標を持つ stops が0件です。先に Step 3.5 (enrich_stops.py) を実行してください。",
              file=sys.stderr)
        return 1

    # --- trip ごとの stop_id 列を構築 ---
    trip_stops_seq = build_trip_stop_sequences(stop_times)
    print(f"Trips with stop sequence: {len(trip_stops_seq)}")

    # --- trip ごとに route_id を引けるよう map 作成 ---
    trip_route_map: dict[str, str] = {}
    for t in trips:
        trip_route_map[t.get("trip_id", "")] = t.get("route_id", "") or "unknown"

    # --- パターンごとに shape を生成 ---
    cache = load_cache(cache_path)
    patterns_cache = cache["patterns"]

    # pattern_key → (shape_id, list[stop_ids], list[lat,lon points])
    pattern_to_shape: dict[str, dict] = {}
    # trip_id → shape_id
    trip_shape_map: dict[str, str] = {}
    # 統計
    n_skipped_no_coords = 0
    n_osrm_success = 0
    n_osrm_fail_fallback = 0
    n_cache_hits = 0
    n_api_calls = 0

    start = time.time()

    for i, (trip_id, stop_ids) in enumerate(trip_stops_seq.items(), 1):
        route_id = trip_route_map.get(trip_id, "unknown")
        key = pattern_key(stop_ids)

        # 既にこのパターンの shape が作られていればそれを再利用
        if key in pattern_to_shape:
            trip_shape_map[trip_id] = pattern_to_shape[key]["shape_id"]
            continue

        # キャッシュ
        if not args.no_osrm and key in patterns_cache:
            entry = patterns_cache[key]
            pattern_to_shape[key] = {
                "shape_id": entry["shape_id"],
                "points": [tuple(p) for p in entry["points"]],
                "source": entry.get("source", "cache"),
            }
            trip_shape_map[trip_id] = entry["shape_id"]
            n_cache_hits += 1
            print(f"[{i}/{len(trip_stops_seq)}] {trip_id}: cache hit ({entry['shape_id']})")
            continue

        # 座標列に変換
        coords, skipped = coords_for_stop_ids(stop_ids, stop_coord_map)
        if len(coords) < 2:
            print(f"[{i}/{len(trip_stops_seq)}] {trip_id}: skip (座標 {len(coords)} 点のみ, "
                  f"未補完 {skipped} 件)")
            n_skipped_no_coords += 1
            continue

        # shape_id 命名
        shape_id = f"shape_{route_id}_{short_hash(key)}"

        # OSRM 試行 or 直線
        used_source = "fallback"
        points: list[tuple[float, float]] | None = None
        if not args.no_osrm:
            print(f"[{i}/{len(trip_stops_seq)}] {trip_id}: OSRM 呼び出し ({len(coords)} 点)")
            time.sleep(args.rate)
            n_api_calls += 1
            points = call_osrm_match(coords, osrm_base=args.osrm_url)
            if points is not None:
                used_source = "osrm"
                n_osrm_success += 1
            else:
                n_osrm_fail_fallback += 1

        if points is None:
            points = fallback_straight_lines(coords)
            if used_source != "osrm":
                print(f"  → 直線フォールバック ({len(coords)} 点)")

        pattern_to_shape[key] = {
            "shape_id": shape_id,
            "points": points,
            "source": used_source,
        }
        trip_shape_map[trip_id] = shape_id

        # キャッシュ更新（OSRM の結果は再利用したい）
        patterns_cache[key] = {
            "shape_id": shape_id,
            "points": [[lat, lon] for lat, lon in points],
            "source": used_source,
            "cached_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 10 パターンごとに保存（耐障害性）
        if len(pattern_to_shape) % 10 == 0:
            save_cache(cache, cache_path)

    elapsed = time.time() - start

    # --- shapes.txt 書き出し ---
    shapes_rows: list[dict] = []
    for key, info in pattern_to_shape.items():
        shapes_rows.extend(make_shape_rows(info["shape_id"], info["points"]))

    fieldnames = ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence",
                  "shape_dist_traveled"]
    write_csv_dict(out_path, shapes_rows, fieldnames)

    # --- trips.txt 更新（任意）---
    if args.update_trips:
        if "shape_id" not in trip_fields:
            trip_fields.append("shape_id")
        for t in trips:
            tid = t.get("trip_id")
            t["shape_id"] = trip_shape_map.get(tid, "") if tid else ""
        write_csv_dict(Path(args.update_trips), trips, trip_fields)

    # --- キャッシュ最終保存 ---
    save_cache(cache, cache_path)

    # --- レポート ---
    report = {
        "summary": {
            "total_trips": len(trip_stops_seq),
            "unique_patterns": len(pattern_to_shape),
            "trips_with_shape": len(trip_shape_map),
            "trips_skipped_no_coords": n_skipped_no_coords,
            "osrm_success": n_osrm_success,
            "osrm_failed_fallback": n_osrm_fail_fallback,
            "cache_hits": n_cache_hits,
            "api_calls": n_api_calls,
            "elapsed_sec": round(elapsed, 1),
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # --- サマリ表示 ---
    s = report["summary"]
    print()
    print("=" * 64)
    print("SHAPES REPORT")
    print("=" * 64)
    print(f"Total trips:              {s['total_trips']}")
    print(f"Unique patterns:          {s['unique_patterns']}")
    print(f"Trips with shape_id:      {s['trips_with_shape']}")
    print(f"Trips skipped (座標不足):  {s['trips_skipped_no_coords']}")
    print(f"OSRM success:             {s['osrm_success']}")
    print(f"OSRM failed → fallback:   {s['osrm_failed_fallback']}")
    print(f"Cache hits:               {s['cache_hits']}")
    print(f"API calls:                {s['api_calls']}")
    print(f"Elapsed:                  {s['elapsed_sec']} sec")
    print("=" * 64)
    print(f"shapes.txt written:    {out_path}  ({len(shapes_rows)} rows)")
    if args.update_trips:
        print(f"trips updated:         {args.update_trips}")
    print(f"Cache:                 {cache_path}  ({len(patterns_cache)} patterns)")
    print(f"Report:                {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
