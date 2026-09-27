import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Download, Eraser, Package } from "lucide-react";
import { api, downloadChat, downloadTasks, useGroupSocket, type Approval, type Attachment, type ChatEvent, type Message, type MessageFeedback } from "../api";
import { useData } from "../data";
import { draftFiles, draftText, setDraftFiles, setDraftText } from "../drafts";
import { useRoute } from "../hooks";
import { useConfirm, useOutside } from "../ui";
import { useI18n } from "../i18n";
import Bubble from "../components/Bubble";
import Composer from "../components/Composer";
import ApprovalBar from "../components/ApprovalBar";
import PlanCard from "../components/PlanCard";
import { useCapabilities } from "../components/group/capabilities";
import WorkspacePicker from "../components/WorkspacePicker";
import "../styles/members.css";

interface Props {
  gid: string;
  autoSend?: string;
  onAutoSent: () => void;
  /** The outputs column on the right of this chat: whether it is showing, how many files it counted,
   *  and how to flip it. The count is computed *there* (one place, one number) and only displayed
   *  here — two independent readings of "how many files" is a mistake this project has already made. */
  outputs: { open: boolean; count: number | null; onToggle: () => void };
}

export default function ChatView({ gid, autoSend, onAutoSent, outputs }: Props) {
  const { t } = useI18n();
  const { groups, agents, models, reloadGroups, reload } = useData();
  useEffect(() => {
    let live = true;
    api.feedback(gid, 500).then((board) => {
      if (!live) return;
      setJudged(new Map((board.items ?? []).map((f) => [f.message_id, f])));
    }).catch(() => { /* no ratings yet, or none reachable: the keys still work */ });
    return () => { live = false; };
  }, [gid]);
  const confirm = useConfirm();
  const route = useRoute();
  const [msgs, setMsgs] = useState<Message[]>([]);
  // What the user has already judged in this group, by message id. Read once per group and updated
  // from each rating's own answer, so a thumb that is lit is one the backend agrees is lit.
  const [judged, setJudged] = useState<Map<string, MessageFeedback>>(new Map());
  // Initialised from the draft store, not from "": this component is remounted whenever the group
  // changes (`key={view.gid}` in App), and a draft that lives only here dies with the mount.
  const [text, setText] = useState(() => draftText(gid));
  const [files, setFiles] = useState<Attachment[]>(() => draftFiles(gid));
  const [busy, setBusy] = useState(false);
  const [wsUp, setWsUp] = useState(false);
  // ⚠️ 这里原来有两个 state 管「本群设置」那个对话框。2026-09-25 用户要求把整个面板删掉
  // (「技能、MCP、提示词都一样,在这个地方不合适,可以删除」),所以连它的开合状态、那个齿轮按钮
  // 和 `GroupPanel` 一起没了。**能力列表(`useCapabilities`)留着** —— 成员栏要它。
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

  const { caps } = useCapabilities(group);
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

  // Save the draft under this group as it changes, so it is already stored by the time something
  // unmounts this view.
  //
  // `gid` comes from the closure, which is only safe because App keys this view by group id
  // (`key={view.gid}`): one mount therefore belongs to exactly one group. Without that key a
  // switch would write one group's text into another's slot — visible, not silent, but worth
  // knowing before removing it.
  useEffect(() => { setDraftText(gid, text); }, [gid, text]);
  useEffect(() => { setDraftFiles(gid, files); }, [gid, files]);

  useEffect(() => {
    const el = listRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [msgs]);
  useEffect(() => () => window.clearTimeout(hlTimer.current), []);

  const onScroll = () => {
    const el = listRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };

  /** Reference an earlier message: the token goes in the composer, and the backend inlines that
   *  message's text into the prompt — so quoting an old turn never depends on it still fitting in
   *  the context window. */
  const quote = useCallback((text: string, whole: boolean) => {
    if (whole) {
      setText((cur) => `${cur}${cur && !/\s$/.test(cur) ? " " : ""}@msg:${text} `);
      return;
    }
    // A passage is quoted as text, not as a reference: what the user selected is exactly what they
    // want the members to answer about, and `@msg:` would inline the whole message instead.
    const lines = text.split("\n").map((l) => `> ${l}`).join("\n");
    setText((cur) => `${cur}${cur && !/\s$/.test(cur) ? "\n\n" : ""}${lines}\n\n`);
  }, []);

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
          case "thinking":
            // Kept in `meta` so that the streaming path and the stored message agree on where the
            // working lives; "" is the reset, not an append.
            return cur.map((m) => (m.id === e.message_id
              ? { ...m, meta: { ...m.meta, thinking: e.text === "" ? "" : (m.meta?.thinking ?? "") + e.text } }
              : m));
          case "message_discard":
            return cur.filter((m) => m.id !== e.message_id);
          case "stopped":
            return cur.filter((m) => !m.streaming);
          default:
            return cur;
        }
      });
      if (e.type === "approval") setApprovals((cur) => (cur.some((a) => a.id === e.approval.id) ? cur : [...cur, e.approval]));
      if (e.type === "group_updated") void reload();
      if (e.type === "message_start" || (e.type === "message" && e.message.sender_type === "user")) setBusy(true);
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
    [reloadGroups, reload],
  );
  useGroupSocket(gid, onEvent, setWsUp);

  const sendText = useCallback(
    async (body: string, fileIds: string[] = []): Promise<boolean> => {
      setErr("");
      setBusy(true);
      try {
        await api.send(gid, body, fileIds);
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
      void sendText(autoSend).then((ok) => {
        if (!ok) setText((current) => current || autoSend);
      });
    }
  }, [autoSend, wsUp, onAutoSent, sendText]);

  const send = () => {
    const body = text.trim();
    const ids = files.map((i) => i.id);
    if ((!body && !ids.length) || busy) return;
    setText("");
    setFiles([]);
    void sendText(body, ids).then((ok) => {
      if (!ok) {
        // Not sent: give the text and the files back to the user
        setText((cur) => cur || body);
        setFiles((cur) => (cur.length ? cur : files));
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
          {!wsUp && <span className="chip warn">{t("Live connection lost, reconnecting…")}</span>}
          <div className="grow" />
          <div className="head-actions nodrag">
            <button
              className="icon-btn"
              title={t("Clear chat history")}
              aria-label={t("Clear chat history")}
              disabled={busy}
              onClick={async () => {
                if (await confirm(t("Clear this group's chat history?"), { okText: t("Clear all") })) {
                  try {
                    await api.clearMessages(gid);
                    setMsgs([]);
                    await reloadGroups();
                  } catch (e) {
                    setErr((e as Error).message);
                  }
                }
              }}
            >
              <Eraser size={16} />
            </button>
            <ExportMenu gid={gid} />
            {/* 「成果」入口就在聊天头部:那一栏挂在**这个对话**的右侧,所以打开/收起它的开关也
                应该在对话自己身上。面板开着时它是亮着的(和「本群设置」那个按钮同一种写法)。 */}
            <button className={"icon-btn" + (outputs.open ? " on" : "") +
                               (outputs.count ? " with-n" : "")}
              title={t(outputs.open ? "Hide the outputs column" : "Show the outputs column")}
              aria-label={t("Outputs")} aria-pressed={outputs.open} onClick={outputs.onToggle}>
              <Package size={16} />
              {outputs.count ? <span className="n">{outputs.count}</span> : null}
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
                  gid={gid}
                  groups={groups}
                  agent={m.sender_id ? agentById.get(m.sender_id) : undefined}
                  models={models}
                  toolSources={toolSources}
                  highlight={hl === m.id}
                  judged={judged.get(m.id)}
                  onQuote={quote}
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
            onStop={() => { void api.stop(gid).catch((e) => setErr((e as Error).message)); }}
            members={members}
            placeholder={t("Type a message; @mention a member to assign work. Enter to send, Shift+Enter for a new line")}
            routeText={route.text}
            routeTitle={route.title}
            offline={route.offline}
            onToggleExternal={route.toggleExternal}
            rows={2}
            error={err}
            groupId={gid}
            files={files}
            onFiles={setFiles}
            extra={<WorkspacePicker gid={gid} group={group} />}
          />
          <div className="composer-hint">{t(busy ? "Members are working — you can stop at any time" : "With no @mention, the group host answers")}</div>
        </div>
      </section>

    </div>
  );
}

/** Export the chat: the whole log as Markdown, every task board as a table, or straight into the
 * Obsidian vault when one is configured (into a _chat-log subfolder, so it is not read as memory). */
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
      <button className="icon-btn" title={t("Export")} aria-label={t("Export")} aria-haspopup="menu" aria-expanded={open} onClick={() => {
        if (!open) api.obsidian().then((o) => setHasObsidian(!!o.dir && o.exists)).catch(() => setHasObsidian(false));
        setOpen((o) => !o);
      }}>
        <Download size={16} />
      </button>
      {open && (
        <div className="gp-addmenu-pop" role="menu" aria-label={t("Export")} style={{ right: 0, left: "auto" }}>
          <button role="menuitem" onClick={() => void run(async () => { await downloadChat(gid); return t("Markdown file downloaded"); })}><span>{t("Download as Markdown")}</span></button>
          <button role="menuitem" onClick={() => void run(async () => { await downloadTasks(gid); return t("Task list downloaded"); })}>
            <span>{t("Download every task list")}</span><small>{t("One row per task, across all task boards")}</small>
          </button>
          {hasObsidian && (
            <button role="menuitem" onClick={() => void run(async () => t("Written to Obsidian: {path}", { path: (await api.exportChatObsidian(gid)).path }))}><span>{t("Write to Obsidian")}</span><small>{t("The _chat-log folder in your vault")}</small></button>
          )}
        </div>
      )}
      {note && <div className="chip warn" role="status" style={{ position: "absolute", right: 0, top: "100%", whiteSpace: "nowrap", zIndex: 5 }}>{note}</div>}
    </div>
  );
}
