import { useMemo, useState } from "react";
import { Anchor, Archive, ArchiveRestore, Check, ChevronDown, ChevronRight, Compass, Info, ListFilter, MessageSquarePlus, Package, PanelLeftClose, PanelLeftOpen, Palette, Plug, Puzzle, Search, Settings, Sparkles, TerminalSquare, Trash2, Undo2, Users, Webhook, WifiOff } from "lucide-react";
import { api, relTime, type Group } from "../api";
import { useData } from "../data";
import { Switch, useConfirm, useOutside } from "../ui";
import { useI18n } from "../i18n";
import SearchPalette from "./SearchPalette";
import type { SettingsTab } from "../settings/SettingsModal";
import "../styles/members.css";

export type View =
  | { kind: "home" }
  | { kind: "chat"; gid: string; autoSend?: string }
  | { kind: "agents" }
  | { kind: "skills" }
  | { kind: "plugins" }
  | { kind: "mcp" }
  | { kind: "external" }
  | { kind: "channels" }
  | { kind: "hooks" }
  // A group's own library, opened from a chat. The overview of every document lives in
  // Settings, so this view always belongs to one group.
  | { kind: "library"; gid: string }
  | { kind: "appearance" };

/** The Tools section of the sidebar: each entry is its own page.
 *
 *  Prompts, the library and memory are deliberately *not* here — they are Settings tabs, which
 *  is where the rest of the configuration lives. External agents and chat channels are the
 *  other way round: they are things you set up once and then keep an eye on, so they sit here
 *  rather than behind a settings menu.
 */
const TOOL_NAV: { kind: "skills" | "plugins" | "mcp" | "hooks" | "external" | "channels"; label: string; icon: typeof Sparkles }[] = [
  { kind: "skills", label: "Skills", icon: Sparkles },
  { kind: "plugins", label: "Plugins", icon: Puzzle },
  { kind: "mcp", label: "MCP", icon: Plug },
  { kind: "hooks", label: "Hooks", icon: Anchor },
  { kind: "external", label: "External agents", icon: TerminalSquare },
  { kind: "channels", label: "Chat channels", icon: Webhook },
];

/** Which projects the list shows. Three states and no fourth: they are the whole of it, so the three
 *  numbers add up to the number of projects — a tab called "All" that quietly left the archived ones
 *  out read as an arithmetic error. */
type ProjectState = "running" | "active" | "done" | "archived";

/** The states, in the order they are drawn — as a *filter* you tick, not a row of tabs.
 *
 *  "Running now" comes first because it is the one state you cannot work out from the list itself —
 *  a project in the middle of a turn looks exactly like an idle one otherwise. It is *not* a fourth
 *  place a project can be: it is a live subset of "In progress" (the three reaching-to-the-end states
 *  still add up to the number of projects). Ticking it together with "In progress" is therefore
 *  harmless — the union is "In progress".
 *
 *  They are a filter and not a heading plus four tabs in the panel body, which is what the user asked
 *  for: the list stays the list, the states sit behind the button in the top row next to the search
 *  that narrows it by text, and an empty selection means every project — archived ones included, the
 *  one thing a row of tabs could not say. */
const PROJECT_STATES: { id: ProjectState; label: string; empty: string; title?: string }[] = [
  { id: "running", label: "Running now", empty: "Nothing is running right now",
    title: "A project with a turn in flight — it is working at this moment" },
  { id: "active", label: "In progress", empty: "No projects in progress" },
  { id: "done", label: "Completed", empty: "Nothing completed yet" },
  { id: "archived", label: "Archived", empty: "Nothing archived yet" },
];

/** One project's state, as the filter reads it — the single place that decides what each state means. */
const inState = (g: Group, s: ProjectState): boolean =>
  s === "archived" ? !!g.archived
    : s === "running" ? !g.archived && !!g.busy
      : s === "done" ? !g.archived && g.status === "done"
        : !g.archived && g.status !== "done";   // "active"

interface Props {
  view: View;
  onView: (v: View) => void;
  onSettings: (tab: SettingsTab) => void;
  /** True when the panel is collapsed to its top strip: only the toggle is drawn, and it does not move */
  collapsed: boolean;
  /** Collapse it, or bring it back — one control, always in the same spot */
  onCollapse: () => void;
  version: string;
  /** Whether the member column is showing beside the sidebar */
  rail: boolean;
  /** Show or hide that column */
  onRail: () => void;
  /** How many members the group that column would list has, or null when no group has been opened */
  memberCount: number | null;
}

