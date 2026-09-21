import { useCallback, useEffect, useMemo, useState } from "react";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import { api, type PermMode, type Permissions, type PlanMode, type Settings } from "../api";
import { useData } from "../data";
import { Switch } from "../ui";
import { NumInput, Row, useSettingsSaver } from "./rows";
import type { PageProps } from "./SettingsModal";
import "../styles/perms.css";

const MODES: { id: PermMode; title: string; desc: string; danger?: boolean }[] = [
  { id: "ask_risky", title: "只在执行类操作时问我(推荐)", desc: "插件、没有标明只读的 MCP 工具,每次调用前先问你。检索资料库/记忆、查时间、只读的 MCP 工具直接使用。" },
  { id: "ask_all", title: "每次都问我", desc: "除了查询时间,成员调用任何工具前都先确认。最稳妥,但会频繁打断。" },
  { id: "allow_all", title: "全部自动放行", desc: "成员可以直接调用本群启用的所有工具,包括会执行代码的插件。只有在你完全信任已安装的插件和 MCP 时才选。", danger: true },
];
const POLICY_TEXT = { allow: "直接使用", ask: "先问我", deny: "禁止" } as const;
const PLAN_MODES: { id: Exclude<PlanMode, "inherit">; label: string }[] = [
  { id: "auto", label: "自动" },
  { id: "on", label: "总是分工" },
  { id: "off", label: "不分工" },
];
const PLAN_HINT: Record<Exclude<PlanMode, "inherit">, string> = {
  auto: "复杂任务由群主先出分工计划,简单问题直接回答。",
  on: "每条消息都先由群主出分工计划,再由成员执行。",
  off: "不做分工,按 @ 和默认规则直接回复。",
};

type Override = "default" | "allow" | "deny";

