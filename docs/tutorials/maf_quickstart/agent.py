"""
MAF + agora-workbench quickstart tutorial.

Wires a Microsoft Agent Framework (MAF) agent to the agora-workbench tools:

  * ``search_data`` / ``query_catalog`` / ``get_artifact`` / ``list_domains`` —
    data catalog tools exposed by MCP servers that explicitly wire catalog
    indexing plus ``register_catalog_tools(...)`` at startup (see
    ``src/code_execution/catalog.example.yaml`` and
    ``src/code_execution/catalog_tools.py``).
  * ``chemistry`` MCP toolset — the chemistry MCP server from
    ``examples/domain_examples/chemistry/``. The server exposes a generic
    ``execute_chemistry_code`` tool with RDKit pre-imported, plus a
    server-side ``search_chemistry_tools`` BM25 search tool over the
    domain's typed helpers (``parse_molecule``, ``compute_descriptors``,
    ``filter_drug_candidates``, ``compute_fingerprints``,
    ``find_similar_molecules``, ``cluster_molecules``,
    ``enumerate_functional_groups``). Pass ``query=""`` with ``top=999``
    to enumerate the full catalog.

    The typed helpers are *not* separate MCP tools — they are auto-injected
    as Python proxy functions in the kernel namespace, so the agent calls
    them inside ``execute_chemistry_code`` (e.g. ``compute_descriptors("CCO")``)
    without any explicit import. A ``list_tools()`` function is also
    available in the kernel.
  * ``energysystems`` MCP toolset — the energy systems MCP server from
    ``examples/domain_examples/energysystems/``. Exposes an
    ``execute_energysystems_code`` tool with PyPSA pre-imported, a
    server-side ``search_energysystems_tools`` BM25 search tool, plus typed
    helpers (``define_network``, ``add_components``, ``add_time_series``,
    ``run_power_flow``, ``run_optimal_power_flow``, ``run_capacity_expansion``,
    ``analyze_costs``, ``analyze_topology``).

Tool search and indexing now live entirely server-side: each MCP server
builds a BM25 index over its own ``ToolRegistry`` at startup and exposes
it as an MCP tool. No client-side search infrastructure or middleware is
required.

Both domain servers ship with SKILL.md workflows that are consumed through MCP
skill tools: discover them with ``search_*_tools(category="skills")`` and
load them on demand with ``load_*_skill``.

Run from the repo root:

    uv run python docs/tutorials/maf_quickstart/agent.py

Prerequisites:
  1. ``.env`` populated (see docs/tutorials/maf_quickstart/.env.example)
  2. ``az login`` (LLM auth uses Entra ID by default)
  3. Chemistry MCP server running locally:
       cd src && docker build -f deployment/mcp_server/base.Dockerfile \\
                              -t mcp-server-base:local .
       cd examples/domain_examples/chemistry && docker compose up -d --build
  4. Energy systems MCP server running locally:
       cd examples/domain_examples/energysystems && docker compose up -d --build

If any MCP server is unreachable the script still runs with the remaining
tools and prints a friendly skip message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Make the repo's top-level packages importable when running this script directly.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

# Load the repo-root .env (where AZURE_OPENAI_ENDPOINT, DATA_LAKE_*, etc. live).
load_dotenv(REPO_ROOT / ".env")

LOGGER = logging.getLogger("maf_quickstart")

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
# Step B — connect domain MCP tools
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _McpServerConfig:
    name: str
    url: str
    description: str
    tool_name_prefix: str
    start_hint: str


async def _connect_mcp_tool(config: _McpServerConfig):
    """Connect one MCP server using the shared explicit-config pattern from #124."""
    from agent_framework import MCPStreamableHTTPTool

    health_url = config.url.rsplit("/mcp", 1)[0] + "/health"
    try:
        async with httpx.AsyncClient(timeout=2.0) as probe:
            resp = await probe.get(health_url)
            resp.raise_for_status()
    except Exception as exc:
        LOGGER.warning(
            "MCP server %r unreachable at %s (%s) — skipping. Start it with: %s",
            config.name,
            health_url,
            exc,
            config.start_hint,
        )
        return None

    http_client = httpx.AsyncClient(headers={"Authorization": "Bearer dev-token"})
    return MCPStreamableHTTPTool(
        name=config.name,
        url=config.url,
        description=config.description,
        approval_mode="never_require",
        http_client=http_client,
        tool_name_prefix=config.tool_name_prefix,
    )


