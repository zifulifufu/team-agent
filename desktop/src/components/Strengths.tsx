import { useEffect, useState } from "react";
import { api, type Tag } from "../api";

/** 强项标签清单(后端为准),整个应用共用一份缓存。 */
let cache: { id: Tag; desc: string }[] | null = null;
let inflight: Promise<{ id: Tag; desc: string }[]> | null = null;

export function useStrengthTags(): { id: Tag; desc: string }[] {
  const [tags, setTags] = useState(cache ?? []);
  useEffect(() => {
    if (cache) return;
    inflight ??= api.strengthTags().then((r) => (cache = r.tags));
    let alive = true;
    inflight.then((t) => alive && setTags(t)).catch(() => { inflight = null; });
    return () => { alive = false; };
  }, []);
  return tags;
}

/** 只读的强项标签串。max 限制显示个数,多出的折成「+N」。 */
export function StrengthChips({ tags, max = 6, className = "" }: { tags: Tag[]; max?: number; className?: string }) {
  const desc = new Map(useStrengthTags().map((t) => [t.id, t.desc]));
  if (!tags.length) return null;
  const shown = tags.slice(0, max);
  return (
    <span className={"str-chips " + className}>
      {shown.map((t) => (
        <span key={t} className={"str-chip" + (t === "本地" ? " local" : "")} title={desc.get(t)}>{t}</span>
      ))}
      {tags.length > max && <span className="str-chip more" title={tags.slice(max).join("、")}>+{tags.length - max}</span>}
    </span>
  );
}

/** 可点选的强项选择器(多选)。 */
export function StrengthPicker({ value, onChange, disabled }: { value: Tag[]; onChange: (v: Tag[]) => void; disabled?: boolean }) {
  const all = useStrengthTags();
  return (
    <div className="str-picker" role="group" aria-label="强项">
      {all.map((t) => {
        const on = value.includes(t.id);
        return (
          <button
            key={t.id}
            type="button"
            disabled={disabled}
            className={"str-chip pick" + (on ? " on" : "")}
            aria-pressed={on}
            title={t.desc}
            onClick={() => onChange(on ? value.filter((x) => x !== t.id) : [...value, t.id])}
          >
            {t.id}
          </button>
        );
      })}
    </div>
  );
}
