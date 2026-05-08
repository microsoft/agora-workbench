"""
MAF + agora-workbench quickstart tutorial.

Wires a Microsoft Agent Framework (MAF) agent to two agora-workbench tools:

  * ``search_data_lake_catalog`` — discovers datasets in the Azure AI Search-
    backed data lake catalog (via ``data_lake.tools.adapters.maf``)
  * ``chemistry`` MCP toolset — runs RDKit code in an isolated sandbox
    (the chemistry MCP server from ``src/domain_examples/chemistry/``)

Run from the repo root:

    uv run python docs/tutorials/maf_quickstart/agent.py

Prerequisites:
  1. ``.env`` populated (see docs/tutorials/maf_quickstart/.env.example)
  2. ``az login`` (TRAPI / data lake auth use Entra ID by default)
  3. Chemistry MCP server running locally:
       cd src && docker build -f deployment/mcp_server/base.Dockerfile \\
                              -t mcp-server-base:local .
       cd src/domain_examples/chemistry && docker compose up -d --build

If the chemistry server is unreachable the script still runs the data-lake-
search portion and prints a friendly skip message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Make the repo's `src/` packages importable when running this script directly.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

# Load the repo-root .env (where AZURE_OPENAI_ENDPOINT, DATA_LAKE_*, etc. live).
load_dotenv(REPO_ROOT / ".env")

LOGGER = logging.getLogger("maf_quickstart")


# ---------------------------------------------------------------------------
# Step A — build the chat client (BYO LLM)
# ---------------------------------------------------------------------------
def step_a_chat_client():
    """Return a MAF ChatClient based on $LLM_PROVIDER (see llm.py)."""
    from llm import build_chat_client  # local import: tutorial-only module

    client = build_chat_client()
    LOGGER.info("Step A: built chat client %s", type(client).__name__)
    return client


# ---------------------------------------------------------------------------
# Step B — data lake search tool
# ---------------------------------------------------------------------------
async def step_b_data_lake_tool():
    """Build the agora-workbench data lake search tool.

    Uses the default Azure AI Search-backed backend, which reads
    ``DATA_LAKE_SEARCH_ENDPOINT`` and ``DATA_LAKE_CATALOG_INDEX_NAME`` from
    the environment and authenticates via the shared Entra credential chain.

    Returns ``None`` if the data lake isn't configured — letting the tutorial
    still demonstrate the agent loop with just the chemistry tool.
    """
    from data_lake.tools.adapters.maf import (
        create_data_lake_search_tool,
        is_data_lake_configured,
    )

    if not is_data_lake_configured():
        LOGGER.warning(
            "Step B: DATA_LAKE_SEARCH_ENDPOINT not set — skipping data lake tool. "
            "Set it in .env to enable catalog search."
        )
        return None

    tool = await create_data_lake_search_tool()
    LOGGER.info("Step B: built data lake search tool")
    return tool


# ---------------------------------------------------------------------------
# Step C — chemistry MCP tool
# ---------------------------------------------------------------------------
async def step_c_chemistry_tool():
    """Build an MCPStreamableHTTPTool pointing at the local chemistry server.

    Returns ``None`` if the server isn't reachable so the tutorial degrades
    gracefully when Docker isn't running yet.
    """
    from agent_framework import MCPStreamableHTTPTool

    url = os.getenv("CHEMISTRY_MCP_URL", "http://localhost:8020/mcp")
    health_url = url.rsplit("/mcp", 1)[0] + "/health"

    # Probe health before constructing the tool — produces a clear skip message
    # rather than a confusing failure deeper in the agent loop.
    try:
        async with httpx.AsyncClient(timeout=2.0) as probe:
            resp = await probe.get(health_url)
            resp.raise_for_status()
    except Exception as exc:
        LOGGER.warning(
            "Step C: chemistry MCP server unreachable at %s (%s) — skipping. "
            "Start it with: cd src/domain_examples/chemistry && docker compose up -d",
            health_url,
            exc,
        )
        return None

    # The local chemistry server uses noop auth, but its middleware still
    # expects an Authorization header. Any non-empty bearer string is accepted.
    http_client = httpx.AsyncClient(headers={"Authorization": "Bearer dev-token"})
    tool = MCPStreamableHTTPTool(
        name="chemistry",
        url=url,
        description=(
            "Execute Python code with RDKit and cheminformatics packages. "
            "Use for molecular analysis, SMILES parsing, descriptor calculation, "
            "fingerprints, substructure search."
        ),
        approval_mode="never_require",
        http_client=http_client,
    )
    LOGGER.info("Step C: built chemistry MCP tool @ %s", url)
    return tool


# ---------------------------------------------------------------------------
# Step D — assemble the agent
# ---------------------------------------------------------------------------
def step_d_build_agent(chat_client, tools):
    """Compose the chat client + tools into a MAF agent."""
    instructions = (
        "You are a chemistry research assistant.\n"
        "\n"
        "Tools available to you:\n"
        "  * search_data_lake_catalog — discover datasets in the data lake.\n"
        "    Call it with a `query` string and optional `domains`/`tags` filters.\n"
        "  * execute_chemistry_code — run Python with RDKit pre-imported.\n"
        "    `Chem`, `Descriptors`, `AllChem`, `rdMolDescriptors`, `Draw`,\n"
        "    `np`, `pd` are already in scope. Print results so the user sees\n"
        "    the output.\n"
        "\n"
        "Workflow for the user's request:\n"
        "  1. Search the catalog for relevant chemistry datasets.\n"
        "  2. Pick one molecule (a SMILES string from the catalog or a well-\n"
        "     known molecule like aspirin: CC(=O)OC1=CC=CC=C1C(=O)O) and use\n"
        "     execute_chemistry_code to compute at least one descriptor\n"
        "     (e.g. molecular weight via Descriptors.MolWt).\n"
        "  3. Report the descriptor value back in natural language.\n"
    )

    agent = chat_client.as_agent(
        name="chem_quickstart_agent",
        instructions=instructions,
        tools=tools,
    )
    LOGGER.info("Step D: built agent with %d tool(s)", len(tools))
    return agent


# ---------------------------------------------------------------------------
# Step E — run a single turn
# ---------------------------------------------------------------------------
async def step_e_run(agent):
    prompt = (
        "Find a chemistry dataset in the data lake. Then compute the molecular "
        "weight of one example molecule using RDKit and report the value."
    )
    print("\n" + "=" * 70)
    print(f"USER: {prompt}")
    print("=" * 70 + "\n")

    response = await agent.run(prompt)
    print("AGENT:")
    print(response.text if hasattr(response, "text") else str(response))
    print()
    return response


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

    agent = step_d_build_agent(chat_client, tools)

    # Open the chemistry MCP tool's connection (MAF supports `async with`
    # on tools with persistent connections; data lake tool is stateless).
    async with chemistry_tool if chemistry_tool is not None else _nullcontext():
        await step_e_run(agent)

    return 0


class _nullcontext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
