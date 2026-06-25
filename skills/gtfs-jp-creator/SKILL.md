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

### Step 1: 入力（Excel / PDF / 画像）→ 抽出

**入力源の選び方（確実な順）**。元データが機械可読なほど高精度なので、上から検討する:

| 入力 | 経路 | スクリプト | 精度 |
|---|---|---|---|
| **Excel(.xlsx)** | グリッドを直接読む（最確実・推奨） | `scripts/extract_timetable_excel.py` | 高（OCR/座標不要） |
| テキストPDF | 座標方式（文字座標で決定的抽出） | `scripts/extract_timetable_coords.py` | 高（誤差ゼロ実証） |
| 装飾的PDF | Markdown化→LLM構造化 | `scripts/pdf_to_markdown.py`（MinerU） | 中〜高 |
| 画像化PDF | OCR(MinerU)で抽出を試みる（**精度低下・要目視確認**）。元データ(Excel)があればそちらが最善 | `pdf_to_markdown.py --engine mineru` | 低 |
| **Publisher(.pub)** | Publisher COM で PDF化 → テキストPDFとして座標方式 | （下記の変換手順） | 高（テキスト保持時） |

**テキストPDFか画像PDFか（よくある質問）**: Word/Excel から「PDFとして保存／エクスポート」した
PDFは**通常テキストPDF**（文字オブジェクトが埋め込まれ、選択・抽出でき座標方式が効く）。
画像PDFになるのは「紙をスキャン」「画像として書き出し」等の例外のみ。判定法: PDFで文字を
ドラッグ選択できればテキストPDF。座標方式は文字数 < 20 で画像PDFと自動判定し、**OCR(MinerU)経路へ
誘導する**（精度が下がるため要目視確認の警告つき）。元データ(Excel)があれば下記で直接読むのが最善。
→ **元データが Excel/Office にあるなら、PDF化せず Excel を直接読むのが最善**（下記）。
画像PDFしか無い場合は、発行元に元データ(Excel)やテキストPDFの再エクスポートを依頼する。

**Microsoft Publisher(.pub) が渡されたとき**: `.pub` は OLE2複合ファイルで直接は読めない。
本体 Publisher(MSPUB.EXE) があれば COM でPDF化してから座標方式にかける（生成PDFがテキスト
保持なら誤差ゼロで抽出できる）。PowerShell:
`$pub=New-Object -ComObject Publisher.Application; $doc=$pub.Open($src); $doc.ExportAsFixedFormat(2,$out,1); $doc.Close(2); $pub.Quit()`
（2=PDF。Open は引数を絞る＝SaveChanges列挙の型エラー回避。Close/Quit段でCOM例外が出てもPDFは
生成済みのことが多い）。チラシ型レイアウト（路線図＋利用案内＋時刻表）でも中央の時刻表だけ拾える。
Publisher が無い環境では LibreOffice(libmspub) でも変換できる。

**Excel を直接読む（`extract_timetable_excel.py`）**:
```bash
python scripts/extract_timetable_excel.py timetable.xlsx -o extract.json
#   [--sheet シート名] [--name-col A] [--header-rows N]
```
- 停留所名の列・便（時刻列）を自動検出し、**座標方式と同じ抽出JSON形式**で出力する。
  以降の Step2(構造化)・Step3〜7 は PDF と完全に共通（`blocks[].trips[].cells[]`）。
- セルの datetime 時刻・文字列時刻どちらも対応。要予約停留所（名前に「要予約」）も検出。
- 自動検出が外れる場合は `--name-col`/`--sheet` で上書き。読めない時は推測せず警告（正しく失敗）。
- **転置レイアウト（停留所が「列」・便が「行」）**には本スクリプトは非対応。その場合は
  `scripts/extract_excel_transposed.py` を使う（各シート＝1方向、JR接続時刻・所要時間など
  停留所でない列は見出しパターンで自動除外）。実証: 芦屋タウンバス（2方向32停留所116便）で
  内部整合 1945/1945＝100\%。
  ```bash
  python scripts/extract_excel_transposed.py timetable.xlsx -o extract.json [--header-row 2]
  ```

**PDF → Markdown（`pdf_to_markdown.py`）** ※装飾的PDFをLLMで構造化する経路:

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

