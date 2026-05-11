# Implementation plan — Model/LLM abstraction (`src/llm/`)

This plan implements **Future Abstraction #1** from
[ABSTRACTION_LAYERS.md](./ABSTRACTION_LAYERS.md): a framework-agnostic
`ModelSpec` dataclass plus per-framework factories.

## Scope discipline

Ship `ModelSpec` + the **MAF factory only**. LangGraph / OpenAI Agents
factories are deliberately deferred — they land in follow-up PRs when those
framework integrations actually begin. Stubbing them now would be
misleading.

## Branching (Option A)

- This branch (`bnguy/abstraction_layers`) is **doc-only** — holds
  `ABSTRACTION_LAYERS.md` and this plan.
- Code work lands on a separate branch `bnguy/llm-abstraction` cut off
  `main`.

## Phases

### Phase 1 — Core module (~120 LOC)
New files:
```
src/llm/
├── __init__.py        # exports ModelSpec, default_credential_factory
├── spec.py            # ModelSpec dataclass + from_env classmethod
└── credentials.py     # default_credential_factory wrapping auth.providers
```

`spec.py`:
- `ModelSpec` frozen dataclass: `provider`, `model`, `endpoint`,
  `api_version`, `credential_factory`, `api_key`, `scope`, `temperature`,
  `max_tokens`, `extra: dict`.
- `provider: Literal["azure_openai", "openai", "ollama", "litellm"]` — all
  four literals defined now, but only `azure_openai` supported in Phase 2.
- `ModelSpec.from_env(prefix="AZURE_OPENAI")` reading
  `{prefix}_ENDPOINT`, `{prefix}_DEPLOYMENT_NAME`, `{prefix}_API_KEY`,
  `API_VERSION`, `AOAI_SCOPE`.
- `__post_init__` validates exactly one of `credential_factory` xor
  `api_key`.

`credentials.py`:
- `default_credential_factory()` wraps `auth.providers.get_token_provider()`.

### Phase 2 — MAF factory (~50 LOC)
```
src/llm/factories/
├── __init__.py        # exports make_maf_client
└── maf.py             # def make_maf_client(spec) -> OpenAIChatClient
```
- Lazy-imports `agent_framework.openai.OpenAIChatClient` at call time.
- Raises `NotImplementedError` for non-`azure_openai` providers.
- Mirrors the kwargs currently used in
  [docs/tutorials/maf_quickstart/llm.py](./tutorials/maf_quickstart/llm.py).

### Phase 3 — Tests (~150 LOC test code)
```
src/llm/tests/
├── __init__.py
├── conftest.py
├── test_spec.py
└── test_factory_maf.py
```
`test_spec.py`:
- `from_env` reads each documented env var.
- Missing required env vars raise clearly.
- `__post_init__` validation (both auth modes → raise; neither → raise).
- Frozen / hashable.

`test_factory_maf.py`:
- `pytest.importorskip("agent_framework")`.
- `make_maf_client(spec)` returns an `OpenAIChatClient` instance with
  expected attributes.
- `NotImplementedError` for `provider="openai"`.
- **No** live LLM calls.

### Phase 4 — Migrate MAF quickstart (~30 LOC delta)
- Replace bodies of `_build_azure_openai_entra` /
  `_build_azure_openai_apikey` in
  [docs/tutorials/maf_quickstart/llm.py](./tutorials/maf_quickstart/llm.py)
  with `ModelSpec` + `make_maf_client`. Keep public signatures.
- One-sentence note in the tutorial README.
- Validate: re-run the quickstart end-to-end (`aspirin` query) and
  confirm bit-identical behavior.

### Phase 5 — CI / live test (follow-up PR)
- Register `pytest.mark.live` in `pyproject.toml`.
- `src/llm/tests/test_factory_maf_live.py` — 1-token completion against
  TRAPI.
- CI runs `pytest -m "not live"` always; `pytest -m live` only when
  secrets present.

## Risk register

| Risk | Mitigation |
|---|---|
| `from_env` defaults silently change behavior | Phase 4 validation: re-run tutorial; document expected `ModelSpec` values for current `.env` in a test fixture. |
| `OpenAIChatClient` API changes again | Lazy import + single factory function → 5-line fix. |
| Parallel PR already added something similar | Run `grep_search "ModelSpec\|llm_factory"` on `main` before starting Phase 1. |
| Auth scope drift | Let `default_credential_factory()` return whatever it returns; `ModelSpec.scope` passed explicitly. |

## Sequencing summary

| Phase | What | Files | LOC | Done when |
|---|---|---|---|---|
| 0 | Branch | — | 0 | `bnguy/llm-abstraction` exists |
| 1 | Core module | 3 new | ~120 | `from llm import ModelSpec` works |
| 2 | MAF factory | 2 new | ~50 | `make_maf_client(spec)` returns client |
| 3 | Tests | 3 new | ~150 | `uv run pytest src/llm/tests` green |
| 4 | Tutorial migration | 2 edits | ~30 net | quickstart still passes |
| 5 (later) | CI / live | 1 new | ~40 | — |

**Total scope of phases 0–4: ~350 LOC + ~12 tests.**

## Out of scope (explicitly)

- `make_langgraph_client` / `make_openai_agents_model`.
- `openai` direct, Ollama, LiteLLM providers (literals exist; factories
  raise).
- Unified `ChatClient` Protocol for invocation parity.
- `BudgetTracker` integration (separate abstraction #5).
