"""
download_official_feed.py
=========================
能動確認で見つけた公式GTFS（BODIK等のオープンデータ）を自動ダウンロードし、
Step 3.5a（merge_stop_coords）の reference_feed として座標再利用に使う。

設計方針:
    - 再利用するのは **停留所座標のみ**（merge_stop_coords が名称マッチで引き当てる）。
      ダイヤ（時刻・便）は手元PDF/Excelを優先する（公式が古い版のことがあるため）。
    - ダウンロードはキャッシュする（同じURLは再取得しない）。
    - 公式データは多くが CC-BY。座標を使う場合は **出典明記が必要**（feed_info 等）。
      本スクリプトは取得元URLを report に記録し、利用者に出典明記を促す。

Usage:
    python download_official_feed.py --url <GTFS zip URL> -o <out_dir>
    # 関数利用: from download_official_feed import get_official_feed

License: Apache 2.0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path


def get_official_feed(url: str, out_dir: str | Path, timeout: int = 60) -> Path:
    """url から GTFS zip をダウンロードして out_dir に保存し、保存先パスを返す。
    同じURLの取得物が既にあれば再ダウンロードしない（キャッシュ）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # URL のハッシュでキャッシュ名を決める（ファイル名衝突回避）
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    out_path = out_dir / f"official_feed_{h}.zip"
    if out_path.exists() and out_path.stat().st_size > 0:
        print(f"  公式feedキャッシュ利用: {out_path}", file=sys.stderr)
        return out_path
    req = urllib.request.Request(url, headers={"User-Agent": "gtfs-jp-creator/1.0"})
    print(f"  公式feedをダウンロード: {url}", file=sys.stderr)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    out_path.write_bytes(data)
    print(f"  保存: {out_path} ({len(data)/1024:.0f}KB)", file=sys.stderr)
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="公式GTFS(オープンデータ)をDLしてreference_feedにする")
    ap.add_argument("--url", required=True, help="公式GTFS zip の直接ダウンロードURL")
    ap.add_argument("-o", "--out-dir", default="official_feed_cache", help="保存先ディレクトリ")
    ap.add_argument("--report", default=None, help="取得元URLを記録するレポート(任意・出典明記用)")
    a = ap.parse_args()
    try:
        path = get_official_feed(a.url, a.out_dir)
    except Exception as e:  # noqa: BLE001
        print(f"Error: 公式feedの取得に失敗: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if a.report:
        Path(a.report).write_text(json.dumps(
            {"source_url": a.url, "saved_to": str(path),
             "note": "座標のみ再利用（ダイヤは手元優先）。CC-BY等のライセンスは feed_info 等で出典明記すること。"},
            ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
