import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Info, TriangleAlert } from "lucide-react";
import { api, type Capabilities, type Group, type GroupExt, type LibraryDoc, type LibraryMode, type McpServer, type McpTemplate, type PlanMode, type PluginInfo, type Skill } from "../../api";
import { useData } from "../../data";
import { Switch } from "../../ui";
import type { SettingsTab } from "../../settings/SettingsModal";
import { SkillDialog } from "../../settings/SkillsPage";
import { EMPTY_INIT, McpDialog, hasPlaceholder, type FormInit } from "../../settings/McpPage";
import { RepoDiscoverModal } from "../RepoDiscover";
import AddMenu from "./AddMenu";
import { tr, useI18n } from "../../i18n";
import "../../styles/ext.css";

interface Props {
  group: Group;
  caps: Capabilities | null;
  capsErr: string;
  refreshCaps: () => void;
  active: boolean;
  onSettings: (t: SettingsTab) => void;
}

const PLAN_OPTIONS: { v: PlanMode; label: string }[] = [
  { v: "inherit", label: "Follow the global setting" },
  { v: "auto", label: "Auto (only split complex tasks)" },
  { v: "on", label: "Always split" },
  { v: "off", label: "Never split" },
];
/** Module scope, so these use `tr` and stay reactive to the current language. */
const GLOBAL_PLAN_LABEL: Record<string, string> = { auto: "Auto", on: "Always split", off: "Never split" };
const SOURCE_LABEL: Record<string, string> = { builtin: "Built-in", plugin: "Plugin", mcp: "MCP" };

function Check({ id, checked, disabled, onChange, children }: { id: string; checked: boolean; disabled?: boolean; onChange: (v: boolean) => void; children: ReactNode }) {
  return (
    <label className={"gp-check" + (checked ? " on" : "") + (disabled ? " off" : "")} htmlFor={id}>
      <input id={id} type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="gp-check-body">{children}</span>
    </label>
  );
}

function Section({ title, note, children, right }: { title: string; note?: ReactNode; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="gp-block">
      <div className="gp-sec">
        {title}
        <span className="grow" />
        {right}
      </div>
      {note && <div className="gp-note">{note}</div>}
      {children}
    </section>
  );
}

