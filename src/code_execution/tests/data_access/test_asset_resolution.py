"""
Tests for asset resolution utilities.

Tests the logic in data_access.resolution module for detecting DataLake
type-tagged qualified names and determining when parameter values should
be resolved as assets.  Detection is purely value-based — any string
matching ``<type>id</type>`` is resolved, regardless of parameter type.
"""

from ...code_execution.data_access.resolution import (
    looks_like_qualified_name,
    should_resolve_as_asset,
)


class TestLooksLikeQualifiedName:
    """Test qualified name detection for type-tagged format."""

    # Valid tagged qualified names

    def test_valid_blob_tag(self):
        """Test valid blob-tagged artifact IDs are recognized."""
        assert looks_like_qualified_name("<blob>aHR0cHM6Ly9ncmlkMGVhc3R1czI=</blob>")
        assert looks_like_qualified_name("<blob>abc123</blob>")

    def test_valid_sql_tag(self):
        """Test valid sql-tagged artifact IDs are recognized."""
        assert looks_like_qualified_name("<sql>some_base64_id</sql>")

    def test_valid_custom_tag(self):
        """Test custom type tags are recognized."""
        assert looks_like_qualified_name("<delta>lake_id_abc</delta>")
        assert looks_like_qualified_name("<adls>container_path_id</adls>")

    # Invalid/malformed tags

    def test_mismatched_tags(self):
        """Test mismatched opening/closing tags are not recognized."""
        assert not looks_like_qualified_name("<blob>id</sql>")
        assert not looks_like_qualified_name("<sql>id</blob>")

    def test_missing_closing_tag(self):
        """Unclosed tags are accepted as a fallback for LLM-generated code."""
        assert looks_like_qualified_name("<blob>id")
        assert not looks_like_qualified_name("<blob>id<blob>")

    def test_empty_tag_content(self):
        """Test empty content between tags is not recognized."""
        assert not looks_like_qualified_name("<blob></blob>")

    def test_nested_tags(self):
        """Test nested tags are not recognized (angle brackets in content)."""
        assert not looks_like_qualified_name("<blob><inner>id</inner></blob>")

    # URLs and other formats are NOT recognized

    def test_urls_not_recognized(self):
        """Test that raw URLs are not recognized as qualified names."""
        assert not looks_like_qualified_name("https://storage.blob.core.windows.net/container/file.json")
        assert not looks_like_qualified_name("abfss://container@storage.dfs.core.windows.net/data.parquet")
        assert not looks_like_qualified_name("mssql://server.database.windows.net/db/schema/table")
        assert not looks_like_qualified_name("http://example.com/path")
        assert not looks_like_qualified_name("https://example.com/api/data")

    # Edge cases

    def test_empty_string(self):
        """Test empty string is not a qualified name."""
        assert not looks_like_qualified_name("")

    def test_whitespace_only(self):
        """Test whitespace-only strings are not qualified names."""
        assert not looks_like_qualified_name(" ")
        assert not looks_like_qualified_name("   ")
        assert not looks_like_qualified_name("\t\n")

    def test_none_value(self):
        """Test None is not a qualified name."""
        assert not looks_like_qualified_name(None)  # pyright: ignore reportArgumentType

    def test_non_string_types(self):
        """Test non-string types are not qualified names."""
        assert not looks_like_qualified_name(123)  # pyright: ignore reportArgumentType
        assert not looks_like_qualified_name(123.45)  # pyright: ignore reportArgumentType
        assert not looks_like_qualified_name([])  # pyright: ignore reportArgumentType
        assert not looks_like_qualified_name({})  # pyright: ignore reportArgumentType
        assert not looks_like_qualified_name(True)  # pyright: ignore reportArgumentType
        assert not looks_like_qualified_name(object())  # pyright: ignore reportArgumentType

    def test_tag_with_whitespace(self):
        """Test tagged values with leading/trailing whitespace are handled."""
        assert looks_like_qualified_name(" <blob>id</blob>")
        assert looks_like_qualified_name("<blob>id</blob> ")
        assert looks_like_qualified_name("  <blob>id</blob>  ")

    def test_url_like_strings(self):
        """Test strings that look URL-like but aren't qualified names."""
        assert not looks_like_qualified_name("file:///path/to/file")
        assert not looks_like_qualified_name("s3://bucket/key")
        assert not looks_like_qualified_name("gs://bucket/object")
        assert not looks_like_qualified_name("ftp://server/path")

    def test_plain_paths(self):
        """Test plain file paths are not qualified names."""
        assert not looks_like_qualified_name("/path/to/file.csv")
        assert not looks_like_qualified_name("C:\\path\\to\\file.csv")
        assert not looks_like_qualified_name("./relative/path.parquet")

    def test_plain_strings(self):
        """Test plain strings are not qualified names."""
        assert not looks_like_qualified_name("hello world")
        assert not looks_like_qualified_name("my_data.csv")
        assert not looks_like_qualified_name("some_id_12345")


