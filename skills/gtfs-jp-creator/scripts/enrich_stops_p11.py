"""
enrich_stops_p11.py
====================

Step 3.5b (P11 ベース緯度経度補完): 国土数値情報 P11 バス停留所データから、
停留所名マッチで stops.txt の緯度経度を補完する。

設計の根拠:
    国土数値情報P11統合設計書_v1.md を参照。

P11 データの入手:
    https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P11.html
    最新版（P11-2024 等）の対象都道府県 zip を DL → 展開 → .shp を指定。

依存ライブラリ:
    pyshp (pip install pyshp)
        Pure Python・軽量・C拡張なし

マッチング戦略（4段階・優先順）:
    1. 完全一致（正規化後）
    2. 前方一致 / 後方一致
    3. 部分一致 (substring)
    4. Fuzzy match (difflib.SequenceMatcher, 閾値 0.80 既定)

Usage:
    python enrich_stops_p11.py <stops.txt> --p11 <P11_xxxx.shp>
        [-o <output.txt>]
        [--bbox lon_min,lat_min,lon_max,lat_max]
        [--fuzzy-threshold 0.80]
        [--report <p11_report.json>]
        [--overwrite]

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import shapefile  # pyshp
    _PYSHP_AVAILABLE = True
except ImportError:
    _PYSHP_AVAILABLE = False


DEFAULT_FUZZY_THRESHOLD = 0.80
DEFAULT_MAX_FUZZY_CANDIDATES = 5
# 同名の P11 候補が複数あるとき、最大ペア距離がこの値(m)を超えたら「別地点の疑い」
# として要確認警告を出す。P11 は上り/下りを別レコードで持つため近接重複は正常
# （多くは 0m）。既定 100m は方向ペアを誤検出せず、別地点（実測で 182m・18km の
# 例あり）を捕捉できる値。
DEFAULT_AMBIGUITY_THRESHOLD_M = 100.0


# ---------------------------------------------------------------------------
# 名前正規化
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """停留所名を比較用に正規化する（NFKC + 全角/半角空白統一）。"""
    if name is None:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    s = s.replace("　", " ")
    s = " ".join(s.split())
    return s


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def read_stops_csv(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def write_stops_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def has_coords(row: dict) -> bool:
    lat = (row.get("stop_lat") or "").strip()
    lon = (row.get("stop_lon") or "").strip()
    return bool(lat) and bool(lon)


# ---------------------------------------------------------------------------
# P11 Shapefile 読み込み
# ---------------------------------------------------------------------------

# P11 バージョン別フィールド名候補（順に試す）
#   v3.0（令和4年度 / 2022年）: P11_001 = バス停名 (最新)
#   v2.0（平成22年度 / 2010年）: P11_002 = バス停名 (旧)
#   その他の英語名・日本語名フィールドも一応サポート
P11_NAME_FIELDS = [
    "P11_001",       # v3.0 最新
    "P11_002",       # v2.0 旧
    "BUSSTOPNAM",
    "BS_NM",
    "name",
    "停留所名",
    "バス停名",
]


def load_p11_stops(shapefile_path: Path,
                    bbox: tuple[float, float, float, float] | None = None
                    ) -> list[dict]:
    """P11 Shapefile を読み、[{name, lat, lon, raw_fields}, ...] を返す。

    Args:
        shapefile_path: .shp ファイルのパス（.dbf 等も同名で同じディレクトリに）
        bbox:           (lon_min, lat_min, lon_max, lat_max) / 範囲外は除外

    Returns:
        list of dict with keys: name (str), lat (float), lon (float), fields (dict)
    """
    if not _PYSHP_AVAILABLE:
        raise RuntimeError(
            "pyshp が必要です。`pip install pyshp` でインストールしてください。"
        )

    reader = shapefile.Reader(str(shapefile_path), encoding="cp932")  # 日本語 Shapefile は cp932 が多い
    field_names = [f[0] for f in reader.fields[1:]]  # 先頭は DeletionFlag なのでスキップ

    # 名前フィールドを特定
    name_field = None
    for cand in P11_NAME_FIELDS:
        if cand in field_names:
            name_field = cand
            break
    if name_field is None:
        # 最後の手段：フィールド名に "name" や "002" を含むものを探す
        for f in field_names:
            if "name" in f.lower() or "002" in f.lower():
                name_field = f
                break
    if name_field is None:
        raise ValueError(
            f"P11 名前フィールドが見つかりません。利用可能フィールド: {field_names}"
        )

    print(f"  P11 名前フィールド: {name_field}")
    print(f"  P11 全フィールド: {field_names[:8]}{'...' if len(field_names) > 8 else ''}")

    stops: list[dict] = []
    n_records = 0
    n_in_bbox = 0

    for sr in reader.shapeRecords():
        n_records += 1
        shape = sr.shape
        rec = sr.record
        if not shape.points:
            continue
        # Point shapefile は points[0] が (lon, lat)
        lon, lat = shape.points[0]

        if bbox:
            lon_min, lat_min, lon_max, lat_max = bbox
            if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
                continue
        n_in_bbox += 1

        # record はリスト or namedtuple のような形なので index 経由でアクセス
        name_idx = field_names.index(name_field)
        name = str(rec[name_idx])
        if not name or name == "None":
            continue

        # フィールド全体を dict として保存（デバッグ用）
        fields_dict = {fn: rec[i] for i, fn in enumerate(field_names)}

        stops.append({
            "name": name,
            "lat": float(lat),
            "lon": float(lon),
            "fields": fields_dict,
        })

    print(f"  P11 読み込み: 全{n_records}件中、bbox内 {n_in_bbox}件、有効名 {len(stops)}件")
    return stops


# ---------------------------------------------------------------------------
# マッチング
# ---------------------------------------------------------------------------

def build_match_index(p11_stops: list[dict]) -> dict:
    """マッチング高速化のための索引を構築。

    Returns:
        {
            "by_exact": {normalized_name: [p11_stops...]},
            "all_normalized": [normalized_names...]  # fuzzy 用
        }
    """
    by_exact: dict[str, list[dict]] = {}
    all_norm: list[str] = []
    for s in p11_stops:
        norm = normalize_name(s["name"])
        if not norm:
            continue
        by_exact.setdefault(norm, []).append(s)
        all_norm.append(norm)
    return {"by_exact": by_exact, "all_normalized": list(set(all_norm))}


# 日本語の停留所名で「ここが落ちると別停留所」になりやすい方位接頭辞。
# 例: 「上江洲公民館前」と「江洲公民館前」は別停留所（数km離れる）。
#     「東山」と「山」も別停留所。
DIRECTIONAL_PREFIXES = ("上", "中", "下", "東", "西", "南", "北", "新", "旧")


def _directional_prefix(name: str) -> str:
    """名前の先頭文字が方位接頭辞ならそれを返す。なければ空文字。"""
    if name and name[0] in DIRECTIONAL_PREFIXES:
        return name[0]
    return ""


def directional_compatible(target: str, candidate: str) -> bool:
    """方位接頭辞（上/中/下/東/西/南/北/新/旧）の付き方を見て、
    target と candidate が「実質的に同じ停留所」と扱えるかを判定する。

    片方にだけ接頭辞があり、接頭辞を取り除いた残りが他方と一致／部分一致する
    場合（例: 「上江洲公民館前」と「江洲公民館前」）は False を返し、別停留所
    として棄却対象とする。異なる接頭辞同士（「上の原」と「東の原」など）で
    残りが揃う場合も False。それ以外は True（既存のロジックに判定を委ねる）。
    """
    if not target or not candidate:
        return True
    tp = _directional_prefix(target)
    cp = _directional_prefix(candidate)
    if tp == cp:
        return True  # 接頭辞が一致（双方無しを含む）
    # 残り（接頭辞を除いた部分）が 1 文字だと substring 誤検出が起きるため、
    # 部分一致系の判定は残り 2 文字以上に限定する（完全一致は 1 文字でも有効）。
    if tp and not cp:
        tr = target[1:]
        if not tr:
            return True
        if tr == candidate:
            return False
        if len(tr) >= 2 and (
            candidate.startswith(tr) or candidate.endswith(tr)
            or tr.startswith(candidate) or tr.endswith(candidate)
        ):
            return False
    elif cp and not tp:
        cr = candidate[1:]
        if not cr:
            return True
        if cr == target:
            return False
        if len(cr) >= 2 and (
            target.startswith(cr) or target.endswith(cr)
            or cr.startswith(target) or cr.endswith(target)
        ):
            return False
    else:
        # 双方に方位接頭辞があり、かつ異なる文字
        tr, cr = target[1:], candidate[1:]
        if tr and cr:
            if tr == cr:
                return False
            if len(tr) >= 2 and len(cr) >= 2 and (tr in cr or cr in tr):
                return False
    return True


def match_exact(target: str, index: dict) -> list[dict]:
    return index["by_exact"].get(target, [])


def match_prefix_suffix(target: str, index: dict) -> list[dict]:
    """前方一致または後方一致する P11 停留所を返す。"""
    results: list[dict] = []
    for norm, stops in index["by_exact"].items():
        if norm == target:
            continue
        if norm.startswith(target) or target.startswith(norm):
            results.extend(stops)
        elif norm.endswith(target) or target.endswith(norm):
            results.extend(stops)
    return results


def match_substring(target: str, index: dict) -> list[dict]:
    """部分一致する P11 停留所を返す（target が P11 名に含まれる、または逆）。"""
    results: list[dict] = []
    for norm, stops in index["by_exact"].items():
        if norm == target:
            continue
        if target in norm or norm in target:
            results.extend(stops)
    return results


def match_fuzzy(target: str, index: dict, threshold: float,
                 max_candidates: int) -> list[tuple[dict, float]]:
    """difflib による fuzzy match。閾値以上の候補を類似度付きで返す。"""
    norms = index["all_normalized"]
    # get_close_matches は類似度上位を返すが、閾値・件数指定可能
    close = difflib.get_close_matches(target, norms, n=max_candidates, cutoff=threshold)
    results: list[tuple[dict, float]] = []
    for cand_norm in close:
        ratio = difflib.SequenceMatcher(None, target, cand_norm).ratio()
        for stop in index["by_exact"].get(cand_norm, []):
            results.append((stop, ratio))
    return results


def find_best_match(target_name: str, index: dict, fuzzy_threshold: float,
                     max_fuzzy: int) -> tuple[dict | None, str, float]:
    """1停留所に対して最良の P11 マッチを返す。

    Returns:
        (matched_stop, strategy, similarity)
        matched_stop は None なら未マッチ
        strategy: "exact" | "prefix_suffix" | "substring" | "fuzzy" | "none"
        similarity: 0.0〜1.0
    """
    target = normalize_name(target_name)
    if not target:
        return None, "none", 0.0

    # 方位接頭辞（上/中/下/東/西/南/北/新/旧）の不整合を弾くフィルタ。
    # exact は対象外（接頭辞も一致しているはずなので無条件採用）。
    def filt_dir(cands):
        return [c for c in cands
                if directional_compatible(target, normalize_name(c["name"]))]

    # 1. 完全一致
    cands = match_exact(target, index)
    if cands:
        return cands[0], "exact", 1.0

    # 2. 前方/後方一致（fuzzy_threshold 以上の類似度がある場合のみ採用）
    cands = filt_dir(match_prefix_suffix(target, index))
    if cands:
        cands_with_score = [
            (c, difflib.SequenceMatcher(None, target, normalize_name(c["name"])).ratio())
            for c in cands
        ]
        cands_with_score.sort(key=lambda x: x[1], reverse=True)
        best, score = cands_with_score[0]
        if score >= fuzzy_threshold:
            return best, "prefix_suffix", score
        # 類似度不足なら fall-through（substring / fuzzy へ）

    # 3. 部分一致（fuzzy_threshold 以上の類似度がある場合のみ採用）
    cands = filt_dir(match_substring(target, index))
    if cands:
        cands_with_score = [
            (c, difflib.SequenceMatcher(None, target, normalize_name(c["name"])).ratio())
            for c in cands
        ]
        cands_with_score.sort(key=lambda x: x[1], reverse=True)
        best, score = cands_with_score[0]
        if score >= fuzzy_threshold:
            return best, "substring", score
        # 類似度不足なら fall-through

    # 4. fuzzy（最終手段）
    fuzzy_cands = match_fuzzy(target, index, fuzzy_threshold, max_fuzzy)
    fuzzy_cands = [(c, s) for (c, s) in fuzzy_cands
                   if directional_compatible(target, normalize_name(c["name"]))]
    if fuzzy_cands:
        fuzzy_cands.sort(key=lambda x: x[1], reverse=True)
        best, score = fuzzy_cands[0]
        return best, "fuzzy", score

    return None, "none", 0.0


# ---------------------------------------------------------------------------
# 同名あいまい判定（座標選択は変えず、別地点の疑いを警告するためだけに使う）
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点の緯度経度から直線距離(m)を返す。"""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def compute_match_ambiguity(match: dict, index: dict, threshold_m: float
                            ) -> dict | None:
    """選ばれた P11 名に同名レコードが複数あり、かつ候補間の最大距離が閾値を
    超えるとき「別地点の疑い（要確認）」として情報を返す。閾値以内（方向ペア等の
    近接重複）や同名が1件なら None。

    座標の採否は変更しない（呼び出し側は従来どおり先頭を採用する）。本関数は
    「黙って1つに決めてよいか」を後判定し、危ういときだけ人に確認を促すためのもの。
    """
    same = index["by_exact"].get(normalize_name(match["name"]), [])
    if len(same) < 2:
        return None
    max_d = 0.0
    for i in range(len(same)):
        for j in range(i + 1, len(same)):
            d = _haversine_m(same[i]["lat"], same[i]["lon"],
                             same[j]["lat"], same[j]["lon"])
            if d > max_d:
                max_d = d
    if max_d <= threshold_m:
        return None
    return {
        "p11_name": match["name"],
        "candidate_count": len(same),
        "max_pair_m": round(max_d),
        "chosen": {"lat": match["lat"], "lon": match["lon"]},
        "candidates": [{"lat": round(s["lat"], 6), "lon": round(s["lon"], 6)}
                       for s in same],
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="国土数値情報 P11 から停留所名マッチで緯度経度を補完する"
    )
    parser.add_argument("input", help="座標を埋めたい stops.txt")
    parser.add_argument("--p11", required=True,
                        help="P11 Shapefile (.shp) のパス")
    parser.add_argument("-o", "--output", default=None,
                        help="出力 stops.txt（既定: <input>.p11.txt）")
    parser.add_argument("--bbox", default=None,
                        help="P11 を絞る範囲 (lon_min,lat_min,lon_max,lat_max)")
    parser.add_argument("--fuzzy-threshold", type=float, default=DEFAULT_FUZZY_THRESHOLD,
                        help=f"fuzzy match の最低類似度（既定 {DEFAULT_FUZZY_THRESHOLD}）")
    parser.add_argument("--max-fuzzy-candidates", type=int, default=DEFAULT_MAX_FUZZY_CANDIDATES,
                        help=f"fuzzy match で考慮する候補数（既定 {DEFAULT_MAX_FUZZY_CANDIDATES}）")
    parser.add_argument("--ambiguity-threshold", type=float, default=DEFAULT_AMBIGUITY_THRESHOLD_M,
                        help=f"同名候補が複数あるとき、最大ペア距離がこの m を超えたら要確認"
                             f"警告を出す（既定 {DEFAULT_AMBIGUITY_THRESHOLD_M}m。座標選択は変えない）")
    parser.add_argument("--report", default="p11_enrichment_report.json",
                        help="レポート出力先（既定: ./p11_enrichment_report.json）")
    parser.add_argument("--overwrite", action="store_true",
                        help="既に座標がある stop も上書きする")
    args = parser.parse_args()

    if not _PYSHP_AVAILABLE:
        print("Error: pyshp が必要です。`pip install pyshp` でインストールしてください。",
              file=sys.stderr)
        return 1

    in_path = Path(args.input)
    p11_path = Path(args.p11)
    if not in_path.exists():
        print(f"Error: input not found: {in_path}", file=sys.stderr)
        return 1
    if not p11_path.exists():
        print(f"Error: P11 shapefile not found: {p11_path}", file=sys.stderr)
        return 1

    out_path = Path(args.output) if args.output else in_path.with_suffix(".p11.txt")
    report_path = Path(args.report)

    # bbox パース
    bbox = None
    if args.bbox:
        try:
            parts = [float(x.strip()) for x in args.bbox.split(",")]
            if len(parts) != 4:
                raise ValueError("4つの値が必要")
            bbox = tuple(parts)
        except ValueError as e:
            print(f"Error: --bbox parse failed: {e}", file=sys.stderr)
            return 1

    print(f"Input:           {in_path}")
    print(f"P11 Shapefile:   {p11_path}")
    print(f"Output:          {out_path}")
    print(f"BBox:            {bbox if bbox else '(none / 全国)'}")
    print(f"Fuzzy threshold: {args.fuzzy_threshold}")
    print()

    # --- 読み込み ---
    print("[1/3] stops.txt 読み込み...")
    rows, fieldnames = read_stops_csv(in_path)
    if "stop_lat" not in fieldnames:
        fieldnames.append("stop_lat")
    if "stop_lon" not in fieldnames:
        fieldnames.append("stop_lon")
    print(f"  {len(rows)} stops loaded")

    print("[2/3] P11 Shapefile 読み込み...")
    p11_stops = load_p11_stops(p11_path, bbox=bbox)

    print("[3/3] マッチング...")
    index = build_match_index(p11_stops)
    print(f"  P11 ユニーク正規化名: {len(index['all_normalized'])}")
    print()

    # --- マッチング ---
    counters = {"exact": 0, "prefix_suffix": 0, "substring": 0, "fuzzy": 0, "none": 0}
    matched_details = []
    unmatched_details = []
    ambiguous_details = []
    skipped_already = 0

    for row in rows:
        name = row.get("stop_name", "")
        if has_coords(row) and not args.overwrite:
            skipped_already += 1
            print(f"  - {name}: skip (既に座標あり)")
            continue

        match, strategy, score = find_best_match(
            name, index, args.fuzzy_threshold, args.max_fuzzy_candidates
        )
        counters[strategy] += 1

        if match is not None:
            row["stop_lat"] = f"{match['lat']:.6f}"
            row["stop_lon"] = f"{match['lon']:.6f}"
            # 同名候補が別地点に散らばっていないか後判定（座標は変えず、警告のみ）
            ambiguity = compute_match_ambiguity(match, index, args.ambiguity_threshold)
            detail = {
                "stop_id": row.get("stop_id"),
                "stop_name": name,
                "strategy": strategy,
                "similarity": round(score, 3),
                "p11_name": match["name"],
                "lat": match["lat"],
                "lon": match["lon"],
            }
            if ambiguity is not None:
                detail["ambiguous"] = ambiguity
                ambiguous_details.append({
                    "stop_id": row.get("stop_id"),
                    "stop_name": name,
                    **ambiguity,
                })
            matched_details.append(detail)
            print(f"  [一致] {name}: [{strategy} sim={score:.2f}] "
                  f"→ {match['name']} ({match['lat']:.5f}, {match['lon']:.5f})")
            if ambiguity is not None:
                print(f"      [要確認] 同名のP11候補が {ambiguity['candidate_count']}件、"
                      f"最大 {ambiguity['max_pair_m']}m 離れています（先頭を採用）。"
                      f"別地点の疑い。原典で位置を確認してください。")
        else:
            unmatched_details.append({
                "stop_id": row.get("stop_id"),
                "stop_name": name,
            })
            print(f"  [未] {name}: 未マッチ")

    # --- 書き出し ---
    write_stops_csv(out_path, rows, fieldnames)

    # --- レポート ---
    total = len(rows)
    enriched = counters["exact"] + counters["prefix_suffix"] + counters["substring"] + counters["fuzzy"]
    coverage = (enriched + skipped_already) / max(total, 1) * 100
    report = {
        "summary": {
            "total_stops": total,
            "already_had_coords": skipped_already,
            "newly_enriched": enriched,
            "by_strategy": counters,
            "unmatched": counters["none"],
            "ambiguous": len(ambiguous_details),
            "coverage_pct": round(coverage, 1),
        },
        "matched": matched_details,
        "unmatched": unmatched_details,
        "ambiguous_matches": ambiguous_details,
        "p11_file": str(p11_path),
        "bbox": list(bbox) if bbox else None,
        "fuzzy_threshold": args.fuzzy_threshold,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # --- サマリ ---
    print()
    print("=" * 64)
    print("P11 ENRICHMENT REPORT")
    print("=" * 64)
    print(f"Total stops:                    {total}")
    print(f"Already had coords:             {skipped_already}")
    print(f"Newly enriched (exact):         {counters['exact']}")
    print(f"Newly enriched (prefix/suffix): {counters['prefix_suffix']}")
    print(f"Newly enriched (substring):     {counters['substring']}")
    print(f"Newly enriched (fuzzy):         {counters['fuzzy']}")
    print(f"Unmatched:                      {counters['none']}")
    print(f"Ambiguous (要確認・同名別地点):  {len(ambiguous_details)}")
    print(f"Coverage:                       {report['summary']['coverage_pct']}%")
    if ambiguous_details:
        print()
        print("[要確認] 同名のP11候補が別地点に散らばっています（先頭を採用済み・原典で確認を）:")
        for a in ambiguous_details[:10]:
            print(f"  {a['stop_id']}  {a['stop_name']}  "
                  f"（候補{a['candidate_count']}件・最大{a['max_pair_m']}m）")
        if len(ambiguous_details) > 10:
            print(f"  ... and {len(ambiguous_details) - 10} more (see report)")
    if unmatched_details:
        print()
        print("Unmatched stops (Nominatim へフォールバック対象):")
        for u in unmatched_details[:10]:
            print(f"  {u['stop_id']}  {u['stop_name']}")
        if len(unmatched_details) > 10:
            print(f"  ... and {len(unmatched_details) - 10} more (see report)")
    print("=" * 64)
    print(f"Output written:  {out_path}")
    print(f"Report saved:    {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
