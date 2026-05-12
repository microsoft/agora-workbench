# Abstraction Layers in agora-workbench

A map of the project's abstraction layers and an honest assessment of whether
they provide enough flexibility/coverage to integrate with agent frameworks
beyond Microsoft Agent Framework (MAF) — primarily LangGraph, the OpenAI
Agents SDK, Pydantic AI, and MCP-as-protocol.

## The seven layers

```
┌─────────────────────────────────────────────────────────────────────┐
│  L7  Agent runtime           (MAF / OpenAI Agents / LangGraph / …)  │  ← framework
├─────────────────────────────────────────────────────────────────────┤
│  L6  Framework adapters      adapters/maf.py, …/openai_agents.py     │  ← thin glue
├─────────────────────────────────────────────────────────────────────┤
│  L5  Middleware protocols    src/middleware/protocols/               │  ← runtime hooks
├─────────────────────────────────────────────────────────────────────┤
│  L4  Tool descriptors        src/tools/tool_descriptor.py            │  ← capability surface
├─────────────────────────────────────────────────────────────────────┤
│  L3  Domain components       data_lake, planning, tool_learning, …   │  ← logic + Protocols
├─────────────────────────────────────────────────────────────────────┤
│  L2  Transport adapters      mcp/, code_execution/, search backends  │  ← wire/runtime
├─────────────────────────────────────────────────────────────────────┤
│  L1  Cross-cutting infra     auth/, utilities/, tools/search/_bm25   │  ← primitives
└─────────────────────────────────────────────────────────────────────┘
```

---

## L1 — Cross-cutting infrastructure

**What's there:** [src/auth/providers.py](../src/auth/providers.py) (the
`AzureCliCredential → ManagedIdentityCredential` chain + scope-aware token
providers); `src/utilities/bm25/` (the generic dependency-free BM25 index);
[src/tools/search/_bm25.py](../src/tools/search/_bm25.py).

**Flexibility:** Maximum. Pure Python, no framework dependencies, no I/O
assumptions. Any framework adapter inherits these for free.

**Integration cost for a new framework:** Zero. You never write a
`langgraph_bm25.py`.

---

## L2 — Transport adapters

**What's there:**
- [src/tools/mcp/](../src/tools/mcp/) — generic MCP client wrappers.
- [src/code_execution/](../src/code_execution/) — the long-lived MCP server
  pattern (chemistry, earthscience). Container + auth + Jupyter kernel +
  tool-proxy injection.
- [src/data_lake/search/](../src/data_lake/search/) and
  [src/data_lake/tools/adapters/local.py](../src/data_lake/tools/adapters/local.py)
  — pluggable backends (Azure AI Search vs. local YAML/BM25).

**Flexibility:** High. The MCP server pattern is **framework-independent by
construction** — any MCP-aware host (MAF, OpenAI Agents, Claude Desktop,
Cursor, VS Code) consumes it without an adapter.

**Integration cost:** Zero for MCP-aware frameworks. This is the
highest-leverage layer for the multi-framework story.

---

## L3 — Domain components (the "logic" tier)

**What's there:**

- **`src/data_lake/`** — defines `DataLakeSearchBackend` (ABC) with
  `DefaultDataLakeSearchBackend` (Azure) and `LocalYAMLBackend` (offline).
  Schema + permissions logic is backend-agnostic.
- **`src/middleware/tool_learning/`** — `VignetteSearchRepo` Protocol +
  `VignetteWriteRepo` Protocol. Two implementations each
  (`SearchVignetteRepo` / `LocalFileSearchVignetteRepo` for read;
  `TableVignetteRepo` / `LocalFileVignetteRepo` for write).
- **`src/planning/`** — skill/plan models, store, `tools.py` exposes plan
  operations as functions.
- **`src/middleware/decision_log/`** — `DecisionLog` (in-memory +
  sink-pluggable).

**Flexibility:** Very high. This is **Protocol-first design** — every
component exposes a structural interface, framework code never imports it,
and swapping a backend is a constructor kwarg.

**Integration cost for a new framework:** Zero. None of these care which
agent runtime is calling them.

