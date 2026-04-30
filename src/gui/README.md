# GIS Agent GUI

Chat with the GIS agent and interact with maps it creates — zoom, pan, toggle layers, click features for details.

## Prerequisites

- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- Node.js 22+ (install via [nvm](https://github.com/nvm-sh/nvm) if needed)
- GIS MCP server running at `http://localhost:8006/mcp`
- Azure OpenAI credentials configured (see `.env`)

## Setup

```bash
cd AgoraAgentMAF

# Install Python dependencies (picks up fastapi, uvicorn, etc.)
uv sync

# Install frontend dependencies
cd gui/frontend
npm install
cd ../..
```

## Running

You need **two terminals** — one for the backend, one for the frontend.

### Terminal 1: Backend (FastAPI)

```bash
cd AgoraAgentMAF
uv run uvicorn gui.server:app --host 0.0.0.0 --port 8000 --reload
```

### Terminal 2: Frontend (React + Vite)

```bash
cd AgoraAgentMAF/gui/frontend
npm run dev
```

Open the URL Vite prints (usually http://localhost:5173). The frontend proxies all `/api/*` requests to the backend, so **only port 5173 needs to be accessible** (no need to forward port 8000 separately).

## Stopping

Press **Ctrl+C** in each terminal to stop the servers. If a server is stuck or backgrounded:

```bash
# Kill backend
fuser -k 8000/tcp

# Kill frontend
fuser -k 5173/tcp
```

## How it works

1. **Chat** with the agent in the left panel — ask it to load spatial data, create maps, zoom in, etc.
2. The backend streams the response via **Server-Sent Events (SSE)**. Real-time tool-call indicators appear in the chat panel while the agent is executing, so you can see what it is doing at each step.
3. The agent saves **GeoJSON layers** + a **`map_state.json`** to the `maps/` directory.
4. The React frontend renders layers on an interactive **Leaflet map** in the right panel.
5. Use **layer checkboxes** to toggle visibility. **Click features** to see attribute popups.
6. **Drop pins** or **draw polygons** on the map, then refer to them in chat (e.g. "What substations are inside Polygon 1?"). The coordinates are sent to the agent automatically.
7. Ask the agent to zoom/pan — it updates the view without regenerating data.
8. Open **Experience** to view/edit persistent preferences and lessons, or click **Learn from session** to auto-summarize the current conversation into reusable guidance.

## Map Annotations

You can place annotations on the map and reference them in chat to focus the agent on specific locations.

| Annotation type | How to place | What the agent receives |
|-----------------|-------------|-------------------------|
| **Pin** | Click the pin tool and click on the map | Label, latitude, longitude |
| **Polygon** | Click the polygon tool and draw a shape | Label, vertex coordinates, bounding box |

Annotations are included automatically with the next chat message — you do not need to describe the coordinates manually. Use the label (e.g. "Pin 1", "Polygon 2") to refer to an annotation in your message.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/chat` | Send a message (+ optional annotations); response is an **SSE stream** of tool-call events, the final text response, updated map state, and a `done` sentinel |
| `GET` | `/api/map-state` | Current map state (view + layers) |
| `GET` | `/api/layers/{file}` | Serve a GeoJSON layer file |
| `POST` | `/api/reset` | Reset agent session |
| `GET` | `/api/export-map` | Export the current map as a **standalone HTML file** with all visible layers, styles, and legends embedded (no backend required to view) |
| `GET` | `/api/experience` | Read persistent experience Markdown |
| `PUT` | `/api/experience` | Replace persistent experience Markdown |
| `POST` | `/api/experience/summarize` | Auto-extract lessons/preferences from conversation and persist |
| `POST` | `/api/map-capture/{request_id}` | Receive PNG screenshot for a pending `capture_map_view` request |

### SSE event types (`/api/chat`)

| Event | Payload | Description |
|-------|---------|-------------|
| `tool_call` | `{tool, args, …}` | Agent is about to call a tool |
| `tool_result` | `{tool, result, …}` | Tool returned a result |
| `response` | `{text}` | Final agent text response |
| `map_state` | map state JSON | Updated map state after the turn |
| `done` | `{}` | Stream is complete |
| `error` | `{message, error_id}` | An error occurred |
| `capture_request` | `{request_id, center, zoom, purpose}` | Agent requested a map screenshot (frontend captures and POSTs PNG) |

## Experience system

The GUI currently includes a persistent shared/default **Experience** store:

- Shared file: `gui/experiences/default.md`
- Backend (`gui/experience.py`) stores Markdown preferences/lessons in this single file and serves APIs listed above.
- Agent integration: `ExperienceContextProvider` reads this shared file each turn and injects it into agent instructions.
- Frontend integration: `ExperiencePanel` lets users:
  - Edit and save experience directly
  - Click **Learn from session** to trigger `/api/experience/summarize` and auto-extract map preferences, workflow patterns, and corrections from the current conversation into the shared/default file

This experience is injected into every new GUI session, so preferences persist across resets and restarts for this GUI instance. It is not currently keyed by authenticated user in the backend.

## Visual map capture (`capture_map_view`)

The GUI agent has a `capture_map_view(latitude, longitude, zoom, purpose)` tool:

1. Agent emits SSE `capture_request`.
2. Frontend flies to the requested view.
3. Frontend captures the map with `html2canvas`.
4. Frontend posts PNG to `/api/map-capture/{request_id}`.
5. Agent receives image bytes for visual analysis.

Use cases:

- Verify rendered map quality/layer context
- Inspect location features not represented in vector/tabular fields
- Diagnose user-reported map issues by capturing the exact current view

Prompt guidance in `gui/prompts/system_prompt.jinja` explicitly tells the agent to use `capture_map_view` selectively (when visual context matters) and prefer vector/tabular data when sufficient.

### Export map (`/api/export-map`)

The export endpoint embeds all currently visible GeoJSON layer data, styles, and legends into a single HTML file that can be shared and opened without any server. An optional `basemap` query parameter selects the tile provider:

| Value | Tile source |
|-------|------------|
| `street` (default) | OpenStreetMap |
| `satellite` | Esri World Imagery |
| `topo` | OpenTopoMap |

Example:

```bash
curl "http://localhost:8000/api/export-map?basemap=satellite" -o map.html
```

## Architecture

```
┌──────────────┐    HTTP    ┌──────────────┐    MCP     ┌──────────────┐
│  React App   │◄──────────►│  FastAPI      │◄─────────►│  GIS MCP     │
│  (Leaflet)   │  :5173     │  (Agent)      │  :8000    │  Server      │
│              │            │              │           │  :8006       │
└──────────────┘            └──────┬───────┘            └──────────────┘
                                   │
                              maps/ directory
                           (GeoJSON + map_state.json)
```
