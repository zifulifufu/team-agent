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

  // "Go to another page" from the tools page: skills / plugins / MCP switch in the main area, everything else opens Settings
  const goTab = (t: SettingsTab) => {
    if (t === "skills" || t === "plugins" || t === "mcp") setView({ kind: t });
    else setSettings(t);
  };
  const ToolPage = view.kind === "skills" || view.kind === "plugins" || view.kind === "mcp" ? TOOL_PAGES[view.kind] : null;
  // After a group is created from the template gallery, go straight into it: close Settings and switch to that group
  const openGroup = (gid: string) => {
    setSettings(null);
    setView({ kind: "chat", gid, autoSend: "" });
  };

  return (
    <div className="shell">
      {!collapsed && <Sidebar view={view} onView={setView} onSettings={setSettings} onCollapse={() => setSide(true)} version={APP_VERSION} />}
      <main className="main" key={epoch}>
        {collapsed && (
          <button className="icon-btn expand-btn" title={t("Expand the sidebar")} aria-label={t("Expand the sidebar")} onClick={() => setSide(false)}>
            <PanelLeftOpen size={17} />
          </button>
        )}
        {!online && <div className="banner">{t("The backend is not connected; retrying… (the first start needs a few seconds to load LiteLLM)")}</div>}
        {view.kind === "home" && <HomePage onOpen={(gid, autoSend) => setView({ kind: "chat", gid, autoSend })} onSettings={setSettings} />}
        {view.kind === "chat" && (
          <ChatView
            key={view.gid}
            gid={view.gid}
            autoSend={view.autoSend}
            onAutoSent={() => setView((v) => (v.kind === "chat" ? { kind: "chat", gid: v.gid } : v))}
            onSettings={setSettings}
          />
        )}
        {view.kind === "agents" && <AgentsPage onSettings={setSettings} />}
        {ToolPage && view.kind !== "home" && <div className="tool-page"><ToolPage key={view.kind} onTab={goTab} /></div>}
        {view.kind === "library" && <LibraryPage />}
        {view.kind === "memory" && <MemoryPage />}
        {view.kind === "prompts" && <PromptsPage />}
      </main>
      <Toaster />
      {settings && <SettingsModal tab={settings} onTab={setSettings} onClose={() => setSettings(null)} onOpenGroup={openGroup} />}
    </div>
  );
}
