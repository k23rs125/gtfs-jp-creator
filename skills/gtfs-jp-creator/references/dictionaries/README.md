# 読み辞書（階層）

停留所名ふりがなの補正辞書。優先度は 低→高: **全国 < 自治体 < 路線 < 利用者 < プロジェクト**。
（プロジェクトは各案件の manual_readings.json。ここには 全国/自治体/路線/利用者 を置く）

## 置き方（名前キー）
- `national.csv` … 全国共通（同梱・監査して追加）
- `municipality/<自治体名>.csv` … 例 `municipality/福岡県香春町.csv`
- `route/<路線名>.csv` … 例 `route/香春線.csv`
- `user/<利用者名>.csv` … 例 `user/佐藤.csv`

## 列
`stop_name, ja-Hrkt, en, source, updated_by, updated_at, note`
（後半は任意＝出典・変更者・日時・備考。監査証跡の基盤）

## 保存先の変更
環境変数 `GTFS_DICT_DIR` にパスを設定すると、辞書ルートをそこへ移せます（自治体の共有ドライブ等）。
