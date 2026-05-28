"""Tests for the asset provisioner module."""

import hashlib
from pathlib import Path

import pytest

from code_execution.asset_provisioner import (
    _compute_sha256,
    _is_blob_source,
    _is_https_source,
    _is_local_source,
    provision_assets,
)
from code_execution.code_execution_models import AssetSpec, ServerConfig


class TestHelpers:
    """Unit tests for helper functions."""

    def test_is_blob_source_abfss(self):
        assert _is_blob_source("abfss://container@account.dfs.core.windows.net/path/file.bin")

    def test_is_blob_source_https_blob(self):
        assert _is_blob_source("https://myaccount.blob.core.windows.net/container/file.bin")

    def test_is_blob_source_not_blob(self):
        assert not _is_blob_source("https://example.com/model.bin")

    def test_is_local_source_bare_path(self):
        assert _is_local_source("/tmp/model.bin")

    def test_is_local_source_file_uri(self):
        assert _is_local_source("file:///tmp/model.bin")

    def test_is_local_source_relative(self):
        assert _is_local_source("./models/weights.bin")

    def test_is_local_source_not_local(self):
        assert not _is_local_source("https://example.com/model.bin")

    def test_is_https_source(self):
        assert _is_https_source("https://example.com/model.bin")
        assert _is_https_source("http://example.com/model.bin")
        assert not _is_https_source("/tmp/model.bin")

    def test_compute_sha256(self, tmp_path):
        f = tmp_path / "test.bin"
        content = b"hello world"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert _compute_sha256(f) == expected


class TestProvisionAssets:
    """Integration tests for asset provisioning with local file sources."""

    @pytest.fixture
    def source_file(self, tmp_path):
        """Create a source file to provision."""
        src = tmp_path / "source" / "weights.bin"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"fake model weights" * 100)
        return src

    @pytest.fixture
    def config_with_local_asset(self, tmp_path, source_file):
        """Create a ServerConfig with a local file asset."""
        cache_dir = tmp_path / "cache" / "myenv"
        return ServerConfig(
            name="myenv",
            description="Test env",
            type="uv",
            dependency_file="numpy",
            build_dir=cache_dir / "uv",
            assets=[
                AssetSpec(
                    name="test-weights",
                    source=str(source_file),
                    destination="models/weights.bin",
                    checksum=hashlib.sha256(source_file.read_bytes()).hexdigest(),
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_provision_local_file(self, config_with_local_asset):
        """Test that a local file asset is copied to the destination."""
        config = config_with_local_asset
        await provision_assets(config)

        dest = config.get_cache_dir() / "models" / "weights.bin"
        assert dest.exists()
        # Verify content matches
        source_path = Path(config.assets[0].source)
        assert dest.read_bytes() == source_path.read_bytes()

    @pytest.mark.asyncio
    async def test_provision_skips_when_cached(self, config_with_local_asset, tmp_path):
        """Test that provisioning is skipped when checksum matches."""
        config = config_with_local_asset

        # Pre-populate the destination
        dest = config.get_cache_dir() / "models" / "weights.bin"
        dest.parent.mkdir(parents=True, exist_ok=True)
        source_path = Path(config.assets[0].source)
        dest.write_bytes(source_path.read_bytes())

        # Modify source to detect if re-copy happens
        original_content = source_path.read_bytes()
        source_path.write_bytes(b"different content")

        await provision_assets(config)

        # Destination should still have original content (was skipped)
        assert dest.read_bytes() == original_content

    @pytest.mark.asyncio
    async def test_provision_refetches_on_checksum_mismatch(self, config_with_local_asset):
        """Test that asset is re-fetched when checksum doesn't match."""
        config = config_with_local_asset

        # Pre-populate with wrong content
        dest = config.get_cache_dir() / "models" / "weights.bin"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"stale content")

        await provision_assets(config)

        # Should have been re-fetched
        source_path = Path(config.assets[0].source)
        assert dest.read_bytes() == source_path.read_bytes()

    @pytest.mark.asyncio
    async def test_provision_no_assets(self, tmp_path):
        """Test that provisioning with no assets is a no-op."""
        config = ServerConfig(
            name="empty",
            description="No assets",
            type="uv",
            dependency_file="numpy",
            build_dir=tmp_path / "cache" / "empty" / "uv",
        )
        # Should not raise
        await provision_assets(config)

    @pytest.mark.asyncio
    async def test_provision_file_not_found(self, tmp_path):
        """Test that provisioning raises when local source doesn't exist."""
        config = ServerConfig(
            name="missing",
            description="Missing source",
            type="uv",
            dependency_file="numpy",
            build_dir=tmp_path / "cache" / "missing" / "uv",
            assets=[
                AssetSpec(
                    name="ghost",
                    source="/nonexistent/path/model.bin",
                    destination="models/model.bin",
                ),
            ],
        )
        with pytest.raises(RuntimeError, match="Failed to provision asset"):
            await provision_assets(config)

    @pytest.mark.asyncio
    async def test_provision_file_uri(self, tmp_path):
        """Test that file:// URIs work correctly."""
        src = tmp_path / "data" / "tokenizer.json"
        src.parent.mkdir(parents=True)
        src.write_bytes(b'{"vocab": []}')

        config = ServerConfig(
            name="fileuri",
            description="File URI test",
            type="uv",
            dependency_file="numpy",
            build_dir=tmp_path / "cache" / "fileuri" / "uv",
            assets=[
                AssetSpec(
                    name="tokenizer",
                    source=f"file://{src}",
                    destination="tokenizer.json",
                ),
            ],
        )
        await provision_assets(config)

        dest = config.get_cache_dir() / "tokenizer.json"
        assert dest.exists()
        assert dest.read_bytes() == b'{"vocab": []}'
