import { useEffect, useRef } from "react";
import { useApp } from "../useCassette";
import { isErrorMessage, isLocalError, type ChatEvent, type Message } from "../types";
import type { Translate } from "../i18n";

export function Messages() {
  const { t, messages, sending, dismiss } = useApp();
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, sending]);

  return (
    <div className="messages" ref={scrollRef} role="log" aria-live="polite" aria-relevant="additions">
      {messages.map((message) => (
        <MessageItem key={String(message.id)} message={message} t={t} dismiss={dismiss} />
      ))}
      {sending && (
        <article className="message assistant thinking thinking-ring" aria-label={t("thinking")}>
          <span className="typing">
            <i />
            <i />
            <i />
          </span>
        </article>
      )}
    </div>
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
      <article className="message error" role="alert">
        <div className="error-row">
          <span className="error-icon" aria-hidden="true">
            ⚠
          </span>
          <div>{message.text}</div>
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
      </article>
    );
  }

  const event = message as ChatEvent;
  return (
    <article className={`message ${event.role || "assistant"}`}>
      <div>{event.text || ""}</div>
      {event.has_attachment && event.attachment_url && <Attachment event={event} t={t} />}
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
