import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, Copy, Download, RefreshCw, Search, Server, Trash2 } from "lucide-react";
import { api, pullLocalModel, type LocalCandidate, type LocalCatalog, type LocalFamily, type LocalFit, type LocalModelRow, type UpdateItem } from "../api";
import { useData } from "../data";
import { tr, useI18n } from "../i18n";
import { useConfirm } from "../ui";
import { StrengthChips } from "../components/Strengths";
import { Callout, ExtLink, isHttps, Spin } from "../components/ExtBits";
import type { PageProps } from "./SettingsModal";
import "../styles/local.css";

const ACCEL: Record<string, string> = { metal: tr("Apple silicon (Metal acceleration)"), cuda: tr("NVIDIA (CUDA acceleration)"), none: tr("No GPU acceleration (CPU only)"), unknown: tr("Acceleration type unknown") };
const FIT_LABEL: Record<LocalFit, string> = { ok: tr("Plenty of memory"), tight: tr("Memory is tight"), no: tr("Not enough memory"), unknown: "" };
const SRC_LABEL: Record<string, string> = { ollama: tr("New Ollama model"), successor: tr("New generation"), hf: "Hugging Face", github: "GitHub", "ollama-release": tr("Ollama itself") };

const gb = (n: number) => (n >= 100 ? Math.round(n) : Math.round(n * 10) / 10) + " GB";
const asCand = (u: UpdateItem): LocalCandidate => u.detail as unknown as LocalCandidate;

const VLLM_CMD = `pip install vllm\nvllm serve "deepseek-ai/DeepSeek-V4-Flash"`;
const SGLANG_CMD = `pip install sglang\npython3 -m sglang.launch_server --model-path "deepseek-ai/DeepSeek-V4-Flash" --host 127.0.0.1 --port 30000`;

