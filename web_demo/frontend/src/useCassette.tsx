import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as api from "./api";
import { initialLanguage, makeT, type Translate } from "./i18n";
import type { Asset, ChatEvent, Connection, Job, Lang, Message, UploadProgress } from "./types";

const POLL_BASE = 3000;
const POLL_MAX = 15000;

export interface CassetteApi {
  t: Translate;
  language: Lang;
  setLanguage: (lang: Lang) => void;
  connection: Connection;
  sessionId: string;
  messages: Message[];
  assets: Asset[] | null;
  jobs: Job[] | null;
  sending: boolean;
  uploadProgress: UploadProgress | null;
  send: (text: string) => Promise<void>;
  upload: (files: File[]) => Promise<void>;
  refreshNow: () => void;
  reconnect: () => void;
  dismiss: (id: string) => void;
  getApiKey: () => string;
  saveApiKey: (value: string) => void;
}

const Ctx = createContext<CassetteApi | null>(null);

export function useApp(): CassetteApi {
  const value = useContext(Ctx);
  if (!value) throw new Error("useApp must be used within <CassetteProvider>");
  return value;
}

export function CassetteProvider({ children }: { children: ReactNode }) {
  const value = useCassette();
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

function useCassette(): CassetteApi {
  const [language, setLanguageState] = useState<Lang>(initialLanguage);
  const [sessionId, setSessionId] = useState("");
  const [connection, setConnection] = useState<Connection>("connecting");
  const [messages, setMessages] = useState<Message[]>([]);
  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [sending, setSending] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);

  const lastEventId = useRef(0);
  const sessionRef = useRef("");
  const languageRef = useRef(language);
  const errorSeq = useRef(0);
  const localSeq = useRef(0);
  const booted = useRef(false);
  const cleanupSent = useRef(false);

  useEffect(() => {
    sessionRef.current = sessionId;
  }, [sessionId]);
  useEffect(() => {
    languageRef.current = language;
  }, [language]);

  const t = useMemo(() => makeT(language), [language]);

  const pushError = useCallback((text: string, retry?: () => void) => {
    const id = `local-${(errorSeq.current += 1)}`;
    setMessages((prev) => [...prev, { id, kind: "error", text, retry }]);
  }, []);

  const dismiss = useCallback((id: string) => {
    setMessages((prev) => prev.filter((message) => String(message.id) !== id));
  }, []);

  const ingestEvents = useCallback((events: ChatEvent[]) => {
    if (!events.length) return;
    for (const event of events) {
      lastEventId.current = Math.max(lastEventId.current, Number(event.id || 0));
    }
    setMessages((prev) => {
      const replacementClientIds = new Set(events.map((event) => event.client_event_id).filter(Boolean).map(String));
      const replaced = replacementClientIds.size
        ? prev.filter((message) => !replacementClientIds.has(String((message as { client_event_id?: string }).client_event_id || message.id)))
        : prev;
      const seen = new Set(replaced.map((message) => String(message.id)));
      const fresh = events.filter((event) => !seen.has(String(event.id)));
      return fresh.length || replaced.length !== prev.length ? [...replaced, ...fresh] : prev;
    });
  }, []);

  const refresh = useCallback(async () => {
    const sid = sessionRef.current;
    if (!sid) return;
    const [events, assetList, jobList] = await Promise.all([
      api.fetchEvents(sid, lastEventId.current),
      api.fetchAssets(sid),
      api.fetchJobs(sid),
    ]);
    if (events) ingestEvents(events);
    if (assetList) setAssets(assetList);
    if (jobList) setJobs(jobList);
  }, [ingestEvents]);

  const boot = useCallback(async () => {
    setConnection("connecting");
    try {
      const previous = localStorage.getItem("omc_web_session") || "";
      const result = await api.createSession(previous, languageRef.current);
      lastEventId.current = 0;
      setMessages([]);
      setSessionId(result.session_id);
      sessionRef.current = result.session_id;
      localStorage.setItem("omc_web_session", result.session_id);
      api.setServerLanguage(result.session_id, languageRef.current).catch(() => {});
      await refresh();
      setConnection("ok");
    } catch (error) {
      console.error("boot failed:", error);
      setConnection("error");
    }
  }, [refresh]);

  // Boot once on mount (StrictMode-safe).
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    void boot();
  }, [boot]);

  // Polling with exponential backoff; recovers the connection banner on success.
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    let timer = 0;
    let delay = POLL_BASE;
    const tick = async () => {
      let ok = true;
      try {
        await refresh();
      } catch (error) {
        ok = false;
        console.error("refresh failed:", error);
      }
      if (cancelled) return;
      if (ok) {
        delay = POLL_BASE;
        setConnection((current) => (current === "ok" ? current : "ok"));
      } else {
        delay = Math.min(Math.round(delay * 1.6), POLL_MAX);
        setConnection("error");
      }
      timer = window.setTimeout(tick, delay);
    };
    timer = window.setTimeout(tick, POLL_BASE);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [sessionId, refresh]);

  // Best-effort session cleanup when the tab goes away.
  useEffect(() => {
    const handler = () => {
      const sid = sessionRef.current;
      if (!sid || cleanupSent.current) return;
      cleanupSent.current = true;
      api.cleanupSession(sid);
    };
    window.addEventListener("pagehide", handler);
    return () => window.removeEventListener("pagehide", handler);
  }, []);

  // Language switch is a pure state change: relabel via re-render, sync to the
  // server in the background, never refetch. No flash, no DOM teardown.
  const setLanguage = useCallback((lang: Lang) => {
    setLanguageState(lang);
    localStorage.setItem("omc_web_language", lang);
    if (sessionRef.current) api.setServerLanguage(sessionRef.current, lang).catch(() => {});
  }, []);

  const send = useCallback(
    async (text: string) => {
      const sid = sessionRef.current;
      if (!sid) {
        pushError(makeT(languageRef.current)("connectionError"), () => void boot());
        return;
      }
      const clientEventId = `local-send-${Date.now()}-${(localSeq.current += 1)}`;
      setMessages((prev) => [...prev, { id: clientEventId, client_event_id: clientEventId, role: "user", text, kind: "message" }]);
      setSending(true);
      try {
        const result = await api.postMessage(sid, text, languageRef.current, clientEventId);
        await refresh();
        if (!result.ok) {
          console.error("send failed:", result.detail);
          pushError(makeT(languageRef.current)("sendFailed"), () => void send(text));
        }
      } catch (error) {
        console.error("send failed:", error);
        pushError(makeT(languageRef.current)("sendFailed"), () => void send(text));
      } finally {
        setSending(false);
      }
    },
    [refresh, pushError, boot],
  );

  const upload = useCallback(
    async (files: File[]) => {
      if (!files.length) return;
      const sid = sessionRef.current;
      if (!sid) {
        pushError(makeT(languageRef.current)("connectionError"), () => void boot());
        return;
      }
      const clientEventId = `local-upload-${Date.now()}-${(localSeq.current += 1)}`;
      const names = files.map((file) => file.name).filter(Boolean);
      const label = names.slice(0, 3).join(", ") + (names.length > 3 ? ` +${names.length - 3}` : "");
      const prefix = makeT(languageRef.current)("uploadLocalPrefix");
      setMessages((prev) => [...prev, { id: clientEventId, client_event_id: clientEventId, role: "user", kind: "upload", text: `${prefix}${label}` }]);
      setUploadProgress({ id: clientEventId, label: makeT(languageRef.current)("uploadSaving"), percent: 1 });
      try {
        const result = await api.uploadFiles(sid, files, clientEventId, (percent) => {
          setUploadProgress({ id: clientEventId, label: makeT(languageRef.current)("uploadSaving"), percent });
        });
        if (!result.ok) throw new Error(result.detail);
        await refresh();
      } catch (error) {
        console.error("upload failed:", error);
        pushError(makeT(languageRef.current)("uploadFailed"), () => void upload(files));
      } finally {
        setUploadProgress((current) => (current?.id === clientEventId ? null : current));
      }
    },
    [refresh, pushError, boot],
  );

  const refreshNow = useCallback(() => {
    refresh().catch((error) => console.error("refresh failed:", error));
  }, [refresh]);

  const reconnect = useCallback(() => {
    if (sessionRef.current) {
      setConnection("connecting");
      refresh()
        .then(() => setConnection("ok"))
        .catch(() => setConnection("error"));
    } else {
      void boot();
    }
  }, [refresh, boot]);

  const saveApiKey = useCallback((value: string) => {
    if (value) sessionStorage.setItem("omc_deepseek_key", value);
    else sessionStorage.removeItem("omc_deepseek_key");
  }, []);

  return {
    t,
    language,
    setLanguage,
    connection,
    sessionId,
    messages,
    assets,
    jobs,
    sending,
    uploadProgress,
    send,
    upload,
    refreshNow,
    reconnect,
    dismiss,
    getApiKey: api.getApiKey,
    saveApiKey,
  };
}
