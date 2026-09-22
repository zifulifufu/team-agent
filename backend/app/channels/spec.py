"""The catalogue of chat channels — one place that says what can be connected and
what each one needs.

Everything else is derived from this table: the settings keys (and their defaults,
numeric ranges and which ones are secrets), the webhook paths, and the form the
settings page renders. That is deliberate. Adding a platform should mean adding a
block here plus an adapter module, **not** editing four files in step — the failure
mode of a hand-maintained list is a channel that appears in the UI but whose settings
the store silently drops because nobody registered the key.

Two kinds of channel:

* **both** — messages arrive from outside and the group answers back to the sender.
  These need a way in: a webhook (`transport="webhook"`, so a public address is
  required) or polling (`transport="poll"`, which needs nothing inbound at all).
* **out** — pushes only. Nobody can talk to the group through a group robot, so these
  forward what the group already produced into that room.

Labels and descriptions carry `_zh` twins; `i18n.localize()` swaps them at the API
boundary, which is the project's rule for built-in content.
"""

from __future__ import annotations

from typing import Any

CHANNEL_ORDER = ("whatsapp", "telegram", "wecom", "feishu", "dingtalk", "slack")

# Field kinds the settings page knows how to render.
KINDS = ("switch", "text", "secret", "number", "group", "numbers", "ids")


def _f(key: str, kind: str, *, label: str, label_zh: str, desc: str, desc_zh: str,
       default: Any = None, minimum: int | None = None, maximum: int | None = None,
       unit: str = "", unit_zh: str = "", placeholder: str = "", rows: int = 1,
       ident: str = "") -> dict:
    """One field.

    `ident` is the keychain account name for a secret and defaults to the field key with
    dashes. It exists to be pinned explicitly where it would otherwise change: the account
    name is what an already-saved credential is filed under, so renaming a field silently
    loses the credential the user already pasted in.
    """
    return {"key": key, "kind": kind, "label": label, "label_zh": label_zh,
            "desc": desc, "desc_zh": desc_zh, "default": default,
            "min": minimum, "max": maximum, "unit": unit, "unit_zh": unit_zh,
            "placeholder": placeholder, "rows": rows,
            "ident": ident or key.replace("_", "-")}


# Builders for the fields every channel has, so the wording stays consistent and a
# change to it lands everywhere at once.
def _enabled(what: str, what_zh: str) -> dict:
    return _f("enabled", "switch", default=False,
              label=f"Turn {what} on",
              label_zh=f"启用{what_zh}",
              desc="While this is off nothing is sent and nothing is accepted.",
              desc_zh="关闭时既不发送也不接收任何内容。")


def _group() -> dict:
    return _f("group_id", "group", default="",
              label="Which group chat answers",
              label_zh="由哪个群聊应答",
              desc="A message from outside is handed to this group as if you had typed it.",
              desc_zh="来自外部的消息会交给这个群,就像你自己发的一样。")


def _prefix() -> dict:
    return _f("prefix", "text", default="",
              label="Text in front of every message",
              label_zh="每条消息前的文字",
              desc="Optional. Useful when the same group is also used from this app.",
              desc_zh="可留空。同一个群也在这个应用里使用时比较有用。")


def _limit(default: int, maximum: int, note: str = "", note_zh: str = "") -> dict:
    tail = f" {note}" if note else ""
    tail_zh = note_zh or ""
    return _f("max_chars", "number", default=default, minimum=100, maximum=maximum,
              unit="characters", unit_zh="字符",
              label="Message length limit",
              label_zh="消息长度上限",
              desc=f"Long answers are cut here.{tail}",
              desc_zh=f"过长的回答会在这里截断。{tail_zh}")


def _out_room() -> dict:
    """The robot webhook URL, which is itself the credential — treat it as a secret so it
    never comes back out over the API, and so it lands in the keychain rather than in a
    database that ends up in backups."""
    return _f("webhook_url", "secret", default="",
              label="Robot webhook address",
              label_zh="机器人 Webhook 地址",
              desc="Copy it from the group's robot settings. It contains the key, so it is stored in the system keychain and this screen never reads it back.",
              desc_zh="从群机器人设置里复制。它自带密钥,因此存进系统钥匙串,这个界面不会回显。")