> This is where the project is strongest. The Protocol surfaces
> (`VignetteSearchRepo`, `VignetteWriteRepo`, `DataLakeSearchBackend`) are
> the kind of contracts that make multi-framework support actually tractable.

---

## L4 — Tool descriptors

**What's there:** [src/tools/tool_descriptor.py](../src/tools/tool_descriptor.py)
— a `@dataclass` with `name`, `description`, `input_model: Type[BaseModel]`,
`func: Awaitable[str]`, and an auto-derived `input_schema: dict` (JSON
Schema).

**Flexibility:** **This is the linchpin of multi-framework support.** Every
modern agent framework accepts ≈ "(name, description, JSON schema, async
callable)." So one `ToolDescriptor` → many framework-specific tool objects
is a 5–10 line adapter:

| Framework | Adapter |
|---|---|
| MAF | `FunctionTool(name=..., description=..., input_model=..., func=...)` |
| OpenAI Agents | `@function_tool` decorator wrap around `descriptor.func` |
| LangChain/LangGraph | `StructuredTool.from_function(name=..., args_schema=input_model, coroutine=func)` |
| Pydantic AI | `Tool(func, name=..., docstring=description)` |
| Semantic Kernel | `KernelFunction.from_method(func, ...)` |
| MCP | `Tool(name=..., inputSchema=input_schema)` + handler |

**Integration cost for a new framework:** ~30 lines per framework. The hard
work (deriving JSON schema, validating arguments) is already done by
Pydantic.

**Gap to watch:** The `func` signature returns `str`. Some frameworks
(LangGraph, OpenAI Agents) prefer typed return objects. Not a blocker — just
means the adapter may need a `json.loads()` step. Worth tracking as a known
limitation.

---

## L5 — Middleware protocols

**What's there:**
[src/middleware/protocols/middleware.py](../src/middleware/protocols/middleware.py)
defines `ChatMiddleware`, `FunctionMiddleware`, `ChatContext`,
`FunctionInvocationContext`, plus `MiddlewareTermination`. These are
runtime-shaped Protocols (pre/post hooks + `call_next` chain).

**Flexibility:** Good for *function* middleware (matches MAF and Semantic
Kernel cleanly). Less clean for frameworks that **don't have function
middleware at all**:

| Framework | Function middleware? | Where the agora middleware maps |
|---|---|---|
| MAF | Yes (`FunctionMiddleware`) | Direct |
| Semantic Kernel | Yes (`IFunctionInvocationFilter`) | Direct |
| OpenAI Agents | No — but has **input/output guardrails** + **tool hooks** | Hard violations → input guardrail; repair loop → custom `Runner` subclass |
| LangGraph | No — but has **callbacks** and **interrupts** | Hard violations → conditional edge; repair loop → retry node |
| Pydantic AI | Limited (`@agent.tool_plain` decorators wrap individual tools) | Per-tool wrapping |

**Integration cost:** Moderate. The Protocol is *expressive enough* to model
what each framework can do, but the impedance match isn't 1:1. The
repair-loop semantics (re-call with patched args after a failure) are
MAF-shaped and translate awkwardly to frameworks without that hook.

**Gap to watch:** A LangGraph adapter would probably implement the repair
loop as a **separate retry node** rather than middleware. That's fine — but
it means the user-visible API on those frameworks is "configure a retry
node," not "add middleware." Worth being upfront about in docs.

---

## L6 — Framework adapters

**What's there today (all MAF-only):**
- [src/data_lake/tools/adapters/maf.py](../src/data_lake/tools/adapters/maf.py)
  — `create_data_lake_search_tool()`.
- [src/planning/adapters/maf.py](../src/planning/adapters/maf.py) —
  plan-tool factories.
- [src/middleware/decision_log/adapters/maf_chat_middleware.py](../src/middleware/decision_log/adapters/maf_chat_middleware.py),
  [maf_context_provider.py](../src/middleware/decision_log/adapters/maf_context_provider.py),
  [maf_protocols.py](../src/middleware/decision_log/adapters/maf_protocols.py).
- [src/middleware/tool_learning/adapters/maf_function.py](../src/middleware/tool_learning/adapters/maf_function.py)
  — `VignetteFunctionMiddleware` MAF wrapper.
