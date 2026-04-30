"""
PDF document extraction tools.

All functions receive a ``file`` parameter that is a ``Path`` to a cached
PDF file on disk (resolved by AssetResolutionMiddleware).
"""

from pathlib import Path
from typing import Any, Union


import pymupdf


def _sanitize_df(df) -> list[dict]:
    """Convert a DataFrame to a list of row dicts with JSON-safe Python types."""
    clean = df.where(df.notna(), None)
    rows = clean.to_dict(orient="records")
    for row in rows:
        for k, v in row.items():
            if v is not None and not isinstance(v, (str, int, float, bool)):
                row[k] = str(v)
    return rows


def extract_pdf_text(file: Union[Path, Any]) -> dict:
    """
    Extract all text from a PDF document.

    Args:
        file: Path to the PDF file.

    Returns:
        Dict with ``text`` (all pages joined by newlines), ``page_count``,
        and ``pages`` (list of per-page text).
    """
    file = Path(file)
    with pymupdf.open(str(file)) as doc:
        pages = [page.get_text() for page in doc]
    return {
        "text": "\n".join(pages),
        "page_count": len(pages),
        "pages": [{"page_number": i + 1, "text": t} for i, t in enumerate(pages)],
    }


def extract_pdf_tables(file: Union[Path, Any]) -> dict:
    """
    Extract tables from a PDF document.

    Uses PyMuPDF's built-in table detection to find and extract tabular data.

    Args:
        file: Path to the PDF file.

    Returns:
        Dict with ``tables`` (list of table dicts per page) and ``table_count``.
    """
    file = Path(file)
    with pymupdf.open(str(file)) as doc:
        all_tables = []
        for page_num, page in enumerate(doc):
            tabs = page.find_tables()
            for tab in tabs:
                df = tab.to_pandas()
                headers = [str(c) for c in df.columns]
                rows = _sanitize_df(df)
                all_tables.append(
                    {
                        "page_number": page_num + 1,
                        "headers": headers,
                        "rows": rows,
                    }
                )
    return {
        "tables": all_tables,
        "table_count": len(all_tables),
    }


def extract_pdf_markdown(file: Union[Path, Any]) -> dict:
    """
    Extract PDF content as Markdown-formatted text.

    Preserves headings, lists, tables, and text structure as Markdown,
    making the output well-suited for LLM consumption.

    Args:
        file: Path to the PDF file.

    Returns:
        Dict with ``markdown`` (full document as Markdown) and ``page_count``.
    """
    file = Path(file)
    with pymupdf.open(str(file)) as doc:
        pages_md = [page.get_text("markdown") for page in doc]
    return {
        "markdown": "\n\n---\n\n".join(pages_md),
        "page_count": len(pages_md),
    }


def get_pdf_metadata(file: Union[Path, Any]) -> dict:
    """
    Get metadata about a PDF file.

    Args:
        file: Path to the PDF file.

    Returns:
        Dict with ``page_count``, ``metadata`` (title, author, etc.),
        and ``file_size_bytes``.
    """
    file = Path(file)
    with pymupdf.open(str(file)) as doc:
        metadata = doc.metadata or {}
        page_count = doc.page_count
    return {
        "page_count": page_count,
        "metadata": metadata,
        "file_size_bytes": file.stat().st_size,
    }