def _signing() -> dict:
    return _f("signing_secret", "secret", default="",
              label="Signing secret",
              label_zh="签名密钥",
              desc="Only if the robot has signature verification switched on — leave it empty otherwise.",
              desc_zh="仅当机器人开启了签名校验时填写,否则留空。")


def _on_answer() -> dict:
    return _f("on_answer", "switch", default=True,
              label="Push what the group produces",
              label_zh="把群里的成果推送过来",
              desc="Every finished answer in the bound group is forwarded into that room.",
              desc_zh="绑定群里每一条完成的回答都会转发到那个群。")


def _allowed(what: str, what_zh: str, placeholder: str) -> dict:
    return _f("allowed", "ids", default=[],
              label="Who may talk to it",
              label_zh="谁可以对它说话",
              desc=f"{what}. Anyone else is dropped and never reaches the group — an empty list means nobody.",
              desc_zh=f"{what_zh}。其他来源一律丢弃,不会进入群;留空表示谁都不行。",
              placeholder=placeholder)


def _proxy() -> dict:
    return _f("proxy", "text", default="",
              label="Proxy",
              label_zh="代理",
              desc="Leave it empty to use the system proxy. Set it explicitly when the platform needs one and the system has none.",
              desc_zh="留空则使用系统代理;平台需要代理而系统没有时,在这里显式填写。",
              placeholder="http://127.0.0.1:7890")


