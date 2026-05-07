"""Decision log middleware implementations.

The classes exported here implement the Agora middleware protocol ABCs
(:class:`~middleware.protocols.ChatMiddleware` and
:class:`~middleware.protocols.ContextProvider`).  They are framework-agnostic
and do **not** require ``agent_framework``.

To use them inside a MAF agent, wrap with the helpers in
:mod:`~middleware.decision_log.adapters.maf_protocols` (which *does*
require the ``maf`` extra):

    from middleware.decision_log.adapters import (
        DecisionLogChatMiddleware,
        DecisionLogContextProvider,
    )
    from middleware.decision_log.adapters.maf_protocols import (
        MAFChatClientAdapter,
        wrap_chat_middleware,
        wrap_context_provider,
    )

    log = DecisionLog()
    agora_mw = DecisionLogChatMiddleware(
        log, "agent", MAFChatClientAdapter(maf_client)
    )
    agora_provider = DecisionLogContextProvider(log)

    agent = Agent(
        ...,
        middleware=[wrap_chat_middleware(agora_mw)],
        context_providers=[wrap_context_provider(agora_provider)],
    )
"""

from .maf_chat_middleware import DecisionLogChatMiddleware
from .maf_context_provider import DecisionLogContextProvider

__all__ = ["DecisionLogChatMiddleware", "DecisionLogContextProvider"]
