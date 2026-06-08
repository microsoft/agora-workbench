"""
Base class for MCP servers providing shared HTTP hosting, auth middleware,
and standard endpoints.

Both CodeExecutionServer (kernel-backed) and ConnectorServer (proxy-only)
inherit from this to avoid divergence in their transport/auth layers.
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional

import uvicorn
from fastapi import HTTPException
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from agora_workbench.code_execution.auth.base import AuthConfig

LOGGER = logging.getLogger(__name__)


class BaseMCPServer(ABC):
    """Abstract base for MCP servers with shared HTTP hosting infrastructure.

    Subclasses must implement:
    - ``_startup()`` / ``_shutdown()``: lifecycle hooks
    - ``_health_payload()``: dict returned by GET /health
    - ``_catalog_payload()``: dict returned by GET /catalog
    - ``_extract_user_identity(token_data)``: extract user ID from claims

    Subclasses may override:
    - ``_create_middleware()``: to add or replace middleware layers
    - ``_add_custom_endpoints(app)``: to add server-specific routes
    - ``_auth_protected_paths()``: paths requiring Bearer auth
    - ``_auth_skip_paths()``: path prefixes that skip auth entirely
    """

    # These attributes must be set by subclass __init__
    mcp: FastMCP
    auth_config: "AuthConfig"
    entra_client_id: Optional[str]
    entra_tenant_id: Optional[str]

    def __init__(self) -> None:
        self._bind_host: str = "0.0.0.0"
        self._bind_port: int = 8000

    # ========================================================================
    # Lifecycle (abstract)
    # ========================================================================

    @abstractmethod
    async def _startup(self) -> None:
        """Initialize server resources (called once before serving)."""
        raise NotImplementedError

    @abstractmethod
    async def _shutdown(self) -> None:
        """Clean up server resources."""
        raise NotImplementedError

    # ========================================================================
    # Endpoint payloads (abstract)
    # ========================================================================

    @abstractmethod
    async def _health_payload(self) -> dict[str, Any]:
        """Return the JSON body for GET /health."""
        raise NotImplementedError

    @abstractmethod
    async def _catalog_payload(self) -> dict[str, Any]:
        """Return the JSON body for GET /catalog."""
        raise NotImplementedError

    # ========================================================================
    # Auth helpers
    # ========================================================================

    @abstractmethod
    def _extract_user_identity(self, token_data: dict) -> Optional[str]:
        """Extract a unique user identity string from validated token claims."""
        raise NotImplementedError

    async def validate_token(self, token: str, request_path: str = "/mcp", request_method: str = "POST") -> dict:
        """Validate a bearer token using the configured TokenValidator.

        Returns decoded token claims if valid.
        Raises HTTPException if token is invalid.
        """
        from agora_workbench.code_execution.auth.base import TokenValidationError

        try:
            return await self.auth_config.token_validator.validate(
                token, request_path=request_path, request_method=request_method
            )
        except TokenValidationError as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            LOGGER.error("Token validation error: %s", e, exc_info=True)
            raise HTTPException(status_code=401, detail=f"Token validation failed: {e}")

    # ========================================================================
    # HTTP hosting
    # ========================================================================

    async def run_http(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        """Run the MCP server with StreamableHTTP transport."""
        self._bind_host = host
        self._bind_port = port

        await self._startup()

        # Build the Streamable HTTP app
        app = self.mcp.http_app(transport="streamable-http")

        # Add middleware (outermost-last ordering per Starlette convention)
        for middleware_cls, middleware_kwargs in self._create_middleware():
            app.add_middleware(middleware_cls, **middleware_kwargs)

        # Add endpoints
        self._add_custom_endpoints(app)

        # Run with uvicorn
        config = uvicorn.Config(app, host=host, port=port, log_level="info", ws="wsproto")
        server = uvicorn.Server(config)
        await server.serve()

    # ========================================================================
    # Standard endpoints
    # ========================================================================

    def _add_custom_endpoints(self, app) -> None:
        """Add standard endpoints. Subclasses should call super() then add their own."""
        server = self

        async def health_check(request: Request):
            payload = await server._health_payload()
            return JSONResponse(payload)

        app.routes.append(Route("/health", health_check, methods=["GET"]))
        app.routes.append(Route("/healthz", health_check, methods=["GET"]))

        async def catalog(request: Request):
            payload = await server._catalog_payload()
            return JSONResponse(payload)

        app.routes.append(Route("/catalog", catalog, methods=["GET"]))

        # OAuth 2.0 Protected Resource Metadata (RFC 9728)
        async def protected_resource_metadata(request: Request):
            if not server.entra_client_id or not server.entra_tenant_id:
                # When no Entra IDs are configured (e.g. noop auth), return a
                # minimal valid metadata document indicating no authorization is
                # required. This prevents agents that attempt OAuth discovery
                # (e.g. gh cli) from erroring on a 404 while remaining
                # compatible with agents that skip discovery entirely.
                if not server.auth_config.require_authorization_header:
                    host = request.headers.get("host", "localhost")
                    scheme = request.url.scheme
                    resource_url = f"{scheme}://{host}"
                    return JSONResponse(
                        {
                            "resource": resource_url,
                            "authorization_servers": [],
                            "bearer_methods_supported": ["header"],
                        }
                    )
                return JSONResponse(
                    {"error": "OAuth protected-resource metadata is not available."},
                    status_code=404,
                )
            return JSONResponse(
                {
                    "resource": f"api://{server.entra_client_id}",
                    "authorization_servers": [f"https://login.microsoftonline.com/{server.entra_tenant_id}/v2.0"],
                    "scopes_supported": [f"api://{server.entra_client_id}/.default"],
                    "bearer_methods_supported": ["header"],
                }
            )

        app.routes.append(Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]))

    # ========================================================================
    # Auth middleware
    # ========================================================================

    def _auth_protected_paths(self) -> list[str]:
        """Paths that require Bearer token authentication (exact match).

        Subclasses can override to protect additional paths.
        """
        return ["/mcp"]

    def _auth_skip_prefixes(self) -> list[str]:
        """Path prefixes that skip authentication entirely.

        Subclasses can override to add more skip prefixes.
        """
        return ["/health", "/healthz", "/.well-known/", "/catalog"]

    def _create_middleware(self) -> list[tuple[type, dict]]:
        """Create Starlette middleware list as (middleware_class, kwargs) tuples.

        The base implementation provides:
        - MCP session ID extraction middleware
        - Auth middleware (Bearer token validation with WWW-Authenticate)

        Subclasses can override to add additional middleware or change behavior.
        """
        from agora_workbench.code_execution.sessions.context import (
            set_current_request_token,
            set_current_token_claims,
            set_current_user_identity,
        )

        middleware: list[tuple[type, dict]] = []
        server = self

        class MCPSessionMiddleware:
            """Extract MCP session ID from headers and store in ASGI scope."""

            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return

                path = scope.get("path", "")
                if path == "/mcp":
                    headers = dict(scope.get("headers", []))
                    mcp_session_id = headers.get(b"mcp-session-id", b"").decode("utf-8")
                    scope["mcp_session_id"] = mcp_session_id or None

                await self.app(scope, receive, send)

        class AuthMiddleware:
            """Bearer token auth middleware with RFC 9728 WWW-Authenticate."""

            def __init__(self, app, server_instance, www_authenticate, require_authorization_header):
                self.app = app
                self.server_instance = server_instance
                self.www_authenticate = www_authenticate.encode("utf-8") if www_authenticate else b""
                self.require_authorization_header = require_authorization_header

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return

                path = scope.get("path", "")

                # Skip auth for configured prefixes
                for prefix in self.server_instance._auth_skip_prefixes():
                    if path == prefix or path.startswith(prefix):
                        await self.app(scope, receive, send)
                        return

                # Check if path requires auth
                protected_paths = self.server_instance._auth_protected_paths()
                requires_auth = any(path == p or path.startswith(p) for p in protected_paths)

                if not requires_auth:
                    await self.app(scope, receive, send)
                    return

                headers = dict(scope.get("headers", []))
                auth_header = headers.get(b"authorization", b"").decode("utf-8")

                # Build 401 response headers
                resp_401_headers: list[tuple[bytes, bytes]] = [(b"content-type", b"text/plain")]
                if self.www_authenticate:
                    resp_401_headers.append((b"www-authenticate", self.www_authenticate))

                if auth_header and auth_header.startswith("Bearer "):
                    token = auth_header.removeprefix("Bearer ")
                elif self.require_authorization_header:
                    await send({"type": "http.response.start", "status": 401, "headers": resp_401_headers})
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"Missing or invalid Authorization header. Please provide a valid Bearer token.",
                        }
                    )
                    return
                else:
                    token = ""

                try:
                    request_method = scope.get("method", "POST")
                    token_data = await self.server_instance.validate_token(
                        token, request_path=path, request_method=request_method
                    )

                    user_identity = self.server_instance._extract_user_identity(token_data)
                    if not user_identity:
                        await send({"type": "http.response.start", "status": 401, "headers": resp_401_headers})
                        await send(
                            {
                                "type": "http.response.body",
                                "body": b"Token missing required user identity claims.",
                            }
                        )
                        return

                    # Store auth context for downstream handlers
                    set_current_request_token(token)
                    set_current_token_claims(token_data)
                    set_current_user_identity(user_identity)

                except HTTPException as e:
                    resp_headers: list[tuple[bytes, bytes]] = [(b"content-type", b"text/plain")]
                    if e.status_code == 401 and self.www_authenticate:
                        resp_headers.append((b"www-authenticate", self.www_authenticate))
                    await send({"type": "http.response.start", "status": e.status_code, "headers": resp_headers})
                    await send({"type": "http.response.body", "body": e.detail.encode("utf-8")})
                    return

                except Exception as exc:
                    LOGGER.warning("Token validation failed: %s", exc, exc_info=True)
                    await send({"type": "http.response.start", "status": 401, "headers": resp_401_headers})
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"Authentication failed. Please provide a valid Bearer token.",
                        }
                    )
                    return

                await self.app(scope, receive, send)

        # MCPSessionMiddleware added first (runs outermost per Starlette convention)
        middleware.append((MCPSessionMiddleware, {}))

        # Resolve WWW-Authenticate value
        # When require_authorization_header is disabled (noop/open mode), skip
        # the WWW-Authenticate header entirely to avoid triggering OAuth discovery
        # flows in MCP clients.
        if not server.auth_config.require_authorization_header:
            www_authenticate = ""
        elif server.auth_config.www_authenticate_value:
            www_authenticate = server.auth_config.www_authenticate_value
        else:
            public_base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
            if public_base_url:
                metadata_url = f"{public_base_url}/.well-known/oauth-protected-resource"
            else:
                metadata_url = "/.well-known/oauth-protected-resource"
            www_authenticate = f'Bearer resource_metadata="{metadata_url}"'

        middleware.append(
            (
                AuthMiddleware,
                {
                    "server_instance": server,
                    "www_authenticate": www_authenticate,
                    "require_authorization_header": server.auth_config.require_authorization_header,
                },
            )
        )

        return middleware
