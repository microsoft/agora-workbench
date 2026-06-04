# rstats — an R-language domain (tier A, skills-only)

A reference domain showing that a `CodeExecutionServer` does **not** have to run
Python. The agent writes idiomatic **R** into a single `execute_rstats_code`
tool, backed by an [IRkernel](https://irkernel.github.io/)-driven R kernel. As
with [`earthscience`](../earthscience/), there are no wrapper tools — the
deliverable is the *environment* (conda spec + R prelude + skill markdown).

## The one idea

Language is a **ServerConfig knob**, not a framework rewrite:

```python
config = ServerConfig(
    name="rstats",
    language="r",          # <- registers an IRkernel R kernel instead of ipykernel
    type="conda",
    dependency_file=ENVIRONMENT_YML,   # must ship r-base + r-irkernel
    ...
)
```

How does the agent know to write R and not Python? It reads the per-tool
`description`. The server is wired to exactly one R kernel; there is no
language auto-detection. One domain = one language. (Want both R and Python?
Run two domains — the agent simply sees two tools.)

## What the `language="r"` knob does, under the hood

1. `ServerConfig.get_kernel_name()` returns `tools-r` (vs `tools-py`).
2. On startup, `_register_kernel` runs `IRkernel::installspec(...)` inside the
   environment's R, registering a `tools-r` Jupyter kernelspec.
3. The `SessionManager` launches `tools-r` per session.
4. The per-session output preamble is emitted in R
   (`Sys.setenv(AGORA_OUTPUT_DIR=...)`) instead of Python.
5. The Python AST code validator is skipped (it would false-positive on valid R
   like `remove()` / `system()` / `<-`).
6. The `USER_ASSERTION_TOKEN` auth preamble is emitted in R
   (`Sys.setenv` / `Sys.unsetenv`), so the domain also works under Entra auth.

Execution transport and output capture are unchanged: the Jupyter messaging
protocol is language-agnostic, so text results (`execute_result`), inline plots
(`display_data`), and file artifacts all flow through the existing machinery.

## Prerequisites

A conda environment containing `r-base` and `r-irkernel` (see `ENVIRONMENT_YML`
in `server/rstats_server.py`). With `auto_build=True` the server builds it on
first run via conda/mamba. On a host without conda, a standalone
[`micromamba`](https://mamba.readthedocs.io/en/latest/user_guide/micromamba.html)
can create the same environment:

```bash
micromamba create -p ~/.cache/mcp-envs/rstats/conda -c conda-forge \
    r-base r-irkernel r-data.table r-jsonlite r-ggplot2 python=3.11 ipykernel
```

## Run

```bash
python -m domain_examples.rstats.server.rstats_server          # serve over HTTP
python -m domain_examples.rstats.server.rstats_server --warm   # pre-build + register kernel, then exit
```

## Layout

| Path | Purpose |
| --- | --- |
| `server/rstats_server.py` | `ServerConfig` (`language="r"`), R prelude, server entry point |
| `skills/SKILL.md` | R recipes: load data, wrangle with data.table, model, plot, return artifacts |

## Known limitations (this is a first cut)

- **Asset auto-injection is still Python.** The `<local>` asset-reference
  feature injects `from pathlib import Path`; don't use auto asset references in
  R snippets yet. Load data explicitly with `data.table::fread()` instead.
- **`language` supports `python` and `r`.** Julia (IJulia) is the natural next
  one and would follow the same pattern.
