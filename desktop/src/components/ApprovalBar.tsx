import { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { api, ApiError, type Approval } from "../api";
import { useI18n } from "../i18n";
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

/** The confirmation bar at the bottom of the chat when a member wants to use a tool that "runs" something. No answer in time counts as a denial. */
export default function ApprovalBar({ items, onGone }: { items: Approval[]; onGone: (id: string) => void }) {
  const { t } = useI18n();
  const now = useNow(items.length > 0);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => {
    if (items.length === 0) setErr("");   // Everything has been handled: do not carry the previous error over to the next approval
  }, [items.length]);
  if (items.length === 0) return null;

  const answer = async (a: Approval, decision: "allow" | "deny", remember = false) => {
    setBusy(a.id);
    setErr("");
    try {
      await api.answerApproval(a.id, decision, remember);
      onGone(a.id);
    } catch (e) {
      setErr((e as Error).message);
      // Drop it only when the backend no longer knows about it (404). A network failure keeps
      // the bar so the user can retry — and the status, not the message, decides which: the
      // message follows the interface language.
      if (e instanceof ApiError && e.status === 404) onGone(a.id);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="apv-wrap" role="region" aria-label={t("Tool calls that need your confirmation")}>
      {items.map((a) => {
        const { label } = toolDisplay(a.tool);
        const left = Math.max(0, Math.ceil(a.expires_at - now));
        const args = Object.entries(a.args);
        return (
          <div key={a.id} className="apv" role="alertdialog" aria-label={t("{agent} wants to call {tool}", { agent: a.agent, tool: label })}>
            <div className="apv-head">
              <ShieldAlert size={15} aria-hidden />
              <span><b>{a.agent}</b>{t(" wants to call ")}<b>{label}</b></span>
              <span className={"apv-risk " + a.risk}>{a.risk_label}</span>
              {a.source === "plugin" && <span className="apv-src">{t("Plugin")}</span>}
              {a.source === "mcp" && <span className="apv-src">MCP{a.server ? ` · ${a.server}` : ""}</span>}
              <span className="apv-left" title={t("Without a confirmation in time it is treated as a denial")}>{t("Denied automatically in {n}s", { n: left })}</span>
            </div>
            {args.length > 0 && (
              <dl className="apv-args">
                {args.map(([k, v]) => (
                  <div key={k}><dt>{k}</dt><dd>{typeof v === "string" ? v : JSON.stringify(v)}</dd></div>
                ))}
              </dl>
            )}
            <div className="apv-acts">
              <button className="btn small primary" disabled={busy === a.id} onClick={() => void answer(a, "allow")}>{t("Allow once")}</button>
              <button className="btn small" disabled={busy === a.id} onClick={() => void answer(a, "allow", true)} title={t("Never ask about this tool again (undo it under Settings → Permissions & control)")}>{t("Always allow")}</button>
              <button className="btn small" disabled={busy === a.id} onClick={() => void answer(a, "deny")}>{t("Deny")}</button>
            </div>
          </div>
        );
      })}
      {err && <div className="err small apv-err" role="alert">{err}</div>}
    </div>
  );
}
