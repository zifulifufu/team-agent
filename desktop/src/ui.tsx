import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { X } from "lucide-react";

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
  const mask = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // 弹窗叠放时(如确认框叠在对话框上),Esc 只关最上面那一个
    const h = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const all = document.querySelectorAll(".modal-mask");
      if (all[all.length - 1] === mask.current) onClose();
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  // 只有「按下和松开都落在遮罩上」才算点了外面:在输入框里拖选文字时松手到了框外,不该把表单关掉
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
          <button className="icon-btn" aria-label="关闭" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="modal-body">{children}</div>
        {actions && <div className="modal-actions">{actions}</div>}
      </div>
    </div>
  );
}

// ---- 应用内确认框(比 window.confirm 好看,也不会阻塞渲染进程)
type ConfirmFn = (message: string, opts?: { okText?: string; danger?: boolean }) => Promise<boolean>;
const ConfirmCtx = createContext<ConfirmFn>(async () => false);
export const useConfirm = () => useContext(ConfirmCtx);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [st, setSt] = useState<{ message: string; okText: string; danger: boolean } | null>(null);
  const resolver = useRef<((v: boolean) => void) | null>(null);
  const confirm = useCallback<ConfirmFn>(
    (message, opts) =>
      new Promise((res) => {
        resolver.current = res;
        setSt({ message, okText: opts?.okText ?? "确定", danger: opts?.danger ?? true });
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
          title="请确认"
          onClose={() => done(false)}
          actions={
            <>
              <button className="btn" onClick={() => done(false)}>取消</button>
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

/** 防连点:异步操作进行中再点一次会被忽略(否则「创建」点两下就建出两个)。返回 [进行中, 包装函数]。 */
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

/** 兜底:没有被界面接住的异步错误(比如某个按钮的请求失败了)至少弹出一条提示,而不是静悄悄什么都没发生。 */
export function Toaster() {
  const [msg, setMsg] = useState("");
  useEffect(() => {
    let t: number | undefined;
    const h = (e: PromiseRejectionEvent) => {
      e.preventDefault();
      const r = e.reason;
      setMsg(r instanceof Error ? r.message : String(r));
      window.clearTimeout(t);
      t = window.setTimeout(() => setMsg(""), 6000);
    };
    window.addEventListener("unhandledrejection", h);
    return () => {
      window.removeEventListener("unhandledrejection", h);
      window.clearTimeout(t);
    };
  }, []);
  if (!msg) return null;
  return (
    <div className="toast" role="alert">
      <span>{msg}</span>
      <button className="icon-btn tiny" aria-label="关闭提示" onClick={() => setMsg("")}><X size={13} /></button>
    </div>
  );
}

/** 点击组件外部关闭弹层。 */
export function useOutside<T extends HTMLElement>(open: boolean, close: () => void) {
  const ref = useRef<T>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && close();
    const k = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();   // 先只关这个弹层,别顺带把外面的设置页/对话框也关了
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

/** 供应商/成员的圆形字母头像(按名字取稳定的颜色)。 */
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
