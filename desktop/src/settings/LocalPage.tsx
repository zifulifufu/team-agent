import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, Copy, Download, RefreshCw, Search, Server, Trash2 } from "lucide-react";
import { api, pullLocalModel, type LocalCandidate, type LocalCatalog, type LocalFamily, type LocalFit, type LocalModelRow, type UpdateItem } from "../api";
import { useData } from "../data";
import { useConfirm } from "../ui";
import { StrengthChips } from "../components/Strengths";
import { Callout, ExtLink, isHttps, Spin } from "../components/ExtBits";
import type { PageProps } from "./SettingsModal";
import "../styles/local.css";

const ACCEL: Record<string, string> = { metal: "Apple 芯片(Metal 加速)", cuda: "NVIDIA(CUDA 加速)", none: "无 GPU 加速(纯 CPU)", unknown: "加速方式未知" };
const FIT_LABEL: Record<LocalFit, string> = { ok: "内存充足", tight: "内存偏紧", no: "内存不足", unknown: "" };
const SRC_LABEL: Record<string, string> = { ollama: "Ollama 新模型", successor: "新一代", hf: "Hugging Face", github: "GitHub", "ollama-release": "Ollama 程序" };

const gb = (n: number) => (n >= 100 ? Math.round(n) : Math.round(n * 10) / 10) + " GB";
const asCand = (u: UpdateItem): LocalCandidate => u.detail as unknown as LocalCandidate;

const VLLM_CMD = `pip install vllm\nvllm serve "deepseek-ai/DeepSeek-V4-Flash"`;
const SGLANG_CMD = `pip install sglang\npython3 -m sglang.launch_server --model-path "deepseek-ai/DeepSeek-V4-Flash" --host 127.0.0.1 --port 30000`;

