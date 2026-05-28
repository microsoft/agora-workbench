"""
OpenAI Agents + agora-workbench quickstart tutorial.

Wires an `openai-agents` SDK ``Agent`` to the agora-workbench chemistry MCP
server and runs a single-shot drug-likeness screen against a small molecule
library.

Numbered steps mirror ``docs/tutorials/maf_quickstart/agent.py`` so the two
quickstarts can be diffed side-by-side:

  * step_a_chat_client     -> build the model primitive (BYO LLM)
  * step_b_data_lake_tool  -> no-op placeholder for the server-side catalog
  * step_c_chemistry_tool  -> build the chemistry MCPServerStreamableHttp
  * step_d_build_agent     -> assemble the agents.Agent with the MCP server
  * step_e_run             -> Runner.run a single prompt and print the result

Run from the repo root:

    uv run python docs/tutorials/openai_agents_quickstart/agent.py

Prerequisites:
  1. ``.env.agent`` populated (see
     docs/tutorials/openai_agents_quickstart/.env.agent.example)
  2. ``az login`` (LLM auth uses Entra ID by default)
  3. Chemistry MCP server running locally:
       docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
       cd examples/domain_examples/chemistry && docker compose up -d --build
  4. ``openai-agents`` installed (no dedicated extra today):
       uv pip install openai-agents     # or: pip install openai-agents

If the chemistry server is unreachable the script prints a friendly skip
message and exits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import nullcontext
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Make the repo root importable so ``agent_helpers`` and ``code_execution``
# resolve when running this tutorial directly. ``chat_client`` is a sibling
# tutorial module loaded the same way.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load the repo-root .env.agent (where AZURE_OPENAI_ENDPOINT, CHEMISTRY_MCP_URL,
# etc. live).
load_dotenv(REPO_ROOT / ".env.agent")

from chat_client import build_model  # noqa: E402  (sibling tutorial module)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("openai_agents_quickstart")

# Portable chemistry workflow guide (state graph, tool ordering, pitfalls).
CHEMISTRY_SKILL_PATH = (
    REPO_ROOT / "examples" / "domain_examples" / "chemistry" / "skills" / "SKILL.md"
)


# ---------------------------------------------------------------------------
# Step A — build the model primitive (BYO LLM)
# ---------------------------------------------------------------------------
def step_a_chat_client():
    """Return whatever ``agents.Agent(model=...)`` accepts for this provider."""
    model = build_model()
    LOGGER.info("Step A: built openai-agents model %s", type(model).__name__)
    return model


# ---------------------------------------------------------------------------
# Step B — data catalog (server-side placeholder)
# ---------------------------------------------------------------------------
async def step_b_data_lake_tool():
    """Data catalog tools are auto-discovered from any MCP server with a catalog.yaml.

    See ``code_execution/catalog_tools.py`` and
    ``code_execution/catalog.example.yaml``. The bundled chemistry server
    doesn't ship a catalog.yaml today, so this step is a no-op log — kept as
    a numbered placeholder so the tutorial structure matches the MAF
    quickstart.
    """
    LOGGER.info(
        "Step B: data catalog tools (search_data, get_artifact, list_domains) "
        "are auto-discovered from any MCP server configured with a catalog.yaml."
    )
    return None


# ---------------------------------------------------------------------------
# Step C — chemistry MCP server
# ---------------------------------------------------------------------------
async def step_c_chemistry_tool():
    """Build an ``MCPServerStreamableHttp`` for the local chemistry server.

    Returns ``None`` if the server isn't reachable so the tutorial degrades
    gracefully when Docker isn't running yet.
    """
    from agents.mcp import MCPServerStreamableHttp

    url = os.getenv("CHEMISTRY_MCP_URL", "http://localhost:8020/mcp")
    health_url = url.rsplit("/mcp", 1)[0] + "/health"

    try:
        async with httpx.AsyncClient(timeout=2.0) as probe:
            resp = await probe.get(health_url)
            resp.raise_for_status()
    except Exception as exc:
        LOGGER.warning(
            "Step C: chemistry MCP server unreachable at %s (%s) — skipping. "
            "Start it with: cd examples/domain_examples/chemistry && docker compose up -d",
            health_url,
            exc,
        )
        return None

    # The bundled chemistry server uses noop auth, but its HTTP auth layer
    # still expects an Authorization header. Any non-empty bearer is accepted.
    server = MCPServerStreamableHttp(
        name="chemistry",
        params={
            "url": url,
            "headers": {"Authorization": "Bearer dev-token"},
        },
        cache_tools_list=True,
    )
    LOGGER.info("Step C: built chemistry MCP server @ %s", url)
    return server


# ---------------------------------------------------------------------------
# Step D — assemble the agent
# ---------------------------------------------------------------------------
def _load_chemistry_skill() -> str:
    """Read SKILL.md and strip the leading YAML frontmatter."""
    if not CHEMISTRY_SKILL_PATH.is_file():
        LOGGER.warning("Chemistry SKILL.md not found at %s", CHEMISTRY_SKILL_PATH)
        return ""
    text = CHEMISTRY_SKILL_PATH.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :].lstrip()
    return text


def step_d_build_agent(model, mcp_servers):
    """Compose model + MCP servers into an ``agents.Agent``."""
    from agents import Agent

    skill_text = _load_chemistry_skill()

    instructions = (
        "You are a chemistry research assistant.\n"
        "\n"
        "MCP tools available to you:\n"
        "  * execute_chemistry_code — run Python in a kernel with RDKit\n"
        "      pre-imported (`Chem`, `Descriptors`, `AllChem`,\n"
        "      `rdMolDescriptors`, `np`, `pd`).\n"
        "  * search_chemistry_tools — server-side BM25 search over the\n"
        "      chemistry domain's typed helper catalog. Call with a `query`\n"
        "      string and optional `top` (default 5). Pass `query=\"\"` with\n"
        "      `top=999` to enumerate every helper.\n"
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
        "  1. For chemistry work, call execute_chemistry_code. Inside the\n"
        "     code, call the typed helpers above (e.g.\n"
        "     `result = filter_drug_candidates([...], rules='lipinski')`)\n"
        "     and `print(result)` so the values come back in the tool output.\n"
        "  2. Follow the state graph in the injected skill: parse_molecule\n"
        "     first, then downstream helpers.\n"
        "  3. Report results in natural language with the key numbers inline.\n"
    )
    if skill_text:
        instructions += "\n---\n# Chemistry skill (injected from SKILL.md)\n\n"
        instructions += skill_text

    agent = Agent(
        name="chem_quickstart_agent",
        instructions=instructions,
        model=model,
        mcp_servers=mcp_servers,
    )
    LOGGER.info(
        "Step D: built Agent with %d MCP server(s); skill injected: %s",
        len(mcp_servers),
        bool(skill_text),
    )
    return agent


# ---------------------------------------------------------------------------
# Step E — single-turn run
# ---------------------------------------------------------------------------
async def step_e_run(agent):
    from agents import Runner

    prompt = (
        "Screen this small library of molecules for drug-likeness. "
        "Inside execute_chemistry_code call "
        "`filter_drug_candidates(smiles_list, rules='lipinski')` and "
        "`print(result)`. Then for each molecule also call "
        "`compute_descriptors(smi)` to get MW + LogP.\n"
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

    result = await Runner.run(agent, prompt)

    print("AGENT:")
    print(result.final_output)
    print()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
async def main() -> int:
    model = step_a_chat_client()
    await step_b_data_lake_tool()
    chemistry_server = await step_c_chemistry_tool()

    mcp_servers = [s for s in (chemistry_server,) if s is not None]
    if not mcp_servers:
        LOGGER.error(
            "No MCP servers available. Start the chemistry MCP server "
            "(cd examples/domain_examples/chemistry && docker compose up -d) and retry."
        )
        return 1

    # MCPServerStreamableHttp requires `async with` to open/close its session.
    ctx = chemistry_server if chemistry_server is not None else nullcontext()
    async with ctx:
        agent = step_d_build_agent(model, mcp_servers)
        await step_e_run(agent)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
