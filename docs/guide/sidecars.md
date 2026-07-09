# Sidecars: sharing a heavy resource across sessions

A **sidecar** is a long-lived helper process that a `CodeExecutionServer`
launches at startup and shuts down on exit. It exists to hold expensive,
process-global state — most often a large model — so that state is loaded
**once per container** and shared by every kernel session over loopback HTTP.

## When to use a sidecar

Each `execute_{name}_code` session runs in its **own isolated kernel process**.
That isolation is a feature — sessions can't corrupt each other's state — but it
means anything a tool loads is loaded *per session*. For a lightweight import
that's fine. For a multi-gigabyte model it is not: N concurrent sessions load N
copies of the model, and a worker fan-out inside each copy multiplies the cost
further. On a modestly-sized host this quickly exhausts RAM and the kernel's
OOM-killer starts reaping processes.

A sidecar breaks that multiplication. The model is loaded once in the sidecar;
kernels stay cheap and call into it over HTTP.

Reach for a sidecar when **all** of these hold:

- A resource is **expensive to initialize** (large model weights, a big rule
  base, a warm connection pool) and would otherwise be re-initialized inside
  every kernel session.
- The resource is **safe to share** across sessions — it is read-only at
  inference time, or you serialize access inside the service.
- The work is **request/response** shaped and fits a small HTTP surface
  (`POST /predict`, `GET /health`).

Do **not** use a sidecar for:

- **Static files** (model weights on disk, reference datasets). Those are
  [assets](working-with-data.md#asset-provisioning) — declare them in
  `ServerConfig.assets` and read them from `MCP_ASSET_CACHE_DIR`. A sidecar is
  for a *running process*, not a file.
- **Cheap, per-call work** that doesn't benefit from shared warm state.
- **Proxying other MCP servers.** That is a
  [`ConnectorServer`](server-networks.md) — a sidecar is a plain internal
  service, invisible to the agent, not an MCP endpoint.

## How it works

Declare sidecars on your `ServerConfig`:

```python
from agora_workbench.code_execution import ServerConfig, SidecarConfig

config = ServerConfig(
    name="retrochimera",
    description="Retrosynthesis route prediction.",
    type="conda",
    dependency_file="...",
    sidecars=[
        SidecarConfig(
            name="retrochimera-model",
            # argv appended to the kernel env's Python (use_env_python=True)
            command=["-m", "retrochimera_tools.model_service"],
            url_env_var="RETROCHIMERA_MODEL_SERVICE_URL",
            port=8901,
            health_path="/health",
            readiness_timeout_s=300.0,
        ),
    ],
)
```

Lifecycle, handled for you by `CodeExecutionServer`:

1. **Startup** — after the kernel environment is built, the server launches each
   sidecar, then polls its `health_path` until it returns `2xx` (or
   `readiness_timeout_s` elapses, which fails startup). Kernels are only
   registered *after* the sidecars are ready.
2. **Discovery** — the sidecar's base URL (`http://{host}:{port}`) is exported
   into the server process environment under `url_env_var`. Because each kernel
   inherits the server's environment at spawn, **every session sees this
   variable** — the same mechanism used for `MCP_ASSET_CACHE_DIR`.
3. **Shutdown** — on server exit each sidecar is stopped gracefully
   (`SIGTERM`, then `SIGKILL` after a grace period).

Sidecars are **not** started by `warm()` (build-time environment prep only) —
they are runtime processes tied to a serving instance.

### The `use_env_python` split

The server process runs in the base image's Python, but your **tools** — and
usually your model — live in the per-server **kernel environment**. With
`use_env_python=True` (the default), `command` is appended to that environment's
interpreter (`ServerConfig.get_python_path()`), so the sidecar can import the
same heavy dependencies the tools use, with no second environment to manage.

Set `use_env_python=False` to run an arbitrary executable verbatim instead (a
standalone binary, or a service served from a different interpreter).

## Writing the sidecar service

The sidecar is an ordinary HTTP server. It is told where to bind via the
`SIDECAR_HOST` and `SIDECAR_PORT` environment variables the framework injects,
and it must expose the `health_path` used for the readiness probe. Keep it on
loopback — a sidecar is an internal implementation detail and must not be
exposed off-box.

```python
# retrochimera_tools/model_service.py
import os
from fastapi import FastAPI
from .model import load_model  # expensive: called exactly once here

app = FastAPI()
_model = load_model()  # loaded once for the whole container

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/predict")
def predict(payload: dict):
    return _model.predict(payload["smiles"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.environ.get("SIDECAR_HOST", "127.0.0.1"),
        port=int(os.environ["SIDECAR_PORT"]),
    )
```

If the model is not safe for concurrent access, serialize calls inside the
service (e.g. an `asyncio.Lock` or a single worker) rather than relying on the
caller.

## Calling the sidecar from tool code

Tool code reads the injected URL and talks to the sidecar over HTTP. Fall back
to a direct in-process call when the variable is absent, so the same tool works
in tests and local development without the sidecar running:

```python
import os
import httpx

def predict_retrosynthesis(smiles: str) -> dict:
    base = os.environ.get("RETROCHIMERA_MODEL_SERVICE_URL")
    if base:
        resp = httpx.post(f"{base}/predict", json={"smiles": smiles}, timeout=600)
        resp.raise_for_status()
        return resp.json()
    # Dev/test fallback: load and run the model in-process.
    from .model import load_model
    return load_model().predict(smiles)
```

## Configuration reference

`SidecarConfig` fields:

| Field | Default | Description |
|-------|---------|-------------|
| `name` | — | Logical name for the sidecar (must be unique within a server). |
| `command` | — | Argument vector. With `use_env_python=True` these args are appended to the kernel env's Python; with `False`, executed verbatim. |
| `use_env_python` | `True` | Prepend the kernel environment's Python interpreter to `command`. |
| `url_env_var` | — | Env var under which the base URL is exported to the server process (and inherited by kernels). |
| `host` | `127.0.0.1` | Loopback bind address. Keep internal. |
| `port` | — | TCP port the sidecar listens on (also passed as `SIDECAR_PORT`). |
| `health_path` | `/health` | Path polled (HTTP GET, expecting `2xx`) to determine readiness. |
| `readiness_timeout_s` | `120.0` | Max seconds to wait for readiness before failing startup. |
| `env` | `{}` | Extra environment variables set on the sidecar process. |

Helpers: `base_url()` returns `http://{host}:{port}`; `health_url()` returns the
full readiness URL.

## Sidecar vs. a second container

A sidecar is a **co-located** process — same container, same kernel
environment, loopback transport. Prefer it: there is no extra image to build,
no cross-container networking, and it reuses the environment your tools already
need.

Reach for a **separate container** only when you need to scale the model
independently of the server, share one model instance across multiple server
replicas, or give it a fundamentally different runtime (e.g. GPU scheduling). In
that case point tool code at the remote service's URL directly — the sidecar
abstraction is unnecessary because the framework isn't managing that process's
lifecycle.
