import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Capabilities, type Group } from "../api";
import { useData } from "../data";
import type { SettingsTab } from "../settings/SettingsModal";
import ExtTab from "./group/ExtTab";
import PromptTab from "./group/PromptTab";
import "../styles/chat.css";

export type PanelTab = "ext" | "prompt";

const TABS: { id: PanelTab; label: string }[] = [
  { id: "ext", label: "技能 · 插件 · MCP" },
  { id: "prompt", label: "提示词" },
];

/**
 * 本群的能力清单(成员实际用的模型 / 强项、可用工具、问题)。
 * 群的成员、群主、扩展设置、成员资料变化后自动重新拉取;也可手动 refresh。
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

interface Props {
  group: Group;
  caps: Capabilities | null;
  capsErr: string;
  refreshCaps: () => void;
  tab: PanelTab;
  onTab: (t: PanelTab) => void;
  onSettings: (t: SettingsTab) => void;
}

export default function GroupPanel({ group, caps, capsErr, refreshCaps, tab, onTab, onSettings }: Props) {
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const onKey = (e: React.KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const n = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length].id;
    onTab(n);
    tabRefs.current[n]?.focus();
  };
  return (
    <aside className="gp" aria-label="群聊设置面板">
      <div className="gp-tabs" role="tablist">
        {TABS.map((t, i) => (
          <button
            key={t.id}
            ref={(el) => { tabRefs.current[t.id] = el; }}
            role="tab"
            id={"gp-tab-" + t.id}
            aria-selected={tab === t.id}
            aria-controls={"gp-pane-" + t.id}
            tabIndex={tab === t.id ? 0 : -1}
            className={tab === t.id ? "on" : ""}
            onClick={() => onTab(t.id)}
            onKeyDown={(e) => onKey(e, i)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="gp-pane" role="tabpanel" id="gp-pane-ext" aria-labelledby="gp-tab-ext" hidden={tab !== "ext"}>
        <ExtTab group={group} caps={caps} capsErr={capsErr} refreshCaps={refreshCaps} active={tab === "ext"} onSettings={onSettings} />
      </div>
      <div className="gp-pane" role="tabpanel" id="gp-pane-prompt" aria-labelledby="gp-tab-prompt" hidden={tab !== "prompt"}>
        <PromptTab group={group} active={tab === "prompt"} />
      </div>
    </aside>
  );
}
