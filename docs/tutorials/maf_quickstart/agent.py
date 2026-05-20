"""
MAF + agora-workbench quickstart tutorial.

Wires a Microsoft Agent Framework (MAF) agent to the agora-workbench tools:

  * ``search_data`` — discovers datasets in the server-side catalog
    (provided by the MCP server's catalog tools)
  * ``chemistry`` MCP toolset — the chemistry MCP server from
    ``src/domain_examples/chemistry/``. The server exposes a generic
    ``execute_chemistry_code`` tool with RDKit pre-imported, plus a
    ``list_chemistry_domain_tools`` discovery tool that catalogs the
    domain's typed helpers (``parse_molecule``, ``compute_descriptors``,
    ``filter_drug_candidates``, ``compute_fingerprints``,
    ``find_similar_molecules``, ``cluster_molecules``,
    ``enumerate_functional_groups``).

    The typed helpers are *not* separate MCP tools — they are auto-injected
    as Python proxy functions in the kernel namespace, so the agent calls
    them inside ``execute_chemistry_code`` (e.g. ``compute_descriptors("CCO")``)
    without any explicit import. A ``list_tools()`` function is also
    available in the kernel.

The chemistry domain ships with a SKILL.md that documents its state-graph
workflow. The tutorial reads that file at startup and injects it into the
agent's system prompt so the agent uses tools in the recommended order.

Run from the repo root:

    uv run python docs/tutorials/maf_quickstart/agent.py

Prerequisites:
  1. ``.env`` populated (see docs/tutorials/maf_quickstart/.env.example)
  2. ``az login`` (LLM and data lake auth use Entra ID by default)
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

# Path to the chemistry domain's SKILL.md — a portable workflow guide
# (state graph, tool ordering, common pitfalls) that we inject into the
# agent's instructions so it knows how to chain the typed tools.
CHEMISTRY_SKILL_PATH = REPO_ROOT / "src" / "domain_examples" / "chemistry" / "skills" / "SKILL.md"


# ---------------------------------------------------------------------------
# Step A — build the chat client (BYO LLM)
# ---------------------------------------------------------------------------
def step_a_chat_client():
    """Return a MAF ChatClient based on $LLM_PROVIDER (see chat_client.py)."""
    from chat_client import build_chat_client  # local import: tutorial-only module

    client = build_chat_client()
    LOGGER.info("Step A: built chat client %s", type(client).__name__)
    return client


# ---------------------------------------------------------------------------
# Step B — data catalog search tool (now provided by MCP server)
# ---------------------------------------------------------------------------
async def step_b_data_lake_tool():
    """The data catalog search is now a server-side MCP tool.

    The MCP server exposes ``search_data``, ``get_artifact``, and
    ``list_domains`` tools automatically when a ``catalog.yaml`` is
    configured. No separate agent-side tool setup is needed — the tools
    are auto-discovered when connecting to the MCP server.

    Returns ``None`` — catalog tools come from the MCP server connection.
    """
    LOGGER.info(
        "Step B: data catalog search is now server-side. "
        "Tools (search_data, get_artifact, list_domains) are auto-discovered "
        "from the MCP server when catalog.yaml is configured."
    )
    return None


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
def _load_chemistry_skill() -> str:
    """Read the chemistry domain's SKILL.md, stripping the YAML frontmatter.

    Skills are portable workflow guides that travel with the domain. Injecting
    one into the system prompt is the simplest way to teach the agent how to
    chain the typed tools (state graph, common pitfalls, default parameters).
    """
    if not CHEMISTRY_SKILL_PATH.is_file():
        LOGGER.warning("Chemistry SKILL.md not found at %s", CHEMISTRY_SKILL_PATH)
        return ""
    text = CHEMISTRY_SKILL_PATH.read_text(encoding="utf-8")
    # Strip the leading YAML frontmatter (--- ... ---) — it's metadata for the
    # skill loader, not useful in a system prompt.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :].lstrip()
    return text


def step_d_build_agent(chat_client, tools):
    """Compose the chat client + tools into a MAF agent."""
    skill_text = _load_chemistry_skill()

    instructions = (
        "You are a chemistry research assistant.\n"
        "\n"
        "MCP tools available to you:\n"
        "  * search_data — discover datasets in the catalog.\n"
        "      Call with a `query` string and optional `domain` filter.\n"
        "  * execute_chemistry_code — run Python in a kernel with RDKit\n"
        "      pre-imported (`Chem`, `Descriptors`, `AllChem`,\n"
        "      `rdMolDescriptors`, `np`, `pd`).\n"
        "  * list_chemistry_domain_tools — catalog of typed helper functions\n"
        "      auto-injected into the kernel namespace.\n"
        "\n"
        "Inside execute_chemistry_code the following typed helpers are\n"
        "available as plain Python functions — no imports needed:\n"
        "  parse_molecule(smiles)\n"
        "  enumerate_functional_groups(smiles)\n"
        "  compute_descriptors(smiles, descriptors=None)\n"
        "  filter_drug_candidates(smiles_list, rules='lipinski')\n"
        "  compute_fingerprints(smiles_list, fingerprint_type='morgan', ...)\n"
        "  find_similar_molecules(query_smiles, candidate_smiles_list, ...)\n"
        "  cluster_molecules(smiles_list, cutoff=0.4, ...)\n"
        "Each returns a dict; prefer them over hand-rolled RDKit code.\n"
        "\n"
        "General workflow:\n"
        "  1. If the user asks about datasets, call search_data.\n"
        "  2. For chemistry work, call execute_chemistry_code. Inside the\n"
        "     code, call the typed helpers above (e.g.\n"
        "     `result = filter_drug_candidates([...], rules='lipinski')`)\n"
        "     and `print(result)` so the values come back in the tool output.\n"
        "  3. Follow the state graph in the injected skill: parse_molecule\n"
        "     first, then downstream helpers.\n"
        "  4. Report results in natural language with the key numbers inline.\n"
    )
    if skill_text:
        instructions += "\n---\n# Chemistry skill (injected from SKILL.md)\n\n"
        instructions += skill_text

    agent = chat_client.as_agent(
        name="chem_quickstart_agent",
        instructions=instructions,
        tools=tools,
    )
    LOGGER.info(
        "Step D: built agent with %d tool(s); skill injected: %s",
        len(tools),
        bool(skill_text),
    )
    return agent


# ---------------------------------------------------------------------------
# Step E — run a single turn
# ---------------------------------------------------------------------------
async def step_e_run(agent):
    prompt = (
        "Look for chemistry datasets in the data lake (one short query). "
        "Then, regardless of what's in the catalog, screen this small library "
        "of well-known molecules for drug-likeness. Inside execute_chemistry_code "
        "call `filter_drug_candidates(smiles_list, rules='lipinski')` and "
        "`print(result)` so the values come back. Then for each molecule also "
        "call `compute_descriptors(smi)` to get MW + LogP.\n"
        "  - aspirin:      CC(=O)OC1=CC=CC=C1C(=O)O\n"
        "  - caffeine:     CN1C=NC2=C1C(=O)N(C(=O)N2C)C\n"
        "  - ibuprofen:    CC(C)CC1=CC=C(C=C1)C(C)C(=O)O\n"
        "  - atorvastatin: CC(C)c1c(C(=O)Nc2ccccc2)c(-c2ccccc2)c(-c2ccc(F)cc2)"
        "n1CC[C@@H](O)C[C@@H](O)CC(=O)O\n"
        "Report which molecules pass and the molecular weight + LogP for each."
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
        LOGGER.error("No tools available. Configure the data lake and/or start the chemistry MCP server, then re-run.")
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
