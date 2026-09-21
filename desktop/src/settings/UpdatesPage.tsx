import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { BookOpen, Boxes, Download, HardDrive, Package, Plug, Puzzle, RefreshCw, Sparkles, type LucideIcon } from "lucide-react";
import { api, relTime, type Settings, type UpdateItem, type UpdatesInfo } from "../api";
import { useData } from "../data";
import { Switch, useConfirm } from "../ui";
import { agoIso, Callout, fmtBytes, isHttps, SourceBadge, Spin } from "../components/ExtBits";
import PluginInstallModal from "../components/PluginInstall";
import type { PageProps } from "./SettingsModal";
import "../styles/ext.css";

const KIND_META: Record<UpdateItem["kind"], { icon: LucideIcon; label: string }> = {
  app: { icon: Package, label: "程序本体" },
  catalog: { icon: BookOpen, label: "模型目录" },
  skill: { icon: Sparkles, label: "技能" },
  plugin: { icon: Puzzle, label: "插件" },
  model: { icon: Boxes, label: "新模型" },
  localmodel: { icon: HardDrive, label: "本地模型" },
  localcatalog: { icon: BookOpen, label: "本地模型目录" },
};

const s = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));

export default function UpdatesPage({ onTab }: PageProps) {
  const { settings, reload, reloadUpdates } = useData();
  const [info, setInfo] = useState<UpdatesInfo | null>(null);
  const [err, setErr] = useState("");
  const [checking, setChecking] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const poll = useRef<number>();

  const refresh = useCallback(async () => {
    try {
      const u = await api.updates();
      setInfo(u);
      setErr("");
      void reloadUpdates();
      return u;
    } catch (e) {
      setErr((e as Error).message);
      return null;
    }
  }, [reloadUpdates]);
  useEffect(() => { void refresh(); }, [refresh]);

  // 后台正在检查时(定时检查或别处触发),轮询直到结束
  useEffect(() => {
    window.clearTimeout(poll.current);
    if (info?.checking && !checking) poll.current = window.setTimeout(() => void refresh(), 2000);
    return () => window.clearTimeout(poll.current);
  }, [info, checking, refresh]);

  const check = async () => {
    setChecking(true);
    setMsg(null);
    try {
      const r = await api.checkUpdates();
      const errs = Array.isArray(r.errors) ? (r.errors as unknown[]) : [];
      const u = await refresh();
      if (r.busy) setMsg({ ok: false, text: "上一次检查还没结束,请稍等。" });
      else if (errs.length === 0) setMsg({ ok: true, text: u && u.items.length ? `检查完成,有 ${u.items.length} 条待处理的提醒。` : "检查完成,没有发现新的更新。" });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setChecking(false);
    }
  };

  const last = info?.last_check ?? null;
  const errors = (last?.errors as string[] | undefined) ?? [];
  const appRes = (last?.app ?? null) as Record<string, unknown> | null;
  const busyNow = checking || !!info?.checking;

  return (
    <div className="sp">
      <div className="sp-head">
        <h2 className="sp-title">更新与发现</h2>
        <div className="sp-head-actions">
          <button className="btn primary" onClick={check} disabled={busyNow}>
            {busyNow ? <><Spin /> 检查中…</> : <><RefreshCw size={14} /> 立即检查</>}
          </button>
        </div>
      </div>
      <p className="sp-desc">
        从 GitHub 检查程序本体、模型目录、已安装技能和插件有没有新版本,也可以去搜索新的技能、插件和 MCP 服务器。检查需要联网(「路由与回退」里允许外呼)。
      </p>

      {err && <div className="ext-errbox"><div className="err">读取失败:{err}</div><button className="btn small" onClick={() => void refresh()}>重试</button></div>}
      {msg && <div className={msg.ok ? "ok-text" : "err"} style={{ marginBottom: 8 }}>{msg.text}</div>}

      {info && (
        <>
          {!info.configured && (
            <Callout title="还没有填写程序仓库">
              检查程序本体的新版本需要先在下面的「更新设置」里填写程序所在的 GitHub 仓库(owner/repo)。技能、插件、新模型的检查不受影响。
            </Callout>
          )}
          <div className="ext-check-bar muted small" style={{ marginBottom: 6 }}>
            <span>上次检查:{last?.at ? <span title={new Date(last.at * 1000).toLocaleString()}>{relTime(last.at)}</span> : "还没有检查过"}</span>
            {last && errors.length === 0 && appRes && !!appRes.configured && appRes.available === false && <span>程序已是最新版本{appRes.current ? `(${s(appRes.current)})` : ""}</span>}
            {last && errors.length === 0 && appRes && typeof appRes.note === "string" && <span>{appRes.note}</span>}
          </div>
          {errors.length > 0 && (
            <div className="ext-errbox" role="alert">
              <div className="err">
                上次检查遇到问题:
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
              </div>
            </div>
          )}

          <div className="sec">待处理的提醒{info.items.length > 0 && <span className="count-badge-plain">{info.items.length}</span>}</div>
          <div className="card flush">
            {info.items.length === 0 && <div className="empty">没有待处理的更新</div>}
            {info.items.map((it) => (
              <UpdateRow key={it.id} item={it} onTab={onTab} onDone={async (text) => { if (text) setMsg({ ok: true, text }); await refresh(); if (text) await reload(); }} />
            ))}
          </div>

          <div className="sec">更新设置</div>
          {settings && <SettingsCard settings={settings} onSaved={async () => { await reload(); await refresh(); }} />}

          <div className="sec">已记录来源</div>
          <p className="muted small" style={{ margin: "-4px 0 8px", lineHeight: 1.7 }}>从 GitHub 安装的技能和插件会记下来源,检查更新时就是拿这里的文件和 GitHub 上的比对。</p>
          <div className="card flush">
            {info.sources.length === 0 && <div className="empty">还没有从 GitHub 安装过技能或插件</div>}
            {info.sources.map((r) => (
              <div key={r.kind + r.name} className="ext-src-row">
                <span className="tag">{r.kind === "skill" ? "技能" : r.kind === "plugin" ? "插件" : r.kind}</span>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 500 }}>{r.name}</div>
                  <div className="muted small mono">{r.path}{r.ref ? ` @ ${r.ref}` : ""}</div>
                </div>
                <div style={{ textAlign: "right" }}>
                  <SourceBadge repo={r.repo} path={r.path} />
                  <div className="muted small" style={{ marginTop: 3 }}>{r.installed_at ? `安装于 ${new Date(r.installed_at * 1000).toLocaleDateString()}` : ""}</div>
                </div>
              </div>
            ))}
          </div>

          <div className="sec">发现新扩展</div>
          <div className="ext-cards3">
            <button className="ext-entry" onClick={() => onTab("skills")}>
              <b><Sparkles size={15} /> 技能</b>
              <span>纯文本提示词,不执行代码。从 GitHub 搜索、预览后安装。</span>
            </button>
            <button className="ext-entry" onClick={() => onTab("plugins")}>
              <b><Puzzle size={15} /> 插件</b>
              <span>Python 代码,会在本机运行。安装前必须先读完整源码。</span>
            </button>
            <button className="ext-entry" onClick={() => onTab("mcp")}>
              <b><Plug size={15} /> MCP 服务器</b>
              <span>不会自动安装,只帮你找到仓库、预填配置表单。</span>
            </button>
          </div>

          <div className="ext-foot">
            当前模型目录版本 {info.catalog.version}({info.catalog.source === "override" ? "已更新" : "内置"})。
            <br />
            目录是某个日期的快照,型号和强项标签是根据公开信息和名称整理、推断出来的,不是评测成绩。想看服务商此刻实际提供哪些模型,请在「模型服务 → 添加模型」里刷新实时清单。
          </div>
        </>
      )}
      {!info && !err && <div className="empty"><Spin /> 加载中…</div>}
    </div>
  );
}

