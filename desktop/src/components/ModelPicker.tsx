import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronRight, Plus, RefreshCw, Search } from "lucide-react";
import { api, type ModelOption, type ModelOptions, type Provider, type Tag } from "../api";
import { useData } from "../data";
import { Modal, useBusy, useConfirm } from "../ui";
import ModelUseTag from "./ModelUseTag";
import { StrengthChips, useStrengthTags } from "./Strengths";
import { tr, useI18n } from "../i18n";
import "../styles/models.css";

/** Module scope, so these use `tr` and stay reactive to the current language. */
const TIER_LABEL: Record<string, string> = { flagship: "Flagship", balanced: "Balanced", fast: "Fast" };
const tierLabel = (tier: string): string => tr(TIER_LABEL[tier] ?? tier);

/** 1000000 becomes "1M context", 128000 becomes "128K context". */
function fmtContext(n: number | null | undefined): string {
  if (!n || n <= 0) return "";
  const label = (v: string) => tr("{v} context", { v });
  if (n >= 1_000_000) return label(`${+(n / 1_000_000).toFixed(1)}M`);
  if (n >= 1000) return label(`${Math.round(n / 1000)}K`);
  return label(String(n));
}

/** For the "N new models" badge: newly seen and not yet added (an already added
 * new model needs no further picking). */
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
  /** Open with the "new only" filter on */
  focusNew?: boolean;
  onClose: () => void;
  /** Latest options, so the caller can refresh its "N new models" badge */
  onOptions?: (o: ModelOptions) => void;
}) {
  const { reload } = useData();
  const confirm = useConfirm();
  const { t } = useI18n();
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
    // Anything that became "added" no longer counts as selected
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
      const ok = await confirm(t("The provider has retired (or is retiring) this model ({reason}). Calls may fail after adding it. Add anyway?", { reason: m.retired_reason }), { okText: t("Add anyway"), danger: false });
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
      setManualMsg({ ok: true, text: t("Added {name}", { name }) });
      await reload();
      apply(await api.modelOptions(provider.id));
    } catch (e) {
      setManualMsg({ ok: false, text: (e as Error).message });
    }
  });

  const close = () => { if (!confirming.current) onClose(); };
  const fromLabel = opts ? t(opts.catalog_source === "shipped" ? "bundled" : "updated") : "";

  return (
    <Modal
      title={t("Pick models · {provider}", { provider: provider.name })}
      onClose={close}
      wide
      actions={
        <>
          {addErr && <span className="err mp-foot-note">{addErr}</span>}
          <button className="btn" onClick={close}>{t("Cancel")}</button>
          <button className="btn primary" disabled={sel.size === 0 || busy} onClick={addSelected}>{busy ? t("Adding…") : t("Add selected ({n})", { n: sel.size })}</button>
        </>
      }
    >
      <div className="mp-root">
        <p className="mp-sub">
          {opts && <b>{t("Model catalog {version} ({source})", { version: opts.catalog_version, source: fromLabel })}</b>}
          <span className="mp-note">
            {t("The catalog is a snapshot taken on one date, so it can lag behind the latest releases. To see what the provider offers right now, use \"Refresh live list\". Strength tags are inferred from the model family and name — they are not benchmark results.")}
          </span>
        </p>

        <div className="mp-bar">
          <div className="search-box">
            <Search size={14} />
            <input autoFocus placeholder={t("Search by name / id / description")} value={q} onChange={(e) => setQ(e.target.value)} aria-label={t("Search models")} />
          </div>
          <label className="check-inline"><input type="checkbox" checked={onlyNew} onChange={(e) => setOnlyNew(e.target.checked)} />{t("New only")}{opts && opts.new_count > 0 ? `(${opts.new_count})` : ""}</label>
          <button className="btn small" disabled={refreshing} onClick={refresh} title={t("Ask the provider what it offers right now (needs network access)")}>
            <RefreshCw size={13} className={refreshing ? "mp-spin" : ""} /> {refreshing ? t("Refreshing…") : t("Refresh live list")}
          </button>
          <button className="btn small ghost" disabled={!opts || opts.new_count === 0} onClick={markSeen} title={t("Clear every \"new\" badge")}>{t("Mark all as seen")}</button>
        </div>

        <div className="mp-filter">
          <span className="mp-filter-label">{t("Strengths (multiple allowed, all required):")}</span>
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
          {want.length > 0 && <button className="link small" onClick={() => setWant([])}>{t("Clear")}</button>}
        </div>

        {refreshErr ? (
          <div className="mp-status err-box" role="alert">
            {t("Refreshing the live list failed: {err}", { err: refreshErr })}
            <span className="mp-err-note">{t("Still showing the bundled catalog{suffix}; the \"new\" and \"gone\" badges need a live list to be determined.", { suffix: opts && opts.catalog_source !== "shipped" ? t(" (an updated version)") : "" })}</span>
          </div>
        ) : opts?.live_fetched_at ? (
          <div className="mp-status ok-text">{opts.new_count > 0
            ? t("Live list updated at {time}, {n} new models", { time: fmtClock(opts.live_fetched_at), n: opts.new_count })
            : t("Live list updated at {time}, no new models", { time: fmtClock(opts.live_fetched_at) })}</div>
        ) : opts ? (
          <div className="mp-status muted">{t("The live list has not been refreshed yet; showing the bundled catalog.")}</div>
        ) : null}

        <div className="mp-listbar">
          <button className="link" disabled={pickable.length === 0} onClick={toggleAll}>{t(allOn ? "Deselect all" : "Select all filtered")}</button>
          <span className="muted">
            {opts ? t(shown.length !== list.length ? "{total} models, showing {shown}" : "{total} models", { total: list.length, shown: shown.length }) : ""}
          </span>
        </div>

        <div className="mp-list" role="list" aria-label={t("Model list")}>
          {!opts && !loadErr && <div className="empty">{t("Loading…")}</div>}
          {loadErr && <div className="empty err">{loadErr}</div>}
          {shown.map((m) => (
            <ModelRow key={m.id} m={m} local={provider.is_local} checked={m.added || sel.has(m.id)} onToggle={() => void toggle(m)} />
          ))}
          {opts && shown.length === 0 && (
            <div className="empty">
              {list.length === 0 ? t("The catalog has no models for this provider; you can add one by hand below")
                : onlyNew && !q && want.length === 0 ? t("No new models. Use \"Refresh live list\" to see whether the provider released any")
                : t("No models match")}
            </div>
          )}
        </div>

        <div className="mp-manual">
          <button className={"mp-manual-toggle" + (manualOpen ? " open" : "")} aria-expanded={manualOpen} onClick={() => setManualOpen((v) => !v)}>
            <ChevronRight size={14} /> {t("Enter a model by hand (not in the catalog)")}
          </button>
          {manualOpen && (
            <div className="mp-manual-body">
              <div className="input-group">
                <input value={manual} onChange={(e) => setManual(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && void addManual()} placeholder={t("Model id, e.g. deepseek-chat / qwen-plus / gpt-4o-mini")} aria-label={t("Model id")} spellCheck={false} />
                <button className="btn" onClick={() => void addManual()} disabled={!manual.trim() || addingManual}><Plus size={15} /> {t("Add")}</button>
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
  const { t } = useI18n();
  const meta: string[] = [];
  const ctx = fmtContext(m.context);
  if (ctx) meta.push(ctx);
  if (m.tier) meta.push(tierLabel(m.tier));
  if (m.params) meta.push(m.params);
  if (m.size_gb) meta.push(fmtSize(m.size_gb));
  return (
    <label className={"mp-row" + (m.added ? " added" : "") + (checked && !m.added ? " checked" : "")} role="listitem">
      <input type="checkbox" checked={checked} disabled={m.added} onChange={onToggle} aria-label={t("Select {name}", { name: m.name })} />
      <div className="mp-body">
        <div className="mp-title">
          <span className="mp-name">{m.name}</span>
          <span className="mp-id">{m.id}</span>
          <span className="mp-badges">
            <ModelUseTag use={m.use} />
            {m.added && <span className="tag"><Check size={11} /> {t("Added")}</span>}
            {m.is_new && <span className="tag new">{t("New")}</span>}
            {m.preview && <span className="tag">{t("Preview")}</span>}
            {m.legacy && <span className="tag">{t("Legacy")}</span>}
            {m.retired_reason && <span className="tag mp-warn-tag">{t("Retired")}</span>}
            {m.gone && <span className="tag danger" title={t("This model was added, but the provider's live list no longer has it — most likely retired")}>{t("Gone from the provider list")}</span>}
            {!m.gone && !m.added && !local && m.live === false && <span className="tag" title={t("Not in the live list: the catalog may be stale, or this account cannot use it")}>{t("Not in the live list")}</span>}
            {local && m.installed === true && <span className="tag on">{t("Installed")}</span>}
            {local && m.installed === false && <span className="tag">{t("Not installed")}</span>}
          </span>
        </div>
        {m.summary && <div className="mp-summary">{m.summary}</div>}
        {m.retired_reason && <div className="mp-warn">{m.retired_reason}</div>}
        <div className="mp-meta">
          {meta.map((part, i) => (
            <span key={part}>{i > 0 && <span className="sep">· </span>}{part}</span>
          ))}
          {m.strengths.length > 0 && <StrengthChips tags={m.strengths} max={8} />}
        </div>
      </div>
    </label>
  );
}
