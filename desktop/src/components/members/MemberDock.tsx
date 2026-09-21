import { useMemo } from "react";
import { useData } from "../../data";
import type { Group } from "../../api";
import { useCapabilities } from "../GroupPanel";
import MemberCard, { type MemberRow } from "./MemberCard";

/** 侧边栏「成员」下面:当前群的成员列表(随时可展开、换模型、移出)。 */
export default function MemberDock({ group }: { group: Group }) {
  const { agents } = useData();
  const { caps, err } = useCapabilities(group);

  // 能力清单还没回来时,先用本地数据顶上,避免列表闪空
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
    <div className="mdock" aria-label={`「${group.name}」的成员`}>
      {rows.length === 0 && <div className="side-empty">本群还没有成员,点上面的 + 添加</div>}
      {rows.map((m) => <MemberCard key={m.agent_id} group={group} m={m} hasCaps={!!caps} />)}
      {err && <div className="err mdock-err">读取成员能力失败:{err}</div>}
    </div>
  );
}
