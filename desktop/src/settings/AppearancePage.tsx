import { useState } from "react";
import { Check } from "lucide-react";
import { useI18n, LANGS, type Lang } from "../i18n";
import { DEFAULT_PRIMARY, PRIMARY_CHOICES, isHex, useTheme, type ThemeMode } from "../theme";

const MODES: { id: ThemeMode; key: string }[] = [
  { id: "light", key: "Light" },
  { id: "dark", key: "Dark" },
  { id: "system", key: "Follow system" },
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
  const { t, lang, setLang } = useI18n();
  const { mode, setMode, primary, setPrimary } = useTheme();
  const [hex, setHex] = useState(primary);
  const valid = isHex(hex);
  return (
    <div className="sp">
      <h2 className="sp-title">{t("Appearance")}</h2>

      <h3 className="sec">{t("Language")}</h3>
      <div className="card">
        <div className="theme-grid" role="radiogroup" aria-label={t("Language")}>
          {LANGS.map((l) => (
            <button key={l.id} role="radio" aria-checked={lang === l.id}
                    className={"theme-card" + (lang === l.id ? " on" : "")}
                    onClick={() => setLang(l.id as Lang)}>
              <span className="tc-label" style={{ padding: "10px 0" }}>
                {l.label}{lang === l.id && <Check size={14} />}
              </span>
            </button>
          ))}
        </div>
        <div className="muted small" style={{ marginTop: 10 }}>
          {t("Interface language. Built-in templates and role presets follow the same setting.")}
        </div>
      </div>

      <h3 className="sec">{t("Theme")}</h3>
      <div className="theme-grid" role="radiogroup" aria-label={t("Theme")}>
        {MODES.map((m) => (
          <button key={m.id} role="radio" aria-checked={mode === m.id} className={"theme-card" + (mode === m.id ? " on" : "")} onClick={() => setMode(m.id)}>
            <Preview mode={m.id} />
            <span className="tc-label">{t(m.key)}{mode === m.id && <Check size={14} />}</span>
          </button>
        ))}
      </div>

      <h3 className="sec">{t("Accent colour")}</h3>
      <div className="card">
        <div className="color-row" role="radiogroup" aria-label={t("Accent colour")}>
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
            aria-label={t("Custom accent colour (hex)")}
          />
          <button className="btn small" onClick={() => { setPrimary(DEFAULT_PRIMARY); setHex(DEFAULT_PRIMARY); }}>{t("Reset")}</button>
        </div>
        {!valid && <div className="fb-note err">{t("Enter a 6-digit hex colour, e.g. #00B96B")}</div>}
        <div className="muted small" style={{ marginTop: 10 }}>{t("The accent colour is used for switches, selected states and highlights. Stored on this machine.")}</div>
      </div>
    </div>
  );
}
