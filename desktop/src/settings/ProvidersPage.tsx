import { useEffect, useMemo, useRef, useState } from "react";
import { Eye, EyeOff, Plus, Search, Tags, Trash2, X } from "lucide-react";
import { api, type Model, type Preset, type Provider, type Tag } from "../api";
import { useData } from "../data";
import { currentLang, pickLang, useI18n } from "../i18n";
import { LetterIcon, Modal, Switch, useBusy, useConfirm, useFlash } from "../ui";
import { ModelPicker, pendingNew } from "../components/ModelPicker";
import { StrengthChips, StrengthPicker } from "../components/Strengths";
import { HealthDot } from "../components/Health";
import "../styles/models.css";

const KIND_LABEL: Record<string, string> = {
  deepseek: "DeepSeek",
  openai_compatible: "OpenAI-compatible",
  anthropic: "Anthropic",
  gemini: "Gemini",
  ollama: "Ollama",
  minimax_video: "Video generation",
};
/** Providers that generate media instead of chatting: no model list, no chat endpoint. The
 *  backend keeps them out of the model roster too (`store.list_models`). */
const MEDIA_KINDS = new Set(["minimax_video"]);
const isMedia = (kind: string) => MEDIA_KINDS.has(kind);
// Only the kinds whose wording actually differs need a Chinese entry; the rest are proper
// nouns. Read through a plain function (`currentLang()`), not a hook, because AddProvider
// renders it inside a callback.
const KIND_LABEL_ZH: Record<string, string> = { openai_compatible: "OpenAI 兼容", minimax_video: "视频生成" };  // i18n-keep: the Chinese half of the pair table above

const kindLabel = (kind: string): string =>
  pickLang(KIND_LABEL[kind] ?? kind, KIND_LABEL_ZH[kind], currentLang());

const DEFAULT_BASE: Record<string, string> = {
  deepseek: "https://api.deepseek.com",
  anthropic: "https://api.anthropic.com",
  gemini: "https://generativelanguage.googleapis.com",
};

/** Preview the real request URL (the way Cherry Studio does, so the user can confirm the address is right). */
function endpointPreview(kind: string, base: string): string {
  const b = (base || DEFAULT_BASE[kind] || "").replace(/\/+$/, "");
  if (!b) return "";
  if (kind === "openai_compatible" || kind === "deepseek") return `${b}/chat/completions`;
  if (kind === "anthropic") return `${b}/v1/messages`;
  if (kind === "ollama") return `${b}/api/chat`;
  if (kind === "minimax_video") return `${b}/v1/videos`;
  return b;
}

