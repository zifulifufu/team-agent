import { memo, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Circle, CircleCheck, CircleMinus, CircleSlash, CircleX, ListChecks, LoaderCircle, Target, Wrench } from "lucide-react";
import type { Agent, Message, PlanStatus, PlanTaskStatus } from "../api";
import { useData } from "../data";
import { StrengthChips } from "./Strengths";
import { pick, tr, useI18n } from "../i18n";
import "../styles/chat.css";

/** Module scope, so these use `tr` and stay reactive to the current language. */
const PLAN_LABEL: Record<PlanStatus, () => string> = {
  running: () => tr("In progress"),
  integrating: () => tr("Host is consolidating"),
  done: () => tr("Finished"),
  stopped: () => tr("Stopped"),
  failed: () => tr("Failed"),
};

const TASK_LABEL: Record<PlanTaskStatus, () => string> = {
  pending: () => tr("Not started"),
  running: () => tr("In progress"),
  done: () => tr("Done"),
  failed: () => tr("Failed"),
  stopped: () => tr("Stopped"),
  skipped: () => tr("Skipped"),
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

/** The "host consolidates" row derives its state from the whole plan. */
function finalStatus(s: PlanStatus | undefined): { status: PlanTaskStatus; label: string } {
  switch (s) {
    case "integrating":
      return { status: "running", label: tr("In progress") };
    case "done":
      return { status: "done", label: tr("Done") };
    case "failed":
      return { status: "failed", label: tr("Failed") };
    case "stopped":
      return { status: "stopped", label: tr("Not run") };
    default:
      return { status: "pending", label: tr("Not started") };
  }
}

interface Props {
  m: Message;
  /** The host's consolidation message, if there is one (the row jumps to it) */
  finalMessageId?: string;
  onJump: (messageId: string) => void;
  highlight?: boolean;
}

function PlanCard({ m, finalMessageId, onJump, highlight }: Props) {
  const { t } = useI18n();
  const { agents, groups } = useData();
  const [conv, setConv] = useState(true);
  const meta = m.meta ?? {};
  const tasks = meta.tasks ?? [];
  const dropped = meta.dropped ?? [];
  const status: PlanStatus = meta.status ?? "running";
  const needsRework = (meta.score?.tasks ?? []).some((s) => s.judged && ["weak", "rework", "failed"].includes(s.verdict));
  const boardState = status === "done" && needsRework ? "failed" : status;
  const boardLabel = status === "done"
    ? needsRework ? t("Needs rework") : pick("Execution finished", "执行结束")
    : PLAN_LABEL[status]?.() ?? status;
  const group = groups.find((g) => g.id === m.group_id);
  const host: Agent | undefined = agents.find((a) => a.id === group?.host_agent_id) ?? agents.find((a) => a.id === group?.member_ids[0]);
  const byId = new Map(agents.map((a) => [a.id, a]));
  const titleOf = new Map(tasks.map((t) => [t.id, t.title]));

  const done = tasks.filter((t) => t.status === "done").length + (meta.integration?.status === "done" ? 1 : 0);
  const total = tasks.length + (meta.integration ? 1 : 0);
  const pct = total ? Math.round((done / total) * 100) : status === "done" ? 100 : 0;
  const fin = meta.integration
    ? { status: meta.integration.status, label: TASK_LABEL[meta.integration.status]() }
    : finalStatus(status);
  const finalId = meta.integration?.message_id || finalMessageId;
  const conventions = (meta.conventions ?? "").trim();

  const rowProps = (mid: string | undefined) =>
    mid
      ? {
          role: "button" as const,
          tabIndex: 0,
          title: t("Click to jump to this message"),
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
    <section className={"plan-card" + (highlight ? " hl" : "")} data-mid={m.id} aria-label={t("Plan board")}>
      <header className="plan-head">
        <span className="plan-ico"><ListChecks size={15} /></span>
        <b className="plan-title">{t("Plan board")}</b>
        <span className={"plan-pill " + boardState}>
          {(status === "running" || status === "integrating") && <LoaderCircle size={11} className="spin" aria-hidden />}
          {boardLabel}
        </span>
        <span className="grow" />
        {dropped.length > 0 && (
          // ⚠️⚠️ Shown **in the header, next to the progress**, not only as a list at the bottom.
          // The count reads "8/8 executed" on a board where part of the request was never handed to
          // anybody, and that reads as success at a glance. The backend has always written a
          // "(dropped, could not be assigned) …" line into the board's body — which this card, the
          // only renderer of a plan message, never displays. So the number is the honest headline.
          <span className="plan-dropped-n"
            title={pick(`${dropped.length} task(s) could not be assigned`, `${dropped.length} 项没能派出去`)}>
            {pick(`${dropped.length} not assigned`, `${dropped.length} 项未派单`)}
          </span>
        )}
        <span className="plan-count" title={pick("Execution progress; quality review is shown separately", "执行进度；质量复核另行标明")}>
          {pick(`${done}/${total} executed`, `${done}/${total} 已执行`)}
        </span>
      </header>
      <div className="plan-bar" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done} aria-label={t("Task progress")}>
        <i className={status} style={{ width: pct + "%" }} />
      </div>

      {meta.score && meta.score.summary && (
        <div className={"plan-score" + (meta.score.judge ? "" : " plain")}>
          {meta.score.judge
            ? t("Graded by {judge}: {ok} good, {weak} delivered but unusable downstream, {rework} to redo, {failed} failed.",
                { judge: meta.score.judge, ok: meta.score.tasks.filter((s) => s.judged && s.verdict === "ok").length, weak: meta.score.summary.weak,
                  rework: meta.score.summary.rework, failed: meta.score.summary.failed })
            : t("No model graded this round, so only the mechanical checks are shown.")}
          {meta.score.judge && meta.score.summary.judged < meta.score.summary.total
            ? pick(" Some tasks lack evidence for quality review.", " 部分任务质量证据不足，尚未完成复核。") : ""}
          {meta.score.judge_error ? ` ${meta.score.judge_error}` : ""}
        </div>
      )}

      {meta.goal && (
        <div className="plan-goal">
          <Target size={14} aria-hidden />
          <span><b>{t("Goal")}</b>{meta.goal}</span>
        </div>
      )}

      {conventions && (
        <div className="plan-conv">
          <button className="plan-conv-head" aria-expanded={conv} onClick={() => setConv((v) => !v)}>
            {conv ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            {t("Shared conventions")}
          </button>
          {conv && <div className="plan-conv-body">{conventions}</div>}
        </div>
      )}

      <ol className="plan-tasks">
        {tasks.map((task, i) => {
          const owner = byId.get(task.owner_id) ?? agents.find((a) => a.name === task.owner);
          const grade = meta.score?.tasks.find((s) => s.task_id === task.id);
          const rework = task.status === "done" && grade?.judged && grade.verdict !== "ok";
          const state = rework ? "failed" : task.status;
          const label = rework ? t("Needs rework") : task.status === "done"
            ? pick("Executed", "已执行") : TASK_LABEL[task.status]();
          return (
            <li key={task.id} className={"plan-row " + task.status + (task.message_id ? " jump" : "")} {...rowProps(task.message_id)}>
              <span className="plan-no">{i + 1}</span>
              <div className="plan-main">
                <div className="plan-line1">
                  <span className="plan-owner" title={task.owner}>
                    <span className="avatar xs">{owner?.avatar ?? "🤖"}</span>
                    {task.owner}
                  </span>
                  <span className="plan-ttl">{task.title}</span>
                </div>
                {(task.strengths.length > 0 || task.tools.length > 0 || task.needs.length > 0) && (
                  <div className="plan-line2">
                    {task.strengths.length > 0 && <StrengthChips tags={task.strengths} max={4} />}
                    {task.tools.map((n) => (
                      <span key={n} className="plan-tool" title={t("Planned tool: {name}", { name: n })}>
                        <Wrench size={10} aria-hidden />
                        {n}
                      </span>
                    ))}
                    {task.needs.length > 0 && (
                      <span className="plan-needs" title={task.needs.map((n) => `${n} ${titleOf.get(n) ?? ""}`.trim()).join("\n")}>
                        {t("Depends on {tasks}", { tasks: task.needs.join(pick(", ", "、")) })}
                      </span>
                    )}
                  </div>
                )}
                {task.deliverable && (
                  <div className="plan-deliv"><span>{t("Deliverable")}</span>{task.deliverable}</div>
                )}
                {task.error && <div className="plan-err">{task.error}</div>}
                {(() => {
                  // The grade for this task, when the round was graded. Both numbers are shown
                  // rather than one combined score: "delivered but unusable downstream" is the
                  // distinction the whole exercise is for, and a single number hides it.
                  const sc = meta.score?.tasks.find((s) => s.task_id === task.id);
                  if (!sc) return null;
                  const tip = [
                    sc.delivered !== null && sc.usable !== null
                      ? t("Delivered {d} · usable downstream {u}", { d: sc.delivered.toFixed(2), u: sc.usable.toFixed(2) })
                      : t("Not graded by a model; only the mechanical checks ran."),
                    sc.reason,
                  ].filter(Boolean).join("\n");
                  return (
                    <div className={"plan-verdict " + sc.verdict} title={tip}>
                      <span className="pv-dot" aria-hidden />
                      {!sc.judged ? pick("Quality not reviewed", "质量未复核") : sc.verdict === "ok" ? pick("AI review passed", "AI 复核通过") : sc.verdict === "weak" ? t("Unusable downstream")
                        : sc.verdict === "rework" ? t("Needs rework") : t("Failed")}
                      {sc.delivered !== null && sc.usable !== null && (
                        <em>{sc.delivered.toFixed(2)} / {sc.usable.toFixed(2)}</em>
                      )}
                    </div>
                  );
                })()}
              </div>
              <span className={"plan-state " + state} title={label}>
                <StatusIcon status={state} />
                <em>{label}</em>
              </span>
            </li>
          );
        })}
        <li className={"plan-row final " + fin.status + (finalId ? " jump" : "")} {...rowProps(finalId)}>
          <span className="plan-no final">∑</span>
          <div className="plan-main">
            <div className="plan-line1">
              <span className="plan-owner" title={host?.name}>
                <span className="avatar xs">{host?.avatar ?? "🤖"}</span>
                {host?.name ?? t("Host")}
              </span>
              <span className="plan-ttl">{t("Host consolidation")}</span>
            </div>
            <div className="plan-deliv"><span>{t("Note")}</span>{t("Merges every member's deliverable into one final answer.")}</div>
            {meta.integration?.error && <div className="plan-err">{meta.integration.error}</div>}
          </div>
          <span className={"plan-state " + fin.status} title={fin.label}>
            <StatusIcon status={fin.status} />
            <em>{fin.label}</em>
          </span>
        </li>
      </ol>

      {dropped.length > 0 && (
        // ⚠️⚠️ The part of the request that **nobody was given**. `Plan.dropped` is built by the
        // validator and its own comment says "the board has to say what was left out — dropping a
        // task silently would be the same lie as dropping the whole plan used to be". It was written
        // into the message body, and a plan message renders as this card and nothing else, so
        // nothing ever said it. Each entry is `"<id>: <why>"`.
        <div className="plan-dropped">
          <div className="plan-dropped-head">
            <AlertTriangle size={13} aria-hidden />
            {pick("Left out of this plan — nobody was given these", "这次分工里没能派出去的任务")}
          </div>
          <ul>
            {dropped.map((d, i) => (
              <li key={i}>{d}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export default memo(PlanCard);
