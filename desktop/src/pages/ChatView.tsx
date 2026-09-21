import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Download, Eraser, PanelRight } from "lucide-react";
import { api, downloadChat, useGroupSocket, type Approval, type Attachment, type ChatEvent, type Message } from "../api";
import { useData } from "../data";
import { useRoute } from "../hooks";
import { useConfirm, useOutside } from "../ui";
import { useI18n } from "../i18n";
import Bubble from "../components/Bubble";
import Composer from "../components/Composer";
import ApprovalBar from "../components/ApprovalBar";
import PlanCard from "../components/PlanCard";
import GroupPanel, { useCapabilities, type PanelTab } from "../components/GroupPanel";
import AddMemberButton from "../components/members/AddMemberButton";
import "../styles/members.css";
import type { SettingsTab } from "../settings/SettingsModal";

interface Props {
  gid: string;
  autoSend?: string;
  onAutoSent: () => void;
  onSettings: (tab: SettingsTab) => void;
  /** Open this group's own library in the main area */
  onOpenLibrary: () => void;
}

export default function ChatView({ gid, autoSend, onAutoSent, onSettings, onOpenLibrary }: Props) {
  const { t } = useI18n();
  const { groups, agents, models, reloadGroups } = useData();
  const confirm = useConfirm();
  const route = useRoute();
  const [msgs, setMsgs] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [images, setImages] = useState<Attachment[]>([]);
  const [busy, setBusy] = useState(false);
  const [wsUp, setWsUp] = useState(false);
  const [panel, setPanel] = useState(() => window.innerWidth >= 1100);
  const [panelTab, setPanelTab] = useState<PanelTab>("ext");
  const [hl, setHl] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [err, setErr] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const autoSent = useRef(false);
  const stick = useRef(true); // only auto-follow new messages while the user is at the bottom
  const hlTimer = useRef<number>();

  const group = groups.find((g) => g.id === gid) ?? null;
  const agentById = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);
  const members = useMemo(
    () => (group ? (group.member_ids.map((i) => agentById.get(i)).filter(Boolean) as typeof agents) : []),
    [group, agentById],
  );

  const { caps, err: capsErr, refresh: refreshCaps } = useCapabilities(group);
  const toolSources = useMemo(() => new Map((caps?.tools ?? []).map((t) => [t.name, t.source])), [caps]);

  /** Re-sync from the backend: messages, pending approvals, whether a turn is still
   * running. Used when opening a group and after a WebSocket reconnect, so nothing
   * is missed while the socket was down. */
  const resync = useCallback(async () => {
    const [list, aps, st] = await Promise.all([
      api.messages(gid),
      api.approvals(gid).catch(() => [] as Approval[]),
      api.groupStatus(gid).catch(() => ({ busy: false })),
    ]);
    const lastTs = list.length ? list[list.length - 1].created_at ?? 0 : 0;
    const ids = new Set(list.map((m) => m.id));
    // The server is the source of truth. Keep only two kinds of local extras: ones
    // still streaming while the backend is busy, and ones newer than the newest
    // server row (they arrived while this fetch was in flight).
    setMsgs((cur) => [
      ...list,
      ...cur.filter((m) => !ids.has(m.id) && ((m.streaming && st.busy) || (m.created_at ?? 0) > lastTs)),
    ]);
    setApprovals(aps);
    setBusy(st.busy);
  }, [gid]);

  useEffect(() => {
    stick.current = true;
    setMsgs([]);
    setApprovals([]);
    setBusy(false);
    void resync().catch(() => undefined);
  }, [gid, resync]);
  const everUp = useRef(false);
  useEffect(() => {
    if (!wsUp) return;
    if (everUp.current) void resync().catch(() => undefined);   // reconnected: catch up on messages, approvals and run state
    everUp.current = true;
  }, [wsUp, resync]);

  useEffect(() => {
    const el = listRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [msgs]);
  useEffect(() => () => window.clearTimeout(hlTimer.current), []);

  const onScroll = () => {
    const el = listRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };

  /** Clicking a row on the plan card: scroll to that message and flash it briefly. */
  const jumpTo = useCallback((mid: string) => {
    const el = listRef.current?.querySelector(`[data-mid="${mid}"]`);
    if (!el) return;
    stick.current = false;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    window.clearTimeout(hlTimer.current);
    setHl(null);
    requestAnimationFrame(() => setHl(mid));
    hlTimer.current = window.setTimeout(() => setHl(null), 2000);
  }, []);

  const onEvent = useCallback(
    (e: ChatEvent) => {
      setMsgs((cur) => {
        switch (e.type) {
          case "message":
            return cur.some((m) => m.id === e.message.id) ? cur : [...cur, e.message];
          case "message_start":
            return cur.some((m) => m.id === e.message.id) ? cur : [...cur, { ...e.message, streaming: true }];
          case "delta":
            return cur.map((m) => (m.id === e.message_id ? { ...m, content: m.content + e.text } : m));
          case "reset":
            return cur.map((m) => (m.id === e.message_id ? { ...m, content: "" } : m));
          case "message_end":
            // The backend wins; meta fields it omits (tool traces received while
            // streaming, for example) keep their previous value.
            return cur.map((m) =>
              m.id === e.message.id ? { ...e.message, meta: { ...m.meta, ...e.message.meta }, streaming: false } : m,
            );
          case "plan":
            return cur.some((m) => m.id === e.message.id) ? cur.map((m) => (m.id === e.message.id ? e.message : m)) : [...cur, e.message];
          case "tool":
            return cur.map((m) => {
              if (m.id !== e.message_id) return m;
              const tools = [...(m.meta?.tools ?? [])];
              tools[e.index] = e.call;
              return { ...m, meta: { ...m.meta, tools } };
            });
          case "message_discard":
            return cur.filter((m) => m.id !== e.message_id);
          case "stopped":
            return cur.filter((m) => !m.streaming);
          default:
            return cur;
        }
      });
      if (e.type === "approval") setApprovals((cur) => (cur.some((a) => a.id === e.approval.id) ? cur : [...cur, e.approval]));
      if (e.type === "approval_done") setApprovals((cur) => cur.filter((a) => a.id !== e.id));
      if (e.type === "stopped") {
        setApprovals([]);
        setBusy(false);
      }
      if (e.type === "idle") {
        setBusy(false);
        void reloadGroups();
      }
    },
    [reloadGroups],
  );
  useGroupSocket(gid, onEvent, setWsUp);

  const sendText = useCallback(
    async (body: string, imageIds: string[] = []): Promise<boolean> => {
      setErr("");
      setBusy(true);
      try {
        await api.send(gid, body, imageIds);
        return true;
      } catch (e) {
        setBusy(false);
        setErr((e as Error).message);
        return false;
      }
    },
    [gid],
  );

  // Arriving from the home screen with a task: wait for the live connection first so
  // the streaming output is not missed.
  useEffect(() => {
    if (autoSend && wsUp && !autoSent.current) {
      autoSent.current = true;
      onAutoSent();
      void sendText(autoSend);
    }
  }, [autoSend, wsUp, onAutoSent, sendText]);

  const send = () => {
    const body = text.trim();
    const ids = images.map((i) => i.id);
    if ((!body && !ids.length) || busy) return;
    setText("");
    setImages([]);
    void sendText(body, ids).then((ok) => {
      if (!ok) {
        // Not sent: give the text and the images back to the user
        setText((cur) => cur || body);
        setImages((cur) => (cur.length ? cur : images));
      }
    });
  };

  if (!group) return <div className="empty big">{t("Group chat not found")}</div>;

  return (
    <div className="chat-layout">
      <section className="chat">
        <header className="chat-head drag">
          <div className="chat-title">
            {group.name} <span className="muted">({members.length})</span>
          </div>
          <div className="ava-stack" aria-hidden>
            {members.slice(0, 5).map((a) => (
              <span key={a.id} title={a.name}>{a.avatar}</span>
            ))}
          </div>
          {!wsUp && <span className="chip warn">{t("Live connection lost, reconnecting…")}</span>}
          <div className="grow" />
          <div className="head-actions nodrag">
            <button
              className="icon-btn"
              title={t("Clear chat history")}
              aria-label={t("Clear chat history")}
              onClick={async () => {
                if (await confirm(t("Clear this group's chat history?"), { okText: t("Clear all") })) {
                  await api.clearMessages(gid);
                  setMsgs([]);
                  await reloadGroups();
                }
              }}
            >
              <Eraser size={16} />
            </button>
            <ExportMenu gid={gid} />
            <AddMemberButton group={group} align="right" label={t("Add member")} className="icon-btn" />
            <button className={"icon-btn" + (panel ? " on" : "")} title={t("Skills / plugins / MCP and prompts")} aria-label={t("Skills, plugins, MCP and prompts panel")} aria-pressed={panel} onClick={() => setPanel((p) => !p)}>
              <PanelRight size={16} />
            </button>
          </div>
        </header>

        <div className="msg-list" ref={listRef} onScroll={onScroll}>
          <div className="msg-inner">
            {msgs.map((m) =>
              m.sender_type === "plan" ? (
                <PlanCard
                  key={m.id}
                  m={m}
                  finalMessageId={msgs.find((x) => x.meta?.plan_id === m.id && x.meta?.task_id === "final")?.id}
                  onJump={jumpTo}
                  highlight={hl === m.id}
                />
              ) : (
                <Bubble
                  key={m.id}
                  m={m}
                  agent={m.sender_id ? agentById.get(m.sender_id) : undefined}
                  models={models}
                  toolSources={toolSources}
                  highlight={hl === m.id}
                />
              ),
            )}
          </div>
        </div>

        <div className="chat-composer">
          <ApprovalBar items={approvals} onGone={(id) => setApprovals((cur) => cur.filter((a) => a.id !== id))} />
          <Composer
            value={text}
            onChange={setText}
            onSend={send}
            busy={busy}
            onStop={() => api.stop(gid)}
            members={members}
            placeholder={t("Type a message; @mention a member to assign work. Enter to send, Shift+Enter for a new line")}
            routeText={route.text}
            offline={route.offline}
            onToggleExternal={route.toggleExternal}
            rows={2}
            error={err}
            groupId={gid}
            images={images}
            onImages={setImages}
          />
          <div className="composer-hint">{t(busy ? "Members are working — you can stop at any time" : "With no @mention, the group host answers")}</div>
        </div>
      </section>

      {panel && (
        <GroupPanel
          group={group}
          caps={caps}
          capsErr={capsErr}
          refreshCaps={refreshCaps}
          tab={panelTab}
          onTab={setPanelTab}
          onSettings={onSettings}
          onOpenLibrary={onOpenLibrary}
        />
      )}
    </div>
  );
}

