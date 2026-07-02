# GTFS-JP 半自動生成アプリ（Streamlit）のコンテナ。
# これ1つで Python依存＋Java(GTFS Validator)＋アプリを同梱し、URLで提供できる。
# 画像PDFのOCR(MinerU)は重い(~3GB)ため既定では入れない（下部の注記参照）。
FROM python:3.12-slim

# Java(検証用) と最小限のツール。OSRM/P11 は実行時にネット経由で使う（同梱不要）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jre-headless curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依存を先に入れてレイヤキャッシュを効かせる
COPY requirements.txt ./requirements.txt
COPY app/requirements.txt ./app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r app/requirements.txt pandas

# アプリ本体（skills/scripts・app・検証jar など。.dockerignore で不要物は除外）
COPY . .

# 自動保存/P11キャッシュは永続化したい場合に -v でマウント（下記 手順書参照）
ENV HOME=/data
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8534
# 0.0.0.0 で待ち受け（コンテナ外からアクセス可能に）
CMD ["streamlit", "run", "app/app.py", \
     "--server.port=8534", "--server.address=0.0.0.0", "--server.headless=true", \
     "--browser.gatherUsageStats=false"]

# --- 画像PDFのOCR(MinerU)を使う場合（任意・重い）---
# 別イメージにするか、この後に以下を足してビルド:
#   RUN pip install --no-cache-dir -U "mineru[core]"
#   （初回OCR時に ~3GB のMLモデルDLあり。GPUがあると速い）