export default function PermissionsPage({ onTab }: PageProps) {
  const { settings } = useData();
  const { set, err: saveErr, saving } = useSettingsSaver();
  const [perm, setPerm] = useState<Permissions | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const err = saveErr || loadErr;

  const load = useCallback(() => api.permissions().then(setPerm).catch((e) => setLoadErr((e as Error).message)), []);
  useEffect(() => { void load(); }, [load, settings?.perm_mode, settings?.perm_allow, settings?.perm_deny, settings?.tool_rounds]);

  const groups = useMemo(() => {
    const m = new Map<string, NonNullable<typeof perm>["tools"]>();
    for (const t of perm?.tools ?? []) m.set(t.group, [...(m.get(t.group) ?? []), t]);
    return [...m.entries()];
  }, [perm]);

  if (!settings || !perm) return <div className="empty big">{err || "加载中…"}</div>;

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
      <h2 className="sp-title"><ShieldCheck size={20} aria-hidden /> 权限与操控</h2>
      <p className="sp-desc">决定成员能自己做什么、什么时候必须先问你。修改后即时生效。</p>
      {err && <div className="ext-errbox ext-sticky-err" role="alert"><div className="err">保存失败:{err}</div></div>}

      <div className="sec">工具调用审批</div>
      <div className="pm-modes" role="radiogroup" aria-label="工具调用审批模式">
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
        <Row title="等你确认的时长" desc="成员要用需要确认的工具时,聊天窗口底部会出现确认条。超过这个时间没答复,按「拒绝」处理。">
          <NumInput v={settings.perm_timeout} min={10} max={600} unit="秒" label="等你确认的时长" onCommit={(n) => set({ perm_timeout: n })} />
        </Row>
      </div>
      <p className="pm-note">
        「只读」的判断:内置的检索类工具由本程序判定;MCP 工具是否只读由该 MCP 服务器自己声明,程序无法核实。对不完全信任的服务器,请选「每次都问我」。
      </p>

      <div className="sec">各工具当前的处理方式</div>
      {groups.map(([g, tools]) => (
        <div key={g} className="card flush pm-group">
          <div className="pm-group-title">{g}</div>
          {tools.map((t) => {
            const ov = override(t.name);
            return (
              <div key={t.name} className="pm-tool">
                <span className="pm-tname" title={t.name}>{t.name}</span>
                <span className={"pm-risk " + t.risk}>{t.risk_label}</span>
                <span className={"pm-now " + t.policy}>{POLICY_TEXT[t.policy]}</span>
                <select value={ov} disabled={saving} aria-label={`${t.name} 的处理方式`} onChange={(e) => void setOverride(t.name, e.target.value as Override)}>
                  <option value="default">按上面的模式</option>
                  <option value="allow">总是允许</option>
                  <option value="deny">永远禁止</option>
                </select>
              </div>
            );
          })}
        </div>
      ))}
      {orphan.length > 0 && (
        <div className="card flush pm-group">
          <div className="pm-group-title">已设置、但当前没有加载的工具</div>
          {orphan.map((n) => (
            <div key={n} className="pm-tool">
              <span className="pm-tname" title={n}>{n}</span>
              <span className="pm-now">{override(n) === "deny" ? "禁止" : "总是允许"}</span>
              <button className="btn small" disabled={saving} onClick={() => void setOverride(n, "default")}>移除设置</button>
            </div>
          ))}
        </div>
      )}
      <p className="pm-note">MCP 服务器连接成功后,它的工具才会列在这里。「永远禁止」优先于所有模式,包括「全部自动放行」。</p>

      <div className="sec">这个应用能访问什么</div>
      <div className="card flush">
        <Row title="允许外呼(联网)" desc={acc.external_calls
          ? `已开启。对话内容会发给你配置了密钥的云端服务商${acc.cloud_providers.length ? `:${acc.cloud_providers.join("、")}` : "(目前没有配置)"};还会检查更新、查询模型清单。`
          : "已关闭。不会向任何云端服务商发请求,也不检查更新;本地 Ollama 等本机模型仍可使用。"}>
          <Switch checked={acc.external_calls} label="允许外呼" onChange={(v) => void set({ external_calls_enabled: v })} />
        </Row>
        <Row title="本机文件" desc={`应用自己的数据存放在 ${acc.data_dir}(聊天记录、记忆、资料库、技能、插件)。成员本身读不到你电脑上的其他文件——除非你安装的插件或 MCP(例如文件系统类)提供了这个能力,而那类调用会按上面的规则先问你。`}>
        </Row>
        <Row title={`插件(${acc.plugins.length})`} desc={acc.plugins.length ? acc.plugins.map((p) => `${p.name}(${p.tools} 个工具${p.error ? ",加载出错" : ""})`).join("、") + "。插件是 Python 代码,以本程序的权限运行,只安装你看过、信任的。" : "没有安装插件。"}>
          <button className="btn small" onClick={() => onTab("plugins")}>管理</button>
        </Row>
        <Row title={`MCP 服务器(${acc.mcp.length})`} desc={acc.mcp.length ? acc.mcp.map((m) => `${m.name}(${m.kind}${m.enabled ? "" : ",已停用"})`).join("、") + "。本地进程类的服务器会在你的电脑上运行命令。" : "没有配置 MCP 服务器。"}>
          <button className="btn small" onClick={() => onTab("mcp")}>管理</button>
        </Row>
        <Row title="哪些群启用了插件 / MCP" desc={acc.groups.length ? acc.groups.map((g) => `${g.name}(${g.plugins} 个插件,${g.mcp} 个 MCP)`).join("、") : "没有群启用。插件和 MCP 只有在群里勾选后,该群的成员才能看到并调用。"}>
        </Row>
      </div>

      <div className="sec">自动操控</div>
      <div className="card flush">
        <Row title="分工模式" desc={PLAN_HINT[settings.plan_mode] ?? ""}>
          <select className="ext-plan-select" value={settings.plan_mode} aria-label="分工模式" onChange={(e) => void set({ plan_mode: e.target.value as Settings["plan_mode"] })}>
            {PLAN_MODES.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          </select>
        </Row>
        <Row title="分工任务数上限" desc="群主一次分工最多拆成几项任务。">
          <NumInput v={settings.plan_max_tasks} min={2} max={12} unit="项" label="分工任务数上限" onCommit={(n) => set({ plan_max_tasks: n })} />
        </Row>
        <Row title="单条消息最多发言轮数" desc="成员之间互相 @ 时的接力上限,防止无限循环。">
          <NumInput v={settings.max_hops} min={1} max={30} unit="轮" label="单条消息最多发言轮数" onCommit={(n) => set({ max_hops: n })} />
        </Row>
        <Row title="每条回复最多调用几轮工具" desc="成员回复时可以先调用工具再作答。0 表示完全不让成员使用任何工具。">
          <NumInput v={settings.tool_rounds} min={0} max={10} unit="轮" label="每条回复最多调用几轮工具" onCommit={(n) => set({ tool_rounds: n })} />
        </Row>
        <Row title="单次工具调用超时" desc="工具(内置、插件、MCP)超过这个时间没返回,就当作失败。">
          <NumInput v={settings.tool_timeout} min={5} max={600} unit="秒" label="单次工具调用超时" onCommit={(n) => set({ tool_timeout: n })} />
        </Row>
      </div>
      <p className="pm-note">每个群也可以在群聊右侧面板里单独设置分工模式,默认跟随这里。无论怎么设置,<b>安装插件、添加或启用 MCP 服务器、更新程序本体</b>都不会自动进行,一定要你手动确认。</p>

      <div className="sec">浏览器</div>
      <div className="card pm-browser">
        <div>
          <div className="sr-title">没有内置的浏览器操控</div>
          <div className="sr-desc">Team Agent 自己不带浏览器,也不会控制你的 Chrome。想让成员上网查资料或操作网页,可以在 MCP 里添加一个浏览器类的服务器(例如 Playwright 的 MCP)。它作为 MCP 工具工作,同样遵守上面的审批规则:没有标明只读的操作默认每次先问你。</div>
        </div>
        <button className="btn small" onClick={() => onTab("mcp")}>去添加 MCP</button>
      </div>
    </div>
  );
}