export default function Sidebar({ view, onView, onSettings, collapsed, onCollapse, version, rail, onRail, memberCount }: Props) {
  const { groups, settings, online, reload, reloadGroups, updateCount, appUpdateCount } = useData();
  const confirm = useConfirm();
  const { t } = useI18n();
  const [open, setOpen] = useState(true);
  const [searchOpen, setSearchOpen] = useState(false);
  const [menu, setMenu] = useState(false);
  // Which states the list is showing. One state ticked by default ("In progress"), and an empty
  // selection means every project — archived ones included — the way a filter with nothing applied
  // behaves everywhere else.
  const [picked, setPicked] = useState<ProjectState[]>(["active"]);
  const [picking, setPicking] = useState(false);
  const menuRef = useOutside<HTMLDivElement>(menu, () => setMenu(false));
  const pickRef = useOutside<HTMLDivElement>(picking, () => setPicking(false));

  const list = useMemo(() => {
    const rows = [...groups].sort((a, b) => (b.last_at ?? 0) - (a.last_at ?? 0));
    // An empty filter means every project, archived ones included — the way a filter with nothing
    // applied behaves everywhere else. Searching for text is the floating box's job, not this list's:
    // the hits belong in the box, not in a list that also has to stay readable while navigating.
    if (picked.length === 0) return rows;
    return rows.filter((g) => picked.some((x) => inState(g, x)));
  }, [groups, picked]);

  /** How many projects are in each state — the same rows the filter then shows, counted once. */
  const counts = useMemo(() => {
    const live = groups.filter((g) => !g.archived);
    return {
      total: groups.length,
      running: live.filter((g) => g.busy).length,
      active: live.filter((g) => g.status !== "done").length,
      done: live.filter((g) => g.status === "done").length,
      archived: groups.filter((g) => g.archived).length,
    };
  }, [groups]);

  const activeGid = view.kind === "chat" ? view.gid : null;
  const offline = settings ? !settings.external_calls_enabled : false;

  /** Mark a project done, or put it back to in progress. One click, no dialog: it is a note to
   *  self, and the state is written on the row where it can be seen. */
  const markDone = async (g: Group) => {
    await api.patchGroup(g.id, { status: g.status === "done" ? "active" : "done" });
    await reloadGroups();
  };

  /** File a project away, or take it out again. Archiving asks first and says where the project
   *  goes; the request is deliberately not caught, because a refusal (the project is working right
   *  now) comes back as a 409 whose message the global Toaster shows — the reason matters more than
   *  a silently swallowed error. */
  const setArchived = async (g: Group, on: boolean) => {
    if (on && !(await confirm(
      t('File this project away? It stays searchable under "Archived", with its workspace and history.'),
      { okText: t("Archive"), danger: false },
    ))) return;
    await api.patchGroup(g.id, { archived: on });
    await reloadGroups();
  };

  const remove = async (id: string, name: string) => {
    if (!(await confirm(t('Delete the group chat "{name}" and all of its messages?', { name }), { okText: t("Delete") }))) return;
    await api.delGroup(id);
    await reloadGroups();
    if (activeGid === id) onView({ kind: "home" });
  };

  return (
    <aside className={"sidebar" + (collapsed ? " collapsed" : "")}>
      {/* One header row, title left and actions right — the arrangement every other header in this
          app uses (the chat header, the group-list header). It used to read:
          [82px traffic-light gutter][collapse] [91px of nothing] [search][new] — one button parked in
          the middle and two at the far edge, so the row looked split, and the app's name sat on a
          second line indented to a third value (three left edges for three rows).
          The 82px is not decoration: on macOS the window's traffic lights sit over this corner.
          The version badge moved into the name's tooltip — measured, the row is 279px wide, the
          gutter takes 82, and the name plus a badge plus two buttons comes to 309. It is one hover
          away here and always visible in Settings → About.
          The "+" that used to sit here is gone: "New group chat" is a page in the list below with a
          label and a selected state, and two entries for one action 20px apart is the confusion this
          app keeps having to fix. */}
      {/* The window's own controls on the top line, the app's name on the next — and when the panel is
          collapsed this row is all that is left of it, with the button in the same place.
          It used to be one row (name, then two buttons at the far right), and collapsing unmounted the
          whole sidebar, which put the way back at the top-*left* of the main area: the button you
          pressed sat at x=203 and the one that brought it back at x=84, so pressing where you had just
          pressed hit the page behind it and nothing happened. One control, one spot, both directions. */}
      {/* The click-outside guard is on this whole row, not on the panel: the button that opens the
          panel has to be *inside* it, or pressing it again would close (mousedown) and immediately
          reopen (click) — a popover you cannot close with its own button. The search button closes it
          by hand for the same reason it is in this row: it draws its field below this row. */}
      <div className="side-top drag" ref={pickRef}>
        <button className="icon-btn nodrag" title={t(collapsed ? "Expand the sidebar" : "Collapse sidebar")}
          aria-label={t(collapsed ? "Expand the sidebar" : "Collapse sidebar")} aria-expanded={!collapsed}
          onClick={onCollapse}>
          {collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
        </button>
        {/* Both of these belong to the open panel: search reveals a field inside the sidebar, and the
            filter narrows what is below it — with the panel collapsed to one line there is nowhere for
            either to go. The filter is *on* (accented) while a subset is picked, so "why is a project
            missing" has an answer you can see without opening anything. */}
        {!collapsed && (
          <button className={"icon-btn nodrag filter-btn" + (picked.length > 0 ? " on" : "")}
            title={t("Filter projects")} aria-label={t("Filter projects")} aria-expanded={picking}
            onClick={() => setPicking((p) => !p)}>
            <ListFilter size={17} />
          </button>
        )}
        {!collapsed && (
          <button className="icon-btn nodrag" title={t("Search group chats")} aria-label={t("Search group chats")}
            aria-haspopup="dialog" onClick={() => { setPicking(false); setSearchOpen(true); }}><Search size={17} /></button>
        )}
        {/* Anchored under this row and drawn over the list rather than pushing it down: opening the
            filter must not move the project you were about to click. */}
        {!collapsed && picking && (
          <div className="side-filter" role="group" aria-label={t("Filter projects")}>
            <button className="sf-row sf-all" aria-checked={picked.length === 0} role="menuitemcheckbox"
              onClick={() => setPicked([])}>
              <span className="sf-tick">{picked.length === 0 && <Check size={12} />}</span>
              {t("All projects")}
              <span className="n">{counts.total}</span>
            </button>
            <div className="sf-sep" />
            {PROJECT_STATES.map((st) => {
              const on = picked.includes(st.id);
              return (
                <button key={st.id} className="sf-row" role="menuitemcheckbox" aria-checked={on}
                  title={st.title ? t(st.title) : undefined}
                  onClick={() => setPicked((p) => on
                    ? p.filter((x) => x !== st.id)
                    : PROJECT_STATES.filter((x) => x.id === st.id || p.includes(x.id)).map((x) => x.id))}>
                  <span className="sf-tick">{on && <Check size={12} />}</span>
                  {t(st.label)}
                  <span className="n">{counts[st.id]}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>
      <div className="brand-row">
        <span className="brand-name" title={`Team Agent v${version}`}>Team Agent</span>
      </div>

      {/* 导航(新建群聊 / 成员 / 工具)固定在名字下面,项目列表在它们下面 —— 和这一版最早的排法一致 */}
      <nav className="side-nav">
        <button className={"nav-item" + (view.kind === "home" ? " on" : "")} onClick={() => onView({ kind: "home" })}>
          <MessageSquarePlus size={16} /> {t("New group chat")}
        </button>
        {/* One entry, one meaning: it opens the member column beside the sidebar, which lists
            *this* group's members — the same set this count is taken from. It used to open the
            app-wide Members page while printing this group's count, so the number and the list
            behind it were about two different things. Every member of the app is one link away,
            in that column's footer. */}
        <button
          className={"nav-item" + (rail ? " on" : "")}
          aria-expanded={rail}
          title={memberCount === null
            ? t("Open a group chat first: this shows the members of the group you are in")
            : t("Show this group's members in the column beside the sidebar")}
          onClick={onRail}
        >
          <Users size={16} /> {t("Members")}{memberCount !== null && <span className="count">({memberCount})</span>}
        </button>
      </nav>
      <div className="nav-cap">{t("Tools")}</div>
      <nav className="side-nav" aria-label={t("Tools")}>
        {TOOL_NAV.map((item) => (
          <button key={item.kind} className={"nav-item" + (view.kind === item.kind ? " on" : "")} onClick={() => onView({ kind: item.kind } as View)}>
            <item.icon size={16} /> {t(item.label)}
          </button>
        ))}
      </nav>

      {/* 导航在上、项目列表在下 —— the order this panel started with, and the one the user asked to
          have back: the tools you reach for are fixed at the top, and the project list is the part
          that grows and scrolls underneath them (`.side-list` is the flex:1 row) instead of pushing
          them off the bottom. The heading carries the count and folds the list away. */}
      <button className="side-section" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />} {t("Projects")} <span className="count">({list.length})</span>
      </button>
      <div className="side-list">
        {open && list.map((g) => (
          <div key={g.id} className={"conv" + (g.id === activeGid ? " on" : "") + (g.archived ? " arch" : "")}>
            <button className="conv-main" onClick={() => onView({ kind: "chat", gid: g.id })}>
              {g.busy && <span className="conv-run" title={t("Working right now")} aria-label={t("Working right now")} />}
              <span className="conv-name">{g.name}</span>
              <span className={"conv-state" + (g.archived ? "" : g.status === "done" ? " done" : "")}>
                {t(g.archived ? "Archived" : g.status === "done" ? "Completed" : "In progress")}
              </span>
              <span className="conv-time">{relTime(g.last_at)}</span>
            </button>
            <div className="conv-acts">
              <button className="icon-btn tiny" title={g.status === "done" ? t("Mark as not done") : t("Mark as done")}
                aria-label={t("Mark as not done")} disabled={g.archived}
                onClick={() => void markDone(g)}>
                {g.status === "done" ? <Undo2 size={12} /> : <Check size={12} />}
              </button>
              <button className="icon-btn tiny" title={g.archived ? t("Unarchive") : t("Archive")}
                aria-label={g.archived ? t("Unarchive") : t("Archive")} onClick={() => void setArchived(g, !g.archived)}>
                {g.archived ? <ArchiveRestore size={12} /> : <Archive size={12} />}
              </button>
              <button className="icon-btn tiny" title={t("Delete group chat")} aria-label={t("Delete group chat {name}", { name: g.name })} onClick={() => void remove(g.id, g.name)}><Trash2 size={12} /></button>
            </div>
          </div>
        ))}
        {open && list.length === 0 && (
          <div className="side-empty">
            {groups.length === 0 ? t("No group chats yet")
              : picked.length === 1 ? (PROJECT_STATES.find((x) => x.id === picked[0])?.empty ?? "No projects match this filter")
                : t("No projects match this filter")}
          </div>
        )}
      </div>

      <div className="user-wrap" ref={menuRef}>
        {menu && (
          <div className="user-menu" role="menu">
            <button role="menuitem" onClick={() => { setMenu(false); onSettings("providers"); }}><Settings size={15} /> {t("Settings")}</button>
            <button role="menuitem" onClick={() => { setMenu(false); onView({ kind: "appearance" }); }}><Palette size={15} /> {t("Appearance")}</button>
            <div className="menu-row">
              <WifiOff size={15} /> <span>{t("Offline mode")}</span>
              <Switch
                checked={offline}
                label={t("Offline mode")}
                onChange={async (v) => {
                  await api.putSettings({ external_calls_enabled: !v });
                  await reload();
                }}
              />
            </div>
            <button role="menuitem" onClick={() => { setMenu(false); onSettings("discover"); }}><Compass size={15} /> {t("Discover")}{updateCount - appUpdateCount > 0 && <span className="count-badge" style={{ marginLeft: "auto" }}>{updateCount - appUpdateCount}</span>}</button>
            <button role="menuitem" onClick={() => { setMenu(false); onSettings("version"); }}><Package size={15} /> {t("Software update")}{appUpdateCount > 0 && <span className="count-badge" style={{ marginLeft: "auto" }}>{appUpdateCount}</span>}</button>
            <button role="menuitem" onClick={() => { setMenu(false); onSettings("about"); }}><Info size={15} /> {t("About")}</button>
          </div>
        )}
        <button className="user-row" onClick={() => setMenu((m) => !m)} aria-haspopup="menu" aria-expanded={menu}>
          <span className="user-ava">{t("Me")}</span>
          <span className="user-meta">
            <span className="user-name">{t("Local user")}</span>
            <span className="user-sub"><i className={"dot " + (!online ? "bad" : offline ? "off" : "ok")} />{t(!online ? "Backend not connected" : offline ? "Offline mode" : "Hosted + local")}</span>
          </span>
          {updateCount > 0 ? <span className="count-badge" title={t("Updates available")}>{updateCount}</span> : <Settings size={16} className="user-gear" />}
        </button>
      </div>

      {/* 搜索框浮在窗口中间,和侧栏是两回事 —— 它由这一行的查找按钮打开 */}
      {searchOpen && (
        <SearchPalette groups={groups} visible={list} activeGid={activeGid}
          onOpen={(gid) => { setSearchOpen(false); onView({ kind: "chat", gid }); }}
          onClose={() => setSearchOpen(false)} />
      )}
    </aside>
  );
}
