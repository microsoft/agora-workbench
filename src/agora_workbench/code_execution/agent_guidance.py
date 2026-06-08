"""Centralized agent-facing guidance for workbench contracts.

When an agent uses the workbench it must follow several platform-specific
contracts that don't exist in a plain Python environment: load data via
``<type>id</type>`` asset tags, discover data/tools via ``search_data`` /
tool search, save user output under ``AGORA_OUTPUT_DIR``, surface files with
``publish_artifact`` + ``<gui>``, and push objects between peer servers. When
the agent breaks one of these rules, the error it gets back is its only
teacher.

This module is the single home for that wording. It distinguishes two kinds of
guidance:

* :func:`redirect` (**A-class**) — the agent used a non-workbench approach and
  there is a right workbench way it can take *right now*. Point it there.
* :func:`operator_gate` (**B-class**) — the rule is a deployment policy the
  agent *cannot* change (an env-var-gated host allow-list, a size limit). Tell
  it to stop retrying and surface the need to the user/operator instead of
  steering it toward a knob it can't reach.

The intent primitives below are the reusable building blocks — the same advice
("reference data via ``<local>``", "save under ``AGORA_OUTPUT_DIR``") appears in
several surfaces, so it lives here once.
"""

from typing import Optional

# --- Intent primitives (single source of truth, reused across surfaces) ---

SAVE_OUTPUT = (
    "To SAVE a file for the user: write under AGORA_OUTPUT_DIR (a variable and env "
    "var already set in the kernel), e.g. os.path.join(AGORA_OUTPUT_DIR, 'name.ext')."
)

LOAD_ASSET = (
    "To LOAD a dataset/asset: reference it inline as <local>/path/to/file</local> "
    "(or <blob>id</blob> when a data lake is configured); the platform resolves the "
    "tag to a local file before your code runs."
)

DISCOVER_DATA = (
    "To FIND data: the sandbox has no filesystem browsing — datasets are provided to "
    "you, so reference a known one inline as <local>/path</local>. If this server "
    "exposes a data catalog, search it (e.g. search_data) to discover what is available."
)

DISCOVER_TOOLS = (
    "To FIND tools: search this server's tool catalog by name or description to "
    "discover the available tools and skills, then call them by name."
)

# Only ``local`` and ``blob`` are resolvable by the default DataLakeDataManager
# (manager.py routes solely on those two; any other type raises "Unsupported
# artifact type"). Do not advertise types the resolver does not handle.
ASSET_TAG_FORMAT = (
    "Asset references use the form <type>id</type>; the resolvable types are "
    "<local>path</local> (a local file path) and <blob>id</blob> (a data-lake "
    "artifact, which needs DATA_LAKE_SEARCH_ENDPOINT configured)."
)

# Parameterized: the allowed scratch prefixes are server-configurable.
SCRATCH_ONLY = "Internal scratch only (not visible to the user): {prefixes}."


# --- Builders ---


def redirect(problem: str, *, intents: list[str]) -> str:
    """Build an A-class steering message.

    Names what went wrong, then lists the legitimate workbench paths so the
    agent can pick the one matching its intent (save / load / discover).

    Args:
        problem: One sentence stating what was rejected (kept first so any
            diagnostic substring a caller relies on stays at the front).
        intents: Intent primitives (e.g. :data:`SAVE_OUTPUT`) to offer.
    """
    lines = [f"{problem} Depending on what you are doing:"]
    lines.extend(f"- {intent}" for intent in intents)
    return "\n".join(lines)


def operator_gate(problem: str, *, tell_user: str, env_var: Optional[str] = None) -> str:
    """Build a B-class message for a deployment-policy gate.

    The agent cannot change these (they are env-var-gated or fixed by the
    deployment), so steering it toward the knob would only make it loop. Tell
    it to stop and surface the need instead.

    Args:
        problem: One sentence stating what was blocked (kept first).
        tell_user: What the agent should ask the user/operator to do.
        env_var: The environment variable the operator must set, if any.
    """
    policy = "This is a deployment policy controlled by the operator"
    if env_var:
        policy += f" via the {env_var} environment variable"
    policy += " — it cannot be changed from agent code."
    return f"{problem} {policy} Do not retry; {tell_user}"


def no_results_hint(kind: str, query: str) -> str:
    """Next-step hint for an empty discovery result (``kind`` is ``data`` or ``tools``)."""
    if kind == "data":
        return (
            f"No data matched {query!r}. Broaden or rephrase the query, drop the domain/"
            "source_type filter, or call list_domains to see what is available."
        )
    return (
        f"No tools or skills matched {query!r}. Broaden or rephrase the query, or pass an "
        "empty string with top=999 to list the full catalog."
    )
