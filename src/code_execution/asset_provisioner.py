"""
Asset provisioner for large artifacts (model weights, data files).

Downloads or copies assets defined in ``EnvironmentConfig.assets`` into the
environment cache directory.  Supports multiple source URI schemes and
checksum-based skip-if-present logic to avoid redundant transfers.

Reuses the existing ``BlobFetcher`` and ``LocalFileFetcher`` from the
data_access layer for Azure Blob Storage and local file sources.
"""

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

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


def _is_blob_source(source: str) -> bool:
    """Check if a source URI is an Azure Blob Storage reference."""
    from .data_access.fetchers import BlobFetcher

    fetcher = BlobFetcher.__new__(BlobFetcher)
    return fetcher.can_handle(source)


def _is_local_source(source: str) -> bool:
    """Check if a source URI is a local file reference."""
    from .data_access.fetchers import LocalFileFetcher

    fetcher = LocalFileFetcher.__new__(LocalFileFetcher)
    return fetcher.can_handle(source)


def _is_https_source(source: str) -> bool:
    """Check if a source is an HTTPS URL (non-blob)."""
    return source.startswith("https://") or source.startswith("http://")


async def _fetch_https(source: str, dest: Path, timeout: float) -> None:
    """Download a file via HTTP(S) with streaming (for non-Azure-Blob HTTPS URLs)."""
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


async def _fetch_blob(source: str, dest: Path, credential=None) -> None:
    """Download from Azure Blob Storage using the existing BlobFetcher."""
    from .data_access.fetchers import BlobFetcher

    fetcher = BlobFetcher(credential=credential)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    await fetcher.fetch_to_file(source, tmp)
    tmp.rename(dest)


async def _fetch_local(source: str, dest: Path) -> None:
    """Copy a local file using the existing LocalFileFetcher."""
    from .data_access.fetchers import LocalFileFetcher

    fetcher = LocalFileFetcher(allowed_roots=None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    await fetcher.fetch_to_file(source, tmp)
    tmp.rename(dest)


async def _get_default_credential():
    """Get a DefaultAzureCredential for blob access."""
    from azure.identity.aio import DefaultAzureCredential

    return DefaultAzureCredential()


async def _fetch_single_asset(asset: "AssetSpec", cache_dir: Path) -> None:
    """Fetch a single asset with retry logic."""
    dest = cache_dir / asset.destination
    timeout = _timeout_for_asset(asset)

    credential = None
    last_error: Exception | None = None

    try:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if _is_blob_source(asset.source):
                    if credential is None:
                        credential = await _get_default_credential()
                    await _fetch_blob(asset.source, dest, credential)
                elif _is_local_source(asset.source):
                    await _fetch_local(asset.source, dest)
                elif _is_https_source(asset.source):
                    await _fetch_https(asset.source, dest, timeout)
                else:
                    raise ValueError(f"Unsupported asset source: {asset.source}")

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
    finally:
        if credential is not None:
            await credential.close()

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