async def step_b_domain_tools():
    """Build MCP tools from explicit server configs."""
    configs = [
        _McpServerConfig(
            name="chemistry",
            url=os.getenv("CHEMISTRY_MCP_URL", "http://localhost:8020/mcp"),
            description=(
                "Execute Python code with RDKit and cheminformatics packages. "
                "Use for molecular analysis, SMILES parsing, descriptor calculation, "
                "fingerprints, substructure search."
            ),
            tool_name_prefix="chem_",
            start_hint="cd examples/domain_examples/chemistry && docker compose up -d",
        ),
        _McpServerConfig(
            name="energysystems",
            url=os.getenv("ENERGYSYSTEMS_MCP_URL", "http://localhost:8022/mcp"),
            description=(
                "Execute Python code with PyPSA and power system analysis packages. "
                "Use for network modeling, power flow, optimal dispatch, capacity "
                "expansion, and topology analysis."
            ),
            tool_name_prefix="energy_",
            start_hint="cd examples/domain_examples/energysystems && docker compose up -d",
        ),
    ]

    tools = []
    for config in configs:
        tool = await _connect_mcp_tool(config)
        if tool is None:
            continue
        LOGGER.info("Step B: built %s MCP tool @ %s", config.name, config.url)
        tools.append(tool)
    return tools


