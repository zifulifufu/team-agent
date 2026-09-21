import { useCallback, useEffect, useState } from "react";
import { Code2, RefreshCw, Trash2 } from "lucide-react";
import { api, type PluginInfo } from "../api";
import { Modal, useConfirm } from "../ui";
import { Callout, GithubMark, SourceBadge, Spin } from "../components/ExtBits";
import { RepoDiscoverModal } from "../components/RepoDiscover";
import { useData } from "../data";
import "../styles/ext.css";

const SAMPLE = `PLUGIN = {"name": "问候", "description": "生成问候语", "version": "1.0"}

def register(registry):
    registry.register(
        "greet",                                   # 工具名(成员调用时用)
        "生成一句问候语",                            # 描述(写给模型看,说清楚什么时候该用)
        {                                          # 参数,JSON Schema
            "type": "object",
            "properties": {"name": {"type": "string", "description": "对方的名字"}},
        },
        lambda args: "你好," + str(args.get("name", "朋友")),   # 函数:收到参数字典,返回文字
    )`;

export default function PluginsPage() {
  const { groups } = useData();
  const confirm = useConfirm();
  const [plugins, setPlugins] = useState<PluginInfo[] | null>(null);
  const [dataDir, setDataDir] = useState("");
  const [err, setErr] = useState("");
  const [reloading, setReloading] = useState(false);
  const [note, setNote] = useState("");
  const [viewing, setViewing] = useState<PluginInfo | null>(null);
  const [discover, setDiscover] = useState(false);
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
    setNote("");
    try {
      const r = await api.reloadPlugins();
      setPlugins(r);
      setErr("");
      const bad = r.filter((p) => p.error).length;
      setNote(bad ? `已重新加载,${bad} 个插件加载失败(见下方红字)。` : `已重新加载,共 ${r.length} 个插件。`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setReloading(false);
    }
  };

  const remove = async (p: PluginInfo) => {
    const used = groups.filter((g) => g.ext?.plugins?.includes(p.id));
    const extra = used.length ? `它正在 ${used.length} 个群里启用(${used.map((g) => g.name).join("、")}),删除后这些群里的成员就不能再用它的工具了。` : "";
    if (!(await confirm(`删除插件「${p.name || p.id}」的文件 ${p.file}?${extra}`, { okText: "删除" }))) return;
    try {
      await api.delPlugin(p.id);
      await load();
    } catch (e) {
      setRowErr((r) => ({ ...r, [p.id]: (e as Error).message }));
    }
  };

  const dir = dataDir ? `${dataDir}/plugins/` : "数据目录/plugins/";
  return (
    <div className="sp">
      <div className="sp-head">
        <h2 className="sp-title">插件</h2>
        <div className="sp-head-actions">
          <button className="btn" onClick={reload} disabled={reloading}>{reloading ? <><Spin /> 加载中…</> : <><RefreshCw size={14} /> 重新加载</>}</button>
          <button className="btn" onClick={() => setDiscover(true)}><GithubMark size={14} /> 从 GitHub 发现插件</button>
        </div>
      </div>
      <p className="sp-desc">插件用 Python 给成员增加新的「工具」(查天气、调内部接口……)。它和 MCP 服务器是两回事:插件跑在本程序进程里,MCP 是另起的本机进程或远程服务。</p>

      <Callout tone="warn" title="插件不受沙箱保护">
        插件是 Python 代码,在本程序进程里运行,没有沙箱,能访问你的文件和网络。只安装你读过、信得过的。
      </Callout>

      <details className="ext-details" style={{ marginBottom: 6 }}>
        <summary>自己写一个插件</summary>
        <p className="muted small" style={{ margin: "6px 0 0", lineHeight: 1.7 }}>
          把一个 <code>.py</code> 文件放到 <code>{dir}</code>,然后点「重新加载」。文件里要有 <code>register(registry)</code> 函数,用 <code>registry.register(名称, 描述, 参数, 函数)</code> 登记工具;<code>PLUGIN</code> 元数据是可选的。
        </p>
        <pre className="ext-src ext-sample" tabIndex={0} aria-label="最小插件示例">{SAMPLE}</pre>
      </details>

      <div className="sec">已加载的插件{plugins && <span className="count-badge-plain">{plugins.length}</span>}</div>
      {note && <div className={note.includes("失败") ? "err" : "ok-text"} style={{ marginBottom: 8 }}>{note}</div>}
      {err && <div className="ext-errbox"><div className="err">读取插件失败:{err}</div><button className="btn small" onClick={() => void load()}>重试</button></div>}
      {!plugins && !err && <div className="empty"><Spin /> 加载中…</div>}
      {plugins && (
        <div className="card flush">
          {plugins.length === 0 && (
            <div className="empty" style={{ lineHeight: 1.8 }}>
              还没有插件。下一步:点「从 GitHub 发现插件」找一个(安装前会让你读完整源码),<br />
              或者按上面「自己写一个插件」的说明放一个 .py 文件,再点「重新加载」。
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
                      <div className="ext-errline">加载失败:{p.error}</div>
                    ) : (
                      <div className="ext-chips" aria-label="提供的工具">
                        {p.tools.length === 0 && <span className="muted small">没有登记任何工具</span>}
                        {p.tools.map((t) => <span key={t} className="ext-tool-chip">{t}</span>)}
                      </div>
                    )}
                    <div className="ext-item-sub">{usedIn.length ? `已在这些群里启用:${usedIn.join("、")}` : "还没有群启用它"}</div>
                    {rowErr[p.id] && <div className="ext-errline">{rowErr[p.id]}</div>}
                  </div>
                  <div className="ext-item-actions">
                    <button className="btn small" onClick={() => setViewing(p)}><Code2 size={13} /> 查看源码</button>
                    <button className="icon-btn" title="删除" aria-label={`删除插件 ${p.name || p.id}`} onClick={() => void remove(p)}><Trash2 size={15} /></button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <p className="muted small" style={{ marginTop: 12, lineHeight: 1.7 }}>
        插件要在群里启用后才会被成员调用:在群聊右侧面板的「扩展」里勾选它。
      </p>

      {viewing && <SourceModal plugin={viewing} onClose={() => setViewing(null)} />}
      {discover && <RepoDiscoverModal kind="plugin" onClose={() => setDiscover(false)} onInstalled={() => { void load(); }} />}
    </div>
  );
}

function SourceModal({ plugin, onClose }: { plugin: PluginInfo; onClose: () => void }) {
  const [src, setSrc] = useState<string | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    api.pluginSource(plugin.id).then((r) => alive && setSrc(r.content)).catch((e) => alive && setErr((e as Error).message));
    return () => { alive = false; };
  }, [plugin.id]);
  return (
    <Modal title={`源码:${plugin.file}`} onClose={onClose} wide actions={<button className="btn" onClick={onClose}>关闭</button>}>
      <div className="ext-xl">
        {src === null && !err && <div className="empty"><Spin /> 读取中…</div>}
        {err && <div className="err">{err}</div>}
        {src !== null && <pre className="ext-src" style={{ marginTop: 0 }} tabIndex={0} aria-label="插件源码(只读)">{src}</pre>}
      </div>
    </Modal>
  );
}