export default function LocalPage({ onTab }: PageProps) {
  const { settings, models, providers, reload, reloadUpdates } = useData();
  const confirm = useConfirm();
  const [cat, setCat] = useState<LocalCatalog | null>(null);
  const [err, setErr] = useState("");
  const [cands, setCands] = useState<UpdateItem[]>([]);
  const [q, setQ] = useState("");
  const [onlyFit, setOnlyFit] = useState(true);
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [prog, setProg] = useState<Record<string, string>>({});
  const [checking, setChecking] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [errList, setErrList] = useState<string[]>([]);

  const refresh = useCallback(async () => {
    try {
      setCat(await api.localCatalog());
      setErr("");
    } catch (e) {
      setErr((e as Error).message);
    }
    try {
      setCands((await api.updates()).items.filter((i) => i.kind === "localmodel"));
    } catch { /* 提醒读不到不影响主体 */ }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  const ollama = providers.find((p) => p.kind === "ollama");
  const chain = settings?.route_chain ?? [];
  const fallbackId = chain.length ? chain[chain.length - 1] : "";

  const pull = async (tag: string, row?: { size_gb: number; fit: LocalFit; disk_ok: boolean; slow: boolean }) => {
    if (row) {
      const hw = cat?.hardware;
      const warns: string[] = [];
      if (row.fit === "no") warns.push(`需要约 ${gb(row.size_gb)},这台电脑内存只有 ${hw?.ram_gb ?? "?"} GB,很可能装不下`);
      else if (row.fit === "tight") warns.push(`需要约 ${gb(row.size_gb)},内存偏紧,运行时电脑可能变卡`);
      if (!row.disk_ok) warns.push(`可用磁盘只有 ${hw?.disk_free_gb ?? "?"} GB,放不下`);
      if (row.slow) warns.push("这台电脑没有 GPU 加速,这么大的模型会非常慢");
      if (warns.length || row.size_gb >= 30) {
        const text = (warns.length ? warns.join(";") : `下载约 ${gb(row.size_gb)}`) + "。仍要下载吗?";
        if (!(await confirm(`${tag}:${text}`, { okText: "仍然下载", danger: true }))) return;
      }
    }
    setBusy(tag);
    setProg((p) => ({ ...p, [tag]: "开始下载…" }));
    const ok = await pullLocalModel(tag, (p) => {
      const m = p.error ? "✗ " + p.error : p.total && p.completed ? `${p.status} ${Math.round((p.completed / p.total) * 100)}%` : p.status ?? "";
      setProg((x) => ({ ...x, [tag]: m }));
    });
    setBusy(null);
    if (ok) {
      setProg((x) => ({ ...x, [tag]: "✓ 下载完成,已加入模型列表" }));
      await reload();
    }
    void refresh();
  };

  const setFallback = async (name: string) => {
    if (!ollama) return;
    const id = `${ollama.id}/${name}`;
    if (!models.some((m) => m.id === id)) await api.addModel(ollama.id, name);
    const cloud = chain.filter((c) => !models.find((m) => m.id === c)?.is_local);
    await api.putSettings({ route_chain: [...cloud, id] });
    await reload();
  };

  const check = async () => {
    setChecking(true);
    setMsg(null);
    setErrList([]);
    try {
      const r = await api.localCheck();
      setErrList(r.errors);
      await Promise.all([refresh(), reloadUpdates(), reload()]);
      const parts = [r.found ? `发现 ${r.found} 个新的开源模型/仓库` : "没有发现新的开源模型"];
      if (r.catalog_update?.applied) parts.push(`推荐目录已更新到 ${r.catalog_update.latest}`);
      if (r.errors.length) parts.push(`${r.errors.length} 个来源没查成`);
      setMsg({ ok: r.errors.length === 0, text: parts.join(";") + "。" });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setChecking(false);
    }
  };

  const hw = cat?.hardware;
  const needle = q.trim().toLowerCase();
  const view = useMemo(() => {
    if (!cat) return { fams: [] as LocalFamily[], hidden: 0 };
    let hidden = 0;
    const fams = cat.families
      .map((f) => {
        const rows = f.models.filter((m) => {
          if (needle && !`${f.vendor} ${f.name} ${m.tag} ${m.note}`.toLowerCase().includes(needle)) return false;
          if (onlyFit && hw?.ram_gb != null && !m.installed && (m.fit === "no" || !m.disk_ok)) { hidden++; return false; }
          return true;
        });
        return { ...f, models: rows };
      })
      .filter((f) => f.models.length > 0);
    return { fams, hidden };
  }, [cat, needle, onlyFit, hw]);

  const installed = cat?.installed ?? [];
  const has = (n: string) => installed.includes(n) || installed.includes(n + ":latest");

  return (
    <div className="sp lp">
      <h2 className="sp-title">本地模型</h2>
      <p className="sp-desc">
        云端不可用、被禁用外呼时,路由层会自动回退到这里的本地模型,数据不出本机。下面按厂商列出各家最新的开源大语言模型,
        并按这台电脑的内存和磁盘估算哪些跑得动(估算不是保证)。
      </p>

      <div className="card">
        <div className="setting-row">
          <div>
            <div className="sr-title">
              <i className={"dot " + (cat == null ? "off" : cat.running ? "ok" : "bad")} /> Ollama {cat == null ? "检测中…" : cat.running ? "运行中" : "未检测到"}
            </div>
            <div className="sr-desc">
              {cat?.running ? "模型由 Ollama 下载和运行" : "安装并启动 Ollama 后点右侧刷新"}
              {hw && (
                <span className="lp-hw">
                  这台电脑:内存 {hw.ram_gb != null ? `${hw.ram_gb} GB` : "未知"} · 可用磁盘 {hw.disk_free_gb != null ? `${hw.disk_free_gb} GB` : "未知"} · {ACCEL[hw.accel]}
                  {hw.translated && " · 后端 Python 是 Intel 版(经 Rosetta 转译),建议用 arm64 版 Python 重建 .venv"}
                </span>
              )}
            </div>
          </div>
          <button className="btn small" onClick={refresh}><RefreshCw size={14} /> 刷新</button>
        </div>
        {cat && !cat.running && (
          <div className="hint">
            macOS:<code>brew install ollama && ollama serve</code>;其它系统从 github.com/ollama/ollama 下载安装包。也可以运行项目里的 <code>scripts/setup_local_model.sh</code> 一键完成。
          </div>
        )}
        {err && <div className="err small" style={{ marginTop: 8 }}>{err}</div>}
      </div>

      <div className="lp-bar">
        <h3 className="sec" style={{ margin: 0 }}>推荐模型{cat && <span className="muted small lp-ver"> 目录 {cat.version}{cat.source === "override" ? "(已更新)" : ""}</span>}</h3>
        <button className="btn small" disabled={checking} onClick={check} title="从 Ollama 模型库、Hugging Face、GitHub 上找新的开源模型和新版本">
          {checking ? <><Spin size={12} /> 检查中…</> : <><RefreshCw size={13} /> 检查新模型</>}
        </button>
      </div>
      {msg && <div className={"small " + (msg.ok ? "ok-text" : "err")} role="status" style={{ marginBottom: 8 }}>{msg.text}</div>}
      {errList.length > 0 && (
        <details className="small muted lp-errs">
          <summary>查看没查成的来源({errList.length})</summary>
          <ul>{errList.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </details>
      )}

      {cands.length > 0 && (
        <div className="lp-cands" aria-label="发现的新模型">
          <div className="lp-cands-title">发现 {cands.length} 条新的开源模型动态</div>
          {cands.map((u) => (
            <CandRow key={u.id} item={u} onDone={async (t) => { if (t) setMsg({ ok: true, text: t }); await Promise.all([refresh(), reloadUpdates()]); }} />
          ))}
          <div className="muted small">
            「加入推荐」只是把型号(和从 Ollama 注册表读到的大小)加进下面的列表,不会下载。GitHub 上通常只有代码,开放权重多在 Hugging Face 和 Ollama。
          </div>
        </div>
      )}

      <div className="lp-tools">
        <div className="lp-search">
          <Search size={14} />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索厂商或型号,如 gemma、deepseek、30b" aria-label="搜索模型" />
        </div>
        <label className={"check lp-only" + (onlyFit ? " on" : "")} title="隐藏超出这台电脑内存或磁盘的型号(已下载的始终显示)">
          <input type="checkbox" checked={onlyFit} onChange={(e) => setOnlyFit(e.target.checked)} />只看这台电脑跑得动的
        </label>
      </div>
      {onlyFit && view.hidden > 0 && (
        <div className="muted small lp-hidden">已隐藏 {view.hidden} 个超出这台电脑内存或磁盘的型号;取消勾选可查看全部。</div>
      )}

      {cat == null && !err && <div className="empty"><Spin size={14} /> 正在读取…</div>}
      {cat && view.fams.length === 0 && <div className="empty">没有符合条件的型号{onlyFit ? "(试试取消「只看这台电脑跑得动的」)" : ""}</div>}

      <div className="lp-fams">
        {view.fams.map((f) => (
          <section key={f.id} className="lp-fam" aria-label={f.name}>
            <div className="lp-fam-head">
              <span className="tag lp-vendor">{f.vendor}</span>
              <b>{f.name}</b>
              {f.license && <span className="tag" title="许可证以模型发布页为准">{f.license}</span>}
            </div>
            <div className="muted small lp-fam-desc">{f.desc}</div>
            {f.strengths.length > 0 && <StrengthChips tags={f.strengths} max={6} />}
            <div className="lp-rows">
              {f.models.map((m) => (
                <ModelRow
                  key={m.tag}
                  m={m}
                  installed={m.installed || has(m.tag)}
                  isFallback={!!ollama && fallbackId === `${ollama.id}/${m.tag}`}
                  busy={busy}
                  progress={prog[m.tag]}
                  canFallback={!!ollama}
                  onPull={() => pull(m.tag, m)}
                  onFallback={() => setFallback(m.tag)}
                  onRemove={f.extra ? async () => { await api.localRemoveExtra(m.tag); await refresh(); } : undefined}
                />
              ))}
            </div>
          </section>
        ))}
      </div>
      {cat && <div className="muted small lp-note">{cat.note}</div>}

      {cat && cat.selfhost.map((s) => (
        <SelfHost key={s.id} fam={s} onTab={onTab} hasProvider={providers.some((p) => p.id === "deepseek-selfhost")} onAdded={reload} />
      ))}

      {cat && cat.cloud_only.length > 0 && (
        <details className="lp-cloud">
          <summary>还有几个开源模型在 Ollama 上只有云端版({cat.cloud_only.map((c) => c.name).join("、")})</summary>
          <ul>
            {cat.cloud_only.map((c) => <li key={c.name}><b>{c.name}</b>({c.vendor}):{c.note}</li>)}
          </ul>
          <div className="muted small">云端版要联网、走 Ollama 的账号,不属于「数据不出本机」。要在本地跑,需要自行下载权重并自建服务(见上面的自建服务卡片)。</div>
        </details>
      )}

      <h3 className="sec">其它模型</h3>
      <div className="input-group">
        <input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="任意 Ollama 模型名,如 qwen3.8:27b、llama4:16x17b" aria-label="模型名" />
        <button className="btn" disabled={!cat?.running || !custom.trim() || busy !== null} onClick={() => pull(custom.trim())}><Download size={15} /> 下载</button>
      </div>
      {custom.trim() && prog[custom.trim()] && <div className={"small " + (prog[custom.trim()].startsWith("✗") ? "err" : "muted")} style={{ marginTop: 6 }}>{prog[custom.trim()]}</div>}

      {cat?.running && (
        <>
          <h3 className="sec">已下载</h3>
          {installed.length === 0 && <div className="muted small">暂无</div>}
          <div className="lp-installed">
            {installed.map((n) => {
              const isFb = !!ollama && fallbackId === `${ollama.id}/${n}`;
              return (
                <div key={n} className="lp-inst">
                  <code>{n}</code>
                  {isFb ? <span className="tag on">当前兜底</span> : <button className="btn small" disabled={!ollama} onClick={() => setFallback(n)}>设为兜底</button>}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ 一个型号一行
function ModelRow({ m, installed, isFallback, busy, progress, canFallback, onPull, onFallback, onRemove }: {
  m: LocalModelRow; installed: boolean; isFallback: boolean; busy: string | null; progress?: string; canFallback: boolean;
  onPull: () => void; onFallback: () => void; onRemove?: () => void;
}) {
  const bad = m.fit === "no" || !m.disk_ok;
  return (
    <div className="lp-row">
      <div className="lp-row-main">
        <code className="lp-tag">{m.tag}</code>
        <span className="lp-size">{m.size_gb ? gb(m.size_gb) : "大小未知"}</span>
        {m.ctx && <span className="muted small">上下文 {m.ctx}</span>}
        {FIT_LABEL[m.fit] && <span className={"tag lp-fit " + m.fit}>{FIT_LABEL[m.fit]}</span>}
        {!m.disk_ok && <span className="tag lp-fit no">磁盘不足</span>}
        {m.slow && !bad && <span className="tag lp-fit tight" title="没有 GPU 加速时,较大的模型生成很慢">会比较慢</span>}
        {m.note && <span className="muted small lp-note-txt">{m.note}</span>}
      </div>
      <div className="lp-row-act">
        {installed ? (
          <>
            <span className="ok-text lc-done"><CheckCircle2 size={14} /> 已下载</span>
            {isFallback ? <span className="tag on">当前兜底</span> : <button className="btn small" disabled={!canFallback} onClick={onFallback}>设为兜底</button>}
          </>
        ) : (
          <button className={"btn small" + (bad ? "" : " primary")} disabled={busy !== null} onClick={onPull}>
            {busy === m.tag ? <><Spin size={12} /> 下载中</> : <><Download size={13} /> 下载</>}
          </button>
        )}
        {onRemove && <button className="icon-btn tiny" aria-label={`从推荐列表移除 ${m.tag}`} title="从推荐列表移除" onClick={onRemove}><Trash2 size={13} /></button>}
      </div>
      {progress && <div className={"small lp-prog " + (progress.startsWith("✗") ? "err" : "muted")}>{progress}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ 发现的新模型
function CandRow({ item, onDone }: { item: UpdateItem; onDone: (t?: string) => Promise<void> }) {
  const c = asCand(item);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const run = async (fn: () => Promise<string | void>) => {
    setBusy(true);
    setErr("");
    try {
      await onDone((await fn()) || undefined);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="lp-cand">
      <div className="lp-cand-main">
        <div>
          <span className="tag new">{SRC_LABEL[c.source] ?? c.source}</span> <b>{c.name}</b>
          {c.size_gb ? <span className="muted small"> · 约 {gb(c.size_gb)}</span> : null}
          {c.replaces && <span className="muted small"> · {c.replaces} 的后续</span>}
          {c.license && <span className="muted small"> · {c.license}</span>}
          {typeof c.stars === "number" && c.stars > 0 && <span className="muted small"> · ★ {c.stars}</span>}
        </div>
        {c.source === "ollama-release" ? (
          <div className="muted small">本机 Ollama {c.local || "?"},GitHub 最新 {c.latest}。新模型常常需要较新的 Ollama;程序不会自动升级它,请自己下载安装。</div>
        ) : (
          c.desc && <div className="muted small">{c.desc}</div>
        )}
        {err && <div className="err small">{err}</div>}
      </div>
      <div className="lp-cand-act">
        {c.tag && <button className="btn small primary" disabled={busy} onClick={() => run(async () => { await api.localAdd(c.tag as string); return `已把 ${c.tag} 加入推荐列表。`; })}>加入推荐</button>}
        {isHttps(c.url) && <ExtLink href={c.url} className="btn small">打开页面</ExtLink>}
        <button className="btn small ghost" disabled={busy} onClick={() => run(async () => { await api.dismissUpdate(item.id); })}>忽略</button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ 自建 DeepSeek 服务
function SelfHost({ fam, hasProvider, onTab, onAdded }: {
  fam: LocalCatalog["selfhost"][number]; hasProvider: boolean; onTab: PageProps["onTab"]; onAdded: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState("");
  const add = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.addProvider({ preset: "deepseek-selfhost" });
      await onAdded();
      onTab("providers");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const copy = async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      window.setTimeout(() => setCopied(""), 1500);
    } catch { /* 剪贴板不可用时用户可以手动选中文字 */ }
  };
  return (
    <section className="lp-self" aria-label={`自建 ${fam.name} 服务`}>
      <h3 className="sec"><Server size={15} /> 自建 {fam.name} 服务</h3>
      <div className="muted small lp-fam-desc">{fam.desc}</div>
      {fam.strengths.length > 0 && <StrengthChips tags={fam.strengths} max={6} />}
      <div className="lp-rows">
        {fam.models.map((m) => (
          <div key={m.id} className="lp-row">
            <div className="lp-row-main">
              <code className="lp-tag">{m.id}</code>
              <span className="muted small">{m.params}</span>
              {m.size_gb > 0 && <span className="lp-size">4 位量化约 {gb(m.size_gb)}</span>}
              {m.size_gb > 0 && FIT_LABEL[m.fit] && <span className={"tag lp-fit " + m.fit}>{FIT_LABEL[m.fit]}</span>}
              <span className="muted small lp-note-txt">{m.note}</span>
            </div>
          </div>
        ))}
      </div>
      <Callout tone="warn" title="先说清楚">
        这类模型不是点一下就能在普通电脑上跑:官方权重需要多张高端 GPU,或用社区的量化版(例如 Unsloth 的 GGUF,4 位约 155GB 内存)配合 llama.cpp。
        这里只负责把你已经起好的服务接进群聊。它的权重发布在 Hugging Face({fam.license || "见发布页"} 许可),GitHub 上没有对应的模型仓库;上一代 V3 见 github.com/deepseek-ai/DeepSeek-V3。
      </Callout>
      <div className="lp-cmds">
        {([["vLLM(默认端口 8000,地址填 http://127.0.0.1:8000/v1)", VLLM_CMD, "vllm"], ["SGLang(端口 30000,预设默认地址)", SGLANG_CMD, "sglang"]] as const).map(([title, cmd, key]) => (
          <div key={key} className="lp-cmd">
            <div className="lp-cmd-head"><span className="small">{title}</span>
              <button className="btn small ghost" onClick={() => copy(key, cmd)} aria-label={`复制 ${key} 命令`}><Copy size={12} /> {copied === key ? "已复制" : "复制"}</button>
            </div>
            <pre>{cmd}</pre>
          </div>
        ))}
        <div className="muted small">命令取自模型发布页的部署说明;我没有在你的机器上验证过。SGLang 命令里的 <code>--host 127.0.0.1</code> 让服务只对本机开放,不要改成 0.0.0.0,否则同一网络里的人都能调用它。</div>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        {hasProvider
          ? <button className="btn" onClick={() => onTab("providers")}>去「模型服务」修改地址和型号</button>
          : <button className="btn primary" disabled={busy} onClick={add}>{busy ? <><Spin size={12} /> 添加中</> : "添加为本地服务商"}</button>}
        {err && <span className="err small">{err}</span>}
      </div>
    </section>
  );
}