CHANNELS: dict[str, dict] = {
    "whatsapp": {
        "id": "whatsapp",
        "name": "WhatsApp",
        "name_zh": "WhatsApp",
        "avatar": "📱",
        "direction": "both",
        "transport": "webhook",
        "needs_public_url": True,
        "docs": "https://developers.facebook.com/docs/whatsapp/cloud-api/guides/set-up-webhooks",
        "summary": "People message your WhatsApp number and a group chat here answers.",
        "summary_zh": "别人给你的 WhatsApp 号码发消息,由这里的群聊回答。",
        "setup": [
            "Create a Meta app with the WhatsApp product; note the phone number id, the access token and the app secret.",
            "Expose this app publicly (cloudflared, ngrok, or your own server) and put that hostname below.",
            "In the Meta console, register the callback URL and the verify token, and subscribe to `messages`.",
        ],
        "setup_zh": [
            "建一个开通 WhatsApp 产品的 Meta 应用,记下 phone number id、访问令牌和 App Secret。",
            "把本应用暴露到公网(cloudflared、ngrok 或自己的服务器),把域名填在下面。",
            "在 Meta 后台填上回调地址与 verify token,并订阅 `messages` 事件。",
        ],
        "fields": [
            _enabled("the channel", "这条通道"),
            _group(),
            _allowed("Phone numbers separated by commas", "逗号分隔的手机号", "+86 138 0000 0000"),
            _limit(1500, 4096, "WhatsApp itself refuses anything over 4096.", "WhatsApp 本身不接受超过 4096 字符。"),
            _prefix(),
            _f("phone_number_id", "text", default="",
               label="Phone number id", label_zh="Phone number id",
               desc="From WhatsApp → API setup. It is the id, not the phone number itself.",
               desc_zh="在 WhatsApp → API setup 里。这是编号,不是电话号码本身。"),
            _f("token", "secret", default="", ident="access-token",
               label="Access token", label_zh="访问令牌",
               desc="Used to send replies. A temporary token expires in 24 hours, so make a permanent one.",
               desc_zh="用来发送回复。临时令牌 24 小时就过期,请申请永久令牌。"),
            _f("app_secret", "secret", default="",
               label="App secret", label_zh="App Secret",
               desc="This is what proves a webhook post really came from Meta. Without it the endpoint refuses everything.",
               desc_zh="用来证明收到的请求确实来自 Meta。没有它,接口会拒绝一切请求。"),
            _f("verify_token", "secret", default="",
               label="Verify token", label_zh="Verify token",
               desc="A string you invent. Meta echoes it back once when you save the callback URL, so paste the same value into Meta.",
               desc_zh="自己编一个字符串。保存回调地址时 Meta 会回显一次,把它填进 Meta 后台。"),
            _f("public_host", "text", default="",
               label="Public hostname", label_zh="公网域名",
               desc="The tunnel or server that forwards to this app. Meta rejects localhost and private addresses, so one is required. Restart the app after changing this.",
               desc_zh="转发到本应用的隧道或服务器。Meta 不接受 localhost 和内网地址,所以必须填。改完要重启应用。",
               placeholder="abc.trycloudflare.com"),
            _proxy(),
        ],
    },
    "telegram": {
        "id": "telegram",
        "name": "Telegram",
        "name_zh": "Telegram",
        "avatar": "✈️",
        "direction": "both",
        "transport": "poll",
        "needs_public_url": False,
        "docs": "https://core.telegram.org/bots/api",
        "summary": "A bot answers in Telegram. Nothing to expose: this app polls Telegram instead.",
        "summary_zh": "机器人在 Telegram 里应答。无需暴露任何地址——本应用主动去 Telegram 取消息。",
        "setup": [
            "Talk to @BotFather, run /newbot, and copy the token it gives you.",
            "Send the bot one message so it can find your chat id, then add that id below.",
            "That is all — no tunnel, no public address, no certificate.",
        ],
        "setup_zh": [
            "找 @BotFather 执行 /newbot,复制它给的 token。",
            "先给机器人发一条消息,让它知道你的 chat id,再把该 id 填到下面。",
            "就这些——不需要隧道、公网地址或证书。",
        ],
        "fields": [
            _enabled("the channel", "这条通道"),
            _group(),
            _allowed("Numeric chat ids, one per line", "数字 chat id,每行一个", "123456789"),
            _limit(3500, 4096, "Telegram itself refuses anything over 4096.", "Telegram 本身不接受超过 4096 字符。"),
            _prefix(),
            _f("bot_token", "secret", default="",
               label="Bot token", label_zh="机器人 token",
               desc="From @BotFather. It is the bot's whole identity, so it goes into the system keychain.",
               desc_zh="来自 @BotFather。它等同于机器人的身份,因此存进系统钥匙串。"),
            _proxy(),
            _f("drop_pending", "switch", default=True,
               label="Ignore messages sent while this app was off",
               label_zh="忽略应用关闭期间收到的消息",
               desc="Otherwise the backlog arrives at once the next time the app starts.",
               desc_zh="否则下次启动时会把积压的消息一次性灌进来。"),
        ],
    },
    "wecom": {
        "id": "wecom",
        "name": "WeCom",
        "name_zh": "企业微信",
        "avatar": "🏢",
        "direction": "out",
        "transport": "robot",
        "needs_public_url": False,
        "docs": "https://developer.work.weixin.qq.com/document/path/91770",
        "summary": "Push what the group produces into a WeCom group. Group robots cannot receive messages, so this is one-way.",
        "summary_zh": "把群里的成果推送到企业微信群。群机器人无法接收消息,因此这是单向的。",
        "setup": [
            "In the WeCom group, open the group bots panel and add a robot.",
            "Copy its webhook address and paste it below.",
            "Send a test message from here to confirm it lands in the room.",
        ],
        "setup_zh": [
            "在企业微信群里打开「群机器人」面板,添加一个机器人。",
            "复制它的 Webhook 地址,粘贴到下面。",
            "在这里发一条测试消息,确认能到那个群。",
        ],
        "fields": [
            _enabled("this push", "这条推送"),
            _group(),
            _on_answer(),
            _limit(500, 680, "A wecom robot counts bytes, not characters, so the real ceiling is 2048 bytes — about 680 Chinese characters. Chinese text is cut at 2048 bytes whatever this says.",
                   "企业微信机器人按字节计,上限 2048 字节,约 680 个汉字。无论这里填多少,中文都会在 2048 字节处截断。"),
            _prefix(),
            _out_room(),
        ],
    },
    "feishu": {
        "id": "feishu",
        "name": "Feishu",
        "name_zh": "飞书",
        "avatar": "🐦",
        "direction": "out",
        "transport": "robot",
        "needs_public_url": False,
        "docs": "https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot",
        "summary": "Push what the group produces into a Feishu group.",
        "summary_zh": "把群里的成果推送到飞书群。",
        "setup": [
            "In the Feishu group, add a custom bot (群设置 → 群机器人 → 添加机器人 → 自定义机器人).",
            "Copy the webhook address. If you switched on signature verification, copy that secret too.",
            "Send a test message from here to confirm it lands in the room.",
        ],
        "setup_zh": [
            "在飞书群里添加自定义机器人(群设置 → 群机器人 → 添加机器人 → 自定义机器人)。",
            "复制 Webhook 地址;如果开启了签名校验,把密钥也复制下来。",
            "在这里发一条测试消息,确认能到那个群。",
        ],
        "fields": [
            _enabled("this push", "这条推送"),
            _group(),
            _on_answer(),
            _limit(1500, 4096),
            _prefix(),
            _out_room(),
            _signing(),
        ],
    },
    "dingtalk": {
        "id": "dingtalk",
        "name": "DingTalk",
        "name_zh": "钉钉",
        "avatar": "📌",
        "direction": "out",
        "transport": "robot",
        "needs_public_url": False,
        "docs": "https://open.dingtalk.com/document/orgapp/custom-robot-access",
        "summary": "Push what the group produces into a DingTalk group.",
        "summary_zh": "把群里的成果推送到钉钉群。",
        "setup": [
            "In the DingTalk group, add a custom robot (群设置 → 智能群助手 → 添加机器人 → 自定义).",
            "Copy its webhook address. If the security setting is 加签, copy that secret too.",
            "Send a test message from here to confirm it lands in the room.",
        ],
        "setup_zh": [
            "在钉钉群里添加自定义机器人(群设置 → 智能群助手 → 添加机器人 → 自定义)。",
            "复制 Webhook 地址;如果安全设置选的是「加签」,把密钥也复制下来。",
            "在这里发一条测试消息,确认能到那个群。",
        ],
        "fields": [
            _enabled("this push", "这条推送"),
            _group(),
            _on_answer(),
            _limit(1500, 4096),
            _prefix(),
            _out_room(),
            _signing(),
        ],
    },
    "slack": {
        "id": "slack",
        "name": "Slack",
        "name_zh": "Slack",
        "avatar": "💬",
        "direction": "out",
        "transport": "robot",
        "needs_public_url": False,
        "docs": "https://api.slack.com/messaging/webhooks",
        "summary": "Push what the group produces into a Slack channel.",
        "summary_zh": "把群里的成果推送到 Slack 频道。",
        "setup": [
            "Create an app at api.slack.com, turn on Incoming Webhooks, and add one for the channel you want.",
            "Copy the webhook URL and paste it below.",
            "Send a test message from here to confirm it lands in the channel.",
        ],
        "setup_zh": [
            "在 api.slack.com 建一个 app,开启 Incoming Webhooks,为目标频道添加一个。",
            "复制 Webhook URL,粘贴到下面。",
            "在这里发一条测试消息,确认能到那个频道。",
        ],
        "fields": [
            _enabled("this push", "这条推送"),
            _group(),
            _on_answer(),
            _limit(3000, 4000, "Slack itself refuses anything over 4000.", "Slack 本身不接受超过 4000 字符。"),
            _prefix(),
            _out_room(),
        ],
    },
}


