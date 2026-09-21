import { useCallback, useEffect, useMemo, useState } from "react";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import { api, type PermMode, type Permissions, type PlanMode, type Settings } from "../api";
import { useData } from "../data";
import { tr, useI18n } from "../i18n";
import { Switch } from "../ui";
import { NumInput, Row, useSettingsSaver } from "./rows";
import type { PageProps } from "./SettingsModal";
import "../styles/perms.css";

const MODES: { id: PermMode; title: string; desc: string; danger?: boolean }[] = [
  { id: "ask_risky", title: tr("Ask me only for actions that do something (recommended)"), desc: tr("Plugins and MCP tools that do not declare themselves read-only ask you before every call. Searching the library/memory, checking the time, and read-only MCP tools run directly.") },
  { id: "ask_all", title: tr("Ask me every time"), desc: tr("Members confirm before calling any tool other than the clock. Safest, but it interrupts often.") },
  { id: "allow_all", title: tr("Allow everything automatically"), desc: tr("Members can call any tool enabled in this group, including plugins that execute code. Only choose this if you fully trust the plugins and MCP servers you installed."), danger: true },
];
const POLICY_TEXT = { allow: tr("Runs directly"), ask: tr("Asks me first"), deny: tr("Blocked") } as const;
const PLAN_MODES: { id: Exclude<PlanMode, "inherit">; label: string }[] = [
  { id: "auto", label: tr("Auto") },
  { id: "on", label: tr("Always split") },
  { id: "off", label: tr("Never split") },
];
const PLAN_HINT: Record<Exclude<PlanMode, "inherit">, string> = {
  auto: tr("For complex tasks the host drafts a split plan first; simple questions are answered directly."),
  on: tr("Every message gets a split plan from the host before the members act."),
  off: tr("No splitting — replies follow @mentions and the default rules."),
};

type Override = "default" | "allow" | "deny";

