import { useEffect, useState } from "react";
import { Plus, Settings2, Trash2 } from "lucide-react";
import { api, type Agent, type AgentPreset, type Model, type PromptItem, type Skill } from "../api";
import { useData } from "../data";
import { useBusy, useConfirm, useFlash } from "../ui";
import { StrengthChips, StrengthPicker } from "../components/Strengths";
import { ModelSelect } from "../components/Health";
import ExternalDialog from "../components/ExternalDialog";
import type { SettingsTab } from "../settings/SettingsModal";
import { pick, useI18n } from "../i18n";
import "../styles/models.css";

const BLANK: Partial<Agent> = { name: "", avatar: "🤖", role: "", prompt: "", model_id: null, skills: [], tags: [] };

export default function AgentsPage({ onSettings }: { onSettings: (tab: SettingsTab) => void }) {
  const { t } = useI18n();
  const { agents, reload } = useData();
  const [sel, setSel] = useState<string | "new" | null>(null);
  const creating = sel === "new";
  const cur = creating ? null : agents.find((a) => a.id === sel) ?? (sel === null ? agents[0] : null) ?? null;

  return (
    <div className="page-cols">
      <aside className="page-list">
        <div className="page-list-head">
          <h2>{t("Members")}</h2>
          <button className="btn small" onClick={() => setSel("new")}><Plus size={14} /> {t("New")}</button>
        </div>
        <div className="page-list-body">
          {agents.map((a) => (
            <button key={a.id} className={"list-item" + (!creating && cur?.id === a.id ? " on" : "")} onClick={() => setSel(a.id)}>
              <span className="avatar sm">{a.avatar}</span>
              <span className="li-main">
                <span className="li-name">{a.name}</span>
                <span className="li-sub">
                  {a.role || t("Member")}
                  {a.tags?.length > 0 && <span className="ag-tags"> · {a.tags.slice(0, 2).join(" ")}</span>}
                </span>
              </span>
            </button>
          ))}
        </div>
      </aside>
      <section className="page-detail">
        {creating || cur ? (
          <AgentForm key={creating ? "new" : cur!.id} agent={creating ? null : cur} onDone={async (id) => { await reload(); setSel(id ?? null); }} onSettings={onSettings} />
        ) : (
          <div className="empty big">{t("No members yet — create one first")}</div>
        )}
      </section>
    </div>
  );
}

