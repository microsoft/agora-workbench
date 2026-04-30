import type { MapState, MapAnnotation, StoryMap } from "./types";

export const API_BASE = "";

/* ------------------------------------------------------------------ */
/* Streaming chat (SSE)                                               */
/* ------------------------------------------------------------------ */

export interface ToolCallArgs {
  [key: string]: unknown;
}

export interface StreamCallbacks {
  onToolCall: (callId: string, name: string, args: ToolCallArgs) => void;
  onToolResult: (callId: string, name: string, success: boolean, result?: string, error?: string, args?: ToolCallArgs) => void;
  onResponse: (text: string, needsInput?: boolean) => void;
  onMapState: (state: MapState | null) => void;
  onCaptureRequest: (requestId: string, center: [number, number], zoom: number) => void;
  onStoryMap: (storyMap: StoryMap) => void;
  onError: (message: string) => void;
  onDone: () => void;
}

/**
 * Send a chat message and stream tool events via SSE.
 *
 * The endpoint returns `text/event-stream` with event types:
 *   tool_call   — agent invoked a tool (name + arguments)
 *   tool_result — tool returned (name + result/error)
 *   response    — final agent response text
 *   map_state   — map state JSON
 *   error       — something went wrong
 *   done        — stream finished
 */
export async function sendMessageStreaming(
  message: string,
  callbacks: StreamCallbacks,
  annotations?: MapAnnotation[],
  viewport?: { center: [number, number]; zoom: number },
): Promise<void> {
  const body: Record<string, unknown> = { message };
  if (annotations && annotations.length > 0) {
    body.annotations = annotations.map((a) => ({
      label: a.label,
      type: a.type,
      geometry: a.geometry,
    }));
  }
  if (viewport) {
    body.viewport = viewport;
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (fetchErr) {
    const msg = fetchErr instanceof Error ? fetchErr.message : String(fetchErr);
    callbacks.onError(`Connection failed: ${msg}`);
    callbacks.onDone();
    return;
  }

  if (!res.ok) {
    const detail = await res.text();
    callbacks.onError(`Agent error: ${detail}`);
    callbacks.onDone();
    return;
  }

  const reader = res.body?.getReader();
  if (!reader) {
    callbacks.onError("No response stream available");
    callbacks.onDone();
    return;
  }

  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";

      for (const part of parts) {
        if (!part.trim()) continue;

        let eventType = "message";
        let data = "";

        for (const line of part.split("\n")) {
          if (line.startsWith("event: ")) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            data = line.slice(6);
          }
        }

        if (!data) continue;

        try {
          const parsed = JSON.parse(data);
          switch (eventType) {
            case "tool_call":
              callbacks.onToolCall(parsed.call_id ?? "", parsed.name ?? "", parsed.arguments ?? {});
              break;
            case "tool_result":
              callbacks.onToolResult(parsed.call_id ?? "", parsed.name ?? "", parsed.success ?? true, parsed.result, parsed.error, parsed.arguments);
              break;
            case "response":
              callbacks.onResponse(parsed.text ?? "", parsed.needs_input ?? false);
              break;
            case "map_state":
              callbacks.onMapState(parsed as MapState | null);
              break;
            case "capture_request": {
              const requestId =
                typeof parsed.request_id === "string" ? parsed.request_id.trim() : "";
              const center = parsed.center;
              const hasValidCenter =
                Array.isArray(center) &&
                center.length === 2 &&
                typeof center[0] === "number" &&
                Number.isFinite(center[0]) &&
                typeof center[1] === "number" &&
                Number.isFinite(center[1]);
              const zoom =
                parsed.zoom === undefined
                  ? 14
                  : typeof parsed.zoom === "number" && Number.isFinite(parsed.zoom)
                    ? parsed.zoom
                    : null;

              if (!requestId || !hasValidCenter || zoom === null) {
                console.warn("Skipping invalid capture_request SSE event:", parsed);
                break;
              }

              callbacks.onCaptureRequest(requestId, [center[0], center[1]], zoom);
              break;
            }
            case "story_map": {
              callbacks.onStoryMap(parsed as StoryMap);
              break;
            }
            case "error":
              callbacks.onError(parsed.message ?? "Unknown error");
              break;
            case "done":
              break;
          }
        } catch (e) {
          console.warn("Skipping malformed SSE event:", data, e);
        }
      }
    }
  } finally {
    reader.releaseLock();
    callbacks.onDone();
  }
}

/* ------------------------------------------------------------------ */
/* Non-streaming endpoints                                            */
/* ------------------------------------------------------------------ */
export async function fetchMapState(): Promise<MapState> {
  const res = await fetch(`${API_BASE}/api/map-state`);
  if (!res.ok) throw new Error("Failed to fetch map state");
  return res.json();
}

export async function fetchLayerGeoJSON(filename: string): Promise<GeoJSON.GeoJsonObject> {
  const res = await fetch(
    `${API_BASE}/api/layers/${encodeURIComponent(filename)}`
  );
  if (!res.ok) throw new Error(`Failed to fetch layer: ${filename}`);
  return res.json();
}

