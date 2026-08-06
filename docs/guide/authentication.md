# Authentication options

Agora Workbench uses a pluggable authentication architecture. Three abstract interfaces form the contract, with built-in implementations for Azure Entra ID and a no-op mode for local development.

## Architecture

Authentication is configured via an `AuthConfig` dataclass that bundles three providers:

```python
@dataclass
class AuthConfig:
    token_validator: TokenValidator
    identity_extractor: IdentityExtractor
    credential_provider_factory: Optional[Callable[[str], CredentialProvider]] = None
    www_authenticate_value: str = ""
```

| Interface | Responsibility |
|-----------|----------------|
| `TokenValidator` | Validates bearer tokens, returns decoded claims |
| `IdentityExtractor` | Derives a unique user identity string from claims |
| `CredentialProvider` | Provides credentials for downstream resource access |
| `credential_provider_factory` | Optional callable `(user_token) → CredentialProvider` for per-session credentials |

## Built-in configurations

### No-op (local development)

Disables authentication entirely — all requests are accepted with a synthetic identity:

```python
from agora_workbench.code_execution.auth import create_noop_auth_config

server = CodeExecutionServer(
    server_config=config,
    auth_config=create_noop_auth_config(),
)
```

!!! warning
    Never use no-op auth in production. It accepts any request without validation.

### Azure Entra ID

Full production auth with JWT validation, user identity extraction, and downstream credential provisioning:

```python
from agora_workbench.code_execution.auth.entra import create_entra_auth_config

server = CodeExecutionServer(
    server_config=config,
    auth_config=create_entra_auth_config(),
)
```

Required environment variables:

| Variable | Description |
|----------|-------------|
| `ENTRA_CLIENT_ID` | App registration client ID |
| `ENTRA_TENANT_ID` | Azure AD tenant ID |

## Downstream credential provisioning

MCP servers often need to access downstream Azure resources (Storage, AI Search, etc.). The built-in `EntraCredentialProvider` uses Azure Managed Identity:

- **User-assigned identity** — set `AZURE_CLIENT_ID` to the managed identity client ID
- **System-assigned identity** — leave `AZURE_CLIENT_ID` unset

```python
from agora_workbench.code_execution.auth.entra import EntraCredentialProvider

# Tokens for downstream access (e.g., Azure Storage)
provider = EntraCredentialProvider()  # uses AZURE_CLIENT_ID if set
token = await provider.get_token("https://storage.azure.com/.default")
```

For local development without managed identity, use `create_noop_auth_config()` which provides a `NoOpCredentialProvider` that returns synthetic tokens.

## Middleware behavior

The `AuthMiddleware` (Starlette level) runs on every request:

- **Protected paths**: `/mcp`, `/object-transfer/*` — require valid bearer token
- **Bypassed paths**: `/health`, `/.well-known/*` — no auth required
- **On success**: stores token, claims, and user identity in request context
- **On failure**: returns 401 with RFC 9728 `WWW-Authenticate` header for OAuth discovery

## Custom auth implementations

Implement the three interfaces to integrate with any identity provider:

```python
from agora_workbench.code_execution.auth.base import (
    AccessToken,
    AuthConfig,
    CredentialProvider,
    IdentityExtractor,
    TokenValidator,
)

class MyTokenValidator(TokenValidator):
    async def validate(self, token: str, **kwargs) -> dict:
        # Validate JWT signature, expiry, audience
        claims = verify_token(token)
        return claims

class MyIdentityExtractor(IdentityExtractor):
    def extract(self, claims: dict) -> str | None:
        # Return a unique user identifier
        return claims.get("sub")

class MyCredentialProvider(CredentialProvider):
    async def get_token(self, scope: str) -> AccessToken:
        # Acquire token for downstream resource
        token = await my_token_source(scope)
        return AccessToken(token=token.value, expires_on=token.expires)

my_auth = AuthConfig(
    token_validator=MyTokenValidator(),
    identity_extractor=MyIdentityExtractor(),
    credential_provider_factory=lambda user_token: MyCredentialProvider(),
)

server = CodeExecutionServer(server_config=config, auth_config=my_auth)
```

### Using a custom AuthConfig with the connector CLI

Connector servers (`mcp-connector-server`) select their backend from the environment, so a custom `AuthConfig` can be injected without forking the CLI. Point `CONNECTOR_AUTH_FACTORY` at a `"module.path:factory"` returning an `AuthConfig`:

```python
# my_pkg/auth.py
from agora_workbench.code_execution.auth import AuthConfig

def create_auth_config() -> AuthConfig:
    return AuthConfig(
        token_validator=MyTokenValidator(),
        identity_extractor=MyIdentityExtractor(),
        protected_resource_metadata={"resource": "https://my-connector.example.com"},
    )
```

```bash
export CONNECTOR_AUTH_FACTORY="my_pkg.auth:create_auth_config"
mcp-connector-server
```

The attribute may be dotted (`my_pkg.auth:Backend.create`) to reach a classmethod. The package providing it must be installed in the connector's environment; if it can't be imported, the resolved attribute is missing, or the factory returns something other than an `AuthConfig`, the connector fails to start with an explicit error rather than falling back to another backend.

Alternatively, a downstream package can ship its own console script that reuses all of the CLI's environment parsing and server selection:

```python
# my_pkg/console.py
from agora_workbench.connector.cli import main

def run() -> None:
    main(auth_config_factory=create_auth_config)
```

An explicitly passed `auth_config_factory` takes precedence over `CONNECTOR_AUTH_FACTORY`, which takes precedence over the Entra ID variables.

### Connector auth resolution order

`build_auth_config()` selects a backend in this order:

| Precedence | Backend | Selected by |
|------------|---------|-------------|
| 1 | Caller-supplied factory | `main(auth_config_factory=...)` |
| 2 | Custom factory | `CONNECTOR_AUTH_FACTORY="module.path:factory"` |
| 3 | Entra ID | `ENTRA_CLIENT_ID` **and** `ENTRA_TENANT_ID` |
| 4 | No-op | `CONNECTOR_ALLOW_NOOP_AUTH=1` |

!!! warning "Unauthenticated startup is opt-in"
    If none of the above is configured, the connector **exits with a configuration error** instead of starting with authentication disabled. This means a missing or misspelled `ENTRA_CLIENT_ID` fails the deployment rather than quietly running unprotected. Setting only one of the two Entra variables is also an error. For local development, set `CONNECTOR_ALLOW_NOOP_AUTH=1` to explicitly request no-op auth.

## Agent-side credentials

On the agent side (client connecting to MCP servers), `auth/auth.py` provides a `ChainedTokenCredential` that tries in order:

1. `AzureCliCredential` — for local development (`az login`)
2. `ManagedIdentityCredential` — for deployed Azure resources
