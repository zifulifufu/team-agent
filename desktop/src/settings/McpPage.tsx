import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppWindow, ChevronRight, ExternalLink, FileJson, Pencil, Plus, Trash2, X } from "lucide-react";
import { api, type McpServer, type McpTemplate } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Modal, Switch, useConfirm } from "../ui";
import { Callout, GithubMark, Spin } from "../components/ExtBits";
import { RepoDiscoverModal } from "../components/RepoDiscover";
import McpImportModal from "../components/McpImport";
import ImportFromApps from "../components/ImportFromApps";
import "../styles/ext.css";

const MASK = "••••••";
// English keys, translated where they are rendered — a module-level tr() would be evaluated once, at
// import time, and freeze this page in whatever language happened to be current then.
const STATUS_TEXT: Record<string, string> = { ready: "Connected", connecting: "Connecting…", error: "Connection failed", idle: "Not connected" };
const TRANSPORT_TEXT: Record<string, string> = { stdio: "Local command · stdio", http: "Remote · http", sse: "Remote · sse" };
export const hasPlaceholder = (s: string) => /\/path\/to\//.test(s);

/** Highlight the /path/to/… placeholders inside a string. */
function Highlight({ text }: { text: string }) {
  const parts = text.split(/(\/path\/to\/[^\s"]*)/g);
  return <>{parts.map((p, i) => (hasPlaceholder(p) ? <mark key={i} className="ext-ph">{p}</mark> : <Fragment key={i}>{p}</Fragment>))}</>;
}

interface KvRow { id: number; k: string; v: string; masked: boolean }
export interface FormInit {
  name: string;
  description: string;
  remote: boolean;
  command: string;
  args: string[];
  url: string;
  transport: string;   // "" = automatic
  env: Record<string, string>;
  headers: Record<string, string>;
}
export const EMPTY_INIT: FormInit = { name: "", description: "", remote: false, command: "", args: [], url: "", transport: "", env: {}, headers: {} };

type Dialog = { server: McpServer | null; init: FormInit } | null;

const MARKETS = [   // English keys; translated where they are rendered (see STATUS_TEXT above)
  { name: "Official reference servers", url: "https://github.com/modelcontextprotocol/servers", note: "The official MCP repository, with reference implementations for filesystem, web fetching, Git, memory, and more." },
  { name: "Official MCP registry", url: "https://registry.modelcontextprotocol.io", note: "The officially maintained server registry." },
  { name: "mcp.so", url: "https://mcp.so", note: "A community-collected directory of MCP servers." },
  { name: "Smithery", url: "https://smithery.ai", note: "A third-party marketplace for MCP servers." },
];

import type { SettingsTab } from "./SettingsModal";

export default function McpPage({ onTab }: { onTab?: (t: SettingsTab) => void } = {}) {
  const { t } = useI18n();
  const { groups, reloadGroups } = useData();
  const confirm = useConfirm();
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [templates, setTemplates] = useState<McpTemplate[]>([]);
  const [err, setErr] = useState("");
  const [dialog, setDialog] = useState<Dialog>(null);
  const [discover, setDiscover] = useState(false);
  const [importing, setImporting] = useState(false);
  const [fromApps, setFromApps] = useState(false);
  const [note, setNote] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<Record<string, string>>({});       // id → what it is doing
  const [rowErr, setRowErr] = useState<Record<string, string>>({});
  const timer = useRef<number>();

  const load = useCallback(async () => {
    try {
      setServers(await api.mcp());
      setErr("");
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
    api.mcpTemplates().then(setTemplates).catch(() => undefined);
  }, [load]);

  // While a server is still connecting (a connection triggered elsewhere, say), poll until it finishes
  useEffect(() => {
    window.clearTimeout(timer.current);
    if (servers?.some((s) => s.status === "connecting")) timer.current = window.setTimeout(() => void load(), 1500);
    return () => window.clearTimeout(timer.current);
  }, [servers, load]);

  const setB = (id: string, what: string) => setBusy((b) => { const n = { ...b }; if (what) n[id] = what; else delete n[id]; return n; });
  const setE = (id: string, msg: string) => setRowErr((r) => ({ ...r, [id]: msg }));
  const replace = (m: McpServer) => setServers((l) => l?.map((x) => (x.id === m.id ? m : x)) ?? l);

  const connect = async (s: McpServer) => {
    setB(s.id, "connect");
    setE(s.id, "");
    setOpen((o) => new Set(o).add(s.id));
    try {
      replace(await api.connectMcp(s.id));
    } catch (e) {
      setE(s.id, (e as Error).message);   // A remote server returns 403 when outbound calls are off; show the original text
      await load();
    } finally {
      setB(s.id, "");
    }
  };
  const disconnect = async (s: McpServer) => {
    setB(s.id, "disconnect");
    try { replace(await api.disconnectMcp(s.id)); setE(s.id, ""); } catch (e) { setE(s.id, (e as Error).message); } finally { setB(s.id, ""); }
  };
  const toggle = async (s: McpServer, enabled: boolean) => {
    setB(s.id, "toggle");
    try { replace(await api.patchMcp(s.id, { enabled })); setE(s.id, ""); } catch (e) { setE(s.id, (e as Error).message); } finally { setB(s.id, ""); }
  };
  const remove = async (s: McpServer) => {
    const used = groups.filter((g) => g.ext?.mcp?.includes(s.id));
    const extra = used.length ? t("It is enabled in {n} group(s) and deleting it removes it from them.", { n: used.length }) : "";
    if (!(await confirm(t("Delete the MCP server {name}? Its connection is closed first. {extra}", { name: s.name, extra }), { okText: t("Delete") }))) return;
    try { await api.delMcp(s.id); await load(); await reloadGroups(); } catch (e) { setE(s.id, (e as Error).message); }
  };

  const openAdd = (init: Partial<FormInit> = {}) => setDialog({ server: null, init: { ...EMPTY_INIT, ...init } });
  const openEdit = (s: McpServer) =>
    setDialog({
      server: s,
      init: {
        name: s.name, description: s.description, remote: s.transport_effective !== "stdio" || (!s.command && !!s.url),
        command: s.command, args: s.args, url: s.url,
        transport: s.transport === "stdio" ? "" : s.transport,
        env: s.env, headers: s.headers,
      },
    });

  return (
    <div className="sp">
      <div className="sp-head">
        <h2 className="sp-title">{t("MCP servers")}</h2>
        <div className="sp-head-actions">
          <button className="btn" onClick={() => setFromApps(true)}><AppWindow size={14} /> {t("From another app")}</button>
          <button className="btn" onClick={() => setImporting(true)}><FileJson size={14} /> {t("Import JSON")}</button>
          <button className="btn" onClick={() => setDiscover(true)}><GithubMark size={14} /> {t("Discover on GitHub")}</button>
          <button className="btn primary" onClick={() => openAdd()}><Plus size={15} /> {t("Add an MCP server")}</button>
        </div>
      </div>
      <p className="sp-desc">
        {t("An MCP server is a local process (or a remote service) that members use to reach external tools such as reading and writing files or fetching web pages. It is not the same thing as a plugin. Once you enable it under Extensions in the chat's right-hand panel, it connects the first time it is actually used; you can also connect it here by hand to see whether it works.")}{onTab && <> <button className="link" onClick={() => onTab("gallery")}>{t("Template gallery")}</button>{t(" has some ready-made MCP setups (always imported disabled).")}</>}
      </p>
      <Callout tone="warn" title={t("An MCP server runs commands with your privileges")}>
        {t("Local commands run on your computer with your account's privileges, so only add servers you trust. Remote services receive whatever members send them.")}
      </Callout>

      {note && <div className="ok-text" role="status" style={{ marginBottom: 8 }}>{note}</div>}
      {err && <div className="ext-errbox"><div className="err">{t("Failed to load: {err}", { err })}</div><button className="btn small" onClick={() => void load()}>{t("Retry")}</button></div>}
      {!servers && !err && <div className="empty"><Spin /> {t("Loading…")}</div>}
      {servers && (
        <div className="card flush">
          {servers.length === 0 && (
            <div className="empty" style={{ lineHeight: 1.8 }}>
              {t("No MCP servers yet. Start from a template below (it pre-fills the form for you to confirm and save),")}<br />{t("or click Add an MCP server to fill it in by hand.")}
            </div>
          )}
          {servers.map((s) => {
            const st = busy[s.id] === "connect" ? "connecting" : s.status;
            const isOpen = open.has(s.id);
            const eff = s.transport_effective;
            const inGroups = groups.filter((g) => g.ext?.mcp?.includes(s.id)).length;
            return (
              <div key={s.id} className={"ext-item" + (st === "error" ? " err-state" : "")}>
                <div className="model-row">
                  <button className="icon-btn tiny ext-toggle" data-open={isOpen} aria-expanded={isOpen} aria-label={t("{action} details for {name}", { action: isOpen ? t("Collapse") : t("Expand"), name: s.name })}
                    onClick={() => setOpen((o) => { const n = new Set(o); if (n.has(s.id)) n.delete(s.id); else n.add(s.id); return n; })}>
                    <span className={isOpen ? "ext-rot" : ""} style={{ display: "inline-flex", transform: isOpen ? "rotate(90deg)" : "none", transition: "transform .12s" }}><ChevronRight size={14} /></span>
                  </button>
                  <span className={"ext-dot " + st} title={t(STATUS_TEXT[st])} role="img" aria-label={t(STATUS_TEXT[st])} />
                  <div className="mr-main" style={{ opacity: s.enabled ? 1 : 0.55 }}>
                    <div className="mr-name">
                      {s.name}
                      <span className="tag">{TRANSPORT_TEXT[eff] ? t(TRANSPORT_TEXT[eff]) : eff}</span>
                      <span className={"ext-status-text " + st}>{t(STATUS_TEXT[st])}</span>
                      {!s.enabled && <span className="tag warn">{t("Disabled")}</span>}
                    </div>
                    <div className="mr-id" title={eff === "stdio" ? [s.command, ...s.args].join(" ") : s.url}>
                      {eff === "stdio" ? [s.command, ...s.args].join(" ") : s.url}
                    </div>
                    <div className="ext-item-sub">
                      {s.status === "ready" ? t("{n} tools", { n: s.tools.length }) : t("Tools are listed once connected")}
                      {inGroups ? t(" · enabled in {n} groups", { n: inGroups }) : t(" · not enabled in any group yet")}
                    </div>
                    {(rowErr[s.id] || (st === "error" && s.error)) && <div className="ext-errline">{rowErr[s.id] || s.error}</div>}
                  </div>
                  <div className="ext-item-actions">
                    {s.status === "ready" ? (
                      <button className="btn small" disabled={!!busy[s.id]} onClick={() => void disconnect(s)}>{busy[s.id] === "disconnect" ? <Spin size={12} /> : null}{t("Disconnect")}</button>
                    ) : (
                      <button className="btn small" disabled={!!busy[s.id] || s.status === "connecting" || !s.enabled} title={s.enabled ? "" : t("Enable this server first")} onClick={() => void connect(s)}>
                        {busy[s.id] === "connect" || s.status === "connecting" ? <><Spin size={12} /> {t("Connecting")}</> : t("Connect")}
                      </button>
                    )}
                    <Switch checked={s.enabled} disabled={!!busy[s.id]} label={t("Enable {name}", { name: s.name })} onChange={(v) => void toggle(s, v)} />
                    <button className="icon-btn" title={t("Edit")} aria-label={t("Edit {name}", { name: s.name })} onClick={() => openEdit(s)}><Pencil size={15} /></button>
                    <button className="icon-btn" title={t("Delete")} aria-label={t("Delete {name}", { name: s.name })} onClick={() => void remove(s)}><Trash2 size={15} /></button>
                  </div>
                </div>
                {isOpen && (
                  <div className="ext-item-detail">
                    {s.description && <div className="ext-item-desc" style={{ marginTop: 0 }}>{s.description}</div>}
                    {s.tools.length > 0 ? (
                      <div className="ext-tool-list" aria-label={t("Tools of {name}", { name: s.name })}>
                        {s.tools.map((tool) => (
                          <div key={tool.name} className="ext-tool">
                            <code>{tool.name}</code>
                            {tool.read_only && <span className="tag on" title={t("The server declares this tool read-only")}>{t("Read-only")}</span>}
                            <span className="muted">{tool.description}</span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="muted small" style={{ marginTop: 6 }}>{t("No tool information yet — click Connect and the tools this server provides will be listed.")}</div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="sec">{t("Templates")}</div>
      <p className="muted small" style={{ margin: "-4px 0 10px", lineHeight: 1.7 }}>{t("Clicking a template only fills the form in; it is not saved straight away. Check the command, then save.")}</p>
      <div className="ext-tpl-grid">
        {templates.map((tpl) => {
          const ph = tpl.args.some(hasPlaceholder);
          return (
            <button key={tpl.name} className="ext-tpl" onClick={() => openAdd({ name: tpl.name, description: tpl.note, command: tpl.command, args: tpl.args })}>
              <b>{tpl.name}{ph && <span className="tag warn">{t("Path needs changing")}</span>}</b>
              <span className="ext-tpl-cmd"><Highlight text={[tpl.command, ...tpl.args].join(" ")} /></span>
              <span className="ext-tpl-note">{tpl.note}</span>
            </button>
          );
        })}
        {templates.length === 0 && <div className="muted small">{t("No templates available.")}</div>}
      </div>

      <div className="sec">{t("Where to find more")}</div>
      <p className="muted small" style={{ margin: "-4px 0 10px", lineHeight: 1.7 }}>
        {t("These are third-party sites; their content and quality are their responsibility and this app has not verified them. Once you find a server you like, copy its config JSON and add it with Import JSON above — read the command before deciding whether to use it.")}
      </p>
      <div className="ext-tpl-grid">
        {MARKETS.map((m) => (
          <a key={m.url} className="ext-tpl" href={m.url} target="_blank" rel="noreferrer">
            <b>{t(m.name)} <ExternalLink size={12} aria-hidden /></b>
            <span className="ext-tpl-note">{t(m.note)}</span>
          </a>
        ))}
      </div>

      {dialog && (
        <McpDialog
          server={dialog.server}
          init={dialog.init}
          onClose={() => setDialog(null)}
          onSaved={async () => { setDialog(null); await load(); }}
        />
      )}
      {fromApps && (
        <ImportFromApps
          kind="mcp"
          onClose={() => setFromApps(false)}
          onDone={async (added, skipped) => {
            setFromApps(false);
            setNote(added
              ? t("Imported {n} server(s) from another app. They are not connected to any group and they are not running.", { n: added })
              : t("Nothing new was imported ({n} already existed).", { n: skipped }));
            await load();
          }}
        />
      )}
      {importing && (
        <McpImportModal
          onClose={() => setImporting(false)}
          onDone={async (n) => { setImporting(false); setNote(n ? t("Imported {n} servers. They are not enabled in any group yet, and they are not running.", { n }) : t("No new servers were imported (ones with the same name were skipped).")); await load(); }}
        />
      )}
      {discover && (
        <RepoDiscoverModal
          kind="mcp"
          onClose={() => setDiscover(false)}
          onPrefillMcp={(name, description) => openAdd({ name, description })}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Form
let rowId = 1;
const toRows = (o: Record<string, string>): KvRow[] => Object.entries(o).map(([k, v]) => ({ id: rowId++, k, v: v === MASK ? "" : v, masked: v === MASK }));

function KvEditor({ rows, setRows, kPlaceholder, vPlaceholder, label }: { rows: KvRow[]; setRows: (r: KvRow[]) => void; kPlaceholder: string; vPlaceholder: string; label: string }) {
  const { t } = useI18n();
  const upd = (id: number, patch: Partial<KvRow>) => setRows(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  return (
    <div>
      {rows.map((r) => (
        <div key={r.id} className="ext-kv">
          <input value={r.k} readOnly={r.masked} onChange={(e) => upd(r.id, { k: e.target.value })} placeholder={kPlaceholder} aria-label={t("{label} name", { label })} spellCheck={false} title={r.masked ? t("A saved entry cannot be renamed; delete it and add a new row instead") : ""} />
          <input
            className={r.masked && !r.v ? "masked" : ""}
            type={r.masked && !r.v ? "text" : "password"}
            value={r.v}
            onChange={(e) => upd(r.id, { v: e.target.value })}
            placeholder={r.masked ? t("Already set (hidden)") : vPlaceholder}
            aria-label={t("{label} value", { label })}
            autoComplete="off"
            spellCheck={false}
          />
          <button type="button" className="icon-btn tiny" title={t("Delete this row")} aria-label={t("Delete {label} {name}", { label, name: r.k || "" })} onClick={() => setRows(rows.filter((x) => x.id !== r.id))}><X size={14} /></button>
        </div>
      ))}
      <button type="button" className="btn small" onClick={() => setRows([...rows, { id: rowId++, k: "", v: "", masked: false }])}><Plus size={13} /> {t("Add {label}", { label })}</button>
    </div>
  );
}

export function McpDialog({ server, init, onClose, onSaved }: { server: McpServer | null; init: FormInit; onClose: () => void; onSaved: (s: McpServer) => Promise<void> }) {
  const { t } = useI18n();
  const confirm = useConfirm();
  const [name, setName] = useState(init.name);
  const [description, setDescription] = useState(init.description);
  const [remote, setRemote] = useState(init.remote);
  const [command, setCommand] = useState(init.command);
  const [argsText, setArgsText] = useState(init.args.join("\n"));
  const [url, setUrl] = useState(init.url);
  const [transport, setTransport] = useState(init.transport);
  const [env, setEnv] = useState<KvRow[]>(() => toRows(init.env));
  const [headers, setHeaders] = useState<KvRow[]>(() => toRows(init.headers));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const args = useMemo(() => argsText.split("\n").map((l) => l.replace(/\r$/, "")).filter((l) => l.trim() !== ""), [argsText]);
  const phLines = args.map((a, i) => (hasPlaceholder(a) ? i + 1 : 0)).filter(Boolean);

  const kv = (rows: KvRow[]) => Object.fromEntries(rows.filter((r) => r.k.trim()).map((r) => [r.k.trim(), r.masked && r.v === "" ? MASK : r.v]));

  const save = async () => {
    if (!remote && phLines.length && !(await confirm(t("The arguments still contain /path/to/ placeholder paths (line {lines}). These are not real paths, so the server most likely will not start. Save anyway?", { lines: phLines.join(", ") }), { okText: t("Save anyway"), danger: false }))) return;
    setBusy(true);
    setErr("");
    const body: Record<string, unknown> = remote
      ? { name: name.trim(), description: description.trim(), command: "", args: [], env: {}, url: url.trim(), transport, headers: kv(headers) }
      : { name: name.trim(), description: description.trim(), command: command.trim(), args, env: kv(env), url: "", transport: "", headers: {} };
    try {
      const saved = server ? await api.patchMcp(server.id, body) : await api.addMcp(body);
      await onSaved(saved);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  const canSave = name.trim() && (remote ? url.trim() : command.trim()) && !busy;
  return (
    <Modal
      title={server ? t("Edit {name}", { name: server.name }) : t("Add an MCP server")}
      onClose={onClose}
      wide
      actions={
        <>
          {err && <span className="err ext-act-err" role="alert">{err}</span>}
          <button className="btn" onClick={onClose}>{t("Cancel")}</button>
          <button className="btn primary" disabled={!canSave} onClick={() => void save()}>{busy ? <><Spin /> {t("Saving…")}</> : t("Save")}</button>
        </>
      }
    >
      <label className="field">
        <span>{t("Name")}</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("e.g. Filesystem")} autoFocus />
      </label>
      <label className="field">
        <span>{t("Description (optional)")}</span>
        <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t("What it can do")} />
      </label>
      <div className="field">
        <span>{t("Connection type")}</span>
        <div className="ext-radio-row" role="radiogroup" aria-label={t("Connection type")}>
          <label className={"check" + (!remote ? " on" : "")}>
            <input type="radio" name="mcp-mode" checked={!remote} onChange={() => setRemote(false)} />
            <span>{t("Local command")}<small>{t("Starts a process on this machine (stdio)")}</small></span>
          </label>
          <label className={"check" + (remote ? " on" : "")}>
            <input type="radio" name="mcp-mode" checked={remote} onChange={() => setRemote(true)} />
            <span>{t("Remote service")}<small>{t("Connects to an http / sse URL; Allow outbound calls is required")}</small></span>
          </label>
        </div>
      </div>

      {!remote ? (
        <>
          <label className="field">
            <span>{t("Start command")}</span>
            <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder={t("e.g. npx / uvx / python")} spellCheck={false} />
          </label>
          <label className="field">
            <span>{t("Arguments (one per line; arguments containing spaces are not split)")}</span>
            <textarea className={"ext-mono-area" + (phLines.length ? " warn" : "")} rows={4} value={argsText} onChange={(e) => setArgsText(e.target.value)} placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/your/directory"} spellCheck={false} />
            {phLines.length > 0 && <span className="ext-field-note warn">{t("The /path/to/… on line {lines} is a placeholder — replace it with your real path.", { lines: phLines.join(", ") })}</span>}
          </label>
          <div className="field">
            <span>{t("Environment variables (for an API key, say)")}</span>
            <KvEditor rows={env} setRows={setEnv} kPlaceholder={t("Variable name, e.g. GITHUB_TOKEN")} vPlaceholder={t("Value")} label={t("Environment variable")} />
            {env.some((r) => r.masked) && <span className="ext-field-note">{t("A value that is already set (hidden) is never echoed back; leave it alone to keep it, or type a new value to replace it.")}</span>}
          </div>
        </>
      ) : (
        <>
          <label className="field">
            <span>{t("Service URL")}</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/mcp" spellCheck={false} />
          </label>
          <label className="field">
            <span>{t("Transport")}</span>
            <select value={transport} onChange={(e) => setTransport(e.target.value)}>
              <option value="">{t("Automatic (sse if the URL ends in /sse, otherwise http)")}</option>
              <option value="http">http(Streamable HTTP)</option>
              <option value="sse">sse</option>
            </select>
          </label>
          <div className="field">
            <span>{t("Request headers (e.g. Authorization)")}</span>
            <KvEditor rows={headers} setRows={setHeaders} kPlaceholder={t("Name, e.g. Authorization")} vPlaceholder={t("Value")} label={t("Request header")} />
            {headers.some((r) => r.masked) && <span className="ext-field-note">{t("A value that is already set (hidden) is never echoed back; leave it alone to keep it, or type a new value to replace it.")}</span>}
          </div>
        </>
      )}
      <p className="ext-field-note" style={{ margin: "4px 0 0" }}>{t("Saving does not connect it immediately; click Connect in the list to try it, or enable it for a group and it connects the first time it is used.")}</p>
    </Modal>
  );
}
