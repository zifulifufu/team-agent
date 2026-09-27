import { useEffect, type ComponentType } from "react";
import { ArrowLeft, Anchor, BarChart3, BookOpen, Brain, type LucideIcon, Boxes, Compass, Cpu, Database, GitBranch, HardDrive, Info, LayoutTemplate, MessageSquareText, Package, Plug, Puzzle, Server, ShieldCheck, SlidersHorizontal, Sparkles, TerminalSquare, ThumbsUp, Webhook } from "lucide-react";
import { useData } from "../data";
import { useI18n } from "../i18n";
import ProvidersPage from "./ProvidersPage";
import RoutingPage from "./RoutingPage";
import LocalPage from "./LocalPage";
import GalleryPage from "./GalleryPage";
import DiscoverPage from "./DiscoverPage";
import VersionPage from "./VersionPage";
import GeneralPage from "./GeneralPage";
import PermissionsPage from "./PermissionsPage";
import DataPage from "./DataPage";
import StatsPage from "./StatsPage";
import FeedbackPage from "./FeedbackPage";
import DepsPage from "./DepsPage";
import AboutPage from "./AboutPage";
import LibraryPage from "../pages/LibraryPage";
import MemoryPage from "../pages/MemoryPage";
import PromptsPage from "../pages/PromptsPage";
import SkillsPage from "./SkillsPage";
import PluginsPage from "./PluginsPage";
import McpPage from "./McpPage";
import HooksPage from "./HooksPage";
import ExternalPage from "./ExternalPage";
import ChannelsPage from "./ChannelsPage";
import CapabilityWorkspace, { type CapabilityArea } from "./CapabilityWorkspace";

export type SettingsTab =
  | "providers" | "routing" | "local"
  | "skills" | "plugins" | "mcp" | "hooks" | "external" | "channels" | "gallery" | "prompts" | "library" | "memory"
  | "general" | "permissions" | "discover" | "version" | "appearance" | "data" | "stats" | "deps" | "about"
  | "feedback";

/** Each settings page may take an `onTab` so it can jump to another settings tab. */
export interface PageProps {
  onTab: (t: SettingsTab) => void;
  /** Jump straight into a group after creating it from the template gallery (only that page needs it, hence optional). */
  onOpenGroup?: (gid: string) => void;
  /** Which group the capability pages are attached to, and how to change it.
   *
   *  Only the six capability pages read these. They used to live in App, because those pages were
   *  drawn in the main area with a group bar of their own; they are settings tabs now, so the bar
   *  (and the group it points at) keeps travelling with them — otherwise "attach this skill to a
   *  group" would lose the group it was about. */
  capGroupId?: string | null;
  onCapGroup?: (id: string) => void;
}

/** The library, as a settings tab: the overview of every document. A single group's library is
 *  reached from that group's chat instead and shows up in the main area, so this one takes no
 *  `groupId` and is wrapped rather than pointed at directly — the nav hands every page the same
 *  props, and `LibraryPage`'s own are optional and named differently. */
function LibraryTab() {
  return <LibraryPage />;
}

/** A capability page, wrapped in the bar that says **which group** the attach buttons act on.
 *
 *  ⚠️ Called at module scope, once per page — never inside the component. A wrapper defined during
 *  render is a new component type on every render, which remounts the page (and throws away the
 *  search box the user was typing in) on every keystroke of unrelated state.
 */
