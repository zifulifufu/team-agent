import { useCallback, useEffect, useState } from "react";
import { Eraser, LoaderCircle, Pencil, Pin, Plus, Search, Trash2, X } from "lucide-react";
import { api, relTime, type Memory, type MemoryKind, type MemoryScope, type Settings } from "../api";
import { useData } from "../data";
import { Modal, Switch, useConfirm, useOutside } from "../ui";
import ObsidianCard from "../components/ObsidianCard";
import "../styles/know.css";

const MAX = 500;
const KINDS: { id: MemoryKind; label: string; hint: string }[] = [
  { id: "preference", label: "偏好", hint: "你的习惯、口吻、格式要求" },
  { id: "fact", label: "事实", hint: "项目、人物、术语等客观信息" },
  { id: "decision", label: "决定", hint: "已经拍板的选择" },
  { id: "lesson", label: "教训", hint: "踩过的坑、效果好或不好的做法" },
  { id: "action", label: "做法", hint: "多步任务的处理步骤" },
];
const KIND_LABEL = Object.fromEntries(KINDS.map((k) => [k.id, k.label])) as Record<string, string>;
const SOURCE_LABEL: Record<string, string> = { manual: "手动添加", auto: "自动提炼", action: "行为记录", obsidian: "来自 Obsidian" };
/** 行为记录:后端把多步任务的做法存成 kind=action、source=auto(不是 source=action),所以这里按两种写法都认。 */
const isActionLog = (m: Memory) => m.source === "action" || (m.kind === "action" && m.source !== "manual");
const sourceLabel = (m: Memory) => (isActionLog(m) ? SOURCE_LABEL.action : SOURCE_LABEL[m.source] ?? m.source);
type ScopeTab = "all" | MemoryScope;
const SCOPE_TABS: { id: ScopeTab; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "global", label: "全局" },
  { id: "group", label: "群聊" },
  { id: "agent", label: "成员" },
];
const LIST_LIMIT = 500; // 后端一次最多返回这么多条

function scopeText(m: Memory): string {
  if (m.scope === "global") return "全局";
  if (m.scope === "group") return `群聊 · ${m.scope_name || "已删除的群"}`;
  return `成员 · ${m.scope_name || "已删除的成员"}`;
}

