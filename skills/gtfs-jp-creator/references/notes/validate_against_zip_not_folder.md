# 検証は「展開フォルダ」ではなく「正規成果物の zip」に対して行う

対象: `scripts/run_pipeline.py`, `scripts/generate_shapes.py`, `scripts/validate_gtfs.py`
記録日: 2026-06-15（インガット号B日程の点検中に判明）

## 0. 要点

GTFS-JP の検証は、パイプライン出力フォルダ（`<output>/gtfs/`）の中身ではなく、
**最終生成物である zip（`*_gtfs-jp.zip`）に対して行うこと。**
展開フォルダの一部ファイルは「中間状態」であり、最終成果物と内容が異なる。

## 1. きっかけ（trips.txt の shape_id 問題は“バグではなかった”）

インガット号B日程で「`trips.txt` の shape_id が16便すべて空」かつ Validator で
`unused_shape: 13件` が出た。当初これを不具合と疑ったが、調査の結果**設計通り**と判明。

### 仕組み（コード根拠）
- `generate_shapes.py` は `--update-trips <file>` で「shape_id を付与した trips を
  **別ファイル**に書き出す」設計（入力 trips.txt は上書きしない安全設計）。
- `run_pipeline.py` の Step4 は `--update-trips trips.with_shapes.txt` を渡す
  （289-299行）。よって shape_id 入りの版は `trips.with_shapes.txt` に出る。
- `run_pipeline.py` の Step5(zip梱包) は、`trips.with_shapes.txt` が存在すれば
  `--substitute "trips.with_shapes.txt=trips.txt"` で **zip化時に trips.txt として
  詰める**（321-323行）。

### 結果として起きていたこと
- 展開フォルダの `gtfs/trips.txt` … shape_id 空（中間状態。zip化時にしか置換されない）
- 正規成果物の `*_gtfs-jp.zip` 内の `trips.txt` … shape_id 入り（16便・空0）で**正しい**
- zip には `trips.with_shapes.txt` は含まれない（trips.txt に置換済みのため）

### 検証ミスの教訓
展開フォルダを Validator にかけたため、中間状態の trips.txt（shape_id空）を見て
`unused_shape` が出た。**正規成果物の zip を検証していれば最初から出なかった。**

## 2. 確認方法（zip の中身を直接見る）

```python
import zipfile, csv, io
with zipfile.ZipFile(r"<output>/<feed>_gtfs-jp.zip") as zf:
    print(zf.namelist())                      # trips.with_shapes.txt は無いはず
    data = zf.read("trips.txt").decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(data)))
    empty = sum(1 for r in rows if not (r.get("shape_id") or "").strip())
    print("zip内 trips.txt shape_id空:", empty)  # 0 が正しい
```

Validator を zip に対してかける（フォルダではなく zip を -i に渡す）:
```
java -jar tools/gtfs-validator-cli.jar -i <feed>_gtfs-jp.zip -o <out> -c jp -p -svu
```

## 3. 結論・方針

- `trips.txt` shape_id 問題は**設計通りで実害なし**。修正不要（課題クローズ）。
- 今後の検証は**必ず zip に対して**行う。展開フォルダの trips.txt が空でも誤解しない。
- （任意の品質向上案・優先度低）展開フォルダの trips.txt にも shape_id を反映すれば
  フォルダ単位の検証でも誤解が生じないが、実害が無いため必須ではない。
  run_pipeline.py の中核改修リスクに見合わないと判断し、見送り。

## 4. 付随メモ（インガット号B日程・zip以外で残る Validator 通知）

展開フォルダ検証時に出ていた通知のうち、shape_id とは無関係に残るもの（参考）:
- `equal_shape_distance_same_coordinates`（WARNING, 20件）: shape点の重複/同距離。要調査だが実害小。
- `feed_expiration_date30_days`（WARNING, 1件）: フィード有効期限が近い。運用上の通知。
- `missing_feed_contact_email_and_url`（WARNING, 1件）: feed_info に連絡先未記入（任意項目）。
- `mixed_case_recommended_field`（WARNING, 8件）: 表記の大小文字推奨。軽微。
- `unknown_file`（INFO）: agency_jp/routes_jp 等の JP 拡張。正常（Validator が JP 拡張を知らないだけ）。
これらは zip 検証時にも一部残るため、別途扱うか判断する（今回の shape_id 課題とは無関係）。
