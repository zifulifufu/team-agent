import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronRight, ExternalLink, FolderOpen, Library, Loader2, Search } from "lucide-react";
import { api, type AwesomeApp, type AwesomeOverview } from "../api";
import { useData } from "../data";
import { useBusy } from "../ui";
import type { PageProps } from "./SettingsModal";
import "../styles/awesome.css";

type Tab = "teams" | "agents" | "skills" | "mcp";

/** 设置 → 示例库:开源项目 awesome-llm-apps 里的团队 / 角色 / 技能 / MCP,一键导入成群聊、成员、提示词、技能。 */
export default function AwesomePage(_: PageProps) {
  const { reload, reloadGroups } = useData();
  const [ov, setOv] = useState<AwesomeOverview | null>(null);
  const [tab, setTab] = useState<Tab>("teams");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busyKey, setBusyKey] = useState("");
  const [path, setPath] = useState("");
  const [refreshing, guard] = useBusy();
  const pick = window.teamAgent?.pickFolder;

  const load = useCallback(() => { api.awesome().then(setOv).catch((e) => setMsg({ ok: false, text: (e as Error).message })); }, []);
  useEffect(load, [load]);

  const act = async (key: string, fn: () => Promise<string>) => {
    if (busyKey) return;
    setBusyKey(key);
    setMsg(null);
    try {
      setMsg({ ok: true, text: await fn() });
      load();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusyKey("");
    }
  };

  const list = useMemo(() => {
    if (!ov) return [];
    const k = q.trim().toLowerCase();
    const hit = (...xs: string[]) => !k || xs.some((x) => x.toLowerCase().includes(k));
    if (tab === "teams") return ov.teams.filter((a) => hit(a.title, a.title_en, a.desc, a.framework));
    if (tab === "agents") return ov.agents.filter((a) => hit(a.title, a.title_en, a.desc, a.framework));
    return [];
  }, [ov, tab, q]);

  if (!ov) return <div className="empty big">{msg?.text || "加载中…"}</div>;
  const counts: Record<Tab, number> = { teams: ov.teams.length, agents: ov.agents.length, skills: ov.skills.length, mcp: ov.mcp.length };
  const TABS: { id: Tab; label: string }[] = [
    { id: "teams", label: "团队" }, { id: "agents", label: "单个角色 / RAG" }, { id: "skills", label: "技能" }, { id: "mcp", label: "MCP" },
  ];

  const refresh = () => guard(async () => {
    setMsg(null);
    try {
      const r = await api.awesomeRefresh(path.trim());
      setMsg({ ok: true, text: `已从本地克隆刷新(提交 ${r.commit || "未知"}):团队 ${r.teams}、单个角色 ${r.agents}、技能 ${r.skills}、MCP ${r.mcp}。` });
      load();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  });

  const AppRow = ({ a }: { a: AwesomeApp }) => {
    const isOpen = open === a.id;
    const tools = [...new Set(a.members.flatMap((m) => m.tools))];
    return (
      <div className={"aw-item" + (isOpen ? " open" : "")}>
        <button className="aw-head" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : a.id)}>
          <ChevronRight size={14} className={"aw-chev" + (isOpen ? " open" : "")} aria-hidden />
          <span className="aw-main">
            <span className="aw-title">{a.title}{a.title !== a.title_en && <small>{a.title_en}</small>}</span>
            <span className="aw-desc">{a.desc}</span>
            <span className="aw-meta">{a.members.length} 个角色 · {a.category}{a.framework ? ` · ${a.framework}` : ""}{a.sequential ? " · 按顺序接力" : ""}</span>
          </span>
        </button>
        {isOpen && (
          <div className="aw-detail">
            <div className="aw-roles">
              {a.members.map((m) => (
                <div key={m.name} className="aw-role"><b>{m.name}</b>{m.role && <span>{m.role}</span>}</div>
              ))}
            </div>
            {tools.length > 0 && (
              <div className="aw-warn"><AlertTriangle size={13} aria-hidden /> 原示例给角色配了工具({tools.slice(0, 6).join("、")}),在这里不一定有。导入的角色提示词里已写明「做不到就直说」。</div>
            )}
            <div className="aw-acts">
              {a.kind === "team" && (
                <button className="btn small primary" disabled={!!busyKey} onClick={() => void act("g" + a.id, async () => {
                  const r = await api.awesomeCreateGroup(a.id);
                  await reload(); await reloadGroups();
                  return `已建好群聊「${r.group.name}」:群主 ${r.host},成员 ${r.members.join("、")}。到左侧群聊列表里打开。`;
                })}>{busyKey === "g" + a.id ? <Loader2 size={12} className="spin" /> : null} 建为群聊</button>
              )}
              <button className="btn small" disabled={!!busyKey} title="把这些角色建成成员(不入群),之后可在任何群里「添加成员」拉进来" onClick={() => void act("m" + a.id, async () => {
                const r = await api.awesomeAddMembers(a.id);
                await reload();
                return `已创建成员:${r.members.map((m) => m.name).join("、")}。在群聊里「添加成员 → 已有成员」拉进群。`;
              })}>创建为成员</button>
              <button className="btn small" disabled={!!busyKey} onClick={() => void act("p" + a.id, async () => {
                const r = await api.awesomeAddPrompts(a.id);
                return r.added.length ? `已存入提示词库 ${r.added.length} 条。` : "这些提示词已经在库里了。";
              })}>存为提示词</button>
            </div>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="sp aw-page">
      <h2 className="sp-title"><Library size={20} aria-hidden /> 示例库</h2>
      <p className="sp-desc">
        融合开源项目 <a href={ov.source.url} target="_blank" rel="noreferrer">awesome-llm-apps <ExternalLink size={11} aria-hidden /></a>(许可证 {ov.source.license},作者 {ov.source.author}):
        把里面的多智能体团队、单个角色、Agent 技能和 MCP 用法,变成这里能直接用的群聊、成员、提示词、技能。
      </p>
      {msg && <div className={"aw-msg " + (msg.ok ? "ok" : "bad")} role={msg.ok ? "status" : "alert"}>{msg.text}</div>}

      <div className="aw-tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id} className={"aw-tab" + (tab === t.id ? " on" : "")} onClick={() => { setTab(t.id); setOpen(null); }}>
            {t.label} <span className="count-badge-lite">{counts[t.id]}</span>
          </button>
        ))}
        {(tab === "teams" || tab === "agents") && (
          <label className="aw-search"><Search size={13} aria-hidden /><input value={q} placeholder="搜索…" onChange={(e) => setQ(e.target.value)} aria-label="搜索示例" /></label>
        )}
      </div>

      {(tab === "teams" || tab === "agents") && (
        <div className="card flush aw-list">
          {list.length === 0 ? <div className="muted small aw-none">没有匹配的示例</div> : list.map((a) => <AppRow key={a.id} a={a} />)}
        </div>
      )}

      {tab === "skills" && (
        <div className="card flush aw-list">
          {ov.skills.map((s) => (
            <div key={s.id} className="aw-item">
              <div className="aw-head static">
                <span className="aw-main">
                  <span className="aw-title">{s.title}{s.title !== s.name && <small>{s.name}</small>}</span>
                  <span className="aw-desc">{s.desc}</span>
                  {s.needs_runtime && <span className="aw-meta warn">原技能依赖脚本/命令行运行,这里只能当作方法参考{s.compatibility ? `(${s.compatibility})` : ""}</span>}
                  {s.clipped && <span className="aw-meta">正文较长,导入时会截短</span>}
                </span>
                <button className="btn small" disabled={s.installed || !!busyKey} onClick={() => void act("s" + s.id, async () => { const r = await api.awesomeInstallSkill(s.id); return `已导入技能「${r.name}」,到「技能」页勾选后对群或成员生效。`; })}>
                  {s.installed ? "已导入" : "导入"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {tab === "mcp" && (
        <>
          <p className="aw-note">MCP 服务器是会在你电脑上运行的命令。这里导入后<b>一律是停用状态</b>,不会替你填任何密钥;请核对命令、填好需要的密钥,再到「MCP」页自己启用。</p>
          <div className="card flush aw-list">
            {ov.mcp.map((m) => (
              <div key={m.id} className="aw-item">
                <div className="aw-head static">
                  <span className="aw-main">
                    <span className="aw-title">{m.name}</span>
                    <span className="aw-desc">{m.note}</span>
                    <code className="aw-cmd">{[m.command, ...m.args].join(" ")}</code>
                    {m.env_keys.length > 0 && <span className="aw-meta">需要你填:{m.env_keys.join("、")}</span>}
                  </span>
                  <button className="btn small" disabled={m.installed || !!busyKey} onClick={() => void act("c" + m.id, async () => { const r = await api.awesomeAddMcp(m.id); return `已添加 MCP「${r.name}」(停用状态)。`; })}>
                    {m.installed ? "已添加" : "添加"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="sec">数据来源与刷新</div>
      <div className="card aw-src">
        <div className="aw-meta">当前用的是{ov.origin === "local" ? "你刷新过的本地快照" : "程序内置的快照"}(提取自 awesome-llm-apps 提交 {ov.commit || "未知"},{ov.generated_at})。
          快照里的角色提示词是原作者写的英文原文,按 {ov.source.license} 保留来源;中文名和简介是本程序补的。</div>
        <div className="aw-refresh">
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="你 clone 下来的 awesome-llm-apps 文件夹(绝对路径)" aria-label="本地克隆的路径" />
          {pick && <button className="btn small" type="button" onClick={async () => { const p = await pick(); if (p) setPath(p); }}><FolderOpen size={13} /> 选择…</button>}
          <button className="btn small primary" disabled={!path.trim() || refreshing} onClick={() => void refresh()}>{refreshing ? <Loader2 size={12} className="spin" /> : null} 从本地克隆刷新</button>
          {ov.origin === "local" && <button className="btn small" onClick={() => void act("reset", async () => { await api.awesomeReset(); return "已恢复为内置快照。"; })}>恢复内置快照</button>}
        </div>
        <div className="aw-meta">刷新只读取文件、用静态方式解析,<b>不会运行仓库里的任何代码</b>,也不联网;提取不到的内容会保留原来的快照。仓库更新后如果结构变了,可能提取得不完整。</div>
      </div>
    </div>
  );
}
