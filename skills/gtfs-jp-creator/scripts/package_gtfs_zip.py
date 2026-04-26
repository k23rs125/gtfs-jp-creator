"""
package_gtfs_zip.py
===================

最終ステップ: GTFS-JPファイル群をzipにパッケージングする。

GTFS仕様 (https://gtfs.org/schedule/reference/#dataset-files):
    - feed.zip 内のファイルはルート直下に配置 (サブディレクトリ不可)
    - 文字コードはUTF-8
    - 改行コードはLF or CRLF (本ツールはCRLF出力)

Usage:
    python package_gtfs_zip.py <input_dir> -o <output.zip>
    python package_gtfs_zip.py <input_dir> -o feed_<事業者>_<日付>.zip

Status: STUB (skeleton only - implementation TBD)

License: Apache 2.0
"""

import argparse
import sys
import zipfile
from pathlib import Path


# GTFS-JPで含めるべき全ファイル
GTFS_JP_FILES = [
    # GTFS本体
    "agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt",
    "calendar.txt", "calendar_dates.txt",
    "fare_attributes.txt", "fare_rules.txt",
    "shapes.txt", "frequencies.txt", "transfers.txt", "feed_info.txt",
    "translations.txt",
    # GTFS-JP拡張
    "agency_jp.txt", "routes_jp.txt", "office_jp.txt", "pattern_jp.txt",
]


def package_zip(input_dir: Path, output_zip: Path) -> None:
    """input_dir 内のGTFSファイルをzipにまとめる。"""
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        included = []
        for filename in GTFS_JP_FILES:
            file_path = input_dir / filename
            if file_path.exists():
                # arcnameを指定して、ディレクトリ階層を含めない
                zf.write(file_path, arcname=filename)
                included.append(filename)
        print(f"[OK] Packaged {len(included)} files into {output_zip}")
        for f in included:
            print(f"  - {f}")


def main():
    parser = argparse.ArgumentParser(description="Package GTFS-JP files into a zip")
    parser.add_argument("input_dir", help="Directory containing GTFS-JP CSV files")
    parser.add_argument("-o", "--output", required=True, help="Output zip file path")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        print(f"Error: not a directory: {input_dir}", file=sys.stderr)
        sys.exit(1)

    package_zip(input_dir, Path(args.output))


if __name__ == "__main__":
    main()
