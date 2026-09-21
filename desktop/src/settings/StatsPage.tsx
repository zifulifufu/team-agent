import { useEffect, useMemo, useState } from "react";
import { BarChart3, Table2 } from "lucide-react";
import { api, type Stats } from "../api";

const RANGES = [7, 14, 30];

/** 取一个"好看"的纵轴上限:1/2/5 × 10^n,最小 4。 */
function niceMax(v: number): number {
  if (v <= 4) return 4;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 5, 10]) if (v <= m * p) return m * p;
  return v;
}

const fmtMs = (n: number | null) => (n == null ? "—" : n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${n} ms`);

export default function StatsPage() {
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
        <h2 className="sp-title">使用统计</h2>
        <div className="seg" role="tablist" aria-label="统计范围">
          {RANGES.map((r) => (
            <button key={r} role="tab" aria-selected={days === r} className={days === r ? "on" : ""} onClick={() => setDays(r)}>近 {r} 天</button>
          ))}
        </div>
      </div>
      <p className="sp-desc">全部由本机聊天记录计算,不上传。目前统计调用次数、回退次数与延迟(暂不统计 token 用量与费用)。</p>
      {err && <div className="err">读取失败:{err}</div>}

      {st && (
        <>
          <div className="tiles">
            <Tile label="模型调用" value={String(st.total_requests)} sub="成员发言次数" />
            <Tile label="发生回退" value={String(st.fallbacks)} sub={st.total_requests ? `占 ${Math.round((st.fallbacks / st.total_requests) * 100)}%` : "—"} />
            <Tile label="本地模型占比" value={`${localPct}%`} sub={`${st.local_calls} 次由本地完成`} />
            <Tile label="平均响应" value={fmtMs(st.avg_latency_ms)} sub="成功调用的耗时" />
          </div>

          <div className="chart-card">
            <div className="chart-head">
              <div>
                <div className="chart-title">每日调用次数</div>
                <div className="muted small">近 {days} 天</div>
              </div>
              <div className="seg small" role="tablist" aria-label="视图">
                <button role="tab" aria-selected={!table} className={!table ? "on" : ""} onClick={() => setTable(false)}><BarChart3 size={13} /> 图表</button>
                <button role="tab" aria-selected={table} className={table ? "on" : ""} onClick={() => setTable(true)}><Table2 size={13} /> 表格</button>
              </div>
            </div>

            {st.total_requests === 0 ? (
              <div className="empty">这段时间还没有调用记录 —— 去群里发条消息试试</div>
            ) : table ? (
              <table className="data-table">
                <thead><tr><th>日期</th><th className="num">调用</th><th className="num">回退</th></tr></thead>
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
                <div className="bars" role="img" aria-label={`近 ${days} 天每日调用次数柱状图,共 ${st.total_requests} 次`}>
                  <div className="grid-lines" aria-hidden><i /><i /><i /></div>
                  {st.by_day.map((d, i) => (
                    <div key={d.date} className={"bar-col" + (hover === i ? " hot" : "")} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
                      <div className="bar" style={{ height: `${(d.count / max) * 100}%`, minHeight: d.count ? 3 : 0 }} />
                      {hover === i && (
                        <div className="tip" style={{ bottom: `calc(${(d.count / max) * 100}% + 8px)`, ...(i > days / 2 ? { right: "50%" } : { left: "50%" }) }}>
                          <b>{d.date}</b>
                          <span>调用 {d.count} 次</span>
                          <span>回退 {d.fallbacks} 次</span>
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
                <div className="chart-title">按模型</div>
                <div className="muted small">调用次数与平均响应</div>
              </div>
            </div>
            {st.by_model.length === 0 ? (
              <div className="empty">暂无数据</div>
            ) : (
              <div className="hbars">
                {st.by_model.map((m) => (
                  <div key={m.model_id} className="hbar-row">
                    <div className="hb-label" title={m.model_id}>{m.label}{m.is_local && <span className="tag">本地</span>}</div>
                    <div className="hb-track"><div className="hb-fill" style={{ width: `${(m.count / modelMax) * 100}%` }} /></div>
                    <div className="hb-val">{m.count} 次 · {fmtMs(m.avg_latency_ms)}</div>
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
