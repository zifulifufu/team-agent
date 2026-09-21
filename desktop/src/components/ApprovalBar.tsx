import { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { api, type Approval } from "../api";
import { toolDisplay } from "./Bubble";
import "../styles/approval.css";

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, [active]);
  return now;
}

/** 成员想用一个会「执行」的工具时,聊天窗口底部弹出的确认条。超时未答按拒绝。 */
export default function ApprovalBar({ items, onGone }: { items: Approval[]; onGone: (id: string) => void }) {
  const now = useNow(items.length > 0);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => {
    if (items.length === 0) setErr("");   // 全部处理完了,别把上一次的错误带到下一条审批上
  }, [items.length]);
  if (items.length === 0) return null;

  const answer = async (a: Approval, decision: "allow" | "deny", remember = false) => {
    setBusy(a.id);
    setErr("");
    try {
      await api.answerApproval(a.id, decision, remember);
      onGone(a.id);
    } catch (e) {
      const msg = (e as Error).message;
      setErr(msg);
      if (/过期|处理了/.test(msg)) onGone(a.id);   // 后端已经不认这条了才收起;网络出错时保留,可以重试
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="apv-wrap" role="region" aria-label="需要你确认的工具调用">
      {items.map((a) => {
        const { label } = toolDisplay(a.tool);
        const left = Math.max(0, Math.ceil(a.expires_at - now));
        const args = Object.entries(a.args);
        return (
          <div key={a.id} className="apv" role="alertdialog" aria-label={`「${a.agent}」想调用 ${label}`}>
            <div className="apv-head">
              <ShieldAlert size={15} aria-hidden />
              <span><b>{a.agent}</b> 想调用 <b>{label}</b></span>
              <span className={"apv-risk " + a.risk}>{a.risk_label}</span>
              {a.source === "plugin" && <span className="apv-src">插件</span>}
              {a.source === "mcp" && <span className="apv-src">MCP{a.server ? ` · ${a.server}` : ""}</span>}
              <span className="apv-left" title="超时未确认会按拒绝处理">{left}s 后自动拒绝</span>
            </div>
            {args.length > 0 && (
              <dl className="apv-args">
                {args.map(([k, v]) => (
                  <div key={k}><dt>{k}</dt><dd>{typeof v === "string" ? v : JSON.stringify(v)}</dd></div>
                ))}
              </dl>
            )}
            <div className="apv-acts">
              <button className="btn small primary" disabled={busy === a.id} onClick={() => void answer(a, "allow")}>允许一次</button>
              <button className="btn small" disabled={busy === a.id} onClick={() => void answer(a, "allow", true)} title="以后这个工具不再询问(可在 设置 → 权限与操控 里撤销)">总是允许</button>
              <button className="btn small" disabled={busy === a.id} onClick={() => void answer(a, "deny")}>拒绝</button>
            </div>
          </div>
        );
      })}
      {err && <div className="err small apv-err" role="alert">{err}</div>}
    </div>
  );
}
