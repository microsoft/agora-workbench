"""
On-Behalf-Of (OBO) authentication helpers.

These functions are used in multi-tenant enterprise deployments where the
backend service needs to act on behalf of a signed-in user. They exchange
the user's JWT token for a downstream token that carries the user's identity.

For single-user / open-source deployments:
    Set OBO_SIMULATION_MODE=true to bypass OBO and use your local credentials
    (Azure CLI / Managed Identity) directly. This is the correct production
    mode for single-user environments.

Features that require OBO (enterprise-only):
    - Azure RBAC permission checks (data_lake/tools/permissions.py)
    - User-scoped search queries (data_lake/tools/adapters/maf.py)
    - Per-user tool search (tools/search/azure_ai_tool_search.py)
"""

# Re-export from the main auth module for backward compatibility
from .auth import create_async_obo_credential, create_obo_credential

__all__ = [
    "create_async_obo_credential",
    "create_obo_credential",
]
