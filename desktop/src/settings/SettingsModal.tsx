import { useEffect, type ComponentType } from "react";
import { ArrowLeft, BarChart3, BookOpen, Brain, type LucideIcon, Boxes, Cpu, Database, Download, GitBranch, HardDrive, Info, LayoutTemplate, MessageSquareText, Server, ShieldCheck, SlidersHorizontal } from "lucide-react";
import { useData } from "../data";
import { useI18n } from "../i18n";
import ProvidersPage from "./ProvidersPage";
import RoutingPage from "./RoutingPage";
import LocalPage from "./LocalPage";
import GalleryPage from "./GalleryPage";
import UpdatesPage from "./UpdatesPage";
import GeneralPage from "./GeneralPage";
import PermissionsPage from "./PermissionsPage";
import DataPage from "./DataPage";
import StatsPage from "./StatsPage";
import DepsPage from "./DepsPage";
import AboutPage from "./AboutPage";
import LibraryPage from "../pages/LibraryPage";
import MemoryPage from "../pages/MemoryPage";
import PromptsPage from "../pages/PromptsPage";

export type SettingsTab =
  | "providers" | "routing" | "local"
  | "skills" | "plugins" | "mcp" | "external" | "channels" | "gallery" | "prompts" | "library" | "memory"
  | "general" | "permissions" | "updates" | "appearance" | "data" | "stats" | "deps" | "about";

/** Each settings page may take an `onTab` so it can jump to another settings tab. */
export interface PageProps {
  onTab: (t: SettingsTab) => void;
  /** Jump straight into a group after creating it from the template gallery (only that page needs it, hence optional). */
  onOpenGroup?: (gid: string) => void;
}

/** The library, as a settings tab: the overview of every document. A single group's library is
 *  reached from that group's chat instead and shows up in the main area, so this one takes no
 *  `groupId` and is wrapped rather than pointed at directly — the nav hands every page the same
 *  props, and `LibraryPage`'s own are optional and named differently. */
function LibraryTab() {
  return <LibraryPage />;
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
    // Prompts, the library and memory are listed here and nowhere else; skills, plugins and MCP
    // have their own entry in the sidebar's Tools column instead, and both live in the main
    // area (App routes those ids there, so a link from inside Settings — Gallery → "tick the
    // skills" — lands in the right place). External agents and chat channels are the reverse
    // case: they are in the sidebar, because they are set up once and watched, not tuned here.
    title: "Tools",
    items: [
      { id: "prompts", label: "Prompts", icon: MessageSquareText, page: PromptsPage },
      { id: "library", label: "Library", icon: BookOpen, page: LibraryTab },
      { id: "memory", label: "Memory", icon: Brain, page: MemoryPage },
      { id: "gallery", label: "Template gallery", icon: LayoutTemplate, page: GalleryPage },
    ],
  },
  {
    title: "App",
    items: [
      { id: "general", label: "General", icon: SlidersHorizontal, page: GeneralPage },
      { id: "permissions", label: "Permissions & control", icon: ShieldCheck, page: PermissionsPage },
      { id: "updates", label: "Updates & discovery", icon: Download, page: UpdatesPage },
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
  // A tab that is not in the nav means it belongs to the main area (see App.goTab). Falling back
  // to the first item keeps this from rendering a page with no highlighted nav entry.
  const items = GROUPS.flatMap((g) => g.items);
  const active = items.find((i) => i.id === tab) ?? items[0];
  const Page = active.page;
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
                <button key={i.id} className={"nav-item" + (active.id === i.id ? " on" : "")} onClick={() => onTab(i.id)}>
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
