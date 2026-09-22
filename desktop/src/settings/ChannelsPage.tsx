import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, ExternalLink, RefreshCw, Send, Webhook, type LucideIcon } from "lucide-react";
import { api, type ChannelField, type ChannelInfo } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Switch } from "../ui";
import { Row } from "./rows";

/** Splits a whitespace/dash-grouped list into the entries a phone-number allowlist holds.
 *  Commas and newlines separate; spaces and dashes belong to one number, so
 *  "+86 138-0000-0000" stays a single entry instead of becoming two bogus ones. */
export const toNumbers = (raw: string): string[] => {
  const out: string[] = [];
  for (const piece of raw.split(/[,;\n]+/)) {
    const d = piece.replace(/\D/g, "");
    if (d && !out.includes(d)) out.push(d);
  }
  return out;
};

/** A random shared secret. It is generated here and pasted into the platform's console
 *  while the field is still on screen. */
const randomToken = (): string => {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
};

/** What one field looks like while it is being typed. Secrets are write-only, so they are
 *  held here until submitted and the saved value is never fetched back. */
function Field({ field, groups, saving, onSave }: {
  field: ChannelField;
  groups: { id: string; name: string }[];
  saving: boolean;
  onSave: (patch: Record<string, unknown>) => void;
}) {
  const { t } = useI18n();
  const [text, setText] = useState("");

  useEffect(() => {
    if (field.kind === "ids" || field.kind === "numbers") {
      setText(Array.isArray(field.value) ? field.value.join(", ") : String(field.value ?? ""));
    } else if (field.secret) {
      setText("");
    }
  }, [field.value, field.kind, field.secret, field.set]);

  if (field.kind === "switch") {
    return (
      <Row title={field.label} desc={field.desc}>
        <Switch checked={!!field.value} disabled={saving} label={field.label}
                onChange={(v) => onSave({ [field.key]: v })} />
      </Row>
    );
  }
  if (field.kind === "group") {
    return (
      <Row title={field.label} desc={field.desc}>
        <select className="ext-plan-select" value={String(field.value ?? "")} aria-label={field.label}
                onChange={(e) => onSave({ [field.key]: e.target.value })}>
          <option value="">{t("None")}</option>
          {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
        </select>
      </Row>
    );
  }
  if (field.kind === "number") {
    return (
      <Row title={field.label} desc={field.desc}>
        <input className="ext-input" type="number" defaultValue={Number(field.value ?? 0)} disabled={saving}
               aria-label={field.label} min={field.min ?? undefined} max={field.max ?? undefined}
               onBlur={(e) => onSave({ [field.key]: Number(e.target.value) })} />
        {field.unit ? <span className="muted small">{field.unit}</span> : null}
      </Row>
    );
  }
  if (field.secret) {
    return (
      <Row title={field.label} desc={field.desc}>
        <div className="ext-inline-input">
          <input className="ext-input" type="password" value={text} disabled={saving} aria-label={field.label}
                 placeholder={field.set ? t("A value is saved already — leave this empty to keep it") : (field.placeholder || "")}
                 onChange={(e) => setText(e.target.value)} />
          <button className="btn small" type="button" disabled={!text || saving}
                  onClick={() => { onSave({ [field.key]: text }); setText(""); }}>
            {t("Save")}
          </button>
        </div>
      </Row>
    );
  }
  if (field.kind === "ids" || field.kind === "numbers") {
    return (
      <Row title={field.label} desc={field.desc}>
        <input className="ext-input" value={text} disabled={saving} aria-label={field.label}
               placeholder={field.placeholder || ""}
               onChange={(e) => setText(e.target.value)}
               onBlur={() => onSave({ [field.key]: field.kind === "numbers" ? toNumbers(text) : text })} />
      </Row>
    );
  }
  // A plain text field. "Generate" appears where the platform expects a shared secret the
  // user invents (Meta's verify token), which is the only field that needs a random value.
  const wantsGenerate = field.key === "verify_token";
  return (
    <Row title={field.label} desc={field.desc}>
      {wantsGenerate ? (
        <div className="ext-inline-input">
          <input className="ext-input" value={text} disabled={saving} aria-label={field.label}
                 placeholder={field.set ? t("A value is saved already — leave this empty to keep it") : (field.placeholder || "")}
                 onChange={(e) => setText(e.target.value)} />
          <button className="btn small" type="button" onClick={() => setText(randomToken())}>{t("Generate")}</button>
          <button className="btn small" type="button" disabled={!text || saving}
                  onClick={() => { onSave({ [field.key]: text }); setText(""); }}>
            {t("Save")}
          </button>
        </div>
      ) : (
        <input className="ext-input" key={String(field.value)} defaultValue={String(field.value ?? "")}
               disabled={saving} aria-label={field.label} placeholder={field.placeholder || ""}
               onBlur={(e) => onSave({ [field.key]: e.target.value.trim() })} />
      )}
    </Row>
  );
}

/** How each way of receiving is drawn: a webhook the platform calls, a poll this app
 *  drives, or a robot that only accepts pushes. */
const TRANSPORT_ICON: Record<string, LucideIcon> = { webhook: Webhook, poll: RefreshCw, robot: Send };

export default function ChannelsPage() {
  const { t } = useI18n();
  const { groups } = useData();
  const [list, setList] = useState<ChannelInfo[] | null>(null);
  const [open, setOpen] = useState("");
  const [checked, setChecked] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const got = (await api.channels()).channels;
      setList(got);
      setOpen((cur) => cur || got[0]?.id || "");
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const current = useMemo(() => (list ?? []).find((c) => c.id === open) ?? null, [list, open]);

  // Saving goes through the channel route rather than the generic settings one: the
  // backend re-validates each field against its own table, so bounds and allowlists cannot
  // be bypassed by a hand-crafted request.
  const save = async (patch: Record<string, unknown>) => {
    if (!current) return;
    setErr("");
    setSaving(true);
    try {
      const r = await api.setChannel(current.id, patch);
      if (!r.ready && r.missing.length) setErr(r.missing.join(" · "));
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const run = async (what: "probe" | "test" | "reconnect") => {
    if (!current) return;
    setBusy(what);
    setChecked(null);
    setErr("");
    try {
      const r = what === "probe" ? await api.channelProbe(current.id)
        : what === "test" ? await api.channelTest(current.id)
          : await api.channelReconnect(current.id);
      setChecked({ ok: r.ok, text: "detail" in r ? r.detail : t("Reconnected") });
    } catch (e) {
      setChecked({ ok: false, text: (e as Error).message });
    } finally {
      setBusy("");
      await load();
    }
  };

  if (!list) return <div className="empty big">{err || t("Loading…")}</div>;

  return (
    <div className="sp">
      <h2 className="sp-title"><Webhook size={20} aria-hidden /> {t("Chat channels")}</h2>
      <p className="sp-desc">
        {t("Connect a chat platform so its messages drive a group chat here, or so what the group produces is pushed into one. Every channel is off by default, and each authenticates its own traffic — this is the only part of the app that takes input from outside this machine.")}
      </p>
      {err && <div className="err" role="alert">{err}</div>}

      <div className="chan-tabs" role="tablist" aria-label={t("Chat channels")}>
        {list.map((c) => {
          const Icon = TRANSPORT_ICON[c.transport] ?? Webhook;
          return (
            <button key={c.id} role="tab" aria-selected={c.id === open}
                    className={"chan-tab" + (c.id === open ? " on" : "")}
                    onClick={() => { setOpen(c.id); setChecked(null); setErr(""); }}>
              <span className="chan-emoji" aria-hidden>{c.avatar}</span>
              <span className="chan-name">{c.name}</span>
              <span className={"chan-dot" + (c.settings.enabled && c.ready ? " on" : c.settings.enabled ? " warn" : "")}>
                {c.settings.enabled && c.ready ? <CheckCircle2 size={11} aria-hidden />
                  : c.settings.enabled ? <AlertTriangle size={11} aria-hidden /> : <Icon size={11} aria-hidden />}
              </span>
            </button>
          );
        })}
      </div>

      {current && (
        <>
          <p className="sp-desc">
            {current.summary}
            {current.direction === "out" && ` ${t("This one only pushes: nobody can talk to the group through it.")}`}
            {" "}
            <a href={current.docs} target="_blank" rel="noreferrer">{t("Open the platform's docs")} <ExternalLink size={11} aria-hidden /></a>
          </p>
          {checked?.ok === false && <div className="err" role="alert">{checked.text}</div>}
          {checked?.ok && <div className="ext-box ok" role="status"><CheckCircle2 size={15} /><div>{checked.text}</div></div>}

          {current.setup.length > 0 && (
            <>
              <div className="sec">{t("What you do on the platform's side")}</div>
              <div className="card flush">
                {current.setup.map((step, i) => (
                  <div key={i} className="setting-row pad">
                    <div className="sr-title">{i + 1}. {step}</div>
                  </div>
                ))}
              </div>
            </>
          )}

          <div className="sec">{t("Configuration")}</div>
          <div className="card flush">
            {current.fields.map((f) => (
              <Field key={f.key} field={f} groups={groups} saving={saving} onSave={save} />
            ))}
          </div>

          {current.needs_public_url && (
            <>
              <div className="sec">{t("Getting messages delivered")}</div>
              <div className="card flush">
                <Row title={t("Callback URL")}
                     desc={t("Paste this into the platform's console next to the verify token above.")}>
                  <span className={current.public_url ? "mono" : "muted"}>
                    {current.public_url || t("Fill in the public hostname first")}
                  </span>
                </Row>
              </div>
            </>
          )}

          <div className="sec">{t("Checks")}</div>
          <div className="card flush">
            <Row title={t("Check the connection")}
                 desc={t("Reads what the platform reports about its own configuration: a couple of requests, and nothing is sent to anybody.")}>
              <button className="btn small" type="button" disabled={!!busy} onClick={() => void run("probe")}>
                <RefreshCw size={12} className={busy === "probe" ? "spin" : ""} /> {t("Check now")}
              </button>
            </Row>
            <Row title={t("Send a test message")}
                 desc={t("The only honest test: it really does send one, into the room or to the first allowlisted sender.")}>
              <button className="btn small" type="button" disabled={!!busy} onClick={() => void run("test")}>
                <Send size={12} className={busy === "test" ? "spin" : ""} /> {t("Send test")}
              </button>
            </Row>
            {current.transport === "poll" && (
              <Row title={t("Restart the poller")}
                   desc={t("This app fetches messages from the platform itself; restarting re-reads the configuration right away instead of within a few seconds.")}>
                <button className="btn small" type="button" disabled={!!busy} onClick={() => void run("reconnect")}>
                  <RefreshCw size={12} className={busy === "reconnect" ? "spin" : ""} /> {t("Reconnect")}
                  {current.counters.poller ? ` · ${current.counters.poller}` : ""}
                </button>
              </Row>
            )}
          </div>

          <div className="sec">{t("What is still missing")}</div>
          <div className="card flush">
            {current.ready ? (
              <div className="setting-row pad">
                <div className="sr-title"><CheckCircle2 size={13} aria-hidden /> {t("Everything this channel needs is filled in")}</div>
              </div>
            ) : current.missing.map((what, i) => (
              <div key={i} className="setting-row pad">
                <div className="sr-title"><AlertTriangle size={13} aria-hidden /> {what}</div>
              </div>
            ))}
          </div>

          <div className="sec">{t("Recent activity")}</div>
          <div className="card flush">
            <Row title={t("Counters")} desc={t("accepted / rejected / ignored")}>
              <span className="muted">
                {current.counters.accepted} / {current.counters.rejected} / {current.counters.ignored}
              </span>
            </Row>
            <Row title={t("Last message received")}
                 desc={current.counters.last_inbound.text || t("Nothing has arrived yet")}>
              <span className="muted small">{current.counters.last_inbound.sender || "—"}</span>
            </Row>
            <Row title={t("Last reply sent")}
                 desc={current.counters.last_reply.detail || t("Nothing has been sent yet")}>
              <span className={current.counters.last_reply.ok === false ? "err small" : "muted small"}>
                {current.counters.last_reply.ok === false ? t("Failed") : current.counters.last_reply.ok ? t("Sent") : "—"}
              </span>
            </Row>
            {current.counters.last_error && <div className="muted small">{current.counters.last_error}</div>}
            <Row title={t("Refresh")} desc="">
              <button className="btn small" type="button" onClick={() => void load()}><RefreshCw size={12} /> {t("Refresh")}</button>
            </Row>
          </div>

          <p className="sp-desc">
            <AlertTriangle size={13} aria-hidden /> {t("A round started from outside may only use read-only tools: it can search the library and your memory, but it can never run code or write files. Nobody is sitting at this machine to approve anything.")}
          </p>
        </>
      )}
    </div>
  );
}