/** Export the chat: download Markdown, or write it straight into the Obsidian vault
 * when one is configured (into a _chat-log subfolder, so it is not read as memory). */
function ExportMenu({ gid }: { gid: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [hasObsidian, setHasObsidian] = useState(false);
  const [note, setNote] = useState("");
  const ref = useOutside<HTMLDivElement>(open, () => setOpen(false));
  const run = async (fn: () => Promise<string>) => {
    setOpen(false);
    try {
      setNote(await fn());
    } catch (e) {
      setNote(t("Export failed: ") + (e as Error).message);
    }
    window.setTimeout(() => setNote(""), 6000);
  };
  return (
    <div className="gp-addmenu" ref={ref}>
      <button className="icon-btn" title={t("Export chat history")} aria-label={t("Export chat history")} aria-haspopup="menu" aria-expanded={open} onClick={() => {
        if (!open) api.obsidian().then((o) => setHasObsidian(!!o.dir && o.exists)).catch(() => setHasObsidian(false));
        setOpen((o) => !o);
      }}>
        <Download size={16} />
      </button>
      {open && (
        <div className="gp-addmenu-pop" role="menu" aria-label={t("Export chat history")} style={{ right: 0, left: "auto" }}>
          <button role="menuitem" onClick={() => void run(async () => { await downloadChat(gid); return t("Markdown file downloaded"); })}><span>{t("Download as Markdown")}</span></button>
          {hasObsidian && (
            <button role="menuitem" onClick={() => void run(async () => t("Written to Obsidian: {path}", { path: (await api.exportChatObsidian(gid)).path }))}><span>{t("Write to Obsidian")}</span><small>{t("The _chat-log folder in your vault")}</small></button>
          )}
        </div>
      )}
      {note && <div className="chip warn" role="status" style={{ position: "absolute", right: 0, top: "100%", whiteSpace: "nowrap", zIndex: 5 }}>{note}</div>}
    </div>
  );
}
