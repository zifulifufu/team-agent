import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronRight, Loader2, PackagePlus, Search, ShieldCheck } from "lucide-react";
import { api, type GalleryApplyResult, type GalleryItem, type GalleryKind, type GalleryOverview } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import type { PageProps } from "./SettingsModal";
import "../styles/gallery.css";

type Tab = GalleryKind | "all";

const S = (v: unknown) => (v == null ? "" : String(v));
const A = (v: unknown) => (Array.isArray(v) ? v.map(String) : []);

/** Example of the custom template file format (drop one into the data directory and it shows up on this page). */
const SAMPLE = `{
  "schema_version": 1,
  "catalog_version": "1.0.0",
  "author": "Your team name",
  "items": [
    {
      "id": "weekly-report",
      "kind": "team",
      "name": "Weekly report team",
      "summary": "Produce one weekly report every week",
      "icon": "🗓️",
      "members": ["Host", "Scribe", "Reviewer"],
      "host": "Host",
      "skills": ["Official writing style"],
      "prompt": "This group summarises progress once a week."
    },
    {
      "id": "tone",
      "kind": "skill",
      "name": "External wording rules",
      "summary": "Tone requirements for outside communication",
      "body": "Always use a formal tone externally; never promise a schedule."
    }
  ]
}`;

/**
 * Settings → Template gallery.
 *
 * Templates ship with the app and work straight away: no repository to clone, no
 * paths to fill in, no network needed.
 * Every entry is written by this project (see Licence and compliance in the README),
 * so shipping them carries no third-party licence obligations.
 * To add your own team templates, drop JSON into templates/ under the data directory
 * (this page shows what was loaded, and why anything failed).
 */
