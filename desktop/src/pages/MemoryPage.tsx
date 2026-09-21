import { useCallback, useEffect, useState } from "react";
import { Eraser, LoaderCircle, Pencil, Pin, Plus, Search, Trash2, X } from "lucide-react";
import { api, relTime, type Memory, type MemoryKind, type MemoryScope, type Settings } from "../api";
import { useData } from "../data";
import { tr, useI18n } from "../i18n";
import { Modal, Switch, useConfirm, useOutside } from "../ui";
import ObsidianCard from "../components/ObsidianCard";
import "../styles/know.css";

const MAX = 500;
// English keys, translated where they are shown: tr() in a module-level table is evaluated once, while
// the module loads, and would freeze this page in whatever language was current at import time.
const KINDS: { id: MemoryKind; label: string; hint: string }[] = [
  { id: "preference", label: "Preference", hint: "Your habits, tone, and formatting requirements" },
  { id: "fact", label: "Fact", hint: "Objective details such as projects, people, and terms" },
  { id: "decision", label: "Decision", hint: "Choices that have already been settled" },
  { id: "lesson", label: "Lesson", hint: "Pitfalls you hit, and approaches that worked or did not" },
  { id: "action", label: "Action", hint: "Steps for handling multi-step tasks" },
];
const KIND_LABEL = Object.fromEntries(KINDS.map((k) => [k.id, k.label])) as Record<string, string>;
const SOURCE_LABEL: Record<string, string> = { manual: "Added manually", auto: "Auto-extracted", action: "Action log", obsidian: "From Obsidian" };
/** Action logs: the backend stores multi-step task procedures as kind=action with source=auto (not source=action), so accept both shapes here. */
const isActionLog = (m: Memory) => m.source === "action" || (m.kind === "action" && m.source !== "manual");
const sourceLabel = (m: Memory) => tr(isActionLog(m) ? SOURCE_LABEL.action : (SOURCE_LABEL[m.source] ?? m.source));
type ScopeTab = "all" | MemoryScope;
const SCOPE_TABS: { id: ScopeTab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "global", label: "Global" },
  { id: "group", label: "Group" },
  { id: "agent", label: "Member" },
];
const LIST_LIMIT = 500; // The backend returns at most this many per call

function scopeText(m: Memory): string {
  if (m.scope === "global") return tr("Global");
  if (m.scope === "group") return `${tr("Group")} · ${m.scope_name || tr("Deleted group")}`;
  return `${tr("Member")} · ${m.scope_name || tr("Deleted member")}`;
}

