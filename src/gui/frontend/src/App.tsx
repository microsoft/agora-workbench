import { useState, useEffect, useCallback, useRef } from "react";
import ChatPanel, { type Message } from "./components/ChatPanel";
import MapViewer from "./components/MapViewer";
import SessionSidebar from "./components/SessionSidebar";
import ExperiencePanel from "./components/ExperiencePanel";
import DataCatalogPanel from "./components/DataCatalogPanel";
import StoryMapViewer from "./components/StoryMapViewer";
import { fetchMapState, resetSession, postMapCapture, API_BASE } from "./api";
import type { MapState, MapLayer, MapAnnotation, DrawingMode, StoryMap } from "./types";
import {
  createSession,
  listSessions,
  getSession,
  saveSession,
  deleteSession,
  type Session,
} from "./sessions";
import "./App.css";

/** Maximum time (ms) to wait for a single map capture before advancing the queue. */
const CAPTURE_TIMEOUT_MS = 30_000;

export default function App() {
  // --- Session state ---
  const [activeSession, setActiveSession] = useState<Session>(() => {
    const existing = listSessions();
    return existing.length > 0 ? existing[0] : createSession();
  });
  const [sessions, setSessions] = useState<Session[]>(listSessions);
  const [viewingId, setViewingId] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  // The messages/map the user currently sees — either active or a viewed snapshot
  const viewedSession = viewingId ? getSession(viewingId) : null;
  const displayMessages = viewedSession?.messages ?? activeSession.messages;
  const displayMapState = viewedSession?.mapState ?? activeSession.mapState;
  const isViewingOld = viewingId != null && viewingId !== activeSession.id;

  const [chatWidth, setChatWidth] = useState(400);
  const [basemap, setBasemap] = useState("street");
  const dragging = useRef(false);

  // --- Experience panel state ---
  const [experienceOpen, setExperienceOpen] = useState(false);

  // --- Data catalog panel state ---
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [pendingInsert, setPendingInsert] = useState<string | null>(null);

  const handleCatalogInsert = useCallback((assetTag: string, _name: string) => {
    setPendingInsert(assetTag);
  }, []);

  const handleInsertConsumed = useCallback(() => {
    setPendingInsert(null);
  }, []);

  // --- Story map state ---
  const [activeStoryMap, setActiveStoryMap] = useState<StoryMap | null>(null);

  // Story map navigation: fly the map to a step's location
  const handleStoryMapNavigate = useCallback((center: [number, number], zoom: number) => {
    // Update the map state view to trigger a fly-to
    setActiveSession((prev) => ({
      ...prev,
      mapState: prev.mapState
        ? { ...prev.mapState, view: { center, zoom } }
        : { view: { center, zoom }, layers: [] },
    }));
  }, []);

  // Story map highlight: dim non-highlighted layers
  const storyLayerStateRef = useRef<Record<string, { opacity: MapLayer["opacity"]; style: MapLayer["style"] }>>({});
  const handleStoryMapHighlight = useCallback((layerIds: string[]) => {
    setActiveSession((prev) => ({
      ...prev,
      mapState: prev.mapState
        ? {
            ...prev.mapState,
            layers: (() => {
              if (layerIds.length === 0) {
                const original = storyLayerStateRef.current;
                storyLayerStateRef.current = {};
                return prev.mapState.layers.map((l) => ({
                  ...l,
                  opacity: original[l.id]?.opacity ?? l.opacity,
                  style: original[l.id]?.style ?? l.style,
                }));
              }

              if (Object.keys(storyLayerStateRef.current).length === 0) {
                storyLayerStateRef.current = Object.fromEntries(
                  prev.mapState.layers.map((l) => [l.id, { opacity: l.opacity, style: l.style }]),
                );
              }

              return prev.mapState.layers.map((l) => {
                const base = storyLayerStateRef.current[l.id] ?? { opacity: l.opacity, style: l.style };
                const highlighted = layerIds.includes(l.id);
                return {
                  ...l,
                  opacity: highlighted ? base.opacity : 0.2,
                  style: highlighted ? base.style : { ...(base.style ?? {}), opacity: 0.2, fillOpacity: 0.1 },
                };
              });
            })(),
          }
        : prev.mapState,
    }));
  }, []);

  const handleStoryMap = useCallback((storyMap: StoryMap) => {
    handleStoryMapHighlight([]);
    setActiveStoryMap(storyMap);
  }, [handleStoryMapHighlight]);

  useEffect(() => {
    if (isViewingOld && activeStoryMap) {
      handleStoryMapHighlight([]);
      setActiveStoryMap(null);
    }
  }, [isViewingOld, activeStoryMap, handleStoryMapHighlight]);

  // --- Annotation state ---
  const [annotations, setAnnotations] = useState<MapAnnotation[]>([]);
  const [drawingMode, setDrawingMode] = useState<DrawingMode>(null);
  const pinCounter = useRef(0);
  const polyCounter = useRef(0);

  // --- Live viewport state (what the user currently sees) ---
  const viewportRef = useRef<{ center: [number, number]; zoom: number }>({
    center: [39.8283, -98.5795],
    zoom: 5,
  });
  const handleViewportChange = useCallback((center: [number, number], zoom: number) => {
    viewportRef.current = { center, zoom };
  }, []);

  // --- Map capture queue (for agent VLM) ---
  const captureQueueRef = useRef<{ requestId: string; center: [number, number]; zoom: number }[]>([]);
  const [captureRequest, setCaptureRequest] = useState<{
    requestId: string;
    center: [number, number];
    zoom: number;
  } | null>(null);
  // Timeout ref so we can clear it when a capture completes normally
  const captureTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const dequeueCapture = useCallback(() => {
    if (captureTimeoutRef.current !== null) {
      clearTimeout(captureTimeoutRef.current);
      captureTimeoutRef.current = null;
    }
    const next = captureQueueRef.current.shift();
    setCaptureRequest(next ?? null);
  }, []);

  const handleCaptureRequest = useCallback((requestId: string, center: [number, number], zoom: number) => {
    captureQueueRef.current.push({ requestId, center, zoom });
    // If nothing is currently being processed, start immediately
    setCaptureRequest((current) => {
      if (current === null) {
        return captureQueueRef.current.shift() ?? null;
      }
      return current;
    });
  }, []);

  // Start a watchdog timer whenever a capture request becomes active so that
  // a failed/stalled capture (one that never calls onCaptureComplete) doesn't
  // block the queue indefinitely.
  useEffect(() => {
    if (!captureRequest) return;
    captureTimeoutRef.current = setTimeout(() => {
      console.warn("Map capture timed out for request", captureRequest.requestId, "— advancing queue");
      captureTimeoutRef.current = null;
      const next = captureQueueRef.current.shift();
      setCaptureRequest(next ?? null);
    }, CAPTURE_TIMEOUT_MS);
    return () => {
      if (captureTimeoutRef.current !== null) {
        clearTimeout(captureTimeoutRef.current);
        captureTimeoutRef.current = null;
      }
    };
  }, [captureRequest]);

  const handleCaptureComplete = useCallback(async (requestId: string, pngBlob: Blob) => {
    try {
      await postMapCapture(requestId, pngBlob);
    } catch (err) {
      console.error("Failed to post map capture:", err);
    }
    // Process next capture in queue
    dequeueCapture();
  }, [dequeueCapture]);

  const addAnnotation = useCallback((type: "pin" | "polygon", geometry: GeoJSON.Point | GeoJSON.Polygon) => {
    const label = type === "pin"
      ? `Pin ${String.fromCharCode(65 + (pinCounter.current++ % 26))}`
      : `Polygon ${++polyCounter.current}`;
    const annotation: MapAnnotation = {
      id: crypto.randomUUID(),
      label,
      type,
      geometry,
    };
    setAnnotations((prev) => [...prev, annotation]);
    setDrawingMode(null);
  }, []);

  const removeAnnotation = useCallback((id: string) => {
    setAnnotations((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const clearAnnotations = useCallback(() => {
    setAnnotations([]);
    pinCounter.current = 0;
    polyCounter.current = 0;
  }, []);

  // --- Persist active session whenever it changes ---
  const persistActive = useCallback(() => {
    saveSession(activeSession);
    setSessions(listSessions());
  }, [activeSession]);

  useEffect(() => {
    persistActive();
  }, [persistActive]);

  const onMouseDown = useCallback(() => {
    dragging.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      const w = Math.max(280, Math.min(e.clientX, window.innerWidth - 300));
      setChatWidth(w);
    };
    const onMouseUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, []);

  const loadMapState = useCallback(async () => {
    try {
      const state = await fetchMapState();
      setActiveSession((prev) => ({ ...prev, mapState: state }));
    } catch {
      // server may not be up yet
    }
  }, []);

  useEffect(() => {
    loadMapState();
  }, [loadMapState]);

  const handleMapStateUpdate = (state: MapState | null) => {
    if (state) {
      setActiveSession((prev) => ({ ...prev, mapState: state }));
    }
  };

  const setActiveMessages = useCallback(
    (updater: Message[] | ((prev: Message[]) => Message[])) => {
      setActiveSession((prev) => ({
        ...prev,
        messages: typeof updater === "function" ? updater(prev.messages) : updater,
      }));
    },
    [],
  );

  // --- New session (reset agent + create fresh session) ---
  const handleNewSession = () => {
    // Fire backend reset in background — don't block on it
    resetSession().catch(() => {});
    const fresh = createSession();
    saveSession(fresh);
    setActiveSession(fresh);
    setViewingId(null);
    setSessions(listSessions());
    clearAnnotations();
    storyLayerStateRef.current = {};
    setActiveStoryMap(null);
  };

  // --- Select a session from sidebar ---
  const handleSelectSession = (id: string) => {
    if (id === activeSession.id) {
      setViewingId(null); // back to live
    } else {
      setViewingId(id);
    }
  };

  // --- Delete an old session ---
  const handleDeleteSession = (id: string) => {
    deleteSession(id);
    if (viewingId === id) setViewingId(null);
    setSessions(listSessions());
  };

  // --- "Back to live" shortcut ---
  const handleBackToLive = () => setViewingId(null);

  const hasLayers = (displayMapState?.layers?.length ?? 0) > 0;

  const handleSaveMap = () => {
    window.open(`${API_BASE}/api/export-map?basemap=${basemap}`, "_blank");
  };

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-brand">
          <div className="header-logo">
            <svg viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="9" />
              <circle cx="12" cy="12" r="3" />
              <line x1="12" y1="2" x2="12" y2="6" />
              <line x1="12" y1="18" x2="12" y2="22" />
              <line x1="2" y1="12" x2="6" y2="12" />
              <line x1="18" y1="12" x2="22" y2="12" />
            </svg>
          </div>
          <h1>Agora Infrastructure Agent</h1>
        </div>
        <div className="header-actions">
          {isViewingOld && (
            <button className="header-btn header-btn--accent" onClick={handleBackToLive}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="15 18 9 12 15 6" />
              </svg>
              Back to live
            </button>
          )}
          {hasLayers && !isViewingOld && (
            <button className="header-btn" onClick={handleSaveMap}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              Export
            </button>
          )}
          <button className="header-btn" onClick={handleNewSession}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="1 4 1 10 7 10" />
              <path d="M3.51 15a9 9 0 102.13-9.36L1 10" />
            </svg>
            New Session
          </button>
          <button className="header-btn" onClick={() => setExperienceOpen(true)} title="Persistent preferences and lessons">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M2 3h6a4 4 0 014 4v14a3 3 0 00-3-3H2z" />
              <path d="M22 3h-6a4 4 0 00-4 4v14a3 3 0 013-3h7z" />
            </svg>
            Experience
          </button>
          <button className="header-btn" onClick={() => setCatalogOpen(true)} title="Browse available datasets">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <ellipse cx="12" cy="5" rx="9" ry="3" />
              <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
              <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
            </svg>
            Data
          </button>
        </div>
      </header>
      <main className="app-main">
        <SessionSidebar
          sessions={sessions}
          activeId={activeSession.id}
          viewingId={viewingId}
          onSelect={handleSelectSession}
          onDelete={handleDeleteSession}
          onNew={handleNewSession}
          collapsed={sidebarCollapsed}
          onToggle={() => setSidebarCollapsed((v) => !v)}
        />
        <ChatPanel
          onMapStateUpdate={handleMapStateUpdate}
          onCaptureRequest={handleCaptureRequest}
          onStoryMap={handleStoryMap}
          messages={displayMessages}
          setMessages={setActiveMessages}
          readOnly={isViewingOld}
          style={{ width: chatWidth }}
          annotations={annotations}
          viewportRef={viewportRef}
          pendingInsert={pendingInsert}
          onInsertConsumed={handleInsertConsumed}
        />
        <div className="resize-handle" onMouseDown={onMouseDown} />
        <MapViewer
          mapState={displayMapState}
          basemap={basemap}
          onBasemapChange={setBasemap}
          annotations={annotations}
          drawingMode={drawingMode}
          onDrawingModeChange={setDrawingMode}
          onAddAnnotation={addAnnotation}
          onRemoveAnnotation={removeAnnotation}
          onClearAnnotations={clearAnnotations}
          captureRequest={captureRequest}
          onCaptureComplete={handleCaptureComplete}
          onViewportChange={handleViewportChange}
        />
        {activeStoryMap && (
          <StoryMapViewer
            storyMap={activeStoryMap}
            onNavigate={handleStoryMapNavigate}
            onHighlight={handleStoryMapHighlight}
            onClose={() => setActiveStoryMap(null)}
          />
        )}
      </main>
      <ExperiencePanel
        messages={activeSession.messages}
        open={experienceOpen}
        onClose={() => setExperienceOpen(false)}
      />
      <DataCatalogPanel
        open={catalogOpen}
        onClose={() => setCatalogOpen(false)}
        onInsert={handleCatalogInsert}
      />
    </div>
  );
}
