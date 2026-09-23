import { useCallback, useEffect, useRef, useState } from "react";
import { Settings2, X } from "lucide-react";
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
  /** Open this group's own library in the main area */
  onOpenLibrary: () => void;
  onClose: () => void;
}

/**
 * This group's own settings: which skills, plugins, MCP servers and knowledge bases it uses, and
 * its prompt.
 *
 * It is a **dialog**, not a third column. It used to be a panel pinned to the right, and that put
 * the same four words on screen twice — "skills / plugins / MCP" in this panel and again in the
 * left sidebar, where they are the app-wide pages. Two sidebars with the same headings is how
 * somebody ends up configuring the wrong one; one button in the chat header gives this scope a
 * single entrance, and the left sidebar keeps the global pages.
 */
export default function GroupPanel({ group, caps, capsErr, refreshCaps, tab, onTab, onSettings, onOpenLibrary, onClose }: Props) {
  const { t, pick } = useI18n();
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const onKey = (e: React.KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const n = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length].id;
    onTab(n);
    tabRefs.current[n]?.focus();
  };
  // Escape closes it, the way every other dialog in this app behaves.
  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [onClose]);
  return (
    <div className="gp-back" role="dialog" aria-modal="true" aria-label={t("Group settings panel")}>
      <aside className="gp">
        <div className="gp-head">
          <Settings2 size={15} />
          <b>{t("This group's settings")}</b>
          <span className="muted small">{group.name}</span>
          <button className="icon-btn" onClick={onClose} title={t("Close")} aria-label={t("Close")}>
            <X size={14} />
          </button>
        </div>
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
          <ExtTab group={group} caps={caps} capsErr={capsErr} refreshCaps={refreshCaps} active={tab === "ext"} onSettings={onSettings} onOpenLibrary={onOpenLibrary} />
        </div>
        <div className="gp-pane" role="tabpanel" id="gp-pane-prompt" aria-labelledby="gp-tab-prompt" hidden={tab !== "prompt"}>
          <PromptTab group={group} active={tab === "prompt"} />
        </div>
      </aside>
    </div>
  );
}
