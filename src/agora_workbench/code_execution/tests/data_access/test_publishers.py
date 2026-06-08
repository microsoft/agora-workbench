"""
Unit tests for AssetPublisher, BlobPublisher, and LocalFilePublisher.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ...data_access.publishers import (
    BlobPublisher,
    LocalFilePublisher,
    _validate_artifact_name,
    parse_destination_tag,
)


# ---------------------------------------------------------------------------
# parse_destination_tag
# ---------------------------------------------------------------------------


class TestParseDestinationTag:
    """Tests for the ``parse_destination_tag`` helper."""

    def test_closed_tag(self):
        result = parse_destination_tag("<blob>results.csv</blob>")
        assert result == ("blob", "results.csv")

    def test_unclosed_tag_fallback(self):
        """LLM sometimes omits the closing tag — accept gracefully."""
        result = parse_destination_tag("<blob>results.csv")
        assert result == ("blob", "results.csv")

    def test_local_tag(self):
        result = parse_destination_tag("<local>output</local>")
        assert result == ("local", "output")

    def test_path_like_name(self):
        result = parse_destination_tag("<blob>subdir/report.pdf</blob>")
        assert result == ("blob", "subdir/report.pdf")

    def test_strips_whitespace(self):
        result = parse_destination_tag("  <blob>results.csv</blob>  ")
        assert result == ("blob", "results.csv")

    def test_invalid_returns_none(self):
        assert parse_destination_tag("results.csv") is None
        assert parse_destination_tag("https://example.com/file") is None
        assert parse_destination_tag("") is None
        assert parse_destination_tag("<>name</>") is None

    def test_mismatched_tags_returns_none(self):
        # Closed with wrong tag → no match
        assert parse_destination_tag("<blob>name</local>") is None


# ---------------------------------------------------------------------------
# _validate_artifact_name
# ---------------------------------------------------------------------------


class TestValidateArtifactName:
    """Tests for the ``_validate_artifact_name`` helper."""

    def test_accepts_simple_filename(self):
        _validate_artifact_name("results.csv")  # should not raise

    def test_accepts_path_like_name(self):
        _validate_artifact_name("subdir/report.pdf")  # should not raise

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_artifact_name("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_artifact_name("   ")

    def test_rejects_absolute_path(self):
        with pytest.raises(ValueError, match="absolute path"):
            _validate_artifact_name("/etc/passwd")

    def test_rejects_parent_traversal(self):
        with pytest.raises(ValueError, match="parent traversal"):
            _validate_artifact_name("../escape.txt")

    def test_rejects_nested_parent_traversal(self):
        with pytest.raises(ValueError, match="parent traversal"):
            _validate_artifact_name("subdir/../../escape.txt")

    def test_rejects_windows_style_traversal(self):
        with pytest.raises(ValueError, match="parent traversal"):
            _validate_artifact_name("subdir\\..\\..\\escape.txt")


# ---------------------------------------------------------------------------
# BlobPublisher
# ---------------------------------------------------------------------------


class TestBlobPublisherCanHandle:
    """Tests for BlobPublisher.can_handle()."""

    def test_handles_blob_closed_tag(self):
        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c")
        assert pub.can_handle("<blob>results.csv</blob>") is True

    def test_handles_blob_unclosed_tag(self):
        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c")
        assert pub.can_handle("<blob>results.csv") is True

    def test_rejects_local_tag(self):
        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c")
        assert pub.can_handle("<local>output</local>") is False

    def test_rejects_plain_string(self):
        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c")
        assert pub.can_handle("results.csv") is False

    def test_rejects_url(self):
        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c")
        assert pub.can_handle("https://acct.blob.core.windows.net/c/f") is False


class TestBlobPublisherInit:
    """Tests for BlobPublisher initialisation."""

    def test_stores_account_url_stripped(self):
        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net/", container="mycontainer")
        assert pub._account_url == "https://acct.blob.core.windows.net"

    def test_stores_container(self):
        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="mycontainer")
        assert pub._container == "mycontainer"

    def test_credential_stored(self, create_mock_credential):
        cred = create_mock_credential()
        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c", credential=cred)
        assert pub.credential is cred

    def test_client_lazily_created(self):
        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c")
        assert pub._client is None


class TestBlobPublisherPublish:
    """Tests for BlobPublisher.publish()."""

    @pytest.mark.asyncio
    async def test_publish_uploads_file(self, tmp_path):
        src = tmp_path / "results.csv"
        src.write_bytes(b"a,b\n1,2\n")

        mock_blob_client = AsyncMock()
        mock_blob_client.upload_blob = AsyncMock()

        mock_service_client = MagicMock()
        mock_service_client.get_blob_client = MagicMock(return_value=mock_blob_client)
        mock_service_client.close = AsyncMock()

        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="mycontainer")
        pub._client = mock_service_client

        remote_uri = await pub.publish(local_path=src, name="results.csv", session_id="sess-1")

        assert remote_uri == "https://acct.blob.core.windows.net/mycontainer/sess-1/results.csv"
        mock_service_client.get_blob_client.assert_called_once_with(container="mycontainer", blob="sess-1/results.csv")
        mock_blob_client.upload_blob.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_path_like_name(self, tmp_path):
        src = tmp_path / "report.pdf"
        src.write_bytes(b"%PDF")

        mock_blob_client = AsyncMock()
        mock_blob_client.upload_blob = AsyncMock()

        mock_service_client = MagicMock()
        mock_service_client.get_blob_client = MagicMock(return_value=mock_blob_client)

        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="arts")
        pub._client = mock_service_client

        remote_uri = await pub.publish(local_path=src, name="subdir/report.pdf", session_id="sess-2")

        assert remote_uri == "https://acct.blob.core.windows.net/arts/sess-2/subdir/report.pdf"
        mock_service_client.get_blob_client.assert_called_once_with(container="arts", blob="sess-2/subdir/report.pdf")

    @pytest.mark.asyncio
    async def test_publish_raises_if_file_missing(self, tmp_path):
        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c")
        pub._client = MagicMock()

        with pytest.raises(FileNotFoundError):
            await pub.publish(local_path=tmp_path / "nonexistent.csv", name="x.csv", session_id="s")

    @pytest.mark.asyncio
    async def test_publish_rejects_parent_traversal(self, tmp_path):
        src = tmp_path / "data.csv"
        src.write_bytes(b"a,b\n")

        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c")
        pub._client = MagicMock()

        with pytest.raises(ValueError, match="parent traversal"):
            await pub.publish(local_path=src, name="../escape/data.csv", session_id="s")

    @pytest.mark.asyncio
    async def test_publish_rejects_absolute_path(self, tmp_path):
        src = tmp_path / "data.csv"
        src.write_bytes(b"a,b\n")

        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c")
        pub._client = MagicMock()

        with pytest.raises(ValueError, match="absolute path"):
            await pub.publish(local_path=src, name="/etc/passwd", session_id="s")

    @pytest.mark.asyncio
    async def test_close_resets_client(self):
        mock_service_client = AsyncMock()
        mock_service_client.close = AsyncMock()

        pub = BlobPublisher(account_url="https://acct.blob.core.windows.net", container="c")
        pub._client = mock_service_client

        await pub.close()

        mock_service_client.close.assert_called_once()
        assert pub._client is None


# ---------------------------------------------------------------------------
# LocalFilePublisher
# ---------------------------------------------------------------------------


class TestLocalFilePublisherCanHandle:
    """Tests for LocalFilePublisher.can_handle()."""

    def test_handles_local_closed_tag(self):
        pub = LocalFilePublisher(base_dir="/tmp/outputs")
        assert pub.can_handle("<local>output</local>") is True

    def test_handles_local_unclosed_tag(self):
        pub = LocalFilePublisher(base_dir="/tmp/outputs")
        assert pub.can_handle("<local>output") is True

    def test_rejects_blob_tag(self):
        pub = LocalFilePublisher(base_dir="/tmp/outputs")
        assert pub.can_handle("<blob>results.csv</blob>") is False

    def test_rejects_plain_string(self):
        pub = LocalFilePublisher(base_dir="/tmp/outputs")
        assert pub.can_handle("output") is False


class TestLocalFilePublisherPublish:
    """Tests for LocalFilePublisher.publish()."""

    @pytest.mark.asyncio
    async def test_publish_copies_file(self, tmp_path):
        src = tmp_path / "results.csv"
        src.write_bytes(b"x,y\n1,2\n")

        base_dir = tmp_path / "outputs"
        pub = LocalFilePublisher(base_dir=base_dir)

        result = await pub.publish(local_path=src, name="results.csv", session_id="sess-abc")

        dest = base_dir / "sess-abc" / "results.csv"
        assert dest.exists()
        assert dest.read_bytes() == b"x,y\n1,2\n"
        assert result == str(dest)

    @pytest.mark.asyncio
    async def test_publish_creates_parent_dirs(self, tmp_path):
        src = tmp_path / "report.pdf"
        src.write_bytes(b"%PDF")

        base_dir = tmp_path / "shared" / "outputs"
        pub = LocalFilePublisher(base_dir=base_dir)

        result = await pub.publish(local_path=src, name="subdir/report.pdf", session_id="sess-xyz")

        dest = base_dir / "sess-xyz" / "subdir" / "report.pdf"
        assert dest.exists()
        assert result == str(dest)

    @pytest.mark.asyncio
    async def test_publish_raises_if_file_missing(self, tmp_path):
        pub = LocalFilePublisher(base_dir=tmp_path / "out")

        with pytest.raises(FileNotFoundError):
            await pub.publish(local_path=tmp_path / "nonexistent.csv", name="x.csv", session_id="s")

    @pytest.mark.asyncio
    async def test_publish_rejects_parent_traversal(self, tmp_path):
        src = tmp_path / "data.csv"
        src.write_bytes(b"evil\n")

        pub = LocalFilePublisher(base_dir=tmp_path / "outputs")

        with pytest.raises(ValueError, match="parent traversal"):
            await pub.publish(local_path=src, name="../../etc/crontab", session_id="sess")

    @pytest.mark.asyncio
    async def test_publish_rejects_absolute_path(self, tmp_path):
        src = tmp_path / "data.csv"
        src.write_bytes(b"evil\n")

        pub = LocalFilePublisher(base_dir=tmp_path / "outputs")

        with pytest.raises(ValueError, match="absolute path"):
            await pub.publish(local_path=src, name="/etc/passwd", session_id="sess")

    def test_base_dir_resolved(self, tmp_path):
        pub = LocalFilePublisher(base_dir=tmp_path)
        assert pub._base_dir == tmp_path.resolve()

    def test_credential_is_none(self):
        pub = LocalFilePublisher(base_dir="/tmp/outputs")
        assert pub.credential is None


# ---------------------------------------------------------------------------
# GuiPublisher
# ---------------------------------------------------------------------------


class TestGuiPublisherCanHandle:
    """Tests for GuiPublisher.can_handle()."""

    def test_handles_gui_closed_tag(self):
        from ...data_access.publishers import GuiPublisher

        pub = GuiPublisher(public_url_fn=lambda: "http://localhost:8000")
        assert pub.can_handle("<gui>results.csv</gui>") is True

    def test_handles_gui_unclosed_tag(self):
        from ...data_access.publishers import GuiPublisher

        pub = GuiPublisher(public_url_fn=lambda: "http://localhost:8000")
        assert pub.can_handle("<gui>results.csv") is True

    def test_rejects_blob_tag(self):
        from ...data_access.publishers import GuiPublisher

        pub = GuiPublisher(public_url_fn=lambda: "http://localhost:8000")
        assert pub.can_handle("<blob>results.csv</blob>") is False

    def test_rejects_local_tag(self):
        from ...data_access.publishers import GuiPublisher

        pub = GuiPublisher(public_url_fn=lambda: "http://localhost:8000")
        assert pub.can_handle("<local>output</local>") is False


class TestGuiPublisherPublish:
    """Tests for GuiPublisher.publish()."""

    @pytest.mark.asyncio
    async def test_returns_download_url(self, tmp_path):
        from ...data_access.publishers import GuiPublisher

        artifact = tmp_path / "results.csv"
        artifact.write_text("data")

        pub = GuiPublisher(public_url_fn=lambda: "http://localhost:8000")
        pub._download_token = "abc123"

        url = await pub.publish(artifact, "results.csv", "session-1")
        assert url == "http://localhost:8000/artifacts/session-1/abc123/results.csv"

    @pytest.mark.asyncio
    async def test_uses_server_public_url_env(self, tmp_path, monkeypatch):
        from ...data_access.publishers import GuiPublisher

        monkeypatch.setenv("SERVER_PUBLIC_URL", "https://my-server.example.com")
        artifact = tmp_path / "out.png"
        artifact.write_bytes(b"\x89PNG")

        pub = GuiPublisher(public_url_fn=lambda: "http://localhost:8000")
        pub._download_token = "tok42"

        url = await pub.publish(artifact, "out.png", "sess-2")
        assert url == "https://my-server.example.com/artifacts/sess-2/tok42/out.png"

    @pytest.mark.asyncio
    async def test_raises_without_token(self, tmp_path):
        from ...data_access.publishers import GuiPublisher

        artifact = tmp_path / "file.txt"
        artifact.write_text("hello")

        pub = GuiPublisher(public_url_fn=lambda: "http://localhost:8000")
        with pytest.raises(RuntimeError, match="download token"):
            await pub.publish(artifact, "file.txt", "session-1")

    @pytest.mark.asyncio
    async def test_raises_for_missing_file(self, tmp_path):
        from ...data_access.publishers import GuiPublisher

        pub = GuiPublisher(public_url_fn=lambda: "http://localhost:8000")
        pub._download_token = "tok"
        with pytest.raises(FileNotFoundError):
            await pub.publish(tmp_path / "ghost.txt", "ghost.txt", "session-1")


# ---------------------------------------------------------------------------
# publish_artifact MCP tool (via CodeExecutionServer)
# ---------------------------------------------------------------------------


def _make_server_with_publishers(publishers):
    """Build a minimal CodeExecutionServer with the given publishers."""
    from ... import CodeExecutionServer, ServerConfig
    from ...auth import create_noop_auth_config

    return CodeExecutionServer(
        server_config=ServerConfig(
            name="test",
            type="uv",
            description="Test environment",
            dependency_file="# empty",
        ),
        auth_config=create_noop_auth_config(),
        publishers=publishers,
    )


class TestPublishArtifactTool:
    """Tests for the {name}_publish_artifact MCP tool."""

    @pytest.mark.asyncio
    async def test_tool_registered_when_publishers_provided(self, tmp_path):
        pub = LocalFilePublisher(base_dir=tmp_path)
        server = _make_server_with_publishers([pub])

        tool_names = {t.name for t in await server.mcp.list_tools()}
        assert "test_publish_artifact" in tool_names

    @pytest.mark.asyncio
    async def test_tool_always_registered_with_gui_publisher(self):
        """GuiPublisher is auto-registered, so publish tool is always available."""
        server = _make_server_with_publishers([])

        tool_names = {t.name for t in await server.mcp.list_tools()}
        assert "test_publish_artifact" in tool_names

    @pytest.mark.asyncio
    async def test_publish_succeeds(self, tmp_path, monkeypatch):
        """End-to-end: artifact registered, publisher copies the file."""
        from ... import sessions as sessions_pkg
        from ...sessions import (
            SessionManager,
            SessionConfig,
            set_current_user_identity,
            set_current_request_token,
            set_current_token_claims,
        )

        monkeypatch.setattr(sessions_pkg.manager, "_OUTPUTS_BASE_DIR", tmp_path)
        sm = SessionManager(SessionConfig())

        base_out = tmp_path / "published"
        pub = LocalFilePublisher(base_dir=base_out)

        server = _make_server_with_publishers([pub])
        server.session_manager = sm

        # Create a session and register an artifact.
        session_id = sm.create_session(
            data={}, user_identity="u@t", user_token="tok", token_claims={"oid": "u", "tid": "t"}
        )
        outputs = sm._get_outputs_dir(session_id)
        (outputs / "results.csv").write_text("x,y\n1,2\n")

        before = sm._snapshot_outputs_dir(session_id)
        import time as _time
        import os as _os

        _os.utime(outputs / "results.csv", (_time.time() + 1, _time.time() + 1))
        after = sm._snapshot_outputs_dir(session_id)
        sm._register_artifacts_from_diff(session_id, before, after)

        # Set auth context so _get_or_create_session succeeds.
        set_current_user_identity("u@t")
        set_current_request_token("tok")
        set_current_token_claims({"oid": "u", "tid": "t"})

        # Mock _restore_auth_context_for_mcp_session since we set context manually.
        server._restore_auth_context_for_mcp_session = MagicMock()

        # Create a mock ctx that returns our session_id.
        mock_ctx = MagicMock()
        mock_ctx.session_id = session_id

        mcp_tool = await server.mcp.get_tool("test_publish_artifact")
        result_json = await mcp_tool.fn(
            ctx=mock_ctx,
            artifact_name="results.csv",
            destination="<local>results.csv</local>",
        )
        result = json.loads(result_json)
        assert result["success"] is True
        assert "remote_uri" in result

        # Verify file was actually copied to the publisher's destination.
        dest = base_out / session_id / "results.csv"
        assert dest.exists()
        assert dest.read_text() == "x,y\n1,2\n"

        # Clean up auth context.
        set_current_user_identity(None)
        set_current_request_token(None)
        set_current_token_claims(None)

    @pytest.mark.asyncio
    async def test_publish_invalid_destination(self, tmp_path, monkeypatch):
        """Returns error JSON for malformed destination."""
        pub = LocalFilePublisher(base_dir=tmp_path)
        server = _make_server_with_publishers([pub])

        mcp_tool = await server.mcp.get_tool("test_publish_artifact")

        result_json = await mcp_tool.fn(
            ctx=None,
            artifact_name="results.csv",
            destination="results.csv",  # missing tag
        )
        result = json.loads(result_json)

        assert result["success"] is False
        assert "Invalid destination format" in result["error"]

    @pytest.mark.asyncio
    async def test_publish_no_matching_publisher(self, tmp_path, monkeypatch):
        """Returns error JSON when no publisher handles the destination tag."""
        pub = LocalFilePublisher(base_dir=tmp_path)  # only handles <local>
        server = _make_server_with_publishers([pub])

        mcp_tool = await server.mcp.get_tool("test_publish_artifact")

        result_json = await mcp_tool.fn(
            ctx=None,
            artifact_name="results.csv",
            destination="<blob>results.csv</blob>",  # no BlobPublisher registered
        )
        result = json.loads(result_json)

        assert result["success"] is False
        assert "No publisher configured" in result["error"]

    @pytest.mark.asyncio
    async def test_publish_no_session(self, tmp_path):
        """Returns error JSON when there is no auth context (ctx=None)."""
        pub = LocalFilePublisher(base_dir=tmp_path)
        server = _make_server_with_publishers([pub])

        mcp_tool = await server.mcp.get_tool("test_publish_artifact")

        result_json = await mcp_tool.fn(
            ctx=None,
            artifact_name="results.csv",
            destination="<local>results.csv</local>",
        )
        result = json.loads(result_json)

        assert result["success"] is False
        assert "session authentication failed" in result["error"]


# ---------------------------------------------------------------------------
# find_artifact_by_name (SessionManager)
# ---------------------------------------------------------------------------


class TestFindArtifactByName:
    """Tests for SessionManager.find_artifact_by_name."""

    def _make_manager(self, tmp_path, monkeypatch):
        from ... import sessions as sessions_pkg
        from ...sessions import SessionManager, SessionConfig

        monkeypatch.setattr(sessions_pkg.manager, "_OUTPUTS_BASE_DIR", tmp_path)
        return SessionManager(SessionConfig())

    @pytest.mark.unit
    def test_finds_registered_artifact(self, tmp_path, monkeypatch):
        sm = self._make_manager(tmp_path, monkeypatch)
        session_id = sm.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        outputs = sm._get_outputs_dir(session_id)
        (outputs / "results.csv").write_text("a,b\n")

        before = sm._snapshot_outputs_dir(session_id)
        import os as _os
        import time as _t

        _os.utime(outputs / "results.csv", (_t.time() + 1, _t.time() + 1))
        after = sm._snapshot_outputs_dir(session_id)
        sm._register_artifacts_from_diff(session_id, before, after)

        record = sm.find_artifact_by_name(session_id, "results.csv")
        assert record is not None
        assert record.name == "results.csv"
        assert record.path.is_file()

    @pytest.mark.unit
    def test_returns_none_for_unknown_name(self, tmp_path, monkeypatch):
        sm = self._make_manager(tmp_path, monkeypatch)
        session_id = sm.create_session(data={}, user_identity="u", user_token="t", token_claims={})

        record = sm.find_artifact_by_name(session_id, "nope.csv")
        assert record is None

    @pytest.mark.unit
    def test_returns_none_for_unknown_session(self, tmp_path, monkeypatch):
        sm = self._make_manager(tmp_path, monkeypatch)

        record = sm.find_artifact_by_name("no-such-session", "results.csv")
        assert record is None

    @pytest.mark.unit
    def test_returns_none_when_file_deleted(self, tmp_path, monkeypatch):
        sm = self._make_manager(tmp_path, monkeypatch)
        session_id = sm.create_session(data={}, user_identity="u", user_token="t", token_claims={})
        outputs = sm._get_outputs_dir(session_id)
        (outputs / "ghost.txt").write_text("temp")

        before = sm._snapshot_outputs_dir(session_id)
        import os as _os
        import time as _t

        _os.utime(outputs / "ghost.txt", (_t.time() + 1, _t.time() + 1))
        after = sm._snapshot_outputs_dir(session_id)
        sm._register_artifacts_from_diff(session_id, before, after)

        # Delete the file after registration.
        (outputs / "ghost.txt").unlink()

        record = sm.find_artifact_by_name(session_id, "ghost.txt")
        assert record is None
