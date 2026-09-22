import { useCallback, useEffect, useState } from "react";
import { AppWindow, Code2, RefreshCw, Trash2 } from "lucide-react";
import { api, type PluginInfo } from "../api";
import { Modal, useConfirm } from "../ui";
import { Callout, GithubMark, SourceBadge, Spin } from "../components/ExtBits";
import { RepoDiscoverModal } from "../components/RepoDiscover";
import ImportFromApps from "../components/ImportFromApps";
import { useData } from "../data";
import { useI18n } from "../i18n";
import "../styles/ext.css";

const SAMPLE = `PLUGIN = {"name": "greeting", "description": "Generate a greeting", "version": "1.0"}

def register(registry):
    registry.register(
        "greet",                                   # tool name (what members call)
        "Write a one-line greeting",               # description (read by the model; say when to use it)
        {                                          # parameters, JSON Schema
            "type": "object",
            "properties": {"name": {"type": "string", "description": "the other person's name"}},
        },
        lambda args: "Hello, " + str(args.get("name", "friend")),   # takes the arguments, returns text
    )`;

export default function PluginsPage() {
  const { t } = useI18n();
  const { groups } = useData();
  const confirm = useConfirm();
  const [plugins, setPlugins] = useState<PluginInfo[] | null>(null);
  const [dataDir, setDataDir] = useState("");
  const [err, setErr] = useState("");
  const [reloading, setReloading] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [viewing, setViewing] = useState<PluginInfo | null>(null);
  const [discover, setDiscover] = useState(false);
  const [fromApps, setFromApps] = useState(false);
  const [rowErr, setRowErr] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      setPlugins(await api.plugins());
      setErr("");
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
    api.system().then((s) => setDataDir(s.data_dir)).catch(() => undefined);
  }, [load]);

  const reload = async () => {
    setReloading(true);
    setNote(null);
    try {
      const r = await api.reloadPlugins();
      setPlugins(r);
      setErr("");
      const bad = r.filter((p) => p.error).length;
      setNote(bad
        ? { ok: false, text: t("Reloaded, but {n} plugins failed to load (see the red text below).", { n: bad }) }
        : { ok: true, text: t("Reloaded, {n} plugins in total.", { n: r.length }) });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setReloading(false);
    }
  };

  const remove = async (p: PluginInfo) => {
    const used = groups.filter((g) => g.ext?.plugins?.includes(p.id));
    const extra = used.length ? t("It is enabled in {n} groups ({names}); deleting it stops the members of those groups from using its tools.", { n: used.length, names: used.map((g) => g.name).join(", ") }) : "";
    if (!(await confirm(t('Delete the file {file} of the plugin "{name}"? {extra}', { file: p.file, name: p.name || p.id, extra }), { okText: t("Delete") }))) return;
    try {
      await api.delPlugin(p.id);
      await load();
    } catch (e) {
      setRowErr((r) => ({ ...r, [p.id]: (e as Error).message }));
    }
  };

  const dir = dataDir ? `${dataDir}/plugins/` : t("data directory/plugins/");
  return (
    <div className="sp">
      <div className="sp-head">
        <h2 className="sp-title">{t("Plugins")}</h2>
        <div className="sp-head-actions">
          <button className="btn" onClick={reload} disabled={reloading}>{reloading ? <><Spin /> {t("Reloading…")}</> : <><RefreshCw size={14} /> {t("Reload")}</>}</button>
          <button className="btn" onClick={() => setFromApps(true)}><AppWindow size={14} /> {t("From another app")}</button>
          <button className="btn" onClick={() => setDiscover(true)}><GithubMark size={14} /> {t("Find a plugin on GitHub")}</button>
        </div>
      </div>
      <p className="sp-desc">{t("A plugin adds new tools to your members in Python (check the weather, call an internal API…). It is not the same thing as an MCP server: a plugin runs inside this app's own process, while MCP is a separate local process or a remote service.")}</p>

      <Callout tone="info" title={t("Plugins from other AI apps cannot be installed here — but MCP servers and skills can")}>
        {t("A plugin here is a Python file that adds tools, so no other application produces one: a ChatGPT GPT is a prompt with actions behind a login, and a Cursor or VS Code extension is TypeScript against a different host. What does travel is the MCP standard (Claude Desktop, Claude Code, Codex, Cursor, Windsurf, Cline, Roo, Continue, Zed…) and Claude-style skills. Both can be read straight out of those applications' own configuration.")}{" "}
        <button className="link" onClick={() => setFromApps(true)}>{t("Import from another app")}</button>
      </Callout>

      <Callout tone="warn" title={t("A plugin is not sandboxed")}>
        {t("A plugin is Python code running inside this app's process, with no sandbox, and it can reach your files and your network. Install only the ones you have read and trust.")}
      </Callout>

      <details className="ext-details" style={{ marginBottom: 6 }}>
        <summary>{t("Write a plugin yourself")}</summary>
        <p className="muted small" style={{ margin: "6px 0 0", lineHeight: 1.7 }}>
          {t("Put a")} <code>.py</code> {t("file in")} <code>{dir}</code>{t(", then click Reload. The file needs a")} <code>register(registry)</code> {t("function that registers its tools with")} <code>registry.register(name, description, parameters, function)</code>{t("; the PLUGIN metadata is optional.")}
        </p>
        <pre className="ext-src ext-sample" tabIndex={0} aria-label={t("Minimal plugin example")}>{SAMPLE}</pre>
      </details>

      <div className="sec">{t("Loaded plugins")}{plugins && <span className="count-badge-plain">{plugins.length}</span>}</div>
      {note && <div className={note.ok ? "ok-text" : "err"} style={{ marginBottom: 8 }}>{note.text}</div>}
      {err && <div className="ext-errbox"><div className="err">{t("Could not read the plugins:")} {err}</div><button className="btn small" onClick={() => void load()}>{t("Retry")}</button></div>}
      {!plugins && !err && <div className="empty"><Spin /> {t("Loading…")}</div>}
      {plugins && (
        <div className="card flush">
          {plugins.length === 0 && (
            <div className="empty" style={{ lineHeight: 1.8 }}>
              {t("No plugins yet. Next step: find one with Find a plugin on GitHub (you get to read the whole source before installing),")}<br />
              {t("or drop a .py file in as described under Write a plugin yourself, then click Reload.")}
            </div>
          )}
          {plugins.map((p) => {
            const usedIn = groups.filter((g) => g.ext?.plugins?.includes(p.id)).map((g) => g.name);
            return (
              <div key={p.id} className={"ext-item" + (p.error ? " err-state" : "")}>
                <div className="model-row">
                  <div className="mr-main">
                    <div className="mr-name">
                      {p.name || p.id}
                      <code>{p.id}</code>
                      {p.version && <span className="tag">v{p.version}</span>}
                      {p.source && <SourceBadge repo={p.source.repo} path={p.source.path} />}
                    </div>
                    {p.description && <div className="ext-item-desc">{p.description}</div>}
                    {p.error ? (
                      <div className="ext-errline">{t("Could not load it:")} {p.error}</div>
                    ) : (
                      <div className="ext-chips" aria-label={t("Tools it provides")}>
                        {p.tools.length === 0 && <span className="muted small">{t("No tools registered")}</span>}
                        {p.tools.map((t) => <span key={t} className="ext-tool-chip">{t}</span>)}
                      </div>
                    )}
                    <div className="ext-item-sub">{usedIn.length ? t("Enabled in these groups: {names}", { names: usedIn.join(", ") }) : t("No group has it enabled yet")}</div>
                    {rowErr[p.id] && <div className="ext-errline">{rowErr[p.id]}</div>}
                  </div>
                  <div className="ext-item-actions">
                    <button className="btn small" onClick={() => setViewing(p)}><Code2 size={13} /> {t("View the source")}</button>
                    <button className="icon-btn" title={t("Delete")} aria-label={t("Delete the plugin {name}", { name: p.name || p.id })} onClick={() => void remove(p)}><Trash2 size={15} /></button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <p className="muted small" style={{ marginTop: 12, lineHeight: 1.7 }}>
        {t("A plugin is only called by members once it is enabled in a group: tick it under Extensions, in the panel on the right of a chat.")}
      </p>

      {viewing && <SourceModal plugin={viewing} onClose={() => setViewing(null)} />}
      {fromApps && (
        <ImportFromApps kind="mcp" onClose={() => setFromApps(false)}
                        onDone={async (added) => {
                          setFromApps(false);
                          await load();
                          setNote({ ok: true, text: t("Imported {n} item(s) from another app. They are disabled until you enable them.", { n: added }) });
                        }} />
      )}
      {discover && <RepoDiscoverModal kind="plugin" onClose={() => setDiscover(false)} onInstalled={() => { void load(); }} />}
    </div>
  );
}

function SourceModal({ plugin, onClose }: { plugin: PluginInfo; onClose: () => void }) {
  const { t } = useI18n();
  const [src, setSrc] = useState<string | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    api.pluginSource(plugin.id).then((r) => alive && setSrc(r.content)).catch((e) => alive && setErr((e as Error).message));
    return () => { alive = false; };
  }, [plugin.id]);
  return (
    <Modal title={t("Source: {file}", { file: plugin.file })} onClose={onClose} wide actions={<button className="btn" onClick={onClose}>{t("Close")}</button>}>
      <div className="ext-xl">
        {src === null && !err && <div className="empty"><Spin /> {t("Reading…")}</div>}
        {err && <div className="err">{err}</div>}
        {src !== null && <pre className="ext-src" style={{ marginTop: 0 }} tabIndex={0} aria-label={t("Plugin source (read-only)")}>{src}</pre>}
      </div>
    </Modal>
  );
}
