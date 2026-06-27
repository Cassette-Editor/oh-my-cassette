import { useEffect, useState } from "react";
import { CassetteProvider, useApp } from "./useCassette";
import { ConnectionBanner } from "./components/ConnectionBanner";
import { TopBar } from "./components/TopBar";
import { ChatPane } from "./components/ChatPane";
import { SidePanel } from "./components/SidePanel";
import { SettingsDialog } from "./components/SettingsDialog";
import { Onboarding } from "./components/Onboarding";

function Shell() {
  const { language } = useApp();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  }, [language]);

  // Close the status slide-over on Escape.
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  return (
    <div className="app-shell">
      <ConnectionBanner />
      <TopBar onOpenSettings={() => setSettingsOpen(true)} onToggleDrawer={() => setDrawerOpen((open) => !open)} />
      <main className="workspace">
        <ChatPane />
      </main>

      <div
        className={`drawer-backdrop ${drawerOpen ? "show" : ""}`}
        onClick={() => setDrawerOpen(false)}
        aria-hidden="true"
      />
      <SidePanel open={drawerOpen} onClose={() => setDrawerOpen(false)} />

      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <Onboarding />
    </div>
  );
}

export function App() {
  return (
    <CassetteProvider>
      <Shell />
    </CassetteProvider>
  );
}