class TestShouldResolveAsAsset:
    """Test value-based asset resolution detection.

    ``should_resolve_as_asset`` is purely value-based — it only inspects the
    value, not the parameter type.  Any string matching ``<type>id</type>``
    should resolve.
    """

    # Should resolve — tagged values

    def test_resolve_blob_tag(self):
        """Test blob-tagged value is detected."""
        assert should_resolve_as_asset("<blob>abc123</blob>")

    def test_resolve_sql_tag(self):
        """Test sql-tagged value is detected."""
        assert should_resolve_as_asset("<sql>xyz789</sql>")

    def test_resolve_with_realistic_base64_id(self):
        """Test detection with realistic base64-encoded artifact ID."""
        assert should_resolve_as_asset("<blob>aHR0cHM6Ly9ncmlkMGVhc3R1czIuYmxvYi5jb3JlLndpbmRvd3MubmV0</blob>")

    def test_resolve_with_whitespace(self):
        """Test detection with leading/trailing whitespace."""
        assert should_resolve_as_asset("  <blob>abc123</blob>  ")

    # Should NOT resolve — non-tagged strings

    def test_no_resolve_regular_string(self):
        """Test regular strings are not detected."""
        assert not should_resolve_as_asset("my_data.csv")
        assert not should_resolve_as_asset("/path/to/file.parquet")
        assert not should_resolve_as_asset("some_id_12345")

    def test_no_resolve_url(self):
        """Test raw URLs are not detected."""
        assert not should_resolve_as_asset("https://storage.blob.core.windows.net/container/file.nc")

    def test_no_resolve_plain_path(self):
        """Test plain paths are not detected."""
        assert not should_resolve_as_asset("/local/path/data.parquet")

    # Should NOT resolve — non-string values

    def test_no_resolve_non_string_value(self):
        """Test non-string values are never detected."""
        assert not should_resolve_as_asset(123)
        assert not should_resolve_as_asset(123.45)
        assert not should_resolve_as_asset([])
        assert not should_resolve_as_asset({})
        assert not should_resolve_as_asset(None)

    # Edge cases

    def test_empty_string_value(self):
        """Test empty string is not detected."""
        assert not should_resolve_as_asset("")

    def test_whitespace_string(self):
        """Test whitespace-only string is not detected."""
        assert not should_resolve_as_asset("   ")

    def test_none_value(self):
        """Test None value is not detected."""
        assert not should_resolve_as_asset(None)

    # Integration test

    def test_end_to_end_decision_flow(self):
        """Test realistic end-to-end scenarios."""

        # Tagged reference -> resolve
        assert should_resolve_as_asset("<blob>abc123</blob>")

        # Regular URL -> do not resolve
        assert not should_resolve_as_asset("https://storage.blob.core.windows.net/c/f.nc")

        # Regular file path -> do not resolve
        assert not should_resolve_as_asset("/local/path/data.parquet")

        # Non-string value -> do not resolve
        assert not should_resolve_as_asset(object())

        # None -> do not resolve
        assert not should_resolve_as_asset(None)
