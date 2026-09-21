import { useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowUp, AtSign, Cloud, Lock, Square } from "lucide-react";

interface Mentionable {
  name: string;
  avatar: string;
  role: string;
}

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  busy?: boolean;
  onStop?: () => void;
  members: Mentionable[];
  placeholder: string;
  routeText?: string;
  offline?: boolean;
  onToggleExternal?: () => void;
  rows?: number;
  autoFocus?: boolean;
  disabled?: boolean;
  error?: string;
  /** 输入框下方左侧的额外控件(首页用来放"发送到哪个群") */
  extra?: ReactNode;
}

/** WorkBuddy 风格的大圆角输入框:左侧 @ 按钮,中间路由状态,右侧圆形发送。 */
export default function Composer(p: Props) {
  const [mention, setMention] = useState<{ q: string; idx: number } | null>(null);
  const ta = useRef<HTMLTextAreaElement>(null);

  const candidates = useMemo(() => {
    if (!mention) return [];
    const all: Mentionable[] = [{ name: "所有人", avatar: "👥", role: "全员依次发言" }, ...p.members];
    return all.filter((a) => a.name.includes(mention.q));
  }, [mention, p.members]);

  const onInput = (v: string) => {
    p.onChange(v);
    const pos = ta.current?.selectionStart ?? v.length;
    const m = /@([^\s@]*)$/.exec(v.slice(0, pos));
    setMention(m ? { q: m[1], idx: 0 } : null);
  };

  const pick = (name: string) => {
    const pos = ta.current?.selectionStart ?? p.value.length;
    const before = p.value.slice(0, pos).replace(/@([^\s@]*)$/, "@" + name + " ");
    p.onChange(before + p.value.slice(pos));
    setMention(null);
    ta.current?.focus();
  };

  const insertAt = () => {
    const pos = ta.current?.selectionStart ?? p.value.length;
    const pre = p.value.slice(0, pos);
    const sep = pre && !/\s$/.test(pre) ? " " : "";
    p.onChange(pre + sep + "@" + p.value.slice(pos));
    setMention({ q: "", idx: 0 });
    ta.current?.focus();
  };

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (mention && candidates.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const d = e.key === "ArrowDown" ? 1 : -1;
        setMention({ ...mention, idx: (mention.idx + d + candidates.length) % candidates.length });
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing)) {
        e.preventDefault();
        pick(candidates[mention.idx].name);
        return;
      }
      if (e.key === "Escape") return setMention(null);
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!p.busy && p.value.trim() && !p.disabled) p.onSend();
    }
  };

  return (
    <div className="composer">
      {mention && candidates.length > 0 && (
        <div className="mention-pop" role="listbox">
          {candidates.map((c, i) => (
            <button key={c.name} role="option" aria-selected={i === mention.idx} className={i === mention.idx ? "on" : ""} onMouseDown={(e) => { e.preventDefault(); pick(c.name); }}>
              <span className="mp-ava">{c.avatar}</span>
              <b>{c.name}</b>
              <span className="muted">{c.role}</span>
            </button>
          ))}
        </div>
      )}
      {p.error && <div className="err composer-err">{p.error}</div>}
      <div className="composer-box">
        <textarea
          ref={ta}
          value={p.value}
          autoFocus={p.autoFocus}
          onChange={(e) => onInput(e.target.value)}
          onKeyDown={onKey}
          placeholder={p.placeholder}
          rows={p.rows ?? 3}
          aria-label="消息输入框"
        />
        <div className="composer-bar">
          <button className="round-btn" title="@ 点名成员" aria-label="@ 点名成员" onClick={insertAt}>
            <AtSign size={16} />
          </button>
          {p.extra}
          <div className="grow" />
          {p.routeText !== undefined && (
            <button className={"route-pill" + (p.offline ? " off" : "")} onClick={p.onToggleExternal} title="点击切换:允许 / 禁止调用云端模型">
              {p.offline ? <Lock size={13} /> : <Cloud size={13} />}
              <span>{p.routeText}</span>
            </button>
          )}
          {p.busy && p.onStop ? (
            <button className="send-btn stop" title="停止" aria-label="停止" onClick={p.onStop}>
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <button className="send-btn" title="发送" aria-label="发送" disabled={!p.value.trim() || p.disabled || p.busy} onClick={p.onSend}>
              <ArrowUp size={17} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
