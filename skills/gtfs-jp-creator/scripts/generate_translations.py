"""
generate_translations.py
========================

Step 6 (translations.txt 生成): GTFS-JP 拡張ファイル translations.txt を
半自動で生成する。

言語対応の戦略（既定は ja-Hrkt / en の2言語）:
    [ja-Hrkt] 漢字 → ひらがな読み           → pykakasi（pip install pykakasi）
    [en]      日本語 → 英語                 → LLM（Claude/Gemini/ChatGPT）
    [ja]      原文コピー（--include-ja 時のみ）→ 本スクリプトが直接生成

    既定で ja 行を出さないのは、feed_lang=ja のとき停留所名の日本語原本は stops.txt
    が持つため、translations.txt の ja 行は重複になるから（公式 GTFS-JP フィードも
    ja-Hrkt / en の2言語構成）。ja 行も欲しい場合は --include-ja を指定する。

2 段階構成:
    1. 抽出フェーズ: ja-Hrkt を生成、LLM 用プロンプトを export
    2. マージフェーズ: ユーザーが LLM から得た en.json を読み込んで最終CSV

設計の根拠:
    translations生成設計書_v1.md を参照。

Usage:
    # 抽出フェーズ
    python generate_translations.py \\
        --stops stops.txt --routes routes.txt \\
        -o translations.txt \\
        --export-en-prompt translations_en_prompt.txt

    # マージフェーズ
    python generate_translations.py \\
        --stops stops.txt --routes routes.txt \\
        -o translations.txt \\
        --merge-en en.json

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

try:
    import pykakasi  # 漢字 → ひらがな変換（pure Python）
    _PYKAKASI_AVAILABLE = True
except ImportError:
    _PYKAKASI_AVAILABLE = False


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def read_csv(path: Path) -> tuple[list[dict], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


# ---------------------------------------------------------------------------
# ひらがな読み生成 (ja-Hrkt)
# ---------------------------------------------------------------------------

def init_kakasi():
    """pykakasi インスタンスを初期化。pykakasi が無ければ None を返す。"""
    if not _PYKAKASI_AVAILABLE:
        return None
    kks = pykakasi.kakasi()
    return kks


def to_hiragana(text: str, kks) -> str:
    """漢字混じり文字列を ひらがな に変換する。

    半角カナ(ﾌｧﾐﾘｰﾏｰﾄ 等)は pykakasi が濁点・半濁点を誤処理して「ふぁみり゜ま゜」の
    ように文字化けするため、先に NFKC で全角化してから変換する（読みの精度が上がる）。
    """
    if not text or kks is None:
        return ""
    norm = unicodedata.normalize("NFKC", text)
    result = kks.convert(norm)
    return "".join(item.get("hira", "") for item in result)


def load_reading_dict(path: Path) -> dict[str, dict]:
    """難読地名の補正辞書 CSV を読む。列: stop_name, ja-Hrkt, en(任意)。

    pykakasi が苦手な全国共通の難読地名（例: 壱町原=いっちょうばる）だけを載せる。
    停留所名(完全一致)でひらがな/英語を上書きする。無ければ空 dict。
    """
    d: dict[str, dict] = {}
    if not path or not path.exists():
        return d
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                nm = (r.get("stop_name") or "").strip()
                if not nm:
                    continue
                spec = {}
                if (r.get("ja-Hrkt") or "").strip():
                    spec["ja-Hrkt"] = r["ja-Hrkt"].strip()
                if (r.get("en") or "").strip():
                    spec["en"] = r["en"].strip()
                if spec:
                    d[nm] = spec
    except Exception as e:
        print(f"Warning: 読み補正辞書を読めません({path}): {e}", file=sys.stderr)
    return d


# ---------------------------------------------------------------------------
# LLM プロンプト export
# ---------------------------------------------------------------------------

def build_en_prompt(items: list[tuple[str, str]]) -> str:
    """LLM 英訳プロンプトを組み立てる。

    Args:
        items: list of (category, value) ― category は "stop_name" 等
    """
    body = []
    body.append("あなたは GTFS-JP 多言語対応の英訳を行うアシスタントです。")
    body.append("以下の日本語の停留所名・路線名を英訳してください。")
    body.append("")
    body.append("# 英訳の方針")
    body.append("- 施設名・一般名詞は英語に訳す（英訳を主とする）")
    body.append("  例: 「市役所」→ \"City Hall\"、「公民館」→ \"Community Center\"、")
    body.append("      「駅」→ \"Station\"、「小学校」→ \"Elementary School\"")
    body.append("- 読みが必要な固有名詞（地名・人名・施設の愛称）はローマ字（ヘボン式）")
    body.append("  例: 「上江洲」→ \"Uezu\"、「サンエー」→ \"San-A\"")
    body.append("- 位置・方向を表す語は省略せず \"(前置詞句 of) 施設名\" の形で明示する")
    body.append("  「前」→ (in front of) ...、「入口」→ (entrance of) ...、「東口」→ (east entrance of) ...")
    body.append("  例: 「市役所前」→ \"(in front of) City Hall\"")
    body.append("  例: 「上江洲公民館前」→ \"(in front of) Uezu Community Center\"")
    body.append("- 「JR」「駅」などの鉄道用語は英語慣用表現")
    body.append("  例: 「JR古賀駅東口」→ \"(east entrance of) JR Koga Station\"")
    body.append("")
    body.append("# 出力フォーマット")
    body.append("以下のような JSON で返してください（前後の説明文不要）：")
    body.append("")
    body.append("```json")
    body.append("{")
    body.append("  \"市役所前\": \"(in front of) City Hall\",")
    body.append("  \"上江洲公民館前\": \"(in front of) Uezu Community Center\",")
    body.append("  \"JR古賀駅東口\": \"(east entrance of) JR Koga Station\",")
    body.append("  ...")
    body.append("}")
    body.append("```")
    body.append("")
    body.append(f"# 英訳対象（{len(items)} 件）")
    body.append("")
    # 重複排除
    seen = set()
    for cat, val in items:
        if val and val not in seen:
            seen.add(val)
            body.append(f"- {val}")
    body.append("")
    return "\n".join(body)


# ---------------------------------------------------------------------------
# 入力 stops / routes から (table_name, field_name, field_value) のリストを抽出
# ---------------------------------------------------------------------------

def collect_translation_targets(stops_rows: list[dict], routes_rows: list[dict]
                                 ) -> list[tuple[str, str, str]]:
    """翻訳対象のリストを返す。

    Returns: list of (table_name, field_name, field_value)
    """
    targets: list[tuple[str, str, str]] = []
    # stops
    seen_stop_names = set()
    for s in stops_rows:
        name = (s.get("stop_name") or "").strip()
        if name and name not in seen_stop_names:
            seen_stop_names.add(name)
            targets.append(("stops", "stop_name", name))
    # routes
    seen_route_names = set()
    for r in routes_rows:
        name = (r.get("route_long_name") or "").strip()
        if name and name not in seen_route_names:
            seen_route_names.add(name)
            targets.append(("routes", "route_long_name", name))
    return targets


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="GTFS-JP 多言語ファイル translations.txt を半自動生成する"
    )
    parser.add_argument("--stops", required=True, help="入力 stops.txt")
    parser.add_argument("--routes", required=True, help="入力 routes.txt")
    parser.add_argument("-o", "--output", default="translations.txt",
                        help="出力 translations.txt （既定: ./translations.txt）")
    parser.add_argument("--export-en-prompt", default=None,
                        help="LLM 英訳プロンプトを書き出すファイルパス（任意）")
    parser.add_argument("--merge-en", default=None,
                        help="LLM 英訳結果 JSON （{name: translation, ...}）を読み込んでマージ")
    parser.add_argument("--no-hiragana", action="store_true",
                        help="pykakasi 未インストール時の救済（ja-Hrkt を生成しない）")
    parser.add_argument("--reading-dict", default=None,
                        help="難読地名の補正辞書CSV(stop_name,ja-Hrkt,en)。pykakasiより優先。"
                             "未指定なら references/data/stop_readings.csv があれば自動使用")
    parser.add_argument("--include-ja", action="store_true",
                        help="ja（原文コピー）行も出力する（既定は ja-Hrkt / en のみ。"
                             "feed_lang=ja では ja 名は stops.txt が持つため既定では重複を避ける）")
    parser.add_argument("--report", default="translations_report.json",
                        help="レポート出力先")
    args = parser.parse_args()

    stops_path = Path(args.stops)
    routes_path = Path(args.routes)
    out_path = Path(args.output)
    report_path = Path(args.report)

    for p, name in [(stops_path, "stops.txt"), (routes_path, "routes.txt")]:
        if not p.exists():
            print(f"Error: {name} not found: {p}", file=sys.stderr)
            return 1

    print(f"Stops:   {stops_path}")
    print(f"Routes:  {routes_path}")
    print(f"Output:  {out_path}")
    print()

    # --- 読み込み ---
    stops_rows, _ = read_csv(stops_path)
    routes_rows, _ = read_csv(routes_path)
    targets = collect_translation_targets(stops_rows, routes_rows)
    print(f"翻訳対象: {len(targets)} 件 (stops: "
          f"{sum(1 for t in targets if t[0]=='stops')}, "
          f"routes: {sum(1 for t in targets if t[0]=='routes')})")
    print()

    # --- ひらがな読み生成 ---
    kks = None
    hiragana_map: dict[str, str] = {}
    if not args.no_hiragana:
        kks = init_kakasi()
        if kks is None:
            print("Warning: pykakasi がインストールされていません。"
                  "ja-Hrkt をスキップします（`pip install pykakasi` で導入可）。",
                  file=sys.stderr)
        else:
            for _, _, value in targets:
                hiragana_map[value] = to_hiragana(value, kks)

    # --- 難読地名の補正辞書で pykakasi を上書き（全国共通の難読地名のみ） ---
    if args.reading_dict:
        dict_path = Path(args.reading_dict)
    else:
        dict_path = Path(__file__).resolve().parent.parent / "references" / "data" / "stop_readings.csv"
    reading_dict = load_reading_dict(dict_path)
    en_dict: dict[str, str] = {}
    if reading_dict:
        n_over = 0
        for _, _, value in targets:
            spec = reading_dict.get(value)
            if not spec:
                continue
            if spec.get("ja-Hrkt"):
                hiragana_map[value] = spec["ja-Hrkt"]; n_over += 1
            if spec.get("en"):
                en_dict[value] = spec["en"]
        print(f"読み補正辞書: {len(reading_dict)}件中 {n_over}件を停留所名に適用 ({dict_path.name})")

    # --- LLM 英訳結果のマージ ---
    en_map: dict[str, str] = {}
    if args.merge_en:
        en_path = Path(args.merge_en)
        if not en_path.exists():
            print(f"Error: en.json not found: {en_path}", file=sys.stderr)
            return 1
        try:
            en_map = json.loads(en_path.read_text(encoding="utf-8"))
            print(f"英訳マージ: {len(en_map)} 件読み込み from {en_path}")
        except json.JSONDecodeError as e:
            print(f"Error: en.json パース失敗: {e}", file=sys.stderr)
            return 1
    # 補正辞書の英語は、LLMマージに無い分だけ補う（利用者指定を優先）。
    for k, v in en_dict.items():
        en_map.setdefault(k, v)

    # --- translations.txt 行を構築 ---
    fieldnames = ["table_name", "field_name", "language", "translation", "field_value"]
    rows: list[dict] = []
    stats = {"ja": 0, "ja-Hrkt": 0, "en": 0, "en_missing": 0}

    for table_name, field_name, value in targets:
        # ja: 原文そのまま（--include-ja 指定時のみ。既定は出さない）
        if args.include_ja:
            rows.append({
                "table_name": table_name,
                "field_name": field_name,
                "language": "ja",
                "translation": value,
                "field_value": value,
            })
            stats["ja"] += 1

        # ja-Hrkt: pykakasi
        if kks is not None:
            hira = hiragana_map.get(value, "")
            if hira:
                rows.append({
                    "table_name": table_name,
                    "field_name": field_name,
                    "language": "ja-Hrkt",
                    "translation": hira,
                    "field_value": value,
                })
                stats["ja-Hrkt"] += 1

        # en: LLM から
        en = en_map.get(value)
        if en:
            rows.append({
                "table_name": table_name,
                "field_name": field_name,
                "language": "en",
                "translation": en,
                "field_value": value,
            })
            stats["en"] += 1
        else:
            stats["en_missing"] += 1

    write_csv(out_path, rows, fieldnames)

    # --- LLM プロンプトを export ---
    if args.export_en_prompt:
        prompt_path = Path(args.export_en_prompt)
        prompt = build_en_prompt([(t[1], t[2]) for t in targets])
        prompt_path.write_text(prompt, encoding="utf-8")
        print(f"LLM 英訳プロンプトを書き出し: {prompt_path}")

    # --- レポート ---
    report = {
        "summary": {
            "total_targets": len(targets),
            "ja_rows": stats["ja"],
            "ja_hrkt_rows": stats["ja-Hrkt"],
            "en_rows": stats["en"],
            "en_missing": stats["en_missing"],
            "pykakasi_available": _PYKAKASI_AVAILABLE and not args.no_hiragana,
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # --- サマリ ---
    print()
    print("=" * 64)
    print("TRANSLATIONS REPORT")
    print("=" * 64)
    print(f"翻訳対象（ユニーク件数）:  {len(targets)}")
    if args.include_ja:
        print(f"ja 行:                     {stats['ja']}")
    print(f"ja-Hrkt 行:                {stats['ja-Hrkt']}")
    print(f"en 行:                     {stats['en']}")
    print(f"en 未充足:                 {stats['en_missing']}")
    print(f"pykakasi 利用:             {report['summary']['pykakasi_available']}")
    print("=" * 64)
    print(f"Output written:  {out_path}  ({len(rows)} rows)")
    if args.export_en_prompt:
        print(f"En prompt:       {args.export_en_prompt}")
    if stats["en_missing"] > 0:
        print()
        print("[未完了] 英訳 (en) が未充足です。次のステップ:")
        print(f"   1. {args.export_en_prompt or '(export_en_prompt を指定して再実行)'} を Claude/Gemini/ChatGPT にコピペ")
        print(f"   2. LLM から返ってきた JSON を en.json として保存")
        print(f"   3. 本スクリプトを --merge-en en.json で再実行")

    return 0


if __name__ == "__main__":
    sys.exit(main())