- [src/code_execution/code_execution/tool_learning_middleware.py](../src/code_execution/code_execution/tool_learning_middleware.py)
  — code-exec specific.

**Flexibility:** High by *convention*. Each component has an `adapters/`
subdirectory; new frameworks slot in next to the MAF file. No central
registry to update; nothing in L1–L5 changes when you add
`adapters/langgraph.py`.

**Integration cost for a new framework:** This is the only layer that grows
linearly with framework count — and it's intentional. Estimated sizes from
inspecting the MAF adapters:

| Component | MAF adapter size | Expected per-framework size |
|---|---|---|
| Tool surface | ~100 LOC | 30–100 LOC |
| Decision log middleware/context provider | ~250 LOC | 100–300 LOC |
| Vignette function middleware | ~350 LOC | 200–400 LOC |
| Planning tools | ~150 LOC | 50–150 LOC |

So a *full* second-framework adapter ≈ **800–1000 LOC + tests**. A minimal
one (tools only, no middleware) is closer to **150 LOC**.

---

## L7 — The agent runtime (the framework itself)

Not yours; not your problem. Your job is to give it tools, context, and
(where possible) middleware/guardrails.

---

## Coverage assessment

### Solid (no gaps for any reasonable target framework)
- **L1–L3** (infra, transports, domain logic) — Protocol-driven, zero
  coupling to MAF.
- **L4** (tool descriptors) — clean JSON-schema-shaped surface that maps to
  every modern framework.
- **L6** (adapters) — extension *pattern* is already established by MAF;
  convention is good enough that you don't need a meta-registry.

### Solid with caveats
- **L5** (middleware protocols) — function-middleware is universal-ish but
  the **repair-loop semantics** are MAF-shaped. Frameworks without
  first-class function middleware (OpenAI Agents, LangGraph) will need
  adapter authors to project the same intent onto guardrails/callbacks/retry
  nodes. The Protocol doesn't prevent this; it just doesn't make it
  automatic.

### Genuine gaps to call out

1. **No skill adapter pattern yet.** `planning/skills/` is great, but
   there's no framework adapter for "inject a skill state-graph as a
   subgraph/handoff." Today `SKILL.md` is just text appended to a system
   prompt. For LangGraph and OpenAI Agents (both of which have richer
   multi-agent primitives), the skill could be *executed* as a graph instead
   of read as prose. Adding `skills/adapters/<framework>.py` that compiles a
   `Skill` → framework-native subgraph is the most valuable abstraction you
   don't yet have.

2. **No tool-descriptor → MCP server generator.** You ship MCP servers
   (chemistry, earthscience) hand-written. A `ToolDescriptor → FastMCP
   server` generator would make every `ToolDescriptor` automatically usable
   from any MCP host — biggest force-multiplier in the project's reach.

3. **Decision log's adapter only targets MAF middleware.** The underlying
   `DecisionLog` is framework-agnostic, but the wiring
   (`DecisionLogChatMiddleware`) is MAF-shaped. For OpenAI Agents you'd want
   a `TracingProcessor`; for LangGraph, a `BaseCallbackHandler`. Same data
   sink, three adapter shells. Worth standardizing the adapter naming
   convention now (`adapters/<framework>_observer.py`?) before the second
   one lands.

4. **No streaming abstraction.** `ToolDescriptor.func` returns `str`. Some
   frameworks (MAF, OpenAI Agents, LangGraph) natively support streaming
   tool output. If/when a domain tool wants to stream (e.g. a long-running
   code execution), the descriptor needs a sibling type
   (`StreamingToolDescriptor`?) or a discriminated `func: Callable[...,
   Awaitable[str] | AsyncIterator[str]]`.

5. **Configuration sprawl.** Each component reads its own env vars
   (`DATA_LAKE_*`, `TOOL_LEARNING_*`, `AZURE_OPENAI_*`). A new framework
   adapter that wants to bundle agora-workbench into a single agent has to
   know all of these. An `agora.config` package that aggregates
   per-component dataclasses (`AgoraConfig.from_env()` returning a
   composite) would make framework tutorials terser.

---

## Bottom line

