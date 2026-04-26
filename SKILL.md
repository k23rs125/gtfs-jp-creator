---
name: gtfs-jp-creator
description: Use this skill when the user wants to create or generate GTFS-JP (Japan public transit data standard, v4.0, 令和8年3月版) static feed data from non-machine-readable sources such as bus timetable PDFs, photographed paper schedules, Excel spreadsheets, or textual descriptions of routes and stops. Intended for non-experts (bus operators, municipal staff, citizens, researchers) who lack programming expertise. The skill guides users through (1) PDF/image to Markdown extraction, (2) structured extraction of routes/stops/trips/stop_times, (3) generation of all GTFS-JP files including the Japanese extensions agency_jp.txt / office_jp.txt / pattern_jp.txt, (4) shapes.txt generation via OSRM map matching, and (5) validation using the official MobilityData GTFS Validator. Trigger when the user mentions GTFS-JP, GTFSデータ作成, GTFS自動生成, バス時刻表からGTFSを作りたい, 公共交通オープンデータ, バス事業者向けデータ整備, or uploads timetable PDFs/Excel to convert.
---

# GTFS-JP Creator Skill

このSkillは、専門家でない人（バス事業者・自治体担当者・一般市民・研究者など）が、機械可読でない情報源（バス時刻表PDF、Excelシート、紙の時刻表の写真など）から **GTFS-JP v4.0（令和8年3月版）** に準拠した静的GTFSデータを作成するのを支援します。

## いつこのSkillを使うか

以下のような状況で発動します:

- ユーザーが「GTFS-JPデータを作りたい」「バス時刻表からGTFSを作って」と発言
- バス時刻表PDF / Excel / 画像をアップロードして「これを公共交通オープンデータにして」と依頼
- 「GTFS自動生成」「GTFSデータ作成」というキーワードを含む質問
- 既存のGTFSデータの **検証・修正** を依頼された場合

## 全体ワークフロー（5ステップ）

```
[Step 1] PDF/画像/Excel  →  Markdown 抽出
[Step 2] Markdown        →  構造化テーブル（中間表現）
[Step 3] 構造化テーブル   →  GTFS-JP の各CSVファイル
[Step 4] stop_times      →  shapes.txt 生成（OSRM map-matching）
[Step 5] 全ファイル群     →  バリデーション（GTFS Validator + 拡張独自チェック）
                           →  zip パッケージ → 完成
```

## 各ステップの詳細

### Step 1: PDF/画像 → Markdown
- スクリプト: `scripts/pdf_to_markdown.py`
- ライブラリ: `pymupdf4llm`（テキストPDF用）/ `olmOCR`（スキャン画像用）
- LLMプロンプト: `references/prompts/01_pdf_extraction.md`

### Step 2: Markdown → 構造化テーブル
- LLMが Markdown を解析し、JSON形式の中間表現を生成
- LLMプロンプト: `references/prompts/02_structured_extraction.md`

### Step 3: GTFS-JPファイル生成
- スクリプト: `scripts/generate_gtfs_files.py`
- 仕様: `references/gtfs-jp-spec-v4.0-summary.md` および `references/field-definitions/`
- 主要ファイル:
  - `agency.txt` / `agency_jp.txt`
  - `routes.txt` / `routes_jp.txt`
  - `stops.txt`
  - `trips.txt` / `stop_times.txt`
  - `calendar.txt` / `calendar_dates.txt`
  - `fare_attributes.txt` / `fare_rules.txt`
  - `office_jp.txt` / `pattern_jp.txt`
  - `feed_info.txt` / `translations.txt`

### Step 4: shapes.txt 生成
- スクリプト: `scripts/generate_shapes.py`
- 戦略: OSRM map-matching API を使い、停留所列から最尤経路を推定
- フォールバック: map-matchingが失敗した場合は停留所間直線結合

### Step 5: バリデーション
- スクリプト: `scripts/validate_gtfs.py`（MobilityData GTFS Validator のラッパー）
- スクリプト: `scripts/validate_gtfs_jp_extensions.py`（GTFS-JP拡張部の独自チェック）
- 必要環境: Java 11以上のJRE

### 最終: パッケージング
- スクリプト: `scripts/package_gtfs_zip.py`
- 出力: `feed_<事業者名>_<生成日時>.zip`

## 必要環境

- Python 3.10以上
- Java 11以上のJRE（GTFS Validator実行用）
- インターネット接続（OSRM API利用時）
- 推奨実行環境: Cowork mode（Claude desktop app）

## 参考資料

- `references/gtfs-jp-spec-v4.0-summary.md` — GTFS-JP v4.0仕様の要約
- `references/field-definitions/` — 各テーブル・各フィールドの詳細仕様
- `references/examples/` — 正解GTFS-JPデータのサンプル（AIGID提供データなど）
- `references/prompts/` — 各ステップで使うLLMプロンプト集

## ライセンス

Apache License 2.0
