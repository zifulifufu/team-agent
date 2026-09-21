import { useState } from "react";
import { api, type McpImportPreview } from "../api";
import { Modal } from "../ui";
import { Spin } from "./ExtBits";
import { useI18n } from "../i18n";

/** Import an MCP configuration JSON from elsewhere (the common mcpServers shape used by Claude Desktop, Cherry Studio, Cursor and others). It is previewed first and only what you tick is added; nothing is ever run. */
export default function McpImportModal({ onClose, onDone }: { onClose: () => void; onDone: (added: number) => void }) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [prev, setPrev] = useState<McpImportPreview | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const parse = async (raw = text) => {
    setBusy(true);
    setErr("");
    setPrev(null);
    try {
      const p = await api.mcpImportParse(raw);
      setPrev(p);
      setSel(new Set(p.servers.filter((s) => !s.exists).map((s) => s.name)));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const readFile = async (f: File | undefined) => {
    if (!f) return;
    if (f.size > 200_000) return setErr(t("The file is too large (over 200KB) to be an MCP configuration"));
    const body = await f.text();
    setText(body);
    await parse(body);
  };
  const doImport = async () => {
    setBusy(true);
    setErr("");
    try {
      const r = await api.mcpImport(text, [...sel]);
      onDone(r.added.length);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  const toggle = (n: string) => setSel((s) => { const x = new Set(s); if (x.has(n)) x.delete(n); else x.add(n); return x; });

  return (
    <Modal
      title={t("Import an MCP configuration (JSON)")}
      onClose={onClose}
      wide
      actions={
        <>
          <button className="btn" onClick={onClose}>{t("Cancel")}</button>
          {prev ? (
            <button className="btn primary" disabled={busy || sel.size === 0} onClick={() => void doImport()}>{busy ? t("Importing…") : t("Import the selected ones ({n})", { n: sel.size })}</button>
          ) : (
            <button className="btn primary" disabled={busy || !text.trim()} onClick={() => void parse()}>{busy ? <Spin size={12} /> : null} {t("Parse")}</button>
          )}
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0, lineHeight: 1.7 }}>
        {t("Paste a configuration exported by another app, shaped like")} <code>{'{"mcpServers": {"name": {"command": "npx", "args": [...]}}}'}</code>{t(". After parsing you can look at each command and tick the ones you want; importing only saves the configuration — nothing runs, and nothing is enabled in a group automatically.")}
      </p>
      <textarea value={text} onChange={(e) => { setText(e.target.value); setPrev(null); }} rows={prev ? 5 : 10} spellCheck={false} aria-label={t("MCP configuration JSON")} placeholder='{"mcpServers": {...}}' style={{ fontFamily: "ui-monospace, monospace", fontSize: 12.5 }} />
      <div style={{ marginTop: 6 }}>
        <label className="btn small">{t("Choose a .json file…")}<input type="file" accept=".json,application/json" hidden onChange={(e) => void readFile(e.target.files?.[0])} /></label>
      </div>
      {err && <div className="err" role="alert" style={{ marginTop: 8 }}>{err}</div>}
      {prev && (
        <div style={{ marginTop: 12 }}>
          {prev.warnings.length > 0 && <ul className="kn-obs-warn">{prev.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
          <div className="card flush">
            {prev.servers.map((s) => (
              <label key={s.name} className="setting-row pad" style={{ alignItems: "flex-start", cursor: s.exists ? "default" : "pointer" }}>
                <input type="checkbox" style={{ width: "auto", marginTop: 4 }} checked={sel.has(s.name)} disabled={s.exists} onChange={() => toggle(s.name)} aria-label={t("Import {name}", { name: s.name })} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="sr-title">
                    {s.name} <span className="tag">{s.command ? t("Local process") : t("Remote")}</span>
                    {s.exists && <span className="tag warn">{t("Already exists, will be skipped")}</span>}
                    {!s.enabled && <span className="tag">{t("Disabled")}</span>}
                  </div>
                  <div className="sr-desc" style={{ fontFamily: "ui-monospace, monospace", overflowWrap: "anywhere" }}>{s.command ? [s.command, ...s.args].join(" ") : s.url}</div>
                  {Object.keys(s.env).length + Object.keys(s.headers).length > 0 && (
                    <div className="sr-desc">{t("{env} environment variables and {headers} headers (the values are not shown here; they are saved exactly as given)", { env: Object.keys(s.env).length, headers: Object.keys(s.headers).length })}</div>
                  )}
                </div>
              </label>
            ))}
          </div>
          {prev.servers.some((s) => s.command) && <p className="muted small" style={{ lineHeight: 1.7 }}>{t("A local-process server runs the command above on this computer with your account's permissions when it is used, so make sure the source is trustworthy.")}</p>}
        </div>
      )}
    </Modal>
  );
}