**順序の原則（重要）**：PDF/Excel に無い情報は、**Web検索や仮値で埋める前に、まず利用者に質問する**。
順序は「抽出 → **利用者に一括質問（下記8項目）** → 利用者が『不明』と答えた項目だけ Web検索 →
それでも不明なら仮値」。逐次に小出しで聞かず、**1枚のサマリにまとめて一括**で質問する。

**一括で質問する8項目（PDF/Excelに無く、毎回必要）**
1. 事業者（agency）：自治体か運行事業者か。名称・正式名称・所在地・電話・URL・連絡先メール
2. 運行する曜日（平日/土曜/日祝など）と運休日（祝日・お盆・年末年始）
3. 有効期間（開始日・終了日／改正日）
4. 路線名・方向・循環の割り当て（例「○○行／駅行」「左回り/右回り」）
5. 運賃（一律◯円／区間料金／無料）
6. 旧GTFS-JPフィードの有無（あれば座標再利用）。**利用者回答だけに頼らず、下記のとおり公式GTFSを能動的に確認する**
7. 要予約・特殊便の扱い
8. 対象自治体の確認（座標補完のP11・自治体制約に使用）

**公式GTFSの能動確認（重要・今回の反省）**：⑥は利用者が「なし」と答えても**実在することがある**
（例：柳川市は「旧フィードなし」との回答だったが、BODIK に公式GTFSが存在した）。対象自治体の公式GTFSを
**BODIK（`data.bodik.jp` を自治体名・自治体コードで検索）や自治体オープンデータ**で必ず確認する。
見つかれば次に使える：(a) 停留所座標の再利用で精度向上、(b) agency名・法人番号・連絡先の確定、
(c) 生成後の精度比較。**発見は利用者に報告し、使うか確認する**（勝手に上書きしない）。なお公式が手元PDFより
古い版のこともあるため（座標は再利用可・ダイヤは新しい手元PDFを優先）、版を見て使い分ける。

**「不明」と答えられた項目の扱い**：その項目を **「不明」と明記**したうえで、当方（LLM）が Web で調査し
暫定値を埋める。ただし埋めた値には必ず **「利用者から不明と回答されたため当方で調査した暫定値であり、
正確とは保証できません（要確認）」** の趣旨の一文を添える。最後にまとめて要確認項目を提示する。
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
- **agency_id の整合を自動強制**：`fare_attributes.agency_id` と `agency_jp.agency_id` は、
  構造化JSONが `AGENCY_TBD` 等のプレースホルダや異なる値を持っていても、必ず `agency.txt` の
  `agency_id` に統一する（不一致時は WARN）。`fare_attributes` の foreign_key_violation 再発を
  決定的に防ぐ。**構造化（Step2）では agency / agency_jp / fare を同一 agency_id で揃えること。**

### Step 3.5: 緯度経度補完（3段階階層）

stops.txt の `stop_lat` / `stop_lon` を埋める。優先順位の高い順に：

- **3.5a** `scripts/merge_stop_coords.py` — 旧 GTFS-JP フィードから停留所名マッチで
  座標を再利用。再作成タスクなら 100% 補完。
- **3.5b** `scripts/enrich_stops_p11.py` — 国土数値情報 P11（国交省バス停データ）から
  fuzzy マッチで補完。新規自治体でも 80〜95%。
  同名のP11候補が**市域bbox内に複数あり別地点の疑い**があるときは、黙って先頭を採用せず
  `--review-csv` で **要確認リスト**（`output_dir/座標_要確認.csv`）に書き出す。
  パイプラインは既定でこのCSVを feed と並べて出力する（あいまい0件なら生成されない）。
  → **このリストに載った停留所は利用者に「どちらの○○か」を質問**し、確定座標を得る。
- **3.5b2** `scripts/select_ambiguous_by_route.py` — **同名複数候補の経路位置選択（既定ON）**。
  3.5bで要確認となった同名複数候補を、便の経路上のあるべき位置（前後の確定停留所からの内挿推定）に
  最も近い候補へ**自動選択**する。黙って先頭採用でも推定座標でもなく、**実在するP11候補から経路に
  最も合うものを選ぶ**（座標は推定値でなく実在座標）。前後に確定座標が無く推定できない停留所は変更せず
  要確認のまま残す。実証: うるま市「城前郵便局前」を石垣島(444km)の誤候補から本島の正候補へ自動補正。
