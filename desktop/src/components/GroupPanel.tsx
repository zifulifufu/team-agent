import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Capabilities, type Group } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import type { SettingsTab } from "../settings/SettingsModal";
import ExtTab from "./group/ExtTab";
import PromptTab from "./group/PromptTab";
import "../styles/chat.css";

export type PanelTab = "ext" | "prompt";

// Both spellings live here; the component picks one, because a module-level `tr()` would
// be evaluated before the language provider is mounted.
const TABS: { id: PanelTab; label: string; labelZh: string }[] = [
  { id: "ext", label: "Skills · plugins · MCP", labelZh: "技能 · 插件 · MCP" },
  { id: "prompt", label: "Prompts", labelZh: "提示词" },
];

/**
 * This group's capability list (the model and strengths each member really uses, the tools in reach, problems).
 * Re-fetched when the members, host, extension settings or member profiles change; there is also a manual refresh.
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
  const { t, pick } = useI18n();
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const onKey = (e: React.KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const n = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length].id;
    onTab(n);
    tabRefs.current[n]?.focus();
  };
  return (
    <aside className="gp" aria-label={t("Group settings panel")}>
      <div className="gp-tabs" role="tablist">
        {TABS.map((tb, i) => (
          <button
            key={tb.id}
            ref={(el) => { tabRefs.current[tb.id] = el; }}
            role="tab"
            id={"gp-tab-" + tb.id}
            aria-selected={tab === tb.id}
            aria-controls={"gp-pane-" + tb.id}
            tabIndex={tab === tb.id ? 0 : -1}
            className={tab === tb.id ? "on" : ""}
            onClick={() => onTab(tb.id)}
            onKeyDown={(e) => onKey(e, i)}
          >
            {pick(tb.label, tb.labelZh)}
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
