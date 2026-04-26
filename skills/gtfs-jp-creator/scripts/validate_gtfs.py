"""
validate_gtfs.py
================

Step 5a: MobilityData GTFS Validator (Java製) を呼び出して
         生成されたGTFS-JPデータをバリデーションする。

Validator JAR:
    https://github.com/MobilityData/gtfs-validator/releases
    Apache License 2.0

Usage:
    python validate_gtfs.py <gtfs_zip_or_dir> [-o <report_dir>]

Requirements:
    - Java 11+ JRE installed and on PATH
    - gtfs-validator-X.X.X-cli.jar 配置済み (デフォルト: ./tools/gtfs-validator-cli.jar)

Status: STUB (skeleton only - implementation TBD)

License: Apache 2.0
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_VALIDATOR_JAR = Path(__file__).parent.parent / "tools" / "gtfs-validator-cli.jar"


def check_java_installed() -> str | None:
    """Java の実行ファイルパスを返す。なければ None。"""
    return shutil.which("java")


def run_validator(input_path: Path, output_dir: Path, jar_path: Path) -> int:
    """gtfs-validator を Java で実行する。

    Returns:
        Validator の exit code
    """
    cmd = [
        "java", "-jar", str(jar_path),
        "--input", str(input_path),
        "--output_base", str(output_dir),
    ]
    # TODO: subprocess.run でcmdを実行 → 戻り値を返す
    raise NotImplementedError("run_validator")


def main():
    parser = argparse.ArgumentParser(description="Validate GTFS-JP feed using MobilityData Validator")
    parser.add_argument("input", help="Input GTFS zip file or directory")
    parser.add_argument("-o", "--output", default="./validation_report", help="Output report directory")
    parser.add_argument("--jar", default=str(DEFAULT_VALIDATOR_JAR), help="Path to gtfs-validator JAR")
    args = parser.parse_args()

    if check_java_installed() is None:
        print("Error: Java is not installed or not on PATH.", file=sys.stderr)
        print("Install Adoptium Temurin JRE 11+ from https://adoptium.net/", file=sys.stderr)
        sys.exit(1)

    jar_path = Path(args.jar)
    if not jar_path.exists():
        print(f"Error: Validator JAR not found: {jar_path}", file=sys.stderr)
        print("Download from https://github.com/MobilityData/gtfs-validator/releases", file=sys.stderr)
        sys.exit(1)

    exit_code = run_validator(Path(args.input), Path(args.output), jar_path)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
