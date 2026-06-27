import { useEffect, useRef } from "react";
import { useApp } from "../useCassette";
import { isErrorMessage, isLocalError, type ChatEvent, type Message } from "../types";
import type { Translate } from "../i18n";

export function Messages() {
  const { t, messages, sending, uploadProgress, dismiss } = useApp();
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, sending, uploadProgress]);

  return (
    <div className="messages" ref={scrollRef} role="log" aria-live="polite" aria-relevant="additions">
      <div className="thread">
        {messages.map((message) => (
          <MessageItem key={String(message.id)} message={message} t={t} dismiss={dismiss} />
        ))}

        {uploadProgress && (
          <article className="msg assistant" aria-label={uploadProgress.label}>
            <AssistantAvatar thinking />
            <div className="msg-content">
              <div className="upload-progress">
                <div className="upload-progress-head">
                  <span>{uploadProgress.label}</span>
                  <span className="mono">{uploadProgress.percent}%</span>
                </div>
                <div className="upload-progress-bar" aria-hidden="true">
                  <span style={{ width: `${uploadProgress.percent}%` }} />
                </div>
              </div>
            </div>
          </article>
        )}

        {!uploadProgress && sending && (
          <article className="msg assistant thinking" aria-label={t("thinking")}>
            <AssistantAvatar thinking />
            <div className="msg-content">
              <span className="typing">
                <i />
                <i />
                <i />
              </span>
            </div>
          </article>
        )}
      </div>
    </div>
  );
}

function AssistantAvatar({ thinking = false }: { thinking?: boolean }) {
  return (
    <span className={`avatar ${thinking ? "is-thinking" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none">
        <rect x="2.5" y="6" width="19" height="12" rx="2.4" stroke="currentColor" strokeWidth="1.6" />
        <circle cx="8.5" cy="12" r="2" stroke="currentColor" strokeWidth="1.6" />
        <circle cx="15.5" cy="12" r="2" stroke="currentColor" strokeWidth="1.6" />
        <path d="M10.5 12h3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
    </span>
  );
}

function MessageItem({
  message,
  t,
  dismiss,
}: {
  message: Message;
  t: Translate;
  dismiss: (id: string) => void;
}) {
  if (isErrorMessage(message)) {
    const retry = isLocalError(message) ? message.retry : undefined;
    return (
      <article className="msg error" role="alert">
        <div className="msg-content">
          <div className="error-row">
            <span className="error-icon" aria-hidden="true">
              ⚠
            </span>
            <div className="msg-text">{message.text}</div>
          </div>
          {retry && (
            <button
              type="button"
              className="retry-inline"
              onClick={() => {
                dismiss(String(message.id));
                retry();
              }}
            >
              {t("retry")}
            </button>
          )}
        </div>
      </article>
    );
  }

  const event = message as ChatEvent;
  const role = event.role || "assistant";

  if (role === "user") {
    return (
      <article className="msg user">
        <div className="msg-bubble">
          <div className="msg-text">{event.text || ""}</div>
          {event.has_attachment && event.attachment_url && <Attachment event={event} t={t} />}
        </div>
      </article>
    );
  }

  return (
    <article className="msg assistant">
      <AssistantAvatar />
      <div className="msg-content">
        <div className="msg-text">{event.text || ""}</div>
        {event.has_attachment && event.attachment_url && <Attachment event={event} t={t} />}
      </div>
    </article>
  );
}

function Attachment({ event, t }: { event: ChatEvent; t: Translate }) {
  if (event.attachment_type === "image") {
    return (
      <div className="attachment">
        <img src={event.attachment_url} alt={event.attachment_name || t("attachment")} />
      </div>
    );
  }
  if (event.attachment_type === "video") {
    return (
      <div className="attachment">
        <video controls src={event.attachment_url} />
      </div>
    );
  }
  return (
    <div className="attachment">
      <a href={event.attachment_url} target="_blank" rel="noreferrer">
        {event.attachment_name || t("attachment")}
      </a>
    </div>
  );
}
