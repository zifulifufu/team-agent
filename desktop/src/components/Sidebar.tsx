import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Download, Info, MessageSquarePlus, PanelLeftClose, Palette, Plug, Puzzle, Search, Settings, Sparkles, TerminalSquare, Trash2, Users, Webhook, WifiOff, X } from "lucide-react";
import { api, relTime } from "../api";
import { useData } from "../data";
import { Switch, useConfirm, useOutside } from "../ui";
import { useI18n } from "../i18n";
import type { SettingsTab } from "../settings/SettingsModal";
import AddMemberButton from "./members/AddMemberButton";
import MemberDock from "./members/MemberDock";
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
const TOOL_NAV: { kind: "skills" | "plugins" | "mcp" | "external" | "channels"; label: string; icon: typeof Sparkles }[] = [
  { kind: "skills", label: "Skills", icon: Sparkles },
  { kind: "plugins", label: "Plugins", icon: Puzzle },
  { kind: "mcp", label: "MCP", icon: Plug },
  { kind: "external", label: "External agents", icon: TerminalSquare },
  { kind: "channels", label: "Chat channels", icon: Webhook },
];

interface Props {
  view: View;
  onView: (v: View) => void;
  onSettings: (tab: SettingsTab) => void;
  onCollapse: () => void;
  version: string;
}

export default function Sidebar({ view, onView, onSettings, onCollapse, version }: Props) {
  const { groups, settings, online, reload, reloadGroups, updateCount } = useData();
  const confirm = useConfirm();
  const { t } = useI18n();
  const [open, setOpen] = useState(true);
  const [searching, setSearching] = useState(false);
  const [q, setQ] = useState("");
  const [menu, setMenu] = useState(false);
  const [memOpen, setMemOpen] = useState(true);
  const [lastGid, setLastGid] = useState<string | null>(null);
  const menuRef = useOutside<HTMLDivElement>(menu, () => setMenu(false));

  const list = useMemo(() => {
    const s = q.trim().toLowerCase();
    return [...groups]
      .sort((a, b) => (b.last_at ?? 0) - (a.last_at ?? 0))
      .filter((g) => !s || g.name.toLowerCase().includes(s));
  }, [groups, q]);

  const activeGid = view.kind === "chat" ? view.gid : null;
  // Members are listed under Members; after leaving the chat view the most recently opened group stays shown
  useEffect(() => { if (view.kind === "chat") setLastGid(view.gid); }, [view]);
  const dockGroup = groups.find((g) => g.id === (activeGid ?? lastGid)) ?? null;
  const offline = settings ? !settings.external_calls_enabled : false;

  const remove = async (id: string, name: string) => {
    if (!(await confirm(t('Delete the group chat "{name}" and all of its messages?', { name }), { okText: t("Delete") }))) return;
    await api.delGroup(id);
    await reloadGroups();
    if (activeGid === id) onView({ kind: "home" });
  };

  return (
    <aside className="sidebar">
      <div className="side-top drag">
        <button className="icon-btn nodrag" title={t("Collapse sidebar")} aria-label={t("Collapse sidebar")} onClick={onCollapse}><PanelLeftClose size={17} /></button>
        <div className="grow" />
        <button className="icon-btn nodrag" title={t("Search group chats")} aria-label={t("Search group chats")} onClick={() => { setSearching((s) => !s); setQ(""); }}><Search size={17} /></button>
        <button className="icon-btn nodrag" title={t("New group chat")} aria-label={t("New group chat")} onClick={() => onView({ kind: "home" })}><MessageSquarePlus size={17} /></button>
      </div>

      <div className="brand">
        <span className="brand-name">Team Agent</span>
        <span className="ver">v{version}</span>
      </div>

      {searching && (
        <div className="side-search">
          <Search size={14} />
          <input autoFocus placeholder={t("Search group chats…")} value={q} onChange={(e) => setQ(e.target.value)} />
          {q && <button className="icon-btn tiny" aria-label={t("Clear")} onClick={() => setQ("")}><X size={13} /></button>}
        </div>
      )}

      <nav className="side-nav">
        <button className={"nav-item" + (view.kind === "home" ? " on" : "")} onClick={() => onView({ kind: "home" })}>
          <MessageSquarePlus size={16} /> {t("New group chat")}
        </button>
        <div className="nav-split">
          <button className={"nav-item" + (view.kind === "agents" ? " on" : "")} onClick={() => onView({ kind: "agents" })}>
            <Users size={16} /> {t("Members")}{dockGroup && <span className="count">({dockGroup.member_ids.length})</span>}
          </button>
          {dockGroup && <AddMemberButton group={dockGroup} />}
          <button className="icon-btn tiny" title={t(memOpen ? "Collapse members" : "Expand members")} aria-label={t(memOpen ? "Collapse members" : "Expand members")} aria-expanded={memOpen} onClick={() => setMemOpen((o) => !o)}>
            {memOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          </button>
        </div>
        {memOpen && (dockGroup ? <MemberDock group={dockGroup} /> : <div className="side-empty">{t("Open a group chat and its members show up here — you can add more at any time")}</div>)}
      </nav>
      <div className="nav-cap">{t("Tools")}</div>
      <nav className="side-nav" aria-label={t("Tools")}>
        {TOOL_NAV.map((item) => (
          <button key={item.kind} className={"nav-item" + (view.kind === item.kind ? " on" : "")} onClick={() => onView({ kind: item.kind } as View)}>
            <item.icon size={16} /> {t(item.label)}
          </button>
        ))}
      </nav>

      <button className="side-section" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />} {t("Group chats")} <span className="count">({list.length})</span>
      </button>
      <div className="side-list">
        {open &&
          list.map((g) => (
            <div key={g.id} className={"conv" + (g.id === activeGid ? " on" : "")}>
              <button className="conv-main" onClick={() => onView({ kind: "chat", gid: g.id })}>
                <span className="conv-name">{g.name}</span>
                <span className="conv-time">{relTime(g.last_at)}</span>
              </button>
              <button className="icon-btn tiny conv-del" title={t("Delete group chat")} aria-label={t("Delete group chat {name}", { name: g.name })} onClick={() => void remove(g.id, g.name)}><Trash2 size={13} /></button>
            </div>
          ))}
        {open && list.length === 0 && <div className="side-empty">{t(q ? "No matching group chats" : "No group chats yet")}</div>}
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
            <button role="menuitem" onClick={() => { setMenu(false); onSettings("updates"); }}><Download size={15} /> {t("Updates & discovery")}{updateCount > 0 && <span className="count-badge" style={{ marginLeft: "auto" }}>{updateCount}</span>}</button>
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
    </aside>
  );
}
