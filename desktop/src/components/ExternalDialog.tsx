import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, FolderOpen, Loader2, ShieldAlert } from "lucide-react";
import { api, type Agent, type ExternalCfg, type ExternalLevel, type ExternalOverview, type ExternalProbe, type Group } from "../api";
import { useData } from "../data";
import { Modal, useBusy } from "../ui";
import "../styles/external.css";

type Props =
  | { mode: "create"; group?: Group; onClose: () => void; onDone: () => void }
  | { mode: "edit"; agent: Agent; onClose: () => void; onDone: () => void };

const LEVEL_ORDER: ExternalLevel[] = ["read", "edit", "full"];

/** 添加 / 设置外部智能体(WorkBuddy):权限级别、工作目录、是否接力。默认最小权限(只读)。 */
export default function ExternalDialog(props: Props) {
  const { reload, reloadGroups } = useData();
  const edit = props.mode === "edit" ? props.agent : null;
  const [ov, setOv] = useState<ExternalOverview | null>(null);
  const [cfg, setCfg] = useState<ExternalCfg | null>(null);
  const [name, setName] = useState("WorkBuddy");
  const [ack, setAck] = useState(false);
  const [probe, setProbe] = useState<ExternalProbe | null>(null);
  const [testing, setTesting] = useState<"" | "quick" | "live">("");
  const [err, setErr] = useState("");
  const [busy, run] = useBusy();
  const pick = window.teamAgent?.pickFolder;

  const load = useCallback(async () => {
    try {
      const o = await api.externalOverview();
      setOv(o);
      setCfg((c) => c ?? { ...o.defaults, ...(edit?.engine_cfg ?? {}) });
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [edit]);
  useEffect(() => { void load(); }, [load]);

  const eng = ov?.engines[0];
  const set = (p: Partial<ExternalCfg>) => setCfg((c) => (c ? { ...c, ...p } : c));
  const wasFull = edit?.engine_cfg?.level === "full";
  const needAck = !!cfg && cfg.level === "full" && !wasFull;
  const blocked = !ov?.enabled;

  const enableSwitch = () => run(async () => {
    setErr("");
    try { await api.putSettings({ external_agents_enabled: true }); await reload(); await load(); } catch (e) { setErr((e as Error).message); }
  });

  const test = async (live: boolean) => {
    setTesting(live ? "live" : "quick");
    setErr("");
    try {
      setProbe(await api.externalTest({ live, agent_id: edit?.id, cli_path: cfg?.cli_path || undefined }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setTesting("");
    }
  };

  const payload = (): Partial<ExternalCfg> => {
    const c = cfg!;
    return { level: c.level, risk_ack: needAck ? ack : c.risk_ack, web: c.level === "full" ? false : c.web, cwd: c.cwd.trim(), handoff: c.handoff, model: c.model.trim(), timeout: c.timeout, cli_path: c.cli_path.trim() };
  };

  const submit = () => run(async () => {
    if (!cfg) return;
    setErr("");
    try {
      if (props.mode === "create") {
        await api.externalCreate({ engine: "workbuddy", name: name.trim() || undefined, group_id: props.group?.id, cfg: payload() });
        await reload();
      } else {
        await api.externalPatch(props.agent.id, payload());
        await reload();
        await reloadGroups();
      }
      props.onDone();
    } catch (e) {
      setErr((e as Error).message);
    }
  });

  const title = props.mode === "create" ? "添加外部智能体 · WorkBuddy" : `外部智能体设置 · ${props.agent.name}`;
  return (
    <Modal
      title={title}
      onClose={props.onClose}
      wide
      actions={
        <>
          <button className="btn" onClick={props.onClose}>取消</button>
          <button className="btn primary" disabled={busy || blocked || !cfg || (needAck && !ack)} onClick={() => void submit()}>
            {props.mode === "create" ? (props.group ? "创建并拉入本群" : "创建") : "保存"}
          </button>
        </>
      }
    >
      <div className="ext-dlg">
        <p className="ext-intro">
          让 WorkBuddy 作为群成员参与讨论。接法:本程序调用 WorkBuddy 应用里<b>自带的命令行引擎</b>(无界面模式),每一轮把群聊记录交给它,取回它的回复。
          它<b>不会去操控 WorkBuddy 的窗口</b>,也不读取它的账号、会话或密钥。它自己带工具(读文件、检索等),所以权限由你在这里限定。
        </p>

        {blocked && ov && (
          <div className="ext-box warn" role="alert">
            <ShieldAlert size={15} />
            <div>
              <b>外部智能体总开关还是关着的。</b>它们是带工具的智能体,可能读写你的文件,所以默认关闭,需要你主动打开。
              <div><button className="btn small" disabled={busy} onClick={() => void enableSwitch()}>打开总开关</button></div>
            </div>
          </div>
        )}
        {ov && !ov.external_calls_enabled && (
          <div className="ext-box warn" role="status"><AlertTriangle size={15} /><div>「禁止外呼」正开着:WorkBuddy 要连接云端模型,这个模式下它不会运行。先在设置 → 路由里放开。</div></div>
        )}

        <div className="ext-status">
          {!eng ? <span className="muted small">检测中…</span> : eng.found ? (
            <span className="ext-ok"><CheckCircle2 size={14} /> 已找到命令行引擎{probe?.version ? ` · 版本 ${probe.version}` : ""}<small title={eng.path}>{eng.path}</small></span>
          ) : (
            <span className="ext-bad"><AlertTriangle size={14} /> {eng.hint}</span>
          )}
          <span className="ext-status-btns">
            <button className="btn small" disabled={blocked || !!testing} onClick={() => void test(false)}>{testing === "quick" ? <Loader2 size={12} className="spin" /> : null} 检测</button>
            <button className="btn small" disabled={blocked || !!testing || !ov?.external_calls_enabled} onClick={() => void test(true)} title="真的发一句话试试,会调用云端模型(消耗极少量额度)">
              {testing === "live" ? <Loader2 size={12} className="spin" /> : null} 测试连接
            </button>
          </span>
        </div>
        {probe?.live && (
          probe.live.ok
            ? <div className="ext-box ok" role="status"><CheckCircle2 size={15} /><div>连接正常:引擎回复「{probe.live.reply}」,用时 {probe.live.seconds} 秒{probe.live.model ? `,模型 ${probe.live.model}` : ""}。</div></div>
            : <div className="ext-box warn" role="alert"><AlertTriangle size={15} /><div>测试没通过:{probe.live.error}</div></div>
        )}
        {probe && !probe.live && probe.hint && <div className="ext-box warn"><AlertTriangle size={15} /><div>{probe.hint}</div></div>}

        {cfg && (
          <>
            {props.mode === "create" && (
              <label className="field">
                <span>成员名字(群里用 @名字 点名,不能含空格)</span>
                <input value={name} onChange={(e) => setName(e.target.value)} maxLength={30} />
              </label>
            )}

            <div className="field">
              <span>权限级别</span>
              <div className="ext-levels" role="radiogroup" aria-label="权限级别">
                {LEVEL_ORDER.map((id) => {
                  const lv = ov?.levels.find((l) => l.id === id);
                  return (
                    <label key={id} className={"ext-level" + (cfg.level === id ? " on" : "") + (id === "full" ? " danger" : "")}>
                      <input type="radio" name="ext-level" checked={cfg.level === id} onChange={() => set({ level: id })} />
                      <span><b>{lv?.label ?? id}</b>{id === "read" && <em>推荐</em>}<small>{lv?.desc}</small></span>
                    </label>
                  );
                })}
              </div>
              {needAck && (
                <label className="check ext-ack">
                  <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
                  我明白「完全」会放开执行命令、读写文件和联网,群里任何人的一句话、它读到的任何文件或网页,都可能让它做出有后果的操作
                </label>
              )}
              {cfg.level !== "full" && (
                <label className="check ext-web">
                  <input type="checkbox" checked={cfg.web} onChange={(e) => set({ web: e.target.checked })} />
                  允许它上网搜索 / 抓取网页(默认关闭)
                </label>
              )}
            </div>

            <label className="field">
              <span>工作目录(它读写文件的范围;留空 = 本程序数据目录下给它专门建的一个空文件夹)</span>
              <span className="ext-dir">
                <input value={cfg.cwd} onChange={(e) => set({ cwd: e.target.value })} placeholder="留空 = 专属空文件夹(最安全)" />
                {pick && <button type="button" className="btn small" onClick={async () => { const p = await pick(); if (p) set({ cwd: p }); }}><FolderOpen size={13} /> 选择…</button>}
              </span>
              {cfg.level !== "read" && !cfg.cwd.trim() && <span className="muted small">当前是「{cfg.level === "edit" ? "可改文件" : "完全"}」:它只能动专属空文件夹里的东西,想让它处理你的项目,请选一个具体的项目文件夹(不能是根目录或整个用户主目录)。</span>}
            </label>

            <label className="check">
              <input type="checkbox" checked={cfg.handoff} onChange={(e) => set({ handoff: e.target.checked })} />
              它的回复里 @ 别的成员时,让被点名的成员接着发言
            </label>

            <details className="ext-adv">
              <summary>高级</summary>
              <div className="form-row">
                <label className="field grow"><span>模型(留空 = 引擎默认)</span><input value={cfg.model} onChange={(e) => set({ model: e.target.value })} /></label>
                <label className="field" style={{ width: 130 }}><span>单次超时(秒)</span><input type="number" min={30} max={3600} value={cfg.timeout} onChange={(e) => set({ timeout: Number(e.target.value) || 600 })} /></label>
              </div>
              <label className="field"><span>命令行位置(留空 = 自动查找 WorkBuddy 应用里自带的;文件名需以 codebuddy 开头)</span><input value={cfg.cli_path} onChange={(e) => set({ cli_path: e.target.value })} /></label>
            </details>
          </>
        )}

        <p className="ext-foot muted small">
          说明:WorkBuddy 每次发言是一个独立进程,没有跨轮记忆(上下文靠群聊记录),回复通常要等几十秒;它的回复只当聊天文字,不会被当作分工计划或工具调用来执行。不会改动 WorkBuddy 应用本身的任何设置(包括它自己的「允许完全访问」)。
        </p>
        {err && <div className="err" role="alert">{err}</div>}
      </div>
    </Modal>
  );
}
