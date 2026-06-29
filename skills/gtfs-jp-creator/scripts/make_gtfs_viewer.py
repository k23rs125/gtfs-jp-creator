# -*- coding: utf-8 -*-
"""作成した GTFS-JP feed から、単一HTMLの GTFS ビューア（データ確認シート）を生成する。

7タブ（路線一覧 / 時刻表 / 運賃表 / 路線図 / 運行カレンダー / バス停一覧 / データチェック結果）を
ブラウザで自動表示する standalone HTML。GTFSの各 .txt を window.__GTFS_DATA__ に埋め込むため、
サーバ無し・file:// で開いてそのまま見られる（CORSの心配なし）。

テンプレート（templates/gtfs_viewer_template.html）の原典は
九州産業大学 稲永研究室(remilab) の standalone GTFS viewer。

使い方:
  python make_gtfs_viewer.py --feed <feedの.txtがあるディレクトリ> -o viewer.html [--title "○○バス データ確認シート"]
"""
import argparse
import json
import sys
from pathlib import Path

# テンプレートが参照するファイル群（順序はテンプレの GTFS_FILES と一致）
GTFS_FILES = [
    "agency.txt", "agency_jp.txt", "feed_info.txt", "office_jp.txt",
    "stops.txt", "routes.txt", "routes_jp.txt", "trips.txt", "stop_times.txt",
    "calendar.txt", "calendar_dates.txt", "shapes.txt",
    "fare_attributes.txt", "fare_rules.txt", "translations.txt",
]
TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "gtfs_viewer_template.html"


def _read_text(p: Path) -> str:
    # BOM(utf-8-sig)を除いて読む。改行は LF に正規化（JS側の split("\n") に合わせる）。
    t = p.read_text(encoding="utf-8-sig")
    return t.replace("\r\n", "\n").replace("\r", "\n")


def _default_title(data: dict) -> str:
    # agency_name か feed の publisher から見出しを作る（無ければ汎用）。
    agency = data.get("agency.txt", "")
    rows = [r for r in agency.split("\n") if r.strip()]
    if len(rows) >= 2:
        header = rows[0].split(",")
        if "agency_name" in header:
            name = rows[1].split(",")[header.index("agency_name")].strip()
            if name:
                return f"{name} データ確認シート"
    return "GTFS-JP データ確認シート"


def build_viewer(feed_dir: Path, out_path: Path, title: str | None = None) -> Path:
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"テンプレートが見つかりません: {TEMPLATE}")
    data = {}
    for f in GTFS_FILES:
        p = feed_dir / f
        # trips は shape_id を埋めた trips.with_shapes.txt があればそちらを使う
        # （これが無いと路線図で経路線が引けず点だけになる）。
        if f == "trips.txt" and (feed_dir / "trips.with_shapes.txt").exists():
            p = feed_dir / "trips.with_shapes.txt"
        if p.exists():
            data[f] = _read_text(p)
    if "stops.txt" not in data or "routes.txt" not in data:
        raise ValueError(f"GTFSの必須ファイル(stops/routes)が {feed_dir} に見つかりません")

    title = title or _default_title(data)
    folder = feed_dir.name

    html = _read_text(TEMPLATE)
    # データ埋め込み（JSON は JS オブジェクトリテラルとして妥当）
    html = html.replace("__GTFS_DATA_JSON__", json.dumps(data, ensure_ascii=False))
    html = html.replace("__VIEWER_TITLE__", title)
    html = html.replace("__FEED_FOLDER__", folder)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="GTFS feed から単一HTMLのビューアを生成")
    ap.add_argument("--feed", required=True, help="GTFSの .txt があるディレクトリ")
    ap.add_argument("-o", "--out", required=True, help="出力HTMLパス")
    ap.add_argument("--title", default=None, help="ビューアの見出し（省略時は事業者名から自動）")
    a = ap.parse_args()
    out = build_viewer(Path(a.feed), Path(a.out), a.title)
    cnt = sum(1 for f in GTFS_FILES if (Path(a.feed) / f).exists())
    print(f"[OK] GTFSビューアを生成: {out} (内蔵 {cnt} ファイル)")


if __name__ == "__main__":
    sys.exit(main())