export default function MemoryPage() {
  const { settings, groups, agents, reload } = useData();
  const confirm = useConfirm();

  const [tab, setTab] = useState<ScopeTab>("all");
  const [scopeId, setScopeId] = useState("");
  const [kind, setKind] = useState<"" | MemoryKind>("");
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");

  const [items, setItems] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [actErr, setActErr] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [tick, setTick] = useState(0); // 递增即重新加载
  const refresh = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    const t = window.setTimeout(() => setQ(qInput.trim()), 300);
    return () => window.clearTimeout(t);
  }, [qInput]);

  useEffect(() => {
    let alive = true;
    api
      .memories({ scope: tab === "all" ? undefined : tab, scope_id: tab === "group" || tab === "agent" ? scopeId || undefined : undefined, kind: kind || undefined, q: q || undefined })
      .then((r) => { if (alive) { setItems(r.memories); setErr(""); } })
      .catch((e) => alive && setErr((e as Error).message))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [tab, scopeId, kind, q, tick]);

  const filtered = tab !== "all" || !!scopeId || !!kind || !!q;
  const scopeNameFor = (s: ScopeTab, id: string) =>
    s === "global" ? "全局" : s === "group" ? `群聊「${groups.find((g) => g.id === id)?.name ?? "?"}」` : `成员「${agents.find((a) => a.id === id)?.name ?? "?"}」`;

  // ---- 设置开关
  const setFlag = async (k: "memory_enabled" | "memory_auto_extract", v: boolean) => {
    setActErr("");
    try {
      await api.putSettings({ [k]: v } as Partial<Settings>);
      await reload();
    } catch (e) {
      setActErr((e as Error).message);
    }
  };

  // ---- 单条操作
  const act = async (fn: () => Promise<unknown>) => {
    setActErr("");
    try {
      await fn();
      refresh();
    } catch (e) {
      setActErr((e as Error).message);
    }
  };
  const remove = async (m: Memory) => {
    if (!(await confirm(`删除这条记忆「${m.content.length > 60 ? m.content.slice(0, 60) + "…" : m.content}」?删除后无法恢复。`, { okText: "删除" }))) return;
    await act(() => api.delMemory(m.id));
  };

  // ---- 清理
  const countText = (n: number) => (n >= LIST_LIMIT ? `${LIST_LIMIT} 条以上` : `${n} 条`);
  const clearBySource = async (source: "auto" | "action") => {
    setActErr("");
    try {
      // 「行为记录」按类型 action(且不是手动添加)筛出来逐条删除;「自动生成」按 source=auto 整体清空
      const all = await api.memories(source === "action" ? { kind: "action" } : {});
      const list = all.memories.filter((m) => (source === "action" ? isActionLog(m) : m.source === "auto"));
      const n = list.length;
      if (n === 0) return setActErr(source === "auto" ? "没有自动生成的记忆可清空" : "没有行为记录可清空");
      const what = source === "auto" ? "自动生成的记忆(偏好、决定、教训等,也包括多步任务的行为记录)" : "行为记录(多步任务的做法)";
      if (!(await confirm(`将删除全部${what},共 ${countText(n)}。手动添加的记忆不受影响。删除后无法恢复。`, { okText: "清空" }))) return;
      let done = 0;
      if (source === "auto") {
        done = (await api.clearMemories({ source: "auto" })).deleted;
      } else {
        const rs = await Promise.allSettled(list.map((m) => api.delMemory(m.id)));
        done = rs.filter((r) => r.status === "fulfilled").length;
        const bad = rs.find((r): r is PromiseRejectedResult => r.status === "rejected");
        if (bad) setActErr(`有 ${n - done} 条没能删除:${(bad.reason as Error).message}`);
      }
      refresh();
      setNotice(`已清空 ${done} 条`);
    } catch (e) {
      setActErr((e as Error).message);
    }
  };
  const clearScope = async () => {
    if (tab === "all") return;
    const sid = tab === "global" ? "" : scopeId;
    setActErr("");
    try {
      const n = (await api.memories({ scope: tab, scope_id: sid || undefined })).memories.length;
      if (n === 0) return setActErr("这个范围里没有记忆");
      if (!(await confirm(`将删除${scopeNameFor(tab, sid)}范围内的全部记忆(含手动添加和置顶的),共 ${countText(n)}。删除后无法恢复。`, { okText: "清空" }))) return;
      const r = await api.clearMemories({ scope: tab, scope_id: sid || undefined });
      refresh();
      setNotice(`已清空 ${r.deleted} 条`);
    } catch (e) {
      setActErr((e as Error).message);
    }
  };
  const [notice, setNotice] = useState("");
  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(""), 3500);
    return () => window.clearTimeout(t);
  }, [notice]);

  const scopeOptions = tab === "group" ? groups.map((g) => ({ id: g.id, name: g.name })) : tab === "agent" ? agents.map((a) => ({ id: a.id, name: `${a.avatar} ${a.name}` })) : [];
  const canClearScope = tab === "global" || ((tab === "group" || tab === "agent") && !!scopeId);
  const enabled = settings?.memory_enabled ?? true;

  return (
    <div className="kn-page">
      <div className="kn-inner">
        <div className="kn-head">
          <div className="kn-head-main">
            <h1>记忆</h1>
            <p className="kn-desc">
              记忆让成员越用越有针对性:群聊结束后,较便宜的模型会自动提炼你的偏好、做出的决定、得到的教训,并记下多步任务的做法;下次遇到相似任务时会被召回并注入成员的提示词。密码、密钥、长数字串等敏感内容不会被自动记录。所有记忆只存在本机,你可以随时编辑或删除。
            </p>
          </div>
          <div className="kn-head-actions">
            <ClearMenu
              canScope={canClearScope}
              onAuto={() => void clearBySource("auto")}
              onAction={() => void clearBySource("action")}
              onScope={() => void clearScope()}
            />
            <button className="btn primary" onClick={() => setAddOpen(true)}><Plus size={15} /> 添加记忆</button>
          </div>
        </div>

        <div className="card flush kn-switches">
          <div className="setting-row pad">
            <div>
              <div className="sr-title">启用记忆</div>
              <div className="sr-desc">总开关。关闭后不再召回、也不再记录;已有的记忆保留。每个群还可以在「扩展」里单独关闭。</div>
            </div>
            <Switch checked={enabled} onChange={(v) => void setFlag("memory_enabled", v)} disabled={!settings} label="启用记忆" />
          </div>
          <div className="setting-row pad">
            <div>
              <div className="sr-title">群聊结束后自动提炼</div>
              <div className="sr-desc">用较便宜的模型总结这轮对话,会消耗少量额度;关闭后不再自动提炼偏好、决定、教训(多步任务的做法仍会记录),你仍可手动添加。</div>
            </div>
            <Switch checked={enabled && (settings?.memory_auto_extract ?? true)} onChange={(v) => void setFlag("memory_auto_extract", v)} disabled={!settings || !enabled} label="自动提炼" />
          </div>
        </div>

        <ObsidianCard onSynced={refresh} />

        <div className="kn-filters">
          <div className="seg" role="tablist" aria-label="记忆范围">
            {SCOPE_TABS.map((t) => (
              <button key={t.id} role="tab" aria-selected={tab === t.id} className={tab === t.id ? "on" : ""} onClick={() => { setTab(t.id); setScopeId(""); }}>
                {t.label}
              </button>
            ))}
          </div>
          {(tab === "group" || tab === "agent") && (
            <select className="kn-select" value={scopeId} onChange={(e) => setScopeId(e.target.value)} aria-label={tab === "group" ? "选择群聊" : "选择成员"}>
              <option value="">{tab === "group" ? "全部群聊" : "全部成员"}</option>
              {scopeOptions.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
            </select>
          )}
          <select className="kn-select" value={kind} onChange={(e) => setKind(e.target.value as "" | MemoryKind)} aria-label="按类型筛选">
            <option value="">全部类型</option>
            {KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
          </select>
          <span className="grow" />
          <label className="search-box kn-search-sm">
            <Search size={15} />
            <input value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="搜索记忆内容" aria-label="搜索记忆内容" />
            {qInput && <button className="icon-btn tiny" aria-label="清空搜索" title="清空" onClick={() => setQInput("")}><X size={13} /></button>}
          </label>
        </div>

        {notice && <div className="ok-text kn-block">{notice}</div>}
        {actErr && <div className="err kn-block">{actErr}</div>}
        {err && <div className="err kn-block">{err}</div>}
        {loading && <div className="empty"><LoaderCircle size={16} className="kn-spin" /> 加载中…</div>}

        {!loading && !err && items.length === 0 && (
          <div className="kn-empty-box">
            {filtered ? (
              <p><b>没有符合条件的记忆。</b>换个范围、类型或关键词试试。</p>
            ) : (
              <>
                <p><b>还没有记忆。</b>记忆有两种来源:</p>
                <ul>
                  <li><b>自动提炼</b>:开启上面的「群聊结束后自动提炼」,聊完之后会自动记下偏好、决定、教训和多步任务的做法。</li>
                  <li><b>手动添加</b>:你可以随时写下希望成员记住的事,比如口吻、格式要求、固定口径。</li>
                </ul>
                <button className="btn small" onClick={() => setAddOpen(true)}><Plus size={14} /> 添加记忆</button>
              </>
            )}
          </div>
        )}

        {items.length > 0 && (
          <>
            <div className="kn-count muted small">
              共 {items.length}{items.length >= LIST_LIMIT ? "+" : ""} 条 · 置顶的记忆每次都会被带上,其余的按与当前任务的相关度召回
            </div>
            <div className="kn-mems">
              {items.map((m) =>
                editing === m.id ? (
                  <EditCard key={m.id} m={m} onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); refresh(); }} />
                ) : (
                  <article key={m.id} className={"kn-mem" + (m.pinned ? " pinned" : "")}>
                    <div className="kn-mem-top">
                      <span className={"tag kn-kind " + m.kind} title={KINDS.find((k) => k.id === m.kind)?.hint}>{KIND_LABEL[m.kind] ?? m.kind}</span>
                      <span className="kn-mem-scope">{scopeText(m)}</span>
                      <span className="grow" />
                      <button
                        className={"icon-btn tiny" + (m.pinned ? " on" : "")}
                        aria-label={m.pinned ? "取消置顶" : "置顶"}
                        aria-pressed={m.pinned}
                        title={m.pinned ? "取消置顶" : "置顶:每次都会被带上"}
                        onClick={() => void act(() => api.patchMemory(m.id, { pinned: !m.pinned }))}
                      >
                        <Pin size={14} fill={m.pinned ? "currentColor" : "none"} />
                      </button>
                      <button className="icon-btn tiny" aria-label="编辑" title="编辑" onClick={() => setEditing(m.id)}><Pencil size={14} /></button>
                      <button className="icon-btn tiny kn-del" aria-label="删除" title="删除" onClick={() => void remove(m)}><Trash2 size={14} /></button>
                    </div>
                    <p className="kn-mem-text">{m.content}</p>
                    <div className="kn-mem-meta">
                      {m.pinned && <span className="tag on"><Pin size={10} /> 置顶</span>}
                      <span>来源:{sourceLabel(m)}</span>
                      <span>命中 {m.hits} 次</span>
                      <span>更新于 {relTime(m.updated_at)}</span>
                    </div>
                  </article>
                ),
              )}
            </div>
          </>
        )}
      </div>

      {addOpen && (
        <AddModal
          initial={{ tab, scopeId }}
          onClose={() => setAddOpen(false)}
          onSaved={() => { setAddOpen(false); refresh(); }}
        />
      )}
    </div>
  );
}

