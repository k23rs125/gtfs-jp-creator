"""
package_gtfs_zip.py
===================

最終ステップ: GTFS-JP ファイル群を zip にパッケージングする。

GTFS仕様 (https://gtfs.org/schedule/reference/#dataset-files):
    - feed.zip 内のファイルはルート直下に配置 (サブディレクトリ不可)
    - 文字コードは UTF-8
    - 改行コードは LF または CRLF (本ツールは CRLF 出力)

主な機能:
    - GTFS-JP 標準ファイル一覧を自動で含める
    - --substitute SRC=DEST で「SRC を DEST という名前で zip に入れる」
      （例: stops.merged.txt を stops.txt として梱包する）
    - 必須ファイルの欠落チェック（warning として表示、終了コードには影響しない）

Usage:
    # 基本
    python package_gtfs_zip.py <input_dir> -o <output.zip>

    # 座標入りの stops.merged.txt を stops.txt として梱包する例
    python package_gtfs_zip.py test_demo/gtfs_output \\
        -o feed_kogabus_20260601.zip \\
        --substitute stops.merged.txt=stops.txt \\
        --substitute trips.with_shapes.txt=trips.txt

License: Apache 2.0
"""

from __future__ import annotations

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

# GTFS 必須ファイル（GTFS-JP も同じ + feed_info.txt）
GTFS_REQUIRED = {
    "agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt",
    "feed_info.txt",
}
# calendar.txt または calendar_dates.txt のいずれかが必要
GTFS_REQUIRED_CALENDAR = {"calendar.txt", "calendar_dates.txt"}


def parse_substitutions(values: list[str] | None) -> dict[str, str]:
    """--substitute "A=B" を {B: A} に変換する。

    key = zip 内での名前（GTFS 標準名）, value = 入力ディレクトリ内の実ファイル名。
    """
    subs: dict[str, str] = {}
    if not values:
        return subs
    for v in values:
        if "=" not in v:
            raise ValueError(f"--substitute は SRC=DEST 形式で指定してください: {v}")
        src, dest = v.split("=", 1)
        src, dest = src.strip(), dest.strip()
        if not src or not dest:
            raise ValueError(f"--substitute の SRC または DEST が空: {v}")
        subs[dest] = src
    return subs


def package_zip(input_dir: Path, output_zip: Path,
                 substitutions: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """input_dir 内の GTFS ファイルを zip にまとめる。

    Args:
        substitutions: { zip内ファイル名: 入力ディレクトリ内の実ファイル名 }
            例: {"stops.txt": "stops.merged.txt"}

    Returns:
        (included_files, warnings)
    """
    subs = substitutions or {}
    included: list[str] = []
    warnings: list[str] = []

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        # GTFS-JP 標準ファイル名で順に処理
        for arcname in GTFS_JP_FILES:
            # 置換指定があれば実ファイル名はそれ
            real_name = subs.get(arcname, arcname)
            file_path = input_dir / real_name
            if file_path.exists():
                zf.write(file_path, arcname=arcname)
                if real_name != arcname:
                    included.append(f"{arcname}  ← {real_name}")
                else:
                    included.append(arcname)

        # 置換指定に出てきた arcname が標準リストに無い場合も含める
        # （標準外のファイル名を強制的に入れたいケース）
        for arcname, real_name in subs.items():
            if arcname in GTFS_JP_FILES:
                continue
            file_path = input_dir / real_name
            if file_path.exists():
                zf.write(file_path, arcname=arcname)
                included.append(f"{arcname}  ← {real_name} [非標準]")

    # 必須ファイルのチェック
    included_arcnames = {entry.split("  ← ")[0] for entry in included}
    for req in GTFS_REQUIRED:
        if req not in included_arcnames:
            warnings.append(f"必須ファイル {req} が欠落")
    if not (included_arcnames & GTFS_REQUIRED_CALENDAR):
        warnings.append(
            f"calendar.txt / calendar_dates.txt のいずれも欠落（最低1つ必要）"
        )

    return included, warnings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GTFS-JP ファイル群を zip にパッケージング",
    )
    parser.add_argument("input_dir", help="GTFS-JP CSV のあるディレクトリ")
    parser.add_argument("-o", "--output", required=True, help="出力 zip ファイルパス")
    parser.add_argument("--substitute", action="append", default=None, metavar="SRC=DEST",
                        help="入力 SRC を zip 内では DEST 名で梱包する。"
                             " 例: --substitute stops.merged.txt=stops.txt"
                             " 複数回指定可")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        print(f"Error: ディレクトリではない: {input_dir}", file=sys.stderr)
        return 1

    try:
        subs = parse_substitutions(args.substitute)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    output_zip = Path(args.output)
    included, warnings = package_zip(input_dir, output_zip, substitutions=subs)

    print(f"[OK] {len(included)} ファイルを zip に梱包: {output_zip}")
    for f in included:
        print(f"  - {f}")

    if warnings:
        print()
        print("⚠️ 警告:")
        for w in warnings:
            print(f"  - {w}")
        print("   （GTFS Validator でエラーになる可能性あり）")

    # サイズ表示
    if output_zip.exists():
        size = output_zip.stat().st_size
        print(f"\nファイルサイズ: {size:,} bytes ({size / 1024:.1f} KB)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
