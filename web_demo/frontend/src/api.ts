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

export async function createSession(language: Lang): Promise<SessionResponse> {
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language }),
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

const UPLOAD_TIMEOUT_MS = 15 * 60 * 1000;

export async function postMessage(sessionId: string, text: string, language: Lang, clientEventId = ""): Promise<MutationResult> {
  const response = await fetch("/api/messages", {
    method: "POST",
    headers: authHeaders(true),
    body: JSON.stringify({ session_id: sessionId, text, language, client_event_id: clientEventId }),
  });
  if (response.ok) return { ok: true };
  return { ok: false, detail: await response.text() };
}

export async function uploadFiles(
  sessionId: string,
  files: File[],
  clientEventId = "",
  onProgress?: (percent: number) => void,
): Promise<MutationResult> {
  return new Promise((resolve) => {
    const form = new FormData();
    form.append("session_id", sessionId);
    if (clientEventId) form.append("client_event_id", clientEventId);
    for (const file of files) form.append("files", file);
    const request = new XMLHttpRequest();
    request.open("POST", "/api/uploads");
    request.timeout = UPLOAD_TIMEOUT_MS;
    request.upload.onprogress = (event) => {
      if (!event.lengthComputable || !onProgress) return;
      onProgress(Math.max(1, Math.min(99, Math.round((event.loaded / event.total) * 100))));
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) {
        onProgress?.(100);
        resolve({ ok: true });
        return;
      }
      resolve({ ok: false, detail: request.responseText || `upload failed: ${request.status}` });
    };
    request.onerror = () => resolve({ ok: false, detail: "upload network error" });
    request.onabort = () => resolve({ ok: false, detail: "upload aborted" });
    request.ontimeout = () => resolve({ ok: false, detail: `upload timed out after ${Math.round(UPLOAD_TIMEOUT_MS / 1000)} seconds` });
    request.send(form);
  });
}

export async function setServerLanguage(sessionId: string, language: Lang): Promise<void> {
  await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/language`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language }),
  });
}

export function cleanupSession(sessionId: string, reason = ""): void {
  const query = reason ? `?reason=${encodeURIComponent(reason)}` : "";
  const url = `/api/sessions/${encodeURIComponent(sessionId)}/cleanup${query}`;
  if (navigator.sendBeacon) {
    navigator.sendBeacon(url, new Blob(["{}"], { type: "application/json" }));
    return;
  }
  void fetch(url, { method: "POST", keepalive: true }).catch(() => {});
}
