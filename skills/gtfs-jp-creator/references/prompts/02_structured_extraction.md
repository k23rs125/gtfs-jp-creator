# 02_structured_extraction.md

LLMプロンプト：**Markdown → 構造化JSON**（GTFS-JP 中間表現）

このファイルは GTFS-JP Creator Skill の Step 2 で使うプロンプトです。
`scripts/pdf_to_markdown.py` (Step 1) で得られた Markdown を、GTFS-JP の各テーブルに対応した中間JSONに変換するため、LLM（Claude）に投げる指示です。

---

## プロンプト本文（このセクションをそのままLLMに渡す）

```
あなたは GTFS-JP（公共交通運行情報標準データ仕様 v4.0、令和8年3月版）の
データ整備を支援する専門家アシスタントです。

# あなたの仕事

入力された日本語のバス時刻表 Markdown を読み取り、
GTFS-JP の主要テーブルに対応する **構造化JSON** を出力してください。

# 入力フォーマット

入力は MinerU や pymupdf4llm などのツールで PDF から抽出された Markdown です。
以下のような特徴があります:

- 路線見出し（例: "第一小学校区ルート 路線番号1 一番田～上須恵線"）
- 各路線に **方向別** のテーブルが複数あります（例: "須恵中学校先回り", "上須恵口先回り"）
- テーブルは HTML table 形式の場合があります（<table><tr><td>...）
- 列: 停留所順序番号, 停留所名, 1便の時刻, 3便の時刻, …
- 平日／土曜／日祝で別テーブルが用意される場合あり
- 装飾的な背景画像や注釈は無視してOK

# 出力フォーマット（このスキーマに厳密に従う）

```json
{
  "agency": {
    "agency_id": "string",
    "agency_name": "string",
    "agency_official_name": "string|null",
    "agency_url": "string|null",
    "agency_phone": "string|null",
    "agency_zip_number": "string|null",
    "agency_address": "string|null"
  },
  "routes": [
    {
      "route_id": "string",
      "route_short_name": "string",
      "route_long_name": "string",
      "route_type": 3,
      "route_color": "string|null",
      "route_origin_stop": "string|null",
      "route_destination_stop": "string|null",
      "route_via_stop": "string|null"
    }
  ],
  "stops": [
    {
      "stop_id": "string",
      "stop_name": "string",
      "stop_lat": "number|null",
      "stop_lon": "number|null"
    }
  ],
  "trips": [
    {
      "trip_id": "string",
      "route_id": "string",
      "service_id": "string",
      "direction_id": 0,
      "trip_headsign": "string|null",
      "shape_id": "string|null"
    }
  ],
  "stop_times": [
    {
      "trip_id": "string",
      "stop_id": "string",
      "stop_sequence": 1,
      "arrival_time": "HH:MM:SS",
      "departure_time": "HH:MM:SS"
    }
  ],
  "calendar": [
    {
      "service_id": "string",
      "monday": 0,
      "tuesday": 0,
      "wednesday": 0,
      "thursday": 0,
      "friday": 0,
      "saturday": 0,
      "sunday": 0,
      "start_date": "YYYYMMDD",
      "end_date": "YYYYMMDD"
    }
  ],
  "_meta": {
    "source": "string (元PDFのファイル名や事業者名)",
    "extraction_notes": "string (抽出時に気付いた点、不確実だった点)",
    "warnings": ["array of strings"]
  }
}
```

# 命名規則

- `route_id`: `R{連番:02d}` 形式（例: R01, R02, R03）
- `stop_id`: `S{連番:03d}` 形式（例: S001, S002）
- `trip_id`: `{route_id}_{direction_id}_{便番号}` 形式（例: R01_0_1, R01_1_2）
- `service_id`: 運行カレンダー種別（例: WEEKDAY, SATURDAY, SUNDAY_HOLIDAY, EVERY_DAY）

# 重要なルール

1. **推測しない**: 入力 Markdown に書かれていない値は `null` を入れる。
   緯度経度・郵便番号・URLなど書かれていなければ `null`。
2. **不確実は明記**: 解釈に迷った点は `_meta.warnings` 配列に文字列で記録。
   例: "路線番号7の便番号にアスタリスクが付与されているが意味不明"
3. **時刻フォーマット**: 24時間表記の `HH:MM:SS`。秒は不明なら `:00`。
   25時超え（深夜便）の表現も維持（例: `25:30:00`）。
4. **arrival_time と departure_time**: バス時刻表は通常1つの時刻のみ記載なので
   両方に同じ値を入れる。
5. **direction_id**: 往き(outbound)=0, 復り(inbound)=1。
   どちらが outbound か曖昧な場合は便番号の若い方を 0 にする。
6. **service_id**: テーブル見出しに「平日」「土曜」「日祝」とあればそれぞれ
   `WEEKDAY` `SATURDAY` `SUNDAY_HOLIDAY` を使用。
   「全ての曜日共通」「町内線」のような表記なら `EVERY_DAY`。
7. **route_type**: バスは常に `3`。
8. **stop_sequence**: 1始まり整数（GTFSは1始まり推奨）。
9. **同一路線の往復で停留所重複**: 物理的に同じ停留所なら同じ stop_id を使い回す。
10. **calendar.start_date/end_date**: 入力に明記なければ `null` を許容（_metaのwarningsに記載）。

# 出力時の注意

- **マークダウン以外**を返さないこと。コメントや説明文を JSON の前後に付けない。
- **JSON はパース可能な形式**で。trailing commaなし。
- 見やすさのため適度に整形してOK（インデント2スペース）。

# Few-shot 例（参考: 須恵町コミュニティバス）

## 入力 Markdown 例（短縮）

```
第一小学校区ルート 路線番号1 一番田～上須恵線
<table>
<tr><td colspan="2">須恵中学校先回り</td><td>1便</td><td>3便</td></tr>
<tr><td>1</td><td>福祉センター</td><td>8:10</td><td>10:20</td></tr>
<tr><td>2</td><td>須恵中学校</td><td>8:11</td><td>10:21</td></tr>
<tr><td>3</td><td>須恵第一小学校南</td><td>8:13</td><td>10:23</td></tr>
</table>
```

## 出力 JSON 例（対応部分）

```json
{
  "agency": {
    "agency_id": "STEMACHI",
    "agency_name": "須恵町コミュニティバス",
    "agency_official_name": null,
    "agency_url": null,
    "agency_phone": null,
    "agency_zip_number": null,
    "agency_address": null
  },
  "routes": [
    {
      "route_id": "R01",
      "route_short_name": "1",
      "route_long_name": "一番田～上須恵線",
      "route_type": 3,
      "route_color": null,
      "route_origin_stop": "福祉センター",
      "route_destination_stop": "福祉センター",
      "route_via_stop": "上須恵"
    }
  ],
  "stops": [
    { "stop_id": "S001", "stop_name": "福祉センター", "stop_lat": null, "stop_lon": null },
    { "stop_id": "S002", "stop_name": "須恵中学校", "stop_lat": null, "stop_lon": null },
    { "stop_id": "S003", "stop_name": "須恵第一小学校南", "stop_lat": null, "stop_lon": null }
  ],
  "trips": [
    { "trip_id": "R01_0_1", "route_id": "R01", "service_id": "EVERY_DAY",
      "direction_id": 0, "trip_headsign": "須恵中学校先回り", "shape_id": null },
    { "trip_id": "R01_0_3", "route_id": "R01", "service_id": "EVERY_DAY",
      "direction_id": 0, "trip_headsign": "須恵中学校先回り", "shape_id": null }
  ],
  "stop_times": [
    { "trip_id": "R01_0_1", "stop_id": "S001", "stop_sequence": 1,
      "arrival_time": "08:10:00", "departure_time": "08:10:00" },
    { "trip_id": "R01_0_1", "stop_id": "S002", "stop_sequence": 2,
      "arrival_time": "08:11:00", "departure_time": "08:11:00" },
    { "trip_id": "R01_0_1", "stop_id": "S003", "stop_sequence": 3,
      "arrival_time": "08:13:00", "departure_time": "08:13:00" },
    { "trip_id": "R01_0_3", "stop_id": "S001", "stop_sequence": 1,
      "arrival_time": "10:20:00", "departure_time": "10:20:00" },
    { "trip_id": "R01_0_3", "stop_id": "S002", "stop_sequence": 2,
      "arrival_time": "10:21:00", "departure_time": "10:21:00" },
    { "trip_id": "R01_0_3", "stop_id": "S003", "stop_sequence": 3,
      "arrival_time": "10:23:00", "departure_time": "10:23:00" }
  ],
  "calendar": [
    {
      "service_id": "EVERY_DAY",
      "monday": 1, "tuesday": 1, "wednesday": 1, "thursday": 1,
      "friday": 1, "saturday": 1, "sunday": 1,
      "start_date": null, "end_date": null
    }
  ],
  "_meta": {
    "source": "須恵町コミュニティバス時刻表 (komyubasujikokuhyou.pdf)",
    "extraction_notes": "Few-shot 例として 路線番号1 の 1便と3便のみ示す。実運用では全路線・全便を抽出する。",
    "warnings": [
      "calendar の start_date/end_date は元PDFに改正日（令和7年4月1日）の記載があるが、フィードの有効期間明記なしで null とした"
    ]
  }
}
```

# それでは入力Markdown を渡しますので、上記スキーマに従って JSON を出力してください

[ここに実際の Markdown が続く]
```

