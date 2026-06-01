#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
download_p11.py  —  国土数値情報 P11(バス停留所) 第3.0版(令和4年度/2022) 自動取得ユーティリティ

目的:
    これまで利用者が手動でダウンロード・展開していた P11 Shapefile を、
    都道府県名(またはコード)を指定するだけで自動取得・展開し、
    enrich_stops_p11.py の --p11 にそのまま渡せる .shp パスを返す。

設計方針:
    - 追加依存なし(標準ライブラリ urllib / zipfile のみ)。skill の "pure Python" 方針に合わせる。
    - 取得対象は第3.0版(年度コード "22" = 令和4年度)。--year で第2.0版("10")にも切替可。
    - ファイル名は datalist ページで確認済み: 例) 福岡=P11-22_40_SHP.zip / 沖縄=P11-22_47_SHP.zip
    - 基底URLは KSJ 共通の静的パス形式 (/ksj/gml/data/P11/P11-22/...)。
      他データ(N02/N03)で確認済みの規則だが、P11 で初回失敗したら --base-url か --url で上書き可能。
    - キャッシュ: 取得済み zip / 展開済み .shp があれば再取得しない。

注意:
    - 実行には *ネットワークが必要* です(Rio の Windows 環境で実行してください。サンドボックス不可)。
    - 第3.0版は座標系 JGD2011、ライセンスは「国土数値情報ダウンロードサイトコンテンツ利用規約」
      (オープンデータ)。第2.0版(2010)は JGD2000・非商用なので、研究では第3.0版を推奨。
    - 第2.0版と第3.0版で shp の属性スキーマが異なります(バス停名は P11_001 で共通だが、
      事業者名/区分の項目が変わっている)。enrich_stops_p11.py の属性名検出は 3.0 で要確認。