export default function PermissionsPage({ onTab }: PageProps) {
  const { t } = useI18n();
  const { settings } = useData();
  const { set, err: saveErr, saving } = useSettingsSaver();
  const [perm, setPerm] = useState<Permissions | null>(null);
  const [loadErr, setLoadErr] = useState("");
  // The workspace path is edited locally and committed on blur: saving every keystroke would
  // fire a request per character, and a half-typed path is not a value worth storing.
  const [workdir, setWorkdir] = useState<string | null>(null);
  const err = saveErr || loadErr;

  const load = useCallback(() => api.permissions().then(setPerm).catch((e) => setLoadErr((e as Error).message)), []);
  useEffect(() => { void load(); }, [load, settings?.perm_mode, settings?.perm_allow, settings?.perm_deny, settings?.tool_rounds]);

  const groups = useMemo(() => {
    const m = new Map<string, NonNullable<typeof perm>["tools"]>();
    for (const tool of perm?.tools ?? []) m.set(tool.group, [...(m.get(tool.group) ?? []), tool]);
    return [...m.entries()];
  }, [perm]);

  if (!settings || !perm) return <div className="empty big">{err || t("Loading…")}</div>;

  const override = (name: string): Override => (settings.perm_deny.includes(name) ? "deny" : settings.perm_allow.includes(name) ? "allow" : "default");
  const setOverride = (name: string, v: Override) =>
    set({
      perm_allow: [...settings.perm_allow.filter((x) => x !== name), ...(v === "allow" ? [name] : [])],
      perm_deny: [...settings.perm_deny.filter((x) => x !== name), ...(v === "deny" ? [name] : [])],
    });
  const known = new Set(perm.tools.map((t) => t.name));
  const orphan = [...settings.perm_allow, ...settings.perm_deny].filter((n, i, a) => !known.has(n) && a.indexOf(n) === i);
  const acc = perm.access;

  return (
    <div className="sp perms">
      <h2 className="sp-title"><ShieldCheck size={20} aria-hidden /> {t("Permissions & control")}</h2>
      <p className="sp-desc">{t("Decides what members may do on their own and when they must ask you first. Changes take effect immediately.")}</p>
      {err && <div className="ext-errbox ext-sticky-err" role="alert"><div className="err">{t("Save failed: {err}", { err })}</div></div>}

      <div className="sec">{t("Tool call approval")}</div>
      <div className="pm-modes" role="radiogroup" aria-label={t("Tool call approval mode")}>
        {MODES.map((m) => (
          <label key={m.id} className={"pm-mode" + (settings.perm_mode === m.id ? " on" : "") + (m.danger ? " danger" : "")}>
            <input type="radio" name="perm_mode" checked={settings.perm_mode === m.id} onChange={() => void set({ perm_mode: m.id })} />
            <span>
              <span className="pm-title">{m.title}{m.danger && <ShieldAlert size={13} aria-hidden />}</span>
              <span className="pm-desc">{m.desc}</span>
            </span>
          </label>
        ))}
      </div>
      <div className="card flush" style={{ marginTop: 10 }}>
        <Row title={t("How long to wait for your confirmation")} desc={t("When a member needs a tool that requires confirmation, a bar appears at the bottom of the chat. If you do not answer within this time, it counts as a refusal.")}>
          <NumInput v={settings.perm_timeout} min={10} max={600} unit={t("sec")} label={t("How long to wait for your confirmation")} onCommit={(n) => set({ perm_timeout: n })} />
        </Row>
      </div>
      <p className="pm-note">
        {t("What counts as read-only: built-in retrieval tools are classified by this app, while MCP tools declare their own status and the app cannot verify it. For a server you do not fully trust, choose Ask me every time.")}
      </p>

      <div className="sec">{t("How each tool is currently handled")}</div>
      {groups.map(([g, tools]) => (
        <div key={g} className="card flush pm-group">
          <div className="pm-group-title">{g}</div>
          {tools.map((tool) => {
            const ov = override(tool.name);
            return (
              <div key={tool.name} className="pm-tool">
                <span className="pm-tname" title={tool.name}>{tool.name}</span>
                <span className={"pm-risk " + tool.risk}>{tool.risk_label}</span>
                <span className={"pm-now " + tool.policy}>{POLICY_TEXT[tool.policy]}</span>
                <select value={ov} disabled={saving} aria-label={t("How {name} is handled", { name: tool.name })} onChange={(e) => void setOverride(tool.name, e.target.value as Override)}>
                  <option value="default">{t("Follow the mode above")}</option>
                  <option value="allow">{t("Always allow")}</option>
                  <option value="deny">{t("Always block")}</option>
                </select>
              </div>
            );
          })}
        </div>
      ))}
      {orphan.length > 0 && (
        <div className="card flush pm-group">
          <div className="pm-group-title">{t("Overrides for tools that are not currently loaded")}</div>
          {orphan.map((n) => (
            <div key={n} className="pm-tool">
              <span className="pm-tname" title={n}>{n}</span>
              <span className="pm-now">{override(n) === "deny" ? t("Blocked") : t("Always allowed")}</span>
              <button className="btn small" disabled={saving} onClick={() => void setOverride(n, "default")}>{t("Remove override")}</button>
            </div>
          ))}
        </div>
      )}
      <p className="pm-note">{t("An MCP server's tools appear here only after it connects. Always block outranks every mode, including Allow everything automatically.")}</p>

      <div className="sec">{t("What this app can access")}</div>
      <div className="card flush">
        <Row title={t("Allow outbound calls (network)")} desc={acc.external_calls
          ? t("On. Conversation content is sent to the cloud providers you set up keys for{list}; updates are checked and the model list is fetched.", { list: acc.cloud_providers.length ? `: ${acc.cloud_providers.join(", ")}` : " (none configured)" })
          : t("Off. No request is sent to any cloud provider and no update check happens; local models such as Ollama still work.")}>
          <Switch checked={acc.external_calls} label={t("Allow outbound calls")} onChange={(v) => void set({ external_calls_enabled: v })} />
        </Row>
        <Row title={t("Local files")} desc={t("The app keeps its own data in {dir} (chats, memories, library, skills, plugins). Members cannot read other files on your computer unless a plugin or MCP server you installed (a filesystem one, say) provides that ability — and those calls ask you first under the rules above.", { dir: acc.data_dir })}>
        </Row>
        <Row title={t("Plugins ({n})", { n: acc.plugins.length })} desc={acc.plugins.length ? acc.plugins.map((p) => t("{name} ({n} tools{err})", { name: p.name, n: p.tools, err: p.error ? t(", failed to load") : "" })).join(", ") + t(". Plugins are Python code that runs with this app's privileges — only install ones you have read and trust.") : t("No plugins installed.")}>
          <button className="btn small" onClick={() => onTab("plugins")}>{t("Manage")}</button>
        </Row>
        <Row title={t("MCP servers ({n})", { n: acc.mcp.length })} desc={acc.mcp.length ? acc.mcp.map((m) => t("{name} ({kind}{off})", { name: m.name, kind: m.kind, off: m.enabled ? "" : t(", disabled") })).join(", ") + t(". Servers that run as local processes execute commands on your computer.") : t("No MCP servers configured.")}>
          <button className="btn small" onClick={() => onTab("mcp")}>{t("Manage")}</button>
        </Row>
        <Row title={t("Which groups enabled plugins / MCP")} desc={acc.groups.length ? acc.groups.map((g) => t("{name} ({plugins} plugins, {mcp} MCP)", { name: g.name, plugins: g.plugins, mcp: g.mcp })).join(", ") : t("No group has enabled any. A plugin or MCP server becomes visible to a group's members only after you tick it for that group.")}>
        </Row>
      </div>

      <div className="sec">{t("Images in the group chat")}</div>
      <div className="card flush">
        <Row title={t("Send attached images to cloud models")} desc={t("Off by default, and separate from allowing outbound calls on purpose: sending text off this machine and sending a picture you attached are different decisions. With it off, only local models that can look at images receive them — every other member is told an image is there that it cannot see, and says so instead of guessing. A message never carries more than {n} images; PNG, JPEG, GIF and WebP up to {mb} MB each.", { n: 10, mb: settings.vision_max_mb })}>
          <Switch checked={settings.vision_cloud} label={t("Send attached images to cloud models")} onChange={(v) => void set({ vision_cloud: v })} />
        </Row>
      </div>

      <div className="sec">{t("Writing and running code")}</div>
      <div className="card flush">
        <Row title={t("Let members write and run code")} desc={t("Off by default. When on, members get a run_code tool: they write a program, it runs on this machine inside the workspace below, and the output comes back to them. There is no sandbox — the code runs with this app's privileges — so it asks you first under the rules above, and a run that exceeds the timeout is killed along with anything it started.")}>
          <Switch checked={settings.code_enabled} label={t("Let members write and run code")} onChange={(v) => void set({ code_enabled: v })} />
        </Row>
        {settings.code_enabled && (
          <>
            <Row title={t("Code run timeout")} desc={t("How long one run may take before it is killed, together with any process it started.")}>
              <NumInput v={settings.code_timeout} min={5} max={600} unit={t("sec")} label={t("Code run timeout")} onCommit={(n) => set({ code_timeout: n })} />
            </Row>
            <Row title={t("Workspace")} desc={t("Runs happen here, and this is the only directory they can use as their working directory. Files written by a run stay here. Leave it empty to use {dir}.", { dir: acc.code_default_dir ?? t("the app's own workspace folder") })}>
              <input className="pm-text" value={workdir ?? settings.code_workdir} placeholder={acc.code_default_dir ?? ""} spellCheck={false} aria-label={t("Workspace")}
                onChange={(e) => setWorkdir(e.target.value)}
                onBlur={() => { const v = workdir; setWorkdir(null); if (v !== null && v !== settings.code_workdir) void set({ code_workdir: v.trim() }); }} />
            </Row>
          </>
        )}
      </div>

      <div className="sec">{t("Automation")}</div>
      <div className="card flush">
        <Row title={t("Split mode")} desc={PLAN_HINT[settings.plan_mode] ?? ""}>
          <select className="ext-plan-select" value={settings.plan_mode} aria-label={t("Split mode")} onChange={(e) => void set({ plan_mode: e.target.value as Settings["plan_mode"] })}>
            {PLAN_MODES.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          </select>
        </Row>
        <Row title={t("Max split tasks")} desc={t("How many tasks the host may split one job into at most.")}>
          <NumInput v={settings.plan_max_tasks} min={2} max={12} unit={t("tasks")} label={t("Max split tasks")} onCommit={(n) => set({ plan_max_tasks: n })} />
        </Row>
        <Row title={t("Max speaking turns per message")} desc={t("The relay limit when members @ each other, to prevent infinite loops.")}>
          <NumInput v={settings.max_hops} min={1} max={30} unit={t("turns")} label={t("Max speaking turns per message")} onCommit={(n) => set({ max_hops: n })} />
        </Row>
        <Row title={t("Max tool rounds per reply")} desc={t("Members may call tools before answering. 0 means members cannot use any tool at all.")}>
          <NumInput v={settings.tool_rounds} min={0} max={10} unit={t("rounds")} label={t("Max tool rounds per reply")} onCommit={(n) => set({ tool_rounds: n })} />
        </Row>
        <Row title={t("Tool call timeout")} desc={t("A tool (built-in, plugin, or MCP) that does not return within this time is treated as failed.")}>
          <NumInput v={settings.tool_timeout} min={5} max={600} unit={t("sec")} label={t("Tool call timeout")} onCommit={(n) => set({ tool_timeout: n })} />
        </Row>
      </div>
      <p className="pm-note">{t("Each group can also set its own split mode in the right-hand panel of the chat, defaulting to this one. Whatever you choose,")}<b>{t("installing plugins, adding or enabling MCP servers, and updating the app itself")}</b>{t("never happen automatically — they always require your explicit confirmation.")}</p>

      <div className="sec">{t("Browser")}</div>
      <div className="card pm-browser">
        <div>
          <div className="sr-title">{t("No built-in browser control")}</div>
          <div className="sr-desc">{t("Team Agent ships without a browser and will not drive your Chrome. To let members look things up online or work with web pages, add a browser-class server under MCP (Playwright's MCP, for example). It works as an MCP tool and follows the same approval rules: operations that are not declared read-only ask you first by default.")}</div>
        </div>
        <button className="btn small" onClick={() => onTab("mcp")}>{t("Add an MCP server")}</button>
      </div>
    </div>
  );
}
