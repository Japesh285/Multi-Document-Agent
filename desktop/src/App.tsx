import { useEffect } from "react";
import { useStore } from "./store";
import Sidebar from "./components/Sidebar";
import ChatPanel from "./components/ChatPanel";
import RightPanel from "./components/RightPanel";
import StatusBar from "./components/StatusBar";
import SettingsModal from "./components/SettingsModal";
import OnboardingModal from "./components/OnboardingModal";
import ReportViewer from "./components/ReportViewer";
import BootSplash from "./components/BootSplash";

export default function App() {
  const { ready, bootError, showSettings, showOnboarding, showReportId,
          boot, refreshHealth, refreshOllama } = useStore();

  useEffect(() => {
    void boot();
  }, [boot]);

  // Lightweight heartbeat — every 8s, refresh health + ollama dot
  useEffect(() => {
    if (!ready) return;
    const t = setInterval(() => {
      void refreshHealth();
      void refreshOllama();
    }, 8000);
    return () => clearInterval(t);
  }, [ready, refreshHealth, refreshOllama]);

  if (!ready) {
    return <BootSplash error={bootError} />;
  }

  return (
    <div className="h-screen w-screen flex flex-col bg-ink-950 text-chalk-100">
      <div className="flex-1 grid grid-cols-[260px_1fr_360px] min-h-0">
        <Sidebar />
        <ChatPanel />
        <RightPanel />
      </div>
      <StatusBar />

      {showSettings    && <SettingsModal />}
      {showOnboarding  && <OnboardingModal />}
      {showReportId    && <ReportViewer reportId={showReportId} />}
    </div>
  );
}
