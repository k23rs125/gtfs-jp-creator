# GTFS-JP 半自動生成アプリ（Streamlit MVP）

バス時刻表（PDF / Excel）をアップロードすると、**抽出 → Claudeで構造化 → 条件確認フォーム →
生成・座標補完・検証 → 地図プレビュー・GTFS-JP ダウンロード** までを画面上で行う半自動アプリ。

> 設計：正確さの源は決定的スクリプト。LLM(Claude API) は **構造化(Step2) の判断だけ**。
> PDF/Excel に無いメタ情報（事業者・運行日・運賃など）は推測せず **条件確認フォームで人が入力**する
> （＝「正しく失敗」）。スキル本体（`skills/gtfs-jp-creator/scripts/`）をエンジンとして再利用している。

## 必要なもの
- スキル本体のセットアップ（リポジトリ README のクイックスタート）が済んでいること
  （Python 依存・任意で Java＋Validator jar）。
- 追加で `pip install streamlit anthropic streamlit-folium folium`（`app/requirements.txt` でも可）。
- 構造化を自動化するなら **ANTHROPIC_API_KEY**（環境変数 or 画面で入力）。
  キーが無くても、画面の decision-spec 欄に貼り付け／編集すれば動く。
- `apply_decisions.py`（リポジトリ直下・構造化の決定的展開に使用）。

## 起動
```bash
pip install streamlit anthropic
streamlit run app/app.py
```
ブラウザで `http://localhost:8501` が開く。

## 画面の流れ
1. **① アップロード**：`.xlsx` / `.pdf` を選び「抽出する」。ブロック・便・停留所順が表示される。
2. **② Claudeで構造化**：API キーを入れて「Claudeで構造化」。路線・方向・循環・除外の判断
   （decision-spec）が出る。内容は画面で編集できる。
3. **③ 条件確認**：路線名・事業者・法人番号・運行曜日・有効期間・運賃・対象自治体を入力
   （不明は空欄でOK＝暫定/要確認として入る）。
4. **生成**：「GTFS-JP を生成する」で `apply_decisions` → `run_pipeline`（生成・座標補完・検証）。
5. **④ 結果**：内部整合・Validator ERROR・座標カバレッジ・**地図プレビュー**・要確認リスト・
   **GTFS-JP(zip) ダウンロード**。
6. **⑤ 座標の確認（地図）**：各停留所の座標を **確定=緑／要確認=橙／未補完=赤** で地図表示
   （信頼度は Step3.5f が分類）。**要確認・未補完は地図クリック or 座標入力で確定**し、
   「確定座標で再生成」で反映する。**全部が確定になるまで「公式提出可」にしない**
   （＝推測座標を人が確認するまで正式採用しない）。官公庁提出向けの核。

## 制限（MVP）
- 入力は **テキストPDF / Excel** を主対象（装飾・画像化PDFのOCRは未統合）。
- 完全自動ではない（条件確認の人手入力は設計上残す）。
- `use_nominatim` を ON にすると POI 補完が効くが時間がかかる。
