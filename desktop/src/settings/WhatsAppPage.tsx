import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw, Smartphone } from "lucide-react";
import { api, type WhatsAppStatus } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Switch } from "../ui";
import { NumInput, Row, useSettingsSaver } from "./rows";

/** Splits whatever the user typed into bare digits, mirroring what the backend stores.
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

/** A random verify token. It is a shared secret with Meta, so it is generated here and the
 *  user pastes the same value into the Meta console while the field is still on screen. */
const randomToken = (): string => {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
};

export default function WhatsAppPage() {
  const { t } = useI18n();
  const { groups, settings } = useData();
  const { set, err, saving } = useSettingsSaver();
  const [status, setStatus] = useState<WhatsAppStatus | null>(null);
  const [allowed, setAllowed] = useState("");
  const [token, setToken] = useState("");
  const [access, setAccess] = useState("");
  const [secret, setSecret] = useState("");
  const [checked, setChecked] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setStatus(await api.whatsappStatus()); } catch { setStatus(null); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { setAllowed((settings?.whatsapp_allowed ?? []).join(", ")); }, [settings?.whatsapp_allowed]);
  // Re-read once the public host changes: the callback URL is assembled by the backend and only
  // read back here, so "what do I paste into Meta" has one implementation rather than two that
  // can disagree — which is exactly how a missing path or a doubled slash gets shipped.
  useEffect(() => { void load(); }, [load, settings?.whatsapp_public_host]);

  if (!settings) return <div className="empty big">{t("Loading…")}</div>;
  const on = settings.whatsapp_enabled;
  const host = settings.whatsapp_public_host.trim();
  const url = status?.public_url ?? "";

  const ready: { ok: boolean; what: string }[] = [
    { ok: !!settings.whatsapp_group_id, what: t("A group chat has been chosen") },
    { ok: (settings.whatsapp_allowed ?? []).length > 0, what: t("At least one sender is allowlisted") },
    { ok: !!settings.whatsapp_app_secret_set, what: t("The app secret is saved") },
    { ok: !!settings.whatsapp_verify_token_set, what: t("The verify token is saved — it has to be the same string in the Meta console") },
    { ok: !!settings.whatsapp_token_set && !!settings.whatsapp_phone_number_id, what: t("The access token and the phone number id are set, so replies can be sent") },
    { ok: !!host, what: t("The public hostname is filled in, so the webhook can be reached at all") },
  ];

  const probe = async () => {
    setBusy(true);
    setChecked(null);
    try {
      const r = await api.whatsappProbe();
      setChecked({ ok: r.ok, text: r.detail });
    } catch (e) {
      setChecked({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
      void load();
    }
  };

  return (
    <div className="sp">
      <h2 className="sp-title"><Smartphone size={20} aria-hidden /> {t("WhatsApp channel")}</h2>
      <p className="sp-desc">
        {t("People message your WhatsApp number; a group chat here answers and the reply is sent back. Off by default, because this is the only part of the app that takes input from outside this machine — and that is why it needs a public address Meta can reach.")}
      </p>
      {(err || checked?.ok === false) && <div className="err" role="alert">{err || checked?.text}</div>}
      {checked?.ok && <div className="ext-box ok" role="status"><CheckCircle2 size={15} /><div>{checked.text}</div></div>}

      <div className="card flush">
        <Row title={t("Turn the channel on")} desc={t("While this is off, the webhook refuses every request.")}>
          <Switch checked={on} disabled={saving} label={t("Turn the channel on")} onChange={(v) => void set({ whatsapp_enabled: v })} />
        </Row>
        <Row title={t("Which group chat answers")} desc={t("A message from WhatsApp is handed to this group as if you had typed it. Without a group the channel does nothing.")}>
          <select className="ext-plan-select" value={settings.whatsapp_group_id} aria-label={t("Which group chat answers")}
                  onChange={(e) => void set({ whatsapp_group_id: e.target.value })}>
            <option value="">{t("None")}</option>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
        </Row>
        <Row title={t("Who may talk to it")} desc={t("Phone numbers separated by commas. Anyone else is dropped and never reaches the group — an empty list means nobody.")}>
          <input className="ext-input" value={allowed} disabled={saving} aria-label={t("Who may talk to it")}
                 placeholder="+86 138 0000 0000"
                 onChange={(e) => setAllowed(e.target.value)}
                 onBlur={() => void set({ whatsapp_allowed: toNumbers(allowed) })} />
        </Row>
        <Row title={t("Reply length limit")} desc={t("Long answers are cut here; WhatsApp itself refuses anything over 4096 characters.")}>
          <NumInput v={settings.whatsapp_max_chars} min={100} max={4096} unit={t("characters")} label={t("Reply length limit")}
                    onCommit={(n) => set({ whatsapp_max_chars: n })} />
        </Row>
        <Row title={t("Text in front of every reply")} desc={t("Optional. Useful when the same group is also used from this app.")}>
          <input className="ext-input" defaultValue={settings.whatsapp_prefix} disabled={saving} aria-label={t("Text in front of every reply")}
                 onBlur={(e) => void set({ whatsapp_prefix: e.target.value })} />
        </Row>
      </div>

      <div className="sec">{t("Credentials from the Meta app")}</div>
      <div className="card flush">
        <div className="ext-box warn" role="note">
          <AlertTriangle size={15} />
          <div>{t("These three values go into the system keychain rather than the database, and this screen never reads them back. Leaving a field empty keeps whatever is already saved.")}</div>
        </div>
        <Row title={t("Phone number id")} desc={t("From WhatsApp → API setup. It is the id, not the phone number itself.")}>
          <input className="ext-input" defaultValue={settings.whatsapp_phone_number_id} disabled={saving} aria-label={t("Phone number id")}
                 onBlur={(e) => void set({ whatsapp_phone_number_id: e.target.value.trim() })} />
        </Row>
        <Row title={t("Access token")} desc={settings.whatsapp_token_set ? t("A token is saved already — leave this empty to keep it.") : t("Used to send replies. A temporary token expires in 24 hours, so make a permanent one.")}>
          <input className="ext-input" type="password" value={access} disabled={saving} aria-label={t("Access token")}
                 placeholder={settings.whatsapp_token_set ? "••••••••" : ""}
                 onChange={(e) => setAccess(e.target.value)}
                 onBlur={() => { if (access) { void set({ whatsapp_token: access }); setAccess(""); } }} />
        </Row>
        <Row title={t("App secret")} desc={t("This is what proves a webhook post really came from Meta. Without it the endpoint refuses everything.")}>
          <input className="ext-input" type="password" value={secret} disabled={saving} aria-label={t("App secret")}
                 placeholder={settings.whatsapp_app_secret_set ? "••••••••" : ""}
                 onChange={(e) => setSecret(e.target.value)}
                 onBlur={() => { if (secret) { void set({ whatsapp_app_secret: secret }); setSecret(""); } }} />
        </Row>
        <Row title={t("Verify token")} desc={settings.whatsapp_verify_token_set ? t("A token is saved already. It has to match what you typed into the Meta console.") : t("A string you invent. Meta echoes it back once when you save the callback URL, so paste the same value into Meta.")}>
          <div className="ext-inline-input">
            <input className="ext-input" value={token} disabled={saving} aria-label={t("Verify token")}
                   placeholder={settings.whatsapp_verify_token_set ? "••••••••" : ""}
                   onChange={(e) => setToken(e.target.value)} />
            <button className="btn small" type="button" onClick={() => setToken(randomToken())}>{t("Generate")}</button>
            <button className="btn small" type="button" disabled={!token || saving} onClick={() => { void set({ whatsapp_verify_token: token }); setToken(""); }}>
              {t("Save")}
            </button>
          </div>
        </Row>
      </div>

      <div className="sec">{t("Getting messages delivered")}</div>
      <div className="card flush">
        <Row title={t("Callback URL")} desc={t("Paste this into the Meta console under WhatsApp → Configuration → Webhook, together with the verify token above.")}>
          <span className={url ? "mono" : "muted"}>{url || (host ? t("Reading status…") : t("Fill in the public hostname first"))}</span>
        </Row>
        <Row title={t("Public hostname")} desc={t("The tunnel or server that forwards to this app — cloudflared, ngrok, or your own VPS. Meta rejects localhost and private addresses, so one is required. Restart the app after changing this.")}>
          <input className="ext-input" defaultValue={settings.whatsapp_public_host} disabled={saving} aria-label={t("Public hostname")}
                 placeholder="abc.trycloudflare.com"
                 onBlur={(e) => void set({ whatsapp_public_host: e.target.value.trim() })} />
        </Row>
        <Row title={t("Proxy")} desc={t("graph.facebook.com cannot be reached from mainland China, so sending a reply needs a proxy here. Leave it empty to use the system proxy.")}>
          <input className="ext-input" defaultValue={settings.whatsapp_proxy} disabled={saving} aria-label={t("Proxy")}
                 placeholder="http://127.0.0.1:7890"
                 onBlur={(e) => void set({ whatsapp_proxy: e.target.value.trim() })} />
        </Row>
        <Row title={t("Check the connection")} desc={t("Reads the phone number's own metadata: one request, and no message is sent to anybody. It proves the token, the number id and the proxy work together.")}>
          <button className="btn small" type="button" disabled={busy} onClick={() => void probe()}>
            <RefreshCw size={12} className={busy ? "spin" : ""} /> {t("Check now")}
          </button>
        </Row>
      </div>

      <div className="sec">{t("What is still missing")}</div>
      <div className="card flush">
        {ready.map((x, i) => (
          <div key={i} className="setting-row pad">
            <div>
              <div className="sr-title">{x.ok ? <CheckCircle2 size={13} aria-hidden /> : <AlertTriangle size={13} aria-hidden />} {x.what}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="sec">{t("Recent activity")}</div>
      <div className="card flush">
        {!status ? <div className="muted small">{t("Reading status…")}</div> : (
          <>
            <Row title={t("Counters")} desc={t("accepted / rejected / ignored")}>
              <span className="muted">{status.counters.accepted} / {status.counters.rejected} / {status.counters.ignored}</span>
            </Row>
            <Row title={t("Last message received")} desc={status.counters.last_inbound.text || t("Nothing has arrived yet")}>
              <span className="muted small">{status.counters.last_inbound.wa_id || "—"}</span>
            </Row>
            <Row title={t("Last reply sent")} desc={status.counters.last_reply.detail || t("Nothing has been sent yet")}>
              <span className={status.counters.last_reply.ok === false ? "err small" : "muted small"}>
                {status.counters.last_reply.ok === false ? t("Failed") : status.counters.last_reply.ok ? t("Sent") : "—"}
              </span>
            </Row>
            {status.counters.last_error && <div className="muted small">{status.counters.last_error}</div>}
            <Row title={t("Refresh")} desc="">
              <button className="btn small" type="button" onClick={() => void load()}><RefreshCw size={12} /> {t("Refresh")}</button>
            </Row>
          </>
        )}
      </div>

      <p className="sp-desc">
        <Smartphone size={13} aria-hidden /> {t("A round started from WhatsApp may only use read-only tools: it can search the library and your memory, but it can never run code or write files. Nobody is sitting at this machine to approve anything.")}
      </p>
    </div>
  );
}
