"""
download_validator.py
=====================
MobilityData GTFS Validator の CLI 版 jar を最新リリースから自動取得して
`skills/gtfs-jp-creator/tools/gtfs-validator-cli.jar` に配置する。

利用者の準備負担を減らすための補助。jar は容量が大きく git に含めていないため、
これを一度実行すれば Step7 の検証が使えるようになる（要 Java 17+）。

- GitHub API で最新リリースの CLI jar アセットを特定（gui jar は除外）。
- validate_gtfs.py が使う引数（--input/--output_base/--country_code）は v4〜v8 で安定。
- 既に jar があればスキップ（--force で再取得）。失敗時は手動DL先を案内する。

Usage:
    python download_validator.py            # tools/ に最新CLI jarを配置
    python download_validator.py --force    # 既存があっても再取得
    python download_validator.py --out <path>

License: Apache 2.0
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

API = "https://api.github.com/repos/MobilityData/gtfs-validator/releases/latest"
RELEASES_PAGE = "https://github.com/MobilityData/gtfs-validator/releases"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "tools" / "gtfs-validator-cli.jar"


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "gtfs-jp-creator/1.0",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def find_cli_jar_url() -> tuple[str, str]:
    """最新リリースから (tag, cli_jar_download_url) を返す。"""
    data = json.loads(_get(API))
    tag = data.get("tag_name", "latest")
    assets = data.get("assets", [])
    # name が .jar で 'cli' を含み 'gui' を含まないものを選ぶ
    for a in assets:
        nm = (a.get("name") or "").lower()
        if nm.endswith(".jar") and "cli" in nm and "gui" not in nm:
            return tag, a["browser_download_url"]
    # 念のため: cli が無ければ gui でない最大の jar
    jars = [a for a in assets if (a.get("name") or "").lower().endswith(".jar")
            and "gui" not in (a.get("name") or "").lower()]
    if jars:
        jars.sort(key=lambda a: a.get("size", 0), reverse=True)
        return tag, jars[0]["browser_download_url"]
    raise RuntimeError("最新リリースに CLI jar が見つかりません")


def main() -> int:
    ap = argparse.ArgumentParser(description="MobilityData GTFS Validator の CLI jar を自動取得")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="配置先 jar パス（既定: tools/gtfs-validator-cli.jar）")
    ap.add_argument("--force", action="store_true", help="既存があっても再取得する")
    a = ap.parse_args()

    out = Path(a.out)
    if out.exists() and out.stat().st_size > 0 and not a.force:
        print(f"既に存在します（スキップ）: {out}\n  再取得は --force", file=sys.stderr)
        return 0
    try:
        tag, url = find_cli_jar_url()
        print(f"最新リリース {tag} の CLI jar を取得: {url}", file=sys.stderr)
        data = _get(url)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        print(f"配置しました: {out} ({len(data)/1048576:.1f}MB)", file=sys.stderr)
        print("確認: java -jar \"%s\" --help" % out, file=sys.stderr)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"Error: 自動取得に失敗しました（{type(e).__name__}: {e}）", file=sys.stderr)
        print(f"  手動で {RELEASES_PAGE} から *-cli.jar をDLし、", file=sys.stderr)
        print(f"  '{out}' に置いてください。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
