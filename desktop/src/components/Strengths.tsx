import { useEffect, useState } from "react";
import { api, type Tag } from "../api";

/** A strength tag: `id` is the stable key sent to the backend, `label`/`desc` are
 * already in the UI language (the backend localizes them). */
export interface StrengthTag {
  id: Tag;
  label?: string;
  desc: string;
}

/** Strength tags (backend is the source of truth); one cache for the whole app. */
let cache: StrengthTag[] | null = null;
let inflight: Promise<StrengthTag[]> | null = null;

export function useStrengthTags(): StrengthTag[] {
  const [tags, setTags] = useState<StrengthTag[]>(cache ?? []);
  useEffect(() => {
    if (cache) return;
    inflight ??= api.strengthTags().then((r) => (cache = r.tags));
    let alive = true;
    inflight.then((t) => alive && setTags(t)).catch(() => { inflight = null; });
    return () => { alive = false; };
  }, []);
  return tags;
}

/** Display name of a tag id, falling back to the id itself. */
export const tagLabel = (tags: StrengthTag[], id: Tag): string =>
  tags.find((t) => t.id === id)?.label ?? id;

/** Read-only strength chips. `max` caps how many show; the rest collapse into "+N". */
export function StrengthChips({ tags, max = 6, className = "" }: { tags: Tag[]; max?: number; className?: string }) {
  const all = useStrengthTags();
  const desc = new Map(all.map((t) => [t.id, t.desc]));
  if (!tags.length) return null;
  const shown = tags.slice(0, max);
  const name = (id: Tag) => tagLabel(all, id);
  return (
    <span className={"str-chips " + className}>
      {shown.map((t) => (
        <span key={t} className={"str-chip" + (t === "local" ? " local" : "")} title={desc.get(t)}>{name(t)}</span>
      ))}
      {tags.length > max && (
        <span className="str-chip more" title={tags.slice(max).map(name).join(", ")}>+{tags.length - max}</span>
      )}
    </span>
  );
}

/** Clickable strength picker (multi-select). Values are ids, labels are localized. */
export function StrengthPicker({ value, onChange, disabled }: { value: Tag[]; onChange: (v: Tag[]) => void; disabled?: boolean }) {
  const all = useStrengthTags();
  return (
    <div className="str-picker" role="group" aria-label="Strength tags">
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
            {t.label ?? t.id}
          </button>
        );
      })}
    </div>
  );
}
