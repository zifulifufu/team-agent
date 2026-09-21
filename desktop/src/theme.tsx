import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type ThemeMode = "light" | "dark" | "system";
export const DEFAULT_PRIMARY = "#00B96B";
export const PRIMARY_CHOICES = ["#00B96B", "#2f7cf6", "#7c5cff", "#f5871f", "#e5484d", "#171717"];

interface ThemeCtx {
  mode: ThemeMode;
  primary: string;
  setMode: (m: ThemeMode) => void;
  setPrimary: (c: string) => void;
}
const Ctx = createContext<ThemeCtx | null>(null);

// localStorage throws in a private window or when disabled, so every access is wrapped; fall back to the default when it cannot be read
const read = (k: string): string | null => {
  try {
    return localStorage.getItem(k);
  } catch {
    return null;
  }
};
const write = (k: string, v: string) => {
  try {
    localStorage.setItem(k, v);
  } catch {
    /* ignore */
  }
};
export const prefs = { read, write };

export const isHex = (s: string) => /^#[0-9a-fA-F]{6}$/.test(s);

/** Whether text on the accent colour should be black or white (chosen by relative luminance so button labels stay readable). */
function onColor(hex: string): string {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const lin = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const L = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  return L > 0.42 ? "#111111" : "#ffffff";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(() => {
    const v = read("ta.theme");
    return v === "light" || v === "dark" || v === "system" ? v : "system";
  });
  const [primary, setPrimaryState] = useState<string>(() => {
    const v = read("ta.primary");
    return v && isHex(v) ? v : DEFAULT_PRIMARY;
  });

  useEffect(() => {
    const root = document.documentElement;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const dark = mode === "dark" || (mode === "system" && mq.matches);
      root.dataset.theme = dark ? "dark" : "light";
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [mode]);

  useEffect(() => {
    const root = document.documentElement;
    root.style.setProperty("--primary", primary);
    root.style.setProperty("--on-primary", onColor(primary));
  }, [primary]);

  const value = useMemo<ThemeCtx>(
    () => ({
      mode,
      primary,
      setMode: (m) => {
        setModeState(m);
        write("ta.theme", m);
      },
      setPrimary: (c) => {
        if (!isHex(c)) return;
        setPrimaryState(c);
        write("ta.primary", c);
      },
    }),
    [mode, primary],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("ThemeProvider missing");
  return v;
}