const capTab = (area: CapabilityArea, Page: ComponentType<PageProps>): ComponentType<PageProps> =>
  function CapabilityTab({ onTab, onOpenGroup, capGroupId, onCapGroup }: PageProps) {
    return (
      <CapabilityWorkspace area={area} groupId={capGroupId ?? null}
        onGroup={(id) => onCapGroup?.(id)} onTab={onTab}
        onOpenGroup={onOpenGroup ?? (() => undefined)}>
        <Page onTab={onTab} onOpenGroup={onOpenGroup} />
      </CapabilityWorkspace>
    );
  };

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
      { id: "prompts", label: "Prompts", icon: MessageSquareText, page: PromptsPage },
      { id: "library", label: "Library", icon: BookOpen, page: LibraryTab },
      { id: "memory", label: "Memory", icon: Brain, page: MemoryPage },
      { id: "gallery", label: "Template gallery", icon: LayoutTemplate, page: GalleryPage },
    ],
  },
  {
    // 这一组 2026-09-27 从侧栏搬进来：用户要的是「移到用户设置面板提示词、资料库那个工具栏里」。
    // 它们和上面那四项是同一类东西 —— 配一次、以后偶尔回来改一处 —— 而侧栏那一格业主已改成**
    // 专区**（做东西的地方）。搬进来之后，`App.goTab` 对这几个 id 不再关掉设置、跳主区域。
    //
    // ⚠️ 每一条都套着 `CapabilityWorkspace`：它是「这些能力接到哪个群」的那条栏。丢掉它，
    // 页面上的「接入本群 / 从本群移出」按钮就没有群可指了（它们读的是那个 context）。
    title: "Capabilities",
    items: [
      { id: "skills", label: "Skills", icon: Sparkles, page: capTab("skills", SkillsPage) },
      { id: "plugins", label: "Plugins", icon: Puzzle, page: capTab("plugins", PluginsPage) },
      { id: "mcp", label: "MCP", icon: Plug, page: capTab("mcp", McpPage) },
      { id: "hooks", label: "Hooks", icon: Anchor, page: capTab("hooks", HooksPage) },
      { id: "external", label: "Agents and local tools", icon: TerminalSquare, page: capTab("external", ExternalPage) },
      { id: "channels", label: "Chat channels", icon: Webhook, page: capTab("channels", ChannelsPage) },
    ],
  },
  {
    title: "App",
    items: [
      { id: "general", label: "General", icon: SlidersHorizontal, page: GeneralPage },
      { id: "permissions", label: "Permissions & control", icon: ShieldCheck, page: PermissionsPage },
      // Discover (skills, plugins, catalogs, models) and Software update (the app itself) used to be
      // one page: the first is a list of things you may act on, the second is a download you do by
      // hand, and mixing them made the one that needs a person look like the ones that do not.
      { id: "discover", label: "Discover", icon: Compass, page: DiscoverPage },
      { id: "version", label: "Software update", icon: Package, page: VersionPage },
      { id: "data", label: "Data", icon: Database, page: DataPage },
      { id: "stats", label: "Usage stats", icon: BarChart3, page: StatsPage },
      { id: "feedback", label: "Feedback", icon: ThumbsUp, page: FeedbackPage },
      { id: "deps", label: "Dependencies", icon: Cpu, page: DepsPage },
      { id: "about", label: "About", icon: Info, page: AboutPage },
    ],
  },
];

export default function SettingsModal({ tab, onTab, onClose, onOpenGroup, capGroupId, onCapGroup }: {
  tab: SettingsTab; onTab: (t: SettingsTab) => void; onClose: () => void;
  onOpenGroup?: (gid: string) => void;
  /** 能力那六个页面「接到哪个群」的那条栏所需的两个值（见 `capTab`）。 */
  capGroupId?: string | null;
  onCapGroup?: (id: string) => void;
}) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      // With a dialog open (adding a provider, say), Esc closes the dialog first
      if (e.key === "Escape" && !document.querySelector(".modal-mask")) onClose();
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  const { updateCount, appUpdateCount } = useData();
  const { t } = useI18n();
  // Each page carries its own count: the two sets are shown in different places, and a badge on the
  // wrong entry sends the reader to a page where the reminder is not.
  const badge = (id: SettingsTab) =>
    id === "version" && appUpdateCount > 0 ? appUpdateCount
      : id === "discover" && updateCount - appUpdateCount > 0 ? updateCount - appUpdateCount
        : 0;
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
                  {badge(i.id) > 0 && <span className="count-badge" style={{ marginLeft: "auto" }}>{badge(i.id)}</span>}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="settings-content">
          <Page key={tab} onTab={onTab} onOpenGroup={onOpenGroup}
                capGroupId={capGroupId} onCapGroup={onCapGroup} />
        </div>
      </div>
    </div>
  );
}
