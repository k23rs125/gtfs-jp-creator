# サンプル: 直方市コミュニティバス 永満寺線（往復）

本スキルが **Excel 時刻表から生成した GTFS-JP の実例**。出力の「正解形式」を示す参照用。

- 21 停留所・9 便（往復: 直方駅 ⇄ 永満寺団地）・stop_times 189 行
- 国土数値情報 P11 で座標 **100% 補完**・標準 MobilityData Validator **ERROR 0**・
  内部整合 **189/189 ＝ 100%**（抽出時刻が stop_times まで保持）
- `calendar.txt` は月〜土運行、`calendar_dates.txt` に年末年始（12/31〜1/3）の運休を展開
- GTFS-JP 拡張（`agency_jp.txt` / `routes_jp.txt`）と `translations.txt`・`shapes.txt` を含む

> ⚠️ 注意：`agency` / `agency_jp` / `fare_attributes` は PDF/Excel に無いため調査した
> **暫定値（要確認）**。本番運用では自治体の正確な値に置き換えること。
> ここでは **ファイル構成・各項目の書式の見本**（LLM の few-shot・形式確認）として使う。
