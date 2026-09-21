import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, ExternalLink, FileJson, Pencil, Plus, Trash2, X } from "lucide-react";
import { api, type McpServer, type McpTemplate } from "../api";
import { useData } from "../data";
import { Modal, Switch, useConfirm } from "../ui";
import { Callout, GithubMark, Spin } from "../components/ExtBits";
import { RepoDiscoverModal } from "../components/RepoDiscover";
import McpImportModal from "../components/McpImport";
import "../styles/ext.css";

const MASK = "••••••";
const STATUS_TEXT: Record<string, string> = { ready: "已连接", connecting: "连接中…", error: "连接失败", idle: "未连接" };
const TRANSPORT_TEXT: Record<string, string> = { stdio: "本地命令 · stdio", http: "远程 · http", sse: "远程 · sse" };
export const hasPlaceholder = (s: string) => /\/path\/to\//.test(s);

/** 把带 /path/to/… 占位的文字里的占位部分高亮出来。 */
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
  transport: string;   // "" = 自动
  env: Record<string, string>;
  headers: Record<string, string>;
}
export const EMPTY_INIT: FormInit = { name: "", description: "", remote: false, command: "", args: [], url: "", transport: "", env: {}, headers: {} };

type Dialog = { server: McpServer | null; init: FormInit } | null;

const MARKETS = [
  { name: "官方参考服务器", url: "https://github.com/modelcontextprotocol/servers", note: "MCP 官方仓库,有文件系统、抓取网页、Git、记忆等参考实现。" },
  { name: "MCP 官方 Registry", url: "https://registry.modelcontextprotocol.io", note: "官方维护的服务器登记处。" },
  { name: "mcp.so", url: "https://mcp.so", note: "社区收集的 MCP 服务器目录。" },
  { name: "Smithery", url: "https://smithery.ai", note: "MCP 服务器的第三方市场。" },
];

import type { SettingsTab } from "./SettingsModal";

