import { useMemo } from "react";
import { useData } from "../../data";
import type { AgentOrigin, Group } from "../../api";
import { useCapabilities } from "../group/capabilities";
import MemberCard, { type MemberRow } from "./MemberCard";
import { useI18n } from "../../i18n";
import AddMemberButton from "./AddMemberButton";

/** The current group's members (switch their model, set the host, remove them) — rendered inside the
 *  popover that hangs off the chat header's avatars. */
export default function MemberDock({ group }: { group: Group }) {
  const { t } = useI18n();
  const { agents } = useData();
  const { caps, err } = useCapabilities(group);

  // Until the capability list arrives, fall back to local data so the list does not
  // flash empty.
  const rows: MemberRow[] = useMemo(() => {
    if (caps) return caps.members;
    return group.member_ids
      .map((id) => agents.find((a) => a.id === id))
      .filter((a): a is NonNullable<typeof a> => !!a)
      .map((a) => ({
        agent_id: a.id, name: a.name, avatar: a.avatar, role: a.role, tags: a.tags,
        is_host: group.host_agent_id === a.id, skills: a.skills, model: null, manual_model: !!a.model_id, strengths: a.tags,
        origin: (a.origin ?? "") as AgentOrigin, engine: a.engine ?? "", model_problem: "",
      }));
  }, [caps, group, agents]);

  return (
    <div className="mdock" aria-label={t("Members of \"{group}\"", { group: group.name })}>
      {rows.length === 0 && <div className="side-empty">{t("This group has no members yet — use the + above to add one")}</div>}
      {(["members", "tools"] as const).map((section) => {
        const list = rows.filter((m) => !!(m.participation?.mode === "listener" || agents.find((a) => a.id === m.agent_id)?.is_tool) === (section === "tools"));
        return <section className="roster-section" key={section} aria-label={t(section === "tools" ? "Tools" : "Members")}>
          <div className="roster-heading"><strong>{t(section === "tools" ? "Tools" : "Members")}</strong><span className="count-badge-lite">{list.length}</span><span className="grow" /><AddMemberButton group={group} section={section} align="right" /></div>
          <div className="roster-note">{t(section === "tools" ? "Execute assignments and return files. Members handle preparation and review." : "Discuss, assign work and review results.")}</div>
          {list.map((m) => <MemberCard key={m.agent_id} group={group} m={m} hasCaps={!!caps} />)}
          {!list.length && <div className="side-empty">{t(section === "tools" ? "No tools in this group yet. Use + to add ComfyUI or another tool." : "No discussion members in this group.")}</div>}
        </section>;
      })}
      {err && <div className="err mdock-err">{t("Could not load member capabilities: {err}", { err })}</div>}
    </div>
  );
}
