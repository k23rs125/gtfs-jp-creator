# GTFS-JP v4.0 仕様サマリ

> 公共交通運行情報標準データ仕様 第4版（令和8年3月）
> 出典: 国土交通省「標準的なバス情報フォーマット」関連資料
> （※詳細は公式仕様書を必ず参照すること）

## ステータス

**作成中（スケルトン）。**
本ファイルは段階的に充実させる予定。当面は AIGID提供の正解データ・公式仕様PDFを参照すること。

## 全体構成

GTFS-JP は GTFS（General Transit Feed Specification, 国際標準）を日本向けに拡張したもの。
v4.0 では以下のテーブル群で構成される（合計32ファイル想定）:

### 必須ファイル（GTFS本体）
- `agency.txt` — 事業者情報
- `stops.txt` — 停留所・乗降場所
- `routes.txt` — 路線情報
- `trips.txt` — 便（ある日の特定路線の運行）
- `stop_times.txt` — 各便の停車時刻
- `calendar.txt` — 運行日（曜日パターン）
- `fare_attributes.txt` — 運賃属性
- `feed_info.txt` — フィード自体のメタ情報

### GTFS-JP拡張（4ファイル）
- `agency_jp.txt` — 事業者の日本固有情報（正式名称、住所、代表者など）
- `routes_jp.txt` — 路線の日本固有情報（更新日、起点/経由/終点）
- `office_jp.txt` — 営業所情報
- `pattern_jp.txt` — 運行パターン情報

### 任意ファイル
- `calendar_dates.txt` — 例外日
- `fare_rules.txt` — 運賃ルール
- `shapes.txt` — 経路形状
- `frequencies.txt` — 頻度ベースの運行
- `transfers.txt` — 乗換
- `translations.txt` — 翻訳

## 主要テーブルのフィールド一覧

→ 詳細は `field-definitions/` 配下の各ファイルを参照。

## バリデーション

GTFS-JP拡張 4ファイルの検証は MobilityData の公式 GTFS Validator では
カバーされない。本Skill では `scripts/validate_gtfs_jp_extensions.py`
で独自にチェックする。

## 参考リンク

- 国土交通省「標準的なバス情報フォーマット」公式ページ
- 公益社団法人 AIGID（地理情報システム学会）「GTFS-JPサンプルデータ」
- MobilityData GTFS仕様: https://gtfs.org/
- MobilityData GTFS Validator: https://github.com/MobilityData/gtfs-validator
