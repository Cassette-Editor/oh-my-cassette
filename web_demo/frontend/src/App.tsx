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

  return (
    <div className="app-shell">
      <ConnectionBanner />
      <TopBar onOpenSettings={() => setSettingsOpen(true)} onToggleDrawer={() => setDrawerOpen((open) => !open)} />
      <main className="workspace">
        <ChatPane />
        <SidePanel open={drawerOpen} onClose={() => setDrawerOpen(false)} />
      </main>
      <div
        className={`drawer-backdrop ${drawerOpen ? "show" : ""}`}
        onClick={() => setDrawerOpen(false)}
        aria-hidden="true"
      />
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
