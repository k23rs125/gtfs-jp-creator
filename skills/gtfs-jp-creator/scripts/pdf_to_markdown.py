"""
pdf_to_markdown.py
==================

Step 1: バス時刻表PDF / 画像 を構造化Markdown に変換する。

Engines:
    --engine text (default, 軽量・高速, pdfplumber/MIT):
        テキストPDF + シンプルなレイアウトに最適。テキストと表を Markdown 化する。
        装飾的レイアウト（カラー背景、並列テーブル等）には弱い→ mineru を推奨。
    --engine mineru (高品質、重い):
        ML レイアウト解析 + OCR を組み合わせ、装飾レイアウトでも
        テーブル構造を保持して抽出可能。CPU推論では数十分かかる。GPU環境では数分。

ライセンス方針: 自治体・公共交通事業者での導入を前提とするため、AGPL の
    PyMuPDF(fitz) / pymupdf4llm は使わず、テキスト抽出は permissive な pdfplumber(MIT)
    のみで実装する。画像PDFの OCR は外部CLI の MinerU（別途導入）を用いる。
    （旧 `--engine pymupdf4llm` は後方互換で受け付け、内部的に text と同義とする。）

Usage:
    python pdf_to_markdown.py <input.pdf> [-o output.md] [--engine ENGINE]

Examples:
    # デフォルト（text / pdfplumber）で高速処理
    python pdf_to_markdown.py timetable.pdf -o out.md

    # 装飾的なPDF・画像PDFには MinerU を使う（時間がかかる）
    python pdf_to_markdown.py timetable.pdf --engine mineru -o out.md

    # スキャン画像PDFを強制的にテキストPDFとして処理（text エンジンのみ）
    python pdf_to_markdown.py scanned.pdf --force-text

Dependencies:
    Required: pip install pdfplumber
    Optional: pip install -U "mineru[core]"  # --engine mineru 利用時

Status: Phase 1 (テキスト/装飾PDF対応, スキャン画像はPhase 2でolmOCR追加予定)

License: Apache 2.0
"""

import argparse
import logging
import shutil
import subprocess

# pdfplumber(pdfminer.six) が一部PDFで出す無害な FontBBox 警告を抑制（出力を汚さない）。
logging.getLogger("pdfminer").setLevel(logging.ERROR)
import sys
from pathlib import Path


# 1ページあたりの平均文字数がこれ未満なら画像PDFと推定（pymupdf4llm経路のみ使用）
MIN_TEXT_CHARS_PER_PAGE = 50


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------

def detect_pdf_type(pdf_path: Path) -> str:
    """PDFが「テキスト型」か「画像型」かを判定する (pdfplumber / MIT)。

    Returns:
        "text" or "image"
    """
    import pdfplumber

    total_text = ""
    with pdfplumber.open(str(pdf_path)) as pdf:
        page_count = len(pdf.pages)
        if page_count == 0:
            return "image"
        for page in pdf.pages:
            total_text += page.extract_text() or ""

    avg_chars = len(total_text.strip()) / page_count
    return "text" if avg_chars >= MIN_TEXT_CHARS_PER_PAGE else "image"


# ----------------------------------------------------------------------
# Engine: text (pdfplumber / MIT) ― 旧 pymupdf4llm(AGPL) の置換
# ----------------------------------------------------------------------

def _table_to_markdown(tbl) -> str:
    """pdfplumber の table（行×列の2次元リスト）を Markdown 表へ。"""
    rows = [[(c or "").replace("\n", " ").strip() for c in row]
            for row in tbl if any((c or "").strip() for c in row)]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * ncol) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def extract_with_text(pdf_path: Path) -> str:
    """テキスト型PDFを Markdown（テキスト＋表）に変換する (pdfplumber / MIT)。

    permissive ライセンスのみで完結（AGPL の pymupdf4llm を置換）。
    装飾的レイアウト・複雑な結合セルの表は苦手なため、そうした時刻表は
    --engine mineru（OCR）を推奨する。
    """
    import pdfplumber
    parts: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        multipage = len(pdf.pages) > 1
        for i, page in enumerate(pdf.pages, 1):
            if multipage:
                parts.append(f"## Page {i}")
            for tbl in page.extract_tables():
                md = _table_to_markdown(tbl)
                if md:
                    parts.append(md)
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(text)
    return ("\n\n".join(parts).strip() + "\n") if parts else ""


# ----------------------------------------------------------------------
# Engine: mineru
# ----------------------------------------------------------------------

