import { useEffect, useRef, useState } from "react";
import { useApp } from "../useCassette";

const COMMANDS = ["/edit", "/refine", "/music", "/cut"];

export function Composer() {
  const { t, send, upload, sending } = useApp();
  const [text, setText] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);

  // Grow the field with its content, capped by the CSS max-height.
  useEffect(() => {
    const el = textRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  const submit = () => {
    const value = text.trim();
    if (!value) return;
    setText("");
    void send(value);
  };

  const canSend = text.trim().length > 0 && !sending;

  return (
    <form
      className="composer"
      data-tour="composer"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="command-chips" role="group" aria-label={t("commandsAria")}>
        {COMMANDS.map((command) => (
          <button
            key={command}
            type="button"
            onClick={() => {
              setText((prev) => `${command} ${prev.replace(/^\/\S+\s*/, "")}`);
              textRef.current?.focus();
            }}
          >
            {command}
          </button>
        ))}
      </div>

      <input
        ref={fileRef}
        type="file"
        accept="video/*,image/*,audio/*"
        multiple
        hidden
        onChange={(event) => {
          const files = Array.from(event.target.files || []);
          if (files.length) void upload(files);
          event.target.value = "";
        }}
      />

      <div className="composer-box">
        <textarea
          ref={textRef}
          value={text}
          onChange={(event) => setText(event.target.value)}
          rows={1}
          placeholder={t("messagePlaceholder")}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing &&
              window.matchMedia("(min-width: 861px)").matches
            ) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <div className="composer-bar">
          <button
            id="uploadBtn"
            className="icon-btn"
            data-tour="upload"
            type="button"
            title={t("uploadTitle")}
            aria-label={t("upload")}
            onClick={() => fileRef.current?.click()}
          >
            <svg className="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M12 16V4m0 0l-4 4m4-4l4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M4 16v2.5A1.5 1.5 0 005.5 20h13a1.5 1.5 0 001.5-1.5V16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </button>
          <button
            id="sendBtn"
            className="send-btn"
            type="submit"
            title={t("sendTitle")}
            aria-label={sending ? t("processing") : t("send")}
            disabled={!canSend}
          >
            {sending ? (
              <span className="spinner" aria-hidden="true" />
            ) : (
              <svg className="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M4.5 12l15-7.5-4 7.5 4 7.5-15-7.5z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
              </svg>
            )}
          </button>
        </div>
      </div>
    </form>
  );
}
