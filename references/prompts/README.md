# LLM プロンプト集

GTFS-JP作成の各ステップで Claude に与える指示テンプレート。

## ファイル一覧（予定）

| ファイル名 | 用途 |
|---|---|
| `01_pdf_extraction.md` | PDF/画像から構造化Markdownを抽出する指示 |
| `02_structured_extraction.md` | Markdownから中間JSON表現を抽出する指示 |
| `03_route_inference.md` | 部分的な情報から routes.txt 用フィールドを推論する指示 |
| `04_calendar_inference.md` | 「平日のみ」「土日祝運休」等の表記を calendar.txt に変換する指示 |
| `05_translation_check.md` | translations.txt の英訳生成・チェック |

## プロンプト設計方針

1. **役割を明示** — 「あなたはGTFS-JPデータ整備の専門家アシスタントです」
2. **入出力スキーマを示す** — JSONスキーマや具体例で形式を固定
3. **不確実性の明示** — わからない値は `null` を入れ、推測しない
4. **few-shot examples** — `references/examples/` のサンプルを参照
5. **検証可能性** — LLMの出力が後段スクリプトでパース失敗しない形を強制

## ステータス

**未作成（スケルトン）。** 各プロンプトはStep実装と並行して書き起こす。
