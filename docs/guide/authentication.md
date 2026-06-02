# Authentication options

Agora Workbench uses a pluggable authentication architecture. Three abstract interfaces form the contract, with built-in implementations for Azure Entra ID and a no-op mode for local development.

## Architecture

Authentication is configured via an `AuthConfig` dataclass that bundles three providers:

```python
@dataclass
class AuthConfig:
    token_validator: TokenValidator
    identity_extractor: IdentityExtractor
    credential_provider: CredentialProvider
```

| Interface | Responsibility |
|-----------|----------------|
| `TokenValidator` | Validates bearer tokens, returns decoded claims |
| `IdentityExtractor` | Derives a unique user identity string from claims |
| `CredentialProvider` | Provides credentials for downstream resource access |

## Built-in configurations

### No-op (local development)

Disables authentication entirely — all requests are accepted with a synthetic identity:

```python
from code_execution.auth import create_noop_auth_config

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
from code_execution.auth.entra import create_entra_auth_config

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

## Credential modes for downstream access

MCP servers often need to access downstream Azure resources (Storage, AI Search, etc.) on behalf of the user. The `CredentialProvider` supports three modes, chosen by priority:

| Priority | Mode | When active | Use case |
|:---:|------|-------------|----------|
| 1 | **Simulation** | `OBO_SIMULATION_MODE=true` | Local dev — uses `az login` credentials |
| 2 | **Managed Identity** | `AZURE_CLIENT_ID` is set | Container with managed identity |
| 3 | **Federated Token (OBO)** | Fallback | Exchange user token via workload identity |

### Simulation mode

For local development, uses the developer's Azure CLI credentials:

```bash
export OBO_SIMULATION_MODE=true
az login
python -m my_server
```

### Managed Identity

For deployed containers with a user-assigned managed identity:

```bash
export AZURE_CLIENT_ID=<your-managed-identity-client-id>
```

### Federated Token (OBO)

For Kubernetes/AKS with workload identity federation:

```bash
export ENTRA_CLIENT_ID=<app-client-id>
export ENTRA_TENANT_ID=<tenant-id>
export AZURE_FEDERATED_TOKEN_FILE=/var/run/secrets/tokens/azure-identity
```

## Middleware behavior

The `AuthMiddleware` (Starlette level) runs on every request:

- **Protected paths**: `/mcp`, `/object-transfer/*` — require valid bearer token
- **Bypassed paths**: `/health`, `/.well-known/*` — no auth required
- **On success**: stores token, claims, and user identity in request context
- **On failure**: returns 401 with RFC 9728 `WWW-Authenticate` header for OAuth discovery

## Custom auth implementations

Implement the three interfaces to integrate with any identity provider:

```python
from code_execution.auth.base import (
    TokenValidator,
    IdentityExtractor,
    CredentialProvider,
    AuthConfig,
)

class MyTokenValidator(TokenValidator):
    async def validate(self, token: str, **kwargs) -> dict:
        # Validate JWT signature, expiry, audience
        claims = verify_token(token)
        return claims

class MyIdentityExtractor(IdentityExtractor):
    def extract(self, claims: dict) -> str:
        # Return a unique user identifier
        return claims["sub"]

class MyCredentialProvider(CredentialProvider):
    async def get_credential(self, token: str, scopes: list[str]):
        # Return credentials for downstream access
        ...

my_auth = AuthConfig(
    token_validator=MyTokenValidator(),
    identity_extractor=MyIdentityExtractor(),
    credential_provider=MyCredentialProvider(),
)

server = CodeExecutionServer(server_config=config, auth_config=my_auth)
```

## Agent-side credentials

On the agent side (client connecting to MCP servers), `auth/auth.py` provides a `ChainedTokenCredential` that tries in order:

1. `AzureCliCredential` — for local development (`az login`)
2. `ManagedIdentityCredential` — for deployed Azure resources