def extract_with_mineru(pdf_path: Path, lang: str = "japan", keep_artifacts: bool = False,
                        backend: str = "pipeline", method: str = "ocr") -> str:
    """MinerU (CJK特化のMLレイアウト解析ツール) で抽出する。

    バックエンドの選択が重要（バス時刻表＝数字の表）:
      - pipeline (既定): PaddleOCRベース。数字の読みが正確で速い（1ページ ~3分）。
        実測: こがバス画像PDFで時刻アノマリ 0 件（hybridは14件）。時刻表はこちらが最適。
      - hybrid/vlm: 視覚言語モデル。一般文書の解析力は高いが、数字をもっともらしく
        読み違えやすく(列ずれ等)・CPUで非常に遅い（1ページ ~40分）。

    Args:
        pdf_path: 入力PDFパス
        lang: OCR言語コード（"japan" / "ch" / "en" 等）
        keep_artifacts: True なら一時ディレクトリの全出力を残す（デバッグ用）
        backend: mineru バックエンド（既定 pipeline）
        method: pipeline 時の解析法（auto/txt/ocr。画像PDFは ocr 既定）

    Returns:
        Markdown文字列

    Raises:
        RuntimeError: mineru CLI が見つからない、または実行失敗
    """
    if shutil.which("mineru") is None:
        raise RuntimeError(
            "mineru CLI が見つかりません。インストールしてください:\n"
            "  pip install -U \"mineru[core]\"\n"
            "Windows でCLIがPATHに登録されない場合は python -m mineru で代替可能です。"
        )

    # 一時出力ディレクトリ（PDFと同じ階層に作成）
    output_dir = pdf_path.parent / f"_mineru_tmp_{pdf_path.stem}"
    output_dir.mkdir(parents=True, exist_ok=True)

    slow = backend != "pipeline"
    print(f"[INFO] MinerU 実行中 (backend={backend})..."
          + ("(CPU推論で数十分かかる場合あり)" if slow else "(pipeline: 数分)"), file=sys.stderr)
    print(f"[INFO] 出力一時ディレクトリ: {output_dir}", file=sys.stderr)

    cmd = [
        "mineru", "-p", str(pdf_path),
        "-o", str(output_dir),
        "-b", backend,
        "--lang", lang,
    ]
    if backend == "pipeline":
        cmd += ["-m", method]   # method は pipeline/hybrid 系でのみ有効

    # stdout/stderr はキャプチャせず、進捗をユーザーに見せる
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"mineru の実行に失敗しました (exit code {result.returncode})")

    # 出力された .md ファイルを探す（mineruは深い階層に出す）
    md_files = sorted(output_dir.rglob("*.md"))
    if not md_files:
        raise RuntimeError(f"mineru の出力 .md が {output_dir} 内に見つかりませんでした")

    markdown = md_files[0].read_text(encoding="utf-8")

    # 一時ファイルのクリーンアップ
    if not keep_artifacts:
        shutil.rmtree(output_dir, ignore_errors=True)
        print(f"[INFO] 一時ディレクトリを削除しました", file=sys.stderr)
    else:
        print(f"[INFO] --keep-artifacts: 一時ディレクトリを保持しました ({output_dir})", file=sys.stderr)

    return markdown


# ----------------------------------------------------------------------
# Engine: olmOCR (Phase 2予定)
# ----------------------------------------------------------------------

def extract_with_olmocr(pdf_path: Path) -> str:
    """画像型PDFをOCRしてMarkdownに変換する (Phase 2 で実装予定)。"""
    raise NotImplementedError(
        f"olmOCR エンジンは Phase 2 で実装予定です。\n"
        f"代替として --engine mineru を使用してください（OCRも内蔵）。\n"
        f"あるいは --force-text で text(pdfplumber) 強制処理も可能です。"
    )


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="バス時刻表PDFをMarkdownに変換 (GTFS-JP Skill Step 1)",
    )
    parser.add_argument("input", help="入力PDFファイルパス")
    parser.add_argument("-o", "--output", help="出力Markdownパス (省略時は標準出力)")
    parser.add_argument(
        "--engine", choices=["text", "mineru", "pymupdf4llm"], default="text",
        help="抽出エンジンを選択 (デフォルト: text=pdfplumber/MIT)。"
             "mineru は高品質だが CPU 推論で数十分かかる。"
             "pymupdf4llm は後方互換の別名（内部的に text と同義）。",
    )
    parser.add_argument(
        "--force-text", action="store_true",
        help="(text時のみ) 自動判定をスキップしてテキスト型として処理",
    )
    parser.add_argument(
        "--lang", default="japan",
        help="OCR言語コード (mineruエンジン用、デフォルト: japan)",
    )
    parser.add_argument(
        "--keep-artifacts", action="store_true",
        help="(mineru時のみ) 一時ディレクトリ・中間ファイルを保持",
    )
    parser.add_argument(
        "--backend", choices=["pipeline", "vlm-auto-engine", "hybrid-auto-engine"],
        default="pipeline",
        help="(mineru時) バックエンド。既定 pipeline=数字に正確で速い（時刻表向き）。"
             "vlm/hybrid は一般文書向きだが数字を読み違えやすく遅い。",
    )
    parser.add_argument(
        "--method", choices=["auto", "txt", "ocr"], default="ocr",
        help="(mineru pipeline時) 解析法。画像PDFは ocr（既定）。",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: 入力ファイルが見つかりません: {input_path}", file=sys.stderr)
        sys.exit(1)

    if args.engine == "mineru":
        print(f"[INFO] エンジン: MinerU (backend={args.backend}, lang={args.lang})", file=sys.stderr)
        try:
            markdown = extract_with_mineru(
                input_path, lang=args.lang, keep_artifacts=args.keep_artifacts,
                backend=args.backend, method=args.method,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(3)
    else:
        # text エンジン (pdfplumber / MIT)。"pymupdf4llm" 指定も後方互換でここに来る。
        if args.force_text:
            pdf_type = "text"
            print("[INFO] --force-text 指定: テキスト型として処理", file=sys.stderr)
        else:
            pdf_type = detect_pdf_type(input_path)
            print(f"[INFO] エンジン: text(pdfplumber) | PDFタイプ判定: {pdf_type}", file=sys.stderr)

        if pdf_type == "text":
            markdown = extract_with_text(input_path)
        else:
            try:
                markdown = extract_with_olmocr(input_path)
            except NotImplementedError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(2)

    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
        print(f"[OK] 出力先: {args.output}", file=sys.stderr)
    else:
        print(markdown)


if __name__ == "__main__":
    main()
