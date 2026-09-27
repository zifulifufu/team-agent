import { useEffect, useMemo, useRef, useState } from "react";
import { GripVertical, Search, X } from "lucide-react";
import { relTime, type Group } from "../api";
import { useI18n } from "../i18n";

/** The project search: a box of its own, in the middle of the window, that can be dragged anywhere.
 *
 *  It used to be a field unfolded inside the sidebar, which meant the thing you were searching for and
 *  the results shared 280px with the list you were searching in — and the list behind it moved while
 *  you typed. A floating panel keeps the search, the query and the hits in one place, over the app
 *  rather than inside it, and it can be pushed aside to look at what is behind.
 *
 *  Two deliberate details: the results are the *same* rows the sidebar shows (same name, same relative
 *  time), so a hit is recognisable without opening it; and a query searches every project, archived
 *  ones included — the same promise the sidebar's search made, since filing a project away must not
 *  make it unfindable. With no query it lists what the sidebar is currently showing.
 *
 *  The state stays a *word* here while the sidebar's rows now carry only a dot: a hit is being read
 *  and compared against its neighbours, which is what a word is for, whereas a row in the list is
 *  being recognised at a glance. */
export default function SearchPalette({ groups, visible, activeGid, onOpen, onClose }: {
  /** Every project: what a query searches. */
  groups: Group[];
  /** What the sidebar is showing right now (the filter applied): what an empty query lists. */
  visible: Group[];
  activeGid: string | null;
  onOpen: (gid: string) => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [q, setQ] = useState("");
  // null means "still centred by CSS". The first drag pins it to real coordinates, so it never jumps
  // when the layout re-centres.
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const [sel, setSel] = useState(0);
  const box = useRef<HTMLDivElement>(null);
  const drag = useRef<{ dx: number; dy: number } | null>(null);

  const hits = useMemo(() => {
    const rows = [...groups].sort((a, b) => (b.last_at ?? 0) - (a.last_at ?? 0));
    const s = q.trim().toLowerCase();
    if (!s) return visible;
    return rows.filter((g) => g.name.toLowerCase().includes(s) || (g.last_message ?? "").toLowerCase().includes(s));
  }, [q, groups, visible]);

  useEffect(() => { setSel(0); }, [q]);

  // Held on window rather than the element, so the pointer may leave the header (or the window) while
  // dragging. Pointer capture would do the same but makes the gesture impossible to drive without a
  // real pointer, and this has to stay testable from headless Chrome.
  useEffect(() => {
    const move = (e: PointerEvent) => {
      const d = drag.current, el = box.current;
      if (!d || !el) return;
      // Keep a strip of the panel reachable in every direction: dragging it fully off-screen would
      // lose the box, and the only way back would be a reload.
      const w = el.offsetWidth;
      setPos({
        x: Math.min(Math.max(e.clientX - d.dx, 12 - w), innerWidth - 12),
        y: Math.min(Math.max(e.clientY - d.dy, 0), innerHeight - 44),
      });
    };
    const up = () => { drag.current = null; };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, []);

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); setSel((i) => Math.min(i + 1, hits.length - 1)); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setSel((i) => Math.max(i - 1, 0)); return; }
      if (e.key === "Enter") { const g = hits[sel]; if (g) onOpen(g.id); }
    };
    document.addEventListener("keydown", k);
    return () => document.removeEventListener("keydown", k);
  }, [hits, sel, onOpen, onClose]);

  return (
    <>
      <div className="search-pal-back" onMouseDown={onClose} />
      <div className={"search-pal" + (pos ? " moved" : "")} ref={box} role="dialog" aria-label={t("Search projects…")}
        style={pos ? { left: pos.x, top: pos.y } : undefined}>
        <div className="search-pal-head" title={t("Drag to move")} onPointerDown={(e) => {
          // The input keeps its own gestures (selecting text, placing the caret); a button is a button.
          if ((e.target as HTMLElement).closest("input, button")) return;
          const r = box.current?.getBoundingClientRect();
          if (!r) return;
          drag.current = { dx: e.clientX - r.left, dy: e.clientY - r.top };
          setPos({ x: Math.round(r.left), y: Math.round(r.top) });
          e.preventDefault();
        }}>
          <GripVertical size={14} className="search-pal-grip" aria-hidden />
          <Search size={15} />
          <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("Search projects…")}
            spellCheck={false} aria-label={t("Search projects…")} />
          <button className="icon-btn tiny" title={t("Close")} aria-label={t("Close")} onClick={onClose}><X size={14} /></button>
        </div>
        <div className="search-pal-list">
          {!q.trim() && <div className="search-pal-hint">{t("Type to search every project, archived ones included")}</div>}
          {hits.length === 0 && <div className="search-pal-empty">{t("No projects match")}</div>}
          {hits.map((g, i) => (
            <button key={g.id} className={"search-pal-row" + (i === sel ? " sel" : "") + (g.id === activeGid ? " on" : "")}
              onMouseEnter={() => setSel(i)} onClick={() => onOpen(g.id)}>
              <span className="conv-name">{g.name}</span>
              <span className={"conv-state" + (g.archived ? "" : g.status === "done" ? " done" : "")}>
                {t(g.archived ? "Archived" : g.status === "done" ? "Completed" : "In progress")}
              </span>
              <span className="conv-time">{relTime(g.last_at)}</span>
            </button>
          ))}
        </div>
      </div>
    </>
  );
}
