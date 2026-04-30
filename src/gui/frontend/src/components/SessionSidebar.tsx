import type { Session } from "../sessions";

interface Props {
  sessions: Session[];
  activeId: string;
  viewingId: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onNew: () => void;
  collapsed: boolean;
  onToggle: () => void;
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) {
    return "Yesterday";
  }
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export default function SessionSidebar({
  sessions,
  activeId,
  viewingId,
  onSelect,
  onDelete,
  onNew,
  collapsed,
  onToggle,
}: Props) {
  const selectedId = viewingId ?? activeId;

  return (
    <div className={`session-sidebar ${collapsed ? "session-sidebar--collapsed" : ""}`}>
      <div className="session-sidebar-header">
        <button className="session-sidebar-toggle" onClick={onToggle} title={collapsed ? "Expand" : "Collapse"}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            {collapsed ? (
              <>
                <line x1="3" y1="12" x2="21" y2="12" />
                <line x1="3" y1="6" x2="21" y2="6" />
                <line x1="3" y1="18" x2="21" y2="18" />
              </>
            ) : (
              <>
                <polyline points="11 17 6 12 11 7" />
                <line x1="6" y1="12" x2="20" y2="12" />
              </>
            )}
          </svg>
        </button>
        {!collapsed && (
          <button className="session-new-btn" onClick={onNew} title="New session">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
          </button>
        )}
      </div>

      {!collapsed && (
        <div className="session-list">
          {sessions.map((s) => (
            <div
              key={s.id}
              className={`session-item ${s.id === selectedId ? "session-item--active" : ""} ${s.id === activeId ? "session-item--current" : ""}`}
              onClick={() => onSelect(s.id)}
            >
              <div className="session-item-title">{s.title}</div>
              <div className="session-item-meta">
                <span>{s.messages.length} msgs</span>
                <span>{formatTime(s.updatedAt)}</span>
              </div>
              {s.id !== activeId && (
                <button
                  className="session-item-delete"
                  onClick={(e) => { e.stopPropagation(); onDelete(s.id); }}
                  title="Delete"
                >
                  &times;
                </button>
              )}
            </div>
          ))}
          {sessions.length === 0 && (
            <div className="session-list-empty">No sessions yet</div>
          )}
        </div>
      )}
    </div>
  );
}
