"""Tool-learning middleware implementations.

The classes exported here implement the Agora middleware protocol ABCs
(:class:`~middleware.protocols.FunctionMiddleware` and
:class:`~middleware.protocols.ContextProvider`).  They are framework-agnostic
and do **not** require ``agent_framework``.

To use them inside a MAF agent, wrap with the helpers in
:mod:`~middleware.decision_log.adapters.maf_protocols` (which *does*
require the ``maf`` extra):

    from middleware.tool_learning.adapters import (
        VignetteFunctionMiddleware,
        VignetteRunMiddleware,
    )
    from middleware.decision_log.adapters.maf_protocols import (
        wrap_function_middleware,
        wrap_context_provider,
    )

    fn_mw = wrap_function_middleware(VignetteFunctionMiddleware(config, credential))
    run_provider = wrap_context_provider(VignetteRunMiddleware(config, credential))

    agent = Agent(
        ...,
        middleware=[fn_mw],
        context_providers=[run_provider],
    )
"""

from .maf_function import VignetteFunctionMiddleware
from .maf_run import VignetteRunMiddleware

__all__ = ["VignetteFunctionMiddleware", "VignetteRunMiddleware"]
