# gtfs-jp-creator

**LLMを用いた GTFS-JP データ自動作成向け Claude プラグイン（Skill同梱）**

A Claude Code / Cowork mode plugin containing a Skill that helps non-experts (bus operators, municipal staff, citizens, researchers) create [GTFS-JP](https://www.mlit.go.jp/sogoseisaku/transport/sosei_transport_tk_000054.html) (Japan public transit data standard, v4.0) data from non-machine-readable sources such as bus timetable PDFs, photographed paper schedules, Excel spreadsheets, or textual route descriptions.

## 状態

**動作する実装段階（中間発表に向けた開発フェーズ）**
Step 1〜7 の全スクリプトが実装済み。古賀市・須恵町の 2 自治体の実データで
PDF → GTFS-JP 生成を検証し、いずれも MobilityData GTFS Validator で
エラー 0 件を達成している。条件確認画面・GTFS-JP 拡張ファイル生成
（agency_jp / office_jp）・JP 拡張検証にも対応済み。

## 構成（プラグイン形式）

```
gtfs-jp-creator/                       # プラグイン＆マーケットプレイスルート（Gitリポルート）
├── .claude-plugin/
│   ├── plugin.json                    # プラグインマニフェスト
│   └── marketplace.json               # マーケットプレイスカタログ（単一プラグイン）
├── skills/
│   └── gtfs-jp-creator/               # Skill本体
│       ├── SKILL.md                   # frontmatter + ガイド
│       ├── references/                # Skillが参照する仕様・例・プロンプト
│       │   ├── gtfs-jp-spec-v4.0-summary.md
│       │   ├── field-definitions/     # 各テーブル仕様
│       │   ├── examples/              # 正解GTFS-JPサンプル
│       │   └── prompts/               # LLM用プロンプト集
│       ├── scripts/                   # 実行スクリプト群
│       │   ├── pdf_to_markdown.py             # Step 1:   PDF→Markdown
│       │   ├── condition_summary.py           # 条件確認:  要入力項目のサマリ生成
│       │   ├── generate_gtfs_files.py         # Step 3:   JSON→GTFS-JP CSV
│       │   ├── merge_stop_coords.py           # Step 3.5a: 旧フィードから座標再利用
│       │   ├── enrich_stops_p11.py            # Step 3.5b: 国土数値情報 P11 で座標補完
│       │   ├── enrich_stops.py                # Step 3.5c: Nominatim で座標補完
│       │   ├── canonicalize_stops.py          # Step 3.x: 停留所名の表記揺れ正規化
│       │   ├── generate_shapes.py             # Step 4:   shapes.txt生成 (OSRM /route)
│       │   ├── generate_translations.py       # Step 6:   translations.txt生成 (3言語)
│       │   ├── package_gtfs_zip.py            # Step 5:   zipパッケージング
│       │   ├── validate_gtfs.py               # Step 7:   GTFS Validator実行
│       │   ├── validate_gtfs_jp_extensions.py # Step 7b:  GTFS-JP拡張の独自検証
│       │   ├── run_pipeline.py                # ワンコマンド: Step 3〜7 を一括実行
│       │   ├── eval_compare.py                # 精度評価: 公式フィードとの集合比較
│       │   └── analyze_stop_times_diff.py     # 精度評価: trip単位の時刻 diff
│       └── tools/                     # （任意配置）gtfs-validator-cli.jar など
├── README.md                          # このファイル
├── LICENSE                            # Apache License 2.0
├── .gitignore
└── .gitattributes                     # 改行コード方針
```

## インストール方法

### 方法1: マーケットプレイス経由（推奨）

このリポジトリはプラグインだけでなく **シングルプラグイン・マーケットプレイス** も兼ねています。Claude Code CLI または Cowork mode で以下を実行：

```shell
/plugin marketplace add k23rs125/gtfs-jp-creator
/plugin install gtfs-jp-creator@gtfs-jp
```

その後、`/reload-plugins` で読み込み完了。Skillはユーザー発話に応じて自動起動します。

### 方法2: ローカル開発テスト（Claude Code CLI 用）

リポジトリをクローンしたディレクトリで：

```bash
claude --plugin-dir ./gtfs-jp-creator
```

### アンインストール

```shell
/plugin uninstall gtfs-jp-creator@gtfs-jp
/plugin marketplace remove gtfs-jp
```

## 全体ワークフロー

時刻表 PDF を入力すると、以下の一連の処理を経て GTFS-JP データ一式（zip）が
完成する。LLM を使うのは Step 2 のみで、それ以外は確定的なスクリプト処理である。

```
[Step 1]    PDF/画像        →  Markdown 抽出（pymupdf4llm / MinerU）
[Step 2]    Markdown        →  構造化中間表現 JSON（LLM）
[条件確認]   JSON            →  PDF外の事業者情報など要入力項目をユーザーが補完
[Step 3]    JSON            →  GTFS-JP の各CSVファイル（JP拡張 agency_jp/office_jp 含む）
[Step 3.5]  stops.txt       →  緯度経度補完（旧フィード → 国土数値情報P11 → Nominatim の3段階）
[Step 3.x]  stops.txt       →  停留所名の表記揺れ正規化
[Step 4]    stop_times      →  shapes.txt 生成（OSRM routing /route）
[Step 6]    stops/routes    →  translations.txt 生成（ja / ja-Hrkt / en の3言語）
[Step 5]    全ファイル群     →  zip パッケージング
[Step 7]    zip             →  バリデーション（MobilityData GTFS Validator）
[Step 7b]   GTFS-JP         →  GTFS-JP 拡張部の独自検証（agency_jp/office_jp 等）
```

**Step 1（PDF抽出）と Step 2（LLM）を済ませれば、続く Step 3〜7b は
`run_pipeline.py` に config を1枚渡すだけで一括実行できる**（条件確認の表示・
shapes 生成・検証まで自動）。詳細は下記「ワンコマンド実行ガイド」を参照。

実証状況: 古賀市・須恵町の2自治体の実データで本ワークフローを通し、
いずれも GTFS Validator エラー0件で GTFS-JP データを生成済み。

## Quick Start（PowerShell 編）

Windows ユーザー向けに、全 Step を通して動かす **最短経路** をまとめました。各 Step の詳細・トラブルシュート・選択肢の比較は、後続の各章を参照してください。

### 前提：環境セットアップ（最初の 1 回だけ）

```powershell
# リポジトリをクローン
git clone https://github.com/k23rs125/gtfs-jp-creator.git
cd gtfs-jp-creator

# Python パッケージ（PDF抽出 + ジオコーディングは標準ライブラリで完結）
pip install pymupdf pymupdf4llm
pip install -U "mineru[core]"   # 装飾的 PDF を扱う場合（~3GB のML モデル DL あり）
```

> 💡 **PowerShell の役割**：PowerShell は Python スクリプトを呼び出すためのターミナルです。実際の変換処理はすべて Python が決定的（deterministic）に行います。Mac/Linux なら同じスクリプトを Bash から呼び出せます。

### Step 1: PDF → Markdown

```powershell
# 装飾的レイアウトの日本のバス時刻表 PDF は MinerU 推奨（CPU で 30〜60 分、GPU で数分）
mineru -p "$HOME\Desktop\komyubasujikokuhyou.pdf" -o test_demo --lang japan

# 出力確認
Get-Content .\test_demo\komyubasujikokuhyou\hybrid_auto\komyubasujikokuhyou.md | Select-Object -First 30
```

詳細：[`## MinerU 利用ガイド（PowerShell 編）`](#mineru-利用ガイドpowershell-編)

### Step 2: Markdown → JSON（LLM が関わる唯一の Step）

ブラウザで [Claude](https://claude.ai) / [ChatGPT](https://chat.openai.com/) / [Gemini](https://gemini.google.com/) を開き、次の2つを連結して送信：

1. `skills/gtfs-jp-creator/references/prompts/02_structured_extraction.md` のプロンプト全文
2. Step 1 で生成された `komyubasujikokuhyou.md` の全文

応答の **JSON 部分のみ** を抽出してファイル保存：

```powershell
notepad test_demo\extracted.json
# LLM 応答の { ... } 部分をペースト → 保存
```

詳細：[`## 複数LLM対応ガイド`](#複数llm対応ガイドclaude--chatgpt--gemini)

### 条件確認: 要入力項目のサマリを確認

Step 2 の JSON から「条件確認サマリ」を生成し、PDF から取れない項目
（事業者の住所・正式名称など）を一覧で確認します。

```powershell
python skills\gtfs-jp-creator\scripts\condition_summary.py `
  test_demo\extracted.json
```

→ 各項目が 🟦自動検出 / 🟨自動補完 / 🟧要入力 に分類して表示されます。
🟧 の値は、JSON の `_meta.user_overrides` に `"table.field"` 形式
（例 `"agency_jp.agency_zip_number": "811-2192"`）で追記してから Step 3 に進むと、
生成時に自動で反映されます。対応テーブルは `agency` / `agency_jp` / `feed_info`。

### Step 3: JSON → CSV（決定的・LLM 不要）

```powershell
python skills\gtfs-jp-creator\scripts\generate_gtfs_files.py `
  test_demo\extracted.json `
  -o test_demo\gtfs_output
```

→ `test_demo\gtfs_output\` に GTFS-JP の各ファイル（`agency.txt`,
`agency_jp.txt`, `routes.txt`, `routes_jp.txt`, `stops.txt`, `trips.txt`,
`stop_times.txt`, `calendar.txt`, `feed_info.txt` ほか、PDF に運賃・運休日が
あれば `fare_*`・`calendar_dates.txt`、営業所情報があれば `office_jp.txt`）が
生成されます。

> 🔑 **なぜ LLM ではなく Python で CSV を作るのか**：LLM だと長い CSV の末尾省略や、8 ファイル相互参照（`trips.route_id` ↔ `routes.route_id` など）の整合性破綻が起きます。Python による決定的変換で、エンコーディング（UTF-8 with BOM + CRLF）も含めて GTFS 仕様準拠を保証します。詳細は `Markdown_JSON_設計説明書_v1.md` を参照。

### Step 3.5: 緯度経度補完

```powershell
python skills\gtfs-jp-creator\scripts\enrich_stops.py `
  test_demo\gtfs_output\stops.txt `
  --context "福岡県糟屋郡須恵町" `
  --agency-name "須恵町コミュニティバス" `
  --bbox "130.40,33.45,130.55,33.60"
```

→ `stops.enriched.txt`、`stops_geocache.json`、`enrichment_report.json` が生成されます。

詳細：[`## 緯度経度補完ガイド（Step 3.5）`](#緯度経度補完ガイドstep-35)

### Step 4: shapes.txt 生成（OSRM routing）

```powershell
python skills\gtfs-jp-creator\scripts\generate_shapes.py `
  test_demo\gtfs_output\stops.enriched.txt `
  test_demo\gtfs_output\stop_times.txt `
  test_demo\gtfs_output\trips.txt `
  -o test_demo\gtfs_output\shapes.txt `
  --update-trips test_demo\gtfs_output\trips.with_shapes.txt
```

→ `shapes.txt`、`shapes_cache.json`、`shapes_report.json` が生成されます。
詳細：[`## shapes.txt 生成ガイド（Step 4）`](#shapestxt-生成ガイドstep-4)

### Step 7: バリデーション

```powershell
# MobilityData GTFS Validator（Java 17 以上が必要）
python skills\gtfs-jp-creator\scripts\validate_gtfs.py `
  test_demo\gtfs_output_gtfs-jp.zip -o test_demo\validation

# GTFS-JP 拡張（agency_jp/office_jp 等）の独自検証（Java 不要）
python skills\gtfs-jp-creator\scripts\validate_gtfs_jp_extensions.py `
  test_demo\gtfs_output
```

→ `validate_gtfs.py` は ERROR / WARNING / INFO を集計表示、
`validate_gtfs_jp_extensions.py` は GTFS-JP 拡張部の必須カラム・参照整合性を
検証します。両者とも下記の `run_pipeline.py` に統合されており、ワンコマンド
実行（Step 7・7b）でまとめて走ります。

### コピペで丸ごと実行できる完全例（須恵町コミュニティバス）

```powershell
# 0. 準備
cd $HOME\Desktop\稲ゼミ\gtfs-jp-creator

# 1. PDF → Markdown
mineru -p "$HOME\Desktop\稲ゼミ\komyubasujikokuhyou.pdf" -o test_demo --lang japan

# 2. （手動）LLM に Markdown を投げて test_demo\extracted.json を作成

# 3. JSON → CSV
python skills\gtfs-jp-creator\scripts\generate_gtfs_files.py `
  test_demo\extracted.json -o test_demo\gtfs_output

# 3.5 緯度経度補完
python skills\gtfs-jp-creator\scripts\enrich_stops.py `
  test_demo\gtfs_output\stops.txt `
  --context "福岡県糟屋郡須恵町" `
  --agency-name "須恵町コミュニティバス" `
  --bbox "130.40,33.45,130.55,33.60"

# enrich_stops は <input>.enriched.txt を出すので、stops.txt に上書きするなら：
Copy-Item test_demo\gtfs_output\stops.enriched.txt test_demo\gtfs_output\stops.txt -Force

# 4. shapes.txt 生成（OSRM routing /route）
python skills\gtfs-jp-creator\scripts\generate_shapes.py `
  test_demo\gtfs_output\stops.txt `
  test_demo\gtfs_output\stop_times.txt `
  test_demo\gtfs_output\trips.txt `
  -o test_demo\gtfs_output\shapes.txt `
  --update-trips test_demo\gtfs_output\trips.with_shapes.txt

# 5. zip パッケージング + 検証は run_pipeline.py で一括実行（下記参照）
```

## ワンコマンド実行ガイド（run_pipeline.py）

Step 2（Markdown→JSON、LLM 利用）まで済んだら、**残りの Step 3〜7 は config JSON 1枚で全自動実行** できます。各 Step スクリプトを個別に叩く必要はありません。

### 何をやってくれるか

```
run_pipeline.py --config <config.json>
  ├ 条件確認   要入力サマリの表示             （情報提示・常時）
  ├ Step 3    JSON → CSV（agency_jp/office_jp 含む）
  ├ Step 3.5a 旧フィードから座標再利用      （reference_feed 指定時）
  ├ Step 3.5b 国土数値情報 P11 で座標補完    （p11_shapefile 指定時）
  ├ Step 3.5c Nominatim で座標補完          （use_nominatim=true 時）
  ├ Step 3.x  停留所名 canonicalize         （canonical_reference 指定時）
  ├ Step 4    shapes.txt 生成 (OSRM)
  ├ Step 6    translations.txt 生成
  ├ Step 5    zip パッケージング
  ├ Step 7    GTFS Validator 検証           （validate=true 時）
  └ Step 7b   GTFS-JP 拡張検証              （常時・Java 不要）
```

各 Step は config に該当オプションが無ければ **自動でスキップ**。stops.txt は Step 間で
自動的にチェーンされる（座標補完 → 正規化 → shapes 生成 まで一貫）。
**条件確認**は実行前に要入力項目を一覧表示するだけで、要入力があってもパイプラインは
止まらない。**Step 7b** は MobilityData Validator が見ない GTFS-JP 拡張部
（agency_jp/office_jp 等）を独自検証する（純 Python のため Java 不要・常時実行）。

### config JSON の書き方

```json
{
  "feed_name": "kogabus_20260601",
  "input_json": "test_demo/kogashi_claude.json",
  "output_dir": "test_demo/kogabus_pipeline",
  "context": "福岡県",
  "bbox": "130.42,33.67,130.52,33.76",
  "reference_feed": "C:/Users/User/Desktop/稲ゼミ/260211kogabus_gtfs-jp.zip",
  "p11_shapefile": "C:/Users/User/Desktop/稲ゼミ/p11_fukuoka/P11-22_40_SHP/P11-22_40.shp",
  "canonical_reference": "C:/Users/User/Desktop/稲ゼミ/Shin_kogashi.zip",
  "use_nominatim": false,
  "translations_en_json": "test_demo/kogashi_en.json",
  "validate": true
}
```

| キー | 必須 | 説明 |
|---|---|---|
| `feed_name` | ✅ | 出力 zip 名のプレフィックス |
| `input_json` | ✅ | Step 2 の出力（LLM が作った構造化JSON）|
| `output_dir` | ✅ | 成果物の出力先ディレクトリ |
| `context` | — | Nominatim 用コンテキスト（例: "福岡県"）|
| `bbox` | — | 座標補完の検索範囲 |
| `reference_feed` | — | 旧 GTFS-JP（Step 3.5a 用）。あれば座標 100% |
| `p11_shapefile` | — | 国土数値情報 P11（Step 3.5b 用）|
| `canonical_reference` | — | 表記揺れ正規化の参照フィード（Step 3.x 用）|
| `use_nominatim` | — | `true` で Step 3.5c を有効化（既定 false）|
| `translations_en_json` | — | LLM 英訳済み JSON。無ければ en プロンプトを export |
| `validate` | — | `true` で Step 7 GTFS Validator を実行 |

### 使い方

```powershell
cd C:\Users\User\Desktop\稲ゼミ\gtfs-jp-creator

# まず dry-run で実行計画を確認（実行はしない）
python skills\gtfs-jp-creator\scripts\run_pipeline.py `
  --config test_demo\kogabus_pipeline_config.json --dry-run

# 本実行
python skills\gtfs-jp-creator\scripts\run_pipeline.py `
  --config test_demo\kogabus_pipeline_config.json
```

### 出力

```
<output_dir>/
├── gtfs/                          ← GTFS-JP CSV 群（13ファイル）
├── work/                          ← 各 Step の中間ファイル・レポート
├── <feed_name>_gtfs-jp.zip        ← ★ 最終成果物
└── validation/                    ← GTFS Validator レポート（validate=true 時）
```

### 実行サマリ例

```
================================================================
パイプライン完了
================================================================
  ✓ Step 3 JSON→CSV              [OK]
  ✓ Step 3.5a 旧フィード座標         [OK]
  ✓ Step 3.5b P11補完              [OK]
  ・ Step 3.5c Nominatim補完        [SKIP]
  ✓ Step 3.x 停留所名正規化          [OK]
  ✓ Step 4 shapes生成              [OK]
  ✓ Step 6 translations生成        [OK]
  ✓ Step 5 zipパッケージ            [OK]
  ✓ Step 7 Validator検証           [OK]
================================================================
```

### オプション

| オプション | 説明 |
|---|---|
| `--dry-run` | 実行せず計画のみ表示 |
| `--stop-on-error` | Step 失敗時に即中断（既定は続行可能なら続行）|

## MinerU 利用ガイド（PowerShell 編）

Step 1（PDF→Markdown）で **MinerU** を使う場合の手順を、PowerShell でのコマンドベースで詳述します。pymupdf4llm（軽量エンジン）の場合は本セクションは飛ばして構いません。

### MinerU とは

[OpenDataLab](https://github.com/opendatalab/mineru) が開発した、CJK（中国語・日本語・韓国語）に特化した PDF→Markdown 変換ツール。バス時刻表のような **装飾的レイアウトの日本語PDF** に対して、pymupdf4llm より大幅に高い抽出品質を発揮します（須恵町PDFで実測：抽出成功率 30% → 95%）。

### インストール

PowerShell で以下を実行：

```powershell
# Python 3.10 以上が必要（事前に python --version で確認）
python --version
```

```powershell
# MinerU の core 版をインストール（ML モデル + Web UI 含むフル版）
pip install -U "mineru[core]"
```

> 💡 初回インストールは **約 700MB のパッケージダウンロード**＋ **約 1〜1.5GB のディスク使用** となります。完了まで数分〜10分程度。

### インストール確認

```powershell
python -c "import mineru; print('mineru imported OK')"
```

```powershell
mineru --version
```

→ `mineru, version 3.x.x` のような表示が出れば成功。

> ⚠️ Windows で `mineru` コマンドが見つからない場合は、代わりに `python -m mineru` で実行してください（[既知のWindows互換性問題](https://github.com/opendatalab/MinerU/issues/4433)）。

### 基本的な使い方

```powershell
mineru -p "<入力PDFのフルパス>" -o "<出力ディレクトリ名>" --lang japan
```

#### 実例：須恵町コミュニティバス時刻表PDFを変換

```powershell
cd $HOME\Desktop\稲ゼミ\gtfs-jp-creator
```

```powershell
mineru -p "$HOME\Desktop\稲ゼミ\komyubasujikokuhyou.pdf" -o test_demo --lang japan
```

### 処理の流れ（実測）

初回実行では以下のステップが進行します：

| ステップ | 内容 | 所要時間（CPU） |
|---|---|---|
| 1 | MLモデルダウンロード（初回のみ、約3GB） | 約 7 分 |
| 2 | VLM推論モデルのロード | 約 7 分 |
| 3 | レイアウト解析（ページ単位） | 約 8 分 / 2ページ |
| 4 | コンテンツ抽出（VLMによるOCR込み） | 約 35 分 / 2ページ |
| 5 | 後処理＋OCR補完 | 約 3 分 |
| **合計** | （初回・CPU推論） | **約 50〜60 分** |
| **2回目以降** | モデルキャッシュ済 | **約 40〜45 分** |

> 💡 GPU環境（CUDA対応）では **数分** に短縮されます。実用的には GPU 推奨。

### 出力ファイル構造

`-o` で指定したディレクトリに、以下のような階層で出力されます：

```
test_demo/
└── komyubasujikokuhyou/
    └── hybrid_auto/
        ├── komyubasujikokuhyou.md           ← ★メインの抽出 Markdown
        ├── komyubasujikokuhyou_content_list.json
        ├── komyubasujikokuhyou_content_list_v2.json
        ├── komyubasujikokuhyou_layout.pdf   ← レイアウト検出可視化PDF
        ├── komyubasujikokuhyou_middle.json
        ├── komyubasujikokuhyou_model.json
        ├── komyubasujikokuhyou_origin.pdf   ← 元PDFのコピー
        └── images/                          ← 切り出された画像群
            ├── (各種 .jpg ファイル)
```

抽出結果の Markdown を確認：

```powershell
Get-Content .\test_demo\komyubasujikokuhyou\hybrid_auto\komyubasujikokuhyou.md | Select-Object -First 30
```

### トラブルシュート

| 症状 | 原因 | 対処 |
|---|---|---|
| `mineru: コマンドが見つかりません` | Windows のPATH問題 | `python -m mineru ...` で代替実行 |
| `--lang japan#` 等のエラー | PowerShellでの全角空白混入 | コマンド全体を再入力（コピペ時に注意） |
| 処理がフリーズして見える | CPU推論はもともと遅い | `tail -f` 不可だが、進捗バーが進んでいれば正常 |
| メモリエラー | 8GB未満のRAMで重いVLM | `--backend pipeline` でCPU軽量モードへ切り替え |
| GPU環境を使いたい | CUDA対応PyTorchが必要 | `pip install torch --index-url https://download.pytorch.org/whl/cu121` |

### 高速化のヒント

1. **GPU を使う**: NVIDIA GPU + CUDA で 10倍以上高速化
2. **ページ範囲を絞る**: 大きなPDFは `--start-page-id 0 --end-page-id 5` で最初の5ページのみ処理（要環境変数）
3. **既に処理済みのMDを使い回す**: 一度処理したPDFは再処理せず、既存の `.md` ファイルを使う（本研究の `test_mineru_output/` 等）

### なぜ pymupdf4llm ではなく MinerU を使うのか

本研究では **2エンジン併用** 方式を採用しています：

| エンジン | 速度 | 装飾PDF品質 | 用途 |
|---|---|---|---|
| pymupdf4llm | 高速（数秒） | 低（30%） | テキスト埋め込み済みのシンプルなPDF |
| **MinerU** | 遅い（CPUで30〜60分） | 高（95%） | **装飾的・並列レイアウトの日本のバス時刻表PDF** |

実証データ（須恵町コミュニティバス時刻表PDFで検証）：
- pymupdf4llm: 並列レイアウトの 7路線分テーブルを正しく分離できず、時刻末尾切れ多数
- MinerU: 全7路線分のテーブルを完璧に分離抽出、時刻完全保持

→ **日本の自治体バス時刻表PDFのほとんどは装飾的レイアウト**のため、MinerU が事実上の標準選択肢になります。

## P11 統合ガイド（Step 3.5b・国土数値情報）

国土交通省「**国土数値情報 P11 バス停留所**」データを使って停留所座標を補完するスクリプト `enrich_stops_p11.py` を提供します。Nominatim（OSM）よりカバレッジが圧倒的に高く、コミュニティバス停も網羅されています。

### Step 3.5 の3段階階層

```
[3.5a] 旧 GTFS-JP フィード再利用  (merge_stop_coords.py)    旧フィードあり→100%
   ↓ そこで補完できなかった stop だけが次に進む
[3.5b] 国土数値情報 P11 から補完  (enrich_stops_p11.py)    全自治体→90%+ 期待 ← 本章
   ↓ そこでも補完できなかった stop だけが次に進む
[3.5c] Nominatim（OSM）で補完    (enrich_stops.py)         残りの安全網
```

### 設計概要

| 項目 | 内容 |
|---|---|
| データソース | 国土交通省 国土数値情報 P11（公的・全国網羅・年次更新）|
| 入手 | https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P11.html から都道府県別 Shapefile |
| ライセンス | 国土数値情報利用約款（出典明記で自由利用可）|
| 依存 | `pip install pyshp`（pure Python・軽量）|
| マッチング | 4段階：完全一致 → 前方/後方一致 → 部分一致 → fuzzy (difflib) |
| ネットワーク | 不要（ローカル処理のみ）|

詳細は `国土数値情報P11統合設計書_v1.md` を参照。

### データ入手手順（一度だけ）

```powershell
# 1. ブラウザで以下を開く
#    https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P11.html
# 2. 「P11 バス停留所」最新版（v2.x）の対象都道府県を選択
#    例: 福岡県 → P11-2024_40_GML.zip
# 3. 任意のフォルダに展開（例: C:\Users\User\Desktop\稲ゼミ\p11_fukuoka\）
# 4. 展開された .shp の絶対パスを控える

# pyshp 依存をインストール
pip install pyshp
```

### 基本的な使い方

```powershell
python skills\gtfs-jp-creator\scripts\enrich_stops_p11.py `
  test_demo\kogashi_gtfs_output\stops.txt `
  --p11 "$HOME\Desktop\稲ゼミ\p11_fukuoka\P11-24_40-jgd_BusStop.shp" `
  --bbox "130.42,33.67,130.52,33.76" `
  -o test_demo\kogashi_gtfs_output\stops.p11.txt
```

### 出力

| ファイル | 説明 |
|---|---|
| `stops.p11.txt` | P11 で補完済み stops.txt |
| `p11_enrichment_report.json` | マッチ詳細（exact/prefix/substring/fuzzy 内訳）|

### Step 3.5 の組み合わせ運用（古賀市の例）

旧フィードと P11 と Nominatim を順に重ねる完全版：

```powershell
# 1. 旧フィードから再利用（あれば最強）
python skills\gtfs-jp-creator\scripts\merge_stop_coords.py `
  test_demo\kogashi_gtfs_output\stops.txt `
  --reference "$HOME\Desktop\稲ゼミ\260211kogabus_gtfs-jp.zip" `
  -o test_demo\kogashi_gtfs_output\stops.merged.txt

# 2. 旧フィードでカバーできなかった stop を P11 で補完
python skills\gtfs-jp-creator\scripts\enrich_stops_p11.py `
  test_demo\kogashi_gtfs_output\stops.merged.txt `
  --p11 "$HOME\Desktop\稲ゼミ\p11_fukuoka\P11-24_40-jgd_BusStop.shp" `
  --bbox "130.42,33.67,130.52,33.76" `
  -o test_demo\kogashi_gtfs_output\stops.p11.txt

# 3. それでも残ったものを Nominatim で（必要な場合のみ）
python skills\gtfs-jp-creator\scripts\enrich_stops.py `
  test_demo\kogashi_gtfs_output\stops.p11.txt `
  --context "福岡県" `
  --bbox "130.42,33.67,130.52,33.76" `
  -o test_demo\kogashi_gtfs_output\stops.final.txt
```

→ 最終 `stops.final.txt` を Step 4 (shapes生成) と Step 5 (zip化) で使用。

### 想定精度

| 自治体 | 旧フィード | + P11 | + Nominatim | 合計 |
|---|---|---|---|---|
| 須恵町（旧フィードなし）| 0% | 85-95% | +0-5% | **90%+** |
| 古賀市（旧フィードあり）| 100% | — | — | 100% |
| 新規自治体（旧フィードなし）| 0% | 80-95% | +5-10% | **90%+** |

### 失敗時の対処

| 症状 | 対処 |
|---|---|
| `pyshp が必要です` | `pip install pyshp` |
| `P11 名前フィールドが見つかりません` | Shapefile のバージョン違い。 `--p11` のパスとフィールド名を確認 |
| カバレッジが低い | bbox を広げる、fuzzy threshold を下げる（`--fuzzy-threshold 0.7`）|
| マッチが緩すぎる | fuzzy threshold を上げる（`--fuzzy-threshold 0.9`）|

## 緯度経度補完ガイド（Step 3.5）

PDF からは取得できない `stop_lat`/`stop_lon` を外部APIで補完するスクリプト `enrich_stops.py` を提供します。OSRM による shapes.txt 生成（Step 4）の前提条件です。

### 設計概要

| 項目 | 内容 |
|---|---|
| 一次バックエンド | Nominatim (OpenStreetMap) |
| クエリ戦略 | 4段階（住所付き → 事業者名付き → 県市レベル → 停留所名のみ） |
| レート制限 | 1.1 sec/req（Nominatim 公式ポリシー遵守） |
| キャッシュ | JSON ファイル（再実行時はキャッシュ優先） |
| エンコーディング | UTF-8 with BOM + CRLF（GTFS仕様） |

詳細は `緯度経度補完設計書_v1.md` を参照。

### 基本的な使い方（v0.1.1 推奨パラメータ込み）

```powershell
python skills\gtfs-jp-creator\scripts\enrich_stops.py `
  test_demo\gtfs_output\stops.txt `
  -o test_demo\gtfs_output\stops.enriched.txt `
  --context "福岡県糟屋郡須恵町" `
  --agency-name "須恵町コミュニティバス" `
  --bbox "130.40,33.45,130.55,33.60" `
  --cache test_demo\stops_geocache.json `
  --report test_demo\enrichment_report.json
```

#### `--bbox` を必ず付ける理由（重要）

`--bbox` を **付けない** と、Nominatim は `countrycodes=jp` だけが効き、日本全国の同名地名を返します。たとえば「須恵中学校」と検索すると **大阪府の同名校** にヒットし、誤った座標を持ってきてしまいます。

`--bbox lon_min,lat_min,lon_max,lat_max` を付けると Nominatim の `viewbox + bounded=1` が効き、**範囲内の結果しか返らなくなります**。

bbox の値は対象自治体を含む緯度経度の最小箱を指定。`https://nominatim.openstreetmap.org/` で対象町を検索し、`Bounding Box` の値をコピーするのが手早いです。例：

| 自治体 | bbox (lon_min,lat_min,lon_max,lat_max) |
|---|---|
| 福岡県 須恵町 | `130.40,33.45,130.55,33.60` |
| 福岡県 柳川市 | `130.30,33.10,130.50,33.30` |

#### `--prefecture` のフォールバック（二重防止）

bbox に加えて、結果の `address.state` を「福岡県」と一致するかチェックします。`--context` に「福岡県…」と書いてあれば **自動推定** されるので、通常は明示不要です。明示する場合：

```powershell
--prefecture "福岡県"
```

#### `--municipality` で隣町誤マッチを防ぐ（v0.1.2 推奨）

bbox + prefecture だけでは **隣町の同名施設** が紛れ込みます。例：

- 「福祉センター」検索 → 須恵町の隣の **宇美町立老人福祉センター** にヒット（福岡県内 + bbox内ではあるが、別の町）
- 「金堀公園前」検索 → **福岡市中央区の駐輪場** にヒット（OSMが「金堀公園前」と関連付けてしまった謎の結果）

これを防ぐため、`--municipality "須恵町"` を指定すると `address.city/town/village` を「須恵町」と一致するかチェックします。`--context "福岡県糟屋郡須恵町"` のように市町村名まで書いていれば **自動推定** されるので明示不要。

### 出力

- `stops.enriched.txt`: `stop_lat`/`stop_lon` が埋まった enriched CSV
- `stops_geocache.json`: キャッシュ（再実行時に活用）
- `enrichment_report.json`: 補完成功・失敗の統計と失敗詳細

### サマリ出力（実行終了時）

```
================================================================
ENRICHMENT REPORT
================================================================
Total stops:           87
Already had coords:     0
Newly enriched:        76 (87.4%)
Failed:                11 (12.6%)
Cache hits:             0
API calls made:       304
Total time:           285 sec
================================================================
```

### デバッグ用：先頭N件だけ試す

```powershell
python skills\gtfs-jp-creator\scripts\enrich_stops.py `
  test_demo\gtfs_output\stops.txt --limit 5 --context "福岡県須恵町"
```

→ 5件だけ補完して動作確認。本番前に 1〜2 分でテスト可能。

### 失敗時の対処

- **すべての停留所が `not_found`**: `--bbox` が狭すぎる or 対象地域から外れている可能性。bbox を広げるか、削除して `--prefecture` のみで試す。
- **県外の場所にヒットしてしまう（v0.1.0 までのバグ）**: v0.1.1 で `--bbox` + `--prefecture` 検証を追加して解消。`--bbox` を必ず付けるべし。
- **特定の停留所だけ失敗**: 漢字表記揺れや略称が原因。手動で stops.txt を編集 or Phase 1.2 で予定の国土数値情報 P11 ベース実装を待つ。
- **HTTP 429**: レート違反。`--rate 2.0` で間隔を広げる。
- **キャッシュに古い不正データがある**: `--force-refresh` で再取得 or `stops_geocache.json` を削除。

### Phase 1.x ロードマップ

| Phase | 内容 |
|---|---|
| v0.1（本実装） | Nominatim 単一バックエンド、4戦略クエリ |
| v0.2 | 国土地理院 AddressSearch をフォールバック追加 |
| v0.3 | 国土数値情報 P11（オフライン・高精度・全国網羅） |
| v1.0 | LLM ベースの曖昧表記正規化 |

## shapes.txt 生成ガイド（Step 4）

PDF にない **実走行経路（lat/lon 点列）** を OSRM の routing API（/route）で自動生成するスクリプト `generate_shapes.py` を提供します。Step 3.5 で補完した stops.txt を入力とし、`shapes.txt` を出力します。

### 設計概要

| 項目 | 内容 |
|---|---|
| 主バックエンド | OSRM 公開デモサーバー (`https://router.project-osrm.org/route/v1/driving/`) |
| フォールバック | OSRM 失敗時は停留所間を直線で結ぶ簡易 shape |
| パターンユニーク化 | 同じ stop_id 列を通る trip は同じ shape を共有（API 呼び出し削減）|
| 累積距離 | Haversine 公式で `shape_dist_traveled` を自動計算 |
| キャッシュ | パターン単位の JSON キャッシュで再実行を高速化 |
| エンコーディング | UTF-8 with BOM + CRLF（GTFS仕様） |

詳細は `shapes生成設計書_v1.md` を参照。

### 基本的な使い方

```powershell
python skills\gtfs-jp-creator\scripts\generate_shapes.py `
  test_demo\gtfs_output\stops.txt `
  test_demo\gtfs_output\stop_times.txt `
  test_demo\gtfs_output\trips.txt `
  -o test_demo\gtfs_output\shapes.txt `
  --update-trips test_demo\gtfs_output\trips.with_shapes.txt
```

### 出力ファイル

| ファイル | 説明 |
|---|---|
| `shapes.txt` | GTFS 標準の shapes ファイル（`shape_id, lat, lon, sequence, dist_traveled`）|
| `trips.with_shapes.txt` | `shape_id` カラムが付与された trips（`--update-trips` 指定時のみ）|
| `shapes_cache.json` | パターンキャッシュ（次回実行で再利用）|
| `shapes_report.json` | 統計レポート |

### サマリ出力例

```
================================================================
SHAPES REPORT
================================================================
Total trips:              42
Unique patterns:          8
Trips with shape_id:      42
Trips skipped (座標不足):  0
OSRM success:             5
OSRM failed → fallback:   3
Cache hits:               0
API calls:                8
Elapsed:                  12.5 sec
================================================================
```

### OSRM を使わない（オフライン直線のみ）

OSRM 公開サーバーが落ちている / 商用利用で外部 API を避けたい場合：

```powershell
python skills\gtfs-jp-creator\scripts\generate_shapes.py `
  ... --no-osrm
```

→ すべての shape を停留所間の直線で生成。精度は落ちるが GTFS としては有効。

### 座標が欠落している stops への対処

Step 3.5 で補完できなかった停留所は、shape 生成時にスキップされます：

- `stops.txt` に座標がある stop は OSRM / 直線フォールバックで利用
- 座標欠落の stop はスキップ（位置不明として扱う）
- 残った座標が 2 点未満になった trip は **shape をスキップ**（レポートに件数を記録）

→ shape の品質は **Step 3.5 の補完率に直結**。Phase 1.2（国土数値情報 P11）で補完率が上がれば、shapes.txt の完成度も上がる依存関係。

### 自前 OSRM サーバーを使う

公開デモサーバーは個人利用向けで、商用・大規模利用には自前で OSRM を立てる必要があります：

```powershell
python skills\gtfs-jp-creator\scripts\generate_shapes.py `
  ... --osrm-url "https://my-osrm.example.com/route/v1/driving"
```

### 失敗時の対処

| 症状 | 対処 |
|---|---|
| `Error: 座標を持つ stops が0件です` | Step 3.5（enrich_stops.py）を先に実行 |
| OSRM が `code: "NoMatch"` を返す | 直線フォールバックされる。問題なし |
| HTTP 429 Too Many Requests | `--rate 2.0` で間隔を広げる |
| shapes.txt が空 | trips に `shape_id` が振られないので地図表示できない。Step 3.5 改善が必要 |

### Phase 1.x ロードマップ

| Phase | 内容 |
|---|---|
| v0.1（本実装）| OSRM デモサーバー + 直線フォールバック + パターン共有 |
| v0.2 | 自前 OSRM サーバー対応の検証、半径パラメータの調整 |
| v0.3 | OSM 道路網がない地域での警告と代替戦略 |

## 複数LLM対応ガイド（Claude / ChatGPT / Gemini）

本Skillの **コア処理（Pythonスクリプト＋プロンプトテキスト）は LLM 非依存** です。LLM が必要なのは Step 2（Markdown→JSON 抽出）のみで、Claude / ChatGPT / Gemini を自由に差し替え可能です。

### LLMが関わる箇所

```
[Step 1] PDF        →  Markdown        ❌ LLM不要（pymupdf4llm/MinerU）
[Step 2] Markdown   →  JSON            ✅ LLM必要 ← ここを差し替え
[Step 3] JSON       →  CSV ×8          ❌ LLM不要（決定的Pythonスクリプト）
[Step 4] stop_times →  shapes.txt       ❌ LLM不要（OSRM API）
[Step 5] CSV ×8     →  Validation       ❌ LLM不要（GTFS Validator）
```

→ **Step 2 だけ** が LLM 依存。他はすべて決定的処理。

### 利用形態の比較

| LLM | 利用形態 | プロンプト投入 | 結果保存 | Skill自動連動 |
|---|---|---|---|---|
| **Claude（Cowork mode）** | Skillとして組込済 | 自動 | 自動 | ✅ |
| **Claude（API）** | スクリプト経由（Phase 1.x で対応予定） | 自動 | 自動 | ✅ |
| **ChatGPT（Web UI）** | 手動コピペ | 手動 | 手動 | ❌ |
| **Gemini（Web UI）** | 手動コピペ | 手動 | 手動 | ❌ |

### Claude（Cowork mode）での利用

Claudeのデスクトップ版（Cowork mode）にプラグインを導入すると、ユーザー発話を Skill が自動検知して、Step 1〜5 を一気通貫で実行します。

```
[ユーザー]
  バス時刻表のPDFをGTFS-JPデータにしたい
  C:\Users\...\komyubasujikokuhyou.pdf

[Claude]
  GTFS-JP Creator Skill を起動します。
  Step 1〜3 を実行して条件確認サマリを出します...
```

詳細は `画面操作フロー設計_v2.md` を参照。

### ChatGPT / Gemini での利用（手動操作モード）

#### 前提

```powershell
git clone https://github.com/k23rs125/gtfs-jp-creator.git
cd gtfs-jp-creator
pip install -U "mineru[core]" pymupdf pymupdf4llm
```

#### Step 1: PDF → Markdown（LLM不要）

```powershell
mineru -p "$HOME\Desktop\komyubasujikokuhyou.pdf" -o test_demo --lang japan
```

→ `test_demo\komyubasujikokuhyou\hybrid_auto\komyubasujikokuhyou.md` が生成される。

#### Step 2: Markdown → JSON（ここで LLM を選択）

ブラウザで [ChatGPT](https://chat.openai.com/) または [Gemini](https://gemini.google.com/) を開き、次の2つを連結して送信：

**(A) プロンプト本文** — リポジトリ内のこのファイル全文をコピー：

```
skills/gtfs-jp-creator/references/prompts/02_structured_extraction.md
```

**(B) Step 1 で生成された Markdown 全文** をプロンプト末尾に貼り付け。

LLM が JSON 形式で応答するので、`{` で始まり `}` で終わる JSON ブロック部分のみを抽出して保存：

```powershell
notepad test_demo\extracted.json
# JSON部分をペースト → 保存
```

> ⚠️ ChatGPT / Gemini は応答冒頭に説明文や ` ```json ... ``` ` のコードブロック囲みを付ける場合があるので、純粋な JSON 部分だけを取り出してください。

#### Step 3: JSON → CSV（LLM不要）

```powershell
python skills\gtfs-jp-creator\scripts\generate_gtfs_files.py test_demo\extracted.json -o test_demo\gtfs_output
```

→ `test_demo\gtfs_output\` に `agency.txt`, `routes.txt`, `stops.txt`, `stop_times.txt`, `calendar.txt` 等が生成される。

#### Step 4 以降（shapes 生成・検証・パッケージング）

`generate_shapes.py`・`validate_gtfs.py` を含む Step 4〜7 はすべて実装済み。
個別実行も可能だが、`run_pipeline.py` で Step 3〜7b を config 1 枚から
一括実行するのが簡単（[ワンコマンド実行ガイド](#ワンコマンド実行ガイドrun_pipelinepy) 参照）。

### LLM 切替の影響範囲

「どの LLM を使うか」で変わるのは **Step 2 の出力品質** のみ。CSV のフォーマット、相互参照の整合性、Validatorの結果は **どの LLM を使っても同じ Python ロジックで処理されるため均質** です。

ベースライン実験での確認：
- Gemini 単体 → JSON 直接生成は不可（CSV を要求すると拒否）
- Gemini + 本Skillのプロンプト + 本研究 Python = ✅ 動作（手動操作モード）

→ 「LLM の差」は意味抽出の精度差として現れるが、**フォーマット差・整合性差にはならない**。これは本研究の設計（Markdown / JSON 中間表現）の利点。

詳細な設計理由は `Markdown_JSON_設計説明書_v1.md` を参照。

### LLM 別のメリット・デメリット（実証ベース）

| LLM | メリット | デメリット |
|---|---|---|
| **Claude** | Skill自動起動、プロンプト管理不要、対話型UI | 個別プラン契約 |
| **ChatGPT** | 普及度高、無料プランあり | 手動操作、Skill連動なし |
| **Gemini** | 無料、Google AI Studio統合 | 手動操作、JSON出力時に説明文混入の傾向 |

### Phase 1.x ロードマップ（API モード対応）

将来的には、各 LLM の API を呼び出す Python スクリプトを追加し、ChatGPT/Gemini でも自動連動させる予定：

```
scripts/llm_extract.py
  --provider {claude,openai,gemini}
  --input  test_demo/extracted.md
  --output test_demo/extracted.json
```

→ これが入れば、コマンド1本で Step 1-5 を完走できる。

## 精度評価ガイド（trip-aligned diff）

生成した GTFS-JP を公式フィードと比較して **stop_times の真の精度** を測る `analyze_stop_times_diff.py` を提供します。

### なぜ専用ツールが必要か

`eval_compare.py` は (停留所名, 時刻) ペアの **集合比較** を行いますが、停留所名の表記揺れや時刻フォーマットの差で精度が **過小評価** されます（古賀市実証：集合比較 71.9% に対し、本ツールでは 99.7%）。

`analyze_stop_times_diff.py` は **trip（便）単位で対応付け** て比較します：

1. 各 trip の「最初の停留所の (名前, 時刻)」をシグネチャとして1対1対応付け
2. 対応した trip ごとに stop_sequence の各行で時刻・停留所名を比較
3. 真の時刻誤差／名前不一致／余分・欠落を分けて集計

### 使い方

```powershell
python skills\gtfs-jp-creator\scripts\analyze_stop_times_diff.py `
  --official "C:\path\to\official_gtfs.zip" `
  --ours test_demo\kogashi_gtfs_v3 `
  -o test_demo\trip_aligned_report.md `
  --json test_demo\trip_aligned_report.json
```

### 出力例（古賀市実証）

```
================================================================
TRIP-ALIGNED STOP_TIMES DIFF
================================================================
  公式 trips:                36
  当方 trips:                36
  マッチした trip ペア:       36
  比較した行数:               342
  時刻一致:                  341 / 342 = 99.71%
  時刻不一致:                 1
  停留所名不一致:             0
================================================================
```

→ 集合比較 71.9% は測定手法の弱点による錯覚で、**真の Step 2（LLM）精度は 99.7%** であることが定量化できます。

## 必要環境

- Python 3.10以上
- Java 11以上のJRE（GTFS Validator実行用）
- インターネット接続（OSRM routing API利用時、MinerU初回モデルDL用）
- 推奨: Cowork mode（Claude desktop app）

### Step 1 エンジン別の追加要件

- **pymupdf4llm**（default）: `pip install pymupdf pymupdf4llm`
- **mineru**（opt-in、装飾PDF用）: `pip install -U "mineru[core]"`（初回 ~3GB のMLモデルDLあり）

## 研究背景

本Skillは九州産業大学 稲永研究室における2026年度卒業研究
「**LLMを用いたGTFS-JPデータ自動作成向けスキルの開発**」（本田璃陽）の成果物として開発されています。

## ライセンス

Apache License 2.0 — 詳細は [LICENSE](./LICENSE) を参照。

## 参考リンク

- 国土交通省「標準的なバス情報フォーマット」公式ページ
- [MobilityData GTFS Validator](https://github.com/MobilityData/gtfs-validator)
- [GTFS公式仕様 (gtfs.org)](https://gtfs.org/)
- [Open Source Routing Machine (OSRM)](http://project-osrm.org/)
