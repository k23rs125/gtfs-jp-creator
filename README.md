# gtfs-jp-creator

**LLMを用いた GTFS-JP データ自動作成向け Claude プラグイン（Skill同梱）**

A Claude Code / Cowork mode plugin containing a Skill that helps non-experts (bus operators, municipal staff, citizens, researchers) create [GTFS-JP](https://www.mlit.go.jp/sogoseisaku/transport/sosei_transport_tk_000054.html) (Japan public transit data standard, v4.0) data from non-machine-readable sources such as bus timetable PDFs, photographed paper schedules, Excel spreadsheets, or textual route descriptions.

## 状態

🚧 **開発中（v0.1未満）**
本プラグインはまだ雛形段階で、各スクリプトは未実装スタブです。
v0.1（最低限動作するMVP）リリース予定: 2026年8月

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
│       └── scripts/                   # 実行スクリプト群
│           ├── pdf_to_markdown.py             # Step 1: PDF→Markdown
│           ├── generate_gtfs_files.py         # Step 3: GTFS-JPファイル生成
│           ├── generate_shapes.py             # Step 4: shapes.txt生成 (OSRM)
│           ├── validate_gtfs.py               # Step 5a: GTFS Validator実行
│           ├── validate_gtfs_jp_extensions.py # Step 5b: JP拡張独自検証
│           └── package_gtfs_zip.py            # 最終: zipパッケージング
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

```
[Step 1]   PDF/画像/Excel  →  Markdown抽出（pymupdf4llm / MinerU）
[Step 2]   Markdown        →  構造化中間表現（LLM）
[Step 3]   中間表現        →  GTFS-JPの各CSVファイル
[Step 3.5] stops.txt       →  緯度経度補完（Nominatim API）
[Step 4]   stop_times      →  shapes.txt（OSRM map-matching）
[Step 5]   全ファイル群    →  バリデーション（GTFS Validator + JP拡張独自）
                            →  zipパッケージ → 完成
```

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

### Step 3: JSON → CSV（決定的・LLM 不要）

```powershell
python skills\gtfs-jp-creator\scripts\generate_gtfs_files.py `
  test_demo\extracted.json `
  -o test_demo\gtfs_output
```

→ `test_demo\gtfs_output\` に GTFS-JP 8 ファイル（`agency.txt`, `routes.txt`, `stops.txt`, `trips.txt`, `stop_times.txt`, `calendar.txt`, `feed_info.txt`, `routes_jp.txt`）が生成されます。

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

### Step 4-5: shapes.txt 生成・バリデーション

> 🚧 **v0.1 リリース時に対応予定**。現時点ではスタブ実装のため、Step 4-5 は未動作です（中間発表後の実装目標）。

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

# 4-5. （v0.1 で対応予定）
```

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

#### Step 4-5: 任意（v0.1 リリース時に対応）

現時点（v0.1 未満）では `generate_shapes.py` および `validate_gtfs.py` はスタブ実装のため、Step 4-5 は手動 or 別ツールでの実施を推奨。

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

## 必要環境

- Python 3.10以上
- Java 11以上のJRE（GTFS Validator実行用）
- インターネット接続（OSRM map-matching API利用時、MinerU初回モデルDL用）
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
