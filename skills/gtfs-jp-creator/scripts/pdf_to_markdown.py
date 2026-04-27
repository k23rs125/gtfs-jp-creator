"""
pdf_to_markdown.py
==================

Step 1: バス時刻表PDF / 画像 を構造化Markdown に変換する。

Strategy (Phase 1):
    - テキストPDF (selectable text) は pymupdf4llm を使う
    - 画像PDF (スキャン画像) は Phase 2 で olmOCR を実装予定

Usage:
    python pdf_to_markdown.py <input.pdf> [-o output.md] [--force-text]

Examples:
    python pdf_to_markdown.py timetable.pdf
    python pdf_to_markdown.py timetable.pdf -o out.md
    python pdf_to_markdown.py scanned.pdf --force-text   # 自動判定をスキップ

Dependencies:
    pip install pymupdf pymupdf4llm

Status: Phase 1 (テキストPDF対応, OCR未対応)

License: Apache 2.0
"""

import argparse
import sys
from pathlib import Path


# 判定の閾値: 1ページあたりの平均文字数がこれ未満なら画像PDFと推定
MIN_TEXT_CHARS_PER_PAGE = 50


def detect_pdf_type(pdf_path: Path) -> str:
    """PDFが「テキスト型」か「画像型」かを判定する。

    Strategy: PyMuPDF (fitz) で各ページのテキスト量を確認。
    1ページあたりの平均文字数が閾値未満なら画像型と判定。

    Returns:
        "text" or "image"
    """
    import pymupdf  # PyMuPDF >= 1.24.4 (旧 fitz)

    doc = pymupdf.open(pdf_path)
    page_count = len(doc)
    if page_count == 0:
        doc.close()
        return "image"  # 空のPDFは画像扱い

    total_text = ""
    for page in doc:
        total_text += page.get_text()
    doc.close()

    avg_chars = len(total_text.strip()) / page_count
    return "text" if avg_chars >= MIN_TEXT_CHARS_PER_PAGE else "image"


def extract_with_pymupdf4llm(pdf_path: Path) -> str:
    """テキスト型PDFを構造化Markdownに変換する (pymupdf4llm)。

    pymupdf4llm はLLM/RAG向けにフォーマット保持されたMarkdownを生成する。
    表(Table)もMarkdown table形式で抽出される。
    """
    import pymupdf4llm
    md_text = pymupdf4llm.to_markdown(str(pdf_path))
    return md_text


def extract_with_olmocr(pdf_path: Path) -> str:
    """画像型PDFをOCRしてMarkdownに変換する。

    Status: Phase 2で実装予定。現状は明示的にエラー。
    """
    raise NotImplementedError(
        f"画像型PDFが検出されました: {pdf_path}\n"
        "OCRサポート(olmOCR)はPhase 2で実装予定です。\n"
        "暫定対処: テキスト埋め込み版で再生成可能なら、それを使ってください。\n"
        "あるいは --force-text を指定して pymupdf4llm で強制処理できます。"
    )


def main():
    parser = argparse.ArgumentParser(
        description="バス時刻表PDFをMarkdownに変換 (GTFS-JP Skill Step 1)",
    )
    parser.add_argument("input", help="入力PDFファイルパス")
    parser.add_argument("-o", "--output", help="出力Markdownパス (省略時は標準出力)")
    parser.add_argument(
        "--force-text", action="store_true",
        help="自動判定をスキップしてpymupdf4llmで強制変換",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: 入力ファイルが見つかりません: {input_path}", file=sys.stderr)
        sys.exit(1)

    if args.force_text:
        pdf_type = "text"
        print("[INFO] --force-text 指定: テキスト型として処理", file=sys.stderr)
    else:
        pdf_type = detect_pdf_type(input_path)
        print(f"[INFO] PDFタイプ判定: {pdf_type}", file=sys.stderr)

    if pdf_type == "text":
        markdown = extract_with_pymupdf4llm(input_path)
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