def ids() -> tuple[str, ...]:
    return CHANNEL_ORDER


def all_channels() -> list[dict]:
    return [CHANNELS[c] for c in CHANNEL_ORDER]


def get(cid: str) -> dict | None:
    return CHANNELS.get(cid)


def can_receive(cid: str) -> bool:
    ch = CHANNELS.get(cid)
    return bool(ch and ch["direction"] == "both")


def setting_keys(cid: str) -> set[str]:
    ch = CHANNELS.get(cid)
    return {f"{cid}_{f['key']}" for f in ch["fields"]} if ch else set()


def defaults() -> dict[str, Any]:
    """Every channel setting and its default, for `presets.DEFAULT_SETTINGS`.

    Generated rather than written out so a new channel cannot be half-added: an
    unregistered key is dropped by `update_settings` without a word, so the symptom of
    forgetting would be a form that appears to save and never does.
    """
    out: dict[str, Any] = {}
    for ch in all_channels():
        for f in ch["fields"]:
            default = f["default"]
            out[f"{ch['id']}_{f['key']}"] = list(default) if isinstance(default, list) else default
    return out


def secrets() -> dict[str, tuple[str, str]]:
    """`{setting key: (keychain scope, keychain account)}` for `store.SECRET_SETTINGS`."""
    out: dict[str, tuple[str, str]] = {}
    for ch in all_channels():
        for f in ch["fields"]:
            if f["kind"] == "secret":
                out[f"{ch['id']}_{f['key']}"] = (ch["id"], f["ident"])
    return out


