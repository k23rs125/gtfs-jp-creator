"""
generate_translations.py
========================

Step 6 (translations.txt 生成): GTFS-JP 拡張ファイル translations.txt を
半自動で生成する。

言語対応の戦略（既定は ja-Hrkt / en の2言語）:
    [ja-Hrkt] 漢字 → ひらがな読み           → SudachiPy（辞書付き形態素解析・Apache-2.0）
                                              ＋停留所名の連濁/地名末尾の決定的な補正規則
                                              ＋難読地名の補正辞書（stop_readings.csv）
    [en]      日本語 → 英語                 → LLM（Claude/Gemini/ChatGPT）
    [ja]      原文コピー（--include-ja 時のみ）→ 本スクリプトが直接生成

    読みエンジンは SudachiPy（Apache-2.0）のみを用いる。以前フォールバックに使っていた
    pykakasi は GPL-3.0-or-later（コピーレフト）で、自治体が自己ホストで配布する形態だと
    ライセンス上の制約が生じうるため採用しない。読みは「決定的・オフライン・無料・
    Apache系のみ」で完結させ、外れる地名は補正辞書＋人手確認（④）で担保する。

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

# 読みエンジンは SudachiPy（Apache-2.0）に一本化。pykakasi は GPL-3.0-or-later のため不使用
# （自治体の自己ホスト配布でコピーレフトの制約が生じうる。ライセンス最優先の方針）。
# SudachiPy（辞書付き形態素解析）は決定的（同じ入力→同じ読み）でオフライン・無料。
try:
    from sudachipy import dictionary as _sudachi_dict, tokenizer as _sudachi_tok
    _SUDACHI_AVAILABLE = True
except ImportError:
    _SUDACHI_AVAILABLE = False

_sudachi = None   # 遅延初期化した Tokenizer（辞書ロードは重いので1回だけ）


def _get_sudachi():
    global _sudachi
    if not _SUDACHI_AVAILABLE:
        return None
    if _sudachi is None:
        try:
            _sudachi = _sudachi_dict.Dictionary(dict="core").create()
        except Exception:
            _sudachi = False   # 辞書が無い等で初期化失敗 → 以後スキップ
    return _sudachi or None


# 停留所名の末尾でよく起きる「連濁・地名の慣用読み」を決定的に補正する規則。
# バス停名はほぼ地名で、末尾「口」は連濁して「ぐち」になるのが通例（東口＝ひがしぐち、
# 鏡山口＝かがみやまぐち）。形態素解析は一般語基準のため「くち」「つう」と読み外しやすい。
# ここに載せるのは【全国共通で安全な語尾規則のみ】。地域固有の難読読み（例: 長木＝おさぎ、
# 小長田＝こながた）は地域で読みが異なりうる（長木は他地域では「ながき」）ため規則化せず、
# 各案件の手動読み（manual_readings.json）や補正辞書での個別指定に委ねる＝自治体横断で安全。
# 適用は _apply_suffix_rules 参照：接尾辞が【形態素の境界から始まる時だけ】当てる。
# これにより「交通（会社名）」「普通」「水口＝みなくち」等の複合語内の同字は誤補正しない。
# 長い接尾辞から順に照合する（入口/出口 を単独の 口 より先に）。
SUFFIX_READING_RULES = [
    ("入口", "いりぐち"), ("出口", "でぐち"),
    ("東口", "ひがしぐち"), ("西口", "にしぐち"),
    ("南口", "みなみぐち"), ("北口", "きたぐち"),
    ("通り", "どおり"), ("通", "どおり"),
    ("口", "ぐち"),
]


def _morpheme_offsets(name: str):
    """Sudachi(mode C) での各形態素の開始文字オフセット集合（＝形態素境界）を返す。
    読みエンジンが無ければ None。副作用防止のガード判定に使う。"""
    tk = _get_sudachi()
    if tk is None:
        return None
    try:
        offs = {0}
        pos = 0
        for m in tk.tokenize(name, _sudachi_tok.Tokenizer.SplitMode.C):
            pos += len(m.surface())
            offs.add(pos)
        return offs
    except Exception:
        return None


def _apply_suffix_rules(name: str, base_reading: str) -> str:
    """表記 name の末尾が既知の地名接尾辞で、かつ【その接尾辞が独立した形態素の境界から
    始まる】時だけ、末尾の読みを規則で確定する。

    形態素境界で判定するのが要点。「交通」「普通」「水口」のように接尾辞が複合語の一部
    （＝境界でない）場合は規則を当てない。これにより「○○交通（会社名）→○○どおり」の
    ような誤補正を、個別の除外リストではなく一般的な仕組みで防ぐ。
    前半だけ Sudachi で読み、末尾を規則の読みに差し替える。前半が読めない・境界が取れない
    時は規則を無理に当てず base_reading を返す（誤補正しない＝正しく失敗）。"""
    offs = _morpheme_offsets(name)
    if offs is None:
        return base_reading
    for suf, suf_read in SUFFIX_READING_RULES:   # 長い接尾辞から順に
        if name.endswith(suf) and (len(name) - len(suf)) in offs:
            prefix = name[: -len(suf)]
            if not prefix:
                return suf_read
            pre = _sudachi_hira(prefix)
            if pre:
                return pre + suf_read
            return base_reading
    return base_reading


def _kata2hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _has_kanji(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in s)


def _sudachi_hira(text: str):
    """SudachiPy でひらがな読みを得る。読めない語が漢字で残る場合は None（→pykakasiへ）。"""
    tk = _get_sudachi()
    if tk is None:
        return None
    try:
        mode = _sudachi_tok.Tokenizer.SplitMode.C
        out = []
        for m in tk.tokenize(text, mode):
            pos = m.part_of_speech()[0] if m.part_of_speech() else ""
            # 記号・括弧・空白（（）・- 等）は読みにすると「きごう」等になるため原文のまま残す。
            if "記号" in pos or "空白" in pos:
                out.append(m.surface())
                continue
            r = m.reading_form() or ""
            out.append(_kata2hira(r) if r else m.surface())
        res = "".join(out)
        if res and res.strip() and not _has_kanji(res):
            return res
    except Exception:
        return None
    return None


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

def to_hiragana(text: str) -> str:
    """漢字混じり文字列を ひらがな に変換する（SudachiPy＝Apache-2.0・オフライン）。

    半角カナ(ﾌｧﾐﾘｰﾏｰﾄ 等)は濁点・半濁点の誤処理を避けるため先に NFKC で全角化する。
    Sudachi の読みに対し、停留所名の連濁・地名末尾を決定的な規則で補正する
    （東口=ひがしぐち 等）。読めなければ "" を返す（→ 補正辞書 or ④で人が入力）。
    """
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", text)
    base = _sudachi_hira(norm) or ""
    fixed = _apply_suffix_rules(norm, base)
    return fixed or base


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

def collect_translation_targets(stops_rows: list[dict], routes_rows: list[dict],
                                 agency_rows: list[dict] | None = None,
                                 trips_rows: list[dict] | None = None
                                 ) -> list[tuple[str, str, str]]:
    """翻訳対象のリストを返す（stops / routes / agency / trips をカバー）。

    Returns: list of (table_name, field_name, field_value)
    """
    targets: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(table: str, field: str, value: str) -> None:
        v = (value or "").strip()
        if v and (table, field, v) not in seen:
            seen.add((table, field, v))
            targets.append((table, field, v))

    for s in stops_rows:
        _add("stops", "stop_name", s.get("stop_name") or "")
    for r in routes_rows:                       # 路線名（長・短の両方）
        _add("routes", "route_long_name", r.get("route_long_name") or "")
        _add("routes", "route_short_name", r.get("route_short_name") or "")
    for a in (agency_rows or []):               # 事業者名
        _add("agency", "agency_name", a.get("agency_name") or "")
    for t in (trips_rows or []):                # 行き先表示（trip_headsign）
        _add("trips", "trip_headsign", t.get("trip_headsign") or "")
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
    parser.add_argument("--agency", default=None, help="入力 agency.txt（任意・事業者名の読み用）")
    parser.add_argument("--trips", default=None, help="入力 trips.txt（任意・行き先表示の読み用）")
    parser.add_argument("-o", "--output", default="translations.txt",
                        help="出力 translations.txt （既定: ./translations.txt）")
    parser.add_argument("--export-en-prompt", default=None,
                        help="LLM 英訳プロンプトを書き出すファイルパス（任意）")
    parser.add_argument("--merge-en", default=None,
                        help="LLM 英訳結果 JSON （{name: translation, ...}）を読み込んでマージ")
    parser.add_argument("--no-hiragana", action="store_true",
                        help="ja-Hrkt（ふりがな）を生成しない（SudachiPy 未導入時の救済など）")
    parser.add_argument("--reading-dict", default=None,
                        help="全国共通辞書CSV(stop_name,ja-Hrkt,en)を明示指定。"
                             "未指定なら階層辞書の national.csv（無ければ旧 stop_readings.csv）を使用")
    # 階層辞書（全国<自治体<路線<利用者）の識別子。指定した層があればマージして上書きする。
    parser.add_argument("--dict-dir", default=None,
                        help="辞書ルート。未指定は references/dictionaries/（環境変数 GTFS_DICT_DIR 可）")
    parser.add_argument("--municipality", default=None, help="自治体辞書のキー（例: 福岡県香春町）")
    parser.add_argument("--route", default=None, help="路線辞書のキー（カンマ区切りで複数可）")
    parser.add_argument("--dict-user", default=None, help="利用者辞書のキー（例: 佐藤）")
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
    agency_rows: list[dict] = []
    trips_rows: list[dict] = []
    if args.agency and Path(args.agency).exists():
        agency_rows, _ = read_csv(Path(args.agency))
    if args.trips and Path(args.trips).exists():
        trips_rows, _ = read_csv(Path(args.trips))
    targets = collect_translation_targets(stops_rows, routes_rows, agency_rows, trips_rows)
    print(f"翻訳対象: {len(targets)} 件 (stops: "
          f"{sum(1 for t in targets if t[0]=='stops')}, "
          f"routes: {sum(1 for t in targets if t[0]=='routes')})")
    print()

    # --- ひらがな読み生成（SudachiPy／Apache-2.0・オフライン。pykakasi(GPL)は不使用）---
    hiragana_map: dict[str, str] = {}
    reading_available = _get_sudachi() is not None
    if not args.no_hiragana:
        if not reading_available:
            print("Warning: 読み生成エンジン SudachiPy がありません。ja-Hrkt をスキップします"
                  "（`pip install sudachipy sudachidict_core` を推奨）。"
                  "読みは補正辞書 or ④の確認画面で入力・補正できます。",
                  file=sys.stderr)
        else:
            for _, _, value in targets:
                hiragana_map[value] = to_hiragana(value)

    # --- 階層辞書（全国<自治体<路線<利用者）で Sudachi＋規則の読みを上書き ---
    # 低優先→高優先でマージ済みの {stop_name: spec(+_layer)} を得る。_layer が出典（監査基盤）。
    # 後方互換: reading_dictionary が無い/失敗しても、旧 load_reading_dict にフォールバック。
    en_dict: dict[str, str] = {}
    reading_layers: dict[str, dict] = {}
    try:
        import reading_dictionary as _rd
        reading_layers = _rd.resolve(dict_dir=args.dict_dir, municipality=args.municipality,
                                     routes=args.route, user=args.dict_user,
                                     national_path=args.reading_dict)
    except Exception as e:
        print(f"Warning: 階層辞書の読込に失敗、旧辞書にフォールバック: {e}", file=sys.stderr)
        _legacy = Path(args.reading_dict) if args.reading_dict else (
            Path(__file__).resolve().parent.parent / "references" / "data" / "stop_readings.csv")
        for nm, spec in load_reading_dict(_legacy).items():
            reading_layers[nm] = spec
    if reading_layers:
        n_over = 0
        _by_layer: dict[str, int] = {}
        for _, _, value in targets:
            spec = reading_layers.get(value)
            if not spec:
                continue
            if spec.get("ja-Hrkt"):
                hiragana_map[value] = spec["ja-Hrkt"]; n_over += 1
                _by_layer[spec.get("_layer", "national")] = _by_layer.get(spec.get("_layer", "national"), 0) + 1
            if spec.get("en"):
                en_dict[value] = spec["en"]
        _layers_desc = "、".join(f"{k}:{v}" for k, v in _by_layer.items()) or "なし"
        print(f"階層辞書で {n_over}件の読みを上書き（層別 {_layers_desc}）")

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

        # ja-Hrkt: SudachiPy＋補正規則＋補正辞書
        if not args.no_hiragana and reading_available:
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
            "reading_engine": ("sudachipy" if reading_available and not args.no_hiragana
                               else "none"),
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
    print(f"読みエンジン:              {report['summary']['reading_engine']}（SudachiPy/Apache-2.0）")
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
