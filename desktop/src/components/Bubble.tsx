import { memo, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import MessageFile from "./MessageFile";
import MessageVideo from "./MessageVideo";
import MessageImageGen from "./MessageImageGen";
import MessageLook from "./MessageLook";
import MessageActions from "./MessageActions";
import remarkGfm from "remark-gfm";
import { Brain, Check, ChevronDown, ChevronRight, CornerUpLeft, Lock, LoaderCircle, Megaphone, ShieldAlert, ShieldOff, Wrench, X } from "lucide-react";
import { modelLabel, type Agent, type Attachment, type Group, type Message, type MessageFeedback, type Model, type ToolCall } from "../api";
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

/** Translator, as `useI18n()` hands it out. */
type TFn = (key: string, vars?: Record<string, string | number>) => string;

/** Arguments that carry the meaning of a call when the tool has no wording of its own: the
 *  command, the file, the query. Ordered, because the first one present wins. */
const DETAIL_KEYS = ["command", "cmd", "query", "file_path", "path", "file", "url", "pattern",
                     "glob", "title", "doc", "prompt", "description", "question", "action", "content"];

const oneLine = (s: string) => s.replace(/\s+/g, " ").trim();
const firstLine = (s: string) => (s.split("\n").find((l) => l.trim()) ?? "").trim();

/** What a call *does*, in words — "Run a command · npm run build".
 *
 * A tool name on its own answers nothing: the members call `Bash`, `Read`, `make_figure`, and the
 * user reads "bash" with no idea what is being run or on what. The verb comes from the tool
 * (translated), the object from the argument that matters.
 *
 * The wording is written as literal `t()` calls rather than a name → label table on purpose: a
 * table of English keys is invisible to `scripts/check-i18n.py`, and a missing Chinese entry would
 * then show English in the middle of a Chinese screen with nothing to catch it.
 *
 * Label `""` means "no wording for this tool" — the caller falls back to the tool's display name.
 */
function describeCall(name: string, args: Record<string, unknown>, t: TFn): { label: string; detail: string } {
  const arg = (...keys: string[]): string => {
    for (const k of keys) {
      const v = args[k];
      if (typeof v === "string" && v.trim()) return oneLine(v);
      if (typeof v === "number" || typeof v === "boolean") return String(v);
    }
    return "";
  };
  switch (name) {
    // Engine tools, named as the vendor names them (Claude Code / CodeBuddy / WorkBuddy): this is
    // exactly the set of names that mean nothing on their own.
    case "Bash": case "bash":
      return { label: t("Run a command"), detail: arg("command", "cmd", "description") };
    case "Read": case "NotebookRead":
      return { label: t("Read a file"), detail: arg("file_path", "path", "file") };
    case "Write":
      return { label: t("Write a file"), detail: arg("file_path", "path", "file") };
    case "Edit": case "MultiEdit": case "NotebookEdit":
      return { label: t("Edit a file"), detail: arg("file_path", "path", "file") };
    case "Grep": return { label: t("Search inside files"), detail: arg("pattern", "query", "path") };
    case "Glob": case "LS": return { label: t("Find files"), detail: arg("pattern", "glob", "path") };
    case "WebFetch": return { label: t("Fetch a web page"), detail: arg("url") };
    case "WebSearch": return { label: t("Search the web"), detail: arg("query") };
    case "Task": case "Agent": return { label: t("Hand off a subtask"), detail: arg("description", "prompt") };
    case "TodoWrite": return { label: t("Update the task plan"), detail: "" };
    // This app's own built-ins.
    case "run_code":
      return { label: t("Run code"),
               detail: [arg("language") || "python", firstLine(String(args.code ?? ""))].filter(Boolean).join(" · ") };
    case "current_time": return { label: t("Check the time"), detail: "" };
    case "library_search": return { label: t("Search the library"), detail: arg("query") };
    case "library_read": return { label: t("Read from the library"), detail: arg("doc") };
    case "memory_search": return { label: t("Search memory"), detail: arg("query") };
    case "memory_save": return { label: t("Save to memory"), detail: arg("content") };
    case "generate_video": return { label: t("Generate a video"), detail: arg("prompt") };
    case "generate_image": return { label: t("Generate an image"), detail: arg("prompt") };
    case "make_animation": return { label: t("Render an animation"), detail: arg("name", "prompt") };
    case "make_music": return { label: t("Compose music"), detail: arg("prompt", "name") };
    case "assemble_video": return { label: t("Assemble a film"), detail: arg("name", "title") };
    case "study_video": return { label: t("Study a video"), detail: arg("url", "path", "file") };
    case "make_figure": return { label: t("Draw a figure"), detail: arg("figure", "doc") };
    case "list_figures": return { label: t("List figures"), detail: arg("doc") };
    case "write_document": return { label: t("Write a document"), detail: arg("title", "name") };
    case "render_document": return { label: t("Preview document pages"), detail: arg("path") };
    case "find_team_resources": return { label: pick("Find members and tools", "查找成员和工具"), detail: arg("query") };
    case "list_workspace_files": return { label: pick("Find workspace files", "查找群内文件"), detail: arg("query") };
    case "invite_team_resource": return { label: pick("Add to this group", "拉入本群"), detail: arg("ref") };
    case "synthesize_speech": return { label: t("Synthesize cloned speech"), detail: arg("voice", "text") };
    case "review_picture": return { label: t("Review a picture"), detail: arg("file", "path") };
    case "review_audio": return { label: t("Review audio"), detail: arg("file", "path") };
    case "ask_advisor": return { label: t("Ask another model"), detail: arg("question") };
    case "process_log": return { label: t("Process log"), detail: arg("action", "key") };
    default: break;
  }
  // A plugin, an MCP server, or a tool this build has never heard of: keep its own name (that is
  // what the member called) and read the arguments for the one that carries meaning.
  return { label: "", detail: arg(...DETAIL_KEYS) };
}

/** Seconds a call has been running, ticked on screen. Only counts while it is running, so a pill
 *  restored from history does not pretend to be counting from the moment the page opened. */
function useElapsed(active: boolean): number {
  const start = useRef(Date.now());
  const [, tick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => tick((n) => n + 1), 100);
    return () => window.clearInterval(id);
  }, [active]);
  return Math.max(0, (Date.now() - start.current) / 1000);
}

