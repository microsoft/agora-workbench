"""
Tests for LocalFileFetcher class.

Tests path handling, allowed_roots sandboxing, fetch, and fetch_to_file.
"""

import pytest

from ...code_execution.data_access.fetchers import LocalFileFetcher


class TestLocalFileFetcherCanHandle:
    """Tests for LocalFileFetcher.can_handle()."""

    def test_handles_absolute_path(self):
        fetcher = LocalFileFetcher()
        assert fetcher.can_handle("/data/weather/obs.csv") is True

    def test_handles_relative_path_dot_slash(self):
        fetcher = LocalFileFetcher()
        assert fetcher.can_handle("./data/weather/obs.csv") is True

    def test_handles_relative_path_dot_dot_slash(self):
        fetcher = LocalFileFetcher()
        assert fetcher.can_handle("../data/weather/obs.csv") is True

    def test_handles_file_uri(self):
        fetcher = LocalFileFetcher()
        assert fetcher.can_handle("file:///data/weather/obs.csv") is True

    def test_rejects_https_url(self):
        fetcher = LocalFileFetcher()
        assert fetcher.can_handle("https://storage.blob.core.windows.net/container/file") is False

    def test_rejects_abfss_url(self):
        fetcher = LocalFileFetcher()
        assert fetcher.can_handle("abfss://container@storage.dfs.core.windows.net/path") is False

    def test_rejects_bare_relative_path(self):
        fetcher = LocalFileFetcher()
        assert fetcher.can_handle("data/weather/obs.csv") is False

    def test_credential_is_none(self):
        fetcher = LocalFileFetcher()
        assert fetcher.credential is None


class TestLocalFileFetcherFetch:
    """Tests for LocalFileFetcher.fetch()."""

    @pytest.mark.asyncio
    async def test_fetch_reads_file_contents(self, tmp_path):
        test_file = tmp_path / "test.csv"
        test_file.write_bytes(b"col1,col2\na,b\n")

        fetcher = LocalFileFetcher()
        data = await fetcher.fetch(str(test_file))
        assert data == b"col1,col2\na,b\n"

    @pytest.mark.asyncio
    async def test_fetch_handles_file_uri(self, tmp_path):
        test_file = tmp_path / "test.csv"
        test_file.write_bytes(b"hello")

        fetcher = LocalFileFetcher()
        data = await fetcher.fetch(f"file://{test_file}")
        assert data == b"hello"

    @pytest.mark.asyncio
    async def test_fetch_raises_for_missing_file(self):
        fetcher = LocalFileFetcher()
        with pytest.raises(FileNotFoundError, match="Local file not found"):
            await fetcher.fetch("/nonexistent/path/file.csv")

    @pytest.mark.asyncio
    async def test_fetch_allowed_roots_permits_valid_path(self, tmp_path):
        test_file = tmp_path / "data" / "test.csv"
        test_file.parent.mkdir(parents=True)
        test_file.write_bytes(b"ok")

        fetcher = LocalFileFetcher(allowed_roots=[str(tmp_path)])
        data = await fetcher.fetch(str(test_file))
        assert data == b"ok"

    @pytest.mark.asyncio
    async def test_fetch_allowed_roots_blocks_outside_path(self, tmp_path):
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        outside_file = tmp_path / "secret.txt"
        outside_file.write_bytes(b"secret")

        fetcher = LocalFileFetcher(allowed_roots=[str(allowed_dir)])
        with pytest.raises(PermissionError, match="outside allowed roots"):
            await fetcher.fetch(str(outside_file))

    @pytest.mark.asyncio
    async def test_fetch_allowed_roots_blocks_traversal(self, tmp_path):
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        outside_file = tmp_path / "secret.txt"
        outside_file.write_bytes(b"secret")

        fetcher = LocalFileFetcher(allowed_roots=[str(allowed_dir)])
        # Try path traversal
        traversal_path = str(allowed_dir / ".." / "secret.txt")
        with pytest.raises(PermissionError, match="outside allowed roots"):
            await fetcher.fetch(traversal_path)


class TestLocalFileFetcherFetchToFile:
    """Tests for LocalFileFetcher.fetch_to_file()."""

    @pytest.mark.asyncio
    async def test_fetch_to_file_copies_content(self, tmp_path):
        source = tmp_path / "source.csv"
        source.write_bytes(b"col1,col2\n1,2\n")
        dest = tmp_path / "output" / "dest.csv"

        fetcher = LocalFileFetcher()
        bytes_written = await fetcher.fetch_to_file(str(source), dest)

        assert bytes_written == 14
        assert dest.read_bytes() == b"col1,col2\n1,2\n"

    @pytest.mark.asyncio
    async def test_fetch_to_file_creates_parent_dirs(self, tmp_path):
        source = tmp_path / "source.txt"
        source.write_bytes(b"data")
        dest = tmp_path / "a" / "b" / "c" / "out.txt"

        fetcher = LocalFileFetcher()
        await fetcher.fetch_to_file(str(source), dest)

        assert dest.exists()
        assert dest.read_bytes() == b"data"

    @pytest.mark.asyncio
    async def test_fetch_to_file_respects_allowed_roots(self, tmp_path):
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        outside_file = tmp_path / "secret.txt"
        outside_file.write_bytes(b"secret")
        dest = tmp_path / "out.txt"

        fetcher = LocalFileFetcher(allowed_roots=[str(allowed_dir)])
        with pytest.raises(PermissionError, match="outside allowed roots"):
            await fetcher.fetch_to_file(str(outside_file), dest)


class TestLocalFileFetcherNoRoots:
    """Tests for LocalFileFetcher with no allowed_roots (permissive mode)."""

    @pytest.mark.asyncio
    async def test_no_roots_allows_any_path(self, tmp_path):
        test_file = tmp_path / "anywhere.txt"
        test_file.write_bytes(b"anything")

        fetcher = LocalFileFetcher(allowed_roots=None)
        data = await fetcher.fetch(str(test_file))
        assert data == b"anything"

    @pytest.mark.asyncio
    async def test_empty_roots_list_allows_any_path(self, tmp_path):
        test_file = tmp_path / "anywhere.txt"
        test_file.write_bytes(b"anything")

        fetcher = LocalFileFetcher(allowed_roots=[])
        data = await fetcher.fetch(str(test_file))
        assert data == b"anything"
