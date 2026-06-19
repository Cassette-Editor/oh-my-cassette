import type { Asset, ChatEvent, Job, Lang } from "./types";

export function getApiKey(): string {
  return sessionStorage.getItem("omc_deepseek_key") || "";
}

function authHeaders(json: boolean): Record<string, string> {
  const headers: Record<string, string> = {};
  if (json) headers["Content-Type"] = "application/json";
  const key = getApiKey();
  if (key) headers["X-DeepSeek-Api-Key"] = key;
  return headers;
}

export interface SessionResponse {
  session_id: string;
  language: Lang;
}

export async function createSession(cleanupSessionId: string, language: Lang): Promise<SessionResponse> {
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cleanup_session_id: cleanupSessionId, language }),
  });
  if (!response.ok) throw new Error(`session request failed: ${response.status}`);
  return response.json();
}

export async function fetchEvents(sessionId: string, after: number): Promise<ChatEvent[] | null> {
  const response = await fetch(`/api/events?session_id=${encodeURIComponent(sessionId)}&after=${after}`);
  if (!response.ok) return null;
  const payload = await response.json();
  return (payload.events || []) as ChatEvent[];
}

export async function fetchAssets(sessionId: string): Promise<Asset[] | null> {
  const response = await fetch(`/api/assets?session_id=${encodeURIComponent(sessionId)}`);
  if (!response.ok) return null;
  const payload = await response.json();
  return (((payload.data || {}).manifest || {}).assets || []) as Asset[];
}

export async function fetchJobs(sessionId: string): Promise<Job[] | null> {
  const response = await fetch(`/api/jobs?session_id=${encodeURIComponent(sessionId)}&limit=8`);
  if (!response.ok) return null;
  const payload = await response.json();
  return ((payload.data || {}).jobs || []) as Job[];
}

export interface MutationResult {
  ok: boolean;
  detail?: string;
}

export async function postMessage(sessionId: string, text: string, language: Lang): Promise<MutationResult> {
  const response = await fetch("/api/messages", {
    method: "POST",
    headers: authHeaders(true),
    body: JSON.stringify({ session_id: sessionId, text, language }),
  });
  if (response.ok) return { ok: true };
  return { ok: false, detail: await response.text() };
}

export async function uploadFiles(sessionId: string, files: File[]): Promise<MutationResult> {
  const form = new FormData();
  form.append("session_id", sessionId);
  for (const file of files) form.append("files", file);
  const response = await fetch("/api/uploads", { method: "POST", body: form });
  if (response.ok) return { ok: true };
  return { ok: false, detail: await response.text() };
}

export async function setServerLanguage(sessionId: string, language: Lang): Promise<void> {
  await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/language`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language }),
  });
}

export function cleanupSession(sessionId: string): void {
  const url = `/api/sessions/${encodeURIComponent(sessionId)}/cleanup`;
  if (navigator.sendBeacon) {
    navigator.sendBeacon(url, new Blob(["{}"], { type: "application/json" }));
    return;
  }
  void fetch(url, { method: "POST", keepalive: true }).catch(() => {});
}
