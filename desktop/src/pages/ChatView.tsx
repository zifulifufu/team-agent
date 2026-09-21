import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Download, Eraser, PanelRight } from "lucide-react";
import { api, downloadChat, useGroupSocket, type Approval, type ChatEvent, type Message } from "../api";
import { useData } from "../data";
import { useRoute } from "../hooks";
import { useConfirm, useOutside } from "../ui";
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
  onSettings: (t: SettingsTab) => void;
}

export default function ChatView({ gid, autoSend, onAutoSent, onSettings }: Props) {
  const { groups, agents, models, reloadGroups } = useData();
  const confirm = useConfirm();
  const route = useRoute();
  const [msgs, setMsgs] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [wsUp, setWsUp] = useState(false);
  const [panel, setPanel] = useState(() => window.innerWidth >= 1100);
  const [panelTab, setPanelTab] = useState<PanelTab>("ext");
  const [hl, setHl] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [err, setErr] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const autoSent = useRef(false);
  const stick = useRef(true); // 用户在底部时才自动跟随新消息
  const hlTimer = useRef<number>();

  const group = groups.find((g) => g.id === gid) ?? null;
  const agentById = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);
  const members = useMemo(
    () => (group ? (group.member_ids.map((i) => agentById.get(i)).filter(Boolean) as typeof agents) : []),
    [group, agentById],
  );

  const { caps, err: capsErr, refresh: refreshCaps } = useCapabilities(group);
  const toolSources = useMemo(() => new Map((caps?.tools ?? []).map((t) => [t.name, t.source])), [caps]);

  /** 从后端补齐现状:消息、等确认的调用、是否还在跑。打开群、WebSocket 重连时都用它,不会因为断线漏掉东西。 */
  const resync = useCallback(async () => {
    const [list, aps, st] = await Promise.all([
      api.messages(gid),
      api.approvals(gid).catch(() => [] as Approval[]),
      api.groupStatus(gid).catch(() => ({ busy: false })),
    ]);
    const lastTs = list.length ? list[list.length - 1].created_at ?? 0 : 0;
    const ids = new Set(list.map((m) => m.id));
    // 服务器给的为准;本地多出来的只留两种:正在流式输出的(且后端确实还在跑),和比服务器最新一条还新的(拉取期间刚到的)
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
    if (everUp.current) void resync().catch(() => undefined);   // 断线后重新连上:补齐断线期间漏掉的消息、审批和「是否还在跑」
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

  /** 任务板里点某一行 → 滚到对应发言并短暂高亮 */
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
            // 以后端给的为准;后端没带的 meta 字段(如流式期间收到的工具轨迹)沿用之前的
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
    async (t: string): Promise<boolean> => {
      setErr("");
      setBusy(true);
      try {
        await api.send(gid, t);
        return true;
      } catch (e) {
        setBusy(false);
        setErr((e as Error).message);
        return false;
      }
    },
    [gid],
  );

  // 从首页带着任务进来:等实时连接就绪后再发,保证能收到流式输出
  useEffect(() => {
    if (autoSend && wsUp && !autoSent.current) {
      autoSent.current = true;
      onAutoSent();
      void sendText(autoSend);
    }
  }, [autoSend, wsUp, onAutoSent, sendText]);

  const send = () => {
    const t = text.trim();
    if (!t || busy) return;
    setText("");
    void sendText(t).then((ok) => {
      if (!ok) setText((cur) => cur || t);   // 没发出去:把输入还给用户,别让一大段话白写
    });
  };

  if (!group) return <div className="empty big">群聊不存在</div>;

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
          {!wsUp && <span className="chip warn">实时连接中断,重连中…</span>}
          <div className="grow" />
          <div className="head-actions nodrag">
            <button
              className="icon-btn"
              title="清空聊天记录"
              aria-label="清空聊天记录"
              onClick={async () => {
                if (await confirm("清空本群的聊天记录?", { okText: "清空" })) {
                  await api.clearMessages(gid);
                  setMsgs([]);
                  await reloadGroups();
                }
              }}
            >
              <Eraser size={16} />
            </button>
            <ExportMenu gid={gid} />
            <AddMemberButton group={group} align="right" label="添加成员" className="icon-btn" />
            <button className={"icon-btn" + (panel ? " on" : "")} title="技能 / 插件 / MCP 与提示词" aria-label="技能、插件、MCP 与提示词面板" aria-pressed={panel} onClick={() => setPanel((p) => !p)}>
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
            placeholder="输入消息,@成员 点名分工;Enter 发送,Shift+Enter 换行"
            routeText={route.text}
            offline={route.offline}
            onToggleExternal={route.toggleExternal}
            rows={2}
            error={err}
          />
          <div className="composer-hint">{busy ? "成员正在协作…可随时点击停止" : "不 @ 任何人时由群主持人响应"}</div>
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
        />
      )}
    </div>
  );
}

/** 导出聊天记录:下载 Markdown;设置了 Obsidian 文件夹时还可以直接写进库里(_聊天记录 子文件夹,不会被当成记忆)。 */
function ExportMenu({ gid }: { gid: string }) {
  const [open, setOpen] = useState(false);
  const [hasObsidian, setHasObsidian] = useState(false);
  const [note, setNote] = useState("");
  const ref = useOutside<HTMLDivElement>(open, () => setOpen(false));
  const run = async (fn: () => Promise<string>) => {
    setOpen(false);
    try {
      setNote(await fn());
    } catch (e) {
      setNote("导出失败:" + (e as Error).message);
    }
    window.setTimeout(() => setNote(""), 6000);
  };
  return (
    <div className="gp-addmenu" ref={ref}>
      <button className="icon-btn" title="导出聊天记录" aria-label="导出聊天记录" aria-haspopup="menu" aria-expanded={open} onClick={() => {
        if (!open) api.obsidian().then((o) => setHasObsidian(!!o.dir && o.exists)).catch(() => setHasObsidian(false));
        setOpen((o) => !o);
      }}>
        <Download size={16} />
      </button>
      {open && (
        <div className="gp-addmenu-pop" role="menu" aria-label="导出聊天记录" style={{ right: 0, left: "auto" }}>
          <button role="menuitem" onClick={() => void run(async () => { await downloadChat(gid); return "已下载 Markdown 文件"; })}><span>下载为 Markdown</span></button>
          {hasObsidian && (
            <button role="menuitem" onClick={() => void run(async () => `已写入 Obsidian:${(await api.exportChatObsidian(gid)).path}`)}><span>写入 Obsidian</span><small>库里的 _聊天记录 文件夹</small></button>
          )}
        </div>
      )}
      {note && <div className="chip warn" role="status" style={{ position: "absolute", right: 0, top: "100%", whiteSpace: "nowrap", zIndex: 5 }}>{note}</div>}
    </div>
  );
}
