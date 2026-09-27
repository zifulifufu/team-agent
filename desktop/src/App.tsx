import { useCallback, useEffect, useRef, useState, type ComponentType } from "react";
import { useData } from "./data";
import { useI18n } from "./i18n";
import { prefs } from "./theme";
import { APP_VERSION } from "./lib";
import { Toaster, notify } from "./ui";
import { api } from "./api";
import Sidebar, { type View } from "./components/Sidebar";
import MemberRail from "./components/members/MemberRail";
import OutputsPanel from "./components/outputs/OutputsPanel";
import HomePage from "./pages/HomePage";
import ChatView from "./pages/ChatView";
import AgentsPage from "./pages/AgentsPage";
import LibraryPage from "./pages/LibraryPage";
import VideoZonePage from "./pages/VideoZonePage";
import AppearancePage from "./settings/AppearancePage";
import CapabilityWorkspace, { type CapabilityArea } from "./settings/CapabilityWorkspace";
import SkillsPage from "./settings/SkillsPage";
import PluginsPage from "./settings/PluginsPage";
import McpPage from "./settings/McpPage";
import ExternalPage from "./settings/ExternalPage";
import ChannelsPage from "./settings/ChannelsPage";
import HooksPage from "./settings/HooksPage";
import SettingsModal, { type PageProps, type SettingsTab } from "./settings/SettingsModal";

/** The pages that are the main area rather than a tab inside Settings. Prompts, the library
 *  and memory are the other way round: they are Settings tabs, so a link to one of them keeps
 *  Settings open and just switches tab (see `goTab`). */
const AREA_PAGES: Record<"skills" | "plugins" | "mcp" | "hooks" | "external" | "channels", ComponentType<PageProps>> = {
  skills: SkillsPage, plugins: PluginsPage, mcp: McpPage, hooks: HooksPage, external: ExternalPage,
  channels: ChannelsPage,
};

/** Settings ids that name one of those pages, as the `View` that shows it. */
const AREA_VIEWS: Partial<Record<SettingsTab, View>> = {
  skills: { kind: "skills" }, plugins: { kind: "plugins" }, mcp: { kind: "mcp" },
  hooks: { kind: "hooks" }, external: { kind: "external" }, channels: { kind: "channels" },
  appearance: { kind: "appearance" },
};

