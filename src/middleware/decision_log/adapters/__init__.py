"""MAF adapters for decision log middleware.

Requires the ``maf`` extra: ``pip install agora-workbench[maf]``

The classes exported here implement the Agora middleware protocol ABCs
(:class:`~middleware.protocols.ChatMiddleware` and
:class:`~middleware.protocols.ContextProvider`) and must be wrapped for
use inside a MAF agent:

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
