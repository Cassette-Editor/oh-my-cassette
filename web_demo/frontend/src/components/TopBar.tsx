import { useApp } from "../useCassette";

interface Props {
  onOpenSettings: () => void;
  onToggleDrawer: () => void;
}

export function TopBar({ onOpenSettings, onToggleDrawer }: Props) {
  const { t, language, setLanguage, sessionId, refreshNow } = useApp();
  return (
    <header className="topbar">
      <div className="brand" data-tour="brand">
        <svg className="brand-mark" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <rect x="2.25" y="5.25" width="19.5" height="13.5" rx="3" stroke="currentColor" strokeWidth="1.5" />
          <circle cx="8" cy="12" r="2.4" stroke="currentColor" strokeWidth="1.5" />
          <circle cx="16" cy="12" r="2.4" stroke="currentColor" strokeWidth="1.5" />
          <path d="M10.4 12h3.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          <path d="M7 18.75l1.6-2.4h6.8l1.6 2.4" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
        </svg>
        <div style={{ minWidth: 0 }}>
          <h1>Oh My Cassette</h1>
          <p>
            {sessionId ? (
              <>
                {t("sessionPrefix")}: <span className="mono">{sessionId}</span>
              </>
            ) : (
              t("webDemo")
            )}
          </p>
        </div>
      </div>
      <div className="topbar-actions">
        <div className="language-toggle" role="group" aria-label="Language">
          <button
            type="button"
            className={language === "zh" ? "active" : ""}
            aria-pressed={language === "zh"}
            onClick={() => setLanguage("zh")}
          >
            中
          </button>
          <button
            type="button"
            className={language === "en" ? "active" : ""}
            aria-pressed={language === "en"}
            onClick={() => setLanguage("en")}
          >
            EN
          </button>
        </div>
        <button type="button" className="status-toggle" data-tour="status-toggle" onClick={onToggleDrawer} title={t("status")}>
          {t("status")}
        </button>
        <button type="button" onClick={refreshNow} title={t("refreshTitle")}>
          {t("refresh")}
        </button>
        <button type="button" onClick={onOpenSettings} title={t("settingsTitle")}>
          {t("settings")}
        </button>
      </div>
    </header>
  );
}
