import { useState, useRef, useEffect, useCallback, type FormEvent } from "react";
import Markdown from "react-markdown";
import { sendMessageStreaming, fetchSkills, fetchTools, type ToolCallArgs, type SkillInfo, type DomainToolInfo } from "../api";
import type { MapState, MapAnnotation, StoryMap } from "../types";

export interface Message {
  role: "user" | "assistant";
  content: string;
  /** Tool calls that happened before this response. */
  toolCalls?: ToolCallRecord[];
}

/** A completed tool call with its result. */
interface ToolCallRecord {
  callId: string;
  name: string;
  args?: ToolCallArgs;
  success: boolean;
  result?: string;
  error?: string;
  expanded: boolean;
}

/** Inline tool-call indicator shown while agent is working. */
interface ToolIndicator {
  callId: string;
  name: string;
  status: "running" | "success" | "error";
  args?: ToolCallArgs;
  result?: string;
  error?: string;
  expanded?: boolean;
}

/** Format the display result based on tool name. */
function formatToolResult(name: string, raw: string | undefined): string {
  if (!raw) return "";

  // Data lake search — show only name + description per result
  if (name === "search_data_lake_catalog") {
    try {
      let obj = JSON.parse(raw);
      // Unwrap if double-stringified
      if (typeof obj === "string") obj = JSON.parse(obj);
      // Find the array: could be top-level, or nested under results/artifacts
      const results = Array.isArray(obj) ? obj : (obj?.results ?? obj?.artifacts ?? null);
      if (Array.isArray(results)) {
        if (results.length === 0) return "(no results)";
        return results
          .map((r: Record<string, unknown>) => {
            const n = r.name ?? r.artifact_name ?? "";
            const desc = r.semantic_dataset_description ?? r.description ?? "";
            return `${n}${desc ? ` — ${desc}` : ""}`;
          })
          .join("\n");
      }
    } catch { /* fall through */ }
  }

  // Default: return raw (truncated by backend already)
  return raw;
}

