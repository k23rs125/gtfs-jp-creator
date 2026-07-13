"""
run_pipeline.py
===============

ワンコマンド GTFS-JP 生成パイプライン。

Step 2（Markdown → JSON、LLM 利用）は手動で行う必要があるが、
それ以降の Step 3〜7 を config ファイル1枚で全自動実行する。

実行する Step:
    条件確認   要入力サマリの表示          condition_summary.py    （情報提示・常時）
    Step 3    JSON → CSV               generate_gtfs_files.py
    Step 3.5a 旧フィードから座標再利用     merge_stop_coords.py    （reference_feed / official_feed_url 指定時）
    Step 3.5b 国土数値情報 P11 で補完      enrich_stops_p11.py     （p11_shapefile 指定時）
    Step 3.5b2 同名複数候補の経路位置選択   select_ambiguous_by_route.py （P11使用時・既定ON）
    Step 3.5c Nominatim で補完           enrich_stops.py         （use_nominatim=true 時）
    Step 3.x  停留所名 canonicalize       canonicalize_stops.py   （canonical_reference 指定時）
    Step 3.5d 手動座標オーバーライド       apply_manual_coords.py  （manual_coords 指定時・shapes前）
    Step 3.5f 座標の信頼度分類(確定/要確認)    classify_coord_confidence.py （coord_confidence≠false で常時）
    Step 3.6  祝日・運休日を calendar_dates へ展開 generate_calendar_dates.py （holiday_* 指定時）
    Step 4    shapes.txt 生成            generate_shapes.py
    Step 4b   検証用マップHTML生成          make_map_view.py        （map_view≠false で常時）
    Step 4c   GTFSビューア(単一HTML)生成    make_gtfs_viewer.py     （gtfs_viewer≠false で常時）
    Step 6    translations.txt 生成      generate_translations.py
    Step 6b   手動読みオーバーライド       apply_manual_readings.py（manual_readings 指定時）
    Step 5    zip パッケージング          package_gtfs_zip.py
    Step 7    GTFS Validator 検証        validate_gtfs.py        （validate=true 時）
    Step 7b   GTFS-JP 拡張検証           validate_gtfs_jp_extensions.py （常時・Java不要）
    Step 7c   内部整合検証(時刻照合)        verify_stop_times_vs_extract.py （extract_json 指定時）
    Step 7d   時刻アノマリ検出(OCR誤読)      detect_time_anomalies.py        （extract_json 指定時）

各 Step は前提となるオプションが config に無ければ graceful skip する。
条件確認は情報提示のみで、要入力があってもパイプラインは止めない。

Usage:
    python run_pipeline.py --config <pipeline_config.json>
    python run_pipeline.py --config <config.json> --dry-run   # 実行計画だけ表示

config フォーマット（JSON）:
    {
      "feed_name": "kogabus",
      "input_json": "test_demo/kogashi_claude.json",
      "extract_json": "test_demo/kogashi_extract.json",
      "output_dir": "test_demo/kogabus_pipeline",
      "context": "福岡県",
      "bbox": "130.42,33.67,130.52,33.76",
      "reference_feed": "260211kogabus_gtfs-jp.zip",
      "official_feed_url": "https://data.bodik.jp/.../download/xxx-gtfs-jp.zip",
      "p11_shapefile": "p11_fukuoka/P11-22_40_SHP/P11-22_40.shp",
      "canonical_reference": "Shin_kogashi.zip",
      "use_nominatim": false,
      "translations_en_json": "test_demo/kogashi_en.json",
      "holiday_nenmatsu": "12-29:01-03",
      "holiday_syukujitsu": "syukujitsu.csv",
      "timetable_review_dir": "review/",
      "decision_json": "test_demo/kogashi_decision.json",
      "validate": true
    }

    必須: feed_name, input_json, output_dir
    任意: それ以外（無ければ該当 Step をスキップ）

    時刻修正: timetable_review_dir に export_timetable_review.py が出した修正済みCSVの
    フォルダを指定すると、構造化前に extract の時刻へ反映する。stop_times に効かせるには
    decision_json（decision-spec）も指定して修正後の抽出から再構造化する（無指定時は警告）。

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import csv
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
        log(f"  [OK] {label} 完了 ({dt:.1f}秒)")
        return True
    else:
        log(f"  [NG] {label} 失敗 (exit {proc.returncode}, {dt:.1f}秒)", "ERROR")
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
    extract_json = cfg.get("extract_json")          # 抽出JSON(blocks/cells)。Step7c内部整合検証に使用
    manual_coords = cfg.get("manual_coords")        # 手動座標JSON (Step4 shapes 前に適用)
    manual_readings = cfg.get("manual_readings")    # 手動読みJSON (Step6 後に適用)
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

    # ---- 時刻修正CSV(export_timetable_review)があれば構造化前に反映 ----
    # 修正は extract 段階で反映しないと stop_times に届かない。decision_json が
    # あれば修正後の抽出から再構造化して input_json(stop_times) にも反映する。
    timetable_review_dir = cfg.get("timetable_review_dir")
    decision_json = cfg.get("decision_json")
    if timetable_review_dir and not args.dry_run:
        rv = Path(timetable_review_dir)
        if not rv.exists():
            log(f"timetable_review_dir が見つかりません: {rv}", "WARN")
        elif not extract_json:
            log("extract_json 未指定のため時刻修正を適用できません", "WARN")
        else:
            from apply_timetable_review import apply_reviews
            ex = json.loads(Path(extract_json).read_text(encoding="utf-8"))
            ch, warn = apply_reviews(ex, str(rv))
            fixed = work_dir / "extract_fixed.json"
            fixed.write_text(json.dumps(ex, ensure_ascii=False, indent=2), encoding="utf-8")
            extract_json = str(fixed)   # 7c/7d 照合も修正後で行う
            log(f"時刻修正を反映: {len(ch)} セル → {fixed}")
            for w in warn:
                log(f"  [時刻修正・警告] {w}", "WARN")
            if decision_json:
                new_in = work_dir / "structured_fixed.json"
                rc = subprocess.run(
                    [PYTHON, str(SCRIPT_DIR.parents[2] / "apply_decisions.py"),
                     "--extract", str(fixed), "--decisions", str(decision_json),
                     "--out", str(new_in)], check=False)
                if rc.returncode == 0:
                    input_json = new_in
                    log(f"修正後の抽出から再構造化 → {new_in}")
                else:
                    log("再構造化に失敗。input_json は元のまま", "WARN")
            else:
                log("decision_json 未指定のため stop_times は input_json のまま。"
                    "時刻を stop_times に反映するには decision_json を指定するか "
                    "apply_decisions --timetable-review で構造化してください。", "WARN")

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

    # ---- 公式GTFSの自動再利用: 能動確認で見つけた公式feedをDLして reference_feed にする ----
    # 座標のみ再利用し、ダイヤ（時刻・便）は手元の入力を優先する（公式は古い版のことがある）。
    # reference_feed が明示指定されていればそちらを優先（official_feed_url は補助）。
    official_feed_url = cfg.get("official_feed_url")
    if official_feed_url and not reference_feed:
        if args.dry_run:
            log(f"(dry-run) 公式feed自動DL予定: {official_feed_url}")
        else:
            try:
                here = str(SCRIPT_DIR.resolve())
                if here not in sys.path:
                    sys.path.insert(0, here)
                from download_official_feed import get_official_feed
                reference_feed = str(get_official_feed(official_feed_url, work_dir))
                log(f"公式feedを reference_feed に設定（座標再利用・ダイヤは手元優先）: {reference_feed}")
                log("  ※公式データはCC-BY等。座標を使う場合は feed_info 等に出典明記すること。")
            except Exception as e:  # noqa: BLE001
                log(f"公式feedの自動取得に失敗: {e}", "ERROR")
                log("  → official_feed_url を確認するか、手動で reference_feed を指定してください。")

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
               "--report", str(work_dir / "p11_report.json"),
               # 同名複数候補（別地点の疑い）は黙って先頭採用せず、feed と一緒に
               # 要確認リストを置いて利用者/手動確認に回す（あいまい0件なら生成されない）。
               "--review-csv", str(output_dir / "座標_要確認.csv")]
        if bbox:
            cmd += ["--bbox", bbox]
        # context（例: 福岡県久留米市）から市域bboxを取得し、県内同名別自治体への
        # 誤マッチを防ぐ（市区町村名を含む context のときのみ）。
        if context and any(context.endswith(s) or s in context for s in ("市", "町", "村", "区")):
            cmd += ["--municipality", context]
        ok = run_step("Step 3.5b: 国土数値情報 P11 で補完 (enrich_stops_p11)",
                      cmd, args.dry_run)
        record("Step 3.5b P11補完", ok)
        if ok:
            stops_current = out
    else:
        record("Step 3.5b P11補完", True, skipped=True)
        log("Step 3.5b: p11_shapefile 未指定のためスキップ")

    # ---- Step 3.5b2: 同名複数候補の経路位置選択（P11使用時・既定ON） ----
    # P11で同名候補が市域bbox内に複数あり要確認となった停留所を、便の経路上のあるべき位置
    # （前後の確定停留所からの内挿推定）に最も近い候補へ自動選択する。黙って先頭採用でも
    # 推定座標でもなく、実在するP11候補から経路に最も合うものを選ぶ（要確認の一歩先）。
    if p11_shapefile and cfg.get("select_ambiguous_by_route", True):
        out = work_dir / "stops_3.5b2.txt"
        ok = run_step(
            "Step 3.5b2: 同名複数候補の経路位置選択 (select_ambiguous_by_route)",
            [PYTHON, script("select_ambiguous_by_route.py"), str(stops_current),
             "--stop-times", str(gtfs_dir / "stop_times.txt"),
             "--p11-report", str(work_dir / "p11_report.json"),
             "-o", str(out), "--report", str(work_dir / "ambiguous_select_report.json")],
            args.dry_run)
        record("Step 3.5b2 同名経路選択", ok)
        if ok:
            stops_current = out
    else:
        record("Step 3.5b2 同名経路選択", True, skipped=True)
        log("Step 3.5b2: P11未使用 または select_ambiguous_by_route=false のためスキップ")

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

    # ---- Step 3.5d: 手動座標オーバーライド (shapes 生成前に適用) ----
    # P11/Nominatim で当たらない停留所を手動座標で確定する。shapes(Step4)より「前」に
    # 適用するのが要点で、こうすると OSRM 経路がその停留所を通り、後付けで起きる
    # stop_too_far_from_shape を防げる。手動が最優先(apply_manual_coords の思想)。
    if manual_coords:
        out = work_dir / "stops_3.5d.txt"
        ok = run_step(
            "Step 3.5d: 手動座標オーバーライド (apply_manual_coords)",
            [PYTHON, script("apply_manual_coords.py"), str(stops_current),
             "--coords", manual_coords, "-o", str(out)],
            args.dry_run,
        )
        record("Step 3.5d 手動座標", ok)
        if ok:
            stops_current = out
    else:
        record("Step 3.5d 手動座標", True, skipped=True)
        log("Step 3.5d: manual_coords 未指定のためスキップ")

    # ---- Step 3.5d2: 経路ジオメトリ外れ値の棄却（同名誤マッチ検出・既定ON） ----
    # 座標が付いていても経路から大きく外れる停留所（南北に長い自治体のbbox内での同名別地点
    # への誤マッチ等）を棄却し、後段の内挿/手動に回す。公式データと比較しないと見えない誤りを
    # 自動検出する（例: 築城巡回線の八津田が約10km、京築恵みの郷が約8km離れた同名にヒット）。
    if cfg.get("reject_geom_outliers", True):
        out = work_dir / "stops_3.5d2.txt"
        ok = run_step("Step 3.5d2: 経路ジオメトリ外れ値の棄却 (reject_geom_outliers)",
                      [PYTHON, script("reject_geom_outliers.py"), str(stops_current),
                       "--stop-times", str(gtfs_dir / "stop_times.txt"), "-o", str(out),
                       "--report", str(work_dir / "geom_outlier_report.json")],
                      args.dry_run)
        record("Step 3.5d2 経路外れ値棄却", ok)
        if ok:
            stops_current = out
    else:
        record("Step 3.5d2 経路外れ値棄却", True, skipped=True)
        log("Step 3.5d2: reject_geom_outliers=false のためスキップ")

    # ---- Step 3.5e: 経路内挿による「推定座標（要確認）」補完（既定OFF・opt-in） ----
    # 旧feed/P11/Nominatim/手動でも埋まらない停留所を、便の停留所順で前後の既知座標から
    # 内挿する。内挿値は推定（誤差中央値約146m）なのでレポートで要確認として明示し、
    # 市域外に出た内挿は外れ値として採用しない（座標補完評価.tex 参照）。
    # 「正しく失敗」の原則上、既定はOFF。config で "interpolate_coords": true にすると有効。
    if cfg.get("interpolate_coords"):
        out = work_dir / "stops_3.5e.txt"
        cmd = [PYTHON, script("interpolate_coords.py"), str(stops_current),
               "--stop-times", str(gtfs_dir / "stop_times.txt"), "-o", str(out),
               "--report", str(work_dir / "interpolate_report.json")]
        if context and any(s in context for s in ("市", "町", "村", "区")):
            cmd += ["--municipality", context]
        ok = run_step("Step 3.5e: 経路内挿で推定座標を補完 (interpolate_coords)",
                      cmd, args.dry_run)
        record("Step 3.5e 経路内挿(推定)", ok)
        if ok:
            stops_current = out
    else:
        record("Step 3.5e 経路内挿(推定)", True, skipped=True)
        log("Step 3.5e: interpolate_coords 未指定のためスキップ（既定OFF）")

    # ---- 最終 stops.txt を gtfs_dir に反映 ----
    if not args.dry_run and stops_current != (gtfs_dir / "stops.txt"):
        shutil.copy(stops_current, gtfs_dir / "stops.txt")
        log(f"最終 stops.txt を反映: {stops_current} → {gtfs_dir / 'stops.txt'}")

    # ---- Step 3.5e2: 行き/帰りの同座標を反対側へ推定オフセット（方向分割時・要確認） ----
    # 多くの停留所は反対車線にあり方向で座標が異なるが、P11は上り/下りを別座標で持たない。
    # 経路(停留所の並び)から進行方向の左（日本は左側通行）へ寄せ、行き/帰りを反対側に分ける。
    # ★推定なので必ず要確認（classifyで要確認に落とし、提出前チェックでブロック）。手動確定は除外。
    off_report = work_dir / "direction_offset_report.json"
    if cfg.get("direction_offset", True):
        cmd = [PYTHON, script("offset_direction_coords.py"), str(gtfs_dir),
               "--report", str(off_report)]
        if manual_coords:
            cmd += ["--manual", manual_coords]
        ok = run_step("Step 3.5e2: 行き/帰りを反対側へ推定オフセット (offset_direction_coords)",
                      cmd, args.dry_run)
        record("Step 3.5e2 方向オフセット(推定)", ok)
    else:
        record("Step 3.5e2 方向オフセット(推定)", True, skipped=True)
        log("Step 3.5e2: direction_offset=false のためスキップ")

    # ---- Step 3.5f: 座標の信頼度分類（確定/要確認/未補完。既定ON） ----
    # 各停留所の最終座標を補完源と経路整合から分類し、output_dir/座標_信頼度.csv に出力する。
    # 「推測座標(内挿/Nominatim/あいまい一致)を確定として黙って出さない」ための層で、官公庁
    # 提出のように誤りが許されない用途で、要確認の座標を人手確認に回す根拠となる。
    if cfg.get("coord_confidence", True):
        cmd = [PYTHON, script("classify_coord_confidence.py"), str(gtfs_dir / "stops.txt"),
               "--stop-times", str(gtfs_dir / "stop_times.txt"),
               "--reports-dir", str(work_dir),
               "-o", str(output_dir / "座標_信頼度.csv"),
               "--report", str(work_dir / "coord_confidence_report.json")]
        if manual_coords:
            cmd += ["--manual", manual_coords]
        if cfg.get("direction_offset", True) and off_report.exists():
            cmd += ["--estimated", str(off_report)]   # 推定オフセットは要確認に落とす
        ok = run_step("Step 3.5f: 座標の信頼度分類 (classify_coord_confidence)", cmd, args.dry_run)
        record("Step 3.5f 座標信頼度", ok)
    else:
        record("Step 3.5f 座標信頼度", True, skipped=True)
        log("Step 3.5f: coord_confidence=false のためスキップ")

    # ---- Step 3.6: 祝日・運休日を calendar_dates に展開（設定時のみ） ----
    # PDF外の運行日メタ（祝日運休・年末年始・お盆）を、推測せず公式データ/利用者指定で
    # 決定的に展開する。祝日は内閣府CSV(syukujitsu)を一次データに、年末年始/お盆は範囲指定時のみ。
    # いずれも未指定ならスキップ（＝運休日を付けない＝要確認・正しく失敗）。
    holiday_syukujitsu = cfg.get("holiday_syukujitsu")   # 内閣府祝日CSVのパス（祝日運休のとき）
    holiday_nenmatsu = cfg.get("holiday_nenmatsu")        # 例 "12-29:01-03"
    holiday_obon = cfg.get("holiday_obon")                # 例 "08-13:08-15"
    if holiday_syukujitsu or holiday_nenmatsu or holiday_obon:
        cal_path = gtfs_dir / "calendar.txt"
        if args.dry_run:
            service_ids = "SVC"
        else:
            with cal_path.open(encoding="utf-8-sig") as f:
                service_ids = ",".join(sorted(
                    {r["service_id"] for r in csv.DictReader(f) if r.get("service_id")}))
        cmd = [PYTHON, script("generate_calendar_dates.py"),
               "--calendar", str(cal_path), "--service-id", service_ids,
               "-o", str(gtfs_dir / "calendar_dates.txt")]
        if holiday_syukujitsu:
            cmd += ["--syukujitsu", holiday_syukujitsu]
        if holiday_nenmatsu:
            cmd += ["--nenmatsu", holiday_nenmatsu]
        if holiday_obon:
            cmd += ["--obon", holiday_obon]
        ok = run_step("Step 3.6: 祝日・運休日を展開 (generate_calendar_dates)", cmd, args.dry_run)
        record("Step 3.6 運休日展開", ok)
    else:
        record("Step 3.6 運休日展開", True, skipped=True)
        log("Step 3.6: holiday_* 未指定のためスキップ（運休日を付けない＝要確認）")

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

    # ---- Step 4b: 検証用マップHTML生成（既定ON） ----
    # stops/shapes/trips/stop_times から1枚完結の Leaflet マップ(map_view.html)を生成する。
    # 座標の妥当性（日本範囲外・想定bbox外・未補完）を色分けし、便を選ぶと停車順に強調する。
    # 外部送信なし・インストール不要の検証物。output_dir に置く（検証物はfeedと同じ場所に）。
    if cfg.get("map_view", True):
        cmd = [PYTHON, script("make_map_view.py"), str(gtfs_dir / "stops.txt"),
               "--out", str(output_dir / "map_view.html"), "--title", feed_name]
        if bbox:
            cmd += ["--bbox", bbox]
        if args.dry_run or (gtfs_dir / "shapes.txt").exists():
            cmd += ["--shapes", str(gtfs_dir / "shapes.txt")]
            if args.dry_run or trips_with_shapes.exists():
                cmd += ["--trips", str(trips_with_shapes)]
        if args.dry_run or (gtfs_dir / "stop_times.txt").exists():
            cmd += ["--stop-times", str(gtfs_dir / "stop_times.txt")]
        ok = run_step("Step 4b: 検証用マップ生成 (make_map_view)", cmd, args.dry_run)
        record("Step 4b 検証用マップ", ok)
    else:
        record("Step 4b 検証用マップ", True, skipped=True)
        log("Step 4b: map_view=false のためスキップ")

    # ---- Step 6: generate_translations ----
    cmd = [PYTHON, script("generate_translations.py"),
           "--stops", str(gtfs_dir / "stops.txt"),
           "--routes", str(gtfs_dir / "routes.txt"),
           "--agency", str(gtfs_dir / "agency.txt"),   # 事業者名の読み
           "--trips", str(gtfs_dir / "trips.txt"),     # 行き先表示の読み
           "-o", str(gtfs_dir / "translations.txt"),
           "--report", str(work_dir / "translations_report.json")]
    if translations_en_json:
        cmd += ["--merge-en", translations_en_json]
    else:
        cmd += ["--export-en-prompt", str(work_dir / "translations_en_prompt.txt")]
    ok = run_step("Step 6: translations.txt 生成 (generate_translations)",
                  cmd, args.dry_run)
    record("Step 6 translations生成", ok)

    # ---- Step 6b: 手動読みオーバーライド (難読地名のふりがな/英訳を上書き) ----
    # pykakasi の誤読等を手動読みで上書きする。translations 生成(Step6)の「後」に適用。
    if manual_readings:
        ok = run_step(
            "Step 6b: 手動読みオーバーライド (apply_manual_readings)",
            [PYTHON, script("apply_manual_readings.py"),
             str(gtfs_dir / "translations.txt"), "--readings", manual_readings],
            args.dry_run,
        )
        record("Step 6b 手動読み", ok)
    else:
        record("Step 6b 手動読み", True, skipped=True)
        log("Step 6b: manual_readings 未指定のためスキップ")

    # ---- Step 4c: GTFSビューア(単一HTML)生成（既定ON） ----
    # 作成した feed の各 .txt を埋め込んだ standalone HTML を生成する。7タブ（路線一覧/時刻表/
    # 運賃表/路線図/運行カレンダー/バス停一覧/データチェック結果）をブラウザで自動表示。
    # 外部送信なし・file:// で開けて見られる検証物。translations 確定後に作るため output_dir に置く。
    if cfg.get("gtfs_viewer", True):
        cmd = [PYTHON, script("make_gtfs_viewer.py"), "--feed", str(gtfs_dir),
               "-o", str(output_dir / "gtfs_viewer.html")]
        if cfg.get("viewer_title"):
            cmd += ["--title", str(cfg["viewer_title"])]
        ok = run_step("Step 4c: GTFSビューア生成 (make_gtfs_viewer)", cmd, args.dry_run)
        record("Step 4c GTFSビューア", ok)
    else:
        record("Step 4c GTFSビューア", True, skipped=True)
        log("Step 4c: gtfs_viewer=false のためスキップ")

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
        [PYTHON, script("validate_gtfs_jp_extensions.py"), str(gtfs_dir),
         "-o", str(output_dir / "jp_ext_report.json")],
        args.dry_run,
    )
    record("Step 7b JP拡張検証", ok)

    # ---- Step 7c: 内部整合検証（抽出JSON <-> stop_times の時刻照合） ----
    # 座標方式(Step1)の抽出JSONと生成 stop_times の時刻が便ごとに一致するかを照合し、
    # Step2(LLM構造化)・Step3(生成)での時刻の改変・欠落を検出する。公式feed不要・版差非依存。
    # extract_json（blocks/cells形式の抽出JSON）が config にあるときのみ実行。--strict で
    # 不一致を FAIL として拾う。レポートは output_dir に揃える（検証物はfeedと同じ場所に）。
    if extract_json:
        ok = run_step(
            "Step 7c: 内部整合検証 (verify_stop_times_vs_extract)",
            [PYTHON, script("verify_stop_times_vs_extract.py"), extract_json,
             "--gtfs", str(gtfs_dir),
             "-o", str(output_dir / "stoptimes_verify.md"),
             "--json", str(output_dir / "stoptimes_verify.json"),
             "--strict"],
            args.dry_run,
        )
        record("Step 7c 内部整合検証", ok)
    else:
        record("Step 7c 内部整合検証", True, skipped=True)
        log("Step 7c: extract_json 未指定のためスキップ")

    # ---- Step 7d: 時刻アノマリ検出（OCR誤読の疑い） ----
    # 便内の時刻逆行・便間パターンからの外れを検出し、修正候補つきで output_dir に出す。
    # 主に画像PDF→OCR(MinerU)の数字読み違い対策。検証物として置く（自動修正はしない）。
    if extract_json:
        ok = run_step(
            "Step 7d: 時刻アノマリ検出 (detect_time_anomalies)",
            [PYTHON, script("detect_time_anomalies.py"), extract_json,
             "-o", str(output_dir / "時刻アノマリ.json")],
            args.dry_run,
        )
        record("Step 7d 時刻アノマリ検出", ok)
    else:
        record("Step 7d 時刻アノマリ検出", True, skipped=True)
        log("Step 7d: extract_json 未指定のためスキップ")

    # ---- Step 7e: 速度チェック（区間速度から座標/時刻の誤りを炙り出す） ----
    # 停留所間の直線距離と時刻差から速度を出し、非現実的な速すぎ/時間0を検出する。
    # 「確定」でも別地点に付いた座標は速度が飛ぶため、名称照合をすり抜けた誤りを捕捉できる。
    ok = run_step(
        "Step 7e: 速度チェック (check_speed)",
        [PYTHON, script("check_speed.py"),
         "--stops", str(gtfs_dir / "stops.txt"),
         "--stop-times", str(gtfs_dir / "stop_times.txt"),
         "-o", str(output_dir / "速度_check.csv")],
        args.dry_run,
    )
    record("Step 7e 速度チェック", ok)

    # ---- Step 7f: 経路(shape)カバレッジ（shapeが各停留所を通っているか） ----
    if (gtfs_dir / "shapes.txt").exists():
        ok = run_step(
            "Step 7f: 経路カバレッジ (check_shape_coverage)",
            [PYTHON, script("check_shape_coverage.py"), str(gtfs_dir),
             "--out", str(output_dir / "shape_coverage.csv")],
            args.dry_run,
        )
        record("Step 7f 経路カバレッジ", ok)
    else:
        record("Step 7f 経路カバレッジ", True, skipped=True)
        log("Step 7f: shapes.txt が無いためスキップ")

    # ---- 最終サマリ ----
    print(file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    log("パイプライン完了")
    print("=" * 64, file=sys.stderr)
    for step, status in results:
        mark = {"OK": "[OK]", "SKIP": "・", "FAIL": "[NG]"}.get(status, "?")
        print(f"  {mark} {step:<28} [{status}]", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    if not args.dry_run:
        log(f"成果物 zip: {output_zip}")
        # 同名複数候補の要確認リストがあれば最終サマリで明示（黙って先頭採用しているため）。
        review_csv = output_dir / "座標_要確認.csv"
        if review_csv.exists():
            try:
                n_review = max(0, sum(1 for _ in review_csv.open(encoding="utf-8-sig")) - 1)
            except OSError:
                n_review = 0
            log(f"[要確認] 同名で複数候補がある停留所 {n_review}件: {review_csv}")
            log("  → 黙って先頭候補を採用済み。利用者に位置を確認（どちらの○○か）してください。")
        # 時刻アノマリ（OCR誤読の疑い）があれば最終サマリで明示。
        anom_json = output_dir / "時刻アノマリ.json"
        if anom_json.exists():
            try:
                anom = json.loads(anom_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                anom = []
            if anom:
                log(f"[要確認] 時刻アノマリ（OCR誤読の疑い） {len(anom)}件: {anom_json}")
                log("  → 原典と照合して時刻を確認・修正してください（自動修正はしていません）。")

    n_fail = sum(1 for _, s in results if s == "FAIL")
    return 0 if n_fail == 0 else 1


def _finish(results, code):
    for step, status in results:
        print(f"  {step}: {status}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
