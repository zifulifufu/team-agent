import { useEffect, type ComponentType } from "react";
import { ArrowLeft, BarChart3, type LucideIcon, BookOpen, Boxes, Brain, Cpu, Database, Download, GitBranch, HardDrive, Info, MessageSquareText, Palette, Plug, Puzzle, Server, ShieldCheck, SlidersHorizontal, Library, Sparkles, TerminalSquare } from "lucide-react";
import { useData } from "../data";
import ProvidersPage from "./ProvidersPage";
import RoutingPage from "./RoutingPage";
import LocalPage from "./LocalPage";
import SkillsPage from "./SkillsPage";
import PluginsPage from "./PluginsPage";
import McpPage from "./McpPage";
import ExternalPage from "./ExternalPage";
import AwesomePage from "./AwesomePage";
import PromptsPage from "../pages/PromptsPage";
import LibraryPage from "../pages/LibraryPage";
import MemoryPage from "../pages/MemoryPage";
import UpdatesPage from "./UpdatesPage";
import GeneralPage from "./GeneralPage";
import PermissionsPage from "./PermissionsPage";
import AppearancePage from "./AppearancePage";
import DataPage from "./DataPage";
import StatsPage from "./StatsPage";
import DepsPage from "./DepsPage";
import AboutPage from "./AboutPage";

export type SettingsTab =
  | "providers" | "routing" | "local"
  | "skills" | "plugins" | "mcp" | "external" | "awesome" | "prompts" | "library" | "memory"
  | "general" | "permissions" | "updates" | "appearance" | "data" | "stats" | "deps" | "about";

/** 设置页里的每个页面都可以选择接收 onTab,用来跳到别的设置标签。 */
export interface PageProps {
  onTab: (t: SettingsTab) => void;
}

const GROUPS: { title: string; items: { id: SettingsTab; label: string; icon: LucideIcon; page: ComponentType<PageProps> }[] }[] = [
  {
    title: "模型",
    items: [
      { id: "providers", label: "模型服务", icon: Boxes, page: ProvidersPage },
      { id: "routing", label: "路由与回退", icon: GitBranch, page: RoutingPage },
      { id: "local", label: "本地模型", icon: HardDrive, page: LocalPage },
    ],
  },
  {
    title: "工具",
    items: [
      { id: "skills", label: "技能", icon: Sparkles, page: SkillsPage },
      { id: "plugins", label: "插件", icon: Puzzle, page: PluginsPage },
      { id: "mcp", label: "MCP", icon: Plug, page: McpPage },
      { id: "external", label: "外部智能体", icon: TerminalSquare, page: ExternalPage },
      { id: "awesome", label: "示例库", icon: Library, page: AwesomePage },
      { id: "prompts", label: "提示词", icon: MessageSquareText, page: PromptsPage },
      { id: "library", label: "资料库", icon: BookOpen, page: LibraryPage },
      { id: "memory", label: "记忆", icon: Brain, page: MemoryPage },
    ],
  },
  {
    title: "应用",
    items: [
      { id: "general", label: "通用", icon: SlidersHorizontal, page: GeneralPage },
      { id: "permissions", label: "权限与操控", icon: ShieldCheck, page: PermissionsPage },
      { id: "updates", label: "更新与发现", icon: Download, page: UpdatesPage },
      { id: "appearance", label: "外观", icon: Palette, page: AppearancePage },
      { id: "data", label: "数据", icon: Database, page: DataPage },
      { id: "stats", label: "使用统计", icon: BarChart3, page: StatsPage },
      { id: "deps", label: "依赖", icon: Cpu, page: DepsPage },
      { id: "about", label: "关于", icon: Info, page: AboutPage },
    ],
  },
];

export default function SettingsModal({ tab, onTab, onClose }: { tab: SettingsTab; onTab: (t: SettingsTab) => void; onClose: () => void }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      // 有弹窗(添加服务商等)打开时,Esc 先关弹窗
      if (e.key === "Escape" && !document.querySelector(".modal-mask")) onClose();
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  const { updateCount } = useData();
  const Page = GROUPS.flatMap((g) => g.items).find((i) => i.id === tab)?.page ?? ProvidersPage;
  return (
    <div className="settings" role="dialog" aria-label="设置">
      <div className="settings-top drag">
        <button className="back-btn nodrag" onClick={onClose}><ArrowLeft size={16} /> 返回</button>
        <span className="settings-title"><Server size={15} /> 设置</span>
      </div>
      <div className="settings-body">
        <nav className="settings-nav">
          {GROUPS.map((g) => (
            <div key={g.title} className="nav-group">
              <div className="nav-group-title">{g.title}</div>
              {g.items.map((i) => (
                <button key={i.id} className={"nav-item" + (tab === i.id ? " on" : "")} onClick={() => onTab(i.id)}>
                  <i.icon size={16} /> {i.label}
                  {i.id === "updates" && updateCount > 0 && <span className="count-badge" style={{ marginLeft: "auto" }}>{updateCount}</span>}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="settings-content">
          <Page key={tab} onTab={onTab} />
        </div>
      </div>
    </div>
  );
}