/** Extract a short intent description from tool call arguments. */
function describeToolIntent(name: string, args?: ToolCallArgs): string | null {
  if (!args) return null;

  // search_tools / search_data_lake_catalog — show the query
  const query = args.query as string | undefined;
  if (query) return `"${query}"`;

  // execute_*_code — extract "# Purpose: ..." from code, with fallback to first comment line
  if (name.startsWith("execute_") && name.endsWith("_code")) {
    const code = args.code as string | undefined;
    if (code) {
      const purposeMatch = code.match(/^#\s*Purpose:\s*(.+)/mi);
      if (purposeMatch) return purposeMatch[1].trim();
      // Fallback: first comment line or first non-blank line (truncated)
      const firstComment = code.match(/^#\s*(.+)/m);
      if (firstComment) return firstComment[1].trim().slice(0, 80);
      const firstLine = code.split("\n").map((l) => l.trim()).find((l) => l);
      if (firstLine) return firstLine.slice(0, 80);
    }
  }

  return null;
}

interface Props {
  onMapStateUpdate: (state: MapState | null) => void;
  onCaptureRequest: (requestId: string, center: [number, number], zoom: number) => void;
  onStoryMap: (storyMap: StoryMap) => void;
  messages: Message[];
  setMessages: (updater: Message[] | ((prev: Message[]) => Message[])) => void;
  readOnly?: boolean;
  style?: React.CSSProperties;
  annotations?: MapAnnotation[];
  viewportRef?: React.RefObject<{ center: [number, number]; zoom: number }>;
  /** Text to insert into the chat input (e.g. an asset tag from the data catalog). */
  pendingInsert?: string | null;
  /** Called after the pending insert has been consumed. */
  onInsertConsumed?: () => void;
}

export default function ChatPanel({ onMapStateUpdate, onCaptureRequest, onStoryMap, messages, setMessages, readOnly, style, annotations, viewportRef, pendingInsert, onInsertConsumed }: Props) {
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [toolIndicators, setToolIndicators] = useState<ToolIndicator[]>([]);

  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Keep a mutable ref for tool accumulation (used when building the final message)
  const toolAccumRef = useRef<ToolIndicator[]>([]);

  // --- Slash-command dropdown (e.g. /skills) ---
  const [slashSkills, setSlashSkills] = useState<SkillInfo[] | null>(null);
  const [slashLoading, setSlashLoading] = useState(false);

  const showSlashSkills = input.trimEnd().endsWith("/skills");

  useEffect(() => {
    if (showSlashSkills) {
      if (slashSkills !== null || slashLoading) return;
      setSlashLoading(true);
      fetchSkills()
        .then((res) => setSlashSkills(res.skills))
        .catch(() => setSlashSkills([]))
        .finally(() => setSlashLoading(false));
    } else {
      if (slashSkills !== null) setSlashSkills(null);
    }
  }, [showSlashSkills, slashSkills, slashLoading]);

  // --- /tools dropdown ---
  const [slashTools, setSlashTools] = useState<DomainToolInfo[] | null>(null);
  const [slashToolsLoading, setSlashToolsLoading] = useState(false);

  const showSlashTools = input.trimEnd().endsWith("/tools") && !showSlashSkills;

  useEffect(() => {
    if (showSlashTools) {
      if (slashTools !== null || slashToolsLoading) return;
      setSlashToolsLoading(true);
      fetchTools()
        .then((res) => setSlashTools(res.tools))
        .catch(() => setSlashTools([]))
        .finally(() => setSlashToolsLoading(false));
    } else {
      if (slashTools !== null) setSlashTools(null);
    }
  }, [showSlashTools, slashTools, slashToolsLoading]);

  // Handle text insertion from external sources (e.g. data catalog)
  useEffect(() => {
    if (pendingInsert) {
      setInput((prev) => {
        const needsSpace = prev.length > 0 && !prev.endsWith(" ");
        return prev + (needsSpace ? " " : "") + pendingInsert + " ";
      });
      onInsertConsumed?.();
      // Focus the textarea so the user can keep typing
      setTimeout(() => textareaRef.current?.focus(), 0);
    }
  }, [pendingInsert, onInsertConsumed]);

  useEffect(() => {
    if (loading) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, toolIndicators, loading]);

  const autoResize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  }, []);

  useEffect(() => {
    autoResize();
  }, [input, autoResize]);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const raw = input.trim();
    if (!raw || loading || readOnly) return;

    // Strip trailing slash-command triggers before sending
    const text = raw.replace(/\s*\/(skills|tools)\s*$/, "").trim();

    // Nothing left after stripping — just the dropdown trigger
    if (!text) return;

    setInput("");
    setSlashSkills(null);
    setSlashTools(null);
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setLoading(true);
    setToolIndicators([]);
    toolAccumRef.current = [];

    try {
      await sendMessageStreaming(text, {
        onToolCall(callId, name, args) {
          const ti = { callId, name, status: "running" as const, args };
          toolAccumRef.current = [...toolAccumRef.current, ti];
          setToolIndicators([...toolAccumRef.current]);
        },
        onToolResult(callId, _name, success, result, error, args) {
          toolAccumRef.current = toolAccumRef.current.map((t) =>
            t.callId === callId ? { ...t, status: success ? "success" as const : "error" as const, result, error, args: args ?? t.args } : t
          );
          setToolIndicators([...toolAccumRef.current]);
        },
        onResponse(responseText) {
          if (responseText.trim()) {
            // Commit tool calls into the message
            const records: ToolCallRecord[] = toolAccumRef.current.map((t) => ({
              callId: t.callId,
              name: t.name,
              args: t.args,
              success: t.status === "success",
              result: t.result,
              error: t.error,
              expanded: false,
            }));
            setMessages((prev) => [
              ...prev,
              {
                role: "assistant",
                content: responseText,
                toolCalls: records.length > 0 ? records : undefined,
              },
            ]);
            setToolIndicators([]);
            toolAccumRef.current = [];
          }
        },
        onMapState(state) {
          onMapStateUpdate(state);
        },
        onCaptureRequest(requestId, center, zoom) {
          onCaptureRequest(requestId, center, zoom);
        },
        onStoryMap(storyMap) {
          onStoryMap(storyMap);
        },
        onError(msg) {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: `**Error:** ${msg}` },
          ]);
        },
        onDone() {
          setToolIndicators([]);
          toolAccumRef.current = [];
          setLoading(false);
        },
      }, annotations, viewportRef?.current ?? undefined);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `**Error:** ${msg}` },
      ]);
      setToolIndicators([]);
      toolAccumRef.current = [];
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  /** Toggle a tool call's expanded state within a committed message. */
  const toggleToolExpanded = (msgIndex: number, tcIndex: number) => {
    setMessages((prev) =>
      prev.map((m, i) => {
        if (i !== msgIndex || !m.toolCalls) return m;
        return {
          ...m,
          toolCalls: m.toolCalls.map((tc, j) =>
            j === tcIndex ? { ...tc, expanded: !tc.expanded } : tc,
          ),
        };
      }),
    );
  };

  /** Render a tool indicator row (used for both live and committed states). */
  const renderToolRow = (
    tc: { name: string; status?: string; success?: boolean; args?: ToolCallArgs; result?: string; error?: string; expanded?: boolean },
    key: string | number,
    onToggle?: () => void,
  ) => {
    const isSuccess = tc.success ?? (tc.status === "success");
    const isRunning = tc.status === "running";
    const intent = describeToolIntent(tc.name, tc.args);
    const displayResult = tc.result ? formatToolResult(tc.name, tc.result) : "";
    const hasDetail = !!(displayResult || tc.error);
    const statusClass = isRunning ? "running" : isSuccess ? "success" : "error";
    const showDetail = !!tc.expanded;

    return (
      <div key={key} className={`tool-indicator tool-indicator--${statusClass}`}>
        <div
          className="tool-indicator-header"
          onClick={() => hasDetail && onToggle?.()}
          style={{ cursor: hasDetail && onToggle ? "pointer" : "default" }}
        >
          <span className="tool-indicator-icon">
            {isRunning ? "\u23f3" : isSuccess ? "\u2705" : "\u274c"}
          </span>
          <span className="tool-indicator-name">{tc.name}</span>
          {intent && (
            <span className="tool-indicator-intent"> — {intent}</span>
          )}
          {hasDetail && onToggle && (
            <span className="tool-indicator-chevron">{tc.expanded ? "\u25be" : "\u25b8"}</span>
          )}
        </div>
        {showDetail && displayResult && (
          <pre className="tool-indicator-result"><code>{displayResult}</code></pre>
        )}
        {showDetail && tc.error && (
          <pre className="tool-indicator-error"><code>{tc.error}</code></pre>
        )}
      </div>
    );
  };

  return (
    <div className="chat-panel" style={style}>
      <div className="chat-messages">
        {messages.length === 0 && !loading && (
          <div className="chat-empty">
            <div className="chat-empty-icon">
              <svg viewBox="0 0 24 24" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="9" />
                <circle cx="12" cy="12" r="3" />
                <line x1="12" y1="2" x2="12" y2="6" />
                <line x1="12" y1="18" x2="12" y2="22" />
                <line x1="2" y1="12" x2="6" y2="12" />
                <line x1="18" y1="12" x2="22" y2="12" />
              </svg>
            </div>
            <div className="chat-empty-title">Spatial Intelligence</div>
            <div className="chat-empty-subtitle">
              Analyze geospatial data, generate map layers, and query geographic datasets.
            </div>
          </div>
        )}
        {messages.map((m, msgIdx) => (
          <div key={msgIdx} className={`chat-message chat-message--${m.role}`}>
            <div className="chat-bubble">
              {/* Committed tool calls shown above response */}
              {m.toolCalls?.map((tc, tcIdx) =>
                renderToolRow(tc, tc.callId || tcIdx, () => toggleToolExpanded(msgIdx, tcIdx))
              )}
              {m.role === "assistant" ? (
                <Markdown>{m.content}</Markdown>
              ) : (
                m.content
              )}
            </div>
          </div>
        ))}

        {/* Live tool indicators — shown in real-time while agent is working */}
        {loading && toolIndicators.length > 0 && (
          <div className="chat-message chat-message--assistant">
            <div className="chat-bubble">
              {toolIndicators.map((t, idx) =>
                renderToolRow(
                  t,
                  t.callId,
                  () => {
                    const updated = [...toolAccumRef.current];
                    if (updated[idx]) {
                      updated[idx] = { ...updated[idx], expanded: !updated[idx].expanded };
                      toolAccumRef.current = updated;
                      setToolIndicators([...updated]);
                    }
                  },
                )
              )}
            </div>
          </div>
        )}

        {/* Thinking — shown before first tool call */}
        {loading && toolIndicators.length === 0 && (
          <div className="chat-message chat-message--assistant">
            <div className="chat-bubble chat-bubble--loading">
              Thinking
              <span className="typing-dots">
                <span />
                <span />
                <span />
              </span>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
      <div className="chat-input-area">
        {readOnly && (
          <div className="chat-readonly-banner">Viewing saved session</div>
        )}
        {/* Slash-command dropdown */}
        {showSlashSkills && (slashSkills || slashLoading) && (
          <div className="slash-dropdown">
            <div className="slash-dropdown-header">
              <div className="slash-dropdown-header-title">
                <svg viewBox="0 0 24 24" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
                </svg>
                Active Skills
              </div>
              <button
                className="slash-dropdown-close"
                onClick={() => {
                  setInput((prev) => prev.replace(/\s*\/skills\s*$/, ""));
                  setSlashSkills(null);
                  setTimeout(() => textareaRef.current?.focus(), 0);
                }}
                aria-label="Close"
              >×</button>
            </div>
            {slashLoading ? (
              <div className="slash-dropdown-loading">Loading skills…</div>
            ) : (() => {
              const grouped: Record<string, SkillInfo[]> = {};
              for (const s of slashSkills ?? []) {
                (grouped[s.domain] ??= []).push(s);
              }
              const domains = Object.keys(grouped).sort();
              return domains.length === 0 ? (
                <div className="slash-dropdown-empty">No active skills — start an MCP server first</div>
              ) : (
                domains.map((domain) => (
                  <div key={domain} className="slash-dropdown-group">
                    <div className="slash-dropdown-domain">
                      {domain.replace(/[_-]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
                    </div>
                    {grouped[domain].map((s) => {
                      const alreadySelected = input.includes(`[skill: ${s.name}]`);
                      return (
                        <button
                          type="button"
                          key={s.name}
                          aria-label={alreadySelected ? `Deselect ${s.name} skill` : `Select ${s.name} skill`}
                          aria-pressed={alreadySelected}
                          className={`slash-dropdown-item${alreadySelected ? " slash-dropdown-item--selected" : ""}`}
                          onClick={() => {
                            if (alreadySelected) {
                              // Deselect: remove the tag
                              setInput((prev) => prev.replace(`[skill: ${s.name}] `, ""));
                            } else {
                              // Insert tag before /skills so dropdown stays open
                              setInput((prev) => prev.replace(/\/skills\s*$/, `[skill: ${s.name}] /skills`));
                            }
                            setTimeout(() => textareaRef.current?.focus(), 0);
                          }}
                        >
                          <div className="slash-dropdown-name">
                            {alreadySelected && <span className="slash-dropdown-check">✓ </span>}
                            {s.name}
                          </div>
                          <span className="slash-dropdown-desc">{s.description}</span>
                        </button>
                      );
                    })}
                  </div>
                ))
              );
            })()}
          </div>
        )}
        {/* /tools dropdown */}
        {showSlashTools && (slashTools || slashToolsLoading) && (
          <div className="slash-dropdown">
            <div className="slash-dropdown-header">
              <div className="slash-dropdown-header-title">
                <svg viewBox="0 0 24 24" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none" stroke="currentColor">
                  <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
                </svg>
                Domain Tools
              </div>
              <button
                className="slash-dropdown-close"
                onClick={() => {
                  setInput((prev) => prev.replace(/\s*\/tools\s*$/, ""));
                  setSlashTools(null);
                  setTimeout(() => textareaRef.current?.focus(), 0);
                }}
                aria-label="Close"
              >×</button>
            </div>
            {slashToolsLoading ? (
              <div className="slash-dropdown-loading">Loading tools…</div>
            ) : (() => {
              const grouped: Record<string, DomainToolInfo[]> = {};
              for (const t of slashTools ?? []) {
                (grouped[t.server] ??= []).push(t);
              }
              const servers = Object.keys(grouped).sort();
              return servers.length === 0 ? (
                <div className="slash-dropdown-empty">No domain tools available</div>
              ) : (
                servers.map((server) => (
                  <div key={server} className="slash-dropdown-group">
                    <div className="slash-dropdown-domain">
                      {server.replace(/[_-]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
                    </div>
                    {grouped[server].map((t) => {
                      const alreadySelected = input.includes(`[tool: ${t.name}]`);
                      return (
                        <button
                          type="button"
                          key={t.name}
                          aria-label={alreadySelected ? `Deselect ${t.name} tool` : `Select ${t.name} tool`}
                          aria-pressed={alreadySelected}
                          className={`slash-dropdown-item${alreadySelected ? " slash-dropdown-item--selected" : ""}`}
                          onClick={() => {
                            if (alreadySelected) {
                              setInput((prev) => prev.replace(`[tool: ${t.name}] `, ""));
                            } else {
                              setInput((prev) => prev.replace(/\/tools\s*$/, `[tool: ${t.name}] /tools`));
                            }
                            setTimeout(() => textareaRef.current?.focus(), 0);
                          }}
                        >
                          <div className="slash-dropdown-name">
                            {alreadySelected && <span className="slash-dropdown-check">✓ </span>}
                            {t.name}
                          </div>
                          <span className="slash-dropdown-desc">{t.description}</span>
                        </button>
                      );
                    })}
                  </div>
                ))
              );
            })()}
          </div>
        )}
        <form className="chat-input-form" onSubmit={handleSubmit}>
          <textarea
            ref={textareaRef}
            className="chat-input"
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={readOnly ? "Switch to the live session to chat..." : "Ask about spatial data..."}
            disabled={loading || readOnly}
          />
          <button className="chat-send-btn" type="submit" disabled={loading || readOnly || !input.trim()}>
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
              <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
            </svg>
          </button>
        </form>
      </div>
    </div>
  );
}
