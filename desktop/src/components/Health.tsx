import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Check, ChevronDown, RefreshCw } from "lucide-react";
import { relTime, type HealthState, type ModelHealth } from "../api";
import { useData } from "../data";
import { currentLang, pickLang, tr, useI18n } from "../i18n";
import { useOutside } from "../ui";
import "../styles/health.css";

// Both spellings per state. `healthTitle` below is a plain function, so it cannot use the
// hook; it resolves through `pickLang(..., currentLang())` instead, exactly like lib.ts.
const HEALTH_LABEL: Record<HealthState, { en: string; zh: string }> = {
  ok: { en: "Connected", zh: "连通" },
  limited: { en: "Rate-limited / unavailable", zh: "限速/暂不可用" },
  bad: { en: "Cannot connect", zh: "连不通" },
  unknown: { en: "Not checked", zh: "未检测" },
  off: { en: "Not ready", zh: "未就绪" },
};

const healthLabel = (s: HealthState): string =>
  pickLang(HEALTH_LABEL[s].en, HEALTH_LABEL[s].zh, currentLang());

/** Hover text: the state, the reason or latency, and when and how it was checked */
function healthTitle(h: ModelHealth | undefined): string {
  if (!h) return tr("Status unknown");
  const parts = [healthLabel(h.state)];
  if (h.detail) parts.push(h.detail);
  if (h.state === "ok" && h.latency_ms) parts.push(`${h.latency_ms}ms`);
  if (h.checked_at) {
    const how = h.source === "chat" ? tr("during a chat")
      : h.source === "probe" ? tr("local probe")
      : h.source === "test" ? tr("manual check") : "";
    parts.push(`${relTime(h.checked_at)}${how ? ` (${how})` : ""}${h.stale ? tr(", and the result is old") : ""}`);
  }
  return parts.join(" · ");
}

/** A small dot: green = connected, amber = rate-limited, red = cannot connect, hollow = not checked, grey = not ready. Never colour alone: it always has a title, and optionally a label. */
export function HealthDot({ h, label = false }: { h: ModelHealth | undefined; label?: boolean }) {
  const st: HealthState = h?.state ?? "unknown";
  return (
    <span className={"hd hd-" + st + (h?.stale && st !== "off" ? " stale" : "")} title={healthTitle(h)} role="img" aria-label={healthLabel(st)}>
      <i className="hd-dot" />
      {label && <span className="hd-text">{healthLabel(st)}</span>}
    </span>
  );
}

const NO_EXCLUDE: string[] = [];

/**
 * A model picker with indicator lights: one before each model, so it is obvious at a glance which ones can connect.
 * Opening it probes local services silently (no tokens); only Check all sends one very short request to each cloud model.
 */
export function ModelSelect({
  value, onChange, autoLabel, disabled, ariaLabel, className = "", exclude = NO_EXCLUDE,
}: {
  value: string | null;
  onChange: (id: string | null) => void;
  /** Pass it to get an extra "Auto" entry (a null value) */
  autoLabel?: string;
  disabled?: boolean;
  ariaLabel: string;
  className?: string;
  /** Models to leave out (ones already in the chain, say) */
  exclude?: string[];
}) {
  const { t } = useI18n();
  const { providers, health, checkHealth } = useData();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const ref = useOutside<HTMLDivElement>(open, () => setOpen(false));
  const btnRef = useRef<HTMLButtonElement>(null);
  const [pos, setPos] = useState<CSSProperties>({});
  // The popover is fixed-positioned: the sidebar and panels clip with overflow, which would cut an absolute one off
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
        {cur ? <HealthDot h={health[cur.id]} /> : value ? <HealthDot h={{ state: "off", detail: tr("The model was removed or disabled"), latency_ms: 0, checked_at: 0, source: "", stale: false }} /> : null}
        <span className="msel-cur">{cur ? cur.display_name : value ? `${value}${t("(removed/disabled)")}` : (autoLabel ?? t("Choose a model"))}</span>
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
              <div className="msel-prov">{p.name}{p.is_local && <span className="tag">{t("Local")}</span>}</div>
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
          {groups.length === 0 && <div className="msel-empty">{t("No models are enabled yet")}</div>}
          <div className="msel-foot">
            <span className="msel-legend">
              <HealthDot h={{ state: "ok", detail: "", latency_ms: 0, checked_at: 0, source: "", stale: false }} label />
              <HealthDot h={{ state: "limited", detail: "", latency_ms: 0, checked_at: 0, source: "", stale: false }} label />
              <HealthDot h={{ state: "bad", detail: "", latency_ms: 0, checked_at: 0, source: "", stale: false }} label />
              <HealthDot h={undefined} label />
              <HealthDot h={{ state: "off", detail: "", latency_ms: 0, checked_at: 0, source: "", stale: false }} label />
            </span>
            <button type="button" className="btn small" disabled={busy} onClick={() => void runAll()} title={t("Send one very short request to every available cloud model (a few tokens); local models only get a service probe")}>
              <RefreshCw size={12} className={busy ? "mp-spin" : ""} /> {busy ? t("Checking…") : t("Check all")}
            </button>
            {err && <div className="err small" role="alert">{err}</div>}
          </div>
        </div>
      )}
    </div>
  );
}