- **3.5c** `scripts/enrich_stops.py` — Nominatim（OSM）フォールバック。bbox + 県/市町村
  フィルタで誤マッチを排除（補完率は低めだが「正しく失敗」する）。
- **3.5d** `scripts/apply_manual_coords.py` — 手動座標オーバーライド（最優先）。
- **3.5d2** `scripts/reject_geom_outliers.py` — **経路ジオメトリ外れ値の棄却（既定ON）**。
  座標が付いていても便の前後停留所(経路)から大きく外れる停留所（南北に長い自治体のbbox内での
  同名別地点への誤マッチ等）を棄却し、後段の内挿/手動に回す。公式データと比較しないと見えない
  誤りを自動検出する（例: 築城巡回線の八津田が約10km、京築恵みの郷が約8km離れた同名にヒット）。
- **3.5e** `scripts/interpolate_coords.py` — 経路内挿で「推定座標（要確認）」を補完（既定OFF・opt-in）。

なお `enrich_stops_p11.py` の P11 照合は **自治体bboxに経路余裕(corridor margin)** を持たせて
自治体跨ぎの停留所も拾い、**異体字・小書きカナ（祗/祇・ケ/ヶ）を正規化**して取りこぼしを防ぐ。

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
- config に `extract_json`（blocks/cells形式の抽出JSON）を指定すると **Step 7c 内部整合検証**
  （`verify_stop_times_vs_extract.py --strict`）も自動実行し、抽出時刻が stop_times まで
  保たれているかを照合する（不一致はFAIL・`output_dir/stoptimes_verify.{md,json}` に出力）
- **Step 4b 検証用マップ**（`make_map_view.py`）を既定で自動生成し、`output_dir/map_view.html`
  を出力する（座標の範囲外・未補完を色分け、便選択で停車順を強調）。`map_view: false` で抑止
- **Step 3.6 祝日・運休日の展開**（`generate_calendar_dates.py`）：config に `holiday_syukujitsu`
  （内閣府祝日CSVのパス・祝日運休のとき）／`holiday_nenmatsu`（例 `"12-29:01-03"`）／`holiday_obon`
  （例 `"08-13:08-15"`）のいずれかを指定すると、有効期間内の運休日を `calendar_dates.txt` に
  `exception_type=2` で自動展開する。**祝日は計算せず公式CSVを一次データ**にし、年末年始/お盆は
  市ごとに異なるため**範囲指定時のみ**展開。未指定ならスキップ（運休日を付けない＝要確認）
- `translations_en_json` を指定すると Step 6 で英訳を自動マージ
- `--dry-run` で実行計画のプレビュー可能

### 精度評価（任意）

- `scripts/eval_compare.py` — 公式フィードとの集合比較（routes/stops/stop_times）
- `scripts/analyze_stop_times_diff.py` — trip 単位対応付けで時刻の真の精度を測定（公式フィードと比較）
- `scripts/verify_stop_times_vs_extract.py` — **内部整合性検証**：Step1 座標抽出JSON と
  生成 stop_times.txt の時刻を便ごとに照合し、Step2(LLM)/Step3(生成) で抽出時刻が改変・
  取りこぼされていないかを検出する。公式フィード不要・版差の影響を受けない（自己完結）。
  「PDF に忠実に抽出した時刻が最終成果物まで保たれている」ことの保証になる。
  例: `python scripts/verify_stop_times_vs_extract.py extract.json --gtfs gtfs/ -o verify.md`

## 必要環境

- Python 3.10以上
- Java 17以上のJRE（Step 7 GTFS Validator v8 系の実行に必要）
- インターネット接続（Step 4 OSRM API・Step 3.5c Nominatim・MinerU初回モデルDL用）
- 推奨実行環境: Cowork mode（Claude desktop app）

### ステップ別の追加要件

- **Step 1 Excel**（`extract_timetable_excel.py`）: `pip install openpyxl`
- **Step 1 座標方式**（`extract_timetable_coords.py`、テキストPDF用）: `pip install pdfplumber`
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
