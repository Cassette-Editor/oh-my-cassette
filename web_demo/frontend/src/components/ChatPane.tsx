import { useRef, useState, type DragEvent } from "react";
import { useApp } from "../useCassette";
import { Messages } from "./Messages";
import { Composer } from "./Composer";

export function ChatPane() {
  const { t, upload } = useApp();
  const [dragging, setDragging] = useState(false);
  const depth = useRef(0);

  const hasFiles = (event: DragEvent) => Array.from(event.dataTransfer.types || []).includes("Files");

  return (
    <section
      className={`chat-pane ${dragging ? "dragover" : ""}`}
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
      <Composer />
    </section>
  );
}
