import { useMemo, useState } from "react";
import { Clapperboard, Cpu, Image as ImageIcon, Plus, RefreshCw, Search } from "lucide-react";
import { api, type Group, type Model } from "../../api";
import { useData } from "../../data";
import { useI18n } from "../../i18n";
import { StrengthChips } from "../Strengths";
import ModelCategories, { matchesCategory, type ModelCategory } from "../ModelCategories";
import ModelAvailability, { modelAvailability } from "../ModelAvailability";

const PAGE_SIZE = 10;
const STATE_ORDER = { ok: 0, unknown: 1, limited: 2, bad: 3, off: 4 };

export default function ModelRoster({ group, section, busy = "", run, selectedModels = [], onSelect }: {
  group?: Group; section: "members" | "tools"; busy?: string;
  run?: (key: string, action: () => Promise<unknown>, full?: boolean) => Promise<void>;
  selectedModels?: string[];
  onSelect?: (model: Model) => void;
}) {
  const { t } = useI18n();
  const { providers, agents, health, settings, reloadHealth } = useData();
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<ModelCategory>("all");
  const [provider, setProvider] = useState("");
  const [status, setStatus] = useState("all");
  const [page, setPage] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const rows = useMemo(() => {
    const memberIds = new Set(group?.member_ids ?? []);
    const members = new Map(agents.filter((a) => a.origin === "model" || a.origin === "media").map((a) => [a.model_id, a]));
    return providers.flatMap((p) => (section === "tools" ? p.media_models ?? [] : p.models)
      .map((m) => ({ model: m, provider: p, here: memberIds.has(members.get(m.id)?.id ?? "") || selectedModels.includes(m.id),
        availability: modelAvailability(m, p, health[m.id], settings) })));
  }, [providers, agents, group?.member_ids, health, settings, section, selectedModels]);
  const scoped = rows.filter((r) => !provider || r.provider.id === provider);
  const q = query.trim().toLocaleLowerCase();
  const filtered = scoped.filter((r) => matchesCategory(r.model, category)
    && (!q || [r.model.display_name, r.model.model_name, r.provider.name, r.model.summary ?? ""].join(" ").toLocaleLowerCase().includes(q))
    && (status === "all" || (status === "addable" ? !r.here && !r.availability.blocked
      : status === "here" ? r.here : status === "connected" ? r.availability.state === "ok"
        : r.availability.blocked || r.availability.state === "bad" || r.availability.state === "limited")))
    .sort((a, b) => Number(b.here) - Number(a.here)
      || STATE_ORDER[a.availability.state] - STATE_ORDER[b.availability.state]
      || Number(b.provider.kind === "comfyui") - Number(a.provider.kind === "comfyui")
      || a.provider.name.localeCompare(b.provider.name) || a.model.display_name.localeCompare(b.model.display_name));
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pages - 1);
  const shown = filtered.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  const change = (fn: () => void) => { fn(); setPage(0); };

  return <div className="model-roster">
    <p className="madd-note">{t(section === "tools"
      ? "Choose an image or video tool. Members assign the work and review its returned files."
      : "Choose a capability, then a model. Configured models may still be unchecked; connectivity is shown separately.")}</p>
    <ModelCategories value={category} onChange={(v) => change(() => setCategory(v))} models={scoped.map((r) => r.model)} section={section} />
    <div className="model-roster-search search-box"><Search size={14} /><input value={query}
      onChange={(e) => change(() => setQuery(e.target.value))} placeholder={t("Search model or provider…")} aria-label={t("Search models")} /></div>
    <div className="model-roster-filters">
      <label>{t("Provider")}<select value={provider} onChange={(e) => change(() => setProvider(e.target.value))}>
        <option value="">{t("All providers")}</option>
        {providers.filter((p) => rows.some((r) => r.provider.id === p.id)).map((p) => <option key={p.id} value={p.id}>{p.name}{p.is_local ? ` · ${t("Local")}` : ""}</option>)}
      </select></label>
      <label>{t("Model status")}<select value={status} onChange={(e) => change(() => setStatus(e.target.value))}>
        <option value="all">{t("All statuses")}</option>
        <option value="addable">{t("Ready to add")}</option>
        <option value="connected">{t("Connected")}</option>
        <option value="here">{t(onSelect ? "Selected" : "Already in this group")}</option>
        <option value="attention">{t("Needs attention")}</option>
      </select></label>
      <button className="btn small" disabled={refreshing} title={t("Read recorded status without sending model requests")}
        onClick={async () => { setRefreshing(true); try { await reloadHealth(); } finally { setRefreshing(false); } }}>
        <RefreshCw size={13} />{t("Refresh status")}
      </button>
    </div>
    <div className="model-roster-count" role="status">{t("{n} matching models", { n: filtered.length })}</div>
    <div className="model-roster-list" role="list" aria-label={t("Models available to join")}>
      {shown.map(({ model: m, provider: p, here, availability }) => <div key={m.id} className="madd-item" role="listitem">
        <span className="avatar sm" aria-hidden>{m.use === "video" ? <Clapperboard size={15} /> : m.use === "image" ? <ImageIcon size={15} /> : <Cpu size={15} />}</span>
        <div className="madd-main">
          <div className="madd-name">{p.kind === "comfyui" ? `ComfyUI · ${m.display_name}` : m.display_name}</div>
          <div className="madd-sub wrap">{p.name} · {t(p.is_local ? "Local" : "Cloud")}</div>
          <ModelAvailability value={availability} />
          {availability.detail && availability.blocked && <div className="madd-sub wrap">{availability.detail}</div>}
          {m.strengths.length > 0 && <StrengthChips tags={m.strengths} max={3} />}
        </div>
        {here ? <span className="membership-badge madd-here">{t(onSelect ? "Selected" : "Already in this group")}</span>
          : <button className="btn small" disabled={!!busy || availability.blocked} title={availability.blocked ? t(availability.label) : ""}
              onClick={() => { if (onSelect) onSelect(m); else if (group && run) void run("model:" + m.id, () => api.addMemberFromModel(group.id, m.id), true); }}
              aria-label={t("Add model {name} to the group", { name: p.kind === "comfyui" ? `ComfyUI · ${m.display_name}` : m.display_name })}>
              <Plus size={12} />{t(busy === "model:" + m.id ? "Adding…" : "Add")}
            </button>}
      </div>)}
      {!shown.length && <div className="model-roster-empty">
        <b>{t("No models match")}</b>
        <p>{t("Try another type or provider, or show all statuses to see models that need setup.")}</p>
        <button className="btn small" onClick={() => change(() => { setCategory("all"); setProvider(""); setStatus("all"); setQuery(""); })}>{t("Clear filters")}</button>
      </div>}
    </div>
    {pages > 1 && <nav className="model-roster-pages" aria-label={t("Model pages")}>
      <button className="btn small" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>{t("Previous page")}</button>
      <span>{t("Page {page} of {pages}", { page: currentPage + 1, pages })}</span>
      <button className="btn small" disabled={currentPage === pages - 1} onClick={() => setPage(currentPage + 1)}>{t("Next page")}</button>
    </nav>}
    <p className="madd-note">{t("Use Settings → Providers to configure keys, enable models or test a connection.")}</p>
  </div>;
}
