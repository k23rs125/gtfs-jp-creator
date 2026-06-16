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
[条件確認]   JSON            →  条件確認サマリを提示し、要入力項目をユーザーが補完
[Step 3]    JSON            →  GTFS-JP の各CSVファイル（JP拡張 agency_jp/office_jp 含む）
[Step 3.5a] stops.txt       →  緯度経度補完：旧GTFS-JPフィードから再利用
[Step 3.5b] stops.txt       →  緯度経度補完：国土数値情報 P11
[Step 3.5c] stops.txt       →  緯度経度補完：Nominatim（OSM）フォールバック
[Step 3.x]  stops.txt       →  停留所名の表記揺れ正規化（canonicalize）
[Step 4]    stop_times      →  shapes.txt 生成（OSRM routing /route）
[Step 6]    stops/routes    →  translations.txt 生成（既定 ja-Hrkt / en、--include-ja で ja も）
[Step 5]    全ファイル群     →  zip パッケージング
[Step 7]    zip             →  バリデーション（GTFS Validator + JP拡張独自検証）
```

**条件確認（Step 2 と Step 3 の間）**：PDF から取れない事業者情報（住所・
正式名称など）は、Step 2 完了時に `scripts/condition_summary.py` で
「条件確認サマリ」として一覧提示する。各項目を 🟦自動検出 / 🟨自動補完 /
🟧要入力 の3分類で示し、ユーザーは 🟧 をまとめて補完してから生成へ進む。
逐次質問を避け、確認を1回に集約する設計（`画面操作フロー設計_v2.md`）。

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
- スキーマには `calendar_dates` / `fare_attributes` / `fare_rules` のほか、
  GTFS-JP拡張の `agency_jp` / `office_jp` も含む
  （PDF に運休日・運賃の記載があれば抽出。事業者情報は条件確認で補完）

### 条件確認画面

- スクリプト: `scripts/condition_summary.py`
- Step 2 出力 JSON から「条件確認サマリ」（Markdown）を生成し、全項目を
  🟦自動検出 / 🟨自動補完 / 🟧要入力 に分類して一覧提示する。
- ユーザーが補完した値は中間 JSON の `_meta.user_overrides` に
  `"table.field"` 形式（例 `agency_jp.agency_zip_number`）で書き戻す。
  対応テーブル: `agency` / `agency_jp` / `feed_info`。
- 設計の詳細は `画面操作フロー設計_v2.md` を参照。

### Step 3: GTFS-JPファイル生成

- スクリプト: `scripts/generate_gtfs_files.py`
- 仕様: `references/gtfs-jp-spec-v4.0-summary.md` および `references/field-definitions/`
- 生成ファイル: `agency.txt` `agency_jp.txt`（JP拡張・必須）`routes.txt`
  `routes_jp.txt` `stops.txt` `trips.txt` `stop_times.txt` `calendar.txt`
  `calendar_dates.txt` `fare_attributes.txt` `fare_rules.txt` `feed_info.txt`
- `office_jp.txt`（JP拡張・任意）は `office_jp` セクションがある場合のみ生成
- 生成前に `_meta.user_overrides`（条件確認画面の補完値）を最終値へ反映
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
- 既定は `ja-Hrkt`（pykakasi で漢字→ひらがな）/ `en`（LLM 英訳）の2言語。`--include-ja` で
  `ja`（原文）行も出せる。既定で ja を出さないのは、feed_lang=ja のとき停留所名の日本語原本は
  stops.txt が持ち、translations.txt の ja 行は重複になるため（公式フィードも ja-Hrkt / en 構成）
- 2段階構成: 抽出フェーズで LLM 用プロンプトを export → 英訳 JSON を merge
- 難読地名のふりがな・英訳の誤りは `scripts/apply_manual_readings.py` で手動上書きできる
  （ja-Hrkt / en のみ・field_value をキーに上書き。`apply_manual_coords.py` と同じ思想）

### Step 5: パッケージング

- スクリプト: `scripts/package_gtfs_zip.py`
- `--substitute` で座標入り stops.txt や shape_id 付き trips.txt を差し替え梱包
- 出力: `<事業者名>_gtfs-jp.zip`

### Step 7: バリデーション

- スクリプト: `scripts/validate_gtfs.py`（MobilityData GTFS Validator のラッパー）
- 必要環境: Java 17以上のJRE + `gtfs-validator-cli.jar`（`tools/` に配置）
- ERROR / WARNING / INFO を severity 別に集計してサマリ表示
- **JP拡張の独自検証**: `scripts/validate_gtfs_jp_extensions.py`
  - MobilityData Validator は GTFS-JP拡張（agency_jp/office_jp/pattern_jp/
    routes_jp）に未対応のため、本スクリプトで必須カラム・参照整合性
    （agency_id/route_id）・値域（郵便番号形式・direction_id）を検証する

### 目視検証支援ツール（任意・推奨）

機械的な Validator は「形式の正しさ」しか保証できない。**「PDF 通りか」「座標は妥当か」
の最終確認は目視**になる（現場のプロも機械Validator＋目視で品質を担保している）。
以下の3ツールは、その目視を楽に・確実にするための補助で、いずれも決定的処理（推測なし）。
おかしい点は無理に整形せず色・注記で人に確認を促す（「正しく失敗」）。公式データが無くても使える。

- **対応表**: `scripts/make_correspondence_table.py`
  - 座標抽出結果(extract JSON)とstops.txtを突き合わせ、便ごとに 番号・名前・時刻・緯度経度
    を並べたCSVを出力。座標なし・bbox範囲外を自動マーク。人がPDFと照合する作業を助ける。
  - 例: `python scripts/make_correspondence_table.py extract.json --stops gtfs/stops.txt -o correspondence.csv --bbox 130.34,33.18,130.52,33.32`
- **速度チェック**: `scripts/check_speed.py`
  - 停留所間の直線距離(haversine)と時刻差から区間速度を計算し、速すぎ(既定>60km/h)・
    遅すぎ(<2km/h)・距離あり所要0分 を検出。公式データ無しで時刻の妥当性を機械チェックできる。
  - 例: `python scripts/check_speed.py --stops gtfs/stops.txt --stop-times gtfs/stop_times.txt -o speed.csv`
- **地図表示**: `scripts/make_map_view.py`
  - stops.txt（+任意で shapes/trips/stop_times）から検証用マップHTML(1枚完結・データ埋め込み・
    外部送信なし)を生成。範囲外座標を橙で強調、ルート線を重ね、便を選ぶと停車停留所を番号順に強調。
  - 例: `python scripts/make_map_view.py gtfs/stops.txt --shapes gtfs/shapes.txt --trips gtfs/trips.txt --stop-times gtfs/stop_times.txt --bbox 130.34,33.18,130.52,33.32 --title "事業者名" --out map_view.html`
  - 注意: 地図タイル表示にはネット接続が要る（サンドボックス不可・利用者環境で開く）。

### ワンコマンド実行

- スクリプト: `scripts/run_pipeline.py`
- config JSON 1枚で Step 3〜7 を一括オーケストレーション
- 実行前に条件確認サマリ（要入力項目）を表示し、最後に Step 7b として
  GTFS-JP 拡張検証（`validate_gtfs_jp_extensions.py`）も自動実行する
- `translations_en_json` を指定すると Step 6 で英訳を自動マージ
- `--dry-run` で実行計画のプレビュー可能

### 精度評価（任意）

- `scripts/eval_compare.py` — 公式フィードとの集合比較（routes/stops/stop_times）
- `scripts/analyze_stop_times_diff.py` — trip 単位対応付けで時刻の真の精度を測定

## 必要環境

- Python 3.10以上
- Java 17以上のJRE（Step 7 GTFS Validator v8 系の実行に必要）
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
