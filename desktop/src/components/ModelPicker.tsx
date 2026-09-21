import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronRight, Plus, RefreshCw, Search } from "lucide-react";
import { api, type ModelOption, type ModelOptions, type Provider, type Tag } from "../api";
import { useData } from "../data";
import { Modal, useBusy, useConfirm } from "../ui";
import { StrengthChips, useStrengthTags } from "./Strengths";
import "../styles/models.css";

const TIER_LABEL: Record<string, string> = { flagship: "旗舰", balanced: "均衡", fast: "快速" };

/** 1000000 → 「1M 上下文」,128000 → 「128K 上下文」 */
function fmtContext(n: number | null | undefined): string {
  if (!n || n <= 0) return "";
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)}M 上下文`;
  if (n >= 1000) return `${Math.round(n / 1000)}K 上下文`;
  return `${n} 上下文`;
}

/** 「N 个新模型」用:新出现且还没添加的(已添加的新模型不需要再挑选) */
export const pendingNew = (o: ModelOptions): number => o.models.filter((m) => m.is_new && !m.added).length;

const fmtSize = (gb: number) => `${gb >= 100 ? Math.round(gb) : +gb.toFixed(1)} GB`;

function fmtClock(ts: number): string {
  const d = new Date(ts < 1e12 ? ts * 1000 : ts);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function ModelPicker({
  provider,
  focusNew = false,
  onClose,
  onOptions,
}: {
  provider: Provider;
  /** 打开时直接只看新模型 */
  focusNew?: boolean;
  onClose: () => void;
  /** 每次拿到最新的选项(用于让外面更新「N 个新模型」) */
  onOptions?: (o: ModelOptions) => void;
}) {
  const { reload } = useData();
  const confirm = useConfirm();
  const allTags = useStrengthTags();
  const [opts, setOpts] = useState<ModelOptions | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [q, setQ] = useState("");
  const [want, setWant] = useState<Tag[]>([]);
  const [onlyNew, setOnlyNew] = useState(focusNew);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [refreshing, setRefreshing] = useState(false);
  const [refreshErr, setRefreshErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [addErr, setAddErr] = useState("");
  const [manualOpen, setManualOpen] = useState(false);
  const [manual, setManual] = useState("");
  const [manualMsg, setManualMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const confirming = useRef(false);
  const onOptionsRef = useRef(onOptions);
  onOptionsRef.current = onOptions;

  const apply = useCallback((o: ModelOptions) => {
    setOpts(o);
    // 已经变成「已添加」的不再算选中
    setSel((s) => new Set([...s].filter((id) => !o.models.find((m) => m.id === id)?.added)));
    onOptionsRef.current?.(o);
  }, []);

  useEffect(() => {
    let alive = true;
    api.modelOptions(provider.id).then((o) => alive && apply(o)).catch((e) => alive && setLoadErr((e as Error).message));
    return () => { alive = false; };
  }, [provider.id, apply]);

  const list = opts?.models ?? [];
  const shown = useMemo(() => {
    const k = q.trim().toLowerCase();
    return list.filter(
      (m) =>
        (!onlyNew || m.is_new) &&
        want.every((t) => m.strengths.includes(t)) &&
        (!k || m.name.toLowerCase().includes(k) || m.id.toLowerCase().includes(k) || (m.summary ?? "").toLowerCase().includes(k)),
    );
  }, [list, q, want, onlyNew]);
  const pickable = shown.filter((m) => !m.added);
  const allOn = pickable.length > 0 && pickable.every((m) => sel.has(m.id));

  const refresh = async () => {
    setRefreshing(true);
    setRefreshErr("");
    try {
      apply(await api.refreshModelOptions(provider.id));
    } catch (e) {
      setRefreshErr((e as Error).message);
    } finally {
      setRefreshing(false);
    }
  };
  const markSeen = async () => {
    try {
      apply(await api.markModelsSeen(provider.id));
      setOnlyNew(false);
    } catch (e) {
      setRefreshErr((e as Error).message);
    }
  };

  const toggle = async (m: ModelOption) => {
    if (m.added) return;
    if (!sel.has(m.id) && m.retired_reason) {
      confirming.current = true;
      const ok = await confirm(`服务商已停用/将停用该型号(${m.retired_reason})。添加后调用可能失败,仍要添加吗?`, { okText: "仍要添加", danger: false });
      window.setTimeout(() => { confirming.current = false; }, 0);
      if (!ok) return;
    }
    setSel((s) => {
      const n = new Set(s);
      if (n.has(m.id)) n.delete(m.id);
      else n.add(m.id);
      return n;
    });
  };
  const toggleAll = () => {
    if (allOn) setSel((s) => new Set([...s].filter((id) => !pickable.some((m) => m.id === id))));
    else setSel((s) => new Set([...s, ...pickable.filter((m) => !m.retired_reason).map((m) => m.id)]));
  };

  const addSelected = async () => {
    setBusy(true);
    setAddErr("");
    try {
      await api.addModels(provider.id, [...sel]);
      await reload();
      onClose();
    } catch (e) {
      setAddErr((e as Error).message);
      setBusy(false);
    }
  };
  const [addingManual, guardManual] = useBusy();
  const addManual = () => guardManual(async () => {
    const name = manual.trim();
    if (!name) return;
    setManualMsg(null);
    try {
      await api.addModel(provider.id, name);
      setManual("");
      setManualMsg({ ok: true, text: `已添加 ${name}` });
      await reload();
      apply(await api.modelOptions(provider.id));
    } catch (e) {
      setManualMsg({ ok: false, text: (e as Error).message });
    }
  });

  const close = () => { if (!confirming.current) onClose(); };
  const fromLabel = opts ? (opts.catalog_source === "shipped" ? "内置" : "已更新") : "";

  return (
    <Modal
      title={`选择模型 · ${provider.name}`}
      onClose={close}
      wide
      actions={
        <>
          {addErr && <span className="err mp-foot-note">{addErr}</span>}
          <button className="btn" onClick={close}>取消</button>
          <button className="btn primary" disabled={sel.size === 0 || busy} onClick={addSelected}>{busy ? "添加中…" : `添加所选(${sel.size})`}</button>
        </>
      }
    >
      <div className="mp-root">
        <p className="mp-sub">
          {opts && <b>模型目录 {opts.catalog_version}({fromLabel})</b>}
          <span className="mp-note">
            {opts ? "。" : ""}目录是某个日期的快照,可能落后于各家最新发布;想知道服务商此刻真正提供什么,点「刷新实时清单」。强项是根据模型系列和名称推断的标签,不是评测成绩。
          </span>
        </p>

        <div className="mp-bar">
          <div className="search-box">
            <Search size={14} />
            <input autoFocus placeholder="按名称 / 型号 / 简介搜索" value={q} onChange={(e) => setQ(e.target.value)} aria-label="搜索模型" />
          </div>
          <label className="check-inline"><input type="checkbox" checked={onlyNew} onChange={(e) => setOnlyNew(e.target.checked)} />只看新模型{opts && opts.new_count > 0 ? `(${opts.new_count})` : ""}</label>
          <button className="btn small" disabled={refreshing} onClick={refresh} title="向服务商查询它此刻提供的型号(需要联网)">
            <RefreshCw size={13} className={refreshing ? "mp-spin" : ""} /> {refreshing ? "刷新中…" : "刷新实时清单"}
          </button>
          <button className="btn small ghost" disabled={!opts || opts.new_count === 0} onClick={markSeen} title="清掉所有「新」标记">全部标为已看</button>
        </div>

        <div className="mp-filter">
          <span className="mp-filter-label">强项(可多选,须同时具备):</span>
          <span className="str-picker">
            {allTags.map((t) => {
              const on = want.includes(t.id);
              return (
                <button key={t.id} type="button" className={"str-chip pick" + (on ? " on" : "")} aria-pressed={on} title={t.desc} onClick={() => setWant(on ? want.filter((x) => x !== t.id) : [...want, t.id])}>
                  {t.label ?? t.id}
                </button>
              );
            })}
          </span>
          {want.length > 0 && <button className="link small" onClick={() => setWant([])}>清除</button>}
        </div>

        {refreshErr ? (
          <div className="mp-status err-box" role="alert">
            刷新实时清单失败:{refreshErr}
            <span className="mp-err-note">仍显示内置目录{opts && opts.catalog_source !== "shipped" ? "(已更新版本)" : ""},其中「新」「已没有」等标记需要实时清单才能判断。</span>
          </div>
        ) : opts?.live_fetched_at ? (
          <div className="mp-status ok-text">实时清单更新于 {fmtClock(opts.live_fetched_at)}{opts.new_count > 0 ? `,有 ${opts.new_count} 个新模型` : ",没有新模型"}</div>
        ) : opts ? (
          <div className="mp-status muted">还没有刷新过实时清单,当前显示的是内置目录。</div>
        ) : null}

        <div className="mp-listbar">
          <button className="link" disabled={pickable.length === 0} onClick={toggleAll}>{allOn ? "取消全选" : "全选当前筛选"}</button>
          <span className="muted">
            {opts ? `共 ${list.length} 个型号${shown.length !== list.length ? `,当前显示 ${shown.length} 个` : ""}` : ""}
          </span>
        </div>

        <div className="mp-list" role="list" aria-label="模型列表">
          {!opts && !loadErr && <div className="empty">加载中…</div>}
          {loadErr && <div className="empty err">{loadErr}</div>}
          {shown.map((m) => (
            <ModelRow key={m.id} m={m} local={provider.is_local} checked={m.added || sel.has(m.id)} onToggle={() => void toggle(m)} />
          ))}
          {opts && shown.length === 0 && (
            <div className="empty">
              {list.length === 0 ? "目录里没有这个服务商的型号,可在下面手动输入" : onlyNew && !q && want.length === 0 ? "没有新模型。点「刷新实时清单」看看服务商有没有发布新型号" : "没有符合条件的模型"}
            </div>
          )}
        </div>

        <div className="mp-manual">
          <button className={"mp-manual-toggle" + (manualOpen ? " open" : "")} aria-expanded={manualOpen} onClick={() => setManualOpen((v) => !v)}>
            <ChevronRight size={14} /> 手动输入型号(目录里没有的)
          </button>
          {manualOpen && (
            <div className="mp-manual-body">
              <div className="input-group">
                <input value={manual} onChange={(e) => setManual(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && void addManual()} placeholder="型号 ID,如 deepseek-chat / qwen-plus / gpt-4o-mini" aria-label="型号 ID" spellCheck={false} />
                <button className="btn" onClick={() => void addManual()} disabled={!manual.trim() || addingManual}><Plus size={15} /> 添加</button>
              </div>
              {manualMsg && <div className={"fb-note " + (manualMsg.ok ? "ok-text" : "err")}>{manualMsg.text}</div>}
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}

function ModelRow({ m, local, checked, onToggle }: { m: ModelOption; local: boolean; checked: boolean; onToggle: () => void }) {
  const meta: string[] = [];
  const ctx = fmtContext(m.context);
  if (ctx) meta.push(ctx);
  if (m.tier) meta.push(TIER_LABEL[m.tier] ?? m.tier);
  if (m.params) meta.push(m.params);
  if (m.size_gb) meta.push(fmtSize(m.size_gb));
  return (
    <label className={"mp-row" + (m.added ? " added" : "") + (checked && !m.added ? " checked" : "")} role="listitem">
      <input type="checkbox" checked={checked} disabled={m.added} onChange={onToggle} aria-label={`选择 ${m.name}`} />
      <div className="mp-body">
        <div className="mp-title">
          <span className="mp-name">{m.name}</span>
          <span className="mp-id">{m.id}</span>
          <span className="mp-badges">
            {m.added && <span className="tag"><Check size={11} /> 已添加</span>}
            {m.is_new && <span className="tag new">新</span>}
            {m.preview && <span className="tag">预览</span>}
            {m.legacy && <span className="tag">旧版</span>}
            {m.retired_reason && <span className="tag mp-warn-tag">已停用</span>}
            {m.gone && <span className="tag danger" title="这个型号已添加,但服务商的实时清单里已经没有了,多半已下线">服务商清单里已没有</span>}
            {!m.gone && !m.added && !local && m.live === false && <span className="tag" title="实时清单里没有这个型号:目录可能已过时,或该账号无权使用">实时清单里没有</span>}
            {local && m.installed === true && <span className="tag on">已安装</span>}
            {local && m.installed === false && <span className="tag">未安装</span>}
          </span>
        </div>
        {m.summary && <div className="mp-summary">{m.summary}</div>}
        {m.retired_reason && <div className="mp-warn">{m.retired_reason}</div>}
        <div className="mp-meta">
          {meta.map((t, i) => (
            <span key={t}>{i > 0 && <span className="sep">· </span>}{t}</span>
          ))}
          {m.strengths.length > 0 && <StrengthChips tags={m.strengths} max={8} />}
        </div>
      </div>
    </label>
  );
}
