"""
MAF + agora-workbench quickstart tutorial.

Wires a Microsoft Agent Framework (MAF) agent to the agora-workbench tools:

  * ``search_data`` / ``get_artifact`` / ``list_domains`` — data catalog
    tools provided by the MCP server itself when a ``catalog.yaml`` is
    configured (see ``src/code_execution/catalog.example.yaml``). The
    server indexes the declared sources on startup and exposes hybrid
    keyword + vector search as MCP tools — no client-side data lake
    adapter is required.
  * ``chemistry`` MCP toolset — the chemistry MCP server from
    ``src/domain_examples/chemistry/``. The server exposes a generic
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
    ``src/domain_examples/energysystems/``. Exposes an
    ``execute_energysystems_code`` tool with PyPSA pre-imported, a
    server-side ``search_energysystems_tools`` BM25 search tool, plus typed
    helpers (``define_network``, ``add_components``, ``add_time_series``,
    ``run_power_flow``, ``run_optimal_power_flow``, ``run_capacity_expansion``,
    ``analyze_costs``, ``analyze_topology``).

Tool search and indexing now live entirely server-side: each MCP server
builds a BM25 index over its own ``ToolRegistry`` at startup and exposes
it as an MCP tool. No client-side search infrastructure or middleware is
required.

Both domain servers ship with a SKILL.md that documents their state-graph
workflows. The tutorial reads those files at startup and injects them into
the agent's system prompt so the agent uses tools in the recommended order.

Run from the repo root:

    uv run python docs/tutorials/maf_quickstart/agent.py

Prerequisites:
  1. ``.env`` populated (see docs/tutorials/maf_quickstart/.env.example)
  2. ``az login`` (LLM auth uses Entra ID by default)
  3. Chemistry MCP server running locally:
       cd src && docker build -f deployment/mcp_server/base.Dockerfile \\
                              -t mcp-server-base:local .
       cd src/domain_examples/chemistry && docker compose up -d --build
  4. Energy systems MCP server running locally:
       cd src/domain_examples/energysystems && docker compose up -d --build

If any MCP server is unreachable the script still runs with the remaining
tools and prints a friendly skip message.
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
CHEMISTRY_SKILL_PATH = (
    REPO_ROOT / "src" / "domain_examples" / "chemistry" / "skills" / "SKILL.md"
)
ENERGYSYSTEMS_SKILL_PATH = (
    REPO_ROOT / "src" / "domain_examples" / "energysystems" / "skills" / "SKILL.md"
)


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
# Step B — data lake search tool
# ---------------------------------------------------------------------------
async def step_b_data_lake_tool():
    """The data catalog is now served by the MCP server itself.

    When an MCP server is launched with a ``catalog.yaml`` (see
    ``src/code_execution/catalog.example.yaml``), it indexes the declared
    sources on startup and auto-registers ``search_data``, ``get_artifact``,
    and ``list_domains`` as MCP tools. No client-side data lake adapter is
    needed — those tools are discovered when the agent connects to the
    server via ``MCPStreamableHTTPTool``.

    Returns ``None`` — kept as a numbered step purely for tutorial clarity.
    """
    LOGGER.info(
        "Step B: data catalog tools (search_data, get_artifact, list_domains) "
        "are auto-discovered from any MCP server configured with a catalog.yaml."
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

    # The local chemistry server uses noop auth, but its HTTP auth layer
    # still expects an Authorization header. Any non-empty bearer string is
    # accepted.
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
        tool_name_prefix="chem_",
    )
    LOGGER.info("Step C: built chemistry MCP tool @ %s", url)
    return tool


# ---------------------------------------------------------------------------
# Step C2 — energy systems MCP tool
# ---------------------------------------------------------------------------
async def step_c2_energysystems_tool():
    """Build an MCPStreamableHTTPTool pointing at the local energy systems server.

    Returns ``None`` if the server isn't reachable so the tutorial degrades
    gracefully when Docker isn't running yet.
    """
    from agent_framework import MCPStreamableHTTPTool

    url = os.getenv("ENERGYSYSTEMS_MCP_URL", "http://localhost:8022/mcp")
    health_url = url.rsplit("/mcp", 1)[0] + "/health"

    try:
        async with httpx.AsyncClient(timeout=2.0) as probe:
            resp = await probe.get(health_url)
            resp.raise_for_status()
    except Exception as exc:
        LOGGER.warning(
            "Step C2: energy systems MCP server unreachable at %s (%s) — skipping. "
            "Start it with: cd src/domain_examples/energysystems && docker compose up -d",
            health_url,
            exc,
        )
        return None

    http_client = httpx.AsyncClient(headers={"Authorization": "Bearer dev-token"})
    tool = MCPStreamableHTTPTool(
        name="energysystems",
        url=url,
        description=(
            "Execute Python code with PyPSA and power system analysis packages. "
            "Use for network modeling, power flow, optimal dispatch, capacity "
            "expansion, and topology analysis."
        ),
        approval_mode="never_require",
        http_client=http_client,
        tool_name_prefix="energy_",
    )
    LOGGER.info("Step C2: built energy systems MCP tool @ %s", url)
    return tool


# ---------------------------------------------------------------------------
# Step D — assemble the agent
# ---------------------------------------------------------------------------
def _load_skill(path: Path, label: str) -> str:
    """Read a domain SKILL.md, stripping the YAML frontmatter.

    Skills are portable workflow guides that travel with the domain. Injecting
    one into the system prompt is the simplest way to teach the agent how to
    chain the typed tools (state graph, common pitfalls, default parameters).
    """
    if not path.is_file():
        LOGGER.warning("%s SKILL.md not found at %s", label, path)
        return ""
    text = path.read_text(encoding="utf-8")
    # Strip the leading YAML frontmatter (--- ... ---) — it's metadata for the
    # skill loader, not useful in a system prompt.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :].lstrip()
    return text


def step_d_build_agent(chat_client, tools):
    """Compose the chat client + tools into a MAF agent."""
    chem_skill = _load_skill(CHEMISTRY_SKILL_PATH, "Chemistry")
    energy_skill = _load_skill(ENERGYSYSTEMS_SKILL_PATH, "Energy Systems")

    instructions = (
        "You are a scientific research assistant with expertise in chemistry\n"
        "and power systems engineering.\n"
        "\n"
        "MCP tools available to you:\n"
        "  * search_data — hybrid keyword + vector search over the MCP\n"
        "      server's data catalog. Call with a `query` string and\n"
        "      optional `domain` / `source_type` filters. Only available\n"
        "      when the server is configured with a catalog.yaml.\n"
        "  * execute_chemistry_code — run Python in a kernel with RDKit\n"
        "      pre-imported (`Chem`, `Descriptors`, `AllChem`,\n"
        "      `rdMolDescriptors`, `np`, `pd`).\n"
        "  * search_chemistry_tools — server-side BM25 search over the\n"
        "      chemistry domain's typed helper catalog. Call with a\n"
        "      `query` string and optional `top` (default 5). Pass\n"
        "      `query=\"\"` with `top=999` to list every helper.\n"
        "  * execute_energysystems_code — run Python in a kernel with PyPSA\n"
        "      pre-imported (`pypsa`, `np`, `pd`, `nx`, `plt`).\n"
        "  * search_energysystems_tools — server-side BM25 search over the\n"
        "      energy systems domain's typed helper catalog. Same `query`\n"
        "      / `top` signature as `search_chemistry_tools`.\n"
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
        "Inside execute_energysystems_code the following typed helpers are\n"
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
        "  1. If the user asks about datasets, call search_data.\n"
        "  2. For chemistry work, call execute_chemistry_code. Inside the\n"
        "     code, call the typed helpers above (e.g.\n"
        "     `result = filter_drug_candidates([...], rules='lipinski')`)\n"
        "     and `print(result)` so the values come back in the tool output.\n"
        "  3. For power systems work, call execute_energysystems_code. Inside\n"
        "     the code, call the typed helpers (e.g.\n"
        "     `result = define_network('my_grid')`) and `print(result)`.\n"
        "  4. Follow the state graphs in the injected skills: parse_molecule\n"
        "     first for chemistry, define_network first for energy systems.\n"
        "  5. Report results in natural language with the key numbers inline.\n"
    )
    if chem_skill:
        instructions += "\n---\n# Chemistry skill (injected from SKILL.md)\n\n"
        instructions += chem_skill
    if energy_skill:
        instructions += "\n---\n# Energy Systems skill (injected from SKILL.md)\n\n"
        instructions += energy_skill

    agent = chat_client.as_agent(
        name="quickstart_agent",
        instructions=instructions,
        tools=tools,
    )
    LOGGER.info(
        "Step D: built agent with %d tool(s); skills injected: chemistry=%s, energy=%s",
        len(tools),
        bool(chem_skill),
        bool(energy_skill),
    )
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
        "drug-likeness. Inside execute_chemistry_code call "
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
        "optimal power flow. Inside execute_energysystems_code:\n"
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
    data_lake_tool = await step_b_data_lake_tool()
    chemistry_tool = await step_c_chemistry_tool()
    energysystems_tool = await step_c2_energysystems_tool()

    tools = [t for t in (data_lake_tool, chemistry_tool, energysystems_tool) if t is not None]
    if not tools:
        LOGGER.error(
            "No tools available. Configure the data lake and/or start an "
            "MCP server, then re-run."
        )
        return 1

    agent = step_d_build_agent(chat_client, tools)

    # Open MCP tool connections (MAF supports `async with` on tools with
    # persistent connections; data lake tool is stateless).
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
