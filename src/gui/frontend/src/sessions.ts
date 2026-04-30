import type { Message } from "./components/ChatPanel";
import type { MapState } from "./types";

const STORAGE_KEY = "agora_sessions";
const MAX_SESSIONS = 50;

export interface Session {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  messages: Message[];
  mapState: MapState | null;
}

function readAll(): Session[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function writeAll(sessions: Session[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions.slice(0, MAX_SESSIONS)));
}

/** Generate a short title from the first user message. */
function deriveTitle(messages: Message[]): string {
  const first = messages.find((m) => m.role === "user");
  if (!first) return "New session";
  const text = first.content.trim();
  return text.length > 60 ? text.slice(0, 57) + "..." : text;
}

export function createSession(): Session {
  return {
    id: crypto.randomUUID(),
    title: "New session",
    createdAt: Date.now(),
    updatedAt: Date.now(),
    messages: [],
    mapState: null,
  };
}

export function listSessions(): Session[] {
  return readAll().sort((a, b) => b.updatedAt - a.updatedAt);
}

export function getSession(id: string): Session | undefined {
  return readAll().find((s) => s.id === id);
}

export function saveSession(session: Session) {
  session.updatedAt = Date.now();
  session.title = deriveTitle(session.messages);
  const all = readAll().filter((s) => s.id !== session.id);
  all.unshift(session);
  writeAll(all);
}

export function deleteSession(id: string) {
  writeAll(readAll().filter((s) => s.id !== id));
}