export async function resetSession(): Promise<{ map_state: MapState | null }> {
  const res = await fetch(`${API_BASE}/api/reset`, { method: "POST" });
  if (!res.ok) throw new Error("Failed to reset session");
  return res.json();
}

export async function fetchRasterTileUrl(layerId: string): Promise<string> {
  const res = await fetch(
    `${API_BASE}/api/raster-tile-url/${encodeURIComponent(layerId)}`
  );
  if (!res.ok) throw new Error("Failed to get tile URL");
  const data = await res.json();
  return `${API_BASE}${data.tile_url}`;
}

/**
 * Post a map screenshot (PNG) back to the backend for a pending capture request.
 */
export async function postMapCapture(requestId: string, pngBlob: Blob): Promise<void> {
  const res = await fetch(`${API_BASE}/api/map-capture/${encodeURIComponent(requestId)}`, {
    method: "POST",
    headers: { "Content-Type": "image/png" },
    body: pngBlob,
  });
  if (!res.ok) {
    const responseText = await res.text();
    const errorMessage = responseText
      ? `Failed to post map capture: ${responseText}`
      : `Failed to post map capture: ${res.status} ${res.statusText}`;
    console.error(errorMessage);
    throw new Error(errorMessage);
  }
}

/* ------------------------------------------------------------------ */
/* Experience (persistent user preferences)                            */
/* ------------------------------------------------------------------ */

export async function fetchExperience(): Promise<string> {
  const res = await fetch(`${API_BASE}/api/experience`);
  if (!res.ok) throw new Error("Failed to fetch experience");
  const data = await res.json();
  return data.content ?? "";
}

export async function updateExperience(content: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/experience`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!res.ok) throw new Error("Failed to update experience");
  const data = await res.json();
  return data.content ?? "";
}

export async function summarizeExperience(
  messages: { role: string; content: string }[],
): Promise<string> {
  const res = await fetch(`${API_BASE}/api/experience/summarize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Failed to summarize: ${detail}`);
  }
  const data = await res.json();
  return data.content ?? "";
}

/* ------------------------------------------------------------------ */
/* Data Catalog                                                        */
/* ------------------------------------------------------------------ */

export interface CatalogAsset {
  name: string;
  description: string;
  asset_tag: string;
  domain: string;
  artifact_type: string;
}

export interface CatalogResponse {
  assets: CatalogAsset[];
  configured: boolean;
}

export async function searchDataCatalog(
  query: string = "",
  domain?: string,
  top: number = 30,
  skip: number = 0,
): Promise<CatalogResponse> {
  const params = new URLSearchParams();
  if (query) params.set("q", query);
  if (domain) params.set("domain", domain);
  params.set("top", String(top));
  if (skip > 0) params.set("skip", String(skip));

  const res = await fetch(`${API_BASE}/api/data-catalog?${params}`);
  if (!res.ok) throw new Error("Failed to search data catalog");
  return res.json();
}

export async function fetchCatalogDomains(): Promise<{ domains: string[]; configured: boolean }> {
  const res = await fetch(`${API_BASE}/api/data-catalog/domains`);
  if (!res.ok) throw new Error("Failed to fetch catalog domains");
  return res.json();
}

/* ------------------------------------------------------------------ */
/* Skills (agent capability discovery)                                 */
/* ------------------------------------------------------------------ */

export interface SkillInfo {
  name: string;
  description: string;
  domain: string;
}

export interface SkillsResponse {
  skills: SkillInfo[];
}

let _skillsCache: SkillsResponse | null = null;
let _skillsCacheTime = 0;
const SKILLS_CACHE_TTL_MS = 60_000; // re-fetch after 1 minute

export async function fetchSkills(): Promise<SkillsResponse> {
  if (_skillsCache && Date.now() - _skillsCacheTime < SKILLS_CACHE_TTL_MS) {
    return _skillsCache;
  }
  const res = await fetch(`${API_BASE}/api/skills`);
  if (!res.ok) throw new Error("Failed to fetch skills");
  const data: SkillsResponse = await res.json();
  _skillsCache = data;
  _skillsCacheTime = Date.now();
  return data;
}

/* ------------------------------------------------------------------ */
/* Tools (domain tools from MCP servers)                               */
/* ------------------------------------------------------------------ */

export interface DomainToolInfo {
  name: string;
  description: string;
  server: string;
}

export interface ToolsResponse {
  tools: DomainToolInfo[];
}

let _toolsCache: ToolsResponse | null = null;
let _toolsCacheTime = 0;

export async function fetchTools(): Promise<ToolsResponse> {
  if (_toolsCache && Date.now() - _toolsCacheTime < SKILLS_CACHE_TTL_MS) {
    return _toolsCache;
  }
  const res = await fetch(`${API_BASE}/api/tools`);
  if (!res.ok) throw new Error("Failed to fetch tools");
  const data: ToolsResponse = await res.json();
  _toolsCache = data;
  _toolsCacheTime = Date.now();
  return data;
}