The abstraction layers **are sufficient** for the multi-framework
integrations under consideration (LangGraph, OpenAI Agents, Pydantic AI,
MCP-as-protocol). The Protocol-first design at L3 and the `ToolDescriptor`
at L4 are exactly the right shape — they're the parts that would have been
painful to retrofit.

The two abstractions that are *not yet there* and would meaningfully
accelerate the next two frameworks:

- **`Skill → framework-native subgraph` adapters** (instead of prose
  injection).
- **`ToolDescriptor → MCP server` codegen** (one wrapper, every MCP host).

Everything else is "build the adapter when the time comes" — and the LOC
estimates suggest each one is a focused week, not a quarter.

---

## Future abstractions (layers we don't have yet)

The gaps in the previous section are *missing pieces inside existing
layers*. The items below are **whole layers** the project doesn't have yet
but probably will need as it grows beyond a single framework and a single
deployment.

### 1. Model / LLM-client abstraction
**Missing:** A `ChatClient` Protocol or `ModelSpec` dataclass. Today
[docs/tutorials/maf_quickstart/llm.py](tutorials/maf_quickstart/llm.py) is a
BYO factory that returns a concrete `OpenAIChatClient`. Every framework
adapter that wants to construct an agent has to re-implement the
Azure-Entra-token-provider dance.

**Shape:** `ModelSpec(endpoint, deployment, api_version, credential_factory,
temperature, …)` + per-framework factories (`make_maf_client(spec)`,
`make_langgraph_client(spec)`, `make_openai_agents_client(spec)`). ~50 LOC,
saves every downstream tutorial from re-deriving credentials.

**Cost of not having it:** Each new framework tutorial copy-pastes 30 lines
of credential plumbing. The drift is already visible in `llm.py`.

### 2. Session / conversation abstraction
**Missing:** A `SessionStore` Protocol. Each framework handles "thread
state" differently (MAF `AgentThread`, OpenAI Agents `Session`, LangGraph
`checkpointer`, Pydantic AI `message_history`). The decision log and
vignette middleware currently use ad-hoc `tenant_id` / `user_id` /
`thread_id` strings.

**Shape:** `SessionContext(tenant_id, user_id, thread_id, started_at,
metadata)` + `SessionStore` Protocol with `get / put / list / expire`. The
L3 components already *accept* these strings; promoting them to a typed
object makes adapters trivial.

**Why it matters:** Multi-turn behavior + cross-framework portability
*both* need a single place to ask "what's the current conversation, who's
in it, what does it remember?"

### 3. Tool result / structured output abstraction
**Missing:** `ToolDescriptor.func` returns `str`. There's no `ToolResult`
type capturing **(content, structured_data, citations, artifacts, error)**.
Frameworks differ a lot here:

| Framework | Native result shape |
|---|---|
| MAF | string |
| OpenAI Agents | typed return + tool messages |
| LangGraph | `ToolMessage` with `artifact` field |
| MCP | `[TextContent | ImageContent | EmbeddedResource]` |

**Why it matters:** The chemistry/earthscience tools probably want to
return *files* (plots, structures, datasets), not strings. Today they have
to base64-stuff or JSON-stringify.

**Shape:** A discriminated union — `TextResult | JSONResult | FileResult |
ImageResult` — keeps the descriptor framework-agnostic but lets each
adapter render the right thing. This is the same gap as "streaming" in the
previous section: *results are more than strings*.

### 4. Evaluation / scoring abstraction
**Missing:** An `Evaluator` Protocol. Vignettes implicitly carry
"confidence" but there's no contract for *who computes it*, no abstraction
for "run this evaluator over a trace," no plug point for LLM-as-judge, no
harness for regression tests across model versions.

**Why it matters:** Tool-learning is a feedback loop — vignettes are
written, but the quality signal is currently implicit (no failure ⇒
confidence up). A real abstraction would let you:
- swap in a domain-specific judge (chemist rubric vs. earth-scientist rubric);
- replay a decision-log trace against a new model and compute a diff score;
- drive CI gates ("score on the chemistry eval set must not regress").

This is the natural complement to the decision log: log writes traces,
evaluator scores them, vignette store learns from the scored traces. Right
now the middle step is missing.