export default function GalleryPage({ onTab, onOpenGroup }: PageProps) {
  const { t } = useI18n();
  const { groups, reload, reloadGroups } = useData();
  const [ov, setOv] = useState<GalleryOverview | null>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<Tab>("all");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState("");
  const [details, setDetails] = useState<Record<string, GalleryItem & { def: Record<string, unknown> }>>({});
  const [busy, setBusy] = useState("");
  const [res, setRes] = useState<GalleryApplyResult | null>(null);
  const [join, setJoin] = useState("");
  const [showSchema, setShowSchema] = useState(false);

  const load = useCallback(() => {
    api.gallery().then(setOv).catch((e) => setErr((e as Error).message));
  }, []);
  useEffect(load, [load]);

  const apply = async (it: GalleryItem, overwrite = false) => {
    if (busy) return;
    setBusy(it.id);
    setRes(null);
    setErr("");
    try {
      const r = await api.galleryApply(it.id, {
        overwrite: overwrite || undefined,
        group_id: it.kind === "agent" && join ? join : undefined,
      });
      setRes(r);
      await reload();          // a member may have been created
      await reloadGroups();    // a group may have been created
      load();                  // refresh the installed state
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  const toggle = async (it: GalleryItem) => {
    if (open === it.id) {
      setOpen("");
      return;
    }
    setOpen(it.id);
    if (details[it.id]) return;
    try {
      const d = await api.galleryDetail(it.id);
      setDetails((m) => ({ ...m, [it.id]: d }));
    } catch {
      /* If the detail cannot be fetched you can still use the template */
    }
  };

  const items = useMemo(() => {
    if (!ov) return [];
    const k = q.trim().toLowerCase();
    return ov.items.filter((i) => (tab === "all" || i.kind === tab)
      && (!k || [i.name, i.summary, ...i.tags, i.id].some((x) => (x || "").toLowerCase().includes(k))));
  }, [ov, tab, q]);

  if (!ov) return <div className="empty big">{err || t("Loading…")}</div>;

  const tabs: { id: Tab; label: string; n: number }[] = [
    { id: "all", label: t("All"), n: ov.total },
    ...ov.categories.map((c) => ({ id: c.id as Tab, label: c.label, n: ov.counts[c.id] ?? 0 })),
  ];
  const hint = ov.categories.find((c) => c.id === tab)?.hint ?? t("First-party templates shipped with the app — one click and it is ready. Your own template JSON goes below.");

  const actionLabel = (it: GalleryItem) => {
    if (it.kind === "team") return t("Create a group chat");
    if (it.kind === "agent") return join ? t("Create and join a group") : t("Create a member");
    if (it.kind === "skill") return it.installed ? t("Import again") : t("Import the skill");
    if (it.kind === "prompt") return it.installed ? t("Overwrite with the template version") : t("Save to the prompt library");
    return t("Add (disabled)");
  };

  const Detail = ({ it }: { it: GalleryItem }) => {
    const { t } = useI18n();
    const d = details[it.id];
    const p = it.preview;
    if (!d) return <div className="gl-detail muted small">{t("Loading the content…")}</div>;
    return (
      <div className="gl-detail">
        {it.kind === "team" && (
          <>
            <div className="gl-roles">
              {(p.members ?? []).map((m) => (
                <span key={m.name} className={"gl-role" + (m.name === p.host ? " host" : "")}
                      title={m.name === p.host ? t("{name} (host)", { name: m.name }) : m.role || m.name}>
                  <b>{m.avatar} {m.name}</b>{m.role && <i>{m.role}</i>}
                </span>
              ))}
            </div>
            {A(d.def.skills).length > 0 && (
              <div className="gl-line">{t("Included skills:")} {A(d.def.skills).map((s) => <span key={s} className="tag on">{s}</span>)}</div>
            )}
            {S(d.def.prompt) && <div className="gl-line">{t("Group rules:")} {S(d.def.prompt)}</div>}
          </>
        )}
        {it.kind === "agent" && (
          <>
            <div className="gl-line">{S(d.def.avatar)} {S(d.def.role)}{A(d.def.tags).length > 0 && <> · {t("Strengths")} {A(d.def.tags).join(" / ")}</>}</div>
            <pre className="gl-body">{S(d.def.prompt)}</pre>
          </>
        )}
        {it.kind === "skill" && (
          <>
            <div className="gl-line">
              {t("Used for:")} {S(d.def.scope) === "group" ? t("chat rules (attached to the whole group)") : t("member skills (tick them for members)")}
            </div>
            <pre className="gl-body">{S(d.def.body)}</pre>
          </>
        )}
        {it.kind === "prompt" && (
          <>
            <div className="gl-line">{t("Type:")} {S(d.def.kind) === "group" ? t("group prompt") : t("general prompt")}</div>
            <pre className="gl-body">{S(d.def.content)}</pre>
          </>
        )}
        {it.kind === "mcp" && (
          <>
            <code className="gl-cmd">{[S(d.def.command), ...A(d.def.args)].join(" ")}</code>
            <div className="gl-line">{S(d.def.note)}</div>
            {A(d.def.env_keys).length > 0 && <div className="gl-line">{t("You need to fill in:")} {A(d.def.env_keys).join(", ")}</div>}
          </>
        )}
      </div>
    );
  };

  return (
    <div className="sp">
      <h2 className="sp-title"><PackagePlus size={20} aria-hidden /> {t("Template gallery")}</h2>
      <p className="sp-desc">
        {t("Ready-made teams, roles, skills, prompts, and MCP setups,")}<b>{t("one click and it is ready")}</b>{t(" — nothing to download and no paths to fill in; it all happens on this machine. The templates are original content shipped with the app (catalog version ")}{ov.catalog_version}{t("), and contain no files or data from any third-party project.")}
      </p>

      {err && <div className="gl-msg bad" role="alert">{err}</div>}

      {res && (
        <div className="gl-result" role="status">
          <div className="gl-result-top">
            <b>{res.name}</b>
            <span>{res.summary}</span>
          </div>
          {res.added.length > 0 && <div className="gl-line ok">{t("Done:")} {res.added.join(", ")}</div>}
          {res.skipped.length > 0 && <div className="gl-line">{t("Already present, left alone:")} {res.skipped.join(", ")}</div>}
          {res.notes.map((n) => <div className="gl-line note" key={n}><AlertTriangle size={12} aria-hidden /> {n}</div>)}
          {res.group && onOpenGroup && (
            <button className="btn small primary" onClick={() => onOpenGroup(res.group!.id)}>{t("Open the group chat {name}", { name: res.group.name })}</button>
          )}
          {res.kind === "skill" && <button className="btn small" onClick={() => onTab("skills")}>{t("Go and tick the skills")}</button>}
          {res.kind === "prompt" && <button className="btn small" onClick={() => onTab("prompts")}>{t("Go and attach prompts")}</button>}
          {res.kind === "mcp" && <button className="btn small" onClick={() => onTab("mcp")}>{t("Review and enable")}</button>}
        </div>
      )}

      <div className="gl-tabs" role="tablist">
        {tabs.map((tabItem) => (
          <button key={tabItem.id} role="tab" aria-selected={tab === tabItem.id} className={"gl-tab" + (tab === tabItem.id ? " on" : "")}
                  onClick={() => { setTab(tabItem.id); setOpen(""); }}>
            {tabItem.label} <span className="gl-count">{tabItem.n}</span>
          </button>
        ))}
        <label className="gl-search">
          <Search size={13} aria-hidden />
          <input value={q} placeholder={t("Search templates…")} aria-label={t("Search templates")} onChange={(e) => setQ(e.target.value)} />
        </label>
      </div>
      <p className="gl-hint">{hint}</p>

      {tab === "agent" && groups.length > 0 && (
        <div className="gl-join">
          <span>{t("After creating the member:")}</span>
          <select value={join} onChange={(e) => setJoin(e.target.value)} aria-label={t("Which group to join after creating it")}>
            <option value="">{t("Create only, do not join a group")}</option>
            {groups.map((g) => <option key={g.id} value={g.id}>{t("Also join {name}", { name: g.name })}</option>)}
          </select>
        </div>
      )}

      <div className="card flush">
        {items.length === 0
          ? <div className="empty">{t("No templates match")}</div>
          : items.map((it) => {
            const expanded = open === it.id;
            return (
              <div className={"gl-item" + (expanded ? " open" : "")} key={it.id}>
                <button className="gl-head" aria-expanded={expanded} onClick={() => void toggle(it)}>
                  <ChevronRight size={14} className={"gl-chev" + (expanded ? " open" : "")} aria-hidden />
                  <span className="gl-icon" aria-hidden>{it.icon}</span>
                  <span className="gl-main">
                    <span className="gl-title">
                      {it.name}
                      {it.tags.map((tag) => <span className="tag" key={tag}>{tag}</span>)}
                      {it.source.startsWith("custom:") && <span className="tag new">{t("Your own")}</span>}
                    </span>
                    <span className="gl-sum">{it.summary}</span>
                  </span>
                </button>
                <div className="gl-side">
                  {it.installed && <span className="gl-state"><ShieldCheck size={12} aria-hidden /> {it.state_note}</span>}
                  <button className="btn small primary" disabled={!!busy || (it.kind === "mcp" && it.installed)}
                          onClick={() => void apply(it, it.installed && it.kind !== "mcp")}>
                    {busy === it.id && <Loader2 size={12} className="spin" aria-hidden />} {actionLabel(it)}
                  </button>
                </div>
                {expanded && <Detail it={it} />}
              </div>
            );
          })}
      </div>

      <div className="sec">{t("Custom templates (optional)")}</div>
      <div className="card gl-custom">
        <div className="gl-line">
          {t("Want to add your team's own templates? Drop a JSON file into the directory below and it appears on this page —")} <b>{t("just refresh after editing, no restart needed")}</b>{t("; entries with a bad format are skipped, with the reason shown.")}
        </div>
        <code className="gl-cmd">{ov.custom.dir}</code>
        <div className="gl-line muted small">
          {t("Currently loaded: {n} entries", { n: ov.custom.loaded })}
          {ov.custom.files.length > 0 && <> — {ov.custom.files.map((f) => `${f.name} (${f.items} items${f.author ? `, ${f.author}` : ""})`).join("; ")}</>}
          {!ov.custom.exists && <> — {t("the directory does not exist yet; create one if you need it")}</>}
        </div>
        {ov.custom.errors.length > 0 && (
          <div className="gl-errors">
            {ov.custom.errors.map((e, i) => (
              <div className="gl-line bad" key={`${e.file}-${i}`}>
                <AlertTriangle size={12} aria-hidden /> {e.file}{e.id ? ` · ${e.id}` : ""}:{e.reason}
              </div>
            ))}
          </div>
        )}
        <button className="link small" onClick={() => setShowSchema((v) => !v)}>
          {showSchema ? t("Hide the format guide") : t("Show the format guide and example")}
        </button>
        {showSchema && (
          <>
            <div className="gl-line small">
              {t("Supported")} <code>kind</code>{t(": team / agent / skill / prompt.")}
              <b>{t("the command kind (MCP) is not accepted")}</b>{t(" — that would let one JSON file decide what runs on this machine; add those yourself on the MCP page. The kind of a prompt goes in ")}<code>prompt_kind</code> {t("(general or group); the optional")} <code>requires</code> {t("gives the minimum required catalog version.")}
            </div>
            <pre className="gl-body">{SAMPLE}</pre>
          </>
        )}
      </div>
    </div>
  );
}