// ------------------------------------------------------------- clear menu
function ClearMenu({ canScope, onAuto, onAction, onScope }: { canScope: boolean; onAuto: () => void; onAction: () => void; onScope: () => void }) {
  const [open, setOpen] = useState(false);
  const ref = useOutside<HTMLDivElement>(open, () => setOpen(false));
  const item = (label: string, fn: () => void, disabled?: boolean, title?: string) => (
    <button role="menuitem" className="kn-menu-item" disabled={disabled} title={title} onClick={() => { setOpen(false); fn(); }}>{label}</button>
  );
  return (
    <div className="kn-menu-wrap" ref={ref}>
      <button className="btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}><Eraser size={15} /> 清理</button>
      {open && (
        <div className="kn-menu" role="menu">
          {item("清空自动生成的记忆", onAuto)}
          {item("清空行为记录", onAction)}
          {item("清空当前筛选范围", onScope, !canScope, canScope ? undefined : "先在下方选中「全局」,或选中某个具体的群聊 / 成员")}
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------------- inline edit
function EditCard({ m, onCancel, onSaved }: { m: Memory; onCancel: () => void; onSaved: () => void }) {
  const [text, setText] = useState(m.content);
  const [kind, setKind] = useState<MemoryKind>(m.kind);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const over = text.length > MAX;
  const save = async () => {
    if (!text.trim()) return setErr("内容不能为空");
    if (over) return setErr(`内容不能超过 ${MAX} 字`);
    setBusy(true);
    setErr("");
    try {
      await api.patchMemory(m.id, { content: text.trim(), kind });
      onSaved();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <article className="kn-mem editing" onKeyDown={(e) => { if (e.key === "Escape") onCancel(); }}>
      <textarea
        rows={3}
        autoFocus
        value={text}
        aria-label="记忆内容"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void save(); }}
      />
      <div className="kn-edit-bar">
        <select className="kn-select" value={kind} onChange={(e) => setKind(e.target.value as MemoryKind)} aria-label="类型">
          {KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
        </select>
        <span className={"small " + (over ? "err" : "muted")}>{text.length} / {MAX}</span>
        {err && <span className="err small">{err}</span>}
        <span className="grow" />
        <button className="btn small" onClick={onCancel}>取消</button>
        <button className="btn small primary" disabled={busy} onClick={save}>{busy ? "保存中…" : "保存"}</button>
      </div>
    </article>
  );
}

// ---------------------------------------------------------------- add modal
function AddModal({ initial, onClose, onSaved }: { initial: { tab: ScopeTab; scopeId: string }; onClose: () => void; onSaved: () => void }) {
  const { groups, agents } = useData();
  const [content, setContent] = useState("");
  const [scope, setScope] = useState<MemoryScope>(initial.tab === "all" ? "global" : initial.tab);
  const [scopeId, setScopeId] = useState(initial.tab === "group" || initial.tab === "agent" ? initial.scopeId : "");
  const [kind, setKind] = useState<MemoryKind>("fact");
  const [pinned, setPinned] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const over = content.length > MAX;
  const opts = scope === "group" ? groups.map((g) => ({ id: g.id, name: g.name })) : scope === "agent" ? agents.map((a) => ({ id: a.id, name: `${a.avatar} ${a.name}` })) : [];

  const save = async () => {
    if (!content.trim()) return setErr("请填写记忆内容");
    if (over) return setErr(`内容不能超过 ${MAX} 字(现在 ${content.length} 字)`);
    if (scope !== "global" && !scopeId) return setErr(scope === "group" ? "请选择一个群聊" : "请选择一个成员");
    setBusy(true);
    setErr("");
    try {
      await api.addMemory({ content: content.trim(), scope, scope_id: scope === "global" ? "" : scopeId, kind, pinned });
      onSaved();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <Modal
      title="添加记忆"
      wide
      onClose={onClose}
      actions={
        <>
          {err && <span className="err" style={{ marginRight: "auto" }}>{err}</span>}
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={busy} onClick={save}>{busy ? "保存中…" : "添加"}</button>
        </>
      }
    >
      <label className="field">
        <span>内容(一条只记一件事,越具体越好用)</span>
        <textarea rows={4} autoFocus value={content} onChange={(e) => setContent(e.target.value)} placeholder="如:对外发布的文案统一用「您」,不用「你」" />
        <span className={"small kn-counter " + (over ? "err" : "muted")}>{content.length} / {MAX}</span>
      </label>
      <div className="field">
        <span>范围</span>
        <div className="row kn-scope-row">
          <div className="seg" role="radiogroup" aria-label="记忆范围">
            {(["global", "group", "agent"] as MemoryScope[]).map((s) => (
              <button key={s} role="radio" aria-checked={scope === s} className={scope === s ? "on" : ""} onClick={() => { setScope(s); setScopeId(""); setErr(""); }}>
                {s === "global" ? "全局" : s === "group" ? "群聊" : "成员"}
              </button>
            ))}
          </div>
          {scope !== "global" && (
            <select value={scopeId} onChange={(e) => { setScopeId(e.target.value); setErr(""); }} aria-label={scope === "group" ? "选择群聊" : "选择成员"}>
              <option value="">{scope === "group" ? "选择群聊…" : "选择成员…"}</option>
              {opts.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
            </select>
          )}
        </div>
        <span className="muted small">全局:所有群都可能用到;群聊:只在那个群里用;成员:只在那位成员发言时用。</span>
      </div>
      <div className="field">
        <span>类型</span>
        <div className="kn-kinds" role="radiogroup" aria-label="记忆类型">
          {KINDS.map((k) => (
            <button key={k.id} role="radio" aria-checked={kind === k.id} className={"kn-kind-pick" + (kind === k.id ? " on" : "")} onClick={() => setKind(k.id)} title={k.hint}>
              {k.label}
            </button>
          ))}
        </div>
        <span className="muted small">{KINDS.find((k) => k.id === kind)?.hint}</span>
      </div>
      <label className="check-inline" style={{ marginBottom: 6 }}>
        <input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} />
        置顶(每次对话都带上,不宜太多)
      </label>
    </Modal>
  );
}