export default function ExtTab({ group, caps, capsErr, refreshCaps, active, onSettings }: Props) {
  const { t } = useI18n();
  const { reloadGroups, reloadUpdates, settings } = useData();
  const reloadUpdatesSafe = () => { void reloadUpdates(); };
  const gid = group.id;
  const [ext, setExt] = useState<GroupExt>(group.ext);
  const extRef = useRef(ext);
  extRef.current = ext;
  const [err, setErr] = useState("");
  const [saving, setSaving] = useState(false);

  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [plugins, setPlugins] = useState<PluginInfo[] | null>(null);
  const [mcp, setMcp] = useState<McpServer[] | null>(null);
  const [docs, setDocs] = useState<LibraryDoc[] | null>(null);
  const [loadErr, setLoadErr] = useState<Record<string, string>>({});
  const [templates, setTemplates] = useState<McpTemplate[]>([]);
  // Add things straight from this panel: a new skill, an MCP server, or discovery
  // from GitHub.
  const [skillDlg, setSkillDlg] = useState(false);
  const [mcpDlg, setMcpDlg] = useState<FormInit | null>(null);
  const [discover, setDiscover] = useState<"skill" | "plugin" | "mcp" | null>(null);
  const [note, setNote] = useState("");

  // The group itself was changed elsewhere (switched, or reloaded after applying a
  // prompt): sync the new value in.
  useEffect(() => setExt(group.ext), [group.ext]);

  const load = useCallback(() => {
    const fail = (k: string) => (e: unknown) => setLoadErr((o) => ({ ...o, [k]: (e as Error).message }));
    const ok = (k: string) => setLoadErr((o) => (k in o ? Object.fromEntries(Object.entries(o).filter(([x]) => x !== k)) : o));
    api.skills().then((v) => { setSkills(v); ok("skills"); }).catch(fail("skills"));
    api.plugins().then((v) => { setPlugins(v); ok("plugins"); }).catch(fail("plugins"));
    api.mcp().then((v) => { setMcp(v); ok("mcp"); }).catch(fail("mcp"));
    api.library().then((v) => { setDocs(v.docs); ok("docs"); }).catch(fail("docs"));
    api.mcpTemplates().then(setTemplates).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (active) load();
  }, [active, load]);

  /** Save straight away: update optimistically first, and on failure roll back only
   *  the fields this call changed, showing the backend error verbatim. Saves are
   *  queued so that a failed rollback cannot undo a later, successful change when
   *  the user clicks twice in a row. */
  const queue = useRef<Promise<void>>(Promise.resolve());
  const save = (patch: Partial<GroupExt>): Promise<void> => {
    const keys = Object.keys(patch) as (keyof GroupExt)[];
    const before = Object.fromEntries(keys.map((k) => [k, extRef.current[k]])) as Partial<GroupExt>;
    const next = { ...extRef.current, ...patch };
    extRef.current = next;
    setExt(next);
    setSaving(true);
    setErr("");
    queue.current = queue.current.then(async () => {
      try {
        await api.patchGroup(gid, { ext: patch });
        await reloadGroups();
        refreshCaps();
      } catch (e) {
        const back = { ...extRef.current, ...before };
        extRef.current = back;
        setExt(back);
        setErr((e as Error).message);
      }
    }).finally(() => setSaving(false));
    return queue.current;
  };
  const toggle = (key: "skills" | "plugins" | "mcp", id: string, on: boolean) => {
    const cur = extRef.current[key];
    void save({ [key]: on ? [...cur.filter((x) => x !== id), id] : cur.filter((x) => x !== id) });
  };
  const lib = ext.library;
  const setLib = (mode: LibraryMode, ids = extRef.current.library.ids) => void save({ library: { mode, ids } });
  const toggleDoc = (id: string, on: boolean) => setLib("selected", on ? [...extRef.current.library.ids.filter((x) => x !== id), id] : extRef.current.library.ids.filter((x) => x !== id));

  const groupSkills = (skills ?? []).filter((s) => s.scope === "group");
  const memberSkills = (skills ?? []).filter((s) => s.scope !== "group");
  const globalPlan = settings ? GLOBAL_PLAN_LABEL[settings.plan_mode] ?? settings.plan_mode : "";

  const toolGroups = ["builtin", "plugin", "mcp"]
    .map((src) => ({ src, list: (caps?.tools ?? []).filter((t) => t.source === src) }))
    .filter((g) => g.list.length > 0);
  const otherTools = (caps?.tools ?? []).filter((t) => !["builtin", "plugin", "mcp"].includes(t.source));
  if (otherTools.length) toolGroups.push({ src: "other", list: otherTools });

  const empty = (what: string, tab: SettingsTab) => (
    <div className="gp-none">
      {t("No {what} yet.", { what })}
      <button className="link-btn" onClick={() => onSettings(tab)}>{t("Add one in Settings")}</button>
    </div>
  );

  return (
    <div className="gp-scroll">
      <div className="gp-status" aria-live="polite">{saving ? t("Saving…") : t("Changes save immediately and apply to the whole group")}</div>
      {err && <div className="err gp-err" role="alert">{err}</div>}
      {note && <div className="ok-text gp-err" role="status">{note}</div>}

      <Section
        title={t("Skills")}
        note={t("Ticking one applies it to the whole group; use \"Add\" to create a new one or find one on GitHub.")}
        right={
          <AddMenu
            label={t("Add skill")}
            items={[
              { key: "new", label: t("New skill…"), hint: t("Write your own instructions and attach them to this group"), onClick: () => setSkillDlg(true) },
              { key: "gh", label: t("Discover on GitHub…"), hint: t("Plain-text skills; installing one never runs code"), onClick: () => setDiscover("skill") },
            ]}
          />
        }
      >
        {loadErr.skills && <div className="err gp-err">{loadErr.skills}</div>}
        {skills === null && !loadErr.skills && <div className="gp-none">{t("Loading…")}</div>}
        {skills !== null && skills.length === 0 && empty(t("skills"), "skills")}
        {groupSkills.length > 0 && <div className="gp-sub">{t("Group rules")}</div>}
        {groupSkills.map((s) => (
          <Check key={s.name} id={"gp-sk-" + s.name} checked={ext.skills.includes(s.name)} onChange={(v) => toggle("skills", s.name, v)}>
            <b>{s.name}</b>
            {s.description && <small>{s.description}</small>}
          </Check>
        ))}
        {memberSkills.length > 0 && <div className="gp-sub">{t("Member skills")}</div>}
        {memberSkills.map((s) => (
          <Check key={s.name} id={"gp-sk-" + s.name} checked={ext.skills.includes(s.name)} onChange={(v) => toggle("skills", s.name, v)}>
            <b>{s.name}</b>
            {s.description && <small>{s.description}</small>}
          </Check>
        ))}
      </Section>

      <Section
        title={t("Plugins")}
        note={t("A plugin is Python code that runs on this machine. You are shown the full source before installing, and it only affects this group once you tick it.")}
        right={
          <AddMenu
            label={t("Add plugin")}
            items={[
              { key: "gh", label: t("Install from GitHub…"), hint: t("Preview the source before installing"), onClick: () => setDiscover("plugin") },
              { key: "reload", label: t("Reload"), hint: t("Use this after dropping a .py file into the plugin folder"), onClick: () => { setNote(""); api.reloadPlugins().then((r) => { setPlugins(r); setNote(t("Reloaded: {n} plugins.", { n: r.length })); }).catch((e) => setErr((e as Error).message)); } },
            ]}
          />
        }
      >
        {loadErr.plugins && <div className="err gp-err">{loadErr.plugins}</div>}
        {plugins === null && !loadErr.plugins && <div className="gp-none">{t("Loading…")}</div>}
        {plugins !== null && plugins.length === 0 && empty(t("plugins"), "plugins")}
        {(plugins ?? []).map((p) => {
          const bad = !!p.error;
          return (
            <Check key={p.id} id={"gp-pl-" + p.id} checked={!bad && ext.plugins.includes(p.id)} disabled={bad} onChange={(v) => toggle("plugins", p.id, v)}>
              <b>{p.name || p.id}</b>
              <span className="gp-inline-meta">{bad ? "" : t("{n} tools", { n: p.tools.length })}</span>
              {bad ? <small className="danger-text">{p.error}</small> : p.description && <small>{p.description}</small>}
            </Check>
          );
        })}
      </Section>

      <Section
        title="MCP"
        note={t("An MCP server is only connected the first time it is used.")}
        right={
          <AddMenu
            label={t("Add MCP server")}
            items={[
              { key: "new", label: t("Add by hand…"), hint: t("Enter a command or a remote address and attach it to this group"), onClick: () => setMcpDlg({ ...EMPTY_INIT }) },
              ...templates.map((tpl) => ({
                key: "tpl:" + tpl.name,
                label: tr("Template · {name}", { name: tpl.name }),
                hint: tpl.args.some(hasPlaceholder) ? tr("Needs a path change") : tpl.note.slice(0, 20),
                onClick: () => setMcpDlg({ ...EMPTY_INIT, name: tpl.name, description: tpl.note, command: tpl.command, args: tpl.args }),
              })),
              { key: "gh", label: t("Discover on GitHub…"), onClick: () => setDiscover("mcp") },
            ]}
          />
        }
      >
        {loadErr.mcp && <div className="err gp-err">{loadErr.mcp}</div>}
        {mcp === null && !loadErr.mcp && <div className="gp-none">{t("Loading…")}</div>}
        {mcp !== null && mcp.length === 0 && empty(t("MCP servers"), "mcp")}
        {(mcp ?? []).map((s) => {
          const checked = ext.mcp.includes(s.id);
          const dot = s.status === "ready" ? "ok" : s.status === "error" ? "bad" : "off";
          const stText = s.status === "ready" ? t("Connected") : s.status === "error" ? t("Connection failed")
            : s.status === "connecting" ? t("Connecting") : t("Not connected");
          return (
            <Check key={s.id} id={"gp-mcp-" + s.id} checked={checked} disabled={!s.enabled && !checked} onChange={(v) => toggle("mcp", s.id, v)}>
              <b>{s.name}</b>
              <span className="gp-inline-meta">
                <i className={"dot " + dot} title={stText} aria-label={stText} />
                {s.transport_effective}
              </span>
              {!s.enabled && <small>{t("Disabled in Settings")}</small>}
              {s.status === "error" && s.error && <small className="danger-text">{s.error}</small>}
              {s.description && s.status !== "error" && <small>{s.description}</small>}
            </Check>
          );
        })}
      </Section>

      <Section title={t("Library")} note={caps ? t("{n} searchable documents. Members search them when they need to; the whole library is never stuffed into the prompt.", { n: caps.docs }) : undefined}>
        <div className="seg gp-seg" role="group" aria-label={t("Library scope")}>
          {(
            [
              ["all", t("All")],
              ["selected", t("Selected only")],
              ["off", t("Off")],
            ] as [LibraryMode, string][]
          ).map(([v, l]) => (
            <button key={v} className={lib.mode === v ? "on" : ""} aria-pressed={lib.mode === v} onClick={() => lib.mode !== v && setLib(v)}>
              {l}
            </button>
          ))}
        </div>
        {lib.mode === "selected" && (
          <div className="gp-doclist">
            {loadErr.docs && <div className="err gp-err">{loadErr.docs}</div>}
            {docs === null && !loadErr.docs && <div className="gp-none">{t("Loading…")}</div>}
            {docs !== null && docs.length === 0 && <div className="gp-none">{t("The library has no documents yet.")}</div>}
            {(docs ?? []).map((d) => (
              <Check key={d.id} id={"gp-doc-" + d.id} checked={lib.ids.includes(d.id)} onChange={(v) => toggleDoc(d.id, v)}>
                <b>{d.title}</b>
                {!d.enabled && <small>{t("Disabled in the library")}</small>}
                {d.enabled && <small>{d.kind} · {t("{n} chars", { n: d.chars })}</small>}
              </Check>
            ))}
            {docs !== null && docs.length > 0 && lib.ids.length === 0 && <div className="gp-note warn">{t("No documents selected yet, so this group cannot search any material.")}</div>}
          </div>
        )}
      </Section>

      <Section title={t("Memory")}>
        <div className="gp-switch-row">
          <div>
            <div className="gp-sw-title">{t("Use memory in this group")}</div>
            <div className="gp-note">{t("When on, members can search it and note down preferences and decisions as needed.")}</div>
          </div>
          <Switch checked={ext.memory} onChange={(v) => void save({ memory: v })} label={t("Use memory in this group")} />
        </div>
        {settings && !settings.memory_enabled && <div className="gp-note warn">{t("The global memory switch is off, so turning it on here has no effect.")}</div>}
      </Section>

      <Section title={t("Task splitting")}>
        <label className="gp-select">
          <span className="sr-only">{t("Task splitting")}</span>
          <select value={ext.plan} onChange={(e) => void save({ plan: e.target.value as PlanMode })} aria-label={t("Task splitting")}>
            {PLAN_OPTIONS.map((o) => (
              <option key={o.v} value={o.v}>
                {o.label}
                {o.v === "inherit" && globalPlan ? t("(currently: {value})", { value: globalPlan }) : ""}
              </option>
            ))}
          </select>
        </label>
        <div className="gp-note">{t("Always split: every task starts with a plan board. Never split: the host answers directly or names members with @.")}</div>
      </Section>

      <Section title={t("Tools available in this group")} right={caps ? <span className="count-badge-lite">{caps.tools.length}</span> : null}>
        {caps && caps.problems.length > 0 && (
          <div className="gp-problems" role="alert">
            <TriangleAlert size={13} aria-hidden />
            <div>
              {caps.problems.map((p, i) => (
                <div key={i}>{p}</div>
              ))}
              {/* `mcp_deferred` is a flag from the backend: the problem text follows the request
                  language, so matching on it would stop working once the language changes. */}
              {caps.mcp_deferred && (
                <div className="gp-problem-note">{t("An MCP server is only connected the first time it is used, so \"not connected yet\" is not an error.")}</div>
              )}
            </div>
          </div>
        )}
        {capsErr && <div className="err gp-err">{t("Could not load this group's capabilities: {err}", { err: capsErr })}</div>}
        {!caps && !capsErr && <div className="gp-none">{t("Loading…")}</div>}
        {caps && toolGroups.length === 0 && <div className="gp-none">{t("No tools are available in this group yet.")}</div>}
        {toolGroups.map((g) => (
          <div key={g.src} className="gp-toolgroup">
            <div className="gp-sub">{t(SOURCE_LABEL[g.src] ?? "Other")} · {g.list.length}</div>
            {/* `tool`, not `t`: a callback parameter named `t` would shadow the translate function */}
            {g.list.map((tool) => (
              <div key={tool.name} className="gp-tool" title={tool.description}>
                <code>{tool.name}</code>
                <span>{tool.description}</span>
              </div>
            ))}
          </div>
        ))}
      </Section>

      <div className="gp-foot">
        <Info size={13} aria-hidden />
        <span>{t("Plugins and MCP servers run code on this machine — only enable ones you trust.")}</span>
      </div>

      {skillDlg && (
        <SkillDialog
          skill={null}
          onClose={() => setSkillDlg(false)}
          onSaved={async (name) => { setSkillDlg(false); await Promise.resolve(load()); toggle("skills", name, true); }}
        />
      )}
      {mcpDlg && (
        <McpDialog
          server={null}
          init={mcpDlg}
          onClose={() => setMcpDlg(null)}
          onSaved={async (srv) => { setMcpDlg(null); load(); toggle("mcp", srv.id, true); setNote(t("Added \"{name}\" and attached it to this group (it connects the first time it is used).", { name: srv.name })); }}
        />
      )}
      {discover && (
        <RepoDiscoverModal
          kind={discover}
          onClose={() => setDiscover(null)}
          onInstalled={() => { setDiscover(null); load(); reloadUpdatesSafe(); }}
          onPrefillMcp={(name, description) => { setDiscover(null); setMcpDlg({ ...EMPTY_INIT, name, description }); }}
        />
      )}
    </div>
  );
}
