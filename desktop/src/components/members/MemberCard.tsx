import { useState } from "react";
import { AlertTriangle, ChevronRight, Cpu, Crown, Settings2, TerminalSquare, UserMinus } from "lucide-react";
import { api, type Capabilities, type Group } from "../../api";
import { useData } from "../../data";
import { useConfirm } from "../../ui";
import { StrengthChips } from "../Strengths";
import { HealthDot, ModelSelect } from "../Health";
import ExternalDialog from "../ExternalDialog";

const LEVEL_TEXT: Record<string, string> = { read: "只读", edit: "可改文件", full: "完全权限" };

export type MemberRow = Capabilities["members"][number];

/** 侧边栏里的一个群成员:一行摘要,点开可以看强项、换模型、设群主、移出群聊。 */
export default function MemberCard({ group, m, hasCaps }: { group: Group; m: MemberRow; hasCaps: boolean }) {
  const { agents, health, reload, reloadGroups } = useData();
  const confirm = useConfirm();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [extOpen, setExtOpen] = useState(false);
  const agent = agents.find((a) => a.id === m.agent_id);

  const run = async (fn: () => Promise<unknown>, full = false) => {
    setBusy(true);
    setErr("");
    try {
      await fn();
      await (full ? reload() : reloadGroups());
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    const msg = m.is_host
      ? `「${m.name}」是本群群主。移出后本群暂时没有群主(未 @ 任何人的消息会交给排在第一位的成员),你可以再指定新的群主。确定移出?`
      : `把「${m.name}」移出本群?(不会删除这个成员,只是不再参与本群)`;
    if (!(await confirm(msg, { okText: "移出" }))) return;
    await run(() => api.removeMember(group.id, m.agent_id));
  };

  const ext = !!m.engine;
  const modelText = ext ? `外部智能体 · ${LEVEL_TEXT[agent?.engine_cfg?.level ?? "read"]}` : m.model ? (m.manual_model ? m.model.display_name : `自动·${m.model.display_name}`) : hasCaps ? "暂无可用模型" : "";
  return (
    <div className={"mc" + (open ? " open" : "")}>
      <button className="mc-row" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="avatar sm">{m.avatar}</span>
        <span className="mc-main">
          <span className="mc-name">
            {m.name}
            {m.is_host && <span className="chip host" title="群主">主持</span>}
            {ext && <span className="chip mc-model-chip" title="外部智能体:由 WorkBuddy 自带的命令行引擎发言,不走本程序的模型路由"><TerminalSquare size={10} /> 外部</span>}
            {m.origin === "model" && <span className="chip mc-model-chip" title="由「我添加的模型」拉进群"><Cpu size={10} /> 模型</span>}
          </span>
          <span className="mc-sub">{m.role || "成员"}{modelText ? ` · ${modelText}` : ""}</span>
        </span>
        {m.model_problem && <AlertTriangle size={13} className="mc-warn-ico" aria-label="指定的模型现在用不了" />}
        {!ext && m.model && <HealthDot h={health[m.model.id]} />}
        <ChevronRight size={14} className={"mc-chev" + (open ? " open" : "")} aria-hidden />
      </button>

      {open && (
        <div className="mc-detail">
          {ext ? (
            <>
              <div className="mc-line"><span className="mc-k">权限</span>{LEVEL_TEXT[agent?.engine_cfg?.level ?? "read"]}{agent?.engine_cfg?.web ? " · 可上网" : ""}</div>
              <div className="mc-line"><span className="mc-k">目录</span><span className="mc-path" title={agent?.engine_cfg?.cwd || "专属空文件夹"}>{agent?.engine_cfg?.cwd || "专属空文件夹"}</span></div>
              <div className="mc-line muted small">每次发言是独立进程,通常要等几十秒;不能当群主。</div>
            </>
          ) : m.origin === "model" ? (
            <div className="mc-line"><span className="mc-k">模型</span>{m.model ? <><HealthDot h={health[m.model.id]} label />&nbsp;{m.model.display_name}</> : "暂无可用模型"}(模型成员,固定用这个模型)</div>
          ) : (
            <div className="mc-line">
              <span className="mc-k">模型</span>
              <ModelSelect value={agent?.model_id ?? null} autoLabel="自动(按强项挑)" disabled={busy} ariaLabel={`${m.name} 使用的模型`} onChange={(id) => void run(() => api.patchAgent(m.agent_id, { model_id: id }), true)} />
            </div>
          )}
          {!ext && hasCaps && m.model && <div className="mc-line mc-now"><span className="mc-k">实际</span>{m.manual_model ? "" : <em>自动·</em>}{m.model.display_name}&nbsp;<HealthDot h={health[m.model.id]} label /></div>}
          {!ext && m.model_problem && <div className="mc-warn" role="status"><AlertTriangle size={12} /> 指定的模型现在用不了({m.model_problem}),暂时用别的顶替</div>}
          {m.strengths.length > 0 && <StrengthChips tags={m.strengths} max={6} className="mc-str" />}
          {err && <div className="err mc-err" role="alert">{err}</div>}
          <div className="mc-acts">
            {ext && agent && <button className="btn small" disabled={busy} onClick={() => setExtOpen(true)}><Settings2 size={12} /> 设置</button>}
            {!m.is_host && !ext && <button className="btn small" disabled={busy} onClick={() => void run(() => api.patchGroup(group.id, { host_agent_id: m.agent_id }))}><Crown size={12} /> 设为群主</button>}
            <button className="btn small" disabled={busy} onClick={() => void remove()}><UserMinus size={12} /> 移出群聊</button>
          </div>
        </div>
      )}
      {extOpen && agent && <ExternalDialog mode="edit" agent={agent} onClose={() => setExtOpen(false)} onDone={() => setExtOpen(false)} />}
    </div>
  );
}