function AgentForm({ agent, onDone, onSettings }: { agent: Agent | null; onDone: (id?: string) => Promise<void>; onSettings: (tab: SettingsTab) => void }) {
  const { t } = useI18n();
  const { models } = useData();
  const confirm = useConfirm();
  const [f, setF] = useState<Partial<Agent>>(agent ?? BLANK);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [presets, setPresets] = useState<AgentPreset[]>([]);
  const [presetKey, setPresetKey] = useState("");
  const [prompts, setPrompts] = useState<PromptItem[]>([]);
  const [reco, setReco] = useState<{ models: (Model & { score: number })[] } | null>(null);
  const [recoErr, setRecoErr] = useState("");
  const [err, setErr] = useState("");
  const [extOpen, setExtOpen] = useState(false);
  const [saved, flash] = useFlash();
  useEffect(() => {
    api.skills().then((l) => setSkills(l.filter((s) => s.scope !== "group"))).catch(() => undefined);
    api.prompts().then((r) => setPrompts(r.prompts.filter((x) => x.kind === "general"))).catch(() => undefined);
    if (!agent) api.agentPresets().then(setPresets).catch(() => undefined);
  }, [agent]);

  // With no pinned model, preview which model would be picked for this role, based on
  // its strengths (debounced by 300ms).
  const tagsKey = (f.tags ?? []).join(",");
  useEffect(() => {
    setRecoErr("");
    if (f.model_id || !tagsKey) { setReco(null); return; }
    let alive = true;
    const t = window.setTimeout(() => {
      api.recommendModels(tagsKey.split(","), 3)
        .then((r) => alive && setReco({ models: r.models }))
        .catch((e) => { if (alive) { setReco(null); setRecoErr((e as Error).message); } });
    }, 300);
    return () => { alive = false; window.clearTimeout(t); };
  }, [tagsKey, f.model_id]);

  const applyPreset = (p: AgentPreset) => {
    setPresetKey(p.key);
    setF((x) => ({ ...x, name: p.name, avatar: p.avatar, role: p.role, tags: [...p.tags], prompt: p.prompt }));
  };
  const insertPrompt = (id: string) => {
    const it = prompts.find((x) => x.id === id);
    if (!it) return;
    setF((x) => ({ ...x, prompt: x.prompt?.trim() ? `${x.prompt.replace(/\s+$/, "")}\n\n${it.content}` : it.content }));
  };

  const [saving, guard] = useBusy();
  const save = () => guard(async () => {
    setErr("");
    try {
      if (agent) {
        await api.patchAgent(agent.id, f);
        flash();
        await onDone(agent.id);
      } else {
        const a = await api.createAgent(f);
        await onDone(a.id);
      }
    } catch (e) {
      setErr((e as Error).message);
    }
  });

  const fixed = f.model_id ? models.find((m) => m.id === f.model_id && m.enabled) : undefined;
  return (
    <div className="detail-inner">
      <div className="detail-head">
        <h2>{agent ? agent.name : t("New member")}</h2>
        {agent && (
          <button
            className="btn ghost small danger-text"
            onClick={async () => {
              if (await confirm(t("Delete the member \"{name}\"?", { name: agent.name }), { okText: t("Delete") })) {
                await api.delAgent(agent.id);
                await onDone();
              }
            }}
          >
            <Trash2 size={14} /> {t("Delete")}
          </button>
        )}
      </div>

      {!agent && presets.length > 0 && (
        <div className="field-block">
          <div className="fb-label">{t("Start from a preset")}</div>
          <div className="ag-presets" role="group" aria-label={t("Role presets")}>
            {presets.map((p) => (
              <button
                key={p.key}
                type="button"
                className="ag-preset"
                style={presetKey === p.key ? { borderColor: "var(--primary)", background: "var(--primary-soft)" } : undefined}
                disabled={p.exists}
                title={p.exists ? t("A member with this name already exists") : `${p.role}:${p.tags.join(pick(", ", "、"))}`}
                onClick={() => applyPreset(p)}
              >
                <span>{p.avatar}</span>{p.name}{p.exists && <small>{t("Same name exists")}</small>}
              </button>
            ))}
          </div>
          <div className="ag-help">{t("Picking a preset fills the form below with a name, role, strengths and prompt; nothing is saved until you press Create.")}</div>
        </div>
      )}
      <div className="card">
        <div className="form-row">
          <label className="field" style={{ width: 84 }}><span>{t("Avatar")}</span><input value={f.avatar ?? ""} onChange={(e) => setF({ ...f, avatar: e.target.value })} maxLength={4} /></label>
          <label className="field grow"><span>{t("Name (used as @name in a group; no spaces)")}</span><input value={f.name ?? ""} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
          <label className="field grow"><span>{t("Role")}</span><input value={f.role ?? ""} onChange={(e) => setF({ ...f, role: e.target.value })} placeholder={t("e.g. storyboard artist")} /></label>
        </div>
        <div className="field">
          <span>{t("Strengths (the more accurate, the better the task assignment; 4-5 is plenty)")}</span>
          <StrengthPicker value={f.tags ?? []} onChange={(v) => setF({ ...f, tags: v })} />
          {(f.tags?.length ?? 0) > 5 && <div className="ag-help warn">{t("{n} selected — too many makes strengths meaningless; keep the 4-5 that matter most.", { n: f.tags!.length })}</div>}
          <div className="ag-help">{t("The host assigns tasks by these strengths, and they pick the best model when none is pinned. Strength tags are inferred from the model family and name — they are not benchmark results.")}</div>
        </div>
        {agent?.engine ? (
          <div className="field">
            <span>{t("External agent")}</span>
            <div className="ag-reco">
              {t("Replies come from the command-line engine it ships with, not through this app's model routing, so there is no model to pick here.")}
              {t("Permission level, working folder and hand-off are configured separately below.")}
              <div style={{ marginTop: 8 }}><button type="button" className="btn small" onClick={() => setExtOpen(true)}><Settings2 size={12} /> {t("External agent settings")}</button></div>
            </div>
          </div>
        ) : (
        <div className="field">
          <span>{t("Model (falls back to the routing chain when the first choice fails)")}</span>
          <ModelSelect value={f.model_id ?? null} autoLabel={t("Pick automatically by strength (recommended)")} ariaLabel={t("Model in use")} disabled={agent?.origin === "model"} onChange={(id) => setF({ ...f, model_id: id })} />
          {!f.model_id ? <RecoHint tags={f.tags ?? []} reco={reco} err={recoErr} /> : fixed ? (
            <div className="ag-reco">
              {t("Pinned to")} <b>{fixed.display_name}</b>{t(", whose strengths (for comparison with the role's) are:")}
              {fixed.strengths.length ? <StrengthChips tags={fixed.strengths} max={10} /> : <span className="muted"> {t("none")}</span>}
            </div>
          ) : (
            <div className="ag-reco warn">{t("This model was removed or disabled; after saving, the model is chosen by the routing chain again.")}</div>
          )}
        </div>
        )}
        <div className="field">
          <div className="ag-prompt-head">
            <span>{t("Role prompt")}</span>
            {prompts.length > 0 && (
              <select value="" onChange={(e) => insertPrompt(e.target.value)} aria-label={t("Insert from the prompt library")}>
                <option value="">{t("Insert from the prompt library…")}</option>
                {prompts.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
              </select>
            )}
          </div>
          <textarea rows={6} value={f.prompt ?? ""} onChange={(e) => setF({ ...f, prompt: e.target.value })} placeholder={t("Describe what this member is good at, plus its output style and format")} aria-label={t("Role prompt")} />
        </div>
        <div className="field">
          <span>{t("Skills (injected into this member's prompt; works with every model)")}</span>
          <div className="check-grid">
            {skills.map((s) => (
              <label key={s.name} className={"check" + (f.skills?.includes(s.name) ? " on" : "")} title={s.description}>
                <input type="checkbox" checked={!!f.skills?.includes(s.name)} onChange={(e) => setF({ ...f, skills: e.target.checked ? [...(f.skills ?? []), s.name] : (f.skills ?? []).filter((x) => x !== s.name) })} />
                {s.name}
              </label>
            ))}
            {skills.length === 0 && (
              <span className="muted small">{t("No skills yet,")} <button className="link" onClick={() => onSettings("skills")}>{t("add one")}</button></span>
            )}
          </div>
        </div>
      </div>
      <div className="row">
        <button className="btn primary" disabled={!f.name?.trim() || saving} onClick={() => void save()}>{agent ? t("Save") : t("Create")}</button>
        {saved && <span className="ok-text">{t("Saved")}</span>}
        {err && <span className="err">{err}</span>}
      </div>
      {extOpen && agent && <ExternalDialog mode="edit" agent={agent} onClose={() => setExtOpen(false)} onDone={() => setExtOpen(false)} />}
    </div>
  );
}

function RecoHint({ tags, reco, err }: { tags: string[]; reco: { models: (Model & { score: number })[] } | null; err: string }) {
  const { t } = useI18n();
  if (err) return <div className="ag-reco warn">{t("Cannot estimate which model would be used right now: {err}", { err })}</div>;
  if (!tags.length) return <div className="ag-reco">{t("No strengths picked yet: the model is chosen by the default order in Routing & fallback. Pick a few strengths and this shows which model would be preferred.")}</div>;
  if (!reco) return <div className="ag-reco muted">{t("Picking by strength…")}</div>;
  if (!reco.models.length) return <div className="ag-reco warn">{t("No models available yet: add an API key under Providers, or start a local Ollama.")}</div>;
  const [top, ...rest] = reco.models;
  const hit = tags.filter((t) => top.strengths.includes(t));
  return (
    <div className="ag-reco">
      {t("Would prefer:")} <b>{top.display_name}</b>{t(" (matches {n}{list})", { n: hit.length || Math.round(top.score), list: hit.length ? `: ${hit.join(pick(", ", "、"))}` : "" })}
      {rest.length > 0 && <div className="muted small">{t("Alternatives: {list}", { list: rest.map((m) => m.display_name).join(pick(", ", "、")) })}</div>}
    </div>
  );
}
