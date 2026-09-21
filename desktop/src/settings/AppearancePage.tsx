import { useState } from "react";
import { Check } from "lucide-react";
import { DEFAULT_PRIMARY, PRIMARY_CHOICES, isHex, useTheme, type ThemeMode } from "../theme";

const MODES: { id: ThemeMode; label: string }[] = [
  { id: "light", label: "浅色" },
  { id: "dark", label: "深色" },
  { id: "system", label: "跟随系统" },
];

function Preview({ mode }: { mode: ThemeMode }) {
  const half = mode === "system";
  return (
    <span className={"theme-prev " + (half ? "split" : mode)} aria-hidden>
      <span className="tp-side" />
      <span className="tp-main"><i /><i /></span>
      {half && <span className="tp-dark"><span className="tp-side" /><span className="tp-main"><i /><i /></span></span>}
    </span>
  );
}

export default function AppearancePage() {
  const { mode, setMode, primary, setPrimary } = useTheme();
  const [hex, setHex] = useState(primary);
  const valid = isHex(hex);
  return (
    <div className="sp">
      <h2 className="sp-title">外观</h2>

      <h3 className="sec">主题</h3>
      <div className="theme-grid" role="radiogroup" aria-label="主题">
        {MODES.map((m) => (
          <button key={m.id} role="radio" aria-checked={mode === m.id} className={"theme-card" + (mode === m.id ? " on" : "")} onClick={() => setMode(m.id)}>
            <Preview mode={m.id} />
            <span className="tc-label">{m.label}{mode === m.id && <Check size={14} />}</span>
          </button>
        ))}
      </div>

      <h3 className="sec">主题色</h3>
      <div className="card">
        <div className="color-row" role="radiogroup" aria-label="主题色">
          {PRIMARY_CHOICES.map((c) => (
            <button
              key={c}
              role="radio"
              aria-checked={primary.toLowerCase() === c.toLowerCase()}
              aria-label={c}
              className={"swatch" + (primary.toLowerCase() === c.toLowerCase() ? " on" : "")}
              style={{ background: c }}
              onClick={() => { setPrimary(c); setHex(c); }}
            >
              {primary.toLowerCase() === c.toLowerCase() && <Check size={14} color={c === "#171717" ? "#fff" : "#fff"} />}
            </button>
          ))}
        </div>
        <div className="input-group" style={{ marginTop: 14, maxWidth: 320 }}>
          <span className="hex-dot" style={{ background: valid ? hex : "transparent" }} />
          <input
            value={hex}
            onChange={(e) => { setHex(e.target.value); if (isHex(e.target.value)) setPrimary(e.target.value); }}
            placeholder="#00B96B"
            spellCheck={false}
            aria-label="自定义主题色(十六进制)"
          />
          <button className="btn small" onClick={() => { setPrimary(DEFAULT_PRIMARY); setHex(DEFAULT_PRIMARY); }}>重置</button>
        </div>
        {!valid && <div className="fb-note err">请输入 6 位十六进制颜色,如 #00B96B</div>}
        <div className="muted small" style={{ marginTop: 10 }}>主题色用于开关、选中状态和强调元素。设置保存在本机。</div>
      </div>
    </div>
  );
}
