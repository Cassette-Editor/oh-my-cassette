import { useRef, useState, type DragEvent } from "react";
import { useApp } from "../useCassette";
import { Messages } from "./Messages";
import { Composer } from "./Composer";

export function ChatPane() {
  const { t, upload, messages } = useApp();
  const [dragging, setDragging] = useState(false);
  const depth = useRef(0);
  const isEmpty = messages.length === 0;

  const hasFiles = (event: DragEvent) => Array.from(event.dataTransfer.types || []).includes("Files");

  return (
    <section
      className={`chat-pane ${isEmpty ? "is-empty" : "is-active"} ${dragging ? "dragover" : ""}`}
      aria-label={t("messagesAria")}
      onDragEnter={(event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        depth.current += 1;
        setDragging(true);
      }}
      onDragOver={(event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(event) => {
        if (!hasFiles(event)) return;
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        depth.current = 0;
        setDragging(false);
        const files = Array.from(event.dataTransfer.files || []);
        if (files.length) void upload(files);
      }}
    >
      <Messages />

      <div className="drop-hint" aria-hidden="true">
        <svg className="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M12 16V4m0 0l-4 4m4-4l4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M4 16v2.5A1.5 1.5 0 005.5 20h13a1.5 1.5 0 001.5-1.5V16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
        <span>{t("dropHint")}</span>
      </div>

      <div className="composer-dock">
        {isEmpty && (
          <div className="hero">
            <svg className="hero-mark" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <rect x="2.25" y="5.25" width="19.5" height="13.5" rx="3" stroke="currentColor" strokeWidth="1.5" />
              <circle cx="8" cy="12" r="2.4" stroke="currentColor" strokeWidth="1.5" />
              <circle cx="16" cy="12" r="2.4" stroke="currentColor" strokeWidth="1.5" />
              <path d="M10.4 12h3.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              <path d="M7 18.75l1.6-2.4h6.8l1.6 2.4" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
            </svg>
            <h2 className="hero-title">{t("emptyTitle")}</h2>
            <p className="hero-sub">{t("emptyBody")}</p>
          </div>
        )}
        <Composer />
      </div>
    </section>
  );
}