---

## このプロンプトの設計意図（メモ）

### なぜ全テーブルを一気に出させるか

- 各テーブル間に **参照整合性** がある（trip.route_id ⇒ routes、stop_times.stop_id ⇒ stops）
- 別々に出させると、参照ID が食い違うリスクが高い
- 同一プロンプト内で生成すれば LLM が整合を保ちやすい

### なぜ `_meta` フィールドを設けたか

- LLM が抽出時に気づいた問題（曖昧な記載、推測した点）を **明示的に申告**できるようにする
- 後段のスクリプトはこの warnings を見て「人手レビューが必要な箇所」を可視化できる
- これは **研究上の透明性**（誤りを隠さない）にも貢献する

### なぜ命名規則を厳密に決めたか

- LLM の出力が予測可能になり、後段スクリプトのパースが楽
- 異なる事業者で同じ規則を使えば、データの結合や比較も容易
- ID が連番＋ゼロパディングで人間にも読みやすい

### 不足しているもの（今後の課題）

- **GTFS-JP 拡張**（`agency_jp`, `office_jp`, `pattern_jp`, `routes_jp`）の生成
  → 別プロンプトで対応するか、このプロンプトを拡張する
- **fare_attributes / fare_rules**（運賃情報）
  → PDF に運賃表があれば抽出可能、別ステップで実装予定
- **shapes.txt**（経路形状）
  → Step 4 で OSRM map-matching を使うため、このプロンプトの責務外

---

## 動作テストの計画

このプロンプトを実際にテストするには：

1. 須恵町PDFを MinerU で Markdown 化（既に済み）
2. その Markdown を本プロンプトと一緒に Claude に渡す
3. 出力 JSON が以下を満たすか検証:
   - JSON.parse() で読める
   - 各テーブルの必須フィールドが欠けていない
   - route_id 等の参照整合性が保たれている
   - stop_times の時刻フォーマットが統一されている
4. 公式 GTFS-JP データと比較して内容の一致率を計測

実装は次のステップ（`scripts/extract_structured.py` 等）で行う。
