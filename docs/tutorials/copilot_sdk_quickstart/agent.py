"""
GitHub Copilot SDK + agora-workbench quickstart tutorial.

Runs a single economic-dispatch prompt against the agora-workbench energy
systems MCP server using the ``github-copilot-sdk`` Python SDK
(``CopilotClient``).

Unlike MAF / openai-agents, the Copilot CLI *is* the agent — there is no
chat client, no Agent object, no Runner. Your code just:

  1. (Optional) pick a BYOK provider; default uses the logged-in Copilot
     subscription (``copilot auth login``).
  2. Open a ``CopilotClient`` and ``create_session(...)`` with the MCP
     server config and an appended system message.
  3. ``send_and_wait(...)`` and print the reply.

For a side-by-side with the MAF / openai-agents quickstarts, see the
"Mapping to the sister quickstarts" table in the README.

Run from the repo root:

    uv run python docs/tutorials/copilot_sdk_quickstart/agent.py

Prerequisites:
  1. ``github-copilot-sdk`` installed (via the ``copilot-sdk`` extra or directly):
       uv add 'agora-agent[copilot-sdk]'   # or: uv pip install github-copilot-sdk
  2. Default ``copilot`` provider: ``copilot auth login`` once.
     BYOK: populate ``.env.agent`` (see .env.agent.example).
  3. Energy systems MCP server running locally:
       docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
       cd examples/domain_examples/energysystems && docker compose up -d --build
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env.agent")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("copilot_sdk_quickstart")

ENERGY_MCP_URL = os.getenv("ENERGYSYSTEMS_MCP_URL", "http://localhost:8022/mcp")
ENERGY_SKILL_PATH = (
    REPO_ROOT / "examples" / "domain_examples" / "energysystems" / "skills" / "SKILL.md"
)


# ---------------------------------------------------------------------------
# BYO-LLM: resolve model + optional ProviderConfig from $LLM_PROVIDER.
# ---------------------------------------------------------------------------
def resolve_llm() -> tuple[str, dict[str, Any] | None]:
    """Return ``(model, provider)`` for ``create_session(...)``.

    Supported ``$LLM_PROVIDER`` values:
      * ``copilot`` *(default)* — logged-in subscription; ``provider`` is ``None``.
      * ``azure_openai_key`` — BYOK against Azure OpenAI (api key only; no Entra).
      * ``openai`` — BYOK against public OpenAI / OpenAI-compatible endpoints.
    """
    kind = os.getenv("LLM_PROVIDER", "copilot")
    if kind == "copilot":
        return os.getenv("COPILOT_MODEL", "gpt-5.2"), None
    if kind == "azure_openai_key":
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or os.getenv("MODEL_DEPLOYMENT_NAME")
        if not deployment:
            raise ValueError(
                "LLM_PROVIDER=azure_openai_key requires AZURE_OPENAI_DEPLOYMENT_NAME (or MODEL_DEPLOYMENT_NAME)."
            )
        return (
            deployment,
            {
                "type": "azure",
                "base_url": os.environ["AZURE_OPENAI_ENDPOINT"],
                "api_key": os.environ["AZURE_OPENAI_API_KEY"],
                "azure": {"api_version": os.getenv("API_VERSION", "2024-10-21")},
            },
        )
    if kind == "openai":
        return (
            os.getenv("OPENAI_MODEL", "gpt-4o"),
            {
                "type": "openai",
                "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                "api_key": os.environ["OPENAI_API_KEY"],
            },
        )
    raise ValueError(
        f"Unknown LLM_PROVIDER={kind!r}. "
        "Supported: copilot, azure_openai_key, openai."
    )


# ---------------------------------------------------------------------------
# MCP server config (a plain dict — the CLI manages the transport itself).
# ---------------------------------------------------------------------------
async def energy_mcp_config() -> dict[str, Any] | None:
    """Return the ``mcp_servers["energysystems"]`` entry, or ``None`` if down."""
    health_url = ENERGY_MCP_URL.rsplit("/mcp", 1)[0] + "/health"
    try:
        async with httpx.AsyncClient(timeout=2.0) as probe:
            (await probe.get(health_url)).raise_for_status()
    except Exception as exc:
        LOGGER.warning(
            "Energysystems MCP server unreachable at %s (%s). Start it with: "
            "cd examples/domain_examples/energysystems && docker compose up -d",
            health_url,
            exc,
        )
        return None
    # noop auth on the bundled server still requires *some* bearer header.
    return {
        "type": "http",
        "url": ENERGY_MCP_URL,
        "headers": {"Authorization": "Bearer dev-token"},
        "tools": ["*"],
    }


# ---------------------------------------------------------------------------
# System message: base instructions + the energysystems SKILL.md.
# ---------------------------------------------------------------------------
def _load_energy_skill() -> str:
    if not ENERGY_SKILL_PATH.is_file():
        LOGGER.warning("Energy systems SKILL.md not found at %s", ENERGY_SKILL_PATH)
        return ""
    text = ENERGY_SKILL_PATH.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :].lstrip()
    return text


def build_system_message() -> dict[str, str]:
    instructions = (
        "You are a power systems engineering assistant.\n"
        "\n"
        "MCP tools available to you:\n"
        "  * execute_energysystems_code — run Python in a kernel with PyPSA\n"
        "      pre-imported (`pypsa`, `np`, `pd`, `nx`, `plt`).\n"
        "  * search_energysystems_tools — server-side BM25 search over the\n"
        "      energy systems domain's typed helper catalog. Call with a\n"
        "      `query` string and optional `top` (default 5). Pass\n"
        '      `query=""` with `top=999` to enumerate every helper.\n'
        "\n"
        "General workflow:\n"
        "  1. For power-system work, call execute_energysystems_code. Inside\n"
        "     the code, call the typed helpers above and `print(result)` so\n"
        "     the values come back in the tool output.\n"
        "  2. Follow the state graph in the injected skill: define → add\n"
        "     components → (optional time series) → solve.\n"
        "  3. Report results in natural language with the key numbers inline.\n"
    )
    skill_text = _load_energy_skill()
    if skill_text:
        instructions += "\n---\n# Energy systems skill (injected from SKILL.md)\n\n"
        instructions += skill_text
    # Append, not replace — keeps the SDK's safety guardrails in place.
    return {"mode": "append", "content": instructions}


PROMPT = (
    "Solve an economic dispatch problem on a 3-bus PyPSA network and\n"
    "explain how transmission congestion forces the expensive generator on.\n"
    "\n"
    "Network:\n"
    "  - Buses:   b1, b2, b3 (all v_nom=110 kV)\n"
    "  - Gens:    g_cheap on b1  (p_nom=150 MW, marginal_cost=20 $/MWh)\n"
    "             g_exp   on b2  (p_nom=150 MW, marginal_cost=80 $/MWh)\n"
    "  - Load:    d1 on b3 (p_set=200 MW)\n"
    "  - Lines:   l_13 b1<->b3 (r=0.01, x=0.1, s_nom=100 MW)  ← intentionally tight\n"
    "             l_23 b2<->b3 (r=0.01, x=0.1, s_nom=200 MW)\n"
    "             l_12 b1<->b2 (r=0.01, x=0.1, s_nom=200 MW)\n"
    "\n"
    "Inside execute_energysystems_code:\n"
    "  1. Build the network with define_network + add_components.\n"
    "  2. Solve with run_optimal_power_flow(net, solver_name='highs').\n"
    "  3. Call analyze_costs(net) to get total system cost and per-generator dispatch.\n"
    "  4. print() each result so the values flow back.\n"
    "\n"
    "Report:\n"
    "  - Dispatch of g_cheap and g_exp (MW).\n"
    "  - Loading of each line (MW and %% of s_nom); flag any line at its limit.\n"
    "  - Total system cost ($/h).\n"
    "  - One sentence on *why* g_exp has to run — name which line binds and\n"
    "    how that caps the cheap generator's deliverable output."
)


async def main() -> int:
    from copilot import CopilotClient
    from copilot.session import AssistantMessageData, PermissionHandler

    model, provider = resolve_llm()
    provider_kind = os.getenv("LLM_PROVIDER", "copilot")
    provider_label = "copilot-subscription" if provider_kind == "copilot" else provider_kind
    LOGGER.info(
        "LLM configured (provider=%s)",
        provider_label,
    )

    energy = await energy_mcp_config()
    if energy is None:
        LOGGER.error(
            "Energysystems MCP server unreachable; exiting with status 1. "
            "Start it with: cd examples/domain_examples/energysystems && docker compose up -d"
        )
        return 1

    session_kwargs: dict[str, Any] = {
        "model": model,
        "mcp_servers": {"energysystems": energy},
        "system_message": build_system_message(),
    }
    if provider is not None:
        session_kwargs["provider"] = provider

    print("\n" + "=" * 70)
    print(f"USER: {PROMPT}")
    print("=" * 70 + "\n")

    async with CopilotClient() as client:
        async with await client.create_session(
            # Tutorial-only: auto-approve every tool call. Safe here because the
            # only exposed surface is the sandboxed energysystems MCP kernel.
            # In production, replace with a custom handler that allowlists tools.
            on_permission_request=PermissionHandler.approve_all,
            **session_kwargs,
        ) as session:
            reply = await session.send_and_wait(PROMPT, timeout=300.0)

    print("AGENT:")
    if reply and isinstance(reply.data, AssistantMessageData):
        print(reply.data.content)
    else:
        print("(no assistant.message returned before idle)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