"""

import argparse
import os
import sys
import glob
import zipfile
import urllib.request
import urllib.error

# 標準の都道府県コード(全国地方公共団体コードの上2桁)。datalist ページの一覧と一致。
PREF_CODES = {
    "北海道": "01", "青森": "02", "岩手": "03", "宮城": "04", "秋田": "05",
    "山形": "06", "福島": "07", "茨城": "08", "栃木": "09", "群馬": "10",
    "埼玉": "11", "千葉": "12", "東京": "13", "神奈川": "14", "新潟": "15",
    "富山": "16", "石川": "17", "福井": "18", "山梨": "19", "長野": "20",
    "岐阜": "21", "静岡": "22", "愛知": "23", "三重": "24", "滋賀": "25",
    "京都": "26", "大阪": "27", "兵庫": "28", "奈良": "29", "和歌山": "30",
    "鳥取": "31", "島根": "32", "岡山": "33", "広島": "34", "山口": "35",
    "徳島": "36", "香川": "37", "愛媛": "38", "高知": "39", "福岡": "40",
    "佐賀": "41", "長崎": "42", "熊本": "43", "大分": "44", "宮崎": "45",
    "鹿児島": "46", "沖縄": "47",
}

DEFAULT_BASE_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/P11/"


def resolve_pref_code(value: str) -> str:
    """'福岡県' / '福岡' / '40' などを 2桁コードに正規化する。"""
    v = value.strip()
    if v.isdigit():
        return v.zfill(2)          # 都道府県(01-47) / 地方(52-59) コードをそのまま許可
    if v in ("全国", "all", "ALL"):
        return ""                  # 全国ファイルはコード無し
    # まずフル名で照合（「北海道」「東京」などはこの時点で解決）
    if v in PREF_CODES:
        return PREF_CODES[v]
    # 次に末尾の 都/道/府/県 を落として照合（「沖縄県」→「沖縄」「東京都」→「東京」）
    for suffix in ("都", "道", "府", "県"):
        if v.endswith(suffix) and v[:-1] in PREF_CODES:
            return PREF_CODES[v[:-1]]
    raise ValueError(f"都道府県を解決できません: {value!r} (例: 沖縄県 / 沖縄 / 47)")


def build_url(code: str, year: str, fmt: str, base_url: str) -> str:
    """KSJ 静的パス形式で zip の URL を組み立てる。"""
    if code:
        filename = f"P11-{year}_{code}_{fmt}.zip"
    else:
        filename = f"P11-{year}_{fmt}.zip"     # 全国
    return f"{base_url.rstrip('/')}/P11-{year}/{filename}", filename


def download(url: str, dest_path: str) -> None:
    """zip を取得して dest_path に保存。明示的な User-Agent を付ける。"""
    req = urllib.request.Request(url, headers={"User-Agent": "gtfs-jp-creator/p11-downloader"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"ダウンロード失敗 (HTTP {e.code}): {url}\n"
            f"  → URL 規則が変わっている可能性があります。datalist ページで実 URL を確認し、\n"
            f"    --url <実URL> もしくは --base-url <基底URL> で上書きしてください。"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"ネットワークエラー: {e.reason} ({url})") from e


def find_shapefile(extract_dir: str) -> str:
    """展開ディレクトリ配下から .shp を 1 つ探す(名前はハードコードせず glob)。"""
    shps = glob.glob(os.path.join(extract_dir, "**", "*.shp"), recursive=True)
    if not shps:
        raise RuntimeError(f".shp が見つかりません: {extract_dir} (zip の中身を確認してください)")
    if len(shps) > 1:
        # 通常は 1 つ。複数あれば最初を返しつつ警告。
        sys.stderr.write(f"[警告] .shp が複数見つかりました。先頭を使用: {shps}\n")
    return shps[0]


def get_p11_shapefile(prefecture: str, out_dir: str = "p11_data",
                      year: str = "22", fmt: str = "SHP",
                      base_url: str = DEFAULT_BASE_URL,
                      url: str = None, use_cache: bool = True) -> str:
    """
    都道府県を指定して P11 Shapefile を取得・展開し、.shp の絶対パスを返す。
    run_pipeline.py から import して使える形にしてある。
    """
    code = resolve_pref_code(prefecture)
    if url:
        zip_url = url
        zip_name = os.path.basename(url)
    else:
        zip_url, zip_name = build_url(code, year, fmt, base_url)

    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, zip_name)
    extract_dir = os.path.join(out_dir, zip_name[:-4])   # 拡張子 .zip を除いた名前のフォルダ

    # キャッシュ: 展開済みなら即返す
    if use_cache and os.path.isdir(extract_dir):
        try:
            return os.path.abspath(find_shapefile(extract_dir))
        except RuntimeError:
            pass   # 展開が壊れていれば取り直す

    # 取得
    if not (use_cache and os.path.isfile(zip_path)):
        sys.stderr.write(f"[取得] {zip_url}\n")
        download(zip_url, zip_path)
    else:
        sys.stderr.write(f"[キャッシュ] 既存 zip を使用: {zip_path}\n")

    # 展開
    os.makedirs(extract_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    shp = find_shapefile(extract_dir)
    return os.path.abspath(shp)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="国土数値情報 P11(バス停留所) 第3.0版 の都道府県別 Shapefile を自動取得する")
    p.add_argument("prefecture", help="都道府県名 or コード (例: 沖縄県 / 沖縄 / 47 / 全国)")
    p.add_argument("-o", "--out-dir", default="p11_data", help="保存先ディレクトリ (既定: p11_data)")
    p.add_argument("--year", default="22",
                   help="年度コード。22=令和4年度(第3.0版, 既定) / 10=平成22年度(第2.0版)")
    p.add_argument("--format", dest="fmt", default="SHP", choices=["SHP", "GML"],
                   help="取得形式 (既定: SHP)")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="基底URLの上書き")
    p.add_argument("--url", default=None, help="zip の実URLを直接指定(規則変更時の保険)")
    p.add_argument("--no-cache", action="store_true", help="キャッシュを使わず再取得する")
    args = p.parse_args(argv)

    try:
        shp = get_p11_shapefile(
            args.prefecture, out_dir=args.out_dir, year=args.year, fmt=args.fmt,
            base_url=args.base_url, url=args.url, use_cache=not args.no_cache)
    except (ValueError, RuntimeError) as e:
        sys.stderr.write(f"エラー: {e}\n")
        return 1

    # 標準出力には .shp パスのみ。シェルで変数に取り込んで --p11 に渡せる。
    print(shp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
