#!/usr/bin/env python3
"""
apply_manual_readings.py
========================
Step 6 (手動読みオーバーライド): translations.txt の停留所名の「ふりがな(ja-Hrkt)」
「英訳(en)」を、人が確認した正しい読み・訳で上書きする。

generate_translations.py のふりがな生成(pykakasi)は難読地名を誤読することがある
（例: 壱町原 → いちまちはら が正しくは いっちょうばる）。再生成すると誤読に戻るため、
手修正を毎回やり直すのは非効率。本スクリプトで「正しい読み」を独立した手動ファイルに
持たせ、再生成のたびに最優先で上書きできるようにする。座標の apply_manual_coords.py と
同じ思想（手動が最優先・推測しない・マッチしない指定は警告＝正しく失敗）。

対象は translations.txt の table_name=stops 行のうち、language が ja-Hrkt / en のもの。
  - ja（原文）は元データのコピーで誤読が起きないため対象外。
  - translations.txt に stop_id 列は無いので、停留所名(=field_value)をキーに照合する。

手動読みファイル(JSON)の形式:
  {
    "by_stop_name": {
      "壱町原":       { "ja-Hrkt": "いっちょうばる",        "en": "Itchobaru" },
      "六町原公民館": { "ja-Hrkt": "ろくちょうばるこうみんかん", "en": "Rokuchobaru Community Center" },
      "江島納骨堂前": { "ja-Hrkt": "えしまのうこつどうまえ",  "en": "(in front of) Eshima Charnel House" }
    }
  }
  - 各停留所で ja-Hrkt / en は片方だけの指定も可（指定された言語だけ上書きする）。
  - ja-Hrkt / en 以外の言語キー（例: ja）は対象外として警告し、適用しない。

Usage:
  python apply_manual_readings.py <translations.txt> --readings <manual_readings.json> [-o <out.txt>]
  -o 省略時は入力 translations.txt を上書き。

License: Apache 2.0
"""
import argparse
import csv
import json
import sys
from pathlib import Path

# 上書き対象の言語（ja=原文は誤読が起きないため対象外）
TARGET_LANGS = ("ja-Hrkt", "en")
REQUIRED_COLS = ("table_name", "field_name", "language", "translation", "field_value")