export default function McpPage({ onTab }: { onTab?: (t: SettingsTab) => void } = {}) {
  const { groups, reloadGroups } = useData();
  const confirm = useConfirm();
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [templates, setTemplates] = useState<McpTemplate[]>([]);
  const [err, setErr] = useState("");
  const [dialog, setDialog] = useState<Dialog>(null);
  const [discover, setDiscover] = useState(false);
  const [importing, setImporting] = useState(false);
  const [note, setNote] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<Record<string, string>>({});       // id → 正在做什么
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

  // 有服务器还在「连接中」(比如在别处触发的连接)时,轮询到结束
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
      setE(s.id, (e as Error).message);   // 外呼关闭时远程服务器会返回 403,原文显示
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
    const extra = used.length ? `它正在 ${used.length} 个群里启用,删除后会从这些群里移除。` : "";
    if (!(await confirm(`删除 MCP 服务器「${s.name}」?会先断开连接。${extra}`, { okText: "删除" }))) return;
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
        <h2 className="sp-title">MCP 服务器</h2>
        <div className="sp-head-actions">
          <button className="btn" onClick={() => setImporting(true)}><FileJson size={14} /> 导入 JSON</button>
          <button className="btn" onClick={() => setDiscover(true)}><GithubMark size={14} /> 从 GitHub 发现</button>
          <button className="btn primary" onClick={() => openAdd()}><Plus size={15} /> 添加 MCP 服务器</button>
        </div>
      </div>
      <p className="sp-desc">
        MCP 服务器是一个本机进程(或远程服务),成员通过它使用外部工具,如读写文件、抓取网页。它和「插件」是两回事。在群聊右侧面板的「扩展」里启用后,第一次被用到时才会连接;也可以在这里手动连接看看能不能用。{onTab && <> <button className="link" onClick={() => onTab("gallery")}>模板中心</button>里有一些现成的 MCP 用法(导入后一律是停用状态)。</>}
      </p>
      <Callout tone="warn" title="MCP 服务器会以你的权限运行命令">
        本地命令会在你的电脑上以你的账号权限执行,请只添加信任的。远程服务会收到成员发给它的内容。
      </Callout>

      {note && <div className="ok-text" role="status" style={{ marginBottom: 8 }}>{note}</div>}
      {err && <div className="ext-errbox"><div className="err">读取失败:{err}</div><button className="btn small" onClick={() => void load()}>重试</button></div>}
      {!servers && !err && <div className="empty"><Spin /> 加载中…</div>}
      {servers && (
        <div className="card flush">
          {servers.length === 0 && (
            <div className="empty" style={{ lineHeight: 1.8 }}>
              还没有 MCP 服务器。可以从下面的模板开始(会先填好表单,由你确认后保存),<br />或点「添加 MCP 服务器」手动填写。
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
                  <button className="icon-btn tiny ext-toggle" data-open={isOpen} aria-expanded={isOpen} aria-label={`${isOpen ? "收起" : "展开"} ${s.name} 详情`}
                    onClick={() => setOpen((o) => { const n = new Set(o); if (n.has(s.id)) n.delete(s.id); else n.add(s.id); return n; })}>
                    <span className={isOpen ? "ext-rot" : ""} style={{ display: "inline-flex", transform: isOpen ? "rotate(90deg)" : "none", transition: "transform .12s" }}><ChevronRight size={14} /></span>
                  </button>
                  <span className={"ext-dot " + st} title={STATUS_TEXT[st]} role="img" aria-label={STATUS_TEXT[st]} />
                  <div className="mr-main" style={{ opacity: s.enabled ? 1 : 0.55 }}>
                    <div className="mr-name">
                      {s.name}
                      <span className="tag">{TRANSPORT_TEXT[eff] ?? eff}</span>
                      <span className={"ext-status-text " + st}>{STATUS_TEXT[st]}</span>
                      {!s.enabled && <span className="tag warn">已停用</span>}
                    </div>
                    <div className="mr-id" title={eff === "stdio" ? [s.command, ...s.args].join(" ") : s.url}>
                      {eff === "stdio" ? [s.command, ...s.args].join(" ") : s.url}
                    </div>
                    <div className="ext-item-sub">
                      {s.status === "ready" ? `${s.tools.length} 个工具` : "连接后列出工具"}
                      {inGroups ? ` · ${inGroups} 个群启用` : " · 还没有群启用"}
                    </div>
                    {(rowErr[s.id] || (st === "error" && s.error)) && <div className="ext-errline">{rowErr[s.id] || s.error}</div>}
                  </div>
                  <div className="ext-item-actions">
                    {s.status === "ready" ? (
                      <button className="btn small" disabled={!!busy[s.id]} onClick={() => void disconnect(s)}>{busy[s.id] === "disconnect" ? <Spin size={12} /> : null}断开</button>
                    ) : (
                      <button className="btn small" disabled={!!busy[s.id] || s.status === "connecting" || !s.enabled} title={s.enabled ? "" : "先启用这个服务器"} onClick={() => void connect(s)}>
                        {busy[s.id] === "connect" || s.status === "connecting" ? <><Spin size={12} /> 连接中</> : "连接"}
                      </button>
                    )}
                    <Switch checked={s.enabled} disabled={!!busy[s.id]} label={`启用 ${s.name}`} onChange={(v) => void toggle(s, v)} />
                    <button className="icon-btn" title="编辑" aria-label={`编辑 ${s.name}`} onClick={() => openEdit(s)}><Pencil size={15} /></button>
                    <button className="icon-btn" title="删除" aria-label={`删除 ${s.name}`} onClick={() => void remove(s)}><Trash2 size={15} /></button>
                  </div>
                </div>
                {isOpen && (
                  <div className="ext-item-detail">
                    {s.description && <div className="ext-item-desc" style={{ marginTop: 0 }}>{s.description}</div>}
                    {s.tools.length > 0 ? (
                      <div className="ext-tool-list" aria-label={`${s.name} 的工具`}>
                        {s.tools.map((t) => (
                          <div key={t.name} className="ext-tool">
                            <code>{t.name}</code>
                            {t.read_only && <span className="tag on" title="服务器声明这个工具只读">只读</span>}
                            <span className="muted">{t.description}</span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="muted small" style={{ marginTop: 6 }}>还没有工具信息 —— 点「连接」后会列出这个服务器提供的工具。</div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="sec">模板</div>
      <p className="muted small" style={{ margin: "-4px 0 10px", lineHeight: 1.7 }}>点一个模板只会把它填进表单,不会直接保存;你需要看过命令再点保存。</p>
      <div className="ext-tpl-grid">
        {templates.map((t) => {
          const ph = t.args.some(hasPlaceholder);
          return (
            <button key={t.name} className="ext-tpl" onClick={() => openAdd({ name: t.name, description: t.note, command: t.command, args: t.args })}>
              <b>{t.name}{ph && <span className="tag warn">要改路径</span>}</b>
              <span className="ext-tpl-cmd"><Highlight text={[t.command, ...t.args].join(" ")} /></span>
              <span className="ext-tpl-note">{t.note}</span>
            </button>
          );
        })}
        {templates.length === 0 && <div className="muted small">没有可用的模板。</div>}
      </div>

      <div className="sec">去哪儿找更多</div>
      <p className="muted small" style={{ margin: "-4px 0 10px", lineHeight: 1.7 }}>
        下面是第三方网站,内容和质量由它们负责,本程序没有核实过。看中一个服务器后,把它的配置 JSON 复制下来,用上面的「导入 JSON」添加,先看命令再决定要不要用。
      </p>
      <div className="ext-tpl-grid">
        {MARKETS.map((m) => (
          <a key={m.url} className="ext-tpl" href={m.url} target="_blank" rel="noreferrer">
            <b>{m.name} <ExternalLink size={12} aria-hidden /></b>
            <span className="ext-tpl-note">{m.note}</span>
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
      {importing && (
        <McpImportModal
          onClose={() => setImporting(false)}
          onDone={async (n) => { setImporting(false); setNote(n ? `已导入 ${n} 个服务器。它们还没有在任何群里启用,也没有运行。` : "没有导入新的服务器(同名的已跳过)。"); await load(); }}
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

// ---------------------------------------------------------------- 表单
let rowId = 1;
const toRows = (o: Record<string, string>): KvRow[] => Object.entries(o).map(([k, v]) => ({ id: rowId++, k, v: v === MASK ? "" : v, masked: v === MASK }));

function KvEditor({ rows, setRows, kPlaceholder, vPlaceholder, label }: { rows: KvRow[]; setRows: (r: KvRow[]) => void; kPlaceholder: string; vPlaceholder: string; label: string }) {
  const upd = (id: number, patch: Partial<KvRow>) => setRows(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  return (
    <div>
      {rows.map((r) => (
        <div key={r.id} className="ext-kv">
          <input value={r.k} readOnly={r.masked} onChange={(e) => upd(r.id, { k: e.target.value })} placeholder={kPlaceholder} aria-label={`${label}名`} spellCheck={false} title={r.masked ? "已保存的项不能改名;想改就删掉再新加一行" : ""} />
          <input
            className={r.masked && !r.v ? "masked" : ""}
            type={r.masked && !r.v ? "text" : "password"}
            value={r.v}
            onChange={(e) => upd(r.id, { v: e.target.value })}
            placeholder={r.masked ? "已设置(不显示)" : vPlaceholder}
            aria-label={`${label}值`}
            autoComplete="off"
            spellCheck={false}
          />
          <button type="button" className="icon-btn tiny" title="删除这一行" aria-label={`删除${label} ${r.k || ""}`} onClick={() => setRows(rows.filter((x) => x.id !== r.id))}><X size={14} /></button>
        </div>
      ))}
      <button type="button" className="btn small" onClick={() => setRows([...rows, { id: rowId++, k: "", v: "", masked: false }])}><Plus size={13} /> 添加{label}</button>
    </div>
  );
}

export function McpDialog({ server, init, onClose, onSaved }: { server: McpServer | null; init: FormInit; onClose: () => void; onSaved: (s: McpServer) => Promise<void> }) {
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
    if (!remote && phLines.length && !(await confirm(`参数里还有 /path/to/ 占位路径(第 ${phLines.join("、")} 行),这不是真实路径,服务器多半启动不了。仍然保存吗?`, { okText: "仍然保存", danger: false }))) return;
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
      title={server ? `编辑「${server.name}」` : "添加 MCP 服务器"}
      onClose={onClose}
      wide
      actions={
        <>
          {err && <span className="err ext-act-err" role="alert">{err}</span>}
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={!canSave} onClick={() => void save()}>{busy ? <><Spin /> 保存中…</> : "保存"}</button>
        </>
      }
    >
      <label className="field">
        <span>名称</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如:文件系统" autoFocus />
      </label>
      <label className="field">
        <span>说明(可选)</span>
        <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="它能做什么" />
      </label>
      <div className="field">
        <span>连接方式</span>
        <div className="ext-radio-row" role="radiogroup" aria-label="连接方式">
          <label className={"check" + (!remote ? " on" : "")}>
            <input type="radio" name="mcp-mode" checked={!remote} onChange={() => setRemote(false)} />
            <span>本地命令<small>在本机启动一个进程(stdio)</small></span>
          </label>
          <label className={"check" + (remote ? " on" : "")}>
            <input type="radio" name="mcp-mode" checked={remote} onChange={() => setRemote(true)} />
            <span>远程服务<small>连接一个 http / sse 地址,需要允许外呼</small></span>
          </label>
        </div>
      </div>

      {!remote ? (
        <>
          <label className="field">
            <span>启动命令</span>
            <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="如:npx / uvx / python" spellCheck={false} />
          </label>
          <label className="field">
            <span>参数(每行一个,含空格的参数不会被拆开)</span>
            <textarea className={"ext-mono-area" + (phLines.length ? " warn" : "")} rows={4} value={argsText} onChange={(e) => setArgsText(e.target.value)} placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/你的/目录"} spellCheck={false} />
            {phLines.length > 0 && <span className="ext-field-note warn">第 {phLines.join("、")} 行的 /path/to/… 是占位,需要替换成你的真实路径。</span>}
          </label>
          <div className="field">
            <span>环境变量(如需要 API Key)</span>
            <KvEditor rows={env} setRows={setEnv} kPlaceholder="变量名,如 GITHUB_TOKEN" vPlaceholder="值" label="环境变量" />
            {env.some((r) => r.masked) && <span className="ext-field-note">「已设置(不显示)」的值不会回显;不改就保持原样,输入新值会覆盖。</span>}
          </div>
        </>
      ) : (
        <>
          <label className="field">
            <span>服务地址(URL)</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/mcp" spellCheck={false} />
          </label>
          <label className="field">
            <span>传输方式</span>
            <select value={transport} onChange={(e) => setTransport(e.target.value)}>
              <option value="">自动(地址以 /sse 结尾按 sse,否则按 http)</option>
              <option value="http">http(Streamable HTTP)</option>
              <option value="sse">sse</option>
            </select>
          </label>
          <div className="field">
            <span>请求头(如 Authorization)</span>
            <KvEditor rows={headers} setRows={setHeaders} kPlaceholder="名称,如 Authorization" vPlaceholder="值" label="请求头" />
            {headers.some((r) => r.masked) && <span className="ext-field-note">「已设置(不显示)」的值不会回显;不改就保持原样,输入新值会覆盖。</span>}
          </div>
        </>
      )}
      <p className="ext-field-note" style={{ margin: "4px 0 0" }}>保存后不会立刻连接;在列表里点「连接」试一下,或在群里启用后第一次被用到时自动连接。</p>
    </Modal>
  );
}
