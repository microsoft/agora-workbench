"""
Excel extraction tools.

All functions receive a ``file`` parameter that is a ``Path`` to a cached
Excel file on disk (resolved by AssetResolutionMiddleware). IRM decryption
has already been applied transparently.
"""

import logging
from pathlib import Path
from typing import Any, Optional, Union

import olefile
import pandas as pd

_logger = logging.getLogger(__name__)

# OLE2 magic header — present in legacy .xls AND in IRM-protected files
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _detect_engine(file: Path) -> Optional[str]:
    """
    Choose the pandas Excel engine based on file content, not just extension.

    Returns ``None`` to let pandas auto-detect, or an explicit engine name.
    Raises ``ValueError`` if the file appears to still be IRM-encrypted.
    """
    with open(file, "rb") as f:
        header = f.read(8)

    if header[:4] == b"PK\x03\x04":
        # ZIP-based OOXML (.xlsx / .xlsm) — always use openpyxl
        return "openpyxl"

    if header == _OLE2_MAGIC:
        # OLE2 container — could be a legacy .xls workbook or an
        # IRM-encrypted file (any Office format).  Use olefile to
        # distinguish the two when available.
        if olefile.isOleFile(str(file)):
            with olefile.OleFileIO(str(file)) as ole:
                if ole.exists("\x09DRMContent"):
                    raise ValueError(
                        f"File {file.name} is still IRM-encrypted. "
                        "IRM decryption may not have run — check that "
                        "the server has valid Azure RMS credentials."
                    )
        return "xlrd"

    # Unknown format — let pandas figure it out
    return None


def read_excel_sheets(file: Union[Path, Any]) -> dict:
    """
    List all sheet names in an Excel workbook.

    Args:
        file: Path to the Excel file.

    Returns:
        Dict with ``sheets`` (list of names) and ``sheet_count``.
    """
    file = Path(file)
    engine = _detect_engine(file)
    xls = pd.ExcelFile(file, engine=engine)
    return {
        "sheets": xls.sheet_names,
        "sheet_count": len(xls.sheet_names),
    }


def extract_excel_data(
    file: Union[Path, Any],
    sheet_name: Optional[str] = None,
    max_rows: int = 1000,
) -> dict:
    """
    Extract data from a specific sheet as a table.

    Args:
        file:       Path to the Excel file.
        sheet_name: Sheet to read (default: first sheet).
        max_rows:   Maximum rows to return (-1 for all).

    Returns:
        Dict with ``columns``, ``rows`` (list of dicts), ``total_rows``, and
        ``truncated`` flag.
    """
    file = Path(file)
    engine = _detect_engine(file)
    df = pd.read_excel(file, sheet_name=sheet_name or 0, engine=engine)
    total_rows = len(df)

    truncated = False
    if max_rows >= 0 and total_rows > max_rows:
        df = df.head(max_rows)
        truncated = True

    # Convert to JSON-safe types
    rows = df.where(df.notna(), None).to_dict(orient="records")

    return {
        "columns": list(df.columns),
        "rows": rows,
        "total_rows": total_rows,
        "truncated": truncated,
    }


def _get_sheet_row_count(xls: pd.ExcelFile, sheet_name: str) -> int:
    """Return the data-row count for *sheet_name* without reading all cells."""
    book = xls.book
    # openpyxl
    if hasattr(book, "sheetnames"):
        ws = book[sheet_name]
        if ws.max_row is not None:
            return max(ws.max_row - 1, 0)  # subtract header row
    # xlrd
    elif hasattr(book, "sheet_by_name"):
        ws = book.sheet_by_name(sheet_name)
        return max(ws.nrows - 1, 0)  # subtract header row
    # Fallback for unknown engines: full read
    return len(pd.read_excel(xls, sheet_name=sheet_name))


def get_excel_metadata(file: Union[Path, Any]) -> dict:
    """
    Get metadata about an Excel workbook.

    Args:
        file: Path to the Excel file.

    Returns:
        Dict with ``file_size_bytes`` and ``sheets`` (list of per-sheet info).
    """
    file = Path(file)
    engine = _detect_engine(file)
    xls = pd.ExcelFile(file, engine=engine)

    sheets_info = []
    for name in xls.sheet_names:
        # Small sample for column names and dtype inference; row count via engine
        df_sample = pd.read_excel(xls, sheet_name=name, nrows=100)
        row_count = _get_sheet_row_count(xls, name)
        sheets_info.append(
            {
                "name": name,
                "row_count": row_count,
                "column_count": len(df_sample.columns),
                "columns": [{"name": str(col), "dtype": str(df_sample[col].dtype)} for col in df_sample.columns],
            }
        )

    return {
        "file_size_bytes": file.stat().st_size,
        "sheets": sheets_info,
    }
