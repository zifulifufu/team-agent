import { memo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, ChevronDown, ChevronRight, CornerUpLeft, Lock, LoaderCircle, Megaphone, ShieldAlert, ShieldOff, Wrench, X } from "lucide-react";
import { modelLabel, type Agent, type Message, type Model, type ToolCall } from "../api";
import "../styles/chat.css";

const DECL_PREFIX = "【分工】";

/** 成员发言以「【分工】」开头时,把第一行拆成「分工声明」条。流式中第一行还没写完就按普通内容显示,避免闪烁。 */
function splitDeclaration(content: string, streaming: boolean): { decl: string | null; rest: string } {
  const c = content.replace(/^\s+/, "");
  if (!c.startsWith(DECL_PREFIX)) return { decl: null, rest: content };
  const nl = c.indexOf("\n");
  if (nl < 0) {
    if (streaming) return { decl: null, rest: content };
    return { decl: c.slice(DECL_PREFIX.length).trim(), rest: "" };
  }
  return { decl: c.slice(DECL_PREFIX.length, nl).trim(), rest: c.slice(nl + 1).replace(/^\s+/, "") };
}

type Source = "builtin" | "plugin" | "mcp" | "";

/** 工具名的可读写法:mcp__echo__add → echo · add(mcp);内置/插件工具原样。 */
export function toolDisplay(name: string, sources?: Map<string, string>): { label: string; source: Source } {
  const mm = /^mcp__(.+?)__(.+)$/.exec(name);
  if (mm) return { label: `${mm[1]} · ${mm[2]}`, source: "mcp" };
  const src = sources?.get(name);
  return { label: name, source: src === "plugin" || src === "builtin" || src === "mcp" ? src : "" };
}

function fmtVal(v: unknown): string {
  if (v === null || v === undefined) return "null";
  return typeof v === "string" ? v : JSON.stringify(v);
}

