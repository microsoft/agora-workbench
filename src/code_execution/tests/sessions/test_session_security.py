"""Tests for session security and ownership validation (Phase 1.5)."""

import pytest
from unittest.mock import AsyncMock, patch

from ...code_execution.auth import create_noop_auth_config
from ...code_execution.sessions import (
    Session,
    get_current_request_token,
    set_current_request_token,
)


@pytest.fixture
def mock_entra_env():
    """Mock ENTRA environment variables required for CodeExecutionServer."""
    with patch.dict("os.environ", {"ENTRA_CLIENT_ID": "test-client-id", "ENTRA_TENANT_ID": "test-tenant-id"}):
        yield


class TestRequestTokenContext:
    """Test request token context variable."""

    def test_set_and_get_request_token(self):
        """Test setting and getting request token from context."""
        test_token = "test-bearer-token-12345"

        set_current_request_token(test_token)
        retrieved_token = get_current_request_token()

        assert retrieved_token == test_token

    def test_clear_request_token(self):
        """Test clearing request token from context."""
        test_token = "test-bearer-token-12345"

        set_current_request_token(test_token)
        assert get_current_request_token() == test_token

        set_current_request_token(None)
        # After clearing, getting the token should return None
        assert get_current_request_token() is None

    def test_get_request_token_when_not_set(self):
        """Test getting request token when none is set returns None."""
        # Clear any existing token
        set_current_request_token(None)

        # Getting token when not set should return None
        assert get_current_request_token() is None


