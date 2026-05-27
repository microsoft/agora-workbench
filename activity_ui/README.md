# Activity UI

A small standalone web app that shows what your agent is doing on the
workbench MCP servers — the code it's writing, the tools it's calling, the
data it's pushing — in real time.

For BYOA users (Claude Code, Cursor, custom agents) who don't have a
chat-with-tool-trace UI built into their agent: this is the window into
the server side of the conversation.

## What it is

One FastAPI process in a Docker container. Three source files do the real work:

| File | Role |
|---|---|
| [`server.py`](server.py) | FastAPI app — defines endpoints, in-memory event bus |
| [`models.py`](models.py) | Pydantic `ActivityEvent` schema (the wire format) |
| [`static/index.html`](static/index.html) | The browser page — vanilla HTML/CSS/JS, no build step |

Endpoints (defined in `server.py:82-131`):

| Endpoint | Purpose |
|---|---|
| `POST /events` | Ingest — MCP servers publish here |
| `GET /events/recent` | JSON snapshot of the 200-event ring buffer (browser fetches on load) |
| `GET /stream` | Server-Sent Events — browser tails for live updates |
| `GET /` | Serves `static/index.html` |
| `GET /healthz` | Liveness probe |

## Architecture

```
   Agent (Claude Code / Cursor / custom)
       │ MCP call
       ▼
   chemistry-server-1     earthscience-server-1     ...
       │                       │
       │ fire-and-forget HTTP POST to ACTIVITY_UI_URL/events
       ▼                       ▼
                 agora-activity Docker network
                              │
                              ▼
                       activity-ui-1
                       (FastAPI + ring buffer + SSE fan-out)
                              │ 127.0.0.1:8030 port mapping
                              ▼
                       your browser
```

Each MCP server publishes events fire-and-forget via the
[`ActivityPublisher`](../code_execution/code_execution/activity_publisher.py).
If `ACTIVITY_UI_URL` is unset, or the UI is down, publishes silently
no-op — tool execution is never blocked or failed by observability.

The activity-ui itself owns no agent state; it's a fan-out relay with
a small in-memory buffer.

## Quickstart

```bash
# One-time per host:
docker network create agora-activity

# Start the UI (stays up across MCP-server restarts):
cd src/activity_ui
docker compose up -d --build

# Then bring up whichever MCP servers you want.
# Each one's compose attaches to the same shared network:
cd ../domain_examples/chemistry
docker compose up -d --build

# Open the UI:
#   http://127.0.0.1:8030
```

Stop the UI when you don't need it:

```bash
cd src/activity_ui && docker compose down
```

MCP servers' publishes will fail silently after that — tool calls keep working.

## Wiring a new MCP server to publish events

In the server's `docker-compose.yml`, add two things:

```yaml
services:
  my-server:
    environment:
      # Reach the shared activity-ui by its service name.
      ACTIVITY_UI_URL: "http://activity-ui:8030"
    networks:
      - agora-activity

networks:
  agora-activity:
    external: true
```

That's it. The BYOA template at `deployment/example/docker-compose.yml`
ships with this wiring pre-written but commented out — uncomment the
`[activity-ui]` blocks to opt in.

If `ACTIVITY_UI_URL` is left unset, the publisher is a silent no-op.

## Event schema

All events are flat `ActivityEvent` dicts ([`models.py`](models.py)). The currently-fired event types:

| Type | Fired by | Carries |
|---|---|---|
| `code_executed` | Successful sync `execute_code`; successful parallel child | `code`, `stdout`, `stderr`, `success`, `duration_ms`, `tool_calls`, `session_id`, `batch_id` (parallel) |
| `code_failed` | Failed sync `execute_code`; failed/cancelled parallel child | Same fields with `success=false`, `error` |
| `job_started` | `execute_code(background=True)` | `code`, `job_id`, `session_id` |
| `job_finished` | Background job reaches a terminal state | `job_id`, `session_id`, `success`, `stdout`, `stderr`, `error`, `duration_ms` |
| `tool_search` | `search_{name}_tools` returns | `query`, `category`, `matched_tools`, `matched_skills`, `session_id`, `success` |
| `skill_loaded` | `load_{name}_skill` returns | `skill_name`, `session_id`, `success` |
| `workflow_planned` | `plan_{name}_workflow` returns | `domain`, `mode`, `current_state`, `target_state`, `tool_name`, `session_id` |
| `batch_cancelled` | `{name}_cancel_batch` succeeds or fails | `batch_id`, `session_id`, `success`, `error` |
| `push_object_sent` | `{name}_push_object` (sender side) | `transfer_id`, `variable_name`, `target_server`, `session_id` |
| `push_object_received` | The receiving server's `/receive_transfer` endpoint | `transfer_id`, `variable_name`, `source_server`, `session_id` |

Every event also carries `server` (the MCP server's name) and `timestamp`
(unix seconds, set by the publisher).

The UI groups events by `session_id` in the feed. Events without a
`session_id` land in a `(no session)` bucket.

## Development

### Iterating on `static/index.html`

The static file is copied into the image at build time. After editing:

```bash
cd src/activity_ui
docker compose up -d --build
```

The chemistry/earthscience containers don't need to restart.

### Iterating on publish call sites

Publish calls live in `src/code_execution/code_execution/` — that source is
baked into the base image `mcp-server-base:local`. After editing, rebuild
the base, then the domain server:

```bash
docker build -f deployment/base.Dockerfile -t mcp-server-base:local .
cd examples/domain_examples/chemistry
docker compose up -d --build --force-recreate
```

This is a multi-stage Docker gotcha — `docker compose up --build` only
rebuilds the domain layer; locally-tagged base images don't auto-refresh.

### Smoke test without a real agent

You can POST synthetic events:

```bash
curl -X POST http://127.0.0.1:8030/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"tool_search","server":"chemistry","query":"molecule","matched_tools":["parse_molecule","compute_descriptors"],"matched_skills":[],"session_id":"test-sess"}'
```

Then refresh the page in the browser. The event should appear in a
session block labeled `session test-ses…`.

## Limitations

These are intentional, not bugs:

- **No persistence.** Buffer is in-memory; restart of the activity-ui
  container clears history. Sessions and live publishes are unaffected.
- **No auth.** The compose binds to `127.0.0.1:8030` only — loopback is the
  whole security model. Safe for single-user local dev. **Do not bind to
  `0.0.0.0` without putting auth in front of it** — events contain raw
  agent code, stdout, and tool arguments.
- **No real-time intra-call streaming.** Events fire at MCP-tool-call
  boundaries, not while code is running. A long `execute_code` shows
  nothing until it completes, then the whole event lands.
- **No events for poll-shaped tools.** `check_job` and `check_batch`
  intentionally do not publish — they are just status reads. The
  underlying job/batch is summarised at lifecycle transitions
  (`job_started`/`job_finished`, `batch_cancelled`) instead.
- **No events for session-management tools.** `list_sessions`,
  `get_session_info`, `close_session`, `inspect_session` are
  housekeeping calls and do not surface in the feed.

## Files

```
src/activity_ui/
├── Dockerfile          # Python 3.12-slim image
├── docker-compose.yml  # Standalone service + agora-activity external network
├── requirements.txt    # fastapi, uvicorn, sse-starlette, pydantic
├── server.py           # FastAPI app, ring buffer, SSE fan-out
├── models.py           # ActivityEvent schema
├── static/
│   └── index.html      # The page (no build step)
└── README.md           # This file
```
