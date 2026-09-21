import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Check, ChevronDown, RefreshCw } from "lucide-react";
import { relTime, type HealthState, type ModelHealth } from "../api";
import { useData } from "../data";
import { useOutside } from "../ui";
import "../styles/health.css";

const HEALTH_LABEL: Record<HealthState, string> = {
  ok: "连通",
  limited: "限速/暂不可用",
  bad: "连不通",
  unknown: "未检测",
  off: "未就绪",
};

/** 悬停提示:状态 + 原因/耗时 + 多久前检测的、怎么检测的 */
function healthTitle(h: ModelHealth | undefined): string {
  if (!h) return "状态未知";
  const parts = [HEALTH_LABEL[h.state]];
  if (h.detail) parts.push(h.detail);
  if (h.state === "ok" && h.latency_ms) parts.push(`${h.latency_ms}ms`);
  if (h.checked_at) parts.push(`${relTime(h.checked_at)}${h.source === "chat" ? "(聊天时)" : h.source === "probe" ? "(本地探测)" : h.source === "test" ? "(手动检测)" : ""}${h.stale ? ",结果较旧" : ""}`);
  return parts.join(" · ");
}

/** 一个小圆点:绿=连通 黄=限速 红=连不通 空心=未检测 灰=未就绪。不只靠颜色:有文字提示,可选带文字。 */
export function HealthDot({ h, label = false }: { h: ModelHealth | undefined; label?: boolean }) {
  const st: HealthState = h?.state ?? "unknown";
  return (
    <span className={"hd hd-" + st + (h?.stale && st !== "off" ? " stale" : "")} title={healthTitle(h)} role="img" aria-label={HEALTH_LABEL[st]}>
      <i className="hd-dot" />
      {label && <span className="hd-text">{HEALTH_LABEL[st]}</span>}
    </span>
  );
}

const NO_EXCLUDE: string[] = [];

/**
 * 带指示灯的模型下拉:每个模型前面一个灯,一眼看出哪些能连通。
 * 打开时会静默探测本地服务(不花 token);「检测全部」才会向云端模型各发一条极短的请求。
 */
export function ModelSelect({
  value, onChange, autoLabel, disabled, ariaLabel, className = "", exclude = NO_EXCLUDE,
}: {
  value: string | null;
  onChange: (id: string | null) => void;
  /** 传了就多一项「自动」(值为 null) */
  autoLabel?: string;
  disabled?: boolean;
  ariaLabel: string;
  className?: string;
  /** 不列出的模型(比如已经在链里的) */
  exclude?: string[];
}) {
  const { providers, health, checkHealth } = useData();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const ref = useOutside<HTMLDivElement>(open, () => setOpen(false));
  const btnRef = useRef<HTMLButtonElement>(null);
  const [pos, setPos] = useState<CSSProperties>({});
  // 弹层用 fixed 定位:侧边栏、面板都有 overflow 裁剪,absolute 会被切掉
  useEffect(() => {
    if (!open || !btnRef.current) return;
    const place = () => {
      const r = btnRef.current!.getBoundingClientRect();
      const w = Math.min(320, window.innerWidth - 16);
      const below = window.innerHeight - r.bottom - 12;
      const up = r.top - 12;
      const flip = below < 220 && up > below;
      setPos({
        left: Math.max(8, Math.min(r.left, window.innerWidth - w - 8)), width: w,
        maxHeight: Math.min(360, flip ? up : below),
        ...(flip ? { bottom: window.innerHeight - r.top + 4 } : { top: r.bottom + 4 }),
      });
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open]);
  const groups = useMemo(
    () => providers.filter((p) => p.enabled).map((p) => ({ p, models: p.models.filter((m) => m.enabled && !exclude.includes(m.id)) })).filter((x) => x.models.length > 0),
    [providers, exclude],
  );
  const cur = groups.flatMap((g) => g.models).find((m) => m.id === value);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next) checkHealth(undefined, false).catch(() => undefined);
  };
  const runAll = async () => {
    setBusy(true);
    setErr("");
    try {
      await checkHealth(undefined, true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const pick = (id: string | null) => {
    setOpen(false);
    if (id !== value) onChange(id);
  };

  return (
    <div className={"msel " + className} ref={ref}>
      <button type="button" ref={btnRef} className="msel-btn" disabled={disabled} aria-haspopup="listbox" aria-expanded={open} aria-label={ariaLabel} onClick={toggle}>
        {cur ? <HealthDot h={health[cur.id]} /> : value ? <HealthDot h={{ state: "off", detail: "模型已被移除或停用", latency_ms: 0, checked_at: 0, source: "", stale: false }} /> : null}
        <span className="msel-cur">{cur ? cur.display_name : value ? `${value}(已移除/停用)` : (autoLabel ?? "选择模型")}</span>
        <ChevronDown size={13} aria-hidden />
      </button>
      {open && (
        <div className="msel-pop" style={pos} role="listbox" aria-label={ariaLabel}>
          {autoLabel !== undefined && (
            <button type="button" role="option" aria-selected={value === null} className={"msel-opt" + (value === null ? " sel" : "")} onClick={() => pick(null)}>
              <span className="msel-name">{autoLabel}</span>
              {value === null && <Check size={13} />}
            </button>
          )}
          {groups.map(({ p, models }) => (
            <div key={p.id} className="msel-grp">
              <div className="msel-prov">{p.name}{p.is_local && <span className="tag">本地</span>}</div>
              {models.map((m) => {
                const h = health[m.id];
                const note = h && h.state !== "ok" && h.state !== "unknown" ? h.detail : "";
                return (
                  <button key={m.id} type="button" role="option" aria-selected={m.id === value} className={"msel-opt" + (m.id === value ? " sel" : "") + (h?.state === "off" ? " dim" : "")} title={healthTitle(h)} onClick={() => pick(m.id)}>
                    <HealthDot h={h} />
                    <span className="msel-main">
                      <span className="msel-name">{m.display_name}</span>
                      {note && <span className="msel-note">{note}</span>}
                    </span>
                    {h?.state === "ok" && h.latency_ms > 0 && <span className="msel-ms">{h.latency_ms}ms</span>}
                    {m.id === value && <Check size={13} />}
                  </button>
                );
              })}
            </div>
          ))}
          {groups.length === 0 && <div className="msel-empty">还没有启用的模型</div>}
          <div className="msel-foot">
            <span className="msel-legend">
              <HealthDot h={{ state: "ok", detail: "", latency_ms: 0, checked_at: 0, source: "", stale: false }} label />
              <HealthDot h={{ state: "limited", detail: "", latency_ms: 0, checked_at: 0, source: "", stale: false }} label />
              <HealthDot h={{ state: "bad", detail: "", latency_ms: 0, checked_at: 0, source: "", stale: false }} label />
              <HealthDot h={undefined} label />
              <HealthDot h={{ state: "off", detail: "", latency_ms: 0, checked_at: 0, source: "", stale: false }} label />
            </span>
            <button type="button" className="btn small" disabled={busy} onClick={() => void runAll()} title="向每个可用的云端模型发一条极短的请求(花几个 token),本地模型只探测服务">
              <RefreshCw size={12} className={busy ? "mp-spin" : ""} /> {busy ? "检测中…" : "检测全部"}
            </button>
            {err && <div className="err small" role="alert">{err}</div>}
          </div>
        </div>
      )}
    </div>
  );
}
