---
name: gtfs-jp-creator
description: Use this skill when the user wants to create, generate, validate, or fix GTFS-JP (Japan public transit data standard v4.0) static feed data, especially from non-machine-readable sources such as bus timetable PDFs, Excel spreadsheets, or textual descriptions of routes/stops. Intended for non-experts (bus operators, municipal staff, citizens, researchers) without programming expertise. The skill guides users through (1) PDF/image→Markdown extraction, (2) structured table extraction (routes/stops/trips/stop_times), (3) generation of all GTFS-JP files including JP extensions (agency_jp/office_jp/pattern_jp/routes_jp.txt), (4) shapes.txt via OSRM map matching, and (5) validation using MobilityData GTFS Validator + JP-extension checks. Also use when the user asks about specific GTFS-JP file formats (stops.txt, routes.txt, trips.txt, stop_times.txt, calendar.txt, fare_attributes.txt, feed_info.txt). Trigger keywords: GTFS-JP, GTFSデータ作成, GTFS自動生成, GTFS検証, バス時刻表, 公共交通オープンデータ, MobilityData Validator.
---

# GTFS-JP Creator Skill

このSkillは、専門家でない人（バス事業者・自治体担当者・一般市民・研究者など）が、機械可読でない情報源（バス時刻表PDF、Excelシート、紙の時刻表の写真など）から **GTFS-JP v4.0（令和8年3月版）** に準拠した静的GTFSデータを作成するのを支援します。

## いつこのSkillを使うか

以下のような状況で発動します:

- ユーザーが「GTFS-JPデータを作りたい」「バス時刻表からGTFSを作って」と発言
- バス時刻表PDF / Excel / 画像をアップロードして「これを公共交通オープンデータにして」と依頼
- 「GTFS自動生成」「GTFSデータ作成」というキーワードを含む質問
- 既存のGTFSデータの **検証・修正** を依頼された場合

## 全体ワークフロー

```
[Step 1]    PDF/画像/Excel  →  Markdown 抽出（エンジン選択可：pymupdf4llm or MinerU）
[Step 2]    Markdown        →  構造化中間表現 JSON（LLM：Claude / ChatGPT / Gemini）
[Step 3]    JSON            →  GTFS-JP の各CSVファイル
[Step 3.5a] stops.txt       →  緯度経度補完：旧GTFS-JPフィードから再利用
[Step 3.5b] stops.txt       →  緯度経度補完：国土数値情報 P11
[Step 3.5c] stops.txt       →  緯度経度補完：Nominatim（OSM）フォールバック
[Step 3.x]  stops.txt       →  停留所名の表記揺れ正規化（canonicalize）
[Step 4]    stop_times      →  shapes.txt 生成（OSRM routing /route）
[Step 6]    stops/routes    →  translations.txt 生成（ja / ja-Hrkt / en）
[Step 5]    全ファイル群     →  zip パッケージング
[Step 7]    zip             →  バリデーション（MobilityData GTFS Validator）
```

**Step 3 以降は `scripts/run_pipeline.py` で一括自動実行できる**（config JSON 1枚で
Step 3〜7 をオーケストレーション）。Step 1（PDF抽出）と Step 2（LLM）は個別に実施する。

緯度経度補完（Step 3.5）は **3段階の階層** で動く：旧フィードがあれば 3.5a で 100%、
無ければ 3.5b の国土数値情報 P11 で 80〜95%、残りを 3.5c の Nominatim で補う。

## 各ステップの詳細

### Step 1: PDF/画像 → Markdown

スクリプト: `scripts/pdf_to_markdown.py`

**2つのエンジンから選択可能** (`--engine` オプション):

| エンジン | 速度 | 品質（装飾レイアウト） | 用途 |
|---|---|---|---|
| `pymupdf4llm` (default) | 高速（数秒） | 中（シンプルなPDFに最適） | 一般的なテキストPDF |
| `mineru` (opt-in) | 遅い（CPUで数十分、GPUで数分） | 高（並列テーブル/装飾OK） | カラフル・複雑なバス時刻表PDF |

**実証データ（須恵町コミュニティバス時刻表PDF v4.0、令和7年4月1日改正で検証）:**

- `pymupdf4llm`: 全7路線中、装飾的なpage 1の路線テーブルを正しく分離できず picture text fallback。時刻末尾切れ多数（例 `14:50` → `14:5`）。**抽出成功率 ~30%**
- `mineru`: 全7路線分のテーブルを完璧に分離抽出。時刻完全保持。日本語OCRの誤認識1件のみ（`7→フ`）。**抽出成功率 ~95%**

→ 推奨: 装飾的・複数路線が並列レイアウトされた日本のコミュニティバス時刻表は **`--engine mineru`**。

使用例:

```bash
# 高速・軽量（シンプルなPDFに）
python scripts/pdf_to_markdown.py timetable.pdf -o out.md

# 高品質（装飾的なバス時刻表PDFに）
python scripts/pdf_to_markdown.py timetable.pdf --engine mineru --lang japan -o out.md
```

LLMプロンプト: `references/prompts/01_pdf_extraction.md`

### Step 2: Markdown → 構造化中間表現 JSON

