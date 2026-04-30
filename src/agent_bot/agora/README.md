# agent_bot/agora — Agent Implementations

This package provides two production-ready MAF agent classes.

| Class | Module | Description |
|---|---|---|
| [`AgoraAgent`](#agoraagent) | `agent.py` | Default, opinionated agent. MCP tools auto-discovered, BM25 search, interactive mode. |
| [`ModularAgent`](#modularagent) | `modular_agent.py` | Standalone, hook-driven agent. Every pipeline step can be replaced at construction time. |

Both expose the same public interface (`run`, `go`, async context manager).

---

## AgoraAgent

```python
from agent_bot.agora import AgoraAgent
```

The default agent.  All registered MCP servers are discovered automatically
from `server_registry.yaml`, a BM25 `search_tools` function is created, and
the workflow pauses interactively whenever the model issues a `HelpResponse`.

### Constructor parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `domain_prompt_path` | `str \| None` | `None` | Path to a Jinja template providing the domain-specific portion of the system prompt. |
| `llm` | `str` | `"gpt-4o"` | Azure OpenAI deployment name. |
| `max_iterations` | `int` | `500` | Maximum LLM inference calls per workflow run. |
| `user_token` | `str` | `""` | Bearer token for OBO flows; leave empty for local dev (`az login`). |
| `search_backend` | `type[ToolSearchBackend] \| None` | `None` | Tool-search backend *class* (not instance). Defaults to `BM25ToolSearchBackend`. |
| `context_providers` | `list \| None` | `None` | Extra MAF `BaseContextProvider` instances to register with the executor. |
| `middleware` | `list \| None` | `None` | Extra MAF middleware instances to register with the executor. |

### Quick start

```python
import asyncio
from agent_bot.agora import AgoraAgent
from dotenv import load_dotenv

load_dotenv()

async def main():
    async with AgoraAgent(llm="gpt-4o") as agent:
        print(await agent.run("Summarise the Texas power grid."))

asyncio.run(main())
```

---

## ModularAgent

```python
from agent_bot.agora import ModularAgent
```

A **standalone** agent (not a subclass of `AgoraAgent`) whose behaviour is
composed entirely through hook callables passed at construction time.  Default
behaviour (no hooks supplied) is identical to `AgoraAgent`.

Use `ModularAgent` when you need to:

* Replace or augment the tool list without subclassing.
* Inject a custom search backend or a custom search-tool factory.
* Integrate skill discovery and advertisement.
* Attach sub-agent tools at construction time.
* Run the agent unattended in autopilot mode.
* Enforce a set of required tools and fail fast if they are missing.

### Constructor parameters

#### Shared with AgoraAgent

| Parameter | Type | Default | Description |
|---|---|---|---|
| `domain_prompt_path` | `str \| None` | `None` | Path to a Jinja template providing the domain-specific portion of the system prompt. |
| `llm` | `str` | `"gpt-4o"` | Azure OpenAI deployment name. |
| `max_iterations` | `int` | `500` | Maximum LLM inference calls per workflow run (tracked by `BaseLLMExecutor`). |
| `user_token` | `str` | `""` | Bearer token for OBO flows; leave empty for local dev (`az login`). |
| `search_backend` | `type[ToolSearchBackend] \| None` | `None` | Tool-search backend *class*. Ignored when `search_tool_factory` is supplied. Defaults to `BM25ToolSearchBackend`. |
| `context_providers` | `list \| None` | `None` | Initial MAF `BaseContextProvider` instances. Applied before `context_provider_modulator`. |
| `middleware` | `list \| None` | `None` | Initial MAF middleware instances. Applied before `middleware_modulator`. |

#### ModularAgent-only hooks (keyword-only)

All parameters below are **keyword-only** (must be passed by name).

---

##### Autopilot

| Parameter | Type | Default | Description |
|---|---|---|---|
| `autopilot` | `bool` | `False` | When `True`, any `HelpResponse` or `request_info` pause is auto-resolved with a synthetic "best effort" message so the workflow never blocks for user input. Useful for CI, batch runs, and automated evaluations. |

---

##### Tool pipeline hooks

These hooks are called in order during `_build_tools()`, which runs once at
the start of the first workflow execution.

| Parameter | Signature | Default | Description |
|---|---|---|---|
| `enable_auto_tool_discovery` | `bool` | `True` | When `True`, MCP server tools are auto-discovered from `server_registry.yaml`. Set to `False` to skip MCP discovery and supply all tools manually. |
| `auto_tool_discovery` | `() -> list \| None` | `None` | Called after MCP discovery to append additional tools. Return `None` or an empty list to add nothing. |
| `search_tool_factory` | `(backend_cls: type \| None, user_token: str) -> Any` | `None` | Replaces the default BM25 `search_tools` function. Receives the `search_backend` class (may be `None`) and `user_token`. |
| `skill_search_tool_factory` | `(skill_names: list[str]) -> Any \| None` | `None` | Produces an optional skill-search tool. Receives the list of discovered skill names. Return `None` to skip. |
| `sub_agent_tool_factories` | `list[() -> Any \| None]` | `None` | List of zero-argument factories, each returning a tool (or `None` to skip). Called after `skill_search_tool_factory`. |
| `tool_modulator` | `(tools: list) -> list \| None` | `None` | Post-processing hook called with the complete assembled tool list. Return a modified list, or `None` to leave it unchanged. |
| `required_tools` | `list[str] \| None` | `None` | Tool names that must be present after assembly. Missing names are appended to the errors list returned by `_build_tools()`. |

Assembly order within `_build_tools()`:

```
1. MCP server tools          ← controlled by enable_auto_tool_discovery
2. auto_tool_discovery()     ← append custom tools
3. search tool               ← search_tool_factory or default BM25
4. skill_search_tool_factory ← optional skill search tool
5. sub_agent_tool_factories  ← sub-agent tools
6. tool_modulator            ← final post-processing pass
7. required_tools check      ← validation
8. skill_advertiser          ← system-prompt append
```

---

##### Skill hooks

| Parameter | Signature | Default | Description |
|---|---|---|---|
| `enable_auto_skill_discovery` | `bool` | `False` | When `True`, calls `auto_skill_discovery()` at construction time to populate `self._discovered_skills`. |
| `auto_skill_discovery` | `() -> list[str] \| None` | `None` | Returns a list of skill names. Called once during `__init__` when `enable_auto_skill_discovery=True`. |
| `skill_advertiser` | `(skill_names: list[str]) -> str` | `None` | Receives the discovered skill names and returns a string that is appended to the system prompt. Called at the end of `_build_tools()`. |

---

##### Context and middleware hooks

| Parameter | Signature | Default | Description |
|---|---|---|---|
| `context_provider_modulator` | `(providers: list) -> list \| None` | `None` | Called at construction time with a copy of `context_providers`. The returned list replaces `self._context_providers`. Return `None` to leave unchanged. |
| `middleware_modulator` | `(middleware: list) -> list \| None` | `None` | Called at construction time with a copy of `middleware`. The returned list replaces `self._middleware`. Return `None` to leave unchanged. |

---

### Quick start

```python
import asyncio
from agent_bot.agora import ModularAgent
from dotenv import load_dotenv

load_dotenv()

async def main():
    async with ModularAgent(llm="gpt-4o") as agent:
        print(await agent.run("Summarise the Texas power grid."))

asyncio.run(main())
```

### Recipes

#### Disable MCP discovery and supply tools manually

```python
from agent_bot.agora import ModularAgent

agent = ModularAgent(
    llm="gpt-4o",
    enable_auto_tool_discovery=False,    # skip server_registry.yaml scan
    auto_tool_discovery=lambda: [my_custom_tool],
)
```

#### Replace the search tool

```python
from tools.search import AzureAIToolSearchBackend, create_search_tools_function

agent = ModularAgent(
    llm="gpt-4o",
    search_tool_factory=lambda _cls, token: create_search_tools_function(
        AzureAIToolSearchBackend(user_token=token)
    ),
)
```

#### Advertise domain skills in the system prompt

```python
from planning.skills import discover_skill_paths

agent = ModularAgent(
    llm="gpt-4o",
    enable_auto_skill_discovery=True,
    auto_skill_discovery=discover_skill_paths,
    skill_advertiser=lambda skills: (
        "## Available skills\n" + "\n".join(f"- {s}" for s in skills)
    ),
)
```

#### Filter or add tools at the last step

```python
BLOCKED = {"dangerous_tool"}

def modulator(tools):
    return [t for t in tools if getattr(t, "name", "") not in BLOCKED]

agent = ModularAgent(llm="gpt-4o", tool_modulator=modulator)
```

#### Enforce required tools

```python
agent = ModularAgent(
    llm="gpt-4o",
    required_tools=["execute_code", "search_tools"],
)

# Check errors after first workflow invocation:
tools, errors = agent._build_tools()
if errors:
    raise RuntimeError(f"Tool setup failed: {errors}")
```

#### Run in autopilot mode (no user interaction)

```python
agent = ModularAgent(llm="gpt-4o", autopilot=True)
result = await agent.run("Analyse the ERCOT grid and produce a summary report.")
```

#### Inject custom context providers and middleware

```python
from middleware.decision_log import DecisionLogContextProvider, DecisionLogChatMiddleware

agent = ModularAgent(
    llm="gpt-4o",
    context_providers=[DecisionLogContextProvider()],
    middleware=[DecisionLogChatMiddleware()],
)
```

Or transform the lists at construction time:

```python
agent = ModularAgent(
    llm="gpt-4o",
    context_provider_modulator=lambda providers: providers + [MyExtraProvider()],
    middleware_modulator=lambda mw: [AuditMiddleware()] + mw,
)
```

---

## Public API summary

Both `AgoraAgent` and `ModularAgent` expose:

```python
async with Agent(...) as agent:
    text: str = await agent.run(prompt, input_handler=None)
    msg: Message = await agent.go(prompt, input_handler=None)
    await agent.close()
```

`input_handler` is an optional `async (question: str, context: str) -> str`
callable invoked when the agent asks for user clarification.  Defaults to
console input.  Ignored in `ModularAgent` when `autopilot=True`.