export default function ProvidersPage() {
  const { t } = useI18n();
  const { providers, reload } = useData();
  const [sel, setSel] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [adding, setAdding] = useState(false);
  const [newCounts, setNewCounts] = useState<Record<string, number>>({});
  /** Clicked "N new models" in the list: select that provider and open the picker straight away, showing new models only */
  const [openNew, setOpenNew] = useState<{ pid: string; n: number } | null>(null);
  const cur = providers.find((p) => p.id === sel) ?? providers[0] ?? null;
  const list = useMemo(() => providers.filter((p) => p.name.toLowerCase().includes(q.trim().toLowerCase())), [providers, q]);

  // "N new models": computed locally (no network) and only for enabled providers
  useEffect(() => {
    let alive = true;
    const on = providers.filter((p) => p.enabled);
    Promise.all(on.map((p) => api.modelOptions(p.id).then((o) => [p.id, pendingNew(o)] as const).catch(() => [p.id, 0] as const))).then((r) => {
      if (alive) setNewCounts(Object.fromEntries(r));
    });
    return () => { alive = false; };
  }, [providers]);

  return (
    <div className="providers">
      <div className="prov-list">
        <div className="search-box">
          <Search size={14} />
          <input placeholder={t("Search model platforms…")} value={q} onChange={(e) => setQ(e.target.value)} aria-label={t("Search model platforms")} />
        </div>
        <div className="prov-items">
          {list.map((p) => (
            <button key={p.id} className={"list-item" + (cur?.id === p.id ? " on" : "")} onClick={() => setSel(p.id)}>
              <LetterIcon name={p.name} />
              <span className="li-main">
                <span className="li-name">{p.name}</span>
                {p.enabled && (newCounts[p.id] ?? 0) > 0 && (
                  <span className="li-sub">
                    <span
                      className="tag new mp-new-btn"
                      title={t("Click to see the new models")}
                      onClick={(e) => { e.stopPropagation(); setSel(p.id); setOpenNew({ pid: p.id, n: Date.now() }); }}
                    >
                      {t("{n} new models", { n: newCounts[p.id] })}
                    </span>
                  </span>
                )}
              </span>
              {p.is_local && <span className="tag">{t("Local")}</span>}
              {p.enabled && <span className="tag on">ON</span>}
            </button>
          ))}
          {list.length === 0 && <div className="side-empty">{t("No matching providers")}</div>}
        </div>
        <button className="btn add-prov" onClick={() => setAdding(true)}><Plus size={15} /> {t("Add a provider")}</button>
      </div>
      <div className="prov-detail">
        {cur ? (
          <ProviderDetail
            key={cur.id}
            p={cur}
            newCount={newCounts[cur.id] ?? 0}
            onNewCount={(n) => setNewCounts((c) => ({ ...c, [cur.id]: n }))}
            openNewReq={openNew?.pid === cur.id ? openNew.n : 0}
            onOpenNewHandled={() => setOpenNew(null)}
            onChanged={reload}
            onDeleted={() => { setSel(null); void reload(); }} />
        ) : (
          <div className="empty big">{t("Start by clicking Add a provider at the bottom left")}</div>
        )}
      </div>
      {adding && <AddProvider onClose={() => setAdding(false)} onAdded={async (id) => { setAdding(false); await reload(); setSel(id); }} />}
    </div>
  );
}

