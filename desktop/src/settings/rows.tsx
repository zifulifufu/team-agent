import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Settings } from "../api";
import { useData } from "../data";
import "../styles/ext.css";

/** Saving settings: several saves on one page are queued and run in order (they do not overwrite each other); the settings are re-read afterwards either way, so the UI shows the real values.
 *  While `saving` is true, controls that compute a new value from the current list (move up/down, allow/deny) should be disabled: a second click would work from a stale list. */
export function useSettingsSaver(): { set: (patch: Partial<Settings>) => Promise<boolean>; err: string; saving: boolean } {
  const { reload } = useData();
  const [err, setErr] = useState("");
  const [pending, setPending] = useState(0);
  const tail = useRef<Promise<unknown>>(Promise.resolve());
  const set = useCallback(
    (patch: Partial<Settings>): Promise<boolean> => {
      setPending((n) => n + 1);
      const run = tail.current.then(async () => {
        setErr("");
        let ok = true;
        try {
          await api.putSettings(patch);
        } catch (e) {
          ok = false;
          setErr((e as Error).message);
        }
        await reload();
        return ok;
      });
      tail.current = run.then(() => undefined, () => undefined);
      return run.finally(() => setPending((n) => n - 1));
    },
    [reload],
  );
  return { set, err, saving: pending > 0 };
}

export function Row({ title, desc, children }: { title: string; desc: string; children?: ReactNode }) {
  return (
    <div className="setting-row pad">
      <div>
        <div className="sr-title">{title}</div>
        <div className="sr-desc">{desc}</div>
      </div>
      {children}
    </div>
  );
}

export function NumInput({ v, min, max, unit, onCommit, label }: { v: number; min: number; max: number; unit?: string; onCommit: (n: number) => Promise<boolean>; label: string }) {
  const [x, setX] = useState(String(v));
  useEffect(() => setX(String(v)), [v]);
  const commit = () => {
    const raw = Number(x);
    const n = x.trim() === "" || Number.isNaN(raw) ? v : Math.min(max, Math.max(min, Math.round(raw)));   // Clamp to range; 0 is a legal value
    setX(String(n));
    if (n !== v) void onCommit(n).then((ok) => { if (!ok) setX(String(v)); });   // Save failed: put the previous value back in the field
  };
  return (
    <span className="num-input">
      <input type="number" min={min} max={max} value={x} aria-label={label} onChange={(e) => setX(e.target.value)} onBlur={commit} onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
      {unit && <span className="muted small">{unit}</span>}
    </span>
  );
}
