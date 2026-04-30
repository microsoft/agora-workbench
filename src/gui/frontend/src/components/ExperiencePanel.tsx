import { useState, useEffect, useCallback, useRef } from "react";
import { fetchExperience, updateExperience, summarizeExperience } from "../api";
import type { Message } from "./ChatPanel";

interface Props {
  messages: Message[];
  open: boolean;
  onClose: () => void;
}

export default function ExperiencePanel({ messages, open, onClose }: Props) {
  const [content, setContent] = useState("");
  const [saved, setSaved] = useState("");
  const [loading, setLoading] = useState(false);
  const [summarizing, setSummarizing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const headingId = "experience-panel-title";
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  // Load experience when panel opens
  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    fetchExperience()
      .then((text) => {
        setContent(text);
        setSaved(text);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, [open]);

  // Focus close button when panel opens; restore focus on close
  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    closeButtonRef.current?.focus();
    return () => {
      previousFocus?.focus();
    };
  }, [open]);

  // ESC key closes the panel
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  // Clear success message after a delay
  useEffect(() => {
    if (!success) return;
    const timer = setTimeout(() => setSuccess(null), 3000);
    return () => clearTimeout(timer);
  }, [success]);

  const handleSave = useCallback(async () => {
    setError(null);
    setLoading(true);
    try {
      const result = await updateExperience(content);
      setSaved(result);
      setSuccess("Saved");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setLoading(false);
    }
  }, [content]);

  const handleSummarize = useCallback(async () => {
    if (messages.length === 0) {
      setError("No messages in current session to summarize");
      return;
    }
    setError(null);
    setSummarizing(true);
    try {
      const result = await summarizeExperience(
        messages.map((m) => ({ role: m.role, content: m.content })),
      );
      setContent(result);
      setSaved(result);
      setSuccess("Summarized and saved");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Summarize failed");
    } finally {
      setSummarizing(false);
    }
  }, [messages]);

  const isDirty = content !== saved;
  const isBusy = loading || summarizing;

  if (!open) return null;

  return (
    <div className="experience-overlay" onClick={onClose} aria-hidden="true">
      <div
        className="experience-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="experience-header">
          <h2 id={headingId}>Experience</h2>
          <div className="experience-header-actions">
            {success && <span className="experience-success">{success}</span>}
            {error && <span className="experience-error">{error}</span>}
            <button
              className="experience-btn experience-btn--summarize"
              onClick={handleSummarize}
              disabled={summarizing || messages.length === 0}
              title="Extract preferences and lessons from the current conversation"
            >
              {summarizing ? "Summarizing..." : "Learn from session"}
            </button>
            <button
              className="experience-btn experience-btn--save"
              onClick={handleSave}
              disabled={!isDirty || isBusy}
            >
              Save
            </button>
            <button
              ref={closeButtonRef}
              className="experience-close"
              onClick={onClose}
              title="Close"
              aria-label="Close experience panel"
            >
              ×
            </button>
          </div>
        </div>
        <p className="experience-description">
          Persistent preferences and lessons injected into every session. Edit directly or click
          &ldquo;Learn from session&rdquo; to auto-extract insights from the current conversation.
        </p>
        {loading && !summarizing ? (
          <div className="experience-loading">Loading...</div>
        ) : (
          <textarea
            className="experience-editor"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="No experience saved yet. Start chatting and click 'Learn from session', or type your preferences here."
            spellCheck={false}
          />
        )}
      </div>
    </div>
  );
}
