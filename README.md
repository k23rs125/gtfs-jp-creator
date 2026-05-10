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
[Step 1] PDF/画像/Excel  →  Markdown抽出（pymupdf4llm / olmOCR）
[Step 2] Markdown        →  構造化中間表現（LLM）
[Step 3] 中間表現        →  GTFS-JPの各CSVファイル
[Step 4] stop_times      →  shapes.txt（OSRM map-matching）
[Step 5] 全ファイル群    →  バリデーション（GTFS Validator + JP拡張独自）
                          →  zipパッケージ → 完成
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
