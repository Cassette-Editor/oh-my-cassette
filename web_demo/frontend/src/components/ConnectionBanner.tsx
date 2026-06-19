import { useApp } from "../useCassette";

export function ConnectionBanner() {
  const { t, connection, reconnect } = useApp();
  if (connection === "ok") return null;
  const isError = connection === "error";
  return (
    <div className={`connection-banner ${isError ? "is-error" : "is-connecting"}`} role="status" aria-live="polite">
      <span id="connectionText">{isError ? t("connectionError") : t("connecting")}</span>
      {isError && (
        <button type="button" id="retryBtn" onClick={reconnect}>
          {t("retry")}
        </button>
      )}
    </div>
  );
}