class TestSessionOwnershipValidation:
    """Test session ownership validation logic."""

    @pytest.mark.asyncio
    async def test_verify_session_ownership_no_token(self, mock_entra_env):
        """Test that session access is denied when no token is provided."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(
            name="test", type="uv", description="Test environment", dependency_file="# Test dependencies"
        )
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="test-123",
            data={},
            session_type="test",
            user_identity="user@example.com",
            user_token="test-token",
            token_claims={},
        )

        # No token provided
        is_authorized = await server._verify_session_ownership(session, None)
        assert is_authorized is False

    @pytest.mark.asyncio
    async def test_verify_session_ownership_invalid_token(self, mock_entra_env):
        """Test that session access is denied for invalid token."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="test-123",
            data={},
            session_type="test",
            user_identity="user@example.com",
            user_token="test-token",
            token_claims={},
        )

        # Mock validate_token to raise exception
        server.validate_token = AsyncMock(side_effect=Exception("Invalid token"))

        is_authorized = await server._verify_session_ownership(session, "invalid-token")
        assert is_authorized is False

    @pytest.mark.asyncio
    async def test_verify_session_ownership_valid_token_matching_user(self, mock_entra_env):
        """Test that session access is granted for any valid token with matching user identity."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="test-123",
            data={},
            session_type="test",
            user_identity="user-oid-123@test-tenant-id",
            user_token="test-token",
            token_claims={},
        )

        # Any app with a valid token and matching user identity should be authorized
        server.validate_token = AsyncMock(
            return_value={
                "appid": "any-client-app-id",
                "oid": "user-oid-123",
                "tid": "test-tenant-id",
                "name": "Any Authorized Client",
            }
        )

        is_authorized = await server._verify_session_ownership(session, "valid-token")
        assert is_authorized is True

    @pytest.mark.asyncio
    async def test_verify_session_ownership_valid_token_mismatched_user(self, mock_entra_env):
        """Test that session access is denied when user identity does not match session owner."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="test-123",
            data={},
            session_type="test",
            user_identity="owner-oid@test-tenant-id",
            user_token="test-token",
            token_claims={},
        )

        # Different user identity in the token — must be denied regardless of app
        server.validate_token = AsyncMock(
            return_value={
                "appid": "any-client-app-id",
                "oid": "other-user-oid",
                "tid": "test-tenant-id",
                "name": "Different User",
            }
        )

        is_authorized = await server._verify_session_ownership(session, "valid-token")
        assert is_authorized is False

    @pytest.mark.asyncio
    async def test_verify_session_ownership_missing_identity_claims(self, mock_entra_env):
        """Test that session access is denied when the token is missing required user identity claims."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="test-123",
            data={},
            session_type="test",
            user_identity="user-oid-123@test-tenant-id",
            user_token="test-token",
            token_claims={},
        )

        # Token with no oid/sub or tid — identity cannot be extracted
        server.validate_token = AsyncMock(return_value={"appid": "some-app-id", "name": "App Without Identity Claims"})

        is_authorized = await server._verify_session_ownership(session, "valid-token")
        assert is_authorized is False

    @pytest.mark.asyncio
    async def test_verify_session_ownership_delegated_token_no_appid(self, mock_entra_env):
        """Test that delegated user tokens without appid/azp are allowed when user identity matches."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="test-123",
            data={},
            session_type="test",
            user_identity="user-oid-789@test-tenant-id",
            user_token="test-token",
            token_claims={},
        )

        # Token with NO appid/azp (delegated user token from OAuth code flow)
        server.validate_token = AsyncMock(
            return_value={"oid": "user-oid-789", "tid": "test-tenant-id", "name": "Direct User"}
        )

        is_authorized = await server._verify_session_ownership(session, "delegated-token")
        assert is_authorized is True

    @pytest.mark.asyncio
    async def test_verify_session_ownership_delegated_token_wrong_user(self, mock_entra_env):
        """Test that delegated user tokens are denied when user identity does not match."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="test-123",
            data={},
            session_type="test",
            user_identity="owner-oid@test-tenant-id",
            user_token="test-token",
            token_claims={},
        )

        # Token with NO appid/azp but DIFFERENT user identity
        server.validate_token = AsyncMock(
            return_value={"oid": "attacker-oid", "tid": "test-tenant-id", "name": "Other User"}
        )

        is_authorized = await server._verify_session_ownership(session, "other-user-token")
        assert is_authorized is False

    @pytest.mark.asyncio
    async def test_get_or_create_session_with_ownership_check(self, mock_entra_env):
        """Test that _get_or_create_session validates ownership when loading existing session."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        # Create a session
        session_id = server.session_manager.create_session(
            data={},
            metadata={"type": "test"},
            user_identity="user@example.com",
            user_token="test-token",
            token_claims={},
        )

        # Mock ownership verification to deny access
        server._verify_session_ownership = AsyncMock(return_value=False)

        # Set token and user identity in context
        set_current_request_token("unauthorized-token")
        from ...code_execution.sessions import set_current_user_identity

        set_current_user_identity("user@example.com")

        # Try to load session - should raise PermissionError
        with pytest.raises(PermissionError, match="Not authorized to access session"):
            await server._get_or_create_session("test_tool", session_id=session_id)

    @pytest.mark.asyncio
    async def test_get_or_create_session_authorized_access(self, mock_entra_env):
        """Test that _get_or_create_session allows access for authorized caller."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        # Create a session
        session_id = server.session_manager.create_session(
            data={},
            metadata={"type": "test"},
            user_identity="user@example.com",
            user_token="test-token",
            token_claims={},
        )

        # Mock ownership verification to allow access
        server._verify_session_ownership = AsyncMock(return_value=True)

        # Set token and user identity in context
        set_current_request_token("authorized-token")
        from ...code_execution.sessions import set_current_user_identity

        set_current_user_identity("user@example.com")

        # Load session - should succeed
        session = await server._get_or_create_session("test_tool", session_id=session_id)
        assert session.session_id == session_id
        assert session.user_identity == "user@example.com"


class TestSessionTokenRefresh:
    """Tests for session token refresh on existing sessions."""

    @pytest.fixture(autouse=True)
    def _clean_context(self):
        """Reset context variables after each test to prevent cross-test pollution."""
        yield
        set_current_request_token(None)
        from ...code_execution.sessions import set_current_user_identity, set_current_token_claims

        set_current_user_identity(None)
        set_current_token_claims(None)

    @pytest.mark.asyncio
    async def test_refresh_session_token_updates_stale_token(self, mock_entra_env):
        """Token stored on session should be updated when a fresh context token is available."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="sess-1",
            data={},
            session_type="test",
            user_identity="user@example.com",
            user_token="old-stale-token",
            token_claims={},
        )

        # Simulate a new request with a fresh token
        set_current_request_token("fresh-new-token")

        server._refresh_session_token(session)

        assert session.user_token == "fresh-new-token"

    @pytest.mark.asyncio
    async def test_refresh_session_token_syncs_token_claims(self, mock_entra_env):
        """token_claims should be updated from context when the token changes."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig
        from ...code_execution.sessions import set_current_token_claims

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        old_claims = {"oid": "user-oid", "exp": 1000}
        session = Session(
            session_id="sess-claims",
            data={},
            session_type="test",
            user_identity="user@example.com",
            user_token="old-token",
            token_claims=old_claims,
        )

        new_claims = {"oid": "user-oid", "exp": 9999}
        set_current_request_token("new-token")
        set_current_token_claims(new_claims)

        server._refresh_session_token(session)

        assert session.user_token == "new-token"
        assert session.token_claims == new_claims

    @pytest.mark.asyncio
    async def test_refresh_session_token_recreates_data_manager(self, mock_entra_env):
        """data_manager should be recreated with the fresh token so its OBO provider is up to date."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="sess-2",
            data={},
            session_type="test",
            user_identity="user@example.com",
            user_token="old-stale-token",
            token_claims={},
        )

        original_data_manager = session.data_manager

        set_current_request_token("fresh-new-token")

        server._refresh_session_token(session)

        assert session.user_token == "fresh-new-token"
        # data_manager must have been replaced (not the same object)
        assert session.data_manager is not original_data_manager

    @pytest.mark.asyncio
    async def test_refresh_session_token_noop_when_same(self, mock_entra_env):
        """No update should occur when the context token matches the session token."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="sess-3",
            data={},
            session_type="test",
            user_identity="user@example.com",
            user_token="same-token",
            token_claims={},
        )

        set_current_request_token("same-token")

        server._refresh_session_token(session)

        # Token stays the same (no unnecessary mutation)
        assert session.user_token == "same-token"

    @pytest.mark.asyncio
    async def test_refresh_session_token_noop_when_no_context_token(self, mock_entra_env):
        """No update should occur when there is no fresh token in context."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        session = Session(
            session_id="sess-4",
            data={},
            session_type="test",
            user_identity="user@example.com",
            user_token="original-token",
            token_claims={},
        )

        set_current_request_token(None)

        server._refresh_session_token(session)

        assert session.user_token == "original-token"

    @pytest.mark.asyncio
    async def test_get_or_create_session_refreshes_token_for_existing_session(self, mock_entra_env):
        """_get_or_create_session should refresh the token when returning an existing session."""
        from ...code_execution import CodeExecutionServer
        from ...code_execution.code_execution_models import EnvironmentConfig
        from ...code_execution.sessions import set_current_user_identity

        config = EnvironmentConfig(name="test", type="uv", description="Test", dependency_file="# Test")
        server = CodeExecutionServer(environment_config=config, auth_config=create_noop_auth_config())

        # Create a session with an old token
        session_id = server.session_manager.create_session(
            data={},
            metadata={"type": "test"},
            user_identity="user@example.com",
            user_token="old-token",
            token_claims={},
        )

        # Mock ownership verification to allow access
        server._verify_session_ownership = AsyncMock(return_value=True)

        # Set a fresh token in context (simulating a new request)
        set_current_request_token("refreshed-token")
        set_current_user_identity("user@example.com")

        session = await server._get_or_create_session("test_tool", session_id=session_id)
        assert session.user_token == "refreshed-token"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