// ------------------------------------------------------------------ 单条提醒
function UpdateRow({ item, onTab, onDone }: { item: UpdateItem; onTab: PageProps["onTab"]; onDone: (okText?: string) => Promise<void> }) {
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [reinstall, setReinstall] = useState(false);
  const d = item.detail;
  const meta = KIND_META[item.kind] ?? KIND_META.app;

  const run = async (fn: () => Promise<string | void>) => {
    setBusy(true);
    setErr("");
    try {
      const text = await fn();
      await onDone(text || undefined);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  let body: ReactNode = null;
  let actions: ReactNode = null;

  if (item.kind === "app") {
    const assets = Array.isArray(d.assets) ? (d.assets as { name?: unknown; size?: unknown; url?: unknown }[]) : [];
    body = (
      <>
        <div>当前版本 {s(d.current) || "?"} → 最新版本 <b>{s(d.latest)}</b>{d.published_at ? <span className="muted"> · 发布于 {agoIso(s(d.published_at)) || s(d.published_at)}</span> : null}</div>
        <div><b>程序本体不会自动替换,请下载安装包手动更新。</b></div>
        {s(d.notes) && (
          <details className="ext-details" style={{ marginTop: 6 }}>
            <summary>更新说明</summary>
            <div className="ext-notes">{s(d.notes)}</div>
          </details>
        )}
        {assets.length > 0 && (
          <div className="ext-assets" aria-label="安装包">
            {assets.map((a, i) => (
              <div key={i}>
                {isHttps(a.url) ? <a className="link" href={a.url} target="_blank" rel="noreferrer">{s(a.name) || a.url}</a> : <span>{s(a.name)}</span>}
                {typeof a.size === "number" && a.size > 0 && <span className="muted"> · {fmtBytes(a.size)}</span>}
              </div>
            ))}
          </div>
        )}
      </>
    );
    actions = isHttps(d.url) ? <a className="btn small" href={d.url} target="_blank" rel="noreferrer"><Download size={13} /> 打开发布页</a> : <span className="muted small">没有可用的发布页链接</span>;
  } else if (item.kind === "catalog") {
    body = (
      <>
        <div>当前 {s(d.current)} → 最新 {s(d.latest)}{typeof d.new_models === "number" ? `,新增 ${d.new_models} 个型号` : ""}</div>
        <div className="muted">只更新模型清单和强项标签,不会改动你已添加的模型和路由。</div>
      </>
    );
    actions = (
      <button className="btn small primary" disabled={busy} onClick={() => void run(async () => {
        const r = await api.applyCatalog();
        return r.applied ? `已应用新的模型目录(${s(r.latest)})。` : "目录没有变化。";
      })}>{busy ? <><Spin size={12} /> 应用中</> : "应用新目录"}</button>
    );
  } else if (item.kind === "skill") {
    const name = s(d.name) || item.ref;
    body = <div>GitHub 上的技能文件有变化。更新会用最新内容覆盖本地技能,你在本地做过的修改会丢失。{s(d.repo) && <> 来源:<SourceBadge repo={s(d.repo)} path={s(d.path)} /></>}</div>;
    actions = (
      <button className="btn small primary" disabled={busy} onClick={async () => {
        if (!(await confirm(`用 GitHub 上的最新内容更新技能「${name}」?这会覆盖本地内容,你在本地做过的修改会丢失。`, { okText: "更新并覆盖" }))) return;
        void run(async () => { await api.updateSkill(name); return `技能「${name}」已更新。`; });
      }}>{busy ? <><Spin size={12} /> 更新中</> : "更新"}</button>
    );
  } else if (item.kind === "plugin") {
    const repo = s(d.repo), path = s(d.path);
    body = (
      <>
        <div>GitHub 上的插件文件有变化。{repo && <> 来源:<SourceBadge repo={repo} path={path} /></>}</div>
        <div className="muted">插件是会执行的代码,不能直接更新:需要你先读一遍新源码、确认后才会重新安装。</div>
      </>
    );
    actions = <button className="btn small primary" disabled={!repo || !path} onClick={() => setReinstall(true)}>查看并重新安装</button>;
  } else if (item.kind === "localmodel") {
    const src = s(d.source);
    const tag = s(d.tag);
    body = src === "ollama-release" ? (
      <div>本机 Ollama {s(d.local) || "?"},GitHub 上最新 {s(d.latest)}。新模型常常需要较新的 Ollama。<b>程序不会自动升级 Ollama</b>,请自己下载安装。</div>
    ) : (
      <>
        <div>{s(d.desc) || "发现了新的开源模型"}{d.size_gb ? `,约 ${d.size_gb} GB` : ""}{s(d.license) ? ` · ${s(d.license)}` : ""}</div>
        {tag ? <div className="muted">已用 Ollama 注册表确认这个型号存在。「加入推荐」只是把它加进本地模型页的列表,不会下载。</div>
              : <div className="muted">这条来自 {src === "hf" ? "Hugging Face" : "GitHub"},只是提醒:不一定有 Ollama 版本,需要自己确认许可证和硬件要求。</div>}
      </>
    );
    actions = (
      <>
        {tag && (
          <button className="btn small primary" disabled={busy} onClick={() => void run(async () => { await api.localAdd(tag); return `已把 ${tag} 加入本地模型推荐列表。`; })}>加入推荐</button>
        )}
        {isHttps(d.url) && <a className="btn small" href={d.url} target="_blank" rel="noreferrer">打开页面</a>}
        <button className="btn small" onClick={() => onTab("local")}>去本地模型页</button>
      </>
    );
  } else if (item.kind === "localcatalog") {
    body = <div>当前 {s(d.current)} → 最新 {s(d.latest)}。目录只是型号清单和大小,不含代码。</div>;
    actions = (
      <button className="btn small primary" disabled={busy} onClick={() => void run(async () => {
        const r = await api.applyLocalCatalog();
        return r.applied ? `已应用新的本地模型目录(${s(r.latest)})。` : "目录没有变化。";
      })}>{busy ? <><Spin size={12} /> 应用中</> : "应用新目录"}</button>
    );
  } else if (item.kind === "model") {
    const ids = Array.isArray(d.ids) ? (d.ids as unknown[]).map(s) : [];
    body = (
      <>
        <div>服务商实时清单里出现了你还没看过的型号。</div>
        {ids.length > 0 && (
          <div className="ext-chips">
            {ids.slice(0, 8).map((id) => <span key={id} className="ext-tool-chip">{id}</span>)}
            {ids.length > 8 && <span className="muted small">…共 {ids.length} 个</span>}
          </div>
        )}
      </>
    );
    actions = <button className="btn small primary" onClick={() => onTab("providers")}>去挑选</button>;
  }

  return (
    <div className="ext-upd">
      <div className="ext-upd-ico"><meta.icon size={17} /></div>
      <div className="ext-upd-main">
        <div className="ext-upd-title">{item.title}<span className="tag">{meta.label}</span></div>
        <div className="ext-upd-body">{body}</div>
        {err && <div className="ext-errline">{err}</div>}
        <div className="ext-upd-actions">
          {actions}
          <button className="btn small ghost" disabled={busy} onClick={() => void run(async () => { await api.dismissUpdate(item.id); })}>忽略</button>
        </div>
      </div>
      {reinstall && (
        <PluginInstallModal
          repo={s(d.repo)}
          path={s(d.path)}
          gitRef={s(d.ref)}
          overwrite
          onClose={() => setReinstall(false)}
          onInstalled={() => { setReinstall(false); void onDone(`插件「${item.ref}」已重新安装。`); }}
        />
      )}
    </div>
  );
}

// ------------------------------------------------------------------ 更新设置
const INTERVALS = [1, 6, 12, 24, 72];

function SettingsCard({ settings, onSaved }: { settings: Settings; onSaved: () => Promise<void> }) {
  const [repo, setRepo] = useState(settings.app_repo);
  const [catalogUrl, setCatalogUrl] = useState(settings.catalog_url);
  const [auto, setAuto] = useState(settings.auto_check_updates);
  const [hours, setHours] = useState(settings.update_interval_hours);
  const [autoSkills, setAutoSkills] = useState(settings.auto_update_skills);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");

  const intervals = INTERVALS.includes(hours) ? INTERVALS : [...INTERVALS, hours].sort((a, b) => a - b);
  const dirty =
    repo.trim() !== settings.app_repo || catalogUrl.trim() !== settings.catalog_url || auto !== settings.auto_check_updates ||
    hours !== settings.update_interval_hours || autoSkills !== settings.auto_update_skills || token.trim() !== "";

  const put = async (patch: Partial<Settings>, done: string) => {
    setBusy(true);
    setErr("");
    setOk("");
    try {
      await api.putSettings(patch);
      await onSaved();
      setOk(done);
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    const r = repo.trim();
    const u = catalogUrl.trim();
    if (r && !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(r)) return setErr("程序仓库格式应为 owner/repo,例如 me/team-agent");
    if (u && !u.startsWith("https://")) return setErr("模型目录地址必须以 https:// 开头");
    const patch: Partial<Settings> = {
      app_repo: r, catalog_url: u, auto_check_updates: auto, update_interval_hours: hours, auto_update_skills: autoSkills,
    };
    if (token.trim()) patch.github_token = token.trim();   // 只有输入了新值才发送
    if (await put(patch, "已保存。")) setToken("");
  };

  return (
    <div className="card flush">
      <div className="ext-form-row">
        <label className="sr-title" htmlFor="upd-repo">程序仓库</label>
        <input id="upd-repo" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="owner/repo,例如 me/team-agent" spellCheck={false} />
        <div className="sr-desc">该仓库 Releases 里最新的版本号会和当前版本比较。不填就不检查程序本体。</div>
      </div>
      <div className="ext-form-row">
        <label className="sr-title" htmlFor="upd-cat">模型目录地址(可选)</label>
        <input id="upd-cat" value={catalogUrl} onChange={(e) => setCatalogUrl(e.target.value)} placeholder="https://…/catalog.json" spellCheck={false} />
        <div className="sr-desc">必须是 https。留空则使用程序仓库里的 backend/app/data/catalog.json。</div>
      </div>
      <div className="ext-form-row">
        <label className="sr-title" htmlFor="upd-token">GitHub Token(可选)</label>
        <div className="ext-inline-input">
          <input id="upd-token" type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" spellCheck={false}
            placeholder={settings.github_token_set ? "已设置(不显示)" : "留空即可;ghp_…"} />
          {settings.github_token_set && (
            <button className="btn" disabled={busy} onClick={() => void put({ github_token: "" }, "已清除 GitHub Token。")}>清除</button>
          )}
        </div>
        <div className="sr-desc">不需要任何权限,只用来提高 GitHub 接口的频率上限。它以明文存在本地数据库里,备份导出默认不含。</div>
      </div>
      <div className="setting-row pad">
        <div>
          <div className="sr-title">自动检查更新</div>
          <div className="sr-desc">开着时,程序会在后台按间隔去 GitHub 检查(需要允许外呼)。</div>
        </div>
        <div className="row">
          <select className="ext-select" value={hours} onChange={(e) => setHours(Number(e.target.value))} disabled={!auto} aria-label="检查间隔">
            {intervals.map((h) => <option key={h} value={h}>每 {h} 小时</option>)}
          </select>
          <Switch checked={auto} onChange={setAuto} label="自动检查更新" />
        </div>
      </div>
      <div className="setting-row pad">
        <div>
          <div className="sr-title">自动更新技能</div>
          <div className="sr-desc">开着时,已从 GitHub 安装的技能有新版本会自动覆盖本地内容。只更新文本技能;插件、MCP、程序本体永远不会自动安装。</div>
        </div>
        <Switch checked={autoSkills} onChange={setAutoSkills} label="自动更新技能" />
      </div>
      <div className="setting-row pad" style={{ justifyContent: "flex-start" }}>
        <button className="btn primary" onClick={() => void save()} disabled={busy || !dirty}>{busy ? <><Spin /> 保存中…</> : "保存"}</button>
        {ok && <span className="ok-text">{ok}</span>}
        {err && <span className="err">{err}</span>}
      </div>
    </div>
  );
}
