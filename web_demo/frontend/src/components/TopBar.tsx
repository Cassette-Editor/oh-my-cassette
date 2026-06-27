import { useApp } from "../useCassette";

interface Props {
  onOpenSettings: () => void;
  onToggleDrawer: () => void;
}

export function TopBar({ onOpenSettings, onToggleDrawer }: Props) {
  const { t, language, setLanguage, sessionId, refreshNow, assets, jobs } = useApp();
  const count = (assets?.length || 0) + (jobs?.length || 0);

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

        <button
          type="button"
          className="status-toggle btn-ghost"
          data-tour="status-toggle"
          onClick={onToggleDrawer}
          title={t("status")}
          aria-label={t("status")}
        >
          <svg className="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <rect x="3" y="4.5" width="18" height="15" rx="2.5" stroke="currentColor" strokeWidth="1.6" />
            <path d="M14.5 4.5v15" stroke="currentColor" strokeWidth="1.6" />
          </svg>
          <span className="status-label">{t("status")}</span>
          {count > 0 && <span className="status-count mono">{count}</span>}
        </button>

        <button
          type="button"
          className="btn-ghost btn-icon"
          onClick={refreshNow}
          title={t("refreshTitle")}
          aria-label={t("refresh")}
        >
          <svg className="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M20 11.5a8 8 0 1 0-1.2 4.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            <path d="M20 5v6h-6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>

        <button
          type="button"
          className="btn-ghost btn-icon"
          onClick={onOpenSettings}
          title={t("settingsTitle")}
          aria-label={t("settings")}
        >
          <svg className="icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle cx="12" cy="12" r="3.1" stroke="currentColor" strokeWidth="1.6" />
            <path
              d="M12 2.6v2.3M12 19.1v2.3M21.4 12h-2.3M4.9 12H2.6M18.65 5.35l-1.6 1.6M6.95 17.05l-1.6 1.6M18.65 18.65l-1.6-1.6M6.95 6.95l-1.6-1.6"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </div>
    </header>
  );
}
