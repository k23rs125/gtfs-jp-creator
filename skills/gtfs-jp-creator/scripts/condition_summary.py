"""
condition_summary.py
====================

条件確認画面（v2 設計：条件確認型／自動生成型）のサマリを生成する。

Step 2（Markdown → 構造化 JSON）が完了した時点で本スクリプトを実行すると、
中間表現 JSON を読み取り、GTFS-JP 生成に必要な全項目を 1 枚の
「条件確認サマリ」(Markdown) にまとめて提示する。

各項目は抽出元によって 3 つに分類する:

    🟦 自動検出     PDF 抽出 + LLM で値が取れた項目
    🟨 自動補完     生成時にスクリプトが自動で埋める項目（既定値・外部API）
    🟧 要入力       PDF に無く、ユーザー入力が必要な項目

利用者はこのサマリを見て、要入力（🟧）の項目を埋めてから生成に進む。
ユーザーが上書きした値は中間 JSON の `_meta.user_overrides` に
"table.field" 形式で書き戻し、generate_gtfs_files.py が反映する。

Usage:
    python condition_summary.py <input.json>
    python condition_summary.py <input.json> -o summary.md

Exit code:
    0 = 要入力なし / 1 = 要入力あり（参考情報。生成自体は可能）

License: Apache 2.0
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BLUE = "🟦"
YELLOW = "🟨"
ORANGE = "🟧"

# user_overrides に書き戻せるテーブル（apply_overrides と同じ範囲）
ALLOWED_OVERRIDE_TABLES = ("agency", "agency_jp", "feed_info")


def _has(v) -> bool:
    return v not in (None, "", [], {})


def _get(section: dict, key: str):
    if not isinstance(section, dict):
        return None
    return section.get(key)


def apply_overrides(data: dict) -> int:
    """`_meta.user_overrides`（"table.field" 形式）を data に反映する。

    条件確認画面で一度値を編集したあと再表示する場合に、編集後の状態を
    正しく映すための処理。generate_gtfs_files.py と同じ規則。
    Returns: 反映した件数。
    """
    overrides = (data.get("_meta") or {}).get("user_overrides") or {}
    applied = 0
    for key, value in overrides.items():
        if "." not in key:
            continue
        table, field = key.split(".", 1)
        if table not in ("agency", "agency_jp", "feed_info"):
            continue
        section = data.get(table)
        if not isinstance(section, dict):
            section = {}
            data[table] = section
        section[field] = value
        applied += 1
    return applied


class Field:
    """確認サマリの 1 項目。"""

    def __init__(self, key, label, value, category, example="", note=""):
        # category: detected / default / required / optional
        self.key = key
        self.label = label
        self.value = value
        self.category = category
        self.example = example
        self.note = note

    @property
    def mark(self) -> str:
        if self.category == "default":
            return YELLOW
        if self.category == "detected":
            return BLUE
        # required / optional
        return BLUE if _has(self.value) else ORANGE

    @property
    def is_missing_required(self) -> bool:
        return self.category == "required" and not _has(self.value)

    def render(self) -> str:
        if _has(self.value):
            disp = str(self.value)
        elif self.category == "optional":
            disp = "（任意・未設定）"
        elif self.category == "default":
            disp = self.note or "（生成時に自動で設定）"
        else:  # required, empty
            disp = "[要入力]"
            if self.example:
                disp += f"　(例: {self.example})"
        extra = ""
        if _has(self.value) and self.note:
            extra = f"　— {self.note}"
        return f"  {self.mark} {self.label:<22} {disp}{extra}"


def build_fields(data: dict) -> dict[str, list[Field]]:
    """中間 JSON から確認サマリの全項目を組み立てる。"""
    agency = data.get("agency") or {}
    agency_jp = data.get("agency_jp") or {}
    feed_info = data.get("feed_info") or {}
    calendar = data.get("calendar") or []

    # calendar から運行期間の手掛かりを得る
    start_dates = [c.get("start_date") for c in calendar
                   if isinstance(c, dict) and c.get("start_date")]
    cal_start = start_dates[0] if start_dates else None

    groups: dict[str, list[Field]] = {}

    groups["agency"] = [
        Field("agency.agency_id", "agency_id",
              _get(agency, "agency_id"), "detected", note="LLM 自動採番"),
        Field("agency.agency_name", "agency_name",
              _get(agency, "agency_name"), "detected", note="PDF 表紙から"),
        Field("agency.agency_url", "agency_url",
              _get(agency, "agency_url"), "required",
              example="https://www.town.sue.fukuoka.jp/"),
        Field("agency.agency_phone", "agency_phone",
              _get(agency, "agency_phone"), "required",
              example="092-932-1151"),
        Field("agency.agency_email", "agency_email",
              _get(agency, "agency_email"), "optional"),
        Field("agency.agency_timezone", "agency_timezone",
              _get(agency, "agency_timezone"), "default",
              note="Asia/Tokyo（既定値）"),
        Field("agency.agency_lang", "agency_lang",
              _get(agency, "agency_lang"), "default", note="ja（既定値）"),
    ]

    # 後方互換: 旧スキーマでは agency_jp 系が agency に直接入っていた
    def ajp(key):
        return _get(agency_jp, key) or _get(agency, key)

    groups["agency_jp"] = [
        Field("agency_jp.agency_official_name", "agency_official_name",
              ajp("agency_official_name"), "required", example="須恵町"),
        Field("agency_jp.agency_zip_number", "agency_zip_number",
              ajp("agency_zip_number"), "required", example="811-2192"),
        Field("agency_jp.agency_address", "agency_address",
              ajp("agency_address"), "required",
              example="福岡県糟屋郡須恵町大字須恵771"),
        Field("agency_jp.agency_president_pos", "agency_president_pos",
              ajp("agency_president_pos"), "optional"),
        Field("agency_jp.agency_president_name", "agency_president_name",
              ajp("agency_president_name"), "optional"),
    ]

    groups["feed_info"] = [
        Field("feed_info.feed_publisher_name", "feed_publisher_name",
              _get(feed_info, "feed_publisher_name"), "required",
              example="須恵町"),
        Field("feed_info.feed_publisher_url", "feed_publisher_url",
              _get(feed_info, "feed_publisher_url"), "required",
              example="https://www.town.sue.fukuoka.jp/"),
        Field("feed_info.feed_start_date", "feed_start_date",
              _get(feed_info, "feed_start_date") or cal_start,
              "required", example="20250401",
              note=("PDF 改正日から推定（要確認）" if cal_start else "")),
        Field("feed_info.feed_end_date", "feed_end_date",
              _get(feed_info, "feed_end_date"), "default",
              note="start_date から日本の年度末を自動計算"),
        Field("feed_info.feed_version", "feed_version",
              _get(feed_info, "feed_version"), "default",
              note="生成日から自動設定"),
    ]
    return groups


def office_section(data: dict) -> list[str]:
    offices = data.get("office_jp")
    lines = ["────────── 🏢 営業所情報 (office_jp.txt) ──────────"]
    if not offices:
        lines.append(f"  {ORANGE} （任意・未設定）　営業所情報があれば "
                     f"office_id / office_name を入力してください。")
        return lines
    if isinstance(offices, dict):
        offices = [offices]
    for i, o in enumerate(offices, start=1):
        if not isinstance(o, dict):
            continue
        lines.append(f"  {BLUE} 営業所 {i}: "
                     f"{o.get('office_name') or '[要入力]'} "
                     f"(office_id={o.get('office_id') or f'OFFICE{i:02d}'})")
    return lines


def detection_section(data: dict) -> list[str]:
    routes = data.get("routes") or []
    stops = data.get("stops") or []
    trips = data.get("trips") or []
    stop_times = data.get("stop_times") or []
    calendar = data.get("calendar") or []
    svc = [c.get("service_id") for c in calendar
           if isinstance(c, dict) and c.get("service_id")]
    return [
        "────────── 🚌 検出結果（自動） ──────────",
        f"  {BLUE} 路線数      {len(routes)} 路線",
        f"  {BLUE} 停留所数    {len(stops)}",
        f"  {BLUE} 便数        {len(trips)}",
        f"  {BLUE} 時刻データ  {len(stop_times)} 行 (stop_times)",
        f"  {BLUE} カレンダー  {len(svc)} 種別" +
        (f"（{ ' / '.join(svc) }）" if svc else ""),
    ]


def build_summary(data: dict) -> tuple[str, int]:
    """確認サマリの Markdown 文字列と、要入力（🟧 必須）件数を返す。"""
    groups = build_fields(data)
    missing = sum(f.is_missing_required
                  for g in groups.values() for f in g)

    L: list[str] = []
    L.append("=" * 72)
    L.append("＜条件確認＞ Step 2 完了。生成前に以下をご確認ください。")
    L.append("")
    L.append("凡例:  🟦 自動検出（PDF+LLM）  "
             "🟨 自動補完（生成時）  🟧 要入力（PDF外）")
    L.append("")
    L += detection_section(data)
    L.append("")
    L.append("────────── 🏢 事業者情報 (agency.txt) ──────────")
    L += [f.render() for f in groups["agency"]]
    L.append("")
    L.append("────────── 🏠 拡張情報 (agency_jp.txt / GTFS-JP 必須) ──────────")
    L += [f.render() for f in groups["agency_jp"]]
    L.append("")
    L += office_section(data)
    L.append("")
    L.append("────────── 📅 フィード情報 (feed_info.txt) ──────────")
    L += [f.render() for f in groups["feed_info"]]
    L.append("")
    L.append("────────── ⚠️ 要入力サマリ ──────────")
    if missing:
        L.append(f"  🟧 要入力（GTFS-JP 推奨）: {missing} 件")
        L.append("     空欄のままでも生成は可能ですが、GTFS Validator で"
                 "警告が出ることがあります。")
    else:
        L.append("  ✅ 要入力の項目はありません。そのまま生成できます。")
    L.append("")
    L.append("────────── ✅ 次の操作 ──────────")
    L.append("  A) 値を編集する")
    L.append("     例:「agency_url=https://www.town.sue.fukuoka.jp/ 、"
             "agency_phone=092-932-1151」のように")
    L.append("        複数項目をまとめて指定してください。")
    L.append("  B) この条件で生成する … 「生成」または「OK」と入力")
    L.append("  C) 中止する          … 「中止」と入力")
    L.append("=" * 72)
    return "\n".join(L), missing


def build_questions(data: dict) -> list[dict]:
    """要入力（🟧 必須・空欄）の項目を質問リストとして返す。

    Returns: list of {key, label, example, note}
    """
    groups = build_fields(data)
    questions: list[dict] = []
    for g in groups.values():
        for f in g:
            if f.is_missing_required:
                questions.append({
                    "key": f.key,
                    "label": f.label,
                    "example": f.example,
                    "note": f.note,
                })
    return questions


def export_questions(data: dict, path: Path) -> int:
    """PDF に無い要入力項目を、回答テンプレ付きの質問ファイル(Markdown)に書き出す。

    パイプラインを止めずに「質問形式」を実現するための出力。利用者または
    会話中の Claude がこのファイルを読んで回答し、JSON を埋めて
    --merge-answers に渡す。Returns: 質問件数。
    """
    questions = build_questions(data)
    L: list[str] = []
    L.append("# 条件確認：PDF に無い項目への質問")
    L.append("")
    L.append("Step 2 で PDF から取得できなかった必須項目です。各項目に回答し、")
    L.append("末尾の JSON を埋めて answers.json として保存してください。")
    L.append("（その後 `--merge-answers answers.json` で取り込めます）")
    L.append("")
    if not questions:
        L.append("✅ 要入力の項目はありません。そのまま生成できます。")
        path.write_text("\n".join(L) + "\n", encoding="utf-8")
        return 0

    for i, q in enumerate(questions, start=1):
        L.append(f"## Q{i}. {q['label']}")
        L.append(f"- 設定先キー: `{q['key']}`")
        if q["example"]:
            L.append(f"- 例: {q['example']}")
        if q["note"]:
            L.append(f"- 補足: {q['note']}")
        L.append("- 回答: ____________")
        L.append("")

    # 回答テンプレート（純粋な JSON。コメントは入れない＝そのまま json として読める）
    L.append("## 回答テンプレート（この JSON を埋めて answers.json として保存）")
    L.append("")
    L.append("キーと項目の対応: " +
             "、".join(f"{q['key']}={q['label']}" for q in questions))
    L.append("")
    L.append("```json")
    L.append("{")
    for i, q in enumerate(questions):
        comma = "," if i < len(questions) - 1 else ""
        L.append(f'  "{q["key"]}": ""{comma}')
    L.append("}")
    L.append("```")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return len(questions)


def merge_answers(data: dict, answers: dict) -> tuple[int, list[str]]:
    """回答（"table.field": value）を _meta.user_overrides にマージする。

    既存の user_overrides は保持し、空回答（None/""）は無視する。
    書き戻せるのは ALLOWED_OVERRIDE_TABLES のテーブルのみ。
    Returns: (適用件数, スキップしたキー一覧)
    """
    meta = data.setdefault("_meta", {})
    overrides = meta.setdefault("user_overrides", {})
    applied = 0
    skipped: list[str] = []
    for key, value in answers.items():
        if "." not in key or key.split(".", 1)[0] not in ALLOWED_OVERRIDE_TABLES:
            skipped.append(key)
            continue
        if value in (None, ""):
            continue  # 空回答は未入力のまま据え置く
        overrides[key] = value
        applied += 1
    return applied, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="条件確認画面（v2）のサマリを中間 JSON から生成する")
    parser.add_argument("input", help="Step 2 出力の中間 JSON")
    parser.add_argument("-o", "--output",
                        help="サマリ Markdown の出力先（省略時は標準出力）")
    parser.add_argument("--export-questions", default=None,
                        help="PDF外の要入力項目を質問ファイル(Markdown)として書き出す")
    parser.add_argument("--merge-answers", default=None,
                        help="回答JSON({\"table.field\": value})を user_overrides に反映して書き戻す")
    parser.add_argument("--write-json", default=None,
                        help="--merge-answers の書き戻し先（省略時は入力JSONを上書き）")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"Error: input not found: {in_path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(in_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: JSON パース失敗: {e}", file=sys.stderr)
        return 2

    apply_overrides(data)  # 既に編集済みの値があれば反映してから集計

    # --merge-answers: 回答(JSON)を user_overrides に反映して中間JSONに書き戻す
    if args.merge_answers:
        ans_path = Path(args.merge_answers)
        if not ans_path.exists():
            print(f"Error: answers not found: {ans_path}", file=sys.stderr)
            return 2
        try:
            answers = json.loads(ans_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"Error: answers JSON パース失敗: {e}", file=sys.stderr)
            return 2
        applied, skipped = merge_answers(data, answers)
        apply_overrides(data)  # 反映した値をサマリにも映す
        out_json = Path(args.write_json) if args.write_json else in_path
        out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"[OK] 回答を {applied} 件反映し {out_json} に書き戻しました", file=sys.stderr)
        if skipped:
            print(f"[WARN] 対象外キーをスキップ: {', '.join(skipped)}", file=sys.stderr)

    summary, missing = build_summary(data)

    # --export-questions: PDF外の要入力項目を質問ファイルに書き出す
    if args.export_questions:
        qn = export_questions(data, Path(args.export_questions))
        print(f"[OK] 質問ファイルを出力: {args.export_questions}（{qn} 件）", file=sys.stderr)

    if args.output:
        Path(args.output).write_text(summary + "\n", encoding="utf-8")
        print(f"[OK] 条件確認サマリを出力: {args.output}", file=sys.stderr)
    else:
        print(summary)

    print(f"[INFO] 要入力（🟧 必須）: {missing} 件", file=sys.stderr)
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
