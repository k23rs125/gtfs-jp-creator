"""
pdf_to_markdown.py
==================

Step 1: バス時刻表PDF / 画像 を構造化Markdown に変換する。

Strategy:
    - テキストPDF (selectable text) は pymupdf4llm を使う
    - スキャン画像PDF は olmOCR を使う
    - 自動判定: PDFのテキスト抽出長が閾値以下なら olmOCR にフォールバック

Usage:
    python pdf_to_markdown.py <input.pdf> [-o output.md]

Status: STUB (skeleton only - implementation TBD)

License: Apache 2.0
"""

import argparse
import sys
from pathlib import Path


def detect_pdf_type(pdf_path: Path) -> str:
    """PDFが「テキスト型」か「画像型」かを判定する。

    Returns:
        "text" or "image"
    """
    # TODO: pymupdf でテキスト抽出を試みて、得られた文字数で判定
    raise NotImplementedError("detect_pdf_type")


def extract_with_pymupdf4llm(pdf_path: Path) -> str:
    """テキスト型PDFをMarkdownに変換する (pymupdf4llm)。"""
    # TODO: import pymupdf4llm; return pymupdf4llm.to_markdown(pdf_path)
    raise NotImplementedError("extract_with_pymupdf4llm")


def extract_with_olmocr(pdf_path: Path) -> str:
    """画像型PDFをOCRしてMarkdownに変換する (olmOCR)。"""
    # TODO: olmOCRでページごとにOCR → 結果を結合してMarkdown化
    raise NotImplementedError("extract_with_olmocr")


def main():
    parser = argparse.ArgumentParser(description="Convert bus timetable PDF/image to Markdown")
    parser.add_argument("input", help="Input PDF file path")
    parser.add_argument("-o", "--output", help="Output Markdown file path (default: stdout)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    pdf_type = detect_pdf_type(input_path)
    if pdf_type == "text":
        markdown = extract_with_pymupdf4llm(input_path)
    else:
        markdown = extract_with_olmocr(input_path)

    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
        print(f"[OK] Written to {args.output}")
    else:
        print(markdown)


if __name__ == "__main__":
    main()