### 5. Cost / quota / rate-limit abstraction
**Missing:** A `BudgetTracker` or `UsagePolicy` Protocol. The framework
keeps the token counts; nothing aggregates them across multiple agents in
one session, or enforces a per-tenant budget, or trips a circuit breaker
when an MCP server starts rate-limiting.

**Why it matters:** As soon as agora-workbench gets deployed to multiple
internal users (the planetary plan implies this), "user X just burned $40
on a runaway agent" becomes an incident. This abstraction is **far easier
to add now** (one Protocol + a middleware adapter per framework that wraps
the model call) than after the first runaway. It also slots cleanly under
L5.

### 6. Prompt / template abstraction
**Missing:** A `PromptTemplate` with versioning. System prompts are inline
strings scattered across tutorials and `domain_examples`. No diff tooling,
no eval-tied "which prompt version got 0.83 vs 0.79."

**Why it matters:** Prompt churn is real. When you start running structured
evals (#4), you'll want to identify *which* prompt produced a regression.

**Shape:** `Prompt(name, version, body, variables, parent_id)` + a tiny
registry (could be YAML files in `src/prompts/`). This is also where
`SKILL.md` content should land conceptually — a skill *is* a versioned
prompt template plus tools.

### 7. Multi-agent / handoff abstraction
**Missing:** A notion of "agent A hands off to agent B." MAF, OpenAI
Agents, LangGraph, and AutoGen all have native handoff/swarm primitives,
but they're shaped differently. Without an `AgentHandoff` Protocol (or at
least an `AgentSpec` dataclass), every multi-agent tutorial will reach
directly into the underlying framework.

**Why it matters:** Skills (gap #1 in the previous section) are *almost*
this — a skill is conceptually a sub-agent. Naming it explicitly and
writing per-framework adapters that compile `AgentSpec` to MAF `Workflow` /
OpenAI `handoff()` / LangGraph subgraph would unblock the "multi-framework
multi-agent demo" story before it gets messy.

### 8. Artifact / blob store abstraction
**Missing:** An `ArtifactStore` Protocol (`put(bytes) -> uri`, `get(uri) ->
bytes`, `list(session_id)`, `expire`). Code execution produces plots, data
files, intermediate notebooks. Today these live transiently in the MCP
server's session.

**Why it matters:** Without this, "share the plot from agent A's run with
agent B" requires both to be on the same MCP server with the same session.
With it, plots become first-class results (#3) and can flow through the
decision log as references rather than base64 blobs.

### 9. Permission / capability abstraction
**Missing:** A project-wide `CapabilityPolicy` Protocol.
[src/data_lake/tools/permissions.py](../src/data_lake/tools/permissions.py)
exists, but it's specific to data-lake reads. There's no general "this
thread/session is allowed to invoke tool X with arg shape Y on tenant Z."

**Why it matters:** As soon as agora-workbench is multi-tenant in
production, you'll need to gate `code_execution` (which can run arbitrary
code) and `data_lake` writes (which can mutate a shared catalog). Today
permissions are enforced inconsistently per component. A single Protocol +
a function-middleware adapter that consults it before every tool call is
the cleanest deployment story.

### 10. Telemetry / OpenTelemetry abstraction
**Missing:** A `Tracer` Protocol. The decision log captures decisions, but
it's not OTel. Production observability (Application Insights, Datadog,
Honeycomb) speaks spans/metrics/logs.

**Why it matters:** Decision log is great for *agent-aware* introspection.
OTel is required for *operations-aware* introspection (latency p99, error
budgets, distributed tracing across MCP + LLM + tool calls). A thin
`Tracer` Protocol that the decision log can *also* emit to means you get
both with one set of instrumentation calls.

---

## Priority recommendation

If budget exists for **two** of these in the next month:

1. **#1 (Model/LLM abstraction)** — unblocks every new framework tutorial;
   smallest LOC.
2. **#3 (`ToolResult` discriminated union)** — unblocks domain tools that
   need to return files/plots, which is already a real pain point in
   chemistry/earthscience.

The rest can wait until the second framework adapter actually lands and
exposes which gap hurts most in practice. Building all ten now would be
over-engineering — but **naming** them gives future contributors a
checklist.
