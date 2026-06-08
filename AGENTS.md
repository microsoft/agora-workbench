# Agora Workbench — Development Instructions

## Environment & Commands

The project uses **`uv`** for dependency management with `pyproject.toml` at the repo root. Python ≥3.11 is required. Always run commands from the repo root.

```bash
uv sync                          # Install/update dependencies
uv run pytest -m "not live"      # Run tests (excludes live/credential tests)
uv run ruff check --fix .        # Lint (auto-fix)
uv run ruff format .             # Format
uv run pyright --level error src # Type check (only src/ is checked in CI)
```

Use `uv run` to run any script (e.g. `uv run python src/script.py`) and `uv add` to add packages. The virtual environment is managed automatically — do not manually activate `.venv`.

## Repo Layout

```
src/
  agora_workbench/
    base/              # BaseMCPServer ABC — shared HTTP hosting, auth middleware
    code_execution/    # CodeExecutionServer — kernel-backed code execution
    connector/         # ConnectorServer — lightweight MCP proxy (router, gateway, dispatcher)
    deployment/        # Deployment scaffold CLI and templates
activity_ui/         # Real-time activity monitoring UI
examples/
  domain_examples/   # Reference domain server implementations (chemistry, gis, energy)
  agent_examples/    # Reference agent configurations
docs/               # MkDocs documentation site
```

## Import Conventions

`src/` is on the Python path. All imports use the `agora_workbench` namespace:

```python
from agora_workbench.base import BaseMCPServer
from agora_workbench.code_execution import CodeExecutionServer, ToolDefinition, ToolRegistry
from agora_workbench.connector import RouterServer, GatewayServer
```

Do **not** use bare package names — `from code_execution import ...` is wrong.

## Architecture

`BaseMCPServer` (in `src/agora_workbench/base/`) is the shared abstract base class. Two concrete server types inherit from it:

- **`CodeExecutionServer`** — runs a Python kernel, executes user code, manages sessions and tool registries.
- **`ConnectorServer`** — stateless proxy that aggregates/routes/gates tool calls to upstream servers without its own kernel.

Both expose tools via **FastMCP** over Streamable HTTP with Bearer token auth middleware.

## Adding Domain Tools

Follow the pattern in `examples/domain_examples/`. A domain server:

1. Subclasses `CodeExecutionServer`
2. Defines tools as `ToolDefinition` objects registered with a `ToolRegistry`
3. Provides a `catalog.yaml` describing available tools, data assets, and libraries
4. Tool implementations live in packages installed into the execution kernel environment

## Testing

Tests live in `tests/` subdirectories alongside the code they test. Configured test paths:

- `src/agora_workbench/code_execution/tests/`
- `src/agora_workbench/connector/tests/`
- `examples/domain_examples/tests/`
- `activity_ui/tests/`

Markers: `unit`, `integration`, `live`, `asyncio`. Live tests are excluded by default (they require real credentials/network). `pytest-asyncio` runs in `auto` mode — async test functions are detected automatically.

```bash
uv run pytest -m "not live"                            # All non-live tests
uv run pytest src/agora_workbench/connector/tests/ -v  # Target a specific package
```

## Linting & Type Checking

- **Ruff**: line-length 120, `ruff check` + `ruff format`. Pre-commit auto-fixes.
- **Pyright**: basic mode, `--level error`. The config is intentionally permissive — focus on `reportUndefinedVariable` and `reportUnboundVariable` errors. Do not spend time on suppressed warning categories.

## CI

- **Linting** (`linting.yaml`): triggers on PRs changing `src/**`. Runs `ruff check` and `pyright --level error` on `src/`.
- **Tests** (`tests.yaml`): triggers on PRs changing `src/`, `activity_ui/`, `examples/`. Runs `pytest -m "not live"` across all test paths.

Before finishing work, ensure `uv run ruff check .` and `uv run pytest -m "not live"` pass for any changed areas.

## Optional Extras & Dependency Groups

**Extras** (for optional feature dependencies):
```bash
uv sync --extra openai-agents  # OpenAI Agents SDK adapter
uv sync --extra copilot-sdk    # GitHub Copilot SDK adapter
uv sync --extra geo            # Geospatial (rasterio, titiler)
```

**Dependency groups** (for dev tooling):
```bash
uv sync --group dev    # pytest, ruff, pre-commit, jupyter (default for development)
uv sync --group docs   # mkdocs, mkdocs-material, mkdocstrings
```

## Do Not

- Do not use bare package names — always import via `agora_workbench.*` (e.g., `from agora_workbench.code_execution import ...`).
- Do not bypass `uv` or manually activate `.venv`.
- Do not run live-marked tests unless explicitly requested and credentials are available.
- Do not add strict type annotations or fix pyright warnings that the config intentionally suppresses.