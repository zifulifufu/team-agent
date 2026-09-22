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
import SkillsPage from "./settings/SkillsPage";
import PluginsPage from "./settings/PluginsPage";
import McpPage from "./settings/McpPage";
import ExternalPage from "./settings/ExternalPage";
import ChannelsPage from "./settings/ChannelsPage";
import SettingsModal, { type PageProps, type SettingsTab } from "./settings/SettingsModal";

/** The pages that are the main area rather than a tab inside Settings. Prompts, the library
 *  and memory are the other way round: they are Settings tabs, so a link to one of them keeps
 *  Settings open and just switches tab (see `goTab`). */
const AREA_PAGES: Record<"skills" | "plugins" | "mcp" | "external" | "channels", ComponentType<PageProps>> = {
  skills: SkillsPage, plugins: PluginsPage, mcp: McpPage, external: ExternalPage, channels: ChannelsPage,
};

/** Settings ids that name one of those pages, as the `View` that shows it. */
const AREA_VIEWS: Partial<Record<SettingsTab, View>> = {
  skills: { kind: "skills" }, plugins: { kind: "plugins" }, mcp: { kind: "mcp" },
  external: { kind: "external" }, channels: { kind: "channels" }, appearance: { kind: "appearance" },
};

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
   * "Go to another page". A settings id either names a page in the main area — in which case
   * Settings closes first, so the user is not left with it covering the page it just opened —
   * or it is a tab inside Settings, in which case the dialog stays open and switches to it.
   */
  const goTab = (t: SettingsTab) => {
    const area = AREA_VIEWS[t];
    if (area) {
      setSettings(null);
      setView(area);
    } else {
      setSettings(t);
    }
  };
  const AreaPage = view.kind in AREA_PAGES ? AREA_PAGES[view.kind as keyof typeof AREA_PAGES] : null;
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
        {AreaPage && <div className="tool-page"><AreaPage key={view.kind} onTab={goTab} /></div>}
        {/* A group's own library is a drill-down from its chat, and it goes "back" to the
            library tab in Settings, which is where the overview of every document lives. */}
        {view.kind === "library" && (
          <LibraryPage key={view.gid} groupId={view.gid} onBack={() => goTab("library")} />
        )}
        {view.kind === "appearance" && <div className="tool-page"><AppearancePage /></div>}
      </main>
      <Toaster />
      {settings && <SettingsModal tab={settings} onTab={goTab} onClose={() => setSettings(null)} onOpenGroup={openGroup} />}
    </div>
  );
}
