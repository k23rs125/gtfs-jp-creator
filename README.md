# gtfs-jp-creator

**LLMを用いた GTFS-JP データ自動作成向け Claude Skill**

A Claude Skill that helps non-experts (bus operators, municipal staff, citizens, researchers) create [GTFS-JP](https://www.mlit.go.jp/sogoseisaku/transport/sosei_transport_tk_000054.html) (Japan public transit data standard, v4.0) data from non-machine-readable sources such as bus timetable PDFs, photographed paper schedules, Excel spreadsheets, or textual route descriptions.

## 状態

🚧 **開発中（v0.1未満）**
本Skillはまだ雛形段階で、各スクリプトは未実装スタブです。
v0.1（最低限動作するMVP）リリース予定: 2026年8月

## 構成

```
gtfs-jp-creator/
├── SKILL.md              # Skillの中核（frontmatter + ガイド）
├── README.md             # このファイル
├── LICENSE               # Apache License 2.0
├── references/           # Skillが参照する仕様・例・プロンプト
│   ├── gtfs-jp-spec-v4.0-summary.md
│   ├── field-definitions/    # 各テーブル仕様
│   ├── examples/             # 正解GTFS-JPサンプル
│   └── prompts/              # LLM用プロンプト集
└── scripts/              # 実行スクリプト群
    ├── pdf_to_markdown.py             # Step 1: PDF→Markdown
    ├── generate_gtfs_files.py         # Step 3: GTFS-JPファイル生成
    ├── generate_shapes.py             # Step 4: shapes.txt生成 (OSRM)
    ├── validate_gtfs.py               # Step 5a: GTFS Validator実行
    ├── validate_gtfs_jp_extensions.py # Step 5b: JP拡張独自検証
    └── package_gtfs_zip.py            # 最終: zipパッケージング
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

## 必要環境

- Python 3.10以上
- Java 11以上のJRE（GTFS Validator実行用）
- インターネット接続（OSRM map-matching API利用時）
- 推奨: Cowork mode（Claude desktop app）

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
