"""
Asset provisioner for large artifacts (model weights, data files).

Downloads or copies assets defined in ``EnvironmentConfig.assets`` into the
environment cache directory.  Supports multiple source URI schemes and
checksum-based skip-if-present logic to avoid redundant transfers.
"""

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .code_execution_models import AssetSpec, EnvironmentConfig

LOGGER = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MULTIPLIER = 4.0  # 2s, 8s, 32s

# Default timeout per asset (seconds) when size_hint_mb is not provided
DEFAULT_TIMEOUT_SECONDS = 600

# Bytes per MB for timeout estimation (allow ~1 MB/s as conservative baseline)
TIMEOUT_SECONDS_PER_MB = 3


def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _timeout_for_asset(asset: "AssetSpec") -> float:
    """Compute a reasonable timeout for fetching an asset."""
    if asset.size_hint_mb:
        return max(DEFAULT_TIMEOUT_SECONDS, asset.size_hint_mb * TIMEOUT_SECONDS_PER_MB)
    return DEFAULT_TIMEOUT_SECONDS


def _parse_source_scheme(source: str) -> str:
    """Determine the fetch scheme from a source URI string."""
    if source.startswith("az://"):
        return "az"
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        return "https"
    if parsed.scheme == "file":
        return "file"
    # Bare path (no scheme) — treat as local file
    if not parsed.scheme or parsed.scheme == "":
        return "file"
    return parsed.scheme


def _resolve_local_path(source: str) -> Path:
    """Resolve a file:// URI or bare path to a local Path."""
    if source.startswith("file://"):
        parsed = urlparse(source)
        return Path(parsed.path)
    return Path(source)


async def _fetch_https(source: str, dest: Path, timeout: float) -> None:
    """Download a file via HTTP(S) with streaming."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async with client.stream("GET", source) as response:
            response.raise_for_status()
            with open(tmp, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)

    tmp.rename(dest)


async def _fetch_azure_blob(source: str, dest: Path, timeout: float) -> None:
    """Download a blob from Azure Blob Storage.

    Source format: az://<container_name>/<blob_path>
    Uses DefaultAzureCredential for authentication (supports managed identity).
    """
    import os

    from azure.identity.aio import DefaultAzureCredential
    from azure.storage.blob.aio import BlobServiceClient

    # Parse az://<container>/<blob_path>
    # Requires AZURE_STORAGE_ACCOUNT_URL env var (e.g., https://<account>.blob.core.windows.net)
    stripped = source[len("az://") :]
    parts = stripped.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid az:// URI — expected az://<container>/<blob_path>, got: {source}")

    container_name, blob_path = parts

    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if not account_url:
        raise RuntimeError(
            "AZURE_STORAGE_ACCOUNT_URL environment variable is required for az:// asset sources "
            "(e.g., https://<account>.blob.core.windows.net)"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")

    credential = DefaultAzureCredential()
    try:
        async with BlobServiceClient(account_url=account_url, credential=credential) as blob_service:
            blob_client = blob_service.get_blob_client(container=container_name, blob=blob_path)
            with open(tmp, "wb") as f:
                stream = await blob_client.download_blob()
                async for chunk in stream.chunks():
                    f.write(chunk)
    finally:
        await credential.close()

    tmp.rename(dest)


async def _fetch_local(source: str, dest: Path) -> None:
    """Copy a local file to the destination."""
    src_path = _resolve_local_path(source)
    if not src_path.exists():
        raise FileNotFoundError(f"Local asset source not found: {src_path}")

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Run copy in thread pool to avoid blocking the event loop on large files
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, shutil.copy2, str(src_path), str(dest))


async def _fetch_single_asset(asset: "AssetSpec", cache_dir: Path) -> None:
    """Fetch a single asset with retry logic."""
    dest = cache_dir / asset.destination
    timeout = _timeout_for_asset(asset)
    scheme = _parse_source_scheme(asset.source)

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if scheme == "file":
                await _fetch_local(asset.source, dest)
            elif scheme == "https":
                await _fetch_https(asset.source, dest, timeout)
            elif scheme == "az":
                await _fetch_azure_blob(asset.source, dest, timeout)
            else:
                raise ValueError(f"Unsupported asset source scheme '{scheme}' in: {asset.source}")

            # Verify checksum if provided
            if asset.checksum:
                actual = _compute_sha256(dest)
                if actual != asset.checksum:
                    dest.unlink(missing_ok=True)
                    raise ValueError(
                        f"Checksum mismatch for asset '{asset.name}': expected {asset.checksum}, got {actual}"
                    )

            LOGGER.info(f"Asset '{asset.name}' provisioned at {dest}")
            return

        except Exception as e:
            last_error = e
            # Clean up partial download
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.unlink(missing_ok=True)
            dest.unlink(missing_ok=True)

            if attempt < MAX_RETRIES:
                backoff = BACKOFF_BASE_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1))
                LOGGER.warning(
                    f"Asset '{asset.name}' fetch attempt {attempt}/{MAX_RETRIES} failed: {e}. "
                    f"Retrying in {backoff:.0f}s..."
                )
                await asyncio.sleep(backoff)

    raise RuntimeError(f"Failed to provision asset '{asset.name}' after {MAX_RETRIES} attempts") from last_error


async def provision_assets(config: "EnvironmentConfig") -> None:
    """Provision all assets defined in the environment config.

    Skips assets that already exist at the destination with a matching checksum.
    Downloads are attempted sequentially to avoid overwhelming network/disk.
    """
    if not config.assets:
        return

    cache_dir = config.get_cache_dir()

    for asset in config.assets:
        dest = cache_dir / asset.destination

        # Skip if already present with valid checksum
        if dest.exists() and asset.checksum:
            actual = _compute_sha256(dest)
            if actual == asset.checksum:
                LOGGER.info(f"Asset '{asset.name}' already cached (checksum match), skipping")
                continue
            else:
                LOGGER.info(f"Asset '{asset.name}' exists but checksum mismatch, re-fetching")
        elif dest.exists() and not asset.checksum:
            LOGGER.info(f"Asset '{asset.name}' already exists (no checksum to verify), skipping")
            continue

        LOGGER.info(f"Provisioning asset '{asset.name}' ({asset.size_hint_mb or '?'} MB) from {asset.source}")
        await _fetch_single_asset(asset, cache_dir)