export default function MemoryPage() {
  const { t } = useI18n();
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
  const [tick, setTick] = useState(0); // Incrementing this reloads the list
  const refresh = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    const timer = window.setTimeout(() => setQ(qInput.trim()), 300);
    return () => window.clearTimeout(timer);
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
    s === "global" ? tr("Global") : s === "group" ? `${tr("Group")} · ${groups.find((g) => g.id === id)?.name ?? "?"}` : `${tr("Member")} · ${agents.find((a) => a.id === id)?.name ?? "?"}`;

  // ---- Settings toggles
  const setFlag = async (k: "memory_enabled" | "memory_auto_extract", v: boolean) => {
    setActErr("");
    try {
      await api.putSettings({ [k]: v } as Partial<Settings>);
      await reload();
    } catch (e) {
      setActErr((e as Error).message);
    }
  };

  // ---- Single-item actions
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
    const preview = m.content.length > 60 ? m.content.slice(0, 60) + "…" : m.content;
    if (!(await confirm(t("Delete this memory: {preview}. This cannot be undone.", { preview }), { okText: t("Delete") }))) return;
    await act(() => api.delMemory(m.id));
  };

  // ---- Cleanup
  const countText = (n: number) => (n >= LIST_LIMIT ? tr("{n}+ items", { n: LIST_LIMIT }) : tr("{n} items", { n }));
  const clearBySource = async (source: "auto" | "action") => {
    setActErr("");
    try {
      // Action logs: filter by kind=action (excluding manual ones) and delete them one by one. Auto-extracted: clear by source=auto in bulk.
      const all = await api.memories(source === "action" ? { kind: "action" } : {});
      const list = all.memories.filter((m) => (source === "action" ? isActionLog(m) : m.source === "auto"));
      const n = list.length;
      if (n === 0) return setActErr(source === "auto" ? t("There are no auto-extracted memories to clear") : t("There are no action logs to clear"));
      const what = source === "auto" ? t("auto-extracted memories (preferences, decisions, lessons, and action logs from multi-step tasks)") : t("action logs (procedures from multi-step tasks)");
      if (!(await confirm(t("This will delete all {what} — {count}. Memories you added manually are unaffected. This cannot be undone.", { what, count: countText(n) }), { okText: t("Clear") }))) return;
      let done = 0;
      if (source === "auto") {
        done = (await api.clearMemories({ source: "auto" })).deleted;
      } else {
        const rs = await Promise.allSettled(list.map((m) => api.delMemory(m.id)));
        done = rs.filter((r) => r.status === "fulfilled").length;
        const bad = rs.find((r): r is PromiseRejectedResult => r.status === "rejected");
        if (bad) setActErr(t("{n} could not be deleted: {reason}", { n: n - done, reason: (bad.reason as Error).message }));
      }
      refresh();
      setNotice(t("Cleared {n} items", { n: done }));
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
      if (n === 0) return setActErr(t("There are no memories in this scope"));
      if (!(await confirm(t("This will delete every memory in {scope} — including manually added and pinned ones. {count}. This cannot be undone.", { scope: scopeNameFor(tab, sid), count: countText(n) }), { okText: t("Clear") }))) return;
      const r = await api.clearMemories({ scope: tab, scope_id: sid || undefined });
      refresh();
      setNotice(t("Cleared {n} items", { n: r.deleted }));
    } catch (e) {
      setActErr((e as Error).message);
    }
  };
  const [notice, setNotice] = useState("");
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 3500);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const scopeOptions = tab === "group" ? groups.map((g) => ({ id: g.id, name: g.name })) : tab === "agent" ? agents.map((a) => ({ id: a.id, name: `${a.avatar} ${a.name}` })) : [];
  const canClearScope = tab === "global" || ((tab === "group" || tab === "agent") && !!scopeId);
  const enabled = settings?.memory_enabled ?? true;

  return (
    <div className="kn-page">
      <div className="kn-inner">
        <div className="kn-head">
          <div className="kn-head-main">
            <h1>{t("Memory")}</h1>
            <p className="kn-desc">
              {t("Memory makes members more targeted the more you use them: when a chat ends, a cheaper model auto-extracts your preferences, decisions, and lessons, and records how multi-step tasks were handled. They are recalled and injected into the members' prompts the next time a similar task comes up. Sensitive content such as passwords, keys, and long digit strings is never recorded automatically. All memories live only on this machine, and you can edit or delete them at any time.")}
            </p>
          </div>
          <div className="kn-head-actions">
            <ClearMenu
              canScope={canClearScope}
              onAuto={() => void clearBySource("auto")}
              onAction={() => void clearBySource("action")}
              onScope={() => void clearScope()}
            />
            <button className="btn primary" onClick={() => setAddOpen(true)}><Plus size={15} /> {t("Add memory")}</button>
          </div>
        </div>

        <div className="card flush kn-switches">
          <div className="setting-row pad">
            <div>
              <div className="sr-title">{t("Enable memory")}</div>
              <div className="sr-desc">{t("Master switch. When off, memories are neither recalled nor recorded; existing ones are kept. Each group can also turn this off separately under Extensions.")}</div>
            </div>
            <Switch checked={enabled} onChange={(v) => void setFlag("memory_enabled", v)} disabled={!settings} label={t("Enable memory")} />
          </div>
          <div className="setting-row pad">
            <div>
              <div className="sr-title">{t("Auto-extract when a chat ends")}</div>
              <div className="sr-desc">{t("Summarizes the conversation with a cheaper model, which uses a little quota. When off, preferences, decisions, and lessons are no longer auto-extracted (multi-step task procedures are still recorded), and you can still add memories by hand.")}</div>
            </div>
            <Switch checked={enabled && (settings?.memory_auto_extract ?? true)} onChange={(v) => void setFlag("memory_auto_extract", v)} disabled={!settings || !enabled} label={t("Auto-extract")} />
          </div>
        </div>

        <ObsidianCard onSynced={refresh} />

        <div className="kn-filters">
          <div className="seg" role="tablist" aria-label={t("Memory scope")}>
            {SCOPE_TABS.map((s) => (
              <button key={s.id} role="tab" aria-selected={tab === s.id} className={tab === s.id ? "on" : ""} onClick={() => { setTab(s.id); setScopeId(""); }}>
                {t(s.label)}
              </button>
            ))}
          </div>
          {(tab === "group" || tab === "agent") && (
            <select className="kn-select" value={scopeId} onChange={(e) => setScopeId(e.target.value)} aria-label={tab === "group" ? t("Select a group") : t("Select a member")}>
              <option value="">{tab === "group" ? t("All groups") : t("All members")}</option>
              {scopeOptions.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
            </select>
          )}
          <select className="kn-select" value={kind} onChange={(e) => setKind(e.target.value as "" | MemoryKind)} aria-label={t("Filter by type")}>
            <option value="">{t("All types")}</option>
            {KINDS.map((k) => <option key={k.id} value={k.id}>{t(k.label)}</option>)}
          </select>
          <span className="grow" />
          <label className="search-box kn-search-sm">
            <Search size={15} />
            <input value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder={t("Search memory content")} aria-label={t("Search memory content")} />
            {qInput && <button className="icon-btn tiny" aria-label={t("Clear search")} title={t("Clear")} onClick={() => setQInput("")}><X size={13} /></button>}
          </label>
        </div>

        {notice && <div className="ok-text kn-block">{notice}</div>}
        {actErr && <div className="err kn-block">{actErr}</div>}
        {err && <div className="err kn-block">{err}</div>}
        {loading && <div className="empty"><LoaderCircle size={16} className="kn-spin" /> {t("Loading…")}</div>}

        {!loading && !err && items.length === 0 && (
          <div className="kn-empty-box">
            {filtered ? (
              <p><b>{t("No memories match.")}</b>{t("Try a different scope, type, or keyword.")}</p>
            ) : (
              <>
                <p><b>{t("No memories yet.")}</b>{t("Memories come from two sources:")}</p>
                <ul>
                  <li><b>{t("Auto-extraction")}</b>{t(": turn on Auto-extract when a chat ends above, and it will record preferences, decisions, lessons, and multi-step task procedures once a chat finishes.")}</li>
                  <li><b>{t("Added manually")}</b>{t(": write down anything you want the members to remember, such as tone, formatting rules, or fixed wording.")}</li>
                </ul>
                <button className="btn small" onClick={() => setAddOpen(true)}><Plus size={14} /> {t("Add memory")}</button>
              </>
            )}
          </div>
        )}

        {items.length > 0 && (
          <>
            <div className="kn-count muted small">
              {t("Total {n}{more} items · Pinned memories are always included; the rest are recalled by relevance to the current task.", { n: items.length, more: items.length >= LIST_LIMIT ? "+" : "" })}
            </div>
            <div className="kn-mems">
              {items.map((m) =>
                editing === m.id ? (
                  <EditCard key={m.id} m={m} onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); refresh(); }} />
                ) : (
                  <article key={m.id} className={"kn-mem" + (m.pinned ? " pinned" : "")}>
                    <div className="kn-mem-top">
                      <span className={"tag kn-kind " + m.kind} title={t(KINDS.find((k) => k.id === m.kind)?.hint ?? "")}>{t(KIND_LABEL[m.kind] ?? m.kind)}</span>
                      <span className="kn-mem-scope">{scopeText(m)}</span>
                      <span className="grow" />
                      <button
                        className={"icon-btn tiny" + (m.pinned ? " on" : "")}
                        aria-label={m.pinned ? t("Unpin") : t("Pin")}
                        aria-pressed={m.pinned}
                        title={m.pinned ? t("Unpin") : t("Pin: included in every prompt")}
                        onClick={() => void act(() => api.patchMemory(m.id, { pinned: !m.pinned }))}
                      >
                        <Pin size={14} fill={m.pinned ? "currentColor" : "none"} />
                      </button>
                      <button className="icon-btn tiny" aria-label={t("Edit")} title={t("Edit")} onClick={() => setEditing(m.id)}><Pencil size={14} /></button>
                      <button className="icon-btn tiny kn-del" aria-label={t("Delete")} title={t("Delete")} onClick={() => void remove(m)}><Trash2 size={14} /></button>
                    </div>
                    <p className="kn-mem-text">{m.content}</p>
                    <div className="kn-mem-meta">
                      {m.pinned && <span className="tag on"><Pin size={10} /> {t("Pinned")}</span>}
                      <span>{t("Source: {source}", { source: sourceLabel(m) })}</span>
                      <span>{t("Matched {n} times", { n: m.hits })}</span>
                      <span>{t("Updated {time}", { time: relTime(m.updated_at) })}</span>
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
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const ref = useOutside<HTMLDivElement>(open, () => setOpen(false));
  const item = (label: string, fn: () => void, disabled?: boolean, title?: string) => (
    <button role="menuitem" className="kn-menu-item" disabled={disabled} title={title} onClick={() => { setOpen(false); fn(); }}>{label}</button>
  );
  return (
    <div className="kn-menu-wrap" ref={ref}>
      <button className="btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}><Eraser size={15} /> {t("Clean up")}</button>
      {open && (
        <div className="kn-menu" role="menu">
          {item(t("Clear auto-extracted memories"), onAuto)}
          {item(t("Clear action logs"), onAction)}
          {item(t("Clear the current filter scope"), onScope, !canScope, canScope ? undefined : t("First select Global below, or a specific group / member"))}
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------------- inline edit
function EditCard({ m, onCancel, onSaved }: { m: Memory; onCancel: () => void; onSaved: () => void }) {
  const { t } = useI18n();
  const [text, setText] = useState(m.content);
  const [kind, setKind] = useState<MemoryKind>(m.kind);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const over = text.length > MAX;
  const save = async () => {
    if (!text.trim()) return setErr(t("Content cannot be empty"));
    if (over) return setErr(t("Content cannot exceed {n} characters", { n: MAX }));
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
        aria-label={t("Memory content")}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void save(); }}
      />
      <div className="kn-edit-bar">
        <select className="kn-select" value={kind} onChange={(e) => setKind(e.target.value as MemoryKind)} aria-label={t("Type")}>
          {KINDS.map((k) => <option key={k.id} value={k.id}>{t(k.label)}</option>)}
        </select>
        <span className={"small " + (over ? "err" : "muted")}>{text.length} / {MAX}</span>
        {err && <span className="err small">{err}</span>}
        <span className="grow" />
        <button className="btn small" onClick={onCancel}>{t("Cancel")}</button>
        <button className="btn small primary" disabled={busy} onClick={save}>{busy ? t("Saving…") : t("Save")}</button>
      </div>
    </article>
  );
}

// ---------------------------------------------------------------- add modal
function AddModal({ initial, onClose, onSaved }: { initial: { tab: ScopeTab; scopeId: string }; onClose: () => void; onSaved: () => void }) {
  const { t } = useI18n();
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
    if (!content.trim()) return setErr(t("Please enter the memory content"));
    if (over) return setErr(t("Content cannot exceed {max} characters (currently {n})", { max: MAX, n: content.length }));
    if (scope !== "global" && !scopeId) return setErr(scope === "group" ? t("Please select a group") : t("Please select a member"));
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
      title={t("Add memory")}
      wide
      onClose={onClose}
      actions={
        <>
          {err && <span className="err" style={{ marginRight: "auto" }}>{err}</span>}
          <button className="btn" onClick={onClose}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy} onClick={save}>{busy ? t("Saving…") : t("Add")}</button>
        </>
      }
    >
      <label className="field">
        <span>{t("Content (one memory per item — the more specific, the more useful)")}</span>
        <textarea rows={4} autoFocus value={content} onChange={(e) => setContent(e.target.value)} placeholder={t("e.g. Use a formal tone in externally published copy")} />
        <span className={"small kn-counter " + (over ? "err" : "muted")}>{content.length} / {MAX}</span>
      </label>
      <div className="field">
        <span>{t("Scope")}</span>
        <div className="row kn-scope-row">
          <div className="seg" role="radiogroup" aria-label={t("Memory scope")}>
            {(["global", "group", "agent"] as MemoryScope[]).map((s) => (
              <button key={s} role="radio" aria-checked={scope === s} className={scope === s ? "on" : ""} onClick={() => { setScope(s); setScopeId(""); setErr(""); }}>
                {s === "global" ? t("Global") : s === "group" ? t("Group") : t("Member")}
              </button>
            ))}
          </div>
          {scope !== "global" && (
            <select value={scopeId} onChange={(e) => { setScopeId(e.target.value); setErr(""); }} aria-label={scope === "group" ? t("Select a group") : t("Select a member")}>
              <option value="">{scope === "group" ? t("Select a group…") : t("Select a member…")}</option>
              {opts.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
            </select>
          )}
        </div>
        <span className="muted small">{t("Global: any group can use it; Group: only that group; Member: only when that member speaks.")}</span>
      </div>
      <div className="field">
        <span>{t("Type")}</span>
        <div className="kn-kinds" role="radiogroup" aria-label={t("Memory type")}>
          {KINDS.map((k) => (
            <button key={k.id} role="radio" aria-checked={kind === k.id} className={"kn-kind-pick" + (kind === k.id ? " on" : "")} onClick={() => setKind(k.id)} title={t(k.hint)}>
              {k.label}
            </button>
          ))}
        </div>
        <span className="muted small">{t(KINDS.find((k) => k.id === kind)?.hint ?? "")}</span>
      </div>
      <label className="check-inline" style={{ marginBottom: 6 }}>
        <input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} />
        {t("Pinned (included in every prompt; keep this list short)")}
      </label>
    </Modal>
  );
}