function ToolPill({ call, sources, gid }: { call: ToolCall; sources?: Map<string, string>; gid?: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const { label: display, source } = toolDisplay(call.name, sources);
  const described = describeCall(call.name, call.args ?? {}, t);
  const label = described.label || display;
  const args = Object.entries(call.args ?? {});
  const running = call.status === "running";
  // While it runs there is nothing to click through to yet, and "what is it doing" is the whole
  // question — so the body is open for the duration and folds back to one line when it finishes.
  // Opening it by hand keeps it open, which is what a user reading a result expects.
  const bodyOpen = open || running;
  const seconds = useElapsed(running);
  // Shown without unfolding the pill: a generated clip is the point of the call, and hiding it
  // behind a click makes the result look like a log line.
  // Three sources, one question each — what the call *made* (a clip or a picture, fetched out of the
  // generator's own folder by bare name) and what it *looked at* (any file in the workspace, fetched
  // by path). A review returns the second kind, which is why `where` exists rather than a guess at
  // which folder a name belongs in.
  // Canonical paths also distinguish identically named files in different task folders.
  // Older make_figure messages omitted the folder and were fetched from image/ by mistake.
  const files = (call.files ?? []).map((f) => ({ ...f,
    path: f.path || (call.name === "make_figure" && f.kind === "image"
      ? (f.name.includes("/") ? f.name : `figures/${f.name}`) : undefined),
  }));
  const inWorkspace = (f: typeof files[number]) => f.where === "workspace"
    || Boolean(f.path && ["image", "video", "audio"].includes(f.kind));
  const mine = files.filter((f) => !inWorkspace(f));
  const shown = files.filter(inWorkspace);
  const clips = mine.filter((f) => f.kind === "video");
  const drawn = mine.filter((f) => f.kind === "image");
  const live = useRef<HTMLPreElement>(null);
  useEffect(() => {
    const el = live.current;
    if (el) el.scrollTop = el.scrollHeight;      // a terminal tail: the newest line is the point
  }, [call.live]);
  const stateText = {
    running: t("Running"), waiting: t("Waiting for you"), ok: t("Succeeded"), failed: t("Failed"), denied: t("Denied"),
  }[call.status];
  return (
    <div className={"tool-pill " + call.status + (bodyOpen ? " open" : "")}>
      <button className="tool-pill-head" aria-expanded={bodyOpen}
        aria-label={t("Tool {name}, {state}", { name: label, state: stateText })} onClick={() => setOpen((v) => !v)}>
        {running ? (
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
        {described.detail && <span className="tp-arg" title={described.detail}>{described.detail}</span>}
        {source === "mcp" && <span className="tp-src">mcp</span>}
        {source === "plugin" && <span className="tp-src">{t("plugin")}</span>}
        {(call.status === "waiting" || call.status === "denied") && <span className="tp-src">{stateText}</span>}
        {running ? (
          <span className="tp-ms" title={t("Running")}>{seconds.toFixed(1)} s</span>
        ) : (
          call.status !== "waiting" && typeof call.ms === "number" && <span className="tp-ms">{call.ms} ms</span>
        )}
        {bodyOpen ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
      </button>
      {gid && shown.length > 0 && (
        <div className="tool-pill-files">
          {shown.map((f) => <MessageLook key={f.path || f.name} gid={gid} name={f.path || f.name} kind={f.kind} bytes={f.bytes} />)}
        </div>
      )}
      {gid && clips.length > 0 && (
        <div className="tool-pill-files">
          {clips.map((f) => <MessageVideo key={f.name} gid={gid} name={f.name} bytes={f.bytes} />)}
        </div>
      )}
      {gid && drawn.length > 0 && (
        <div className="tool-pill-files">
          {drawn.map((f) => <MessageImageGen key={f.name} gid={gid} name={f.name} bytes={f.bytes} />)}
        </div>
      )}
      {bodyOpen && (
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
          <div className="tp-sec">{running ? t("Live output") : t("Result preview")}</div>
          {running ? (
            call.live ? (
              <pre className="tp-pre live" ref={live}>{call.live}</pre>
            ) : (
              <div className="tp-empty"><LoaderCircle size={11} className="spin" aria-hidden /> {t("Running…")}</div>
            )
          ) : call.status === "waiting" ? (
            <div className="tp-empty">{t("Waiting for your approval at the bottom of the chat…")}</div>
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

/** The member's working, when it has any to show: a reasoning model's train of thought, or the plan
 * an engine writes out before acting on it.
 *
 * Open while it is arriving — watching it is the reason it is on screen at all — and folded away
 * once the reply is done, because by then the answer is what the reader came for. A click pins it
 * either way, so a reader who is following the reasoning keeps it.
 */
function Thinking({ text, streaming }: { text: string; streaming: boolean }) {
  const { t } = useI18n();
  const [pinned, setPinned] = useState<boolean | null>(null);
  const open = pinned ?? streaming;
  const body = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = body.current;
    if (el) el.scrollTop = el.scrollHeight;    // it is being written: the newest line is the point
  }, [text]);
  return (
    <div className={"think" + (open ? " open" : "") + (streaming ? " live" : "")}>
      <button className="think-head" aria-expanded={open} onClick={() => setPinned(!open)}>
        <Brain size={12} aria-hidden />
        <span className="think-title">{streaming ? t("Thinking…") : t("Reasoning")}</span>
        <span className="think-hint">{open ? t("Hide") : t("Show")}</span>
        {open ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
      </button>
      {open && <div className="think-body" ref={body}>{text}</div>}
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
  /** This group — needed by the keys that act on a message (rate, forward, read aloud). */
  gid?: string;
  /** Every group, for the forward picker. */
  groups?: Group[];
  /** The verdict already given on this message, if any. */
  judged?: MessageFeedback;
  /** Put a reference to this message into the composer (`@msg:<id>`), so the members read it
   *  again instead of relying on it still being inside the context window — or, for a `whole:false`
   *  quote, the passage the user selected. */
  onQuote?: (text: string, whole: boolean) => void;
}

function Bubble({ m, agent, models, toolSources, highlight, gid, groups, judged, onQuote }: Props) {
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
  // `files` since attachments became any kind of file; `images` is what a message stored
  // before that is called, and both have to keep rendering.
  const pics: Attachment[] = m.meta?.files ?? m.meta?.images ?? [];
  const taskChip = m.meta?.task_id
    ? m.meta.task_id === "final"
      ? t("Consolidated")
      : t("Task · {title}", { title: m.meta.task_title || m.meta.task_id })
    : m.meta?.task_title
      ? t("Task · {title}", { title: m.meta.task_title })
      : "";
  // ⚠️ `!!m.streaming` and not `decl === null`: a message whose **whole** content was a tool call has
  // no body left after the backend strips the protocol, and the old condition drew the bubble anyway —
  // which renders as the three typing dots, so a finished message from yesterday looks like it is
  // still working. Nothing to say means no bubble; a live one still shows the dots.
  const showBubble = mine || rest !== "" || !!m.streaming;
  const reasoning = mine ? "" : (m.meta?.thinking ?? "").trim();
  // Images stand on their own: a message may be nothing but a picture, and then the empty bubble
  // underneath would just be a stray line.
  if (pics.length && !mine && rest === "") return (
    <div className={"msg theirs" + (highlight ? " hl" : "")} data-mid={m.id}>
      <div className="avatar">{agent?.avatar ?? "🤖"}</div>
      <div className="msg-body">
        <div className="msg-name">{m.sender_name}{agent?.role && <span className="msg-role">{agent.role}</span>}</div>
        <div className="msg-files">{pics.map((f) => <MessageFile key={f.id} file={f} />)}</div>
      </div>
    </div>
  );
  return (
    <div className={"msg " + (mine ? "mine" : "theirs") + (highlight ? " hl" : "")} data-mid={m.id}>
      <div className="avatar">{mine ? "🙂" : agent?.avatar ?? "🤖"}</div>
      <div className="msg-body">
        <div className="msg-name">
          {m.sender_name}
          {!mine && agent?.role && <span className="msg-role">{agent.role}</span>}
          {taskChip && <span className={"chip task-chip" + (m.meta?.task_id === "final" ? " final" : "")} title={m.meta?.task_title}>{taskChip}</span>}
        </div>
        {reasoning && <Thinking text={reasoning} streaming={!!m.streaming} />}
        {decl !== null && (
          <div className={"decl-bar" + (showBubble ? "" : " alone")}>
            <Megaphone size={12} aria-hidden />
            <span className="decl-tag">{t("Assignment")}</span>
            <span className="decl-text">{decl}</span>
          </div>
        )}
        {showBubble && (
          <div className={"bubble" + (decl !== null ? " under-decl" : "")}>
            {pics.length > 0 && (
              <div className="msg-files">{pics.map((f) => <MessageFile key={f.id} file={f} />)}</div>
            )}
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
            {tools.map((c, i) => (c ? <ToolPill key={i} call={c} sources={toolSources} gid={m.group_id} /> : null))}
          </div>
        )}
        {/* 功能键:复制 / 评价 / 转发 / 朗读 / 引用。成员发言才有 —— 你自己那条没有人为它负责,
            系统提示是程序写的,给它们打分只会污染反馈体系。 */}
        {!mine && gid && (
          <MessageActions m={m} gid={gid} groups={groups ?? []} initial={judged}
            onQuote={onQuote} />
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
            {m.fallback_from && m.meta?.routing_auto && <span className="chip">{pick("Matched to this task", "按任务能力切换")}</span>}
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
