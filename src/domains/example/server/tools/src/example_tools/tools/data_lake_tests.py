"""
Test tools for validating data lake asset resolution.

These tools test the data lake asset pattern where qualified names
(like abfss://, https://, mssql://) are automatically resolved,
cached to disk, and injected as Path objects.

Example Usage:
-------------

To test data lake asset resolution, call inspect_asset with a qualified_name:

    result = inspect_asset(
        asset="https://storage.blob.core.windows.net/container/data.nc"
    )

The system will:
1. Detect the qualified_name pattern in the string
2. Fetch and cache the asset to disk
3. Inject a Path object pointing to the cached file
4. Tool can inspect or load the Path as needed

Example Result:
{
    "type": "pathlib.PosixPath",
    "value_summary": "data.nc (2.67 MB)",
    "details": {
        "file_name": "e1332782972090b4cedc2be5a6053392444bfbdd2d8181a8b21226d8c34fa579.nc",
        "file_size": 2796761,
        "file_exists": true,
        "suffix": ".nc"
    }
}
"""

from pathlib import Path
from typing import Any, Union
import logging

LOGGER = logging.getLogger(__name__)


def inspect_asset(asset: Union[Path, Any]) -> dict:
    """
    Inspect a data lake asset and return its type and value information.

    Tests: Data lake asset resolution and injection.

    When called with a qualified_name string (e.g., "https://..."),
    the system automatically fetches it, caches to disk, and passes
    the Path here. This tool inspects the Path without loading the file.

    Args:
        asset: Path to cached asset file, or any Python object

    Returns:
        Dict with 'type', 'value_summary', and 'details' keys
    """
    # If we received a Path, inspect it directly (don't load)
    if isinstance(asset, Path):
        return _inspect_path(asset)

    # For non-Path objects, inspect normally
    asset_type = type(asset).__name__
    module = type(asset).__module__
    full_type = f"{module}.{asset_type}" if module != "builtins" else asset_type

    # Get value summary based on type
    value_summary = _get_value_summary(asset, asset_type)

    # Get detailed information based on type
    details = _get_asset_details(asset, asset_type)

    return {
        "type": full_type,
        "value_summary": value_summary,
        "details": details,
    }


def _inspect_path(path: Path) -> dict:
    """Inspect a Path object without loading the file."""
    asset_type = type(path).__name__
    module = type(path).__module__
    full_type = f"{module}.{asset_type}"

    # Get file info
    file_exists = path.exists()
    file_size = path.stat().st_size if file_exists else 0

    # Format size nicely
    if file_size < 1024:
        size_str = f"{file_size} B"
    elif file_size < 1024 * 1024:
        size_str = f"{file_size / 1024:.2f} KB"
    elif file_size < 1024 * 1024 * 1024:
        size_str = f"{file_size / (1024 * 1024):.2f} MB"
    else:
        size_str = f"{file_size / (1024 * 1024 * 1024):.2f} GB"

    value_summary = f"{path.name} ({size_str})" if file_exists else f"{path.name} (not found)"

    details = {
        "file_name": path.name,
        "file_size": file_size,
        "file_exists": file_exists,
        "suffix": path.suffix,
        "absolute_path": str(path.absolute()),
    }

    return {
        "type": full_type,
        "value_summary": value_summary,
        "details": details,
    }


def _get_value_summary(asset: Any, asset_type: str) -> str:
    """Generate a short summary of the asset value."""
    try:
        # Handle dict
        if asset_type == "dict":
            return f"{len(asset)} keys"

        # Handle list/tuple
        elif asset_type in ("list", "tuple"):
            return f"{len(asset)} items"

        # Handle primitive types
        elif asset_type in ("int", "float", "bool"):
            return str(asset)

        # Handle string
        elif asset_type == "str":
            preview = asset[:50] + "..." if len(asset) > 50 else asset
            return f"{len(asset)} characters: {preview}"

        # Default: try to get length or string representation
        else:
            try:
                return f"{len(asset)} items"
            except TypeError:
                return str(asset)[:100]
    except Exception:
        LOGGER.debug("Could not generate asset summary", exc_info=True)
        return "Unable to generate summary"


def _get_asset_details(asset: Any, asset_type: str) -> dict:
    """Extract detailed information about the asset."""
    details = {}

    try:
        # Handle dict
        if asset_type == "dict":
            details["keys"] = list(asset.keys())
            if len(asset) > 0:
                first_key = next(iter(asset))
                details["sample_entry"] = {first_key: asset[first_key]}

        # Handle list/tuple
        elif asset_type in ("list", "tuple"):
            if len(asset) > 0:
                details["first_item_type"] = type(asset[0]).__name__
                details["sample_item"] = str(asset[0])[:100]

        # For other types, just store string representation
        else:
            details["str_representation"] = str(asset)[:200]

    except Exception as e:
        details["error"] = f"Could not extract details: {str(e)}"

    return details
