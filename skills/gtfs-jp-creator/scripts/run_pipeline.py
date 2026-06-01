"""
run_pipeline.py
===============

ワンコマンド GTFS-JP 生成パイプライン。

Step 2（Markdown → JSON、LLM 利用）は手動で行う必要があるが、
それ以降の Step 3〜7 を config ファイル1枚で全自動実行する。

実行する Step:
    条件確認   要入力サマリの表示          condition_summary.py    （情報提示・常時）
    Step 3    JSON → CSV               generate_gtfs_files.py
    Step 3.5a 旧フィードから座標再利用     merge_stop_coords.py    （reference_feed 指定時）
    Step 3.5b 国土数値情報 P11 で補完      enrich_stops_p11.py     （p11_shapefile 指定時）
    Step 3.5c Nominatim で補完           enrich_stops.py         （use_nominatim=true 時）
    Step 3.x  停留所名 canonicalize       canonicalize_stops.py   （canonical_reference 指定時）
    Step 4    shapes.txt 生成            generate_shapes.py
    Step 6    translations.txt 生成      generate_translations.py
    Step 5    zip パッケージング          package_gtfs_zip.py
    Step 7    GTFS Validator 検証        validate_gtfs.py        （validate=true 時）
    Step 7b   GTFS-JP 拡張検証           validate_gtfs_jp_extensions.py （常時・Java不要）

各 Step は前提となるオプションが config に無ければ graceful skip する。
条件確認は情報提示のみで、要入力があってもパイプラインは止めない。

Usage:
    python run_pipeline.py --config <pipeline_config.json>
    python run_pipeline.py --config <config.json> --dry-run   # 実行計画だけ表示

config フォーマット（JSON）:
    {
      "feed_name": "kogabus",
      "input_json": "test_demo/kogashi_claude.json",
      "output_dir": "test_demo/kogabus_pipeline",
      "context": "福岡県",
      "bbox": "130.42,33.67,130.52,33.76",
      "reference_feed": "260211kogabus_gtfs-jp.zip",
      "p11_shapefile": "p11_fukuoka/P11-22_40_SHP/P11-22_40.shp",
      "canonical_reference": "Shin_kogashi.zip",
      "use_nominatim": false,
      "translations_en_json": "test_demo/kogashi_en.json",
      "validate": true
    }

    必須: feed_name, input_json, output_dir
    任意: それ以外（無ければ該当 Step をスキップ）

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# ヘルパ
# ---------------------------------------------------------------------------

def log(msg: str, level: str = "INFO") -> None:
    print(f"[{level}] {msg}", file=sys.stderr, flush=True)


def run_step(label: str, cmd: list[str], dry_run: bool) -> bool:
    """1 Step を subprocess で実行。成功なら True。"""
    print(file=sys.stderr)
    log(f"━━━ {label} ━━━")
    log(f"  $ {' '.join(str(c) for c in cmd)}")
    if dry_run:
        log("  (dry-run: スキップ)")
        return True
    t0 = time.time()
    proc = subprocess.run(cmd, check=False)
    dt = time.time() - t0
    if proc.returncode == 0:
        log(f"  ✓ {label} 完了 ({dt:.1f}秒)")
        return True
    else:
        log(f"  ✗ {label} 失敗 (exit {proc.returncode}, {dt:.1f}秒)", "ERROR")
        return False


def script(name: str) -> str:
    """skills/.../scripts/ 内のスクリプトのフルパスを返す。"""
    return str(SCRIPT_DIR / name)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="ワンコマンド GTFS-JP 生成パイプライン (Step 3〜7)"
    )
    parser.add_argument("--config", required=True, help="パイプライン config JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="実行せず計画のみ表示")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="Step 失敗時に即中断（既定は続行可能なら続行）")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        log(f"config が見つかりません: {config_path}", "ERROR")
        return 1

    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log(f"config の JSON パース失敗: {e}", "ERROR")
        return 1

    # 必須キーチェック
    for key in ("feed_name", "input_json", "output_dir"):
        if key not in cfg:
            log(f"config に必須キー '{key}' がありません", "ERROR")
            return 1

    feed_name = cfg["feed_name"]
    input_json = Path(cfg["input_json"])
    output_dir = Path(cfg["output_dir"])
    work_dir = output_dir / "work"
    gtfs_dir = output_dir / "gtfs"

    if not input_json.exists() and not args.dry_run:
        log(f"input_json が見つかりません: {input_json}", "ERROR")
        return 1

    # 任意設定
    context = cfg.get("context")
    bbox = cfg.get("bbox")
    reference_feed = cfg.get("reference_feed")
    p11_shapefile = cfg.get("p11_shapefile")
    p11_prefecture = cfg.get("p11_prefecture")   # 例 "沖縄県"。指定時は第3.0版を自動取得
    p11_cache_dir = cfg.get("p11_cache_dir")      # 省略時は "p11_data"
    canonical_reference = cfg.get("canonical_reference")
    use_nominatim = cfg.get("use_nominatim", False)
    translations_en_json = cfg.get("translations_en_json")
    do_validate = cfg.get("validate", False)

    print("=" * 64, file=sys.stderr)
    log(f"GTFS-JP パイプライン: {feed_name}")
    log(f"  入力 JSON:   {input_json}")
    log(f"  出力先:      {output_dir}")
    log(f"  dry-run:     {args.dry_run}")
    print("=" * 64, file=sys.stderr)

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, str]] = []  # (step, status)

    def record(step: str, ok: bool, skipped: bool = False):
        status = "SKIP" if skipped else ("OK" if ok else "FAIL")
        results.append((step, status))
        return ok

    # ---- 条件確認: 要入力サマリ（情報提示。要入力があってもパイプラインは止めない）----
    print(file=sys.stderr)
    log("━━━ 条件確認: 要入力サマリ (condition_summary) ━━━")
    if args.dry_run:
        log("  (dry-run: スキップ)")
        record("条件確認サマリ", True, skipped=True)
    else:
        # condition_summary は要入力ありのとき exit 1 を返すが、これは
        # 失敗ではなく情報。run_pipeline は止めず、参考表示にとどめる。
        subprocess.run(
            [PYTHON, script("condition_summary.py"), str(input_json)],
            check=False,
        )
        record("条件確認サマリ", True)

    # ---- Step 3: JSON → CSV ----
    ok = run_step(
        "Step 3: JSON → CSV (generate_gtfs_files)",
        [PYTHON, script("generate_gtfs_files.py"), str(input_json), "-o", str(gtfs_dir)],
        args.dry_run,
    )
    record("Step 3 JSON→CSV", ok)
    if not ok and args.stop_on_error:
        return _finish(results, 1)

    stops_current = gtfs_dir / "stops.txt"

    # ---- Step 3.5a: merge_stop_coords ----
    if reference_feed:
        out = work_dir / "stops_3.5a.txt"
        ok = run_step(
            "Step 3.5a: 旧フィードから座標再利用 (merge_stop_coords)",
            [PYTHON, script("merge_stop_coords.py"), str(stops_current),
             "--reference", reference_feed, "-o", str(out),
             "--report", str(work_dir / "merge_report.json")],
            args.dry_run,
        )
        record("Step 3.5a 旧フィード座標", ok)
        if ok:
            stops_current = out
    else:
        record("Step 3.5a 旧フィード座標", True, skipped=True)
        log("Step 3.5a: reference_feed 未指定のためスキップ")

    # p11_shapefile 未指定 & p11_prefecture 指定時は download_p11 で第3.0版を自動取得
    if not p11_shapefile and p11_prefecture:
        if args.dry_run:
            log(f"(dry-run) P11 自動取得予定: 都道府県={p11_prefecture} (第3.0版)")
        else:
            try:
                here = str(SCRIPT_DIR.resolve())
                if here not in sys.path:
                    sys.path.insert(0, here)
                from download_p11 import get_p11_shapefile
                cache = p11_cache_dir or "p11_data"
                log(f"P11 自動取得: 都道府県={p11_prefecture} (第3.0版) → {cache}")
                p11_shapefile = get_p11_shapefile(p11_prefecture, out_dir=cache)
                log(f"P11 取得完了: {p11_shapefile}")
            except Exception as e:
                log(f"P11 自動取得に失敗: {e}", "ERROR")
                log("  → p11_shapefile を手動指定するか download_p11.py を直接実行してください。")
                p11_shapefile = None

    # ---- Step 3.5b: enrich_stops_p11 ----
    if p11_shapefile:
        out = work_dir / "stops_3.5b.txt"
        cmd = [PYTHON, script("enrich_stops_p11.py"), str(stops_current),
               "--p11", p11_shapefile, "-o", str(out),
               "--report", str(work_dir / "p11_report.json")]
        if bbox:
            cmd += ["--bbox", bbox]
        ok = run_step("Step 3.5b: 国土数値情報 P11 で補完 (enrich_stops_p11)",
                      cmd, args.dry_run)
        record("Step 3.5b P11補完", ok)
        if ok:
            stops_current = out
    else:
        record("Step 3.5b P11補完", True, skipped=True)
        log("Step 3.5b: p11_shapefile 未指定のためスキップ")

    # ---- Step 3.5c: enrich_stops (Nominatim) ----
    if use_nominatim:
        out = work_dir / "stops_3.5c.txt"
        cmd = [PYTHON, script("enrich_stops.py"), str(stops_current), "-o", str(out),
               "--report", str(work_dir / "nominatim_report.json")]
        if context:
            cmd += ["--context", context]
        if bbox:
            cmd += ["--bbox", bbox]
        ok = run_step("Step 3.5c: Nominatim で補完 (enrich_stops)", cmd, args.dry_run)
        record("Step 3.5c Nominatim補完", ok)
        if ok:
            stops_current = out
    else:
        record("Step 3.5c Nominatim補完", True, skipped=True)
        log("Step 3.5c: use_nominatim=false のためスキップ")

    # ---- Step 3.x: canonicalize_stops ----
    if canonical_reference:
        out = work_dir / "stops_3.x.txt"
        ok = run_step(
            "Step 3.x: 停留所名 canonicalize (canonicalize_stops)",
            [PYTHON, script("canonicalize_stops.py"), str(stops_current),
             "--reference", canonical_reference, "-o", str(out),
             "--report", str(work_dir / "canonicalize_report.json")],
            args.dry_run,
        )
        record("Step 3.x 停留所名正規化", ok)
        if ok:
            stops_current = out
    else:
        record("Step 3.x 停留所名正規化", True, skipped=True)
        log("Step 3.x: canonical_reference 未指定のためスキップ")

    # ---- 最終 stops.txt を gtfs_dir に反映 ----
    if not args.dry_run and stops_current != (gtfs_dir / "stops.txt"):
        shutil.copy(stops_current, gtfs_dir / "stops.txt")
        log(f"最終 stops.txt を反映: {stops_current} → {gtfs_dir / 'stops.txt'}")

    # ---- Step 4: generate_shapes ----
    trips_with_shapes = gtfs_dir / "trips.with_shapes.txt"
    ok = run_step(
        "Step 4: shapes.txt 生成 (generate_shapes)",
        [PYTHON, script("generate_shapes.py"),
         str(gtfs_dir / "stops.txt"), str(gtfs_dir / "stop_times.txt"),
         str(gtfs_dir / "trips.txt"),
         "-o", str(gtfs_dir / "shapes.txt"),
         "--update-trips", str(trips_with_shapes),
         "--cache", str(work_dir / "shapes_cache.json"),
         "--report", str(work_dir / "shapes_report.json")],
        args.dry_run,
    )
    record("Step 4 shapes生成", ok)

    # ---- Step 6: generate_translations ----
    cmd = [PYTHON, script("generate_translations.py"),
           "--stops", str(gtfs_dir / "stops.txt"),
           "--routes", str(gtfs_dir / "routes.txt"),
           "-o", str(gtfs_dir / "translations.txt"),
           "--report", str(work_dir / "translations_report.json")]
    if translations_en_json:
        cmd += ["--merge-en", translations_en_json]
    else:
        cmd += ["--export-en-prompt", str(work_dir / "translations_en_prompt.txt")]
    ok = run_step("Step 6: translations.txt 生成 (generate_translations)",
                  cmd, args.dry_run)
    record("Step 6 translations生成", ok)

    # ---- Step 5: package_gtfs_zip ----
    output_zip = output_dir / f"{feed_name}_gtfs-jp.zip"
    sub = []
    # trips.with_shapes.txt があれば trips.txt として梱包
    if args.dry_run or trips_with_shapes.exists():
        sub = ["--substitute", "trips.with_shapes.txt=trips.txt"]
    ok = run_step(
        "Step 5: zip パッケージング (package_gtfs_zip)",
        [PYTHON, script("package_gtfs_zip.py"), str(gtfs_dir),
         "-o", str(output_zip)] + sub,
        args.dry_run,
    )
    record("Step 5 zipパッケージ", ok)

    # ---- Step 7: validate_gtfs ----
    if do_validate:
        ok = run_step(
            "Step 7: GTFS Validator 検証 (validate_gtfs)",
            [PYTHON, script("validate_gtfs.py"), str(output_zip),
             "-o", str(output_dir / "validation")],
            args.dry_run,
        )
        record("Step 7 Validator検証", ok)
    else:
        record("Step 7 Validator検証", True, skipped=True)
        log("Step 7: validate=false のためスキップ")

    # ---- Step 7b: GTFS-JP 拡張検証 ----
    # MobilityData Validator が見ない agency_jp/office_jp/pattern_jp/routes_jp を
    # 独自に検証する。純 Python・Java 不要のため validate 設定に関わらず常に実行。
    ok = run_step(
        "Step 7b: GTFS-JP 拡張検証 (validate_gtfs_jp_extensions)",
        [PYTHON, script("validate_gtfs_jp_extensions.py"), str(gtfs_dir)],
        args.dry_run,
    )
    record("Step 7b JP拡張検証", ok)

    # ---- 最終サマリ ----
    print(file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    log("パイプライン完了")
    print("=" * 64, file=sys.stderr)
    for step, status in results:
        mark = {"OK": "✓", "SKIP": "・", "FAIL": "✗"}.get(status, "?")
        print(f"  {mark} {step:<28} [{status}]", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    if not args.dry_run:
        log(f"成果物 zip: {output_zip}")

    n_fail = sum(1 for _, s in results if s == "FAIL")
    return 0 if n_fail == 0 else 1


def _finish(results, code):
    for step, status in results:
        print(f"  {step}: {status}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
