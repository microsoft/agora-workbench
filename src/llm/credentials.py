"""Default credential factory wiring agora's Azure credential chain.

Kept in its own module so :mod:`llm.spec` and the framework
factories don't take a hard dependency on :mod:`auth` — tests can substitute
a fake factory without touching auth at all.
"""

from __future__ import annotations

from typing import Any, Callable


def default_credential_factory(scope: str) -> Callable[[], Any]:
    """Return a zero-arg callable that yields a credential for ``scope``.

    The returned callable is what :class:`~llm.spec.ModelSpec`
    stores in its ``credential_factory`` field. Each call constructs a fresh
    bearer-token provider via :func:`utilities.auth.providers.get_token_provider`,
    which itself uses the standard ``AzureCli → ManagedIdentity`` chain.

    Parameters
    ----------
    scope : str
        The OAuth scope to request (e.g.
        ``"https://cognitiveservices.azure.com/.default"`` for public Azure
        OpenAI, or whatever scope your internal gateway requires).

    Returns
    -------
    Callable[[], Any]
        A zero-arg factory suitable for ``ModelSpec.credential_factory``.
    """

    def _factory() -> Any:
        # Import locally so importing this module never forces the auth
        # package to load (keeps import-time cheap for unit tests).
        from code_execution.auth import get_token_provider

        return get_token_provider(scope)

    return _factory
