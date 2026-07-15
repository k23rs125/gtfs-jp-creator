#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
golden_test.py — 公式GTFSがある路線での回帰テスト（ゴールデンテスト）
=====================================================================
公式GTFSが存在する路線について、本パイプラインを **公式座標の再利用OFF**（P11＋内挿の
独自手法のみ）で回し、公式GTFSを"正解"として比較する。

判定の核心（本研究の主張）は「精度が完璧か」ではなく **「黙って誤りを出していないか」**：
  ・確定(=自信あり)なのに公式から遠い停留所 = 0 でなければ FAIL（＝サイレント誤り）
  ・確定の的中率 100%（確定は必ず正確）
  ・抽出時刻 ↔ stop_times の内部整合 100%
座標の絶対精度（中央値等）や Validator の ERROR/WARNING は **参考値として記録**する
（難路線では独自手法だけだと座標が大きくズレるが、それらは全て要確認へ回るのが正しい挙動）。

Usage:
    python golden_test.py                 # 全ケース実行、PASS/FALで終了コード
    python golden_test.py --keep          # 生成物を消さない（デバッグ用）
"""
import argparse
import csv
import io
import json
import math
import re
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
SCRIPTS = REPO / "skills" / "gtfs-jp-creator" / "scripts"

# ---- ゴールデンケース定義（公式GTFSがある路線＋しきい値）----
CASES = [
    {
        "name": "柳川両開にし（独自手法=P11+内挿のみ／公式再利用OFF）",
        "input_json": "test_demo/ryobiraki_claude.json",
        "extract_json": "test_demo/ryobiraki_extract.json",
        "context": "福岡県柳川市",
        "p11_shapefile": "C:/Users/User/Desktop/稲ゼミ/p11_fukuoka/P11-22_40_SHP/P11-22_40.shp",
        "official": "test_demo/ryobiraki_pipeline/work/official_feed_b990716ba2.zip",
        "accurate_threshold_m": 100.0,   # これ以内を「正確」とみなす
        "gates": {
            "silent_errors_max": 0,        # 確定なのに不正確 = サイレント誤り（0でなければFAIL）
            "confident_precision_min": 100.0,  # 確定の的中率
            "internal_time_match_min": 100.0,  # 抽出時刻↔stop_times
        },
    },
]


# ---------------------------------------------------------------- utils
def _nm(s):
    s = unicodedata.normalize("NFKC", s or "").replace("　", " ")
    return re.sub(r"\s+", " ", s).strip()


def _hav_m(a, b, c, d):
    R = 6371000.0
    p1, p2 = math.radians(a), math.radians(c)
    return 2 * R * math.asin(min(1.0, math.sqrt(
        math.sin(math.radians(c - a) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2)))


def _read_stops_coords_from_zip(zpath):
    with zipfile.ZipFile(zpath) as zf:
        name = next(n for n in zf.namelist() if n.endswith("stops.txt"))
        rows = list(csv.DictReader(io.TextIOWrapper(zf.open(name), encoding="utf-8-sig")))
    out = {}
    for s in rows:
        try:
            out[_nm(s["stop_name"])] = (float(s["stop_lat"]), float(s["stop_lon"]))
        except (KeyError, TypeError, ValueError):
            pass
    return out


def _run(args, **kw):
    return subprocess.run([sys.executable, "-X", "utf8"] + [str(a) for a in args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


# ---------------------------------------------------------------- one case
def run_case(case, keep=False):
    out_dir = Path(tempfile.mkdtemp(prefix="golden_"))
    cfg = {
        "feed_name": "golden",
        "input_json": case["input_json"],
        "extract_json": case["extract_json"],
        "output_dir": str(out_dir),
        "context": case["context"],
        "p11_shapefile": case["p11_shapefile"],
        "use_nominatim": False,          # 決定的にするためネットワーク補完はOFF
        "interpolate_coords": True,
        "reject_geom_outliers": True,
        "validate": True,
        # ※ official_feed_url / reference_feed は入れない → Step3.5a(公式再利用)スキップ
    }
    cfg_path = out_dir / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    r = _run([SCRIPTS / "run_pipeline.py", "--config", cfg_path], cwd=REPO)
    if r.returncode != 0:
        return {"name": case["name"], "error": "pipeline失敗", "log": (r.stderr or r.stdout)[-800:]}

    gtfs = out_dir / "gtfs"
    thr = case["accurate_threshold_m"]
    official = _read_stops_coords_from_zip(REPO / case["official"])

    # 信頼度CSV（confidence）＋当方座標 → 公式との距離でクロス集計
    conf_rows = list(csv.DictReader((out_dir / "座標_信頼度.csv").open(encoding="utf-8-sig")))
    cross = {"確定": [0, 0], "要確認": [0, 0], "未補完": [0, 0]}   # [正確, 不正確]
    dists, silent = [], []
    for row in conf_rows:
        name = _nm(row.get("stop_name", ""))
        conf = row.get("confidence", "")
        if name not in official:
            continue
        try:
            la, lo = float(row["stop_lat"]), float(row["stop_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        d = _hav_m(official[name][0], official[name][1], la, lo)
        dists.append(d)
        acc = d <= thr
        cross.setdefault(conf, [0, 0])[0 if acc else 1] += 1
        if conf == "確定" and not acc:
            silent.append({"stop": name, "dist_m": round(d)})

    conf_total = sum(cross["確定"])
    confident_precision = round(cross["確定"][0] / conf_total * 100, 1) if conf_total else 100.0
    dists.sort()
    median_m = round(dists[len(dists) // 2], 1) if dists else None
    within100 = round(sum(1 for d in dists if d <= 100) / max(len(dists), 1) * 100, 1)

    # 内部整合（抽出時刻↔stop_times）
    sv = out_dir / "stoptimes_verify.json"
    internal = 0.0
    if sv.exists():
        internal = json.loads(sv.read_text(encoding="utf-8")).get("summary", {}).get("time_match_pct", 0.0)

    # Validator（参考値）
    err = warn = None
    rep = out_dir / "validation" / "report.json"
    if rep.exists():
        notices = json.loads(rep.read_text(encoding="utf-8")).get("notices", [])
        err = sum(1 for n in notices if n.get("severity") == "ERROR")
        warn = sum(1 for n in notices if n.get("severity") == "WARNING")

    metrics = {
        "confidence_counts": {k: sum(v) for k, v in cross.items() if sum(v)},
        "matched_with_coords": len(dists),
        "median_m": median_m,
        "within_100m_pct": within100,
        "silent_errors": len(silent),
        "silent_error_detail": silent,
        "confident_precision": confident_precision,
        "internal_time_match": internal,
        "validator_error": err,
        "validator_warning": warn,
    }
    # PASS/FAIL 判定（核心ゲート）
    g = case["gates"]
    checks = [
        ("黙って誤り(確定なのに不正確)=0", metrics["silent_errors"] <= g["silent_errors_max"],
         f"{metrics['silent_errors']} 件"),
        ("確定の的中率100%", metrics["confident_precision"] >= g["confident_precision_min"],
         f"{metrics['confident_precision']}%"),
        ("内部整合(時刻)100%", metrics["internal_time_match"] >= g["internal_time_match_min"],
         f"{metrics['internal_time_match']}%"),
    ]
    metrics["checks"] = [{"name": n, "pass": p, "value": v} for n, p, v in checks]
    metrics["passed"] = all(p for _, p, _ in checks)
    metrics["name"] = case["name"]
    if not keep:
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)
    else:
        metrics["out_dir"] = str(out_dir)
    return metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="生成物を残す")
    args = ap.parse_args()

    all_pass = True
    print("=" * 70)
    print("ゴールデンテスト（公式GTFS比較・公式再利用OFF＝独自手法の実力を測る）")
    print("=" * 70)
    for case in CASES:
        m = run_case(case, keep=args.keep)
        if m.get("error"):
            print(f"\n[FAIL] {m['name']}: {m['error']}\n{m.get('log', '')}")
            all_pass = False
            continue
        print(f"\n■ {m['name']}")
        print(f"  信頼度内訳: {m['confidence_counts']}")
        print(f"  座標(参考): 中央値 {m['median_m']}m / 100m以内 {m['within_100m_pct']}% "
              f"({m['matched_with_coords']}停留所を公式と照合)")
        print(f"  Validator(参考): ERROR {m['validator_error']} / WARNING {m['validator_warning']}")
        print("  --- 合否ゲート（本研究の主張＝正しく失敗）---")
        for c in m["checks"]:
            print(f"   [{'PASS' if c['pass'] else 'FAIL'}] {c['name']}: {c['value']}")
        if m["silent_error_detail"]:
            print(f"   ⚠ サイレント誤りの詳細: {m['silent_error_detail']}")
        print(f"  => {'PASS ✅' if m['passed'] else 'FAIL ❌'}")
        all_pass = all_pass and m["passed"]

    print("\n" + "=" * 70)
    print("総合:", "ALL PASS ✅" if all_pass else "FAIL あり ❌")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