def main():
    ap = argparse.ArgumentParser(
        description="translations.txt のふりがな(ja-Hrkt)・英訳(en)を手動読みで上書きする "
                    "(Step6 手動オーバーライド)")
    ap.add_argument("translations", help="入力 translations.txt")
    ap.add_argument("--readings", required=True, help="手動読みファイル(JSON)")
    ap.add_argument("-o", "--output", default=None,
                    help="出力 translations.txt（省略時は入力を上書き）")
    args = ap.parse_args()

    trans_path = Path(args.translations)
    out_path = Path(args.output) if args.output else trans_path

    # --- 手動読みファイル読み込み ---
    try:
        manual = json.loads(Path(args.readings).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ERROR] 手動読みファイルを読めません: {e}", file=sys.stderr)
        sys.exit(1)
    by_name = manual.get("by_stop_name", {}) or {}
    if not by_name:
        print("[WARN] 手動読みファイルに by_stop_name がありません。何も適用しません。",
              file=sys.stderr)

    # 対象外の言語キー（ja 等）が混ざっていれば警告（正しく失敗）。適用はしない。
    disallowed = []  # (stop_name, lang)
    for name, spec in by_name.items():
        if not isinstance(spec, dict):
            print(f"[ERROR] by_stop_name['{name}'] はオブジェクトである必要があります: {spec!r}",
                  file=sys.stderr)
            sys.exit(1)
        for lang in spec:
            if lang not in TARGET_LANGS:
                disallowed.append((name, lang))

    # --- translations.txt 読み込み ---
    # 入力の BOM 有無・改行コードを保存して書き戻す（不要な差分を生まない＝冪等性）。
    raw = trans_path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    with open(trans_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    missing_cols = [c for c in REQUIRED_COLS if c not in (fieldnames or [])]
    if missing_cols:
        print(f"[ERROR] translations.txt に必要な列がありません: {missing_cols} "
              f"(実際の列: {fieldnames})", file=sys.stderr)
        sys.exit(1)

    # --- 適用 ---
    # spec に書かれた (stop_name, lang) のうち、実際に該当行へ適用できたものを記録。
    applied = []        # (stop_name, lang, old, new)
    unchanged = []      # (stop_name, lang, value)  指定値が既存値と同じ（再実行で安定）
    applied_keys = set()  # (stop_name, lang)
    matched_names = set()  # field_value が手動指定に一致した停留所名

    for r in rows:
        if (r.get("table_name") or "").strip() != "stops":
            continue
        fv = (r.get("field_value") or "").strip()
        if fv not in by_name:
            continue
        matched_names.add(fv)
        lang = (r.get("language") or "").strip()
        if lang not in TARGET_LANGS:
            continue
        spec = by_name[fv]
        if lang not in spec:
            continue
        new_val = spec[lang]
        old_val = r.get("translation", "")
        if old_val == new_val:
            unchanged.append((fv, lang, new_val))
        else:
            r["translation"] = new_val
            applied.append((fv, lang, old_val, new_val))
        applied_keys.add((fv, lang))

    # --- マッチしなかった指定の検出（typo / 言語行欠落の検出。正しく失敗）---
    # (1) どの行の field_value にも一致しなかった停留所名
    unmatched_names = [n for n in by_name if n not in matched_names]
    # (2) 停留所名は一致したが、指定言語の行が無く適用できなかった (name, lang)
    missing_lang = []
    for name in matched_names:
        for lang in by_name[name]:
            if lang in TARGET_LANGS and (name, lang) not in applied_keys:
                missing_lang.append((name, lang))

    # --- 書き戻し（入力の BOM 有無・改行コードを踏襲）---
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding=("utf-8-sig" if has_bom else "utf-8"),
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, lineterminator=newline)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})

    # --- レポート ---
    print(f"[OK] 手動読みを適用: {len(applied)}件 → {out_path}", file=sys.stderr)
    for name, lang, old, new in applied:
        print(f"  - {name} [{lang}]: 「{old}」→「{new}」", file=sys.stderr)
    if unchanged:
        print(f"[INFO] 既に正しい値で据え置き: {len(unchanged)}件", file=sys.stderr)
        for name, lang, val in unchanged:
            print(f"  - {name} [{lang}]: 「{val}」", file=sys.stderr)

    # ふりがな/英語の整合確認: 適用・据え置いた停留所について現在の ja-Hrkt / en を並べて提示。
    touched = sorted(matched_names)
    if touched:
        current = {}  # name -> {lang: translation}
        for r in rows:
            if (r.get("table_name") or "").strip() == "stops":
                fv = (r.get("field_value") or "").strip()
                if fv in matched_names:
                    current.setdefault(fv, {})[(r.get("language") or "").strip()] = r.get("translation", "")
        print(f"[INFO] 整合確認（対象 {len(touched)}停留所の現在値）:", file=sys.stderr)
        for name in touched:
            cur = current.get(name, {})
            print(f"  - {name}: ja-Hrkt「{cur.get('ja-Hrkt','(無)')}」 / en「{cur.get('en','(無)')}」",
                  file=sys.stderr)

    if disallowed:
        print(f"[WARN] 対象外の言語が指定されています（{', '.join(TARGET_LANGS)} のみ上書き可。"
              f"適用しません）:", file=sys.stderr)
        for name, lang in disallowed:
            print(f"  - {name}: {lang}", file=sys.stderr)
    if unmatched_names:
        print(f"[WARN] 手動読みファイルの停留所名が translations.txt に見つかりません"
              f"（typo か表記揺れの可能性。確認してください）:", file=sys.stderr)
        for n in unmatched_names:
            print(f"  - {n}", file=sys.stderr)
    if missing_lang:
        print(f"[WARN] 停留所名は一致しましたが、指定言語の行が translations.txt にありません:",
              file=sys.stderr)
        for name, lang in missing_lang:
            print(f"  - {name} [{lang}]", file=sys.stderr)


if __name__ == "__main__":
    main()
