import { useMemo, useState } from "react";
import { Activity, Anchor, Archive, ArchiveRestore, CheckCircle2, ChevronDown, ChevronRight, CircleDashed, Clapperboard, Compass, Folder, FolderPen, Info, Layers, ListFilter, MessageSquarePlus, Package, PanelLeftClose, PanelLeftOpen, Palette, PencilLine, Plug, Puzzle, Search, Settings, Sparkles, TerminalSquare, Trash2, Users, Webhook, WifiOff } from "lucide-react";
import { api, relTime, type Group } from "../api";
import { humanBytes, NAME_MAX, shortName } from "../lib";
import { useData } from "../data";
import { Switch, useConfirm, useOutside } from "../ui";
import { useI18n } from "../i18n";
import BrandMark from "./BrandMark";
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
  // 视频专区:场景、表情动作、音乐生成在一起,由**用户自己定义并挑选**。它是页面而不是设置
  // 面板 —— 那三样是「做东西」,不是「配置东西」,放设置里会让人以为要先配什么才能用。
  | { kind: "video-zone" }
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
const TOOL_NAV: { kind: "video-zone" | "skills" | "plugins" | "mcp" | "hooks" | "external" | "channels"; label: string; icon: typeof Sparkles }[] = [
  // 排在最前:它是这三样里唯一"点进去就能做出东西"的入口。
  { kind: "video-zone", label: "Video zone", icon: Clapperboard },
  { kind: "skills", label: "Skills", icon: Sparkles },
  { kind: "plugins", label: "Plugins", icon: Puzzle },
  { kind: "mcp", label: "MCP", icon: Plug },
  { kind: "hooks", label: "Hooks", icon: Anchor },
  { kind: "external", label: "Agents and local tools", icon: TerminalSquare },
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
const PROJECT_STATES: { id: ProjectState; label: string; empty: string; icon: typeof Sparkles; hint?: string }[] = [
  // "Running now" is the one chip whose word needs explaining: it is not a fourth place a project can
  // be, it is the live subset of "In progress", so the hint says so where the number is read.
  { id: "running", label: "Running now", empty: "Nothing is running right now", icon: Activity,
    hint: "A project with a turn in flight — it is working at this moment" },
  { id: "active", label: "In progress", empty: "No projects in progress", icon: CircleDashed },
  { id: "done", label: "Completed", empty: "Nothing completed yet", icon: CheckCircle2 },
  { id: "archived", label: "Archived", empty: "Nothing archived yet", icon: Archive },
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

/** 第二行那个状态词 —— 用户在 2026-09-25 要的就是「显示运行状态」。
 *
 *  这里以前是一个**点**:它有颜色、可读名里也有状态,但没读过的人看不出那是什么意思。
 *  用户要的是读得出来的东西,所以点换成了词,颜色留给词。
 *
 *  ⚠️ 顺序有意:一个回合正在跑(`busy`,是**进程事实**)优先于任务板自己的状态 —— 板子可能停在
 *  上一轮留下的 `running` 上,而这一秒到底有没有人在干活,只有 `busy` 知道。
 */
function taskState(g: Group): string | null {
  if (!g.task) return null;                       // 一次都没跑过时,标题自己会说明
  if (g.busy) return "Running now";
  // ⚠️ `board` 是空串 = 这一行显示的不是任务板,而是「最近做了什么」(`planner.said_headline`)。
  // 那种行**没有任务状态可说** —— 硬套一个状态词等于把一句发言说成一个任务。
  if (!g.task.board) return null;
  if (g.task.board === "failed") return "Failed";
  if (g.task.board === "stopped") return "Stopped";
  const s = g.task.status || "";
  return { running: "Running now", pending: "Queued", done: "Done",
           failed: "Failed", stopped: "Stopped", skipped: "Skipped" }[s] || null;
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
  // 折叠成一行的那几个项目(只可能有一个是「当前」的,见行里的 `canFold`)。放在这里而不是行里:
  // 行是每次 render 重建的,状态放进去一 re-render 就没了。
  const [folded, setFolded] = useState<string[]>([]);
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

  /** 打开项目文件夹(交给主进程,因为渲染进程开不了访达)。 */
  const openFolder = async (g: Group) => {
    const open = window.teamAgent?.openPath;
    if (!open || !g.folder?.path) return;
    const got = await open(g.folder.path);
    if (!got.ok) await confirm(t("That folder could not be opened ({why}). It may have been moved or deleted.", { why: got.why ?? "" }), { okText: t("OK"), danger: false });
  };

  /** 给**项目**改名(用户 2026-09-25:点项目时右侧要有「归档 / 重命名 / 删除」)。
   *
   *  ⚠️ 这和下面那个「重命名文件夹」是两件事:项目名是列表里那个标签(自动取的关键词不一定合意,
   *  所以要让用户改得动),文件夹名是磁盘上的目录。改名字**只截到 `NAME_MAX`** —— 用户要的就是
   *  「限制 8 个字以内」,让他敲进去 20 个字然后列表里只显示 8 个,等于把这个限制藏起来。
   */
  const renameProject = async (g: Group) => {
    const want = window.prompt(t("New name for this project (up to {n} characters)", { n: String(NAME_MAX) }), g.name);
    if (want === null) return;
    const name = want.trim().slice(0, NAME_MAX);
    if (!name || name === g.name) return;
    await api.patchGroup(g.id, { name });
    await reloadGroups();
  };

  /** 改名改的是**磁盘上的文件夹**,所以只有用户自己挑的目录才允许 —— 程序管理的目录就是群的 id。 */
  const renameFolder = async (g: Group) => {
    const before = g.folder?.name ?? "";
    const want = window.prompt(t("New name for this folder"), before);
    if (want === null || !want.trim() || want.trim() === before) return;
    try {
      await api.renameFolder(g.id, want.trim());
    } catch (e) {
      await confirm((e as Error).message, { okText: t("OK"), danger: false });
    }
    await reloadGroups();
  };

  /** 清空文件夹:先问后端里面有多少东西,再用这些数字问用户 —— 删掉的是这个项目里所有产出。 */
  const clearFolder = async (g: Group) => {
    let info = null;
    try {
      info = await api.folder(g.id);
    } catch {
      /* 拿不到就先按「不知道」问 */
    }
    const size = info ? humanBytes(info.bytes) : "";
    const agreed = await confirm(
      t('Move this project\'s folder to the Trash?\n\n{path}\n{files} files, {size}.\n\nNothing is deleted outright — it goes to the Trash, so you can put it back.', {
        path: g.folder?.path ?? "", files: String(info ? (info.files ?? 0) : "?"), size,
      }),
      { okText: t("Move to Trash") });
    if (!agreed) return;
    try {
      await api.deleteFolder(g.id, info ? (info.files ?? null) : null);
    } catch (e) {
      await confirm((e as Error).message, { okText: t("OK"), danger: false });
    }
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
      {/* The panel's header, in two bands, and **52px tall on purpose**: the two headers beside it —
          the members column's (`.mrail-head`) and the chat's (`.chat-head`) — are both 52px with a
          bottom border, and this block ends on the same line so the three columns open with one
          continuous edge. On macOS the window's traffic lights occupy the top 26px of this corner, so
          the buttons centre in what is left below them rather than in the whole block.
          Under it: the mark, the name centred, the version on its own line. The version used to be a
          tooltip — measured, it did not fit the single 279px row beside the name and two buttons —
          and it fits here because the name has a line of its own and the room to be centred.
          The band above holds three buttons and nothing else: the "+" that once sat here is gone,
          because "New group chat" is a page in the list below with a label and a selected state, and
          two entries for one action 20px apart is the confusion this app keeps having to fix.
          Collapsed, this band is all that is left of the panel, with the button in the same place. It
          used to unmount the whole sidebar, which put the way back at the top-*left* of the main area
          — the button you pressed sat at x=203 and the one that brought it back at x=84, so pressing
          where you had just pressed hit the page behind it and nothing happened. One control, one
          spot, both directions. */}
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
        {/* One row of icon chips, not a stacked menu. It hangs from the top row and is drawn over the
            list rather than pushing it down, so opening it never moves the project you were about to
            click. Each chip carries its own count and its name in the tooltip: the words ("in
            progress", "archived") are a whole sentence each and four of them stacked was a block the
            size of the list it filters. An empty selection means every project, archived ones
            included — the way a filter with nothing applied behaves everywhere else. */}
        {!collapsed && picking && (
          <div className="side-filter" role="group" aria-label={t("Filter projects")}>
            <button className={"sf-chip sf-all" + (picked.length === 0 ? " on" : "")} role="menuitemcheckbox"
              aria-checked={picked.length === 0} title={`${t("All projects")} · ${counts.total}`}
              onClick={() => setPicked([])}>
              <Layers size={13} /><span className="n">{counts.total}</span>
            </button>
            {PROJECT_STATES.map((st) => {
              const on = picked.includes(st.id);
              return (
                <button key={st.id} className={"sf-chip" + (on ? " on" : "")} role="menuitemcheckbox" aria-checked={on}
                  title={`${t(st.label)} · ${counts[st.id]}${st.hint ? " — " + t(st.hint) : ""}`}
                  onClick={() => setPicked((p) => on
                    ? p.filter((x) => x !== st.id)
                    : PROJECT_STATES.filter((x) => x.id === st.id || p.includes(x.id)).map((x) => x.id))}>
                  <st.icon size={13} /><span className="n">{counts[st.id]}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>
      <div className="brand-row">
        <div className="brand-line">
          {/* One mark for the whole app: the same component the home page and About put in their
              coloured tile, and the same geometry the app icon is drawn from. */}
          <BrandMark className="brand-logo" size={20} />
          <span className="brand-name">Team Agent</span>
          {/* 版本号跟在名字后面,不另起一行。它原来单独占一行,是因为那时这一行还要挤下两个按钮
              (实测名字 + 徽标 + 两个按钮塞不进 279px);按钮后来搬到了上面一行,这一行只剩
              「图标 + 名字」,所以版本号跟着名字读是最自然的写法。 */}
          <span className="brand-ver">{`v${version}`}</span>
        </div>
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
          <Users size={16} /> {t("Members and tools")}{memberCount !== null && <span className="count">({memberCount})</span>}
        </button>
      </nav>
      <div className="nav-cap">{t("Capability center")}</div>
      <nav className="side-nav" aria-label={t("Capability center")}>
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
        {open && list.map((g) => {
          // Two lines per project, always — and the folder gets a line of its own only when its name
          // is a *different* name from the project's.
          //
          // That is the whole rearrangement. An app-managed directory is named after the group's own
          // id and displayed as the group's name, so the old third line repeated the first one word
          // for word ("视频制作" over "📁 视频制作"), and a row stood three bands tall with six icon
          // buttons in it. WorkBuddy's panel reads calmly because a row is a *place* and what sits
          // under it is one recent session: a mark and a name on the first line, that session on the
          // next. Same two lines here; the folder name joins the first line only when it has
          // something of its own to say.
          const dir = g.folder?.name && g.folder.name !== g.name ? g.folder : null;
          const st = g.archived ? "Archived" : g.status === "done" ? "Completed" : "In progress";
          const stateCls = g.archived ? "arch" : g.status === "done" ? "done" : "active";
          const state = taskState(g);
          // 这一行读的是一个**任务板**,还是一句「最近做了什么」?两者形状一样(都来自后端同一格),
          // 靠 `board` 是否为空区分 —— 前端只需要知道「要不要显示完成数」。
          const hasBoard = !!(g.task && g.task.board);
          // 折叠:这一个项目只留第一行(项目名),第二行「它在做什么」收起来。用户 2026-09-25 要的
          // 是 WorkBuddy 那种「项目名后面一个可折叠的下拉符号,点这个项目的时候才出现」。
          // ⚠️ 只有**当前打开的那一个**项目带这个符号(用户原话),所以它不是每行都有的控件 ——
          // 一列里二十个箭头既吵,也没人知道它们各自折的是什么。
          const canFold = g.id === activeGid;
          const foldedNow = canFold && folded.includes(g.id);
          return (
          <div key={g.id} className={"conv" + (g.id === activeGid ? " on" : "") + (g.archived ? " arch" : "")
                                      + (canFold ? " has-fold" : "")}>
            {/* Line 1: the place. The dot is the state — a word on every row was four words per
                screen that were almost always the same one — then the mark, then the name. */}
            <div className="conv-head">
            {/* 两个动作、两个按钮:**点文件夹 = 进那个目录**(用户 2026-09-25:「workbuddy 直接点击
                文件夹就可以进入对应的目录」,交给主进程开访达),点名字那一块 = 进群聊。它们不能互相
                嵌套(button 里套 button 是非法结构,而且点谁算谁就说不清了),所以这一行多了一层。
                ⚠️ 没有外壳时(浏览器里)图标就是个 span:一个点了没反应的按钮不是入口。 */}
            <div className="conv-row">
            {window.teamAgent?.openPath && g.folder?.path ? (
              <button className={"cv-ico cv-open s-" + stateCls} title={g.folder?.path}
                aria-label={`${t("Open the folder")} · ${t(st)}`}
                onClick={() => void openFolder(g)}>
                <Folder size={13} aria-hidden />
                {g.busy && <span className="conv-run" title={t("Working right now")} aria-label={t("Working right now")} />}
                {/* ⚠️ 「正在工作」那一点是**这个图标的角标**(见 .conv-run 的定位),不是行里的一个独立
                    元素:它原来排在名字前面,占 7px + 8px 间距 —— 于是那个项目在跑的时候,它的名字被
                    推右 15px、和自己的任务行错开一格(两行对齐到同一列是这个列表唯一的层次线索)。 */}
              </button>
            ) : (
              /* ⚠️ 没有外壳时这仍然是个 span,但 **title 必须是路径** —— 那是「这个项目落在磁盘哪里」
                 的唯一出口(有外壳时也一样,只是多了一个可点的动作)。状态进 aria-label。
                 ⚠️ 这里**不能**用 JSX 那套大括号注释:它在这个三元表达式的括号里是非法语法,构建会报
                 「逗号或右括号 expected」而 dist 根本不更新 —— 我为此白跑了两轮冒烟。
                 ⚠️ 也**别在这段注释里写注释的结束符号**:写了就把注释提前闭合,同样报那一句。 */
              <span className={"cv-ico s-" + stateCls} title={g.folder?.path} aria-label={t(st)}>
                <Folder size={13} aria-hidden />
                {g.busy && <span className="conv-run" title={t("Working right now")} aria-label={t("Working right now")} />}
              </span>
            )}
            <button className="conv-main" onClick={() => onView({ kind: "chat", gid: g.id })}>
              {/* ⚠️ 这里原来还有一个「状态点」(灰点/绿点)。2026-09-25 codex 读实拍图时点名了它:
                  「每项开头都是文件夹 + 灰点 + 名字,其中重复灰点在视觉上最显多余」。于是状态**并进
                  文件夹图标**(四个状态各一种颜色),信息一个没少、元素少一个;正在跑的仍然单独一个
                  会呼吸的点 —— 那是唯一自己会变的那个状态,值得显眼。 */}
              {/* ⚠️ 名字太长会把这一行撑满、压到右边的控件上(用户 2026-09-25 的原话是
                  「项目的名称把下面功能键挡住了」)。列表里只显示前 10 个字,完整名字进 title。 */}
              <span className="conv-name" title={g.name}>{shortName(g.name)}</span>
              {dir && <span className="conv-dir" title={g.folder!.path}>{`· ${dir.name}`}</span>}
            </button>
            </div>
            {canFold && (
              <button className="icon-btn tiny cv-fold" aria-expanded={!foldedNow}
                title={t(foldedNow ? "Show what this project is doing" : "Show just the project name")}
                aria-label={t(foldedNow ? "Show what this project is doing" : "Show just the project name")}
                onClick={() => setFolded((cur) => (cur.includes(g.id) ? cur.filter((x) => x !== g.id) : [...cur, g.id]))}>
                {foldedNow ? <ChevronRight size={12} aria-hidden /> : <ChevronDown size={12} aria-hidden />}
              </button>
            )}
            {/* 三个动作:**重命名 / 归档 / 删除**(用户 2026-09-25 点的名,顺序也照他说的)。
                ⚠️ 顺序改过一次:原来是「归档 / 重命名 / 删除」,用户 2026-09-25 第二轮明确
                「重命名放在第一,归档放在第二,删除放第三个」—— 最常用的那个排最前。
                ⚠️ 「标记为已完成」那个勾**去掉了**(用户:「只要那三个,完成不要了」)。代价说清楚过:
                侧栏顶部那四档里的「已完成」从此没有手动入口,所以那一档会一直空着。
                出现时机也是用户要的:「点击项目时右侧出现三个按钮」→ 当前这个项目**常显**
                (不靠悬停),别的项目悬停时才让位给它们(见 `.conv.on .conv-acts`)。 */}
            <div className="conv-acts">
              <button className="icon-btn tiny cv-rename" title={t("Rename this project")}
                aria-label={t("Rename this project {name}", { name: g.name })}
                onClick={() => void renameProject(g)}><PencilLine size={12} /></button>
              <button className="icon-btn tiny" title={g.archived ? t("Unarchive") : t("Archive")}
                aria-label={g.archived ? t("Unarchive") : t("Archive")} onClick={() => void setArchived(g, !g.archived)}>
                {g.archived ? <ArchiveRestore size={12} /> : <Archive size={12} />}
              </button>
              <button className="icon-btn tiny danger" title={t("Delete group chat")} aria-label={t("Delete group chat {name}", { name: g.name })} onClick={() => void remove(g.id, g.name)}><Trash2 size={12} /></button>
            </div>
            </div>
            {/* Line 2: what this place is doing — the task its board is summarised by, its progress,
                and when it last moved. The folder's own three actions live here too (they act on the
                very directory this line is about) and appear on hover, so a row is two lines whether
                or not anyone is pointing at it. 这个目录和群聊里的工作空间是**同一个**(后端只解析一次,
                见 `Store.workspace_path`)。 */}
            {!foldedNow && <div className={"conv-task " + (g.task?.status || "pending")}>
              <button className="ct-main" onClick={() => onView({ kind: "chat", gid: g.id })}
                title={g.task
                  ? (hasBoard ? t("{owner} · {done}/{total} done", { owner: g.task.owner, done: g.task.done, total: g.task.total })
                              : t("Last thing said by {who}", { who: g.task.owner }))
                  : undefined}>
                <span className="ct-title">{g.task ? g.task.title : t("No task board yet")}</span>
                {/* ⚠️ 完成数只属于任务板。没有板时这一格显示的是**一句发言的摘要**,给它配一个
                    「3/8」会让人以为那是一个任务的进度。 */}
                {g.task && hasBoard && <span className="ct-owner">{`${g.task.done}/${g.task.total}`}</span>}
                {/* ⚠️ 状态词在**右边这组元信息里**,不在标题前面。它原来那个点(6px)是悬挂在文字列
                    之外的,换成词以后比点宽得多,摆在前面就把标题从「项目名那一列」推开了 —— 而那一列
                    是两行之间唯一读得出层次的东西(冒烟按几何断言它)。 */}
                {state && <span className="ct-state">{t(state)}</span>}
              </button>
              <span className="ct-time">{relTime(g.last_at)}</span>
              {g.folder?.path && (
                <span className="cf-acts">
                  <button className="icon-btn tiny cf-rename" title={g.folder.mine ? t("Rename this folder") : t("This folder is managed by the app, so its name is not yours to change")}
                    aria-label={t("Rename the folder")} disabled={!g.folder.mine} onClick={() => void renameFolder(g)}><FolderPen size={11} /></button>
                  <button className="icon-btn tiny danger cf-empty" title={t("Move this folder to the Trash")} aria-label={t("Empty the folder")} onClick={() => void clearFolder(g)}><Trash2 size={11} /></button>
                </span>
              )}
            </div>}
          </div>
          );
        })}
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
