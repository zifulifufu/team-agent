import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Plus, X } from "lucide-react";
import { api, mayHost, type Group, type AgentPreset, type TeamAdvice, type TeamDraftMember } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Modal } from "../ui";
import ModelAvailability, { modelAvailability, type Availability } from "./ModelAvailability";
import ModelRoster from "./members/ModelRoster";

export const teamRef = (m: TeamDraftMember) => `${m.kind}:${m.id}`;

export default function TeamReview({ task, workspace, advice, initial, onClose, onStart }: {
  task: string; workspace: string; advice: TeamAdvice; initial: TeamDraftMember[];
  onClose: () => void; onStart: (group: Group, task: string) => void;
}) {
  const { t, pick } = useI18n();
  const { agents, providers, health, settings, reload } = useData();
  const [selected, setSelected] = useState(initial);
  const [hostRef, setHostRef] = useState(advice.host_ref ?? "");
  const [view, setView] = useState("lineup");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [presets, setPresets] = useState<AgentPreset[]>([]);
  useEffect(() => {
    if (view !== "experts") return;
    let live = true;
    api.agentPresets().then((all) => { if (live) setPresets(all); }).catch((e) => { if (live) setError((e as Error).message); });
    return () => { live = false; };
  }, [view]);
  const working = useRef(false);
  const created = useRef<Group | null>(null);
  const allModels = providers.flatMap((p) => [...p.models, ...(p.media_models ?? [])].map((m) => ({ m, p })));
  const automaticReady = allModels.some(({ m, p }) => m.use === "chat" && !modelAvailability(m, p, health[m.id], settings).blocked);
  const rows = selected.map((item) => {
    const agent = item.kind === "agent" ? agents.find((a) => a.id === item.id) : undefined;
    const modelId = item.kind === "model" ? item.id : agent?.model_id;
    const model = allModels.find(({ m }) => m.id === modelId);
    const libraryPreset = presets.find((p) => p.key === item.id);
    const preset = item.kind === "preset" ? advice.experts.find((e) => e.key === item.id)
      ?? (libraryPreset ? { ...libraryPreset, name_zh: libraryPreset.name, why: "" } : undefined) : undefined;
    const tool = !!agent?.is_tool || model?.m.use === "image" || model?.m.use === "video";
    let state: Availability = model ? modelAvailability(model.m, model.p, health[model.m.id], settings)
      : { state: automaticReady ? "unknown" : "off", label: automaticReady ? "Automatic routing" : "No configured chat model", blocked: !automaticReady };
    if (agent?.engine) state = { state: settings?.external_agents_enabled ? "unknown" : "off",
      label: settings?.external_agents_enabled ? "Not checked" : "External agents are switched off", blocked: !settings?.external_agents_enabled };
    const missing = item.kind === "agent" ? !agent : item.kind === "model" ? !model : !preset;
    if (missing) state = { state: "off", label: "Member no longer available", blocked: true };
    return { item, ref: teamRef(item), name: agent?.name || (model ? `${model.p.kind === "comfyui" ? "ComfyUI · " : ""}${model.m.display_name}` : "") || (preset ? pick(preset.name, preset.name_zh) : item.id),
      avatar: agent?.avatar || preset?.avatar || (tool ? "🛠️" : "🤖"), tool,
      mayHost: agent ? mayHost(agent) : !tool && model?.m.use !== "responses", state,
      role: agent?.role || preset?.role || t(tool ? "Generate and return files" : "Discuss and execute assignments"),
      why: advice.members.find((a) => a.id === item.id)?.why || advice.models?.find((m) => m.id === item.id)?.why || preset?.why || "" };
  });
  const host = rows.find((r) => r.ref === hostRef && r.mayHost && !r.state.blocked)
    ?? rows.find((r) => r.mayHost && !r.state.blocked);
  const blocked = rows.filter((r) => r.state.blocked);
  const add = (item: TeamDraftMember) => {
    setSelected((current) => current.some((m) => teamRef(m) === teamRef(item)) ? current : [...current, item]);
    setView("lineup");
  };
  const start = async () => {
    if (working.current || !host || blocked.length) return;
    working.current = true; setBusy(true); setError("");
    try {
      created.current ??= await api.createGroup("", [], null, { workspace, task, lineup: selected, host_ref: host.ref });
      await reload();
      onStart(created.current, task);
    } catch (e) { setError((e as Error).message); }
    finally { working.current = false; setBusy(false); }
  };
  return createPortal(<Modal wide title={t("Review the team before starting")} onClose={() => { if (!busy) onClose(); }} actions={<>
    <button className="btn" disabled={busy} onClick={onClose}>{t("Back to task")}</button>
    <button className="btn primary" disabled={busy || !host || blocked.length > 0} onClick={() => void start()}>{t(busy ? "Starting…" : "Confirm team and start")}</button>
  </>}>
    <div className="team-review">
      <div className="team-flow">{t("Describe task → Review team → Assign work → Verify delivery")}</div>
      <div className="team-task">{task}</div>
      <p className="madd-note">{t("This is a draft. No group is created and no task is sent until you confirm. The host will then coordinate assignments and tools.")}</p>
      <div className="madd-sources" role="group" aria-label={t("Edit the proposed team")}>
        {[["lineup", "Proposed team"], ["existing", "Existing members"], ["members", "Add model members"], ["experts", "Experts"], ["tools", "Add generation tools"], ["local", "Local execution tools"]].map(([id, label]) =>
          <button key={id} aria-pressed={view === id} disabled={busy} onClick={() => setView(id)}>{t(label)}</button>)}
      </div>
      {error && <div className="err" role="alert">{error}</div>}
      {view === "lineup" && <>
        <label className="team-host">{t("Host")}<select disabled={busy} value={host?.ref || ""} onChange={(e) => setHostRef(e.target.value)}>
          {!host && <option value="">{t("Choose a conversational host")}</option>}
          {rows.filter((r) => r.mayHost && !r.state.blocked).map((r) => <option key={r.ref} value={r.ref}>{r.name}</option>)}
        </select></label>
        {([false, true] as const).map((tools) => <section key={String(tools)} aria-label={t(tools ? "Tools" : "Members")}>
          <h4>{t(tools ? "Tools" : "Members")} · {rows.filter((r) => r.tool === tools).length}</h4>
          {rows.filter((r) => r.tool === tools).map((r) => <div className="madd-item" key={r.ref}>
            <span className="avatar sm">{r.avatar}</span><div className="madd-main">
              <div className="madd-name">{r.name}{host?.ref === r.ref && <span className="chip host">{t("Host")}</span>}</div>
              <div className="madd-sub wrap">{host?.ref === r.ref ? t("Assign work, track progress and verify the final delivery") : r.role}</div>
              {r.why && <div className="madd-sub wrap">{r.why}</div>}
              <ModelAvailability value={r.state} />
            </div><button className="icon-btn" disabled={busy} aria-label={t("Remove {name} from draft", { name: r.name })}
              onClick={() => setSelected((all) => all.filter((i) => teamRef(i) !== r.ref))}><X size={14} /></button>
          </div>)}
        </section>)}
        {!host && <div className="err">{t("Add a configured conversational member to lead the team.")}</div>}
        {blocked.length > 0 && <div className="err">{t("Configure or remove unavailable members before starting.")}</div>}
        {advice.warnings?.map((w) => <p key={w} className="madd-note">{w}</p>)}
        <p className="madd-note">{t("The hidden process engineer records failures and feeds corrections into later rounds.")}</p>
      </>}
      {view === "experts" && <>
        <p className="madd-note">{t("Experts are members with domain expertise. They can lead, execute assignments and review results.")}</p>
        <input className="madd-q" aria-label={t("Search members")} placeholder={t("Search members…")} value={query} onChange={(e) => setQuery(e.target.value)} />
        <div className="team-existing-list">{presets.filter((p) => p.kind === "expert" && (!query || `${p.name} ${p.role}`.toLowerCase().includes(query.toLowerCase()))).map((p) => {
          const item: TeamDraftMember = p.agent_id ? { kind: "agent", id: p.agent_id } : { kind: "preset", id: p.key };
          const here = selected.some((s) => teamRef(s) === teamRef(item) || (s.kind === "preset" && s.id === p.key));
          return <div className="madd-item" key={p.key}><span>{p.avatar}</span><div className="madd-main"><b>{p.name}</b><div className="madd-sub wrap">{p.role}</div></div>
            {here ? <span className="membership-badge">{t("Selected")}</span> : <button className="btn small" disabled={busy} aria-label={t("Select {name}", { name: p.name })} onClick={() => add(item)}><Plus size={12} />{t("Add")}</button>}
          </div>;
        })}</div>
      </>}
      {(view === "existing" || view === "local") && <>
        {view === "local" && <p className="madd-note">{t("Renderers need a prepared project; voice tools need the exact narration. Add them here, then let members assign the work.")}</p>}
        <input className="madd-q" aria-label={t("Search members")} placeholder={t("Search members…")} value={query} onChange={(e) => setQuery(e.target.value)} />
        <div className="team-existing-list">{agents.filter((a) => (view === "local" ? a.is_tool && a.engine : !a.is_tool)
          && (!query || `${a.name} ${a.role}`.toLowerCase().includes(query.toLowerCase()))).map((a) => <div className="madd-item" key={a.id}>
          <span>{a.avatar}</span><div className="madd-main"><b>{a.name}</b><div className="madd-sub wrap">{a.role}</div></div>
          {selected.some((s) => s.kind === "agent" && s.id === a.id) ? <span className="membership-badge">{t("Selected")}</span>
            : <button className="btn small" disabled={busy} aria-label={t("Select {name}", { name: a.name })} onClick={() => add({ kind: "agent", id: a.id })}><Plus size={12} />{t("Add")}</button>}
        </div>)}</div>
      </>}
      {(view === "members" || view === "tools") && <ModelRoster key={view} section={view} busy={busy ? "starting" : ""}
        selectedModels={rows.flatMap((r) => r.item.kind === "model" ? [r.item.id] : agents.filter((a) => a.id === r.item.id && a.model_id).map((a) => a.model_id!))}
        onSelect={(m) => add({ kind: "model", id: m.id })} />}
    </div>
  </Modal>, document.body);
}