# ---------------------------------------------------------------------------
# Step D — assemble the agent
# ---------------------------------------------------------------------------
def step_d_build_agent(chat_client, tools):
    """Compose the chat client + tools into a MAF agent."""
    instructions = (
        "You are a scientific research assistant with expertise in chemistry\n"
        "and power systems engineering.\n"
        "\n"
        "MCP tools available to you:\n"
        "  * search_data — hybrid keyword + vector search over the MCP\n"
        "      server's data catalog. Call with a `query` string and\n"
        "      optional `domain` / `source_type` filters. Only available\n"
        "      when the server is configured with a catalog.yaml.\n"
        "  * chem_execute_chemistry_code — run Python in a kernel with RDKit\n"
        "      pre-imported (`Chem`, `Descriptors`, `AllChem`,\n"
        "      `rdMolDescriptors`, `np`, `pd`).\n"
        "  * chem_search_chemistry_tools — server-side BM25 search over the\n"
        "      chemistry domain's typed helper catalog. Call with a\n"
        "      `query` string and optional `top` (default 5). Pass\n"
        "      `query=\"\"` with `top=999` to list every helper.\n"
        "  * chem_load_chemistry_skill — load a chemistry skill by name\n"
        "      (discover names via `chem_search_chemistry_tools` with\n"
        "      `category='skills'`).\n"
        "  * energy_execute_energysystems_code — run Python in a kernel\n"
        "      with PyPSA pre-imported (`pypsa`, `np`, `pd`, `nx`, `plt`).\n"
        "  * energy_search_energysystems_tools — server-side BM25 search\n"
        "      over the energy systems domain's typed helper catalog. Same\n"
        "      `query` / `top` signature as `chem_search_chemistry_tools`.\n"
        "  * energy_load_energysystems_skill — load an energy systems skill\n"
        "      by name (discover names via `energy_search_energysystems_tools`\n"
        "      with `category='skills'`).\n"
        "\n"
        "Inside chem_execute_chemistry_code the following typed helpers are\n"
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
        "Inside energy_execute_energysystems_code the following typed helpers are\n"
        "available as plain Python functions — no imports needed:\n"
        "  define_network(name, snapshots=24, start='2025-01-01', freq='h')\n"
        "  add_components(network_name, buses=None, generators=None,\n"
        "      loads=None, lines=None, storage_units=None)\n"
        "  add_time_series(network_name, profiles)\n"
        "  run_power_flow(network_name, method='ac')\n"
        "  run_optimal_power_flow(network_name)\n"
        "  run_capacity_expansion(network_name)\n"
        "  analyze_costs(network_name)\n"
        "  analyze_topology(network_name)\n"
        "Each returns a dict; prefer them over hand-rolled PyPSA code.\n"
        "\n"
        "General workflow:\n"
        "  1. Discover and load the matching domain skill before starting:\n"
        "     call `*_search_*_tools` with `category='skills'`, then\n"
        "     `*_load_*_skill`.\n"
        "  2. If the user asks about datasets, call search_data.\n"
        "  3. For chemistry work, call chem_execute_chemistry_code. Inside the\n"
        "     code, call the typed helpers above (e.g.\n"
        "     `result = filter_drug_candidates([...], rules='lipinski')`)\n"
        "     and `print(result)` so the values come back in the tool output.\n"
        "  4. For power systems work, call energy_execute_energysystems_code. Inside\n"
        "     the code, call the typed helpers (e.g.\n"
        "     `result = define_network('my_grid')`) and `print(result)`.\n"
        "  5. Follow the state graphs in the loaded skills: parse_molecule\n"
        "     first for chemistry, define_network first for energy systems.\n"
        "  6. Report results in natural language with the key numbers inline.\n"
    )
    agent = chat_client.as_agent(
        name="quickstart_agent",
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
        "Do TWO tasks, STRICTLY ONE AT A TIME. Do NOT issue parallel or\n"
        "simultaneous tool calls — finish all chemistry work and observe its\n"
        "output before issuing ANY energy systems tool call.\n"
        "\n"
        "TASK 1 — Chemistry: Screen this small library of molecules for "
        "drug-likeness. Inside chem_execute_chemistry_code call "
        "`filter_drug_candidates(smiles_list, rules='lipinski')` and "
        "`print(result)`. Then call `compute_descriptors(smi)` for each to "
        "get MW + LogP.\n"
        "  - aspirin:      CC(=O)OC1=CC=CC=C1C(=O)O\n"
        "  - caffeine:     CN1C=NC2=C1C(=O)N(C(=O)N2C)C\n"
        "  - ibuprofen:    CC(C)CC1=CC=C(C=C1)C(C)C(=O)O\n"
        "  - atorvastatin: CC(C)c1c(C(=O)Nc2ccccc2)c(-c2ccccc2)c(-c2ccc(F)cc2)"
        "n1CC[C@@H](O)C[C@@H](O)CC(=O)O\n"
        "\n"
        "Only AFTER TASK 1 is complete and its tool output has been received,\n"
        "begin TASK 2.\n"
        "\n"
        "TASK 2 — Energy Systems: Build a simple 2-bus power grid and run "
        "optimal power flow. Inside energy_execute_energysystems_code:\n"
        "  1. `net = define_network(name='demo_grid', snapshots=24)`\n"
        "  2. Add 2 buses (110 kV), a 200 MW coal generator (marginal_cost=30) "
        "on Bus0, a 150 MW wind generator (marginal_cost=0, p_nom=150) on Bus1, "
        "a 200 MW load on Bus1, and a 200 MVA line connecting them.\n"
        "  3. `opf = run_optimal_power_flow(network_name='demo_grid')`\n"
        "  4. `costs = analyze_costs(network_name='demo_grid')`\n"
        "  5. `print(opf)` and `print(costs)`\n"
        "\n"
        "Once BOTH tasks are complete, report results for both in natural\n"
        "language with the key numbers inline."
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
    tools = await step_b_domain_tools()
    if not tools:
        LOGGER.error("No tools available. Start at least one MCP server, then re-run.")
        return 1

    agent = step_d_build_agent(chat_client, tools)

    chemistry_tool = next((t for t in tools if getattr(t, "name", None) == "chemistry"), None)
    energysystems_tool = next((t for t in tools if getattr(t, "name", None) == "energysystems"), None)

    # Open MCP tool connections (MAF supports `async with` on tools with
    # persistent connections).
    chem_ctx = chemistry_tool if chemistry_tool is not None else _nullcontext()
    energy_ctx = energysystems_tool if energysystems_tool is not None else _nullcontext()
    async with chem_ctx:
        async with energy_ctx:
            await step_e_run(agent)

    return 0


class _nullcontext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
