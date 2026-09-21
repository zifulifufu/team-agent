import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { X } from "lucide-react";
import { useI18n } from "./i18n";

export function Switch({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={"switch" + (checked ? " on" : "")}
      onClick={() => onChange(!checked)}
    >
      <i />
    </button>
  );
}

export function Modal({ title, onClose, children, wide, actions }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean; actions?: ReactNode }) {
  const { t } = useI18n();
  const mask = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // With stacked dialogs (a confirm box on top of a modal), Esc closes only the topmost one
    const h = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const all = document.querySelectorAll(".modal-mask");
      if (all[all.length - 1] === mask.current) onClose();
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  // Only a press *and* a release on the mask count as clicking outside: releasing outside while drag-selecting text in an input must not close the form
  const down = useRef(false);
  return (
    <div
      className="modal-mask"
      ref={mask}
      onMouseDown={(e) => { down.current = e.target === e.currentTarget; }}
      onClick={(e) => {
        if (down.current && e.target === e.currentTarget) onClose();
        down.current = false;
      }}
    >
      <div className={"modal" + (wide ? " wide" : "")} role="dialog" aria-label={title}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="icon-btn" aria-label={t("Close")} onClick={onClose}><X size={16} /></button>
        </div>
        <div className="modal-body">{children}</div>
        {actions && <div className="modal-actions">{actions}</div>}
      </div>
    </div>
  );
}

// ---- In-app confirm dialog (nicer than window.confirm, and it does not block the renderer)
type ConfirmFn = (message: string, opts?: { okText?: string; danger?: boolean }) => Promise<boolean>;
const ConfirmCtx = createContext<ConfirmFn>(async () => false);
export const useConfirm = () => useContext(ConfirmCtx);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [st, setSt] = useState<{ message: string; okText: string; danger: boolean } | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);
  const confirm = useCallback<ConfirmFn>(
    (message, opts) =>
      new Promise((res) => {
        resolver.current = res;
        setSt({ message, okText: opts?.okText ?? t("OK"), danger: opts?.danger ?? true });
      }),
    [],
  );
  const done = (v: boolean) => {
    resolver.current?.(v);
    resolver.current = null;
    setSt(null);
  };
  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      {st && (
        <Modal
          title={t("Please confirm")}
          onClose={() => done(false)}
          actions={
            <>
              <button className="btn" onClick={() => done(false)}>{t("Cancel")}</button>
              <button className={"btn " + (st.danger ? "danger" : "primary")} autoFocus onClick={() => done(true)}>{st.okText}</button>
            </>
          }
        >
          <p style={{ margin: 0, lineHeight: 1.6 }}>{st.message}</p>
        </Modal>
      )}
    </ConfirmCtx.Provider>
  );
}

/** Double-click guard: a second click during an async operation is ignored (otherwise two quick clicks on Create make two of the thing). Returns [busy, run]. */
export function useBusy(): [boolean, <T>(fn: () => Promise<T>) => Promise<T | undefined>] {
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    if (lock.current) return undefined;
    lock.current = true;
    setBusy(true);
    try {
      return await fn();
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }, []);
  return [busy, run];
}

/** Fallback for asynchronous errors nothing else caught (a failed button request, say): show at least one notice instead of failing silently. */
export function Toaster() {
  const { t } = useI18n();
  const [msg, setMsg] = useState("");
  useEffect(() => {
    let timer: number | undefined;
    const h = (e: PromiseRejectionEvent) => {
      e.preventDefault();
      const r = e.reason;
      setMsg(r instanceof Error ? r.message : String(r));
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setMsg(""), 6000);
    };
    window.addEventListener("unhandledrejection", h);
    return () => {
      window.removeEventListener("unhandledrejection", h);
      window.clearTimeout(timer);
    };
  }, []);
  if (!msg) return null;
  return (
    <div className="toast" role="alert">
      <span>{msg}</span>
      <button className="icon-btn tiny" aria-label={t("Dismiss the notice")} onClick={() => setMsg("")}><X size={13} /></button>
    </div>
  );
}

/** Close a popover when the click lands outside it. */
export function useOutside<T extends HTMLElement>(open: boolean, close: () => void) {
  const ref = useRef<T>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && close();
    const k = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();   // Close only this popover, not the settings page or dialog behind it
      close();
    };
    document.addEventListener("mousedown", h);
    document.addEventListener("keydown", k);
    return () => {
      document.removeEventListener("mousedown", h);
      document.removeEventListener("keydown", k);
    };
  }, [open, close]);
  return ref;
}

/** Round letter avatar for a provider or member (the colour is stable per name). */
export function LetterIcon({ name, size = 28 }: { name: string; size?: number }) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return (
    <span className="letter-icon" style={{ width: size, height: size, fontSize: size * 0.46, background: `hsl(${h} 45% 92%)`, color: `hsl(${h} 45% 30%)` }}>
      {name.trim().slice(0, 1).toUpperCase()}
    </span>
  );
}

export function useFlash(ms = 1600): [boolean, () => void] {
  const [on, setOn] = useState(false);
  const t = useRef<number>();
  useEffect(() => () => window.clearTimeout(t.current), []);
  return [
    on,
    () => {
      setOn(true);
      window.clearTimeout(t.current);
      t.current = window.setTimeout(() => setOn(false), ms);
    },
  ];
}
