import { useMemo } from "react";
import { useData } from "../../data";
import type { Group } from "../../api";
import { useCapabilities } from "../GroupPanel";
import MemberCard, { type MemberRow } from "./MemberCard";
import { useI18n } from "../../i18n";

/** Under "Members" in the sidebar: the current group's members (expandable at any
 * time; switch their model or remove them). */
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
        origin: (a.origin ?? "") as "" | "model", engine: a.engine ?? "", model_problem: "",
      }));
  }, [caps, group, agents]);

  return (
    <div className="mdock" aria-label={t("Members of \"{group}\"", { group: group.name })}>
      {rows.length === 0 && <div className="side-empty">{t("This group has no members yet — use the + above to add one")}</div>}
      {rows.map((m) => <MemberCard key={m.agent_id} group={group} m={m} hasCaps={!!caps} />)}
      {err && <div className="err mdock-err">{t("Could not load member capabilities: {err}", { err })}</div>}
    </div>
  );
}
