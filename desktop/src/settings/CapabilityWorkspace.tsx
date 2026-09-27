import { createContext, useContext, useRef, useState, type ReactNode } from "react";
import { api, type Group } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import type { SettingsTab } from "./SettingsModal";
import "../styles/capabilities.css";

export type CapabilityArea = "skills" | "plugins" | "mcp" | "hooks" | "external" | "channels";
type BindingKind = "skills" | "plugins" | "mcp" | "member";
const Scope = createContext<Group | null>(null);
export const useCapabilityGroup = () => useContext(Scope);
const FLOW: { key: CapabilityArea; label: string; role: string }[] = [
  { key: "channels", label: "Chat channels", role: "Receive tasks and deliver replies" },
  { key: "external", label: "Agents and local tools", role: "Join a group and execute assignments" },
  { key: "skills", label: "Skills", role: "Guide how members work" },
  { key: "plugins", label: "Plugins", role: "Provide tools inside this app" },
  { key: "mcp", label: "MCP", role: "Connect tools through a standard service" },
  { key: "hooks", label: "Hooks", role: "React at defined points in the workflow" },
];

export default function CapabilityWorkspace({ area, groupId, onGroup, onTab, onOpenGroup, children }: {
  area: CapabilityArea; groupId: string | null; onGroup: (id: string) => void;
  onTab: (tab: SettingsTab) => void; onOpenGroup: (id: string) => void; children: ReactNode;
}) {
  const { t } = useI18n();
  const { groups } = useData();
  const group = groups.find((g) => g.id === groupId) ?? null;
  const hasGroup = ["skills", "plugins", "mcp", "external"].includes(area);
  return <Scope.Provider value={hasGroup ? group : null}>
    <section className="cap-workspace">
      <header className="cap-intro">
        <div className="cap-eyebrow">{t("Capability center")}</div>
        <p>{t("Task → Team review → Assignment → Tool execution → Delivery review")}</p>
        <details className="cap-guide">
          <summary>{t("How these capabilities work together")}</summary>
          <div className="cap-flow">{FLOW.map((item) => <button key={item.key} onClick={() => onTab(item.key)} aria-current={item.key === area ? "page" : undefined}>
            <b>{t(item.label)}</b><span>{t(item.role)}</span>
          </button>)}</div>
          <p>{t("The host assigns work to members and tools. The hidden process engineer reviews outcomes and records feedback; hooks follow explicit event rules.")}</p>
        </details>
      </header>
      {hasGroup ? <div className="cap-scope">
        <label><span>{t("Target group")}</span><select aria-label={t("Target group")} value={group?.id ?? ""} onChange={(e) => onGroup(e.target.value)}>
          <option value="">{t("Global catalog · choose a group to attach")}</option>
          {groups.map((g) => <option key={g.id} value={g.id}>{g.name}{g.archived ? ` · ${t("Archived")}` : ""}</option>)}
        </select></label>
        <span className="muted small">{group ? t("Group attachment is separate from global configuration. Changes apply to future tasks.") : t("Manage installed capabilities here. Select a group to see and change what it uses.")}</span>
        {group && <button className="btn small" onClick={() => onOpenGroup(group.id)}>{t("Open target group")}</button>}
        {group?.busy && <span className="err small">{t("This group is working. Wait for it to finish before changing attachments.")}</span>}
      </div> : <div className="cap-scope muted small">{area === "hooks" ? t("Each hook has its own event and group scope. Only an explicit All groups selection applies it everywhere.") : t("Each channel binds to its own group. Configure access, choose the group, then enable and check the connection.")}</div>}
      {children}
    </section>
  </Scope.Provider>;
}

export function CapabilityBinding({ kind, id, name, problem = "" }: { kind: BindingKind; id: string; name: string; problem?: string }) {
  const { t } = useI18n();
  const group = useCapabilityGroup();
  const { reloadGroups } = useData();
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [err, setErr] = useState("");
  if (!group) return null;
  const attached = kind === "member" ? group.member_ids.includes(id) : group.ext[kind]?.includes(id);
  const toggle = async () => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setErr("");
    try {
      if (kind === "member") {
        if (attached) await api.removeMember(group.id, id);
        else await api.addMember(group.id, id);
      } else await api.bindCapability(group.id, { kind, ref: id, attached: !attached });
      await reloadGroups();
    } catch (e) { setErr((e as Error).message); }
    finally { lock.current = false; setBusy(false); }
  };
  return <div className="cap-binding">
    <span className={"tag " + (attached ? "on" : "")}>{attached ? t("Attached to this group") : t("Not attached to this group")}</span>
    {attached && problem && <span className="muted small">{problem}</span>}
    <button className="btn small" disabled={busy || group.busy} onClick={() => void toggle()}
      aria-label={attached ? t("Detach {name} from this group", { name }) : t("Attach {name} to this group", { name })}>
      {busy ? t("Saving…") : attached ? t("Detach from group") : t("Attach to group")}
    </button>
    {err && <span className="err small" role="alert">{err}</span>}
  </div>;
}

export function CapabilityFilter({ query, onQuery, attachedOnly, onAttachedOnly, total, shown }: {
  query: string; onQuery: (s: string) => void; attachedOnly: boolean; onAttachedOnly: (b: boolean) => void; total: number; shown: number;
}) {
  const { t } = useI18n();
  const group = useCapabilityGroup();
  return <div className="cap-filter">
    <input type="search" value={query} onChange={(e) => onQuery(e.target.value)} placeholder={t("Search by name or description")} aria-label={t("Search capabilities")} />
    {query && <button className="btn small" onClick={() => onQuery("")}>{t("Clear")}</button>}
    {group && <label className="cap-check"><input type="checkbox" checked={attachedOnly} onChange={(e) => onAttachedOnly(e.target.checked)} />{t("Only attached to this group")}</label>}
    <span className="muted small">{t("{n} of {total} shown", { n: shown, total })}</span>
  </div>;
}
