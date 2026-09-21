import { useState } from "react";
import { AlertTriangle, ChevronRight, Cpu, Crown, Settings2, TerminalSquare, UserMinus } from "lucide-react";
import { api, type Capabilities, type Group } from "../../api";
import { useData } from "../../data";
import { useConfirm } from "../../ui";
import { StrengthChips } from "../Strengths";
import { HealthDot, ModelSelect } from "../Health";
import ExternalDialog from "../ExternalDialog";
import { levelLabel } from "../../lib";
import { useI18n } from "../../i18n";

export type MemberRow = Capabilities["members"][number];

/** One group member in the sidebar: a one-line summary that expands to show
 * strengths, switch the model, set it as host, or remove it from the group. */
export default function MemberCard({ group, m, hasCaps }: { group: Group; m: MemberRow; hasCaps: boolean }) {
  const { t } = useI18n();
  const { agents, health, reload, reloadGroups } = useData();
  const confirm = useConfirm();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [extOpen, setExtOpen] = useState(false);
  const agent = agents.find((a) => a.id === m.agent_id);

  const run = async (fn: () => Promise<unknown>, full = false) => {
    setBusy(true);
    setErr("");
    try {
      await fn();
      await (full ? reload() : reloadGroups());
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    const msg = m.is_host
      ? t("{name} is the host of this group. Removing it leaves the group without a host (messages with no @mention go to the first member); you can appoint a new one. Remove it?", { name: m.name })
      : t("Remove {name} from this group? (The member itself is kept — it just stops taking part in this group.)", { name: m.name });
    if (!(await confirm(msg, { okText: t("Remove") }))) return;
    await run(() => api.removeMember(group.id, m.agent_id));
  };

  const ext = !!m.engine;
  // Only the command-line engine has a working directory and permission levels; every other engine
  // is a chat gateway (Cherry Studio, MetaChat), which just exchanges messages.
  const gateway = ext && m.engine !== "workbuddy";
  const modelText = ext
    ? (gateway ? t("External agent · chat gateway")
               : t("External agent · {level}", { level: levelLabel(agent?.engine_cfg?.level ?? "read") }))
    : m.model
      ? (m.manual_model ? m.model.display_name : t("Auto · {model}", { model: m.model.display_name }))
      : hasCaps ? t("No model available") : "";
  return (
    <div className={"mc" + (open ? " open" : "")}>
      <button className="mc-row" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="avatar sm">{m.avatar}</span>
        <span className="mc-main">
          <span className="mc-name">
            {m.name}
            {m.is_host && <span className="chip host" title={t("Host")}>{t("Host")}</span>}
            {ext && <span className="chip mc-model-chip" title={gateway
              ? t("External agent: replies come from a chat gateway you configured, not through this app's model routing")
              : t("External agent: replies come from a command-line engine of its own, not through this app's model routing")}><TerminalSquare size={10} /> {t("External")}</span>}
            {m.origin === "model" && <span className="chip mc-model-chip" title={t("Added to the group from \"Models I added\"")}><Cpu size={10} /> {t("Model")}</span>}
          </span>
          <span className="mc-sub">{m.role || t("Member")}{modelText ? ` · ${modelText}` : ""}</span>
        </span>
        {m.model_problem && <AlertTriangle size={13} className="mc-warn-ico" aria-label={t("The pinned model is unavailable right now")} />}
        {!ext && m.model && <HealthDot h={health[m.model.id]} />}
        <ChevronRight size={14} className={"mc-chev" + (open ? " open" : "")} aria-hidden />
      </button>

      {open && (
        <div className="mc-detail">
          {ext ? (
            gateway ? (
              <>
                <div className="mc-line"><span className="mc-k">{t("Model")}</span>{agent?.engine_cfg?.model || t("Not set")}</div>
                <div className="mc-line"><span className="mc-k">{t("Address")}</span><span className="mc-path" title={agent?.engine_cfg?.base_url}>{agent?.engine_cfg?.base_url || t("Not set")}</span></div>
                <div className="mc-line muted small">{t("Each reply is a separate request and takes a few seconds; it cannot host a group.")}</div>
              </>
            ) : (
              <>
                <div className="mc-line"><span className="mc-k">{t("Permissions")}</span>{levelLabel(agent?.engine_cfg?.level ?? "read")}{agent?.engine_cfg?.web ? t(" · web access") : ""}</div>
                <div className="mc-line"><span className="mc-k">{t("Folder")}</span><span className="mc-path" title={agent?.engine_cfg?.cwd || t("A dedicated empty folder")}>{agent?.engine_cfg?.cwd || t("A dedicated empty folder")}</span></div>
                <div className="mc-line muted small">{t("Each reply is a separate process and usually takes a while; it cannot host a group.")}</div>
              </>
            )
          ) : m.origin === "model" ? (
            <div className="mc-line"><span className="mc-k">{t("Model")}</span>{m.model ? <><HealthDot h={health[m.model.id]} label />&nbsp;{m.model.display_name}</> : t("No model available")}{t("(a model member, pinned to this model)")}</div>
          ) : (
            <div className="mc-line">
              <span className="mc-k">{t("Model")}</span>
              <ModelSelect value={agent?.model_id ?? null} autoLabel={t("Auto (pick by strength)")} disabled={busy} ariaLabel={t("Model used by {name}", { name: m.name })} onChange={(id) => void run(() => api.patchAgent(m.agent_id, { model_id: id }), true)} />
            </div>
          )}
          {!ext && hasCaps && m.model && <div className="mc-line mc-now"><span className="mc-k">{t("In use")}</span>{m.manual_model ? "" : <em>{t("Auto ·")}</em>}{m.model.display_name}&nbsp;<HealthDot h={health[m.model.id]} label /></div>}
          {!ext && m.model_problem && <div className="mc-warn" role="status"><AlertTriangle size={12} /> {t("The pinned model is unavailable right now ({problem}); falling back to another for now", { problem: m.model_problem })}</div>}
          {m.strengths.length > 0 && <StrengthChips tags={m.strengths} max={6} className="mc-str" />}
          {err && <div className="err mc-err" role="alert">{err}</div>}
          <div className="mc-acts">
            {ext && agent && <button className="btn small" disabled={busy} onClick={() => setExtOpen(true)}><Settings2 size={12} /> {t("Settings")}</button>}
            {!m.is_host && !ext && <button className="btn small" disabled={busy} onClick={() => void run(() => api.patchGroup(group.id, { host_agent_id: m.agent_id }))}><Crown size={12} /> {t("Make host")}</button>}
            <button className="btn small" disabled={busy} onClick={() => void remove()}><UserMinus size={12} /> {t("Remove from group")}</button>
          </div>
        </div>
      )}
      {extOpen && agent && <ExternalDialog mode="edit" agent={agent} onClose={() => setExtOpen(false)} onDone={() => setExtOpen(false)} />}
    </div>
  );
}
