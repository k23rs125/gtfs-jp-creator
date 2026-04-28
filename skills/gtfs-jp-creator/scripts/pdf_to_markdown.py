"""
pdf_to_markdown.py
==================

Step 1: バス時刻表PDF / 画像 を構造化Markdown に変換する。

Engines:
    --engine pymupdf4llm (default, 軽量・高速):
        テキストPDF + シンプルなレイアウトに最適。
        装飾的レイアウト（カラー背景、並列テーブル等）には弱い。
    --engine mineru (高品質、重い):
        ML レイアウト解析 + OCR を組み合わせ、装飾レイアウトでも
        テーブル構造を保持して抽出可能。CPU推論では数十分かかる。
        GPU環境では数分。

Usage:
    python pdf_to_markdown.py <input.pdf> [-o output.md] [--engine ENGINE]

Examples:
    # デフォルト（pymupdf4llm）で高速処理
    python pdf_to_markdown.py timetable.pdf -o out.md

    # 装飾的なPDFには MinerU を使う（時間がかかる）
    python pdf_to_markdown.py timetable.pdf --engine mineru -o out.md

    # スキャン画像PDFを強制的にテキストPDFとして処理（pymupdf4llmのみ）
    python pdf_to_markdown.py scanned.pdf --force-text

Dependencies:
    Required: pip install pymupdf pymupdf4llm
    Optional: pip install -U "mineru[core]"  # --engine mineru 利用時

Status: Phase 1 (テキスト/装飾PDF対応, スキャン画像はPhase 2でolmOCR追加予定)

License: Apache 2.0
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


# 1ページあたりの平均文字数がこれ未満なら画像PDFと推定（pymupdf4llm経路のみ使用）
MIN_TEXT_CHARS_PER_PAGE = 50


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------

def detect_pdf_type(pdf_path: Path) -> str:
    """PDFが「テキスト型」か「画像型」かを判定する (PyMuPDF)。

    Returns:
        "text" or "image"
    """
    import pymupdf

    doc = pymupdf.open(pdf_path)
    page_count = len(doc)
    if page_count == 0:
        doc.close()
        return "image"

    total_text = ""
    for page in doc:
        total_text += page.get_text()
    doc.close()

    avg_chars = len(total_text.strip()) / page_count
    return "text" if avg_chars >= MIN_TEXT_CHARS_PER_PAGE else "image"


# ----------------------------------------------------------------------
# Engine: pymupdf4llm
# ----------------------------------------------------------------------

def extract_with_pymupdf4llm(pdf_path: Path) -> str:
    """テキスト型PDFを構造化Markdownに変換する (pymupdf4llm)。

    軽量・高速だが、装飾的レイアウトでは品質が低下する場合がある。
    """
    import pymupdf4llm
    return pymupdf4llm.to_markdown(str(pdf_path))


# ----------------------------------------------------------------------
# Engine: mineru
# ----------------------------------------------------------------------

def extract_with_mineru(pdf_path: Path, lang: str = "japan", keep_artifacts: bool = False) -> str:
    """MinerU (CJK特化のMLレイアウト解析ツール) で抽出する。

    装飾的なレイアウト・並列テーブル・日本語OCRに強い。
    CPU推論では非常に遅い (2ページPDFで~50分の実測例あり)。
    GPU環境では数分。

    Args:
        pdf_path: 入力PDFパス
        lang: OCR言語コード（"japan" / "ch" / "en" 等）
        keep_artifacts: True なら一時ディレクトリの全出力を残す（デバッグ用）

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

    print(f"[INFO] MinerU 実行中... (CPU推論で数十分かかる場合あり)", file=sys.stderr)
    print(f"[INFO] 出力一時ディレクトリ: {output_dir}", file=sys.stderr)

    cmd = [
        "mineru", "-p", str(pdf_path),
        "-o", str(output_dir),
        "--lang", lang,
    ]

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
        f"あるいは --force-text で pymupdf4llm 強制処理も可能です。"
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
        "--engine", choices=["pymupdf4llm", "mineru"], default="pymupdf4llm",
        help="抽出エンジンを選択 (デフォルト: pymupdf4llm)。"
             "mineru は高品質だが CPU 推論で数十分かかる。",
    )
    parser.add_argument(
        "--force-text", action="store_true",
        help="(pymupdf4llm時のみ) 自動判定をスキップしてテキスト型として処理",
    )
    parser.add_argument(
        "--lang", default="japan",
        help="OCR言語コード (mineruエンジン用、デフォルト: japan)",
    )
    parser.add_argument(
        "--keep-artifacts", action="store_true",
        help="(mineru時のみ) 一時ディレクトリ・中間ファイルを保持",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: 入力ファイルが見つかりません: {input_path}", file=sys.stderr)
        sys.exit(1)

    if args.engine == "mineru":
        print(f"[INFO] エンジン: MinerU (lang={args.lang})", file=sys.stderr)
        try:
            markdown = extract_with_mineru(
                input_path, lang=args.lang, keep_artifacts=args.keep_artifacts,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(3)
    else:
        # pymupdf4llm
        if args.force_text:
            pdf_type = "text"
            print("[INFO] --force-text 指定: テキスト型として処理", file=sys.stderr)
        else:
            pdf_type = detect_pdf_type(input_path)
            print(f"[INFO] エンジン: pymupdf4llm | PDFタイプ判定: {pdf_type}", file=sys.stderr)

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
