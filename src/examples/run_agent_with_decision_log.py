"""
==================================================
Example: Agent With Decision Log Inspection
==================================================

Wraps the no-code-no-tool example with DecisionLog middleware
so you can observe decisions being recorded (via LLM synthesis
using the TRAPI gpt-4o-mini deployment) and injected back
into the agent's context.

Domain skills are discovered on demand via ``query_state_graph``
and loaded with ``load_skill``.

Recording uses a ChatMiddleware (fires per LLM round-trip).
Context injection uses a BaseContextProvider (fires per agent.run()).
"""

import asyncio
import logging
import os

from agent_framework.azure import AzureOpenAIChatClient
from agent_bot.agora import AgoraAgent
from auth import create_entra_token_provider
from dotenv import load_dotenv
from log_config import setup_logging
from middleware.decision_log import (
    DecisionLog,
    DecisionLogChatMiddleware,
    DecisionLogContextProvider,
)

log_path = setup_logging(__file__)
load_dotenv(verbose=True, override=True)

# -- Build a standalone chat client for the decision log synthesiser --
SYNTHESIS_DEPLOYMENT = "gpt-4.1-mini_2025-04-14"

endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
scope = os.environ["AOAI_SCOPE"]
api_version = os.environ["API_VERSION"]
token_provider = create_entra_token_provider(scope)

synthesis_client = AzureOpenAIChatClient(
    endpoint=endpoint,
    api_version=api_version,
    deployment_name=SYNTHESIS_DEPLOYMENT,
    credential=token_provider,
)

# -- Wire up the decision log --
decision_log = DecisionLog()

logging.getLogger("middleware.decision_log").setLevel(logging.DEBUG)
_console = logging.StreamHandler()
_console.setLevel(logging.DEBUG)
_console.setFormatter(logging.Formatter("%(name)s | %(levelname)s | %(message)s"))
logging.getLogger("middleware.decision_log").addHandler(_console)

# ChatMiddleware records decisions at each LLM round-trip
chat_mw = DecisionLogChatMiddleware(
    decision_log=decision_log,
    agent_name="agora",
    chat_client=synthesis_client,
)

# ContextProvider injects the log into the agent's context
ctx_provider = DecisionLogContextProvider(
    decision_log=decision_log,
    inject_context=True,
    chat_middleware=chat_mw,
)

agent = AgoraAgent(
    llm="gpt-5.1_2025-11-13",
    context_providers=[ctx_provider],
    middleware=[chat_mw],
)


async def main():
    prompt = (
        "Analyze the topology of the network in "
        "texas_elec_no_flex_s100_c50_ec_lv1.0_1H_E.nc. "
        "How many buses, lines, and generators does it have? "
        "Is the network fully connected? "
        "Are there any critical lines (bridges) whose failure "
        "would disconnect the network?"
    )

    async with agent:
        result = await agent.go(prompt)

        # Drain any pending background synthesis before inspecting the log
        await chat_mw.flush()

        # --- Decision log inspection ---
        print("\n" + "=" * 60)
        print("DECISION LOG — all entries recorded during this run")
        print("=" * 60)
        if decision_log.entries:
            for i, entry in enumerate(decision_log.entries, 1):
                print(f"\n--- Entry {i} ---")
                print(f"  Timestamp : {entry.timestamp}")
                print(f"  Agent     : {entry.agent}")
                print(f"  Summary   : {entry.summary}")
                if entry.evidence:
                    print(f"  Evidence  : {entry.evidence}")
        else:
            print("(no entries were recorded)")

        print("\n" + "=" * 60)
        print("CONTEXT STRING (what the agent would see)")
        print("=" * 60)
        print(decision_log.to_context_string())

        print(f"\n✅ Final answer: {result.text}")
        print(f"\n📄 Full log file: {log_path}")


if __name__ == "__main__":
    asyncio.run(main())
