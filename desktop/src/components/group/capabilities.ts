import { useCallback, useEffect, useState } from "react";
import { api, type Capabilities, type Group } from "../../api";
import { useData } from "../../data";

/**
 * 这个群**实际能用**哪些能力:每个成员真正用的模型与强项、够得着的工具、以及有什么问题。
 *
 * ⚠️ 它原来住在 `GroupPanel` 里,而那一整个「本群设置」面板在 2026-09-25 被用户要求删掉了
 * (「技能、MCP、提示词都一样,在这个地方不合适」)。**成员栏仍然要它** —— 成员卡片上写着每个人
 * 用哪个模型、能不能看图。所以它搬到了这里,而不是跟着面板一起消失。
 *
 * 成员、群主、扩展设置或成员档案一变就重取;也留了一个手动刷新。
 */
export function useCapabilities(group: Group | null): { caps: Capabilities | null; err: string; refresh: () => void } {
  const { agents } = useData();
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [err, setErr] = useState("");
  const [tick, setTick] = useState(0);
  const gid = group?.id ?? null;
  const key = group ? JSON.stringify([group.member_ids, group.host_agent_id, group.ext]) : "";
  useEffect(() => {
    if (!gid) return;
    let alive = true;
    api
      .capabilities(gid)
      .then((c) => {
        if (alive) {
          setCaps(c);
          setErr("");
        }
      })
      .catch((e) => alive && setErr((e as Error).message));
    return () => {
      alive = false;
    };
  }, [gid, key, agents, tick]);
  const refresh = useCallback(() => setTick((n) => n + 1), []);
  return { caps, err, refresh };
}