- LLMが Markdown を解析し、JSON形式の中間表現を生成
- LLMプロンプト: `references/prompts/02_structured_extraction.md`
- **対応 LLM**: Claude（Skill 自動）/ ChatGPT / Gemini（プロンプトをコピペ）
- Markdown と JSON の2つの中間表現を挟む理由は `Markdown_JSON_設計説明書` を参照
- スキーマには `calendar_dates` / `fare_attributes` / `fare_rules` も含む
  （PDF に運休日・運賃の記載があれば抽出）

### Step 3: GTFS-JPファイル生成

- スクリプト: `scripts/generate_gtfs_files.py`
- 仕様: `references/gtfs-jp-spec-v4.0-summary.md` および `references/field-definitions/`
- 生成ファイル: `agency.txt` `routes.txt` `routes_jp.txt` `stops.txt` `trips.txt`
  `stop_times.txt` `calendar.txt` `calendar_dates.txt` `fare_attributes.txt`
  `fare_rules.txt` `feed_info.txt`
- `calendar.end_date` が未指定なら start_date から日本の年度末を自動計算

### Step 3.5: 緯度経度補完（3段階階層）

stops.txt の `stop_lat` / `stop_lon` を埋める。優先順位の高い順に：

- **3.5a** `scripts/merge_stop_coords.py` — 旧 GTFS-JP フィードから停留所名マッチで
  座標を再利用。再作成タスクなら 100% 補完。
- **3.5b** `scripts/enrich_stops_p11.py` — 国土数値情報 P11（国交省バス停データ）から
  fuzzy マッチで補完。新規自治体でも 80〜95%。
- **3.5c** `scripts/enrich_stops.py` — Nominatim（OSM）フォールバック。bbox + 県/市町村
  フィルタで誤マッチを排除（補完率は低めだが「正しく失敗」する）。

### Step 3.x: 停留所名の正規化

- スクリプト: `scripts/canonicalize_stops.py`
- 参照フィードから canonical な停留所名表記を引き当て、表記揺れ（「JR」接頭辞、
  「(駅前広場)」接尾辞、「」括弧など）を吸収する。

### Step 4: shapes.txt 生成

- スクリプト: `scripts/generate_shapes.py`
- 戦略: OSRM routing API（`/route`）で停留所間の道路経路を取得
  - 停留所は確定ウェイポイントなので map-matching ではなく routing が正しい
- フォールバック: OSRM が使えない場合は停留所間を直線結合
- 同じ停留所列の trip はパターン共有して API 呼び出しを削減

### Step 6: translations.txt 生成

- スクリプト: `scripts/generate_translations.py`
- 3言語対応: `ja`（原文）/ `ja-Hrkt`（pykakasi で漢字→ひらがな）/ `en`（LLM 英訳）
- 2段階構成: 抽出フェーズで LLM 用プロンプトを export → 英訳 JSON を merge

### Step 5: パッケージング

- スクリプト: `scripts/package_gtfs_zip.py`
- `--substitute` で座標入り stops.txt や shape_id 付き trips.txt を差し替え梱包
- 出力: `<事業者名>_gtfs-jp.zip`

### Step 7: バリデーション

- スクリプト: `scripts/validate_gtfs.py`（MobilityData GTFS Validator のラッパー）
- 必要環境: Java 11以上のJRE + `gtfs-validator-cli.jar`（`tools/` に配置）
- ERROR / WARNING / INFO を severity 別に集計してサマリ表示

### ワンコマンド実行

- スクリプト: `scripts/run_pipeline.py`
- config JSON 1枚で Step 3〜7 を一括オーケストレーション
- `--dry-run` で実行計画のプレビュー可能

### 精度評価（任意）

- `scripts/eval_compare.py` — 公式フィードとの集合比較（routes/stops/stop_times）
- `scripts/analyze_stop_times_diff.py` — trip 単位対応付けで時刻の真の精度を測定

## 必要環境

- Python 3.10以上
- Java 11以上のJRE（Step 7 GTFS Validator実行用）
- インターネット接続（Step 4 OSRM API・Step 3.5c Nominatim・MinerU初回モデルDL用）
- 推奨実行環境: Cowork mode（Claude desktop app）

### ステップ別の追加要件

- **Step 1 pymupdf4llm**（default）: `pip install pymupdf pymupdf4llm`
- **Step 1 mineru**（opt-in、装飾PDF用）: `pip install -U "mineru[core]"`（初回 ~3GB のMLモデルDLあり）
- **Step 3.5b P11**: `pip install pyshp` + 国土数値情報 P11 Shapefile（都道府県別 DL）
- **Step 6 translations**: `pip install pykakasi`（ja-Hrkt 生成用）
- **Step 7 Validator**: `gtfs-validator-cli.jar` を `tools/` に配置
  （https://github.com/MobilityData/gtfs-validator/releases から DL）

## 参考資料

- `references/gtfs-jp-spec-v4.0-summary.md` — GTFS-JP v4.0仕様の要約
- `references/field-definitions/` — 各テーブル・各フィールドの詳細仕様
- `references/examples/` — 正解GTFS-JPデータのサンプル（AIGID提供データなど）
- `references/prompts/` — 各ステップで使うLLMプロンプト集

## ライセンス

Apache License 2.0
