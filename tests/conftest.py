# -*- coding: utf-8 -*-
"""pytest 共通設定。

抽出済みモジュールを import できるよう、テストから見えるパスを通す。
（reading_dictionary は skills/gtfs-jp-creator/scripts/ にある単独モジュール）
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "skills" / "gtfs-jp-creator" / "scripts", _ROOT / "app"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