function ToolPill({ call, sources }: { call: ToolCall; sources?: Map<string, string> }) {
  const [open, setOpen] = useState(false);
  const { label, source } = toolDisplay(call.name, sources);
  const args = Object.entries(call.args ?? {});
  const stateText = { running: "调用中", waiting: "等你确认", ok: "成功", failed: "失败", denied: "已拒绝" }[call.status];
  return (
    <div className={"tool-pill " + call.status + (open ? " open" : "")}>
      <button className="tool-pill-head" aria-expanded={open} aria-label={`工具 ${label},${stateText}`} onClick={() => setOpen((v) => !v)}>
        {call.status === "running" ? (
          <LoaderCircle size={12} className="spin tp-ico running" aria-hidden />
        ) : call.status === "waiting" ? (
          <ShieldAlert size={12} className="tp-ico waiting" aria-hidden />
        ) : call.status === "denied" ? (
          <ShieldOff size={12} className="tp-ico failed" aria-hidden />
        ) : call.status === "ok" ? (
          <Check size={12} className="tp-ico ok" aria-hidden />
        ) : (
          <X size={12} className="tp-ico failed" aria-hidden />
        )}
        <Wrench size={11} className="tp-wrench" aria-hidden />
        <span className="tp-name">{label}</span>
        {source === "mcp" && <span className="tp-src">mcp</span>}
        {source === "plugin" && <span className="tp-src">插件</span>}
        {(call.status === "waiting" || call.status === "denied") && <span className="tp-src">{stateText}</span>}
        {call.status !== "running" && call.status !== "waiting" && typeof call.ms === "number" && <span className="tp-ms">{call.ms} ms</span>}
        {open ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
      </button>
      {open && (
        <div className="tool-pill-body">
          <div className="tp-sec">参数</div>
          {args.length === 0 ? (
            <div className="tp-empty">无参数</div>
          ) : (
            <dl className="tp-args">
              {args.map(([k, v]) => (
                <div key={k}>
                  <dt>{k}</dt>
                  <dd>{fmtVal(v)}</dd>
                </div>
              ))}
            </dl>
          )}
          <div className="tp-sec">结果预览</div>
          {call.status === "running" || call.status === "waiting" ? (
            <div className="tp-empty">{call.status === "waiting" ? "等你在聊天窗口底部确认…" : "执行中…"}</div>
          ) : call.preview ? (
            <pre className={"tp-pre" + (call.status === "failed" || call.status === "denied" ? " failed" : "")}>{call.preview}</pre>
          ) : (
            <div className="tp-empty">(无返回内容)</div>
          )}
        </div>
      )}
    </div>
  );
}

interface Props {
  m: Message;
  agent?: Agent;
  models: Model[];
  /** 工具名 → 来源(builtin / plugin / mcp),用来给插件工具打标 */
  toolSources?: Map<string, string>;
  highlight?: boolean;
}

const LEVEL_TEXT: Record<string, string> = { read: "只读", edit: "可改文件", full: "完全权限" };

function Bubble({ m, agent, models, toolSources, highlight }: Props) {
  if (m.sender_type === "system") {
    return <div className="sys-msg">{m.content}</div>;
  }
  const mine = m.sender_type === "user";
  const attempts = m.meta?.attempts ?? [];
  const tools = m.meta?.tools ?? [];
  const offline = attempts.some((a) => a.status === "skipped" && a.detail === "外呼已禁用");
  const tip = attempts
    .map((a) => `${modelLabel(a.model_id, models)}: ${a.status === "ok" ? "成功" : a.status === "skipped" ? "跳过(" + a.detail + ")" : "失败(" + a.detail + ")"}`)
    .join("\n");
  const { decl, rest } = mine ? { decl: null, rest: m.content } : splitDeclaration(m.content, !!m.streaming);
  const taskChip = m.meta?.task_id
    ? m.meta.task_id === "final"
      ? "整合"
      : `任务 · ${m.meta.task_title || m.meta.task_id}`
    : m.meta?.task_title
      ? `任务 · ${m.meta.task_title}`
      : "";
  const showBubble = mine || rest !== "" || decl === null || !!m.streaming;
  return (
    <div className={"msg " + (mine ? "mine" : "theirs") + (highlight ? " hl" : "")} data-mid={m.id}>
      <div className="avatar">{mine ? "🙂" : agent?.avatar ?? "🤖"}</div>
      <div className="msg-body">
        <div className="msg-name">
          {m.sender_name}
          {!mine && agent?.role && <span className="msg-role">{agent.role}</span>}
          {taskChip && <span className={"chip task-chip" + (m.meta?.task_id === "final" ? " final" : "")} title={m.meta?.task_title}>{taskChip}</span>}
        </div>
        {decl !== null && (
          <div className={"decl-bar" + (showBubble ? "" : " alone")}>
            <Megaphone size={12} aria-hidden />
            <span className="decl-tag">分工声明</span>
            <span className="decl-text">{decl}</span>
          </div>
        )}
        {showBubble && (
          <div className={"bubble" + (decl !== null ? " under-decl" : "")}>
            {mine ? (
              <span style={{ whiteSpace: "pre-wrap" }}>{m.content}</span>
            ) : rest ? (
              <div className="md">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{rest}</ReactMarkdown>
              </div>
            ) : (
              <span className="typing">
                <i />
                <i />
                <i />
              </span>
            )}
          </div>
        )}
        {!mine && tools.length > 0 && (
          <div className="tool-pills">
            {tools.map((c, i) => (c ? <ToolPill key={i} call={c} sources={toolSources} /> : null))}
          </div>
        )}
        {!mine && m.model_id && (
          <div className="msg-meta" title={tip}>
            <span className="chip">{modelLabel(m.model_id, models)}</span>
            {m.meta?.engine && m.meta.level && <span className="chip ext-lv" title="它这次发言时的权限级别">{LEVEL_TEXT[m.meta.level] ?? m.meta.level}</span>}
            {m.meta?.external?.duration_ms ? <span className="chip" title="这次发言用时">{Math.round(m.meta.external.duration_ms / 1000)} 秒</span> : null}
            {m.fallback_from &&
              (offline ? (
                <span className="chip"><Lock size={11} /> 离线模式</span>
              ) : (
                <span className="chip warn"><CornerUpLeft size={11} /> 已从 {modelLabel(m.fallback_from, models)} 回退</span>
              ))}
          </div>
        )}
        {!mine && (m.meta?.denied?.length ?? 0) > 0 && (
          <div className="ext-denied" role="note">按你设的权限级别,它想用的 {m.meta!.denied!.join("、")} 被挡下了(没有执行)。需要的话,到成员设置里调整权限级别。</div>
        )}
      </div>
    </div>
  );
}

export default memo(Bubble);
