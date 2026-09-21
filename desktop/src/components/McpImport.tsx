import { useState } from "react";
import { api, type McpImportPreview } from "../api";
import { Modal } from "../ui";
import { Spin } from "./ExtBits";

/** 导入别处的 MCP 配置 JSON(Claude Desktop / Cherry Studio / Cursor 等通用的 mcpServers 写法)。先预览,由你勾选后才添加;不会运行任何东西。 */
export default function McpImportModal({ onClose, onDone }: { onClose: () => void; onDone: (added: number) => void }) {
  const [text, setText] = useState("");
  const [prev, setPrev] = useState<McpImportPreview | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const parse = async (t = text) => {
    setBusy(true);
    setErr("");
    setPrev(null);
    try {
      const p = await api.mcpImportParse(t);
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
    if (f.size > 200_000) return setErr("文件太大(超过 200KB),不像是 MCP 配置");
    const t = await f.text();
    setText(t);
    await parse(t);
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
      title="导入 MCP 配置(JSON)"
      onClose={onClose}
      wide
      actions={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          {prev ? (
            <button className="btn primary" disabled={busy || sel.size === 0} onClick={() => void doImport()}>{busy ? "导入中…" : `导入所选(${sel.size})`}</button>
          ) : (
            <button className="btn primary" disabled={busy || !text.trim()} onClick={() => void parse()}>{busy ? <Spin size={12} /> : null} 解析</button>
          )}
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0, lineHeight: 1.7 }}>
        粘贴别的应用导出的配置,形如 <code>{'{"mcpServers": {"名字": {"command": "npx", "args": [...]}}}'}</code>。解析后你可以逐个看命令再勾选;导入只是保存配置,不会运行,也不会自动在群里启用。
      </p>
      <textarea value={text} onChange={(e) => { setText(e.target.value); setPrev(null); }} rows={prev ? 5 : 10} spellCheck={false} aria-label="MCP 配置 JSON" placeholder='{"mcpServers": {...}}' style={{ fontFamily: "ui-monospace, monospace", fontSize: 12.5 }} />
      <div style={{ marginTop: 6 }}>
        <label className="btn small">选择 .json 文件…<input type="file" accept=".json,application/json" hidden onChange={(e) => void readFile(e.target.files?.[0])} /></label>
      </div>
      {err && <div className="err" role="alert" style={{ marginTop: 8 }}>{err}</div>}
      {prev && (
        <div style={{ marginTop: 12 }}>
          {prev.warnings.length > 0 && <ul className="kn-obs-warn">{prev.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
          <div className="card flush">
            {prev.servers.map((s) => (
              <label key={s.name} className="setting-row pad" style={{ alignItems: "flex-start", cursor: s.exists ? "default" : "pointer" }}>
                <input type="checkbox" style={{ width: "auto", marginTop: 4 }} checked={sel.has(s.name)} disabled={s.exists} onChange={() => toggle(s.name)} aria-label={`导入 ${s.name}`} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="sr-title">
                    {s.name} <span className="tag">{s.command ? "本地进程" : "远程"}</span>
                    {s.exists && <span className="tag warn">已存在,将跳过</span>}
                    {!s.enabled && <span className="tag">已停用</span>}
                  </div>
                  <div className="sr-desc" style={{ fontFamily: "ui-monospace, monospace", overflowWrap: "anywhere" }}>{s.command ? [s.command, ...s.args].join(" ") : s.url}</div>
                  {Object.keys(s.env).length + Object.keys(s.headers).length > 0 && (
                    <div className="sr-desc">含 {Object.keys(s.env).length} 个环境变量、{Object.keys(s.headers).length} 个请求头(值不在此显示,导入时原样保存)</div>
                  )}
                </div>
              </label>
            ))}
          </div>
          {prev.servers.some((s) => s.command) && <p className="muted small" style={{ lineHeight: 1.7 }}>本地进程类的服务器,在被用到时会以你的账号权限在电脑上运行上面的命令,请确认来源可信。</p>}
        </div>
      )}
    </Modal>
  );
}
