import { memo, useState } from "react";
import { ChevronDown, ChevronRight, Circle, CircleCheck, CircleMinus, CircleSlash, CircleX, ListChecks, LoaderCircle, Target, Wrench } from "lucide-react";
import type { Agent, Message, PlanStatus, PlanTaskStatus } from "../api";
import { useData } from "../data";
import { StrengthChips } from "./Strengths";
import "../styles/chat.css";

const PLAN_LABEL: Record<PlanStatus, string> = {
  running: "进行中",
  integrating: "群主整合中",
  done: "已完成",
  stopped: "已停止",
  failed: "失败",
};

const TASK_LABEL: Record<PlanTaskStatus, string> = {
  pending: "待开始",
  running: "进行中",
  done: "完成",
  failed: "失败",
  stopped: "已停止",
  skipped: "已跳过",
};

function StatusIcon({ status, size = 16 }: { status: PlanTaskStatus; size?: number }) {
  switch (status) {
    case "running":
      return <LoaderCircle size={size} className="spin plan-st running" aria-hidden />;
    case "done":
      return <CircleCheck size={size} className="plan-st done" aria-hidden />;
    case "failed":
      return <CircleX size={size} className="plan-st failed" aria-hidden />;
    case "stopped":
      return <CircleSlash size={size} className="plan-st off" aria-hidden />;
    case "skipped":
      return <CircleMinus size={size} className="plan-st off" aria-hidden />;
    default:
      return <Circle size={size} className="plan-st pending" aria-hidden />;
  }
}

/** 「群主整合」这一行的状态由整张任务板的状态推出。 */
function finalStatus(s: PlanStatus | undefined): { status: PlanTaskStatus; label: string } {
  switch (s) {
    case "integrating":
      return { status: "running", label: "进行中" };
    case "done":
      return { status: "done", label: "完成" };
    case "failed":
      return { status: "failed", label: "失败" };
    case "stopped":
      return { status: "stopped", label: "未执行" };
    default:
      return { status: "pending", label: "待开始" };
  }
}

interface Props {
  m: Message;
  /** 「群主整合」那条消息(有的话,点击整合行可以跳过去) */
  finalMessageId?: string;
  onJump: (messageId: string) => void;
  highlight?: boolean;
}

function PlanCard({ m, finalMessageId, onJump, highlight }: Props) {
  const { agents, groups } = useData();
  const [conv, setConv] = useState(true);
  const meta = m.meta ?? {};
  const tasks = meta.tasks ?? [];
  const status: PlanStatus = meta.status ?? "running";
  const group = groups.find((g) => g.id === m.group_id);
  const host: Agent | undefined = agents.find((a) => a.id === group?.host_agent_id) ?? agents.find((a) => a.id === group?.member_ids[0]);
  const byId = new Map(agents.map((a) => [a.id, a]));
  const titleOf = new Map(tasks.map((t) => [t.id, t.title]));

  const done = tasks.filter((t) => t.status === "done").length;
  const total = tasks.length;
  const pct = total ? Math.round((done / total) * 100) : status === "done" ? 100 : 0;
  const fin = finalStatus(status);
  const conventions = (meta.conventions ?? "").trim();

  const rowProps = (mid: string | undefined) =>
    mid
      ? {
          role: "button" as const,
          tabIndex: 0,
          title: "点击定位到这条发言",
          onClick: () => onJump(mid),
          onKeyDown: (e: React.KeyboardEvent) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onJump(mid);
            }
          },
        }
      : {};

  return (
    <section className={"plan-card" + (highlight ? " hl" : "")} data-mid={m.id} aria-label="任务板">
      <header className="plan-head">
        <span className="plan-ico"><ListChecks size={15} /></span>
        <b className="plan-title">任务板</b>
        <span className={"plan-pill " + status}>
          {(status === "running" || status === "integrating") && <LoaderCircle size={11} className="spin" aria-hidden />}
          {PLAN_LABEL[status] ?? status}
        </span>
        <span className="grow" />
        <span className="plan-count" title="已完成任务 / 总任务数">
          {done}/{total} 已完成
        </span>
      </header>
      <div className="plan-bar" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done} aria-label="任务进度">
        <i className={status} style={{ width: pct + "%" }} />
      </div>

      {meta.goal && (
        <div className="plan-goal">
          <Target size={14} aria-hidden />
          <span><b>总目标</b>{meta.goal}</span>
        </div>
      )}

      {conventions && (
        <div className="plan-conv">
          <button className="plan-conv-head" aria-expanded={conv} onClick={() => setConv((v) => !v)}>
            {conv ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            全组统一约定
          </button>
          {conv && <div className="plan-conv-body">{conventions}</div>}
        </div>
      )}

      <ol className="plan-tasks">
        {tasks.map((t, i) => {
          const owner = byId.get(t.owner_id) ?? agents.find((a) => a.name === t.owner);
          return (
            <li key={t.id} className={"plan-row " + t.status + (t.message_id ? " jump" : "")} {...rowProps(t.message_id)}>
              <span className="plan-no">{i + 1}</span>
              <div className="plan-main">
                <div className="plan-line1">
                  <span className="plan-owner" title={t.owner}>
                    <span className="avatar xs">{owner?.avatar ?? "🤖"}</span>
                    {t.owner}
                  </span>
                  <span className="plan-ttl">{t.title}</span>
                </div>
                {(t.strengths.length > 0 || t.tools.length > 0 || t.needs.length > 0) && (
                  <div className="plan-line2">
                    {t.strengths.length > 0 && <StrengthChips tags={t.strengths} max={4} />}
                    {t.tools.map((n) => (
                      <span key={n} className="plan-tool" title={"计划使用工具:" + n}>
                        <Wrench size={10} aria-hidden />
                        {n}
                      </span>
                    ))}
                    {t.needs.length > 0 && (
                      <span className="plan-needs" title={t.needs.map((n) => `${n} ${titleOf.get(n) ?? ""}`.trim()).join("\n")}>
                        承接 {t.needs.join("、")}
                      </span>
                    )}
                  </div>
                )}
                {t.deliverable && (
                  <div className="plan-deliv"><span>交付物</span>{t.deliverable}</div>
                )}
                {t.status === "failed" && t.error && <div className="plan-err">{t.error}</div>}
              </div>
              <span className={"plan-state " + t.status} title={TASK_LABEL[t.status]}>
                <StatusIcon status={t.status} />
                <em>{TASK_LABEL[t.status]}</em>
              </span>
            </li>
          );
        })}
        <li className={"plan-row final " + fin.status + (finalMessageId ? " jump" : "")} {...rowProps(finalMessageId)}>
          <span className="plan-no final">∑</span>
          <div className="plan-main">
            <div className="plan-line1">
              <span className="plan-owner" title={host?.name}>
                <span className="avatar xs">{host?.avatar ?? "🤖"}</span>
                {host?.name ?? "群主"}
              </span>
              <span className="plan-ttl">群主整合</span>
            </div>
            <div className="plan-deliv"><span>说明</span>汇总各成员交付,统一口径后给出最终结果</div>
          </div>
          <span className={"plan-state " + fin.status} title={fin.label}>
            <StatusIcon status={fin.status} />
            <em>{fin.label}</em>
          </span>
        </li>
      </ol>
    </section>
  );
}

export default memo(PlanCard);