function ProviderDetail({
  p,
  newCount,
  onNewCount,
  openNewReq,
  onOpenNewHandled,
  onChanged,
  onDeleted,
}: {
  p: Provider;
  newCount: number;
  onNewCount: (n: number) => void;
  openNewReq: number;
  onOpenNewHandled: () => void;
  onChanged: () => Promise<void>;
  onDeleted: () => void;
}) {
  const { t } = useI18n();
  const confirm = useConfirm();
  const { health } = useData();
  const [key, setKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [base, setBase] = useState(p.base_url);
  const [picker, setPicker] = useState<{ onlyNew: boolean } | null>(null);
  const [editStr, setEditStr] = useState<Model | null>(null);
  const [tests, setTests] = useState<Record<string, string>>({});
  const [check, setCheck] = useState("");
  const [err, setErr] = useState("");
  const [saved, flash] = useFlash();
  const keyRef = useRef(key);
  keyRef.current = key;
  useEffect(() => {
    if (!openNewReq) return;
    setPicker({ onlyNew: true });
    onOpenNewHandled();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openNewReq]);

  const save = async () => {
    const body: Record<string, unknown> = {};
    if (base !== p.base_url) body.base_url = base;
    if (keyRef.current) body.api_key = keyRef.current;
    if (!Object.keys(body).length) return true;
    setErr("");
    try {
      await api.patchProvider(p.id, body);
      setKey("");
      flash();
      await onChanged();
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    }
  };

  // One test per provider at a time: some accounts allow only 3 requests a minute, so a few concurrent clicks get rate-limited
  const [busy, setBusy] = useState(false);
  const test = async (id: string) => {
    if (busy) return;
    setBusy(true);
    setTests((prev) => ({ ...prev, [id]: t("Testing…") }));
    try {
      const r = await api.testModel(id);
      await onChanged(); // The result is recorded in the indicator light
      setTests((prev) => ({ ...prev, [id]: r.ok ? `✓ ${r.reply?.startsWith("(") ? r.reply : `${r.latency_ms}ms`}` : `✗ ${r.error}` }));
    } catch (e) {
      setTests((prev) => ({ ...prev, [id]: `✗ ${(e as Error).message}` }));
    } finally {
      setBusy(false);
    }
  };
  const checkKey = async () => {
    if (busy) return;
    const m = p.models.find((x) => x.enabled) ?? p.models[0];
    if (!m) return setCheck(t("✗ Add a model first, then test"));
    setBusy(true);
    try {
      setCheck(t("Checking…"));
      if (!(await save())) return setCheck("");
      const r = await api.testModel(m.id);
      await onChanged();
      setCheck(r.ok ? `${t("✓ Connected")} · ${m.display_name} · ${r.reply?.startsWith("(") ? r.reply : `${r.latency_ms}ms`}` : `✗ ${r.error}`);
    } catch (e) {
      setCheck(`✗ ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  const preview = endpointPreview(p.kind, base);
  const keyOptional = p.is_local || p.kind === "ollama";
  /** Ask the video server whether it is awake. Renders nothing, so it costs one request. */
  const checkVideo = async () => {
    setBusy(true);
    setCheck("");
    try {
      const r = await api.testVideo(p.id);
      setCheck((r.ok ? `✓ ${t("Connected")}` : `✗ ${t("Not reachable")}`) + (r.detail ? ` · ${r.detail}` : ""));
    } catch (e) {
      setCheck(`✗ ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="prov-inner">
      <div className="detail-head">
        <h2>
          {p.name}
          {p.enabled && newCount > 0 && (
            <span className="mp-head-tags">
              <button className="tag new mp-new-btn" title={t("Open the model picker, showing new models only")} onClick={() => setPicker({ onlyNew: true })}>{t("{n} new models", { n: newCount })}</button>
            </span>
          )}
        </h2>
        <div className="row">
          <Switch checked={p.enabled} label={t("Enable {name}", { name: p.name })} onChange={async (v) => { await api.patchProvider(p.id, { enabled: v }); await onChanged(); }} />
        </div>
      </div>

      {!p.is_local && (
        <div className="field-block">
          <div className="fb-label">{t("API key")} {p.has_key && <span className="tag on">{t("set · {hint}", { hint: p.key_hint })}</span>}</div>
          <div className="input-group">
            <input
              type={showKey ? "text" : "password"}
              value={key}
              onChange={(e) => { setKey(e.target.value); setCheck(""); }}
              onBlur={() => void save()}
              placeholder={p.has_key ? t("Saved — type a new key to replace it") : "sk-…"}
              autoComplete="off"
              spellCheck={false}
              aria-label={t("API key")}
            />
            <button className="icon-btn" aria-label={showKey ? t("Hide the key") : t("Show the key")} onClick={() => setShowKey((s) => !s)}>{showKey ? <EyeOff size={15} /> : <Eye size={15} />}</button>
            <button className="btn" disabled={busy} onClick={checkKey}>{busy ? t("Checking…") : t("Check")}</button>
          </div>
          {check && <div className={"fb-note " + (check.startsWith("✓") ? "ok-text" : check.startsWith("✗") ? "err" : "muted")}>{check}</div>}
          {!p.has_key && !key && <div className="fb-note muted">{t("The key is kept in this machine's data directory only, and is sent to this provider alone.")}</div>}
        </div>
      )}

      <div className="field-block">
        <div className="fb-label">{t("API address")}{DEFAULT_BASE[p.kind] ? t(" (leave it blank for the official default)") : ""}</div>
        <div className="input-group">
          <input value={base} onChange={(e) => setBase(e.target.value)} onBlur={() => void save()} placeholder={DEFAULT_BASE[p.kind] ?? "https://…/v1"} aria-label={t("API address")} spellCheck={false} />
        </div>
        {preview && <div className="fb-note muted">{t("Preview:")} {preview}</div>}
        {saved && <div className="fb-note ok-text">{t("Saved")}</div>}
      </div>

      {isMedia(p.kind) ? (
        <div className="field-block">
          <div className="fb-label">{t("Video generation")}</div>
          <div className="fb-note muted">
            {t("This is not a chat model: it renders video with sound, and members reach it through the generate_video tool. There is no model list to fill in — turn the tool on under Permissions & control, then test the address here.")}
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn small" disabled={busy} onClick={checkVideo}>{busy ? t("Checking…") : t("Test the video server")}</button>
          </div>
          {check && <div className={"fb-note " + (check.startsWith("✓") ? "ok-text" : "err")}>{check}</div>}
        </div>
      ) : (
      <>
      <div className="fb-label models-head">
        <span>{t("Models")} <span className="chip">{p.models.length}</span></span>
        <span className="row">
          <button className="btn small primary" onClick={() => setPicker({ onlyNew: false })}><Plus size={14} /> {t("Add models")}</button>
        </span>
      </div>
      {err && <div className="err" style={{ marginBottom: 8 }}>{err}</div>}
      <div className="model-list">
        {p.models.map((m) => (
          <div key={m.id} className="model-row mp-mrow">
            <div className="mr-main">
              <div className="mr-name">
                <HealthDot h={health[m.id]} label />&nbsp;{m.display_name}
                {m.strengths_custom && <span className="tag" title={t("Strengths were edited by hand; use Strengths to go back to automatic")}>{t("Custom")}</span>}
                {m.retired_reason && <span className="tag mp-warn-tag">{t("Disabled")}</span>}
              </div>
              <div className="mr-id">{m.id}</div>
              {m.summary && <div className="mp-summary">{m.summary}</div>}
              {m.retired_reason && <div className="mp-warn">{m.retired_reason}</div>}
              {tests[m.id]?.startsWith("✗") && <div className="err small mp-testerr" role="alert">{tests[m.id]}</div>}
              {m.strengths.length > 0 && <div><StrengthChips tags={m.strengths} max={8} /></div>}
            </div>
            <div className="mr-actions">
              {!tests[m.id]?.startsWith("✗") && <div className={"test-res" + (tests[m.id]?.startsWith("✓") ? " ok-text" : " muted")} title={tests[m.id]}>{tests[m.id]}</div>}
              <button className="btn small" disabled={busy} onClick={() => test(m.id)}>{t("Test")}</button>
              <button className="btn small" title={t("Edit this model's strength tags")} aria-label={t("Edit the strengths of {name}", { name: m.display_name })} onClick={() => setEditStr(m)}><Tags size={13} /> {t("Strengths")}</button>
              <Switch checked={m.enabled} label={t("Enable {name}", { name: m.display_name })} onChange={async (v) => { await api.patchModel(m.id, { enabled: v }); await onChanged(); }} />
              <button className="icon-btn" title={t("Remove the model")} aria-label={t("Remove {name}", { name: m.display_name })} onClick={async () => {
                if (!(await confirm(t('Remove the model "{name}"? If it was pulled into a group as a member, that member goes too.', { name: m.display_name }), { okText: t("Remove") }))) return;
                await api.delModel(m.id);
                await onChanged();
              }}><X size={15} /></button>
            </div>
          </div>
        ))}
        {p.models.length === 0 && <div className="empty">{t("No models yet — click Add models and tick them from this provider's full list")}</div>}
      </div>
      </>
      )}

      <div className="danger-zone">
        <button
          className="btn ghost small danger-text"
          onClick={async () => {
            if (await confirm(t('Delete the provider "{name}" and all of its models? Members pulled into a group from those models go too.', { name: p.name }), { okText: t("Delete") })) {
              await api.delProvider(p.id);
              onDeleted();
            }
          }}
        >
          <Trash2 size={14} /> {t("Delete this provider")}
        </button>
        {keyOptional && <span className="muted small">{t("A local service needs no API key")}</span>}
      </div>

      {picker && (
        <ModelPicker
          provider={p}
          focusNew={picker.onlyNew}
          onClose={() => setPicker(null)}
          onOptions={(o) => onNewCount(pendingNew(o))}
        />
      )}
      {editStr && (
        <StrengthEditor
          model={editStr}
          onClose={() => setEditStr(null)}
          onSaved={async () => { setEditStr(null); await onChanged(); }}
        />
      )}
    </div>
  );
}

function StrengthEditor({ model, onClose, onSaved }: { model: Model; onClose: () => void; onSaved: () => Promise<void> }) {
  const { t } = useI18n();
  const [val, setVal] = useState<Tag[]>(model.strengths);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const run = async (strengths: Tag[] | null) => {
    setBusy(true);
    setErr("");
    try {
      await api.patchModel(model.id, { strengths });
      await onSaved();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <Modal
      title={t("Strengths · {name}", { name: model.display_name })}
      onClose={onClose}
      actions={
        <>
          {err && <span className="err mp-foot-note">{err}</span>}
          <button className="btn" style={err ? undefined : { marginRight: "auto" }} disabled={busy || !model.strengths_custom} title={model.strengths_custom ? t("Discard the manual edits and go back to the inferred set") : t("Already the inferred set")} onClick={() => run(null)}>{t("Back to automatic")}</button>
          <button className="btn" onClick={onClose}>{t("Cancel")}</button>
          <button className="btn primary" disabled={busy} onClick={() => run(val)}>{t("Save")}</button>
        </>
      }
    >
      <p className="mp-str-note">{t("A strength is a tag inferred from the model family and name, not a benchmark score. Adjust it to match your own experience; when a member has no model of its own, one is chosen from here by the strengths the role needs.")}</p>
      <StrengthPicker value={val} onChange={setVal} disabled={busy} />
      <div className="mp-str-auto">
        {t("Inferred:")}
        {model.strengths_auto.length ? <StrengthChips tags={model.strengths_auto} max={10} /> : <span>{t("(none)")}</span>}
        {model.strengths_custom && <span className="tag">{t("Custom for now")}</span>}
      </div>
    </Modal>
  );
}

function AddProvider({ onClose, onAdded }: { onClose: () => void; onAdded: (id: string) => void }) {
  const { t } = useI18n();
  const [presets, setPresets] = useState<Preset[]>([]);
  const [custom, setCustom] = useState({ name: "", base_url: "", kind: "openai_compatible", is_local: false });
  const [err, setErr] = useState("");
  const { providers } = useData();
  useEffect(() => { api.presets().then(setPresets).catch(() => undefined); }, []);
  const have = new Set(providers.map((p) => p.id));

  const [adding, guard] = useBusy();
  const addPreset = (id: string) => guard(async () => {
    try { onAdded((await api.addProvider({ preset: id })).id); } catch (e) { setErr((e as Error).message); }
  });
  const addCustom = () => guard(async () => {
    try { onAdded((await api.addProvider(custom)).id); } catch (e) { setErr((e as Error).message); }
  });

  return (
    <Modal title={t("Add a provider")} onClose={onClose} wide>
      <div className="preset-grid">
        {presets.map((p) => (
          <button key={p.preset} className="preset" disabled={have.has(p.preset) || adding} onClick={() => void addPreset(p.preset)}>
            <LetterIcon name={p.name} size={30} />
            <span className="preset-text">
              <b>{p.name}</b>
              <span className="muted small">{have.has(p.preset) ? t("Added") : p.hint || kindLabel(p.kind)}</span>
            </span>
          </button>
        ))}
      </div>
      <h4 className="sec-sm">{t("Custom (any OpenAI-compatible endpoint)")}</h4>
      <div className="custom-row">
        <input placeholder={t("Name")} value={custom.name} onChange={(e) => setCustom({ ...custom, name: e.target.value })} aria-label={t("Name")} />
        <input placeholder={t("API address, e.g. http://127.0.0.1:8000/v1")} value={custom.base_url} onChange={(e) => setCustom({ ...custom, base_url: e.target.value })} aria-label={t("API address")} />
        <label className="check-inline"><input type="checkbox" checked={custom.is_local} onChange={(e) => setCustom({ ...custom, is_local: e.target.checked })} />{t("Local")}</label>
        <button className="btn primary" disabled={!custom.name || !custom.base_url || adding} onClick={() => void addCustom()}>{t("Add")}</button>
      </div>
      {err && <div className="err" style={{ marginTop: 8 }}>{err}</div>}
    </Modal>
  );
}
