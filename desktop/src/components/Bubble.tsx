import { memo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, ChevronDown, ChevronRight, CornerUpLeft, Lock, LoaderCircle, Megaphone, ShieldAlert, ShieldOff, Wrench, X } from "lucide-react";
import { modelLabel, type Agent, type Message, type Model, type ToolCall } from "../api";
import { pick, useI18n } from "../i18n";
import { levelLabel } from "../lib";
import "../styles/chat.css";

/** Protocol marker the backend asks members to start their reply with (see the
 * planner prompt). It is a machine-readable token, not UI text, so it is not run
 * through the dictionary — but the backend does emit it in the language of the
 * request, and both spellings have to be recognized. */
const DECL_PREFIXES = ["【分工】", "[Assignment]"];

/** When a member reply starts with the marker, its first line becomes an "assignment"
 * row. While streaming, an unfinished first line is shown as ordinary content so the
 * text does not flicker between the two forms. */
function splitDeclaration(content: string, streaming: boolean): { decl: string | null; rest: string } {
  const c = content.replace(/^\s+/, "");
  const prefix = DECL_PREFIXES.find((p) => c.startsWith(p));
  if (!prefix) return { decl: null, rest: content };
  const nl = c.indexOf("\n");
  if (nl < 0) {
    if (streaming) return { decl: null, rest: content };
    return { decl: c.slice(prefix.length).trim(), rest: "" };
  }
  return { decl: c.slice(prefix.length, nl).trim(), rest: c.slice(nl + 1).replace(/^\s+/, "") };
}

type Source = "builtin" | "plugin" | "mcp" | "";

/** Readable form of a tool name: mcp__echo__add becomes "echo · add" (mcp);
 * built-in and plugin tools are shown as they are. */
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
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const { label, source } = toolDisplay(call.name, sources);
  const args = Object.entries(call.args ?? {});
  const stateText = {
    running: t("Running"), waiting: t("Waiting for you"), ok: t("Succeeded"), failed: t("Failed"), denied: t("Denied"),
  }[call.status];
  return (
    <div className={"tool-pill " + call.status + (open ? " open" : "")}>
      <button className="tool-pill-head" aria-expanded={open} aria-label={t("Tool {name}, {state}", { name: label, state: stateText })} onClick={() => setOpen((v) => !v)}>
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
        {source === "plugin" && <span className="tp-src">{t("plugin")}</span>}
        {(call.status === "waiting" || call.status === "denied") && <span className="tp-src">{stateText}</span>}
        {call.status !== "running" && call.status !== "waiting" && typeof call.ms === "number" && <span className="tp-ms">{call.ms} ms</span>}
        {open ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
      </button>
      {open && (
        <div className="tool-pill-body">
          <div className="tp-sec">{t("Arguments")}</div>
          {args.length === 0 ? (
            <div className="tp-empty">{t("No arguments")}</div>
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
          <div className="tp-sec">{t("Result preview")}</div>
          {call.status === "running" || call.status === "waiting" ? (
            <div className="tp-empty">{call.status === "waiting" ? t("Waiting for your approval at the bottom of the chat…") : t("Running…")}</div>
          ) : call.preview ? (
            <pre className={"tp-pre" + (call.status === "failed" || call.status === "denied" ? " failed" : "")}>{call.preview}</pre>
          ) : (
            <div className="tp-empty">{t("(no output)")}</div>
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
  /** Tool name → source (builtin / plugin / mcp), used to label plugin tools */
  toolSources?: Map<string, string>;
  highlight?: boolean;
}

function Bubble({ m, agent, models, toolSources, highlight }: Props) {
  const { t } = useI18n();
  if (m.sender_type === "system") {
    return <div className="sys-msg">{m.content}</div>;
  }
  const mine = m.sender_type === "user";
  const attempts = m.meta?.attempts ?? [];
  const tools = m.meta?.tools ?? [];
  // `reason` is a stable code from the backend; `detail` follows the interface language, so
  // matching on the message text would stop working as soon as the UI language changes.
  const offline = attempts.some((a) => a.status === "skipped" && a.reason === "offline");
  const tip = attempts
    .map((a) => `${modelLabel(a.model_id, models)}: ` + (a.status === "ok"
      ? t("Succeeded")
      : a.status === "skipped" ? t("Skipped ({detail})", { detail: a.detail ?? "" }) : t("Failed ({detail})", { detail: a.detail ?? "" })))
    .join("\n");
  const { decl, rest } = mine ? { decl: null, rest: m.content } : splitDeclaration(m.content, !!m.streaming);
  const taskChip = m.meta?.task_id
    ? m.meta.task_id === "final"
      ? t("Consolidated")
      : t("Task · {title}", { title: m.meta.task_title || m.meta.task_id })
    : m.meta?.task_title
      ? t("Task · {title}", { title: m.meta.task_title })
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
            <span className="decl-tag">{t("Assignment")}</span>
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
            {m.meta?.engine && m.meta.level && <span className="chip ext-lv" title={t("Permission level it had for this reply")}>{levelLabel(m.meta.level)}</span>}
            {m.meta?.external?.duration_ms ? <span className="chip" title={t("Time taken for this reply")}>{t("{n} s", { n: Math.round(m.meta.external.duration_ms / 1000) })}</span> : null}
            {m.fallback_from &&
              (offline ? (
                <span className="chip"><Lock size={11} /> {t("Offline mode")}</span>
              ) : (
                <span className="chip warn"><CornerUpLeft size={11} /> {t("Fell back from {model}", { model: modelLabel(m.fallback_from, models) })}</span>
              ))}
          </div>
        )}
        {!mine && (m.meta?.denied?.length ?? 0) > 0 && (
          <div className="ext-denied" role="note">
            {t("Blocked by the permission level you set: {tools} was not run. Adjust the level under the member's settings if it should be allowed.", { tools: m.meta!.denied!.join(pick(", ", "、")) })}
          </div>
        )}
      </div>
    </div>
  );
}

export default memo(Bubble);
