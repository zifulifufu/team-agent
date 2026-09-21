import { useEffect, useState, type ComponentType } from "react";
import { PanelLeftOpen } from "lucide-react";
import { useData } from "./data";
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
  const { online, groups, epoch } = useData();
  const [view, setView] = useState<View>({ kind: "home" });
  const [settings, setSettings] = useState<SettingsTab | null>(null);
  const [collapsed, setCollapsed] = useState(() => prefs.read("ta.sidebar") === "0");

  const setSide = (c: boolean) => {
    setCollapsed(c);
    prefs.write("ta.sidebar", c ? "0" : "1");
  };

  // 当前群聊被删除后回到首页
  useEffect(() => {
    if (view.kind === "chat" && online && !groups.some((g) => g.id === view.gid)) setView({ kind: "home" });
  }, [groups, view]);

  // 工具页里的「去别的页面」:技能/插件/MCP 直接在主区切换,其余打开设置
  const goTab = (t: SettingsTab) => {
    if (t === "skills" || t === "plugins" || t === "mcp") setView({ kind: t });
    else setSettings(t);
  };
  const ToolPage = view.kind === "skills" || view.kind === "plugins" || view.kind === "mcp" ? TOOL_PAGES[view.kind] : null;
  // 模板中心里建好群聊后直接进去:关掉设置,切到那个群
  const openGroup = (gid: string) => {
    setSettings(null);
    setView({ kind: "chat", gid, autoSend: "" });
  };

  return (
    <div className="shell">
      {!collapsed && <Sidebar view={view} onView={setView} onSettings={setSettings} onCollapse={() => setSide(true)} version={APP_VERSION} />}
      <main className="main" key={epoch}>
        {collapsed && (
          <button className="icon-btn expand-btn" title="展开侧边栏" aria-label="展开侧边栏" onClick={() => setSide(false)}>
            <PanelLeftOpen size={17} />
          </button>
        )}
        {!online && <div className="banner">后端未连接,正在重试…(首次启动需要几秒加载 LiteLLM)</div>}
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