def ranges() -> dict[str, tuple[int, int]]:
    """`{setting key: (min, max)}` for `main.RANGES` — a numeric setting that is missing
    here is refused by `PUT /api/settings`, which is how `code_timeout` shipped broken."""
    out: dict[str, tuple[int, int]] = {}
    for ch in all_channels():
        for f in ch["fields"]:
            if f["kind"] == "number" and f["min"] is not None and f["max"] is not None:
                out[f"{ch['id']}_{f['key']}"] = (f["min"], f["max"])
    return out


def webhook_path(cid: str) -> str:
    return f"/hooks/{cid}"


def public_url(cid: str, settings: dict) -> str:
    """The URL to hand to the platform, built from whatever was typed as the public host.

    Four shapes all have to work, because all four are things people actually type: a bare
    hostname, one with a scheme, one with a trailing slash, and the whole callback URL
    pasted back in. Getting it wrong is invisible until the platform's verification
    request never arrives — the endpoint simply looks dead — so the path is always
    appended here rather than trusted to be present, and a repeated one is removed first.
    """
    if not CHANNELS.get(cid, {}).get("needs_public_url"):
        return ""
    host = str(settings.get(f"{cid}_public_host") or "").strip()
    if not host:
        return ""
    if "://" not in host:
        host = f"https://{host}"
    path = webhook_path(cid)
    host = host.rstrip("/")
    if host.endswith(path):
        host = host[: -len(path)].rstrip("/")
    return host + path


def strip_prefix(cid: str, settings: dict) -> dict:
    """This channel's own configuration, keyed by field name.

    Adapters receive their own block rather than the whole settings dict, so an adapter
    cannot accidentally read another channel's group or secret — and so a unit test can
    call one without inventing a full settings table. **Secrets are the real values here**;
    use `masked` for anything that goes out over the API.
    """
    ch = CHANNELS.get(cid)
    if not ch:
        return {}
    return {f["key"]: settings.get(f"{cid}_{f['key']}") for f in ch["fields"]}


def masked(cid: str, settings: dict) -> dict:
    """`strip_prefix` with secret values blanked, for anything a client can read.

    `get_settings()` resolves keychain references, so handing its result to the API would
    return a live bot token or webhook URL to anyone who can reach the local API — which is
    exactly the "written safely, then displayed anyway" mistake the keychain indirection
    exists to prevent.
    """
    cfg = strip_prefix(cid, settings)
    ch = CHANNELS.get(cid)
    for f in (ch or {}).get("fields", ()):
        if f["kind"] == "secret" and cfg.get(f["key"]):
            cfg[f["key"]] = ""
    return cfg


def view(cid: str, settings: dict, *, secret_set: dict[str, bool] | None = None) -> dict:
    """The catalogue entry plus this channel's current values, safe to hand to the UI.

    Secrets come back as a `set` flag and never as a value: a webhook URL or a bot token
    that can be read back over the API is a credential with two copies.
    """
    ch = dict(CHANNELS[cid])
    fields = []
    for f in ch.pop("fields"):
        key = f"{cid}_{f['key']}"
        value = settings.get(key)
        item = {**f, "setting": key, "secret": f["kind"] == "secret"}
        if f["kind"] == "secret":
            item["value"] = ""
            item["set"] = bool((secret_set or {}).get(key, bool(value)))
        else:
            item["value"] = value
            item["set"] = bool(value)
        fields.append(item)
    return {**ch, "fields": fields}
