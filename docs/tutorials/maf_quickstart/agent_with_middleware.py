"""
MAF + agora-workbench quickstart — middleware variant (optional).

Same agent as ``agent.py`` (chat client + data lake search + chemistry MCP),
but layered with three pieces of agora-workbench middleware to demonstrate
the framework-agnostic protocol + MAF adapter pattern:

  * ``DecisionLogChatMiddleware`` — observes every LLM round-trip and
    asynchronously synthesises a one-line "what did the agent decide"
    entry into a shared :class:`DecisionLog`.
  * ``DecisionLogContextProvider`` — before each agent run, injects the
    accumulated decision log as a ``<decision_log>`` system message so
    the agent can see its own history.
  * ``VignetteFunctionMiddleware`` (tool-learning) — wraps every tool call
    so it can (a) check Azure-AI-Search-backed anti-pattern guardrails
    before execution, and (b) attempt repair using stored vignettes when
    a tool call fails.

All three are concrete implementations of framework-agnostic protocols
defined in ``src/middleware/protocols/``. The ``maf_protocols`` adapter
wraps each one for use as a native MAF middleware / context provider.

Run from the repo root:

    uv run python docs/tutorials/maf_quickstart/agent_with_middleware.py

Prerequisites in addition to ``agent.py``:

  * ``.env`` should set ``TOOL_LEARNING_SEARCH_ENDPOINT`` and/or
    ``TOOL_LEARNING_TABLE_ENDPOINT`` to enable
    :class:`VignetteFunctionMiddleware`. If both are unset the function
    middleware is silently skipped (the chat middleware + context provider
    will still run).
  * ``az login`` (the same ChainedTokenCredential is used to authenticate
    to Table Storage and Azure AI Search).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# Make the repo's `src/` packages importable when running this script directly,
# and make sibling tutorial modules (``chat_client.py``, ``agent.py``) importable too.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(HERE))

# Reuse the building blocks from the base tutorial so this file stays focused
# on the middleware wiring rather than re-explaining the agent setup.
from agent import (  # noqa: E402
    _nullcontext,
    step_a_chat_client,
    step_b_data_lake_tool,
    step_c_chemistry_tool,
    step_d_build_agent,
    step_e_run,
)

LOGGER = logging.getLogger("maf_quickstart_middleware")


# ---------------------------------------------------------------------------
# Step F — build the decision-log middleware + context provider
# ---------------------------------------------------------------------------
def step_f_build_middleware(chat_client):
    """Wire DecisionLog middleware + context provider for a MAF agent.

    Returns a ``(middlewares, context_providers, log, chat_mw)`` tuple.

    The ``log`` and ``chat_mw`` references are returned so ``main()`` can
    flush pending synthesis at the end of the run and print the captured
    entries — useful for inspecting what the middleware actually recorded.
    """
    from middleware.decision_log import DecisionLog
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

    # Synthesis (turning raw events into a one-line summary) is done by a
    # small LLM call. Reuse the same MAF chat client the agent uses.
    chat_mw = DecisionLogChatMiddleware(
        log,
        agent_name="chem_quickstart_agent",
        chat_client=MAFChatClientAdapter(chat_client),
    )

    # Context provider injects a read-only view of the log before each run.
    # Pass the chat middleware so its synthesis queue is flushed first.
    ctx_provider = DecisionLogContextProvider(log, chat_middleware=chat_mw)

    middlewares = [wrap_chat_middleware(chat_mw)]
    context_providers = [wrap_context_provider(ctx_provider)]
    LOGGER.info("Step F: built decision-log middleware + context provider")
    return middlewares, context_providers, log, chat_mw


# ---------------------------------------------------------------------------
# Step G — (optional) tool-learning function middleware
# ---------------------------------------------------------------------------
def step_g_build_tool_learning_middleware():
    """Wire VignetteFunctionMiddleware if Azure resources are configured.

    Returns a list with one wrapped middleware, or an empty list if both
    ``TOOL_LEARNING_SEARCH_ENDPOINT`` and ``TOOL_LEARNING_TABLE_ENDPOINT``
    are unset — letting the tutorial run end-to-end without those services.
    """
    if not os.getenv("TOOL_LEARNING_SEARCH_ENDPOINT") and not os.getenv(
        "TOOL_LEARNING_TABLE_ENDPOINT"
    ):
        LOGGER.info(
            "Step G: skipped — TOOL_LEARNING_{SEARCH,TABLE}_ENDPOINT unset"
        )
        return []

    from auth import get_purview_credential
    from middleware.decision_log.adapters.maf_protocols import (
        wrap_function_middleware,
    )
    from middleware.tool_learning.adapters import VignetteFunctionMiddleware
    from middleware.tool_learning.config import ToolLearningConfig

    config = ToolLearningConfig.from_env()
    # Reuse the repo's standard sync ChainedTokenCredential
    # (AzureCLI -> ManagedIdentity). The "purview" name is incidental
    # -- it's a generic factory for the sync credential chain.
    credential = get_purview_credential()

    fn_mw = VignetteFunctionMiddleware(
        config=config,
        credential=credential,
        # Don't write new vignettes from the tutorial run — keep the
        # shared index clean. Set ``write_vignettes=True`` in your own
        # code to enable the learning loop.
        write_vignettes=False,
    )
    LOGGER.info(
        "Step G: built VignetteFunctionMiddleware (search=%s, table=%s)",
        bool(config.search_endpoint),
        bool(config.table_storage_endpoint),
    )
    return [wrap_function_middleware(fn_mw)]


# ---------------------------------------------------------------------------
# Step D' — assemble the agent with middleware attached
# ---------------------------------------------------------------------------
def step_d_build_agent_with_middleware(
    chat_client, tools, middlewares, context_providers
):
    """Like ``step_d_build_agent`` but also threads middlewares + providers.

    MAF's ``as_agent`` (and ``create_agent``) accept ``middleware=...`` and
    ``context_providers=...`` kwargs alongside the usual ``tools=...``.
    """
    # Build the same instructions/skill-injected agent as the base tutorial
    # so we get the identical system prompt, then re-create it with the
    # middleware kwargs. We can't just mutate the returned agent because
    # MAF freezes those at construction time, so we rebuild against the
    # same chat_client. ``instructions`` lives in ``default_options``.
    base = step_d_build_agent(chat_client, tools)
    instructions = base.default_options.get("instructions", "")

    agent = chat_client.as_agent(
        name="chem_quickstart_agent",
        instructions=instructions,
        tools=tools,
        middleware=middlewares,
        context_providers=context_providers,
    )
    LOGGER.info(
        "Step D': built agent with %d tool(s), %d middleware(s), "
        "%d context provider(s)",
        len(tools),
        len(middlewares),
        len(context_providers),
    )
    return agent


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    chat_client = step_a_chat_client()
    data_lake_tool = await step_b_data_lake_tool()
    chemistry_tool = await step_c_chemistry_tool()

    tools = [t for t in (data_lake_tool, chemistry_tool) if t is not None]
    if not tools:
        LOGGER.error(
            "No tools available. Configure the data lake and/or start the "
            "chemistry MCP server, then re-run."
        )
        return 1

    middlewares, context_providers, log, chat_mw = step_f_build_middleware(
        chat_client
    )
    # MAF's ``middleware=`` kwarg accepts a mixed list of ChatMiddleware and
    # FunctionMiddleware; broaden the inferred type so step_g's
    # FunctionMiddleware can be appended without a Pyright complaint.
    middlewares: list = list(middlewares)
    middlewares.extend(step_g_build_tool_learning_middleware())
    agent = step_d_build_agent_with_middleware(
        chat_client, tools, middlewares, context_providers
    )

    async with chemistry_tool if chemistry_tool is not None else _nullcontext():
        await step_e_run(agent)

    # Drain any background synthesis tasks so all decision log entries land
    # before we print them. Otherwise the log may be empty/short at exit.
    await chat_mw.flush()

    print("\n" + "=" * 70)
    print("DECISION LOG (captured by DecisionLogChatMiddleware)")
    print("=" * 70)
    print(log.to_context_string())
    print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
