# -*- coding: utf-8 -*-
"""停留所名の表記ゆれ（同名別表記）を検出し、統合するための決定的ユーティリティ。

OCRや原本の揺れで1つの停留所が「リーパスプラザこが／リーバスプラザこが」のように
別名で割れると、別 stop_id になり路線網・時刻・運賃・座標が崩れる。ここでは
候補を検出するだけで、統合するかは人が確定する（似ていて別物＝東口/西口 もあるため）。

- detect_variants(names): 近い名前のグループを返す（空白/表記差、1文字違い）。
- apply_merges(extract, mapping): extract の停留所名を canonical に置換（cells/stops）。

CLI: python stop_name_merge.py extract.json            # 候補を表示
     python stop_name_merge.py extract.json --apply merges.json -o extract_fixed.json
       merges.json = {"variant名": "正規名", ...}
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path


def _norm(s: str) -> str:
    """比較用の正規化：NFKC＋空白除去（全角/半角スペース・記号ゆれを吸収）。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    return re.sub(r"\s+", "", s)


# 視覚的に紛らわしい文字（OCRが取り違えやすい）を代表字に寄せる。
# ここに無い『上/下』『東/西』等の意味が異なる文字は同一視しない＝別停留所は統合候補にしない。
_CONFUSE = {"口": "ロ", "力": "カ", "工": "エ", "一": "ー", "―": "ー", "ｰ": "ー", "ﾛ": "ロ"}


def _strip_marks(s: str) -> str:
    """濁点・半濁点を除去（パ/バ/ハ → ハ）。OCRが取り違えやすい濁点差を吸収。"""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _canon_key(s: str) -> str:
    """同一視キー：空白除去＋濁点除去＋視覚的紛らわしさの代表字化。"""
    s = _norm(s)
    s = _strip_marks(s)
    return "".join(_CONFUSE.get(c, c) for c in s)


def detect_variants(names, min_len: int = 3):
    """表記ゆれのグループを返す。[{names, reason, norm_equal}]。
    濁点/半濁点や視覚的に紛らわしい文字だけを同一視し、意味の違う文字(上/下・東/西 等)は
    統合候補にしない＝誤って別停留所を混ぜない。"""
    uniq = sorted({n for n in names if n and str(n).strip()})
    by_key = {}
    for n in uniq:
        k = _canon_key(n)
        if len(k) < min_len:
            continue
        by_key.setdefault(k, []).append(n)
    groups = []
    for grp in by_key.values():
        if len(grp) > 1:
            all_norm = len({_norm(x) for x in grp}) == 1
            reason = "空白/表記差（ほぼ確実に同じ）" if all_norm else "濁点/文字ゆれ（OCR誤読の疑い）"
            groups.append({"names": sorted(grp), "reason": reason, "norm_equal": all_norm})
    return groups


def apply_merges(extract: dict, mapping: dict) -> int:
    """extract の停留所名を mapping(variant->canonical) で置換。置換件数を返す。"""
    n = 0
    for b in extract.get("blocks", []):
        for s in b.get("stops", []):
            if s.get("name") in mapping:
                s["name"] = mapping[s["name"]]; n += 1
        for t in b.get("trips", []):
            for c in t.get("cells", []):
                if c.get("name") in mapping:
                    c["name"] = mapping[c["name"]]; n += 1
    return n


def all_stop_names(extract: dict):
    names = set()
    for b in extract.get("blocks", []):
        for s in b.get("stops", []):
            if s.get("name"):
                names.add(s["name"])
        for t in b.get("trips", []):
            for c in t.get("cells", []):
                if c.get("name"):
                    names.add(c["name"])
    return names


def main():
    ap = argparse.ArgumentParser(description="停留所名の表記ゆれ検出／統合")
    ap.add_argument("extract", help="extract.json")
    ap.add_argument("--apply", default=None, help="統合マップJSON {variant: canonical}")
    ap.add_argument("-o", "--output", default=None, help="--apply時の出力 extract.json")
    a = ap.parse_args()
    ext = json.loads(Path(a.extract).read_text(encoding="utf-8"))
    if a.apply:
        mapping = json.loads(Path(a.apply).read_text(encoding="utf-8"))
        n = apply_merges(ext, mapping)
        out = a.output or a.extract
        Path(out).write_text(json.dumps(ext, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] {n} 箇所を統合して出力: {out}")
        return 0
    groups = detect_variants(all_stop_names(ext))
    print(f"[OK] 表記ゆれ候補 {len(groups)} グループ")
    for g in groups:
        print(f"  ・{g['reason']}: " + " ／ ".join(g["names"]))
    if not groups:
        print("  （候補なし）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
