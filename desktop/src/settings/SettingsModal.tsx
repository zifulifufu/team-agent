import { useEffect, type ComponentType } from "react";
import { ArrowLeft, BarChart3, type LucideIcon, BookOpen, Boxes, Brain, Cpu, Database, Download, GitBranch, HardDrive, Info, LayoutTemplate, MessageSquareText, Palette, Plug, Puzzle, Server, ShieldCheck, SlidersHorizontal, Sparkles, TerminalSquare } from "lucide-react";
import { useData } from "../data";
import { useI18n } from "../i18n";
import ProvidersPage from "./ProvidersPage";
import RoutingPage from "./RoutingPage";
import LocalPage from "./LocalPage";
import SkillsPage from "./SkillsPage";
import PluginsPage from "./PluginsPage";
import McpPage from "./McpPage";
import ExternalPage from "./ExternalPage";
import GalleryPage from "./GalleryPage";
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
  | "skills" | "plugins" | "mcp" | "external" | "gallery" | "prompts" | "library" | "memory"
  | "general" | "permissions" | "updates" | "appearance" | "data" | "stats" | "deps" | "about";

/** Each settings page may take an `onTab` so it can jump to another settings tab. */
export interface PageProps {
  onTab: (t: SettingsTab) => void;
  /** Jump straight into a group after creating it from the template gallery (only that page needs it, hence optional). */
  onOpenGroup?: (gid: string) => void;
}

const GROUPS: { title: string; items: { id: SettingsTab; label: string; icon: LucideIcon; page: ComponentType<PageProps> }[] }[] = [
  {
    title: "Models",
    items: [
      { id: "providers", label: "Providers", icon: Boxes, page: ProvidersPage },
      { id: "routing", label: "Routing & fallback", icon: GitBranch, page: RoutingPage },
      { id: "local", label: "Local models", icon: HardDrive, page: LocalPage },
    ],
  },
  {
    title: "Tools",
    items: [
      { id: "skills", label: "Skills", icon: Sparkles, page: SkillsPage },
      { id: "plugins", label: "Plugins", icon: Puzzle, page: PluginsPage },
      { id: "mcp", label: "MCP", icon: Plug, page: McpPage },
      { id: "external", label: "External agents", icon: TerminalSquare, page: ExternalPage },
      { id: "gallery", label: "Template gallery", icon: LayoutTemplate, page: GalleryPage },
      { id: "prompts", label: "Prompts", icon: MessageSquareText, page: PromptsPage },
      { id: "library", label: "Library", icon: BookOpen, page: LibraryPage },
      { id: "memory", label: "Memory", icon: Brain, page: MemoryPage },
    ],
  },
  {
    title: "App",
    items: [
      { id: "general", label: "General", icon: SlidersHorizontal, page: GeneralPage },
      { id: "permissions", label: "Permissions & control", icon: ShieldCheck, page: PermissionsPage },
      { id: "updates", label: "Updates & discovery", icon: Download, page: UpdatesPage },
      { id: "appearance", label: "Appearance", icon: Palette, page: AppearancePage },
      { id: "data", label: "Data", icon: Database, page: DataPage },
      { id: "stats", label: "Usage stats", icon: BarChart3, page: StatsPage },
      { id: "deps", label: "Dependencies", icon: Cpu, page: DepsPage },
      { id: "about", label: "About", icon: Info, page: AboutPage },
    ],
  },
];

export default function SettingsModal({ tab, onTab, onClose, onOpenGroup }: { tab: SettingsTab; onTab: (t: SettingsTab) => void; onClose: () => void; onOpenGroup?: (gid: string) => void }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      // With a dialog open (adding a provider, say), Esc closes the dialog first
      if (e.key === "Escape" && !document.querySelector(".modal-mask")) onClose();
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  const { updateCount } = useData();
  const { t } = useI18n();
  const Page = GROUPS.flatMap((g) => g.items).find((i) => i.id === tab)?.page ?? ProvidersPage;
  return (
    <div className="settings" role="dialog" aria-label={t("Settings")}>
      <div className="settings-top drag">
        <button className="back-btn nodrag" onClick={onClose}><ArrowLeft size={16} /> {t("Back")}</button>
        <span className="settings-title"><Server size={15} /> {t("Settings")}</span>
      </div>
      <div className="settings-body">
        <nav className="settings-nav">
          {GROUPS.map((g) => (
            <div key={g.title} className="nav-group">
              <div className="nav-group-title">{t(g.title)}</div>
              {g.items.map((i) => (
                <button key={i.id} className={"nav-item" + (tab === i.id ? " on" : "")} onClick={() => onTab(i.id)}>
                  <i.icon size={16} /> {t(i.label)}
                  {i.id === "updates" && updateCount > 0 && <span className="count-badge" style={{ marginLeft: "auto" }}>{updateCount}</span>}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="settings-content">
          <Page key={tab} onTab={onTab} onOpenGroup={onOpenGroup} />
        </div>
      </div>
    </div>
  );
}
