import { useState, type ReactNode } from "react";
import { Plus } from "lucide-react";
import { useOutside } from "../../ui";
import { useI18n } from "../../i18n";

interface AddItem {
  key: string;
  label: ReactNode;
  hint?: string;
  onClick: () => void;
  disabled?: boolean;
}

/** The "+ Add" button on the right of the panel header: opens a small menu. */
export default function AddMenu({ label, items }: { label: string; items: AddItem[] }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const ref = useOutside<HTMLDivElement>(open, () => setOpen(false));
  return (
    <div className="gp-addmenu" ref={ref}>
      <button className="btn small" aria-haspopup="menu" aria-expanded={open} aria-label={label} onClick={() => setOpen((o) => !o)}>
        <Plus size={12} /> {t("Add")}
      </button>
      {open && (
        <div className="gp-addmenu-pop" role="menu" aria-label={label}>
          {items.map((it) => (
            <button key={it.key} role="menuitem" disabled={it.disabled} onClick={() => { setOpen(false); it.onClick(); }}>
              <span>{it.label}</span>
              {it.hint && <small>{it.hint}</small>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
