# Monitoring your servers

The primary way to observe what your agent is doing on MCP servers is the **activity UI** — a lightweight web app that streams code executions, tool searches, skill loads, and artifact publishes in real time.

## Activity UI overview

The activity UI is a standalone FastAPI service that MCP servers publish events to. You open it in a browser and watch your agent work:

```
Agent (Claude Code / Cursor / custom)
    │ MCP call
    ▼
CodeExecutionServer(s)
    │ fire-and-forget POST to ACTIVITY_UI_URL/events
    ▼
activity-ui (FastAPI + in-memory ring buffer + SSE fan-out)
    │ http://127.0.0.1:8030
    ▼
your browser
```

The UI groups events by session and shows them as expandable cards with the code, stdout/stderr, tool calls, rendered plots, and downloadable artifacts.

## Starting the activity UI

```bash
# Create the shared Docker network (one-time):
docker network create agora-activity

# Start the UI:
cd activity_ui/
docker compose up -d --build

# Open in browser:
#   http://127.0.0.1:8030
```

The UI stays up across MCP server restarts. Stop it when you don't need it:

```bash
cd activity_ui/ && docker compose down
```

MCP servers' publishes silently no-op after that — tool calls keep working.

## Connecting your server to the activity UI

In your server's `docker-compose.yml`, add the environment variable and network:

```yaml
services:
  my-server:
    environment:
      ACTIVITY_UI_URL: "http://activity-ui:8030"
    networks:
      - agora-activity

networks:
  agora-activity:
    external: true
```

If `ACTIVITY_UI_URL` is unset, the publisher is a silent no-op — observability never affects code execution.

## What the UI shows

The UI renders a live feed of event cards grouped by session. Each event type shows different information:

| Event type | What you see |
|---|---|
| `code_executed` | The Python code, stdout, stderr, duration, and any tool calls made |
| `code_failed` | Same as above, plus the error message and traceback |
| `job_started` / `job_finished` | Background job lifecycle with final results and tool-call traces |
| `tool_search` | The query, matched tools, and matched skills |
| `skill_loaded` | Which skill was loaded into the session |
| `workflow_planned` | Domain, current/target state, and planned tool |
| `artifact_published` | File name, destination, size, and download link |
| `object_sent` / `object_received` | Cross-server object transfer tracking |
| `batch_cancelled` | Parallel batch cancellation status |

### Rich outputs

When code produces matplotlib figures, images, SVGs, or HTML via Jupyter display mechanisms, the UI renders them inline in the event card. These rich outputs are streamed to the UI but excluded from the JSON returned to the agent (keeping its context window small).

### Artifacts

Files created in the session's outputs directory appear as a collapsed "artifacts (N)" section with download links. The UI never contains the file bytes — just metadata and links.

## Event schema

Every event carries:

- `type` — the event type (see table above)
- `server` — which MCP server published it (e.g., `"chemistry"`)
- `timestamp` — Unix seconds
- `session_id` — groups events in the UI feed
- `description` — human-readable summary (the `description` parameter you pass to `execute_code`)

!!! tip "Write good descriptions"
    When calling `execute_{name}_code`, always set `description` to a one-line summary (e.g., `"Screen 4 molecules for Lipinski drug-likeness"`). This is the headline shown in the activity UI, so it should narrate *what* and *why*.

## Smoke testing without an agent

POST synthetic events to verify wiring:

```bash
curl -X POST http://127.0.0.1:8030/events \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "code_executed",
    "server": "chemistry",
    "description": "Parse ethanol molecule",
    "code": "result = parse_molecule(smiles=\"CCO\")",
    "stdout": "{\"canonical_smiles\": \"CCO\", \"molecular_weight\": 46.04}",
    "success": true,
    "duration_ms": 42,
    "session_id": "test-session-1"
  }'
```

Refresh the browser — the event appears in a session block.

## Design choices

These are intentional:

- **No persistence** — the buffer is in-memory (200 events by default, configurable via `ACTIVITY_UI_BUFFER`). Restarting the UI clears history.
- **No auth** — binds to `127.0.0.1:8030` only. Loopback is the security model. **Do not bind to `0.0.0.0` without putting auth in front** — events contain raw agent code and stdout.
- **No intra-call streaming** — events fire at MCP tool-call boundaries, not while code is running. A long `execute_code` shows nothing until it completes.
- **No events for poll/housekeeping tools** — `check_job`, `check_batch`, `list_sessions`, `get_session_info` don't publish events. Only lifecycle transitions appear. (Note: `job_finished` events are emitted automatically when a background or promoted job reaches a terminal state — they are not triggered by `check_job` polling.)

## Health endpoint

Every MCP server also exposes `GET /health` for container orchestrator probes:

```yaml
healthProbes:
  - type: liveness
    httpGet:
      path: /health
      port: 8000
    initialDelaySeconds: 10
    periodSeconds: 30
```

The activity UI itself exposes `GET /healthz`.

## Structured logging

For log-based monitoring beyond the activity UI, servers use Python's `logging` module:

| Event | Level | Description |
|-------|-------|-------------|
| Server startup | INFO | Server name, port, environment type |
| Code execution | INFO | Session ID, execution time, success/failure |
| Tool calls | INFO | Tool name, duration, success/failure |
| Auth failures | WARNING | Token validation errors |
| Timeout | WARNING | Execution exceeded timeout |

Set log level via environment variable:

```bash
LOG_LEVEL=DEBUG  # DEBUG, INFO, WARNING, ERROR
```
