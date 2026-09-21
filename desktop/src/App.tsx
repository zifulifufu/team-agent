import { useEffect, useState, type ComponentType } from "react";
import { PanelLeftOpen } from "lucide-react";
import { useData } from "./data";
import { useI18n } from "./i18n";
import { prefs } from "./theme";
import { APP_VERSION } from "./lib";
import { Toaster } from "./ui";
import Sidebar, { type View } from "./components/Sidebar";
import HomePage from "./pages/HomePage";
import ChatView from "./pages/ChatView";
import AgentsPage from "./pages/AgentsPage";
import LibraryPage from "./pages/LibraryPage";
import AppearancePage from "./settings/AppearancePage";
import MemoryPage from "./pages/MemoryPage";
import PromptsPage from "./pages/PromptsPage";
import SkillsPage from "./settings/SkillsPage";
import PluginsPage from "./settings/PluginsPage";
import McpPage from "./settings/McpPage";
import SettingsModal, { type PageProps, type SettingsTab } from "./settings/SettingsModal";

const TOOL_PAGES: Record<"skills" | "plugins" | "mcp", ComponentType<PageProps>> = { skills: SkillsPage, plugins: PluginsPage, mcp: McpPage };

export default function App() {
  const { t } = useI18n();
  const { online, groups, epoch } = useData();
  const [view, setView] = useState<View>({ kind: "home" });
  const [settings, setSettings] = useState<SettingsTab | null>(null);
  const [collapsed, setCollapsed] = useState(() => prefs.read("ta.sidebar") === "0");

  const setSide = (c: boolean) => {
    setCollapsed(c);
    prefs.write("ta.sidebar", c ? "0" : "1");
  };

  // Go back to the home view when the open group is deleted
  useEffect(() => {
    if (view.kind === "chat" && online && !groups.some((g) => g.id === view.gid)) setView({ kind: "home" });
  }, [groups, view]);

  /**
   * "Go to another page". The tools that live in the sidebar's Tools column switch to the main
   * area — and if a link inside Settings points at one, Settings closes first, so the user is
   * not left with a dialog covering the page it just opened.
   */
  const TOOL_VIEWS: SettingsTab[] = ["skills", "plugins", "mcp", "prompts", "library", "memory", "appearance"];
  const goTab = (t: SettingsTab) => {
    if (TOOL_VIEWS.includes(t)) {
      setSettings(null);
      setView(t === "library" ? { kind: "library" } : { kind: t as "skills" });
    } else {
      setSettings(t);
    }
  };
  const ToolPage = view.kind === "skills" || view.kind === "plugins" || view.kind === "mcp" ? TOOL_PAGES[view.kind] : null;
  // After a group is created from the template gallery, go straight into it: close Settings and switch to that group
  const openGroup = (gid: string) => {
    setSettings(null);
    setView({ kind: "chat", gid, autoSend: "" });
  };

  return (
    <div className="shell">
      {!collapsed && <Sidebar view={view} onView={setView} onSettings={goTab} onCollapse={() => setSide(true)} version={APP_VERSION} />}
      <main className="main" key={epoch}>
        {collapsed && (
          <button className="icon-btn expand-btn" title={t("Expand the sidebar")} aria-label={t("Expand the sidebar")} onClick={() => setSide(false)}>
            <PanelLeftOpen size={17} />
          </button>
        )}
        {!online && <div className="banner">{t("The backend is not connected; retrying… (the first start needs a few seconds to load LiteLLM)")}</div>}
        {view.kind === "home" && <HomePage onOpen={(gid, autoSend) => setView({ kind: "chat", gid, autoSend })} onSettings={goTab} />}
        {view.kind === "chat" && (
          <ChatView
            key={view.gid}
            gid={view.gid}
            autoSend={view.autoSend}
            onAutoSent={() => setView((v) => (v.kind === "chat" ? { kind: "chat", gid: v.gid } : v))}
            onSettings={goTab}
            onOpenLibrary={() => setView({ kind: "library", gid: view.gid })}
          />
        )}
        {view.kind === "agents" && <AgentsPage onSettings={goTab} />}
        {ToolPage && view.kind !== "home" && <div className="tool-page"><ToolPage key={view.kind} onTab={goTab} /></div>}
        {view.kind === "library" && (
          <LibraryPage key={view.gid ?? "all"} groupId={view.gid} onBack={() => setView({ kind: "library" })} />
        )}
        {view.kind === "memory" && <MemoryPage />}
        {view.kind === "prompts" && <PromptsPage />}
        {view.kind === "appearance" && <div className="tool-page"><AppearancePage /></div>}
      </main>
      <Toaster />
      {settings && <SettingsModal tab={settings} onTab={goTab} onClose={() => setSettings(null)} onOpenGroup={openGroup} />}
    </div>
  );
}
