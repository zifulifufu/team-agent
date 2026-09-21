import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, Settings2, TerminalSquare } from "lucide-react";
import { api, type ExternalOverview, type ExternalProbe } from "../api";
import { useData } from "../data";
import { Switch } from "../ui";
import ExternalDialog from "../components/ExternalDialog";
import { Row, useSettingsSaver } from "./rows";
import type { PageProps } from "./SettingsModal";
import "../styles/external.css";

const LEVEL_TEXT: Record<string, string> = { read: "只读", edit: "可改文件", full: "完全权限" };

/** 设置 → 外部智能体:总开关、WorkBuddy 命令行引擎的检测/连通性测试、已加入的外部成员。 */
export default function ExternalPage(_: PageProps) {
  const { settings, agents } = useData();
  const { set, err: saveErr, saving } = useSettingsSaver();
  const [ov, setOv] = useState<ExternalOverview | null>(null);
  const [probe, setProbe] = useState<ExternalProbe | null>(null);
  const [testing, setTesting] = useState<"" | "quick" | "live">("");
  const [err, setErr] = useState("");
  const [editId, setEditId] = useState<string | null>(null);

  const load = useCallback(() => { api.externalOverview().then((o) => { setOv(o); setErr(""); }).catch((e) => setErr((e as Error).message)); }, []);
  useEffect(load, [load, settings?.external_agents_enabled, settings?.external_calls_enabled, agents.length]);

  if (!settings) return <div className="empty big">加载中…</div>;
  const on = settings.external_agents_enabled;
  const eng = ov?.engines[0];
  const members = agents.filter((a) => !!a.engine);
  const editing = members.find((a) => a.id === editId);

  const test = async (live: boolean) => {
    setTesting(live ? "live" : "quick");
    setErr("");
    try { setProbe(await api.externalTest({ live })); } catch (e) { setErr((e as Error).message); } finally { setTesting(""); }
  };

  return (
    <div className="sp ext-page">
      <h2 className="sp-title"><TerminalSquare size={20} aria-hidden /> 外部智能体</h2>
      <p className="sp-desc">让 WorkBuddy 作为群成员参与讨论。本程序调用 WorkBuddy 应用里自带的命令行引擎(无界面模式),不会操控它的窗口,也不读它的账号、会话或密钥。</p>
      {(saveErr || err) && <div className="err" role="alert">{saveErr || err}</div>}

      <div className="card flush">
        <Row title="允许使用外部智能体" desc="总开关,默认关闭。它们自带读写文件等工具,打开前请先想清楚给什么权限;关闭后已加入的外部成员不会发言。">
          <Switch checked={on} disabled={saving} onChange={(v) => void set({ external_agents_enabled: v })} label="允许使用外部智能体" />
        </Row>
      </div>
      {on && !settings.external_calls_enabled && (
        <div className="ext-box warn" role="status"><AlertTriangle size={15} /><div>「禁止外呼」正开着:外部智能体要连接云端模型,这时不会运行。到「路由与回退」里放开后才能用。</div></div>
      )}

      <div className="sec">WorkBuddy 命令行引擎</div>
      <div className="card ext-engine">
        {!eng ? <span className="muted small">检测中…</span> : eng.found ? (
          <div className="ext-ok"><CheckCircle2 size={14} /> 已找到{probe?.version ? ` · 版本 ${probe.version}` : ""}<small title={eng.path}>{eng.path}</small></div>
        ) : (
          <div className="ext-bad"><AlertTriangle size={14} /> {eng.hint}</div>
        )}
        <div className="ext-status-btns">
          <button className="btn small" disabled={!on || !!testing} onClick={() => void test(false)}>{testing === "quick" ? <Loader2 size={12} className="spin" /> : null} 检测</button>
          <button className="btn small" disabled={!on || !!testing || !settings.external_calls_enabled} onClick={() => void test(true)} title="真的发一句话试试,会调用云端模型(消耗极少量额度)">
            {testing === "live" ? <Loader2 size={12} className="spin" /> : null} 测试连接
          </button>
        </div>
        {!on && <div className="muted small">先打开上面的总开关,才能检测(检测会真的启动一次命令行)。</div>}
        {probe?.live && (probe.live.ok
          ? <div className="ext-box ok" role="status"><CheckCircle2 size={15} /><div>连接正常:引擎回复「{probe.live.reply}」,用时 {probe.live.seconds} 秒{probe.live.model ? `,模型 ${probe.live.model}` : ""}。</div></div>
          : <div className="ext-box warn" role="alert"><AlertTriangle size={15} /><div>测试没通过:{probe.live.error}</div></div>)}
        {probe && !probe.live && probe.hint && <div className="ext-box warn"><AlertTriangle size={15} /><div>{probe.hint}</div></div>}
      </div>

      <div className="sec">已添加的外部成员</div>
      {members.length === 0 ? (
        <div className="card muted small ext-none">还没有。在群聊里点「添加成员」→「外部智能体」→ WorkBuddy 即可加入。</div>
      ) : (
        <div className="card flush">
          {members.map((a) => (
            <div key={a.id} className="setting-row pad">
              <div>
                <div className="sr-title">{a.avatar} {a.name}</div>
                <div className="sr-desc">权限:{LEVEL_TEXT[a.engine_cfg?.level ?? "read"]}{a.engine_cfg?.web ? " · 可上网" : ""} · 工作目录:{a.engine_cfg?.cwd || "专属空文件夹"}</div>
              </div>
              <button className="btn small" onClick={() => setEditId(a.id)}><Settings2 size={12} /> 设置</button>
            </div>
          ))}
        </div>
      )}

      <div className="sec">使用须知</div>
      <ul className="ext-notes">
        <li>每次发言是一个独立的命令行进程,没有跨轮记忆(上下文靠群聊记录);第一次通常要等几十秒,之后会快一些。</li>
        <li>外部智能体不能当群主,它的回复只当聊天文字,不会被当作分工计划或工具调用来执行。</li>
        <li>「只读」的工作目录只是起点,不是围栏;要限制读取范围,请不要给它读不该读的东西的账户权限。</li>
        <li>它读到的文件、网页内容都可能包含试图指挥它的文字(提示词注入),越高的权限风险越大。</li>
        <li>本程序不会改动 WorkBuddy 应用本身的任何设置(包括它自己的「允许完全访问」)。</li>
      </ul>
      {editing && <ExternalDialog mode="edit" agent={editing} onClose={() => setEditId(null)} onDone={() => { setEditId(null); load(); }} />}
    </div>
  );
}
