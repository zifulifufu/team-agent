import { useEffect, useMemo, useState } from "react";
import { BarChart3, Table2 } from "lucide-react";
import { api, type Stats } from "../api";
import { useI18n } from "../i18n";

const RANGES = [7, 14, 30];

/** Pick a "nice" top for the y-axis: 1/2/5 × 10^n, at least 4. */
function niceMax(v: number): number {
  if (v <= 4) return 4;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 5, 10]) if (v <= m * p) return m * p;
  return v;
}

const fmtMs = (n: number | null) => (n == null ? "—" : n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${n} ms`);

export default function StatsPage() {
  const { t } = useI18n();
  const [days, setDays] = useState(14);
  const [st, setSt] = useState<Stats | null>(null);
  const [err, setErr] = useState("");
  const [table, setTable] = useState(false);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    setErr("");
    api.stats(days).then(setSt).catch((e) => setErr((e as Error).message));
  }, [days]);

  const max = useMemo(() => niceMax(Math.max(0, ...(st?.by_day.map((d) => d.count) ?? [0]))), [st]);
  const modelMax = Math.max(1, ...(st?.by_model.map((m) => m.count) ?? [1]));
  const localPct = st && st.total_requests ? Math.round((st.local_calls / st.total_requests) * 100) : 0;
  const step = days <= 7 ? 1 : days <= 14 ? 2 : 5;

  return (
    <div className="sp">
      <div className="sp-head">
        <h2 className="sp-title">{t("Usage")}</h2>
        <div className="seg" role="tablist" aria-label={t("Time range")}>
          {RANGES.map((r) => (
            <button key={r} role="tab" aria-selected={days === r} className={days === r ? "on" : ""} onClick={() => setDays(r)}>{t("Last {n} days", { n: r })}</button>
          ))}
        </div>
      </div>
      <p className="sp-desc">{t("All of this is computed from the local chat log and never uploaded. It counts calls, fallbacks and latency for now (token usage and cost are not tracked yet).")}</p>
      {err && <div className="err">{t("Could not read it:")} {err}</div>}

      {st && (
        <>
          <div className="tiles">
            <Tile label={t("Model calls")} value={String(st.total_requests)} sub={t("Member replies")} />
            <Tile label={t("Fallbacks")} value={String(st.fallbacks)} sub={st.total_requests ? t("{pct}% of them", { pct: Math.round((st.fallbacks / st.total_requests) * 100) }) : "—"} />
            <Tile label={t("Share served locally")} value={`${localPct}%`} sub={t("{n} served locally", { n: st.local_calls })} />
            <Tile label={t("Average latency")} value={fmtMs(st.avg_latency_ms)} sub={t("Time per successful call")} />
          </div>

          <div className="chart-card">
            <div className="chart-head">
              <div>
                <div className="chart-title">{t("Calls per day")}</div>
                <div className="muted small">{t("Last {n} days", { n: days })}</div>
              </div>
              <div className="seg small" role="tablist" aria-label={t("View")}>
                <button role="tab" aria-selected={!table} className={!table ? "on" : ""} onClick={() => setTable(false)}><BarChart3 size={13} /> {t("Chart")}</button>
                <button role="tab" aria-selected={table} className={table ? "on" : ""} onClick={() => setTable(true)}><Table2 size={13} /> {t("Table")}</button>
              </div>
            </div>

            {st.total_requests === 0 ? (
              <div className="empty">{t("No calls in this period yet — send a message in a group to try it")}</div>
            ) : table ? (
              <table className="data-table">
                <thead><tr><th>{t("Date")}</th><th className="num">{t("Calls")}</th><th className="num">{t("Fallbacks")}</th></tr></thead>
                <tbody>
                  {st.by_day.map((d) => (
                    <tr key={d.date}><td>{d.date}</td><td className="num">{d.count}</td><td className="num">{d.fallbacks}</td></tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="bars-wrap">
                <div className="y-axis" aria-hidden>
                  {[max, max / 2, 0].map((v) => <span key={v}>{Math.round(v)}</span>)}
                </div>
                <div className="bars" role="img" aria-label={t("Bar chart of calls per day over the last {days} days, {total} in total", { days, total: st.total_requests })}>
                  <div className="grid-lines" aria-hidden><i /><i /><i /></div>
                  {st.by_day.map((d, i) => (
                    <div key={d.date} className={"bar-col" + (hover === i ? " hot" : "")} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
                      <div className="bar" style={{ height: `${(d.count / max) * 100}%`, minHeight: d.count ? 3 : 0 }} />
                      {hover === i && (
                        <div className="tip" style={{ bottom: `calc(${(d.count / max) * 100}% + 8px)`, ...(i > days / 2 ? { right: "50%" } : { left: "50%" }) }}>
                          <b>{d.date}</b>
                          <span>{t("{n} calls", { n: d.count })}</span>
                          <span>{t("{n} fallbacks", { n: d.fallbacks })}</span>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
                <div className="x-axis" aria-hidden>
                  {st.by_day.map((d, i) => <span key={d.date}>{(days - 1 - i) % step === 0 ? d.date.slice(5) : ""}</span>)}
                </div>
              </div>
            )}
          </div>

          <div className="chart-card">
            <div className="chart-head">
              <div>
                <div className="chart-title">{t("By model")}</div>
                <div className="muted small">{t("Calls and average latency")}</div>
              </div>
            </div>
            {st.by_model.length === 0 ? (
              <div className="empty">{t("No data yet")}</div>
            ) : (
              <div className="hbars">
                {st.by_model.map((m) => (
                  <div key={m.model_id} className="hbar-row">
                    <div className="hb-label" title={m.model_id}>{m.label}{m.is_local && <span className="tag">{t("Local")}</span>}</div>
                    <div className="hb-track"><div className="hb-fill" style={{ width: `${(m.count / modelMax) * 100}%` }} /></div>
                    <div className="hb-val">{t("{n} calls", { n: m.count })} · {fmtMs(m.avg_latency_ms)}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function Tile({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      <div className="tile-sub">{sub}</div>
    </div>
  );
}
