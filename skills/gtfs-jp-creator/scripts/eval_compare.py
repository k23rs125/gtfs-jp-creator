"""
eval_compare.py (v2)
====================

EVAL-01, EVAL-02: 既存GTFS-JPデータと、本Skill が生成したCSVを比較して
                  精度メトリクスを計算する。

v2 改善:
    - 全角/半角空白の正規化（"正信会　水戸病院前" 問題対応）
    - Unicode NFKC 正規化
    - route_long_name から先頭数字を抽出してマッチング
      （公式は route_short_name 空、"1 一番田～上須恵線" 形式のため）
    - route 経由で trips/stop_times の対応関係も改善

Usage:
    python eval_compare.py \\
        --official "feed_suetown_*.zip" \\
        --ours .\\test_demo_csv \\
        -o eval_report.md \\
        --json eval_report.json

License: Apache 2.0
"""

import argparse
import csv
import json
import re
import sys
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory


# ----------------------------------------------------------------------
# 正規化ユーティリティ
# ----------------------------------------------------------------------

def normalize_name(s: str) -> str:
    """名前の正規化:
    - NFKC で全角英数字を半角に
    - 全角空白 (U+3000) を半角空白に
    - 連続空白を1つに
    - 前後空白を除去
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("　", " ")  # 念のため明示的に
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def extract_route_number(route_long_name: str) -> str:
    """'1 一番田～上須恵線' から '1' を取り出す。
    取り出せなければ空文字。
    """
    if not route_long_name:
        return ""
    s = normalize_name(route_long_name)
    m = re.match(r"^(\d+)\s+", s)
    if m:
        return m.group(1)
    return ""


def get_route_matchkey(route: dict) -> str:
    """route から比較用キーを取り出す。
    1. route_short_name があればそれ
    2. 無ければ route_long_name から先頭数字を抽出（"1 一番田～上須恵線" 形式）
    3. 数字も無ければ route_long_name 全体を正規化してキーにする
       （"JR古賀線" "小竹線" のような番号なし路線に対応）
    """
    short = (route.get("route_short_name") or "").strip()
    if short:
        return normalize_name(short)
    num = extract_route_number(route.get("route_long_name", ""))
    if num:
        return num
    # 番号なし路線は route_long_name 全体をマッチキーにする
    return normalize_name(route.get("route_long_name", ""))


def strip_route_number(route_long_name: str) -> str:
    """'1 一番田～上須恵線' から番号を除いた '一番田～上須恵線' を返す。"""
    s = normalize_name(route_long_name)
    return re.sub(r"^\d+\s+", "", s)


# ----------------------------------------------------------------------
# CSV 読み込み
# ----------------------------------------------------------------------

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ----------------------------------------------------------------------
# テーブル比較
# ----------------------------------------------------------------------

def compare_routes(off_dir: Path, our_dir: Path) -> dict:
    off = read_csv(off_dir / "routes.txt")
    ours = read_csv(our_dir / "routes.txt")

    # マッチキー: route_short_name または route_long_name から番号
    off_by_key = {}
    for r in off:
        key = get_route_matchkey(r)
        if key:
            off_by_key[key] = r
    our_by_key = {}
    for r in ours:
        key = get_route_matchkey(r)
        if key:
            our_by_key[key] = r

    common = set(off_by_key) & set(our_by_key)
    only_official = set(off_by_key) - set(our_by_key)
    only_ours = set(our_by_key) - set(off_by_key)

    # 路線名（番号を除いたもの）の一致率
    name_matches = 0
    name_match_details = []
    for key in sorted(common, key=lambda x: (len(x), x)):
        off_name = strip_route_number(off_by_key[key].get("route_long_name", ""))
        # 当方は route_long_name に番号は付けてない想定
        our_name = normalize_name(our_by_key[key].get("route_long_name", ""))
        if off_name == our_name:
            name_matches += 1
            name_match_details.append((key, off_name, "一致"))
        else:
            name_match_details.append((key, f"公式: {off_name}", f"当方: {our_name}"))

    return {
        "official_count": len(off),
        "our_count": len(ours),
        "matched_count": len(common),
        "match_rate": round(len(common) / max(len(off), 1) * 100, 1),
        "long_name_match_count": name_matches,
        "long_name_match_rate": round(name_matches / max(len(common), 1) * 100, 1),
        "only_in_official": sorted(only_official),
        "only_in_ours": sorted(only_ours),
        "matched_keys": sorted(common),
        "name_match_details": name_match_details,
    }


def compare_stops(off_dir: Path, our_dir: Path) -> dict:
    off = read_csv(off_dir / "stops.txt")
    ours = read_csv(our_dir / "stops.txt")

    # 正規化された名前で比較
    off_names = set(normalize_name(s.get("stop_name", "")) for s in off)
    off_names.discard("")
    our_names = set(normalize_name(s.get("stop_name", "")) for s in ours)
    our_names.discard("")

    common = off_names & our_names
    only_official = off_names - our_names
    only_ours = our_names - off_names

    off_with_coords = sum(1 for s in off if s.get("stop_lat") and s.get("stop_lon"))
    our_with_coords = sum(1 for s in ours if s.get("stop_lat") and s.get("stop_lon"))

    return {
        "official_count": len(off),
        "our_count": len(ours),
        "official_unique_names": len(off_names),
        "our_unique_names": len(our_names),
        "matched_count": len(common),
        "match_rate_vs_official": round(len(common) / max(len(off_names), 1) * 100, 1),
        "match_rate_vs_ours": round(len(common) / max(len(our_names), 1) * 100, 1),
        "official_with_coords": off_with_coords,
        "our_with_coords": our_with_coords,
        "only_in_official": sorted(only_official),
        "only_in_ours": sorted(only_ours),
    }


def compare_trips(off_dir: Path, our_dir: Path) -> dict:
    off_trips = read_csv(off_dir / "trips.txt")
    our_trips = read_csv(our_dir / "trips.txt")
    off_routes = read_csv(off_dir / "routes.txt")
    our_routes = read_csv(our_dir / "routes.txt")

    # route_id → matchkey のマップ
    off_route_to_key = {r["route_id"]: get_route_matchkey(r) for r in off_routes}
    our_route_to_key = {r["route_id"]: get_route_matchkey(r) for r in our_routes}

    off_count_by_key = defaultdict(int)
    for t in off_trips:
        key = off_route_to_key.get(t.get("route_id", ""), "")
        if key:
            off_count_by_key[key] += 1

    our_count_by_key = defaultdict(int)
    for t in our_trips:
        key = our_route_to_key.get(t.get("route_id", ""), "")
        if key:
            our_count_by_key[key] += 1

    per_route = []
    common_keys = set(off_count_by_key) & set(our_count_by_key)
    for k in sorted(common_keys, key=lambda x: (len(x), x)):
        off_n = off_count_by_key[k]
        our_n = our_count_by_key[k]
        per_route.append({
            "route_key": k,
            "official_trips": off_n,
            "our_trips": our_n,
            "match_rate": round(min(off_n, our_n) / max(max(off_n, our_n), 1) * 100, 1),
        })

    return {
        "official_count": len(off_trips),
        "our_count": len(our_trips),
        "official_count_by_route": dict(off_count_by_key),
        "our_count_by_route": dict(our_count_by_key),
        "per_route": per_route,
    }


def compare_stop_times(off_dir: Path, our_dir: Path) -> dict:
    """(正規化stop_name, arrival_time) ペアで比較。"""
    off_st = read_csv(off_dir / "stop_times.txt")
    our_st = read_csv(our_dir / "stop_times.txt")
    off_stops = read_csv(off_dir / "stops.txt")
    our_stops = read_csv(our_dir / "stops.txt")

    # stop_id → 正規化 stop_name
    off_id_to_name = {s["stop_id"]: normalize_name(s.get("stop_name", "")) for s in off_stops}
    our_id_to_name = {s["stop_id"]: normalize_name(s.get("stop_name", "")) for s in our_stops}

    off_pairs = set()
    for st in off_st:
        name = off_id_to_name.get(st.get("stop_id", ""), "")
        time = (st.get("arrival_time") or "").strip()
        if name and time:
            off_pairs.add((name, time))

    our_pairs = set()
    for st in our_st:
        name = our_id_to_name.get(st.get("stop_id", ""), "")
        time = (st.get("arrival_time") or "").strip()
        if name and time:
            our_pairs.add((name, time))

    common = off_pairs & our_pairs

    return {
        "official_count": len(off_st),
        "our_count": len(our_st),
        "official_unique_pairs": len(off_pairs),
        "our_unique_pairs": len(our_pairs),
        "matched_pairs": len(common),
        "match_rate_vs_official": round(len(common) / max(len(off_pairs), 1) * 100, 1),
        "match_rate_vs_ours": round(len(common) / max(len(our_pairs), 1) * 100, 1),
    }


def compare_calendar(off_dir: Path, our_dir: Path) -> dict:
    off = read_csv(off_dir / "calendar.txt")
    ours = read_csv(our_dir / "calendar.txt")

    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    off_patterns = {}
    for c in off:
        pat = "".join(str(c.get(d, "0")) for d in days)
        off_patterns[pat] = c.get("service_id", "")

    our_patterns = {}
    for c in ours:
        pat = "".join(str(c.get(d, "0")) for d in days)
        our_patterns[pat] = c.get("service_id", "")

    common_patterns = set(off_patterns) & set(our_patterns)

    return {
        "official_count": len(off),
        "our_count": len(ours),
        "matched_patterns": len(common_patterns),
        "official_patterns": list(off_patterns.keys()),
        "our_patterns": list(our_patterns.keys()),
        "official_service_ids": list(off_patterns.values()),
        "our_service_ids": list(our_patterns.values()),
    }


# ----------------------------------------------------------------------
# レポート生成
# ----------------------------------------------------------------------

def generate_markdown_report(results: dict) -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = []
    md.append("# GTFS-JP 比較レポート (eval_compare v2)")
    md.append("")
    md.append(f"**生成日時**: {today}")
    md.append(f"**公式データ**: {results['official']}")
    md.append(f"**自分の出力**: {results['ours']}")
    md.append("")
    md.append("---")

    md.append("## サマリ：総合一致率")
    md.append("")
    md.append("| テーブル | 公式件数 | 当方件数 | 一致 | 一致率（公式比） |")
    md.append("|---|---:|---:|---:|---:|")
    r = results["routes"]
    md.append(f"| routes | {r['official_count']} | {r['our_count']} | {r['matched_count']} | **{r['match_rate']}%** |")
    s = results["stops"]
    md.append(f"| stops (名前ベース) | {s['official_unique_names']} | {s['our_unique_names']} | {s['matched_count']} | **{s['match_rate_vs_official']}%** |")
    t = results["trips"]
    md.append(f"| trips | {t['official_count']} | {t['our_count']} | （路線別、下記参照） | — |")
    st = results["stop_times"]
    md.append(f"| stop_times | {st['official_count']} | {st['our_count']} | {st['matched_pairs']} | **{st['match_rate_vs_official']}%** |")
    c = results["calendar"]
    md.append(f"| calendar | {c['official_count']} | {c['our_count']} | {c['matched_patterns']}パターン | — |")
    md.append("")
    md.append("---")

    # routes 詳細
    md.append("## routes.txt 詳細")
    md.append("")
    md.append(f"- **路線一致**: 公式 {r['official_count']} 件、当方 {r['our_count']} 件、一致 **{r['matched_count']}** 件 (**{r['match_rate']}%**)")
    md.append(f"- **路線名（番号除く）一致**: マッチ路線中 {r['long_name_match_count']}/{r['matched_count']} 件 (**{r['long_name_match_rate']}%**)")
    if r["matched_keys"]:
        md.append(f"- **マッチした路線番号**: {', '.join(r['matched_keys'])}")
    if r["only_in_official"]:
        md.append(f"- **公式にのみ存在**: {', '.join(r['only_in_official'])}")
    if r["only_in_ours"]:
        md.append(f"- **当方にのみ存在**: {', '.join(r['only_in_ours'])}")
    if r.get("name_match_details"):
        md.append("")
        md.append("### 路線名の比較詳細")
        md.append("| 路線番号 | 状態 | 詳細 |")
        md.append("|---|---|---|")
        for key, off_name, status in r["name_match_details"]:
            md.append(f"| {key} | {status if status == '一致' else '差分あり'} | {off_name if status == '一致' else off_name + ' / ' + status} |")
    md.append("")

    # stops 詳細
    md.append("## stops.txt 詳細（停留所名・正規化後）")
    md.append("")
    md.append(f"- **停留所数**: 公式 {s['official_count']} 件 (ユニーク名 {s['official_unique_names']})、当方 {s['our_count']} 件 (ユニーク名 {s['our_unique_names']})")
    md.append(f"- **名前ベース一致**: **{s['matched_count']}** 件 (公式比 **{s['match_rate_vs_official']}%**, 当方比 **{s['match_rate_vs_ours']}%**)")
    md.append(f"- **緯度経度ありの停留所**: 公式 {s['official_with_coords']} 件、当方 {s['our_with_coords']} 件")
    if s["only_in_official"]:
        sample = list(s['only_in_official'])[:10]
        md.append(f"- **公式にのみ存在（先頭10件）**: ")
        for stop in sample:
            md.append(f"  - {stop}")
        if len(s['only_in_official']) > 10:
            md.append(f"  - ...他 {len(s['only_in_official']) - 10} 件")
    if s["only_in_ours"]:
        sample = list(s['only_in_ours'])[:10]
        md.append(f"- **当方にのみ存在（先頭10件）**: ")
        for stop in sample:
            md.append(f"  - {stop}")
        if len(s['only_in_ours']) > 10:
            md.append(f"  - ...他 {len(s['only_in_ours']) - 10} 件")
    md.append("")

    # trips 詳細
    md.append("## trips.txt 詳細（路線別便数）")
    md.append("")
    md.append("| 路線番号 | 公式便数 | 当方便数 | 一致率 |")
    md.append("|---|---:|---:|---:|")
    for pr in t["per_route"]:
        md.append(f"| {pr['route_key']} | {pr['official_trips']} | {pr['our_trips']} | {pr['match_rate']}% |")
    if not t["per_route"]:
        md.append("| (マッチした路線なし) | — | — | — |")
    md.append("")
    md.append(f"- 公式 trips 路線別件数: {dict(t['official_count_by_route'])}")
    md.append(f"- 当方 trips 路線別件数: {dict(t['our_count_by_route'])}")
    md.append("")

    # stop_times 詳細
    md.append("## stop_times.txt 詳細")
    md.append("")
    md.append(f"- **公式の (停留所名, 時刻) ユニーク組合せ**: {st['official_unique_pairs']} 件")
    md.append(f"- **当方の (停留所名, 時刻) ユニーク組合せ**: {st['our_unique_pairs']} 件")
    md.append(f"- **一致した組合せ**: {st['matched_pairs']} 件")
    md.append(f"- **公式に対する一致率**: **{st['match_rate_vs_official']}%**")
    md.append(f"- **当方に対する一致率**: **{st['match_rate_vs_ours']}%**")
    md.append("")

    # calendar 詳細
    md.append("## calendar.txt 詳細")
    md.append("")
    md.append(f"- **公式 service 数**: {c['official_count']} ({len(c['official_patterns'])} 種類の曜日パターン)")
    md.append(f"- **当方 service 数**: {c['our_count']} ({len(c['our_patterns'])} 種類の曜日パターン)")
    md.append(f"- **一致した曜日パターン数**: {c['matched_patterns']}")
    md.append(f"- 公式 service_id: {', '.join(c['official_service_ids'])}")
    md.append(f"- 当方 service_id: {', '.join(c['our_service_ids'])}")
    md.append("")

    md.append("---")
    md.append("")
    md.append("## 注意事項")
    md.append("")
    md.append("- **マッチング戦略 (v2.1)**:")
    md.append("  - routes: route_short_name → 先頭数字抽出 → どちらも無ければ route_long_name 全体")
    md.append("  - stops/stop_times: 名前を NFKC + 全角/半角空白正規化してから比較")
    md.append("- **stop_times の一致率**: (停留所名, 到着時刻) ペアの集合比較。便（trip）の対応付けはしていない")
    md.append("  - ダイヤ改正後の時刻表を旧フィードと比べる場合、一致率が低いのは正常（時刻が変わったため）")
    md.append("- **緯度経度**: stops.merged.txt 等で補完済みなら our_with_coords に反映される")
    md.append("- **shapes.txt**: 比較対象外")
    md.append("")
    md.append("v2.1 改善: 番号なし路線（JR古賀線・小竹線等）を route_long_name 全体でマッチ")

    return "\n".join(md)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="GTFS-JP 公式データと本Skill出力の比較レポート (EVAL-01, 02 / v2)",
    )
    parser.add_argument("--official", required=True,
                        help="公式GTFS の zip または展開済みディレクトリ")
    parser.add_argument("--ours", required=True,
                        help="本Skill が出力した CSV ディレクトリ")
    parser.add_argument("-o", "--output", default="eval_report.md",
                        help="出力レポートファイル (Markdown)")
    parser.add_argument("--json", default=None, help="JSON結果の出力先 (省略可)")
    args = parser.parse_args()

    official_p = Path(args.official)
    tmp_holder = None
    if official_p.is_file() and official_p.suffix.lower() == ".zip":
        tmp_holder = TemporaryDirectory()
        with zipfile.ZipFile(official_p) as zf:
            zf.extractall(tmp_holder.name)
        official_dir = Path(tmp_holder.name)
        print(f"[INFO] 公式GTFS zip を展開: {official_dir}", file=sys.stderr)
    elif official_p.is_dir():
        official_dir = official_p
    else:
        sys.exit(f"Error: --official が無効: {args.official}")

    our_dir = Path(args.ours)
    if not our_dir.is_dir():
        sys.exit(f"Error: --ours ディレクトリがありません: {our_dir}")

    print(f"[INFO] 公式: {official_dir}", file=sys.stderr)
    print(f"[INFO] 当方: {our_dir}", file=sys.stderr)
    print(f"[INFO] 比較開始 (v2)...", file=sys.stderr)

    results = {
        "official": str(official_p),
        "ours": str(our_dir),
        "routes": compare_routes(official_dir, our_dir),
        "stops": compare_stops(official_dir, our_dir),
        "trips": compare_trips(official_dir, our_dir),
        "stop_times": compare_stop_times(official_dir, our_dir),
        "calendar": compare_calendar(official_dir, our_dir),
    }

    md = generate_markdown_report(results)
    Path(args.output).write_text(md, encoding="utf-8")
    print(f"[OK] レポート出力: {args.output}", file=sys.stderr)

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        print(f"[OK] JSON出力: {args.json}", file=sys.stderr)

    print(f"\n=== サマリ ===", file=sys.stderr)
    print(f"  routes:     {results['routes']['matched_count']}/{results['routes']['official_count']} = {results['routes']['match_rate']}%", file=sys.stderr)
    print(f"  stops:      {results['stops']['matched_count']}/{results['stops']['official_unique_names']} = {results['stops']['match_rate_vs_official']}%", file=sys.stderr)
    print(f"  stop_times: {results['stop_times']['matched_pairs']}/{results['stop_times']['official_unique_pairs']} = {results['stop_times']['match_rate_vs_official']}%", file=sys.stderr)


if __name__ == "__main__":
    main()
