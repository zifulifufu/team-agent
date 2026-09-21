import { useState, type ReactNode } from "react";
import { Plus } from "lucide-react";
import { useOutside } from "../../ui";

interface AddItem {
  key: string;
  label: ReactNode;
  hint?: string;
  onClick: () => void;
  disabled?: boolean;
}

/** 面板标题栏右侧的「+ 添加」:点开一个小菜单。 */
export default function AddMenu({ label, items }: { label: string; items: AddItem[] }) {
  const [open, setOpen] = useState(false);
  const ref = useOutside<HTMLDivElement>(open, () => setOpen(false));
  return (
    <div className="gp-addmenu" ref={ref}>
      <button className="btn small" aria-haspopup="menu" aria-expanded={open} aria-label={label} onClick={() => setOpen((o) => !o)}>
        <Plus size={12} /> 添加
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
