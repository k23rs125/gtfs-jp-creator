"""
generate_shapes.py
==================

Step 4: stop_times.txt の停留所列から shapes.txt を生成する。

Strategy (主):
    OSRM (Open Source Routing Machine) の map-matching API を使い、
    停留所のlat/lonを「実走行経路」にマッピングする。

Strategy (フォールバック):
    map-matching が失敗した場合は、停留所を直線で結んだ簡易shapeを生成。

Usage:
    python generate_shapes.py <stops.txt> <stop_times.txt> <trips.txt> -o <shapes.txt>

OSRM Public Demo Server:
    https://router.project-osrm.org/match/v1/driving/{lon,lat;...}

Status: STUB (skeleton only - implementation TBD)

License: Apache 2.0
"""

import argparse
import csv
import sys
from pathlib import Path


OSRM_BASE = "https://router.project-osrm.org/match/v1/driving"


def call_osrm_match(coords: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    """OSRM map-matching APIを呼び出して、最尤経路の(lat,lon)列を取得する。

    Returns None on failure.
    """
    # TODO: requests.get() で OSRM /match を呼び出し、geometry を取得
    raise NotImplementedError("call_osrm_match")


def fallback_straight_lines(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """OSRM失敗時のフォールバック: 停留所を単純に直線結合。"""
    return list(coords)


def generate_shapes_for_trip(trip_id: str, stop_coords: list[tuple[float, float]]) -> list[dict]:
    """1 trip分の shape rows を生成する。"""
    # OSRM試行 → 失敗ならフォールバック
    matched = call_osrm_match(stop_coords)
    if matched is None:
        matched = fallback_straight_lines(stop_coords)

    shape_id = f"shape_{trip_id}"
    rows = []
    cumulative_distance = 0.0
    for i, (lat, lon) in enumerate(matched):
        rows.append({
            "shape_id": shape_id,
            "shape_pt_lat": f"{lat:.6f}",
            "shape_pt_lon": f"{lon:.6f}",
            "shape_pt_sequence": str(i),
            "shape_dist_traveled": f"{cumulative_distance:.2f}",
        })
        # TODO: cumulative_distance を haversine で更新
    return rows


def main():
    parser = argparse.ArgumentParser(description="Generate shapes.txt via OSRM map-matching")
    parser.add_argument("stops_txt", help="Path to stops.txt")
    parser.add_argument("stop_times_txt", help="Path to stop_times.txt")
    parser.add_argument("trips_txt", help="Path to trips.txt")
    parser.add_argument("-o", "--output", required=True, help="Output shapes.txt path")
    args = parser.parse_args()

    # TODO: 各trip毎に stop_times を集約 → stop_coords列を作成 → generate_shapes_for_trip
    raise NotImplementedError("main")


if __name__ == "__main__":
    main()
