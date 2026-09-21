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
  { v: "inherit", label: "跟随全局" },
  { v: "auto", label: "自动(复杂任务才分工)" },
  { v: "on", label: "总是分工" },
  { v: "off", label: "不分工" },
];
const GLOBAL_PLAN_LABEL: Record<string, string> = { auto: "自动", on: "总是分工", off: "不分工" };
const SOURCE_LABEL: Record<string, string> = { builtin: "内置", plugin: "插件", mcp: "MCP" };

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
  // 面板里随时添加:新建技能 / 添加 MCP / 从 GitHub 发现
  const [skillDlg, setSkillDlg] = useState(false);
  const [mcpDlg, setMcpDlg] = useState<FormInit | null>(null);
  const [discover, setDiscover] = useState<"skill" | "plugin" | "mcp" | null>(null);
  const [note, setNote] = useState("");

  // 群本身被别处改了(比如切换群、套用提示词后重新加载)时,同步过来
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

  /** 即时保存:先乐观更新,失败只回滚这一次改动的那几项并显示后端原文错误。
   *  保存排成队一个个发:连点两下时,前一次失败的回滚不会把后一次已经成功的改动也冲掉。 */
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
      还没有{what}。
      <button className="link-btn" onClick={() => onSettings(tab)}>去设置里添加</button>
    </div>
  );

  return (
    <div className="gp-scroll">
      <div className="gp-status" aria-live="polite">{saving ? "保存中…" : "修改会立即保存并对全群生效"}</div>
      {err && <div className="err gp-err" role="alert">{err}</div>}
      {note && <div className="ok-text gp-err" role="status">{note}</div>}

      <Section
        title="技能"
        note="勾选后对全群生效;点「添加」随时新建或从 GitHub 找。"
        right={
          <AddMenu
            label="添加技能"
            items={[
              { key: "new", label: "新建技能…", hint: "自己写一段说明,并自动挂到本群", onClick: () => setSkillDlg(true) },
              { key: "gh", label: "从 GitHub 发现…", hint: "纯文本技能,安装后不会执行代码", onClick: () => setDiscover("skill") },
            ]}
          />
        }
      >
        {loadErr.skills && <div className="err gp-err">{loadErr.skills}</div>}
        {skills === null && !loadErr.skills && <div className="gp-none">加载中…</div>}
        {skills !== null && skills.length === 0 && empty("技能", "skills")}
        {groupSkills.length > 0 && <div className="gp-sub">群聊规则</div>}
        {groupSkills.map((s) => (
          <Check key={s.name} id={"gp-sk-" + s.name} checked={ext.skills.includes(s.name)} onChange={(v) => toggle("skills", s.name, v)}>
            <b>{s.name}</b>
            {s.description && <small>{s.description}</small>}
          </Check>
        ))}
        {memberSkills.length > 0 && <div className="gp-sub">成员技能</div>}
        {memberSkills.map((s) => (
          <Check key={s.name} id={"gp-sk-" + s.name} checked={ext.skills.includes(s.name)} onChange={(v) => toggle("skills", s.name, v)}>
            <b>{s.name}</b>
            {s.description && <small>{s.description}</small>}
          </Check>
        ))}
      </Section>

      <Section
        title="插件"
        note="插件是本机运行的 Python 代码:安装前会让你读完整源码,安装后也要你自己勾选才会对本群生效。"
        right={
          <AddMenu
            label="添加插件"
            items={[
              { key: "gh", label: "从 GitHub 安装…", hint: "先预览源码再安装", onClick: () => setDiscover("plugin") },
              { key: "reload", label: "重新加载", hint: "刚把 .py 文件放进插件目录后用", onClick: () => { setNote(""); api.reloadPlugins().then((r) => { setPlugins(r); setNote(`已重新加载,共 ${r.length} 个插件。`); }).catch((e) => setErr((e as Error).message)); } },
            ]}
          />
        }
      >
        {loadErr.plugins && <div className="err gp-err">{loadErr.plugins}</div>}
        {plugins === null && !loadErr.plugins && <div className="gp-none">加载中…</div>}
        {plugins !== null && plugins.length === 0 && empty("插件", "plugins")}
        {(plugins ?? []).map((p) => {
          const bad = !!p.error;
          return (
            <Check key={p.id} id={"gp-pl-" + p.id} checked={!bad && ext.plugins.includes(p.id)} disabled={bad} onChange={(v) => toggle("plugins", p.id, v)}>
              <b>{p.name || p.id}</b>
              <span className="gp-inline-meta">{bad ? "" : `${p.tools.length} 个工具`}</span>
              {bad ? <small className="danger-text">{p.error}</small> : p.description && <small>{p.description}</small>}
            </Check>
          );
        })}
      </Section>

      <Section
        title="MCP"
        note="MCP 在第一次被用到时才会连接。"
        right={
          <AddMenu
            label="添加 MCP 服务器"
            items={[
              { key: "new", label: "手动添加…", hint: "填命令或远程地址,并自动挂到本群", onClick: () => setMcpDlg({ ...EMPTY_INIT }) },
              ...templates.map((t) => ({
                key: "tpl:" + t.name,
                label: "模板 · " + t.name,
                hint: t.args.some(hasPlaceholder) ? "要改路径" : t.note.slice(0, 20),
                onClick: () => setMcpDlg({ ...EMPTY_INIT, name: t.name, description: t.note, command: t.command, args: t.args }),
              })),
              { key: "gh", label: "从 GitHub 发现…", onClick: () => setDiscover("mcp") },
            ]}
          />
        }
      >
        {loadErr.mcp && <div className="err gp-err">{loadErr.mcp}</div>}
        {mcp === null && !loadErr.mcp && <div className="gp-none">加载中…</div>}
        {mcp !== null && mcp.length === 0 && empty("MCP 服务器", "mcp")}
        {(mcp ?? []).map((s) => {
          const checked = ext.mcp.includes(s.id);
          const dot = s.status === "ready" ? "ok" : s.status === "error" ? "bad" : "off";
          const stText = s.status === "ready" ? "已连接" : s.status === "error" ? "连接出错" : s.status === "connecting" ? "连接中" : "未连接";
          return (
            <Check key={s.id} id={"gp-mcp-" + s.id} checked={checked} disabled={!s.enabled && !checked} onChange={(v) => toggle("mcp", s.id, v)}>
              <b>{s.name}</b>
              <span className="gp-inline-meta">
                <i className={"dot " + dot} title={stText} aria-label={stText} />
                {s.transport_effective}
              </span>
              {!s.enabled && <small>已在设置里停用</small>}
              {s.status === "error" && s.error && <small className="danger-text">{s.error}</small>}
              {s.description && s.status !== "error" && <small>{s.description}</small>}
            </Check>
          );
        })}
      </Section>

      <Section title="资料库" note={caps ? `共 ${caps.docs} 篇可检索的文档。成员需要时会自己检索,不会把整库塞进提示词。` : undefined}>
        <div className="seg gp-seg" role="group" aria-label="资料库范围">
          {(
            [
              ["all", "全部"],
              ["selected", "仅选定"],
              ["off", "关闭"],
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
            {docs === null && !loadErr.docs && <div className="gp-none">加载中…</div>}
            {docs !== null && docs.length === 0 && <div className="gp-none">资料库里还没有文档。</div>}
            {(docs ?? []).map((d) => (
              <Check key={d.id} id={"gp-doc-" + d.id} checked={lib.ids.includes(d.id)} onChange={(v) => toggleDoc(d.id, v)}>
                <b>{d.title}</b>
                {!d.enabled && <small>已在资料库里停用</small>}
                {d.enabled && <small>{d.kind} · {d.chars} 字</small>}
              </Check>
            ))}
            {docs !== null && docs.length > 0 && lib.ids.length === 0 && <div className="gp-note warn">还没选文档,本群暂时检索不到任何资料。</div>}
          </div>
        )}
      </Section>

      <Section title="记忆">
        <div className="gp-switch-row">
          <div>
            <div className="gp-sw-title">本群使用记忆</div>
            <div className="gp-note">开启后成员可检索、并在需要时记下偏好与决定。</div>
          </div>
          <Switch checked={ext.memory} onChange={(v) => void save({ memory: v })} label="本群使用记忆" />
        </div>
        {settings && !settings.memory_enabled && <div className="gp-note warn">全局记忆开关目前是关闭的,这里开启也不会生效。</div>}
      </Section>

      <Section title="分工模式">
        <label className="gp-select">
          <span className="sr-only">分工模式</span>
          <select value={ext.plan} onChange={(e) => void save({ plan: e.target.value as PlanMode })} aria-label="分工模式">
            {PLAN_OPTIONS.map((o) => (
              <option key={o.v} value={o.v}>
                {o.label}
                {o.v === "inherit" && globalPlan ? `(当前:${globalPlan})` : ""}
              </option>
            ))}
          </select>
        </label>
        <div className="gp-note">总是分工:每个任务都先出任务板;不分工:群主直接回答或按 @ 点名。</div>
      </Section>

      <Section title="本群可用工具" right={caps ? <span className="count-badge-lite">{caps.tools.length}</span> : null}>
        {caps && caps.problems.length > 0 && (
          <div className="gp-problems" role="alert">
            <TriangleAlert size={13} aria-hidden />
            <div>
              {caps.problems.map((p, i) => (
                <div key={i}>{p}</div>
              ))}
              {caps.problems.some((p) => p.includes("尚未连接")) && (
                <div className="gp-problem-note">MCP 在第一次被用到时才会连接,「尚未连接」不代表出错。</div>
              )}
            </div>
          </div>
        )}
        {capsErr && <div className="err gp-err">读取本群能力失败:{capsErr}</div>}
        {!caps && !capsErr && <div className="gp-none">加载中…</div>}
        {caps && toolGroups.length === 0 && <div className="gp-none">本群暂时没有可用工具。</div>}
        {toolGroups.map((g) => (
          <div key={g.src} className="gp-toolgroup">
            <div className="gp-sub">{SOURCE_LABEL[g.src] ?? "其他"} · {g.list.length}</div>
            {g.list.map((t) => (
              <div key={t.name} className="gp-tool" title={t.description}>
                <code>{t.name}</code>
                <span>{t.description}</span>
              </div>
            ))}
          </div>
        ))}
      </Section>

      <div className="gp-foot">
        <Info size={13} aria-hidden />
        <span>插件和 MCP 会在本机执行代码,只启用你信任的。</span>
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
          onSaved={async (srv) => { setMcpDlg(null); load(); toggle("mcp", srv.id, true); setNote(`已添加「${srv.name}」并挂到本群(第一次用到时才会连接)。`); }}
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