export default function LocalPage({ onTab }: PageProps) {
  const { t } = useI18n();
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
    } catch { /* Failing to read the reminders does not affect the main view */ }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  const ollama = providers.find((p) => p.kind === "ollama");
  const chain = settings?.route_chain ?? [];
  const fallbackId = chain.length ? chain[chain.length - 1] : "";

  const pull = async (tag: string, row?: { size_gb: number; fit: LocalFit; disk_ok: boolean; slow: boolean }) => {
    if (row) {
      const hw = cat?.hardware;
      const warns: string[] = [];
      if (row.fit === "no") warns.push(t("Needs about {size}, but this machine has only {ram} GB of memory — it most likely will not fit", { size: gb(row.size_gb), ram: hw?.ram_gb ?? "?" }));
      else if (row.fit === "tight") warns.push(t("Needs about {size} and memory is tight, so the machine may feel sluggish while it runs", { size: gb(row.size_gb) }));
      if (!row.disk_ok) warns.push(t("Only {disk} GB of disk is free — it will not fit", { disk: hw?.disk_free_gb ?? "?" }));
      if (row.slow) warns.push(t("This machine has no GPU acceleration, so a model this large will be very slow"));
      if (warns.length || row.size_gb >= 30) {
        const text = (warns.length ? warns.join("; ") : t("About {size} to download", { size: gb(row.size_gb) })) + " " + t("Download anyway?");
        if (!(await confirm(`${tag}: ${text}`, { okText: t("Download anyway"), danger: true }))) return;
      }
    }
    setBusy(tag);
    setProg((p) => ({ ...p, [tag]: t("Starting download…") }));
    const ok = await pullLocalModel(tag, (p) => {
      const m = p.error ? "✗ " + p.error : p.total && p.completed ? `${p.status} ${Math.round((p.completed / p.total) * 100)}%` : p.status ?? "";
      setProg((x) => ({ ...x, [tag]: m }));
    });
    setBusy(null);
    if (ok) {
      setProg((x) => ({ ...x, [tag]: t("✓ Download finished, added to the model list") }));
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
      const parts = [r.found ? t("Found {n} new open-source models/repositories", { n: r.found }) : t("No new open-source models found")];
      if (r.catalog_update?.applied) parts.push(t("The recommendation catalog was updated to {v}", { v: String(r.catalog_update.latest) }));
      if (r.errors.length) parts.push(t("{n} sources could not be checked", { n: r.errors.length }));
      setMsg({ ok: r.errors.length === 0, text: parts.join("; ") + "." });
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
      <h2 className="sp-title">{t("Local models")}</h2>
      <p className="sp-desc">
        {t("When the cloud is unavailable or outbound calls are off, routing falls back to the local models here and your data stays on this machine. Below, the latest open-source LLMs are listed by vendor, with a guess at which ones this machine can run based on its memory and disk (a guess, not a guarantee).")}
      </p>

      <div className="card">
        <div className="setting-row">
          <div>
            <div className="sr-title">
              <i className={"dot " + (cat == null ? "off" : cat.running ? "ok" : "bad")} /> Ollama {cat == null ? t("Checking…") : cat.running ? t("Running") : t("Not detected")}
            </div>
            <div className="sr-desc">
              {cat?.running ? t("Models are downloaded and run by Ollama") : t("Install and start Ollama, then click refresh on the right")}
              {hw && (
                <span className="lp-hw">
                  {t("This machine: {ram} memory · {disk} free disk · {accel}", { ram: hw.ram_gb != null ? `${hw.ram_gb} GB` : t("unknown"), disk: hw.disk_free_gb != null ? `${hw.disk_free_gb} GB` : t("unknown"), accel: ACCEL[hw.accel] })}
                  {hw.translated && t(" · The backend Python is the Intel build (translated by Rosetta); rebuilding .venv with an arm64 Python is recommended")}
                </span>
              )}
            </div>
          </div>
          <button className="btn small" onClick={refresh}><RefreshCw size={14} /> {t("Refresh")}</button>
        </div>
        {cat && !cat.running && (
          <div className="hint">
            macOS:<code>brew install ollama && ollama serve</code>{t("; on other systems download the installer from github.com/ollama/ollama. You can also run")} <code>scripts/setup_local_model.sh</code> {t("in the project to do it in one step.")}
          </div>
        )}
        {err && <div className="err small" style={{ marginTop: 8 }}>{err}</div>}
      </div>

      {/* Said here rather than nowhere: this page is an Ollama download list, and someone who has
          just cloned a video model will look for it exactly here. */}
      <div className="hint">
        {t("MiniMax H3 and other video-generation models are not on this list: they are diffusion pipelines with no chat endpoint, so nothing here could run them. Add one under Model providers (its kind is Video generation, served by SGLang or vLLM) and switch it on under Permissions & control → Video generation.")}
      </div>

      <div className="lp-bar">
        <h3 className="sec" style={{ margin: 0 }}>{t("Recommended models")}{cat && <span className="muted small lp-ver"> {t("catalog {v}", { v: cat.version })}{cat.source === "override" ? t(" (updated)") : ""}</span>}</h3>
        <button className="btn small" disabled={checking} onClick={check} title={t("Look for new open-source models and new versions on the Ollama registry, Hugging Face, and GitHub")}>
          {checking ? <><Spin size={12} /> {t("Checking…")}</> : <><RefreshCw size={13} /> {t("Check for new models")}</>}
        </button>
      </div>
      {msg && <div className={"small " + (msg.ok ? "ok-text" : "err")} role="status" style={{ marginBottom: 8 }}>{msg.text}</div>}
      {errList.length > 0 && (
        <details className="small muted lp-errs">
          <summary>{t("Show the sources that failed ({n})", { n: errList.length })}</summary>
          <ul>{errList.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </details>
      )}

      {cands.length > 0 && (
        <div className="lp-cands" aria-label={t("Newly discovered models")}>
          <div className="lp-cands-title">{t("Found {n} new open-source model updates", { n: cands.length })}</div>
          {cands.map((u) => (
            <CandRow key={u.id} item={u} onDone={async (note) => { if (note) setMsg({ ok: true, text: note }); await Promise.all([refresh(), reloadUpdates()]); }} />
          ))}
          <div className="muted small">
            {t("Add to recommendations only puts the model (and the size read from the Ollama registry) into the list below — nothing is downloaded. GitHub usually has code only; the open weights mostly live on Hugging Face and Ollama.")}
          </div>
        </div>
      )}

      <div className="lp-tools">
        <div className="lp-search">
          <Search size={14} />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("Search for a vendor or model, e.g. gemma, deepseek, 30b")} aria-label={t("Search models")} />
        </div>
        <label className={"check lp-only" + (onlyFit ? " on" : "")} title={t("Hide models too large for this machine's memory or disk (already downloaded ones always show)")}>
          <input type="checkbox" checked={onlyFit} onChange={(e) => setOnlyFit(e.target.checked)} />{t("Only show what this machine can run")}
        </label>
      </div>
      {onlyFit && view.hidden > 0 && (
        <div className="muted small lp-hidden">{t("Hid {n} models that are too large for this machine's memory or disk; untick the box to see everything.", { n: view.hidden })}</div>
      )}

      {cat == null && !err && <div className="empty"><Spin size={14} /> {t("Loading…")}</div>}
      {cat && view.fams.length === 0 && <div className="empty">{t("No models match")}{onlyFit ? t(" (try unticking Only show what this machine can run)") : ""}</div>}

      <div className="lp-fams">
        {view.fams.map((f) => (
          <section key={f.id} className="lp-fam" aria-label={f.name}>
            <div className="lp-fam-head">
              <span className="tag lp-vendor">{f.vendor}</span>
              <b>{f.name}</b>
              {f.license && <span className="tag" title={t("The release page of the model is authoritative for the licence")}>{f.license}</span>}
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
          <summary>{t("A few more open-source models are cloud-only on Ollama ({list})", { list: cat.cloud_only.map((c) => c.name).join(", ") })}</summary>
          <ul>
            {cat.cloud_only.map((c) => <li key={c.name}><b>{c.name}</b>({c.vendor}):{c.note}</li>)}
          </ul>
          <div className="muted small">{t("The cloud versions need a network connection and an Ollama account, so they are not data-stays-on-this-machine. To run one locally, download the weights and host the service yourself (see the self-hosted service card above).")}</div>
        </details>
      )}

      <h3 className="sec">{t("Other models")}</h3>
      <div className="input-group">
        <input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder={t("Any Ollama model name, e.g. qwen3.8:27b or llama4:16x17b")} aria-label={t("Model name")} />
        <button className="btn" disabled={!cat?.running || !custom.trim() || busy !== null} onClick={() => pull(custom.trim())}><Download size={15} /> {t("Download")}</button>
      </div>
      {custom.trim() && prog[custom.trim()] && <div className={"small " + (prog[custom.trim()].startsWith("✗") ? "err" : "muted")} style={{ marginTop: 6 }}>{prog[custom.trim()]}</div>}

      {cat?.running && (
        <>
          <h3 className="sec">{t("Downloaded")}</h3>
          {installed.length === 0 && <div className="muted small">{t("None yet")}</div>}
          <div className="lp-installed">
            {installed.map((n) => {
              const isFb = !!ollama && fallbackId === `${ollama.id}/${n}`;
              return (
                <div key={n} className="lp-inst">
                  <code>{n}</code>
                  {isFb ? <span className="tag on">{t("Current fallback")}</span> : <button className="btn small" disabled={!ollama} onClick={() => setFallback(n)}>{t("Set as fallback")}</button>}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ One model row
function ModelRow({ m, installed, isFallback, busy, progress, canFallback, onPull, onFallback, onRemove }: {
  m: LocalModelRow; installed: boolean; isFallback: boolean; busy: string | null; progress?: string; canFallback: boolean;
  onPull: () => void; onFallback: () => void; onRemove?: () => void;
}) {
  const { t } = useI18n();
  const bad = m.fit === "no" || !m.disk_ok;
  return (
    <div className="lp-row">
      <div className="lp-row-main">
        <code className="lp-tag">{m.tag}</code>
        <span className="lp-size">{m.size_gb ? gb(m.size_gb) : t("Size unknown")}</span>
        {m.ctx && <span className="muted small">{t("Context {n}", { n: String(m.ctx) })}</span>}
        {FIT_LABEL[m.fit] && <span className={"tag lp-fit " + m.fit}>{FIT_LABEL[m.fit]}</span>}
        {!m.disk_ok && <span className="tag lp-fit no">{t("Not enough disk")}</span>}
        {m.slow && !bad && <span className="tag lp-fit tight" title={t("Without GPU acceleration, larger models generate very slowly")}>{t("Will be slow")}</span>}
        {m.note && <span className="muted small lp-note-txt">{m.note}</span>}
      </div>
      <div className="lp-row-act">
        {installed ? (
          <>
            <span className="ok-text lc-done"><CheckCircle2 size={14} /> {t("Downloaded")}</span>
            {isFallback ? <span className="tag on">{t("Current fallback")}</span> : <button className="btn small" disabled={!canFallback} onClick={onFallback}>{t("Set as fallback")}</button>}
          </>
        ) : (
          <button className={"btn small" + (bad ? "" : " primary")} disabled={busy !== null} onClick={onPull}>
            {busy === m.tag ? <><Spin size={12} /> {t("Downloading")}</> : <><Download size={13} /> {t("Download")}</>}
          </button>
        )}
        {onRemove && <button className="icon-btn tiny" aria-label={t("Remove {tag} from the recommendation list", { tag: m.tag })} title={t("Remove from the recommendation list")} onClick={onRemove}><Trash2 size={13} /></button>}
      </div>
      {progress && <div className={"small lp-prog " + (progress.startsWith("✗") ? "err" : "muted")}>{progress}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ Newly discovered models
function CandRow({ item, onDone }: { item: UpdateItem; onDone: (t?: string) => Promise<void> }) {
  const { t } = useI18n();
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
          {c.size_gb ? <span className="muted small"> · {t("about {size}", { size: gb(c.size_gb) })}</span> : null}
          {c.replaces && <span className="muted small"> · {t("successor to {name}", { name: String(c.replaces) })}</span>}
          {c.license && <span className="muted small"> · {c.license}</span>}
          {typeof c.stars === "number" && c.stars > 0 && <span className="muted small"> · ★ {c.stars}</span>}
        </div>
        {c.source === "ollama-release" ? (
          <div className="muted small">{t("Local Ollama {local}; latest on GitHub {latest}. New models often need a newer Ollama, and the app never upgrades it — install it yourself.", { local: String(c.local || "?"), latest: String(c.latest ?? "") })}</div>
        ) : (
          c.desc && <div className="muted small">{c.desc}</div>
        )}
        {err && <div className="err small">{err}</div>}
      </div>
      <div className="lp-cand-act">
        {c.tag && <button className="btn small primary" disabled={busy} onClick={() => run(async () => { await api.localAdd(c.tag as string); return t("Added {tag} to the recommendation list.", { tag: String(c.tag) }); })}>{t("Add to recommendations")}</button>}
        {isHttps(c.url) && <ExtLink href={c.url} className="btn small">{t("Open the page")}</ExtLink>}
        <button className="btn small ghost" disabled={busy} onClick={() => run(async () => { await api.dismissUpdate(item.id); })}>{t("Ignore")}</button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ Self-hosted DeepSeek service
function SelfHost({ fam, hasProvider, onTab, onAdded }: {
  fam: LocalCatalog["selfhost"][number]; hasProvider: boolean; onTab: PageProps["onTab"]; onAdded: () => Promise<void>;
}) {
  const { t } = useI18n();
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
    } catch { /* When the clipboard is unavailable the user can select the text by hand */ }
  };
  return (
    <section className="lp-self" aria-label={t("Self-hosted {name} service", { name: fam.name })}>
      <h3 className="sec"><Server size={15} /> {t("Self-hosted {name} service", { name: fam.name })}</h3>
      <div className="muted small lp-fam-desc">{fam.desc}</div>
      {fam.strengths.length > 0 && <StrengthChips tags={fam.strengths} max={6} />}
      <div className="lp-rows">
        {fam.models.map((m) => (
          <div key={m.id} className="lp-row">
            <div className="lp-row-main">
              <code className="lp-tag">{m.id}</code>
              <span className="muted small">{m.params}</span>
              {m.size_gb > 0 && <span className="lp-size">{t("About {size} at 4-bit quantization", { size: gb(m.size_gb) })}</span>}
              {m.size_gb > 0 && FIT_LABEL[m.fit] && <span className={"tag lp-fit " + m.fit}>{FIT_LABEL[m.fit]}</span>}
              <span className="muted small lp-note-txt">{m.note}</span>
            </div>
          </div>
        ))}
      </div>
      <Callout tone="warn" title={t("To be clear up front")}>
        {t("Models like this do not run on an ordinary computer with one click: the official weights need several high-end GPUs, or a community quantization (Unsloth's GGUF, for example — about 155 GB of memory at 4-bit) together with llama.cpp.")}
        {t("This only wires a service you have already started into the group chat. The weights are published on Hugging Face (licence: {lic}), and there is no matching model repository on GitHub; the previous generation, V3, lives at github.com/deepseek-ai/DeepSeek-V3.", { lic: fam.license || t("see the release page") })}
      </Callout>
      <div className="lp-cmds">
        {([["vLLM (default port 8000; use http://127.0.0.1:8000/v1 as the address)", VLLM_CMD, "vllm"], ["SGLang (port 30000; the default address is preset)", SGLANG_CMD, "sglang"]] as const).map(([title, cmd, key]) => (
          <div key={key} className="lp-cmd">
            <div className="lp-cmd-head"><span className="small">{title}</span>
              <button className="btn small ghost" onClick={() => copy(key, cmd)} aria-label={t("Copy the {key} command", { key })}><Copy size={12} /> {copied === key ? t("Copied") : t("Copy")}</button>
            </div>
            <pre>{cmd}</pre>
          </div>
        ))}
        <div className="muted small">{t("The commands come from the model's release notes and have not been verified on your machine. In the SGLang command,")} <code>--host 127.0.0.1</code> {t("keeps the service bound to this machine — do not change it to 0.0.0.0, or anyone on the same network could call it.")}</div>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        {hasProvider
          ? <button className="btn" onClick={() => onTab("providers")}>{t("Open Model services to change the address and model")}</button>
          : <button className="btn primary" disabled={busy} onClick={add}>{busy ? <><Spin size={12} /> {t("Adding")}</> : t("Add as a local provider")}</button>}
        {err && <span className="err small">{err}</span>}
      </div>
    </section>
  );
}
