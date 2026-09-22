import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Download, RefreshCw, Search } from "lucide-react";
import { api, type ImportItem, type ImportSource } from "../api";
import { Modal } from "../ui";
import { useI18n } from "../i18n";
import "../styles/ext.css";

type ImportKind = "mcp" | "skill" | "expert";

/** Import MCP servers, skills or expert packages that already exist in another AI
 *  application on this machine, instead of pasting a config by hand.
 *
 *  The preview is the point: a server is a command line that will later run with your
 *  privileges, so what it found is shown as a command, not as a name and a switch. Nothing
 *  is started here, and everything imported arrives disabled or, for a member, with no
 *  model pinned. */
export default function ImportFromApps({ kind, onClose, onDone }: {
  kind: ImportKind;
  onClose: () => void;
  onDone: (added: number, skipped: number) => void;
}) {
  const { t } = useI18n();
  const [sources, setSources] = useState<ImportSource[] | null>(null);
  const [items, setItems] = useState<ImportItem[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setErr("");
    try {
      const got = await api.importSources();
      setSources(got.sources.filter((s) => s.kind === kind));
    } catch (e) {
      setErr((e as Error).message);
      setSources([]);
    }
  }, [kind]);
  useEffect(() => { void load(); }, [load]);

  const scan = async (pickedSources?: string[]) => {
    setBusy(true);
    setErr("");
    try {
      const got = await api.importScan(pickedSources);
      const mine = got.items.filter((i) => i.kind === kind);
      setItems(mine);
      setNotes(got.notes);
      // Everything not already present is ticked: the common case is "take all of it",
      // and anything that already exists cannot be overwritten anyway.
      setPicked(new Set(mine.filter((i) => !i.exists).map((i) => i.name)));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const toggle = (name: string) => setPicked((prev) => {
    const next = new Set(prev);
    if (next.has(name)) next.delete(name); else next.add(name);
    return next;
  });

  const doImport = async () => {
    const chosen = items.filter((i) => picked.has(i.name));
    if (!chosen.length) return;
    setBusy(true);
    setErr("");
    try {
      // One call per source: a source is the unit the backend re-reads, and grouping by it
      // keeps the request honest about where each entry came from.
      let added = 0, skipped = 0;
      for (const source of [...new Set(chosen.map((i) => i.source))]) {
        const names = chosen.filter((i) => i.source === source).map((i) => i.name);
        const r = kind === "mcp" ? await api.importMcpFrom(source, names)
          : kind === "skill" ? await api.importSkillsFrom(source, names)
            : await api.importExpertsFrom(source, names);
        added += r.added.length;
        skipped += r.skipped.length;
      }
      onDone(added, skipped);
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  const line = (it: ImportItem) => it.command ? [it.command, ...(it.args ?? [])].join(" ") : (it.url ?? "");

  // A connector catalogue alone runs to a few hundred rows, so the list gets a filter as
  // soon as it is long enough to be worth one. Ticking survives filtering: the picks are
  // kept by name, not by what happens to be on screen.
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((i) => `${i.label ?? ""} ${i.name} ${i.role ?? ""} ${i.description ?? ""} ${line(i)}`
      .toLowerCase().includes(needle));
  }, [items, q]);

  const grouped = useMemo(() => {
    const out = new Map<string, ImportItem[]>();
    for (const it of shown) out.set(it.app, [...(out.get(it.app) ?? []), it]);
    return [...out.entries()];
  }, [shown]);

  const title = kind === "mcp" ? t("Import MCP servers from another app")
    : kind === "skill" ? t("Import skills from another app")
      : t("Import experts from another app");

  return (
    <Modal title={title} onClose={onClose} wide>
      <div className="ia">
        <p className="muted small">
          {kind === "expert"
            ? t("This reads the expert packages other AI applications keep on this machine and turns each one into a member, using the package's own text as the prompt. No model is pinned and no skill is attached — read it first, then decide how to use it.")
            : t("This reads the configuration files that other AI applications keep on this machine, and shows what they contain. Nothing is started, and everything imported arrives disabled — a server here is a command line that will later run with your privileges, so read it before switching it on.")}
        </p>
        {err && <div className="err" role="alert">{err}</div>}

        <div className="sp-head-actions" style={{ marginBottom: 8 }}>
          <button className="btn small" disabled={busy} onClick={() => void load()}>
            <RefreshCw size={12} /> {t("Scan again")}
          </button>
          {sources && sources.some((s) => s.found) && (
            <button className="btn small" disabled={busy} onClick={() => void scan()}>
              <Download size={12} className={busy ? "spin" : ""} /> {t("List what is importable")}
            </button>
          )}
          {items.length > 12 && (
            <div className="search-box" style={{ maxWidth: 280 }}>
              <Search size={14} />
              <input value={q} placeholder={t("Filter by name or address")}
                     aria-label={t("Filter the list")} onChange={(e) => setQ(e.target.value)} />
            </div>
          )}
        </div>

        {!sources && <div className="muted small">{t("Looking for other applications…")}</div>}
        {sources && (
          <div className="card flush">
            {sources.map((s) => (
              <div key={s.key} className="setting-row pad">
                <div>
                  <div className="sr-title">
                    {s.found ? <CheckCircle2 size={13} aria-hidden /> : <AlertTriangle size={13} aria-hidden />} {s.app}
                    {s.found && s.count > 0 && <span className="tag">{t("{n} found", { n: s.count })}</span>}
                  </div>
                  <div className="sr-desc">
                    {s.error ? <span className="err small">{s.error}</span>
                      : s.notes || (s.found ? t("Found on this machine") : t("Not found"))}
                  </div>
                  {s.found && s.files.length > 0 && <div className="muted small mono">{s.files[0]}</div>}
                </div>
                {s.found && s.count > 0 && (
                  <button className="btn small" disabled={busy} onClick={() => void scan([s.key])}>
                    {t("Show")}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {notes.length > 0 && (
          <div className="ext-box warn"><AlertTriangle size={15} />
            <div>{notes.map((n, i) => <div key={i}>{n}</div>)}</div>
          </div>
        )}

        {items.length > 0 && shown.length === 0 && (
          <p className="muted small">{t("Nothing here matches the filter.")}</p>
        )}

        {shown.length > 0 && (
          <>
            {grouped.map(([app, group]) => (
              <div key={app}>
                <div className="sec">{app}</div>
                <div className="card flush">
                  {group.map((it) => (
                    <div key={it.source + it.name} className="setting-row pad">
                      <div>
                        <div className="sr-title">
                          <label className="ia-pick">
                            <input type="checkbox" checked={picked.has(it.name)} disabled={it.exists || busy}
                                   onChange={() => toggle(it.name)} />
                            <span>{it.avatar ? `${it.avatar} ` : ""}{it.label ?? it.name}</span>
                          </label>
                          {it.exists && <span className="tag">{t("Already here")}</span>}
                        </div>
                        {it.kind === "mcp"
                          ? <div className="mono small">{line(it)}</div>
                          : it.kind === "expert"
                            ? <div className="sr-desc">
                                {it.role ? `${it.role} · ` : ""}{t("{n} characters", { n: it.chars ?? 0 })}
                                {it.label && it.label !== it.name ? ` · ${it.name}` : ""}
                              </div>
                            : <div className="sr-desc">{it.description || t("No description in the file")} · {t("{n} characters", { n: it.chars ?? 0 })}</div>}
                        {(it.bundled_skills ?? 0) > 0 && (
                          <div className="muted small">{t("This package also ships {n} skill(s); import those from the skill sources.", { n: it.bundled_skills ?? 0 })}</div>
                        )}
                        {(it.env_keys?.length ? it.env_keys : it.header_keys ?? []).length > 0 && (
                          <div className="muted small">
                            {t("Needs these values — they are not carried over:")} {(it.env_keys ?? it.header_keys ?? []).join(", ")}
                          </div>
                        )}
                        {(it.risks ?? []).map((r, i) => (
                          <div key={i} className="sr-desc"><AlertTriangle size={11} aria-hidden /> {r}</div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            <div className="sp-head-actions" style={{ marginTop: 10 }}>
              <button className="btn primary" disabled={busy || picked.size === 0} onClick={() => void doImport()}>
                <Download size={14} /> {t("Import {n} item(s)", { n: picked.size })}
              </button>
              <button className="btn" disabled={busy} onClick={onClose}>{t("Cancel")}</button>
            </div>
            {kind === "mcp" && (
              <p className="muted small">
                {t("Imported servers are disabled and are not connected to any group. Open one here to check the command and enable it.")}
              </p>
            )}
          </>
        )}
        {sources && sources.every((s) => !s.found) && (
          <p className="muted small">
            {t("None of the applications this knows about were found on this machine. Paste a configuration by hand instead.")}
          </p>
        )}
      </div>
    </Modal>
  );
}