export default function App() {
  const { t } = useI18n();
  const { online, groups, epoch } = useData();
  const [view, setView] = useState<View>({ kind: "home" });
  const [settings, setSettings] = useState<SettingsTab | null>(null);
  const [collapsed, setCollapsed] = useState(() => prefs.read("ta.sidebar") === "0");
  // The member column beside the sidebar. It remembers its own choice, and it starts showing
  // because this is where a group's members have always been visible — moving it out of the
  // sidebar was about *where* it is, not about hiding it.
  //
  // The key gained a `.v2` on purpose. The old one was written by builds in which this same toggle
  // meant two other things (a column, then a popover beside the sidebar entry), so whatever it holds
  // is not an instruction about the column as it exists now — and reading it as one is how the user
  // ends up opening the app to find the column they just asked for missing. Nothing is migrated: the
  // first click writes the new key and it persists from then on.
  const [rail, setRail] = useState(() => prefs.read("ta.memberrail.v2") !== "0");
  // **成果栏**挂在聊天区右侧,和成员栏是两件独立的事:它们各自开合、各自记忆。用户给的参照是
  // WorkBuddy 的产物面板 —— 产物属于这次对话,所以它跟着聊天走,而不是挤在左栏右边那一根柱子上
  // (那是它以前的样子,也正是用户要求改掉的那件事)。
  //
  // 默认**开着**:它的存在就是为了「这个项目干出了什么」这句话有地方待,而用户原来的抱怨是它离
  // 聊天太远 —— 默认收起来等于把同一个问题再演一遍。
  const [outPanel, setOutPanel] = useState(() => prefs.read("ta.outputs.v1") !== "0");
  // 哪个面板占满了主区域。**只有一个**:两个面板同时「最大化」在几何上没有意义(主区域只能让一次
  // 位),所以打开一个就关掉另一个。刻意不记 —— 重开应用要看到的是聊天,而不是上次我全屏了谁。
  const [maxed, setMaxed] = useState<"rail" | "outputs" | null>(null);
  // 成果栏数出来的文件数,给它自己头部那个入口用。**一个数只有一处算**(由成果栏回填)。
  const [outCount, setOutCount] = useState<number | null>(null);
  // Which group the column lists. Kept here rather than inside the sidebar so the count printed on
  // the sidebar's "Members" entry and the column it opens cannot be about two different groups —
  // that mismatch is what this change is fixing.
  const [capGroupId, setCapGroupId] = useState<string | null>(null);
  const [lastGid, setLastGid] = useState<string | null>(null);
  useEffect(() => { if (view.kind === "chat") { setLastGid(view.gid); setCapGroupId(view.gid); } }, [view]);
  const railGroup = groups.find((g) => g.id === (view.kind === "chat" ? view.gid : lastGid)) ?? null;
  // One number, printed in two places (the sidebar entry and the column's own subtitle). It used to
  // be two — the sidebar printed this group's count while the entry behind it opened the app-wide
  // list — and the two disagreeing in plain sight is what made the sidebar read as broken.
  const railCount = railGroup ? railGroup.member_ids.length : 0;

  const setSide = (c: boolean) => {
    setCollapsed(c);
    prefs.write("ta.sidebar", c ? "0" : "1");
  };
  const setMemberRail = (on: boolean) => {
    setRail(on);
    prefs.write("ta.memberrail.v2", on ? "1" : "0");
  };
  const setOutputs = (on: boolean) => {
    setOutPanel(on);
    prefs.write("ta.outputs.v1", on ? "1" : "0");
    if (!on) setMaxed((m) => (m === "outputs" ? null : m));
  };
  /**
   * **有成果就自动把这一栏打开**(用户 2026-09-26:「一旦有成果输出自动显示右侧栏」)。
   *
   * 两条判据,缺一条都会变成一个烦人的功能:
   *  1. **只在真的多出文件时开**,不是「这一栏关着就开」。后者意味着每换一个群、每收到一条没有产出的
   *     消息都会把它弹出来 —— 那就成了「关不掉」。所以比的是**上一次看到的条数**,不是「有没有文件」。
   *  2. **只有面板关着时才轮询**。开着的时候它自己按群消息的时间戳刷新(见 OutputsPanel 顶部那段注释),
   *     不需要这儿再敲一次门 —— 那段注释说的「不要一直敲门」正是这个意思。
   * ⚠️ 两处读数必须**互相喂**:面板回填的那个数也要记进这里的基线(见 `takeCount`),否则「关一次再开」
   * 会把基线丢掉,下一次刷新就会被误判成「多了文件」,面板又会自己弹开。
   */
  const seenFiles = useRef<Record<string, number>>({});
  const watchGid = view.kind === "chat" ? view.gid : null;
  const takeCount = useCallback((n: number) => {
    setOutCount(n);
    if (watchGid) seenFiles.current[watchGid] = n;
  }, [watchGid]);
  useEffect(() => {
    if (!watchGid || outPanel) return;
    let live = true;
    const tick = async () => {
      // 窗口看不见时不敲(最小化、切到别的 Space):拿到的结果没人会看到,而且下次可见时那一次
      // tick 照样会把该开的面板开出来 —— 基线留在 ref 里,不会因为跳过而丢掉「多了文件」这件事。
      if (document.hidden) return;
      let n = 0;
      try {
        n = (await api.workspace(watchGid)).files.length;
      } catch {
        return;   // 拿不到就等下一次:这里没有要报给用户的错(面板自己会说明读不了)
      }
      if (!live) return;
      const before = seenFiles.current[watchGid];
      seenFiles.current[watchGid] = n;
      if (before !== undefined && n > before) {
        setOutputs(true);
        notify(t("This group produced {n} new file(s) — shown on the right", { n: n - before }));
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), 6000);
    return () => { live = false; window.clearInterval(timer); };
    // `setOutputs` 与 `t` 每次渲染都是新的,故意不列进依赖:列进去会让定时器每一帧重建。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchGid, outPanel]);
  /** 侧栏那个「成员 (N)」入口:只开成员那一栏。已经开着时再点一下 = 收起(和它一直以来的一键
   *  开合一模一样)。 */
  const onMembersEntry = () => {
    if (view.kind in AREA_PAGES) {
      setView(railGroup ? { kind: "chat", gid: railGroup.id } : { kind: "agents" });
      setMemberRail(true); setMaxed(null); return;
    }
    if (rail) { setMemberRail(false); return; }
    setMemberRail(true);
    setMaxed((m) => (m === "rail" ? null : m));
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
    <div className={"shell" + (maxed && !AreaPage ? " maxed-" + maxed : "")}>
      {/* Always mounted, never unmounted: the sidebar collapses to its own top strip, so the control
          that put it away is still on screen and in the same place to bring it back. When this was
          `{!collapsed && <Sidebar/>}`, the way back lived inside the main area instead — 173px away
          from the button that had just been pressed — and pressing the old spot hit the page behind
          it. A collapse with no visible way back is not a collapse, it is a disappearance. */}
      <Sidebar
        view={view}
        onView={setView}
        onSettings={goTab}
        collapsed={collapsed}
        onCollapse={() => setSide(!collapsed)}
        version={APP_VERSION}
        rail={rail && !AreaPage}
        onRail={onMembersEntry}
        memberCount={railGroup ? railCount : null}
      />
      {!collapsed && rail && !AreaPage && (
        <MemberRail
          group={railGroup}
          count={railCount}
          maximized={maxed === "rail"}
          onMaximize={() => setMaxed((m) => (m === "rail" ? null : "rail"))}
          onOpenAll={() => setView({ kind: "agents" })}
          onClose={() => { setMemberRail(false); setMaxed((m) => (m === "rail" ? null : m)); }}
        />
      )}
      <main className="main" key={epoch}>
        {!online && <div className="banner">{t("The backend is not connected; retrying… (the first start needs a few seconds to load LiteLLM)")}</div>}
        {view.kind === "home" && <HomePage onOpen={(gid, autoSend) => setView({ kind: "chat", gid, autoSend })} onSettings={goTab} />}
        {view.kind === "chat" && (
          <ChatView
            key={view.gid}
            gid={view.gid}
            autoSend={view.autoSend}
            onAutoSent={() => setView((v) => (v.kind === "chat" ? { kind: "chat", gid: v.gid } : v))}
            outputs={{ open: outPanel, count: outCount, onToggle: () => setOutputs(!outPanel) }}
          />
        )}
        {view.kind === "agents" && <AgentsPage onSettings={goTab} />}
        {AreaPage && <div className="tool-page"><CapabilityWorkspace area={view.kind as CapabilityArea} groupId={capGroupId} onGroup={setCapGroupId} onTab={goTab} onOpenGroup={openGroup}><AreaPage key={view.kind} onTab={goTab} /></CapabilityWorkspace></div>}
        {/* A group's own library is a drill-down from its chat, and it goes "back" to the
            library tab in Settings, which is where the overview of every document lives. */}
        {view.kind === "library" && (
          <LibraryPage key={view.gid} groupId={view.gid} onBack={() => goTab("library")} />
        )}
        {view.kind === "appearance" && <div className="tool-page"><AppearancePage /></div>}
        {/* 视频专区:三块能力(场景 / 表情动作 / 音乐)自己成页。**不套 CapabilityWorkspace** ——
            那是设置类页面的外壳(它带群上下文和"接入本群"一类的动作),而专区里的东西不属于某个群,
            它是给键盘前这个人用的。 */}
        {view.kind === "video-zone" && <div className="tool-page"><VideoZonePage /></div>}
      </main>
      {/* 成果栏:聊天框的右侧。**只在聊天里出现** —— 产物属于这次对话(WorkBuddy 的面板也是这样,
          离开对话它就不在),而设置页里挂一列「这个项目产出了什么」是没有对象的。 */}
      {view.kind === "chat" && outPanel && (
        <OutputsPanel
          group={railGroup}
          stamp={railGroup?.last_at}
          onCount={takeCount}
          maximized={maxed === "outputs"}
          onMaximize={() => setMaxed((m) => (m === "outputs" ? null : "outputs"))}
          onClose={() => setOutputs(false)}
        />
      )}
      <Toaster />
      {settings && <SettingsModal tab={settings} onTab={goTab} onClose={() => setSettings(null)} onOpenGroup={openGroup} />}
    </div>
  );
}
