import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, RefreshCw, ThumbsDown, ThumbsUp } from "lucide-react";
import { api, relTime, type FeedbackBoard } from "../api";
import { useI18n } from "../i18n";

/**
 * The feedback system: what the user thought of each member's replies.
 *
 * This is the other half of the thumbs in the chat. A rating that only changes a button's colour is
 * decoration; this is where it becomes something a person can act on — who is doing well, who is
 * not, and the sentences they wrote about why.
 *
 * Two decisions worth keeping when this page is edited:
 *
 *   * **both counts are always shown**, side by side, including a member with nothing but
 *     down-votes. A scoreboard that only shows the winners is one nobody should believe;
 *   * **nothing here changes a member's behaviour.** A down-vote does not rewrite a prompt or swap a
 *     model — it is evidence, and the user (or the process engineer) decides. An automatic rewrite
 *     would be a system where the same input stops producing the same output, with no way to see why.
 */
export default function FeedbackPage() {
  const { t } = useI18n();
  const [board, setBoard] = useState<FeedbackBoard | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  // Every group's verdicts, newest first: the question this page answers is about *members*, and a
  // member works in whichever group it was needed in.
  const load = useCallback(async () => {
    setLoading(true);
    try {
      setBoard(await api.feedback("", 300));
      setErr("");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const empty = !loading && board && board.totals.rated === 0;

  return (
    <div className="fb-page">
      <div className="sec">{t("Feedback")}</div>
      <p className="sp-desc">
        {t("What you thought of each member's replies. The thumbs in a chat land here; nothing here changes a member by itself — it is evidence, and you (or the process engineer) decide what to do with it.")}
      </p>

      <div className="fb-bar">
        <button className="btn" onClick={() => void load()} disabled={loading}>
          {loading ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />} {t("Refresh")}
        </button>
        {board && (
          <span className="fb-totals">
            <span className="chip ok"><ThumbsUp size={11} /> {board.totals.up}</span>
            <span className="chip bad"><ThumbsDown size={11} /> {board.totals.down}</span>
            <span className="muted small">{t("{n} replies judged", { n: board.totals.rated })}</span>
          </span>
        )}
      </div>

      {err && <div className="err">{err}</div>}
      {empty && <div className="empty">{t("No reply has been judged yet. The keys under any member's message put it here.")}</div>}

      {board && !empty && (
        <div className="fb-members">
          {board.members.map((m) => (
            <div key={m.agent_id || m.name} className="fb-member">
              <div className="fb-head">
                <b>{m.name}</b>
                <span className="chip ok"><ThumbsUp size={11} /> {m.up}</span>
                <span className="chip bad"><ThumbsDown size={11} /> {m.down}</span>
                <span className="muted small">{t("{n} judged", { n: m.up + m.down })}</span>
              </div>
              {m.notes.map((n, i) => (
                <div key={`${n.message_id}-${i}`} className="fb-note">
                  <span className={"fb-thumb " + (n.rating === "down" ? "bad" : "ok")} aria-hidden>
                    {n.rating === "down" ? <ThumbsDown size={11} /> : <ThumbsUp size={11} />}
                  </span>
                  <div>
                    <div className="fb-said">{n.note}</div>
                    <div className="muted small">
                      {n.group ? `${n.group} · ` : ""}{relTime(n.at)}
                    </div>
                    {n.text && <div className="fb-source">{n.text}</div>}
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
