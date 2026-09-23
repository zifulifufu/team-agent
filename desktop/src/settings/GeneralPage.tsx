import { useCallback, useEffect, useState } from "react";
import { api, type Settings, type VisionStatus } from "../api";
import { useData } from "../data";
import { useI18n } from "../i18n";
import { Switch } from "../ui";
import { NumInput, Row, useSettingsSaver } from "./rows";

type NumKey = "history_clip" | "tool_output_limit" | "history_limit" | "request_timeout" | "circuit_threshold" | "circuit_cooldown" | "memory_top_k" | "library_top_k" | "upload_max_mb" | "video_frames" | "refs_budget" | "integration_budget";
/** Bilingual pairs, not `t()` calls: these tables are evaluated at module level, before the i18n provider mounts. */
interface NumRow { key: NumKey; title: string; titleZh: string; desc: string; descZh: string; min: number; max: number; unit: string; unitZh: string }

const BASE_ROWS: NumRow[] = [
  { key: "request_timeout", title: "Timeout per request", titleZh: "单次请求超时", desc: "Past this time a request counts as failed and falls back to the next model.", descZh: "超过这个时间还没响应,就视为失败并回退到下一个模型。", min: 5, max: 600, unit: "s", unitZh: "秒" },
  { key: "circuit_threshold", title: "Circuit breaker: failures in a row", titleZh: "熔断:连续失败次数", desc: "Once a model fails this many times in a row, it is left alone for a while.", descZh: "某个模型连续失败达到这个次数后,暂时不再尝试它。", min: 1, max: 10, unit: "", unitZh: "次" },
  { key: "circuit_cooldown", title: "Circuit breaker: cooldown", titleZh: "熔断:冷却时间", desc: "How long to wait before giving that model another chance.", descZh: "熔断后隔多久再给这个模型一次机会。", min: 5, max: 600, unit: "s", unitZh: "秒" },
];
const CTX_ROWS: NumRow[] = [
  { key: "history_limit", title: "History messages to include", titleZh: "带入的历史消息条数", desc: "How many of the most recent group-chat messages accompany each model call. More flows better and costs more tokens. This is the whole of a member's memory of the conversation, so a long collaboration wants a large number.", descZh: "每次调用模型时附带的最近群聊消息数量。越多越连贯,也越耗 token。这就是成员对整段对话的记忆范围,长任务建议调大。", min: 1, max: 200, unit: "", unitZh: "条" },
  { key: "history_clip", title: "Most kept of one history message", titleZh: "单条历史消息最多带入", desc: "When an older message is too long, keep its beginning and end and drop the middle, so one long text cannot fill the context. The newest message is untouched.", descZh: "过去的某条消息太长时,只保留开头和结尾、省掉中间,防止一篇长文占满上下文。最新的一条不受影响。", min: 200, max: 20000, unit: "chars", unitZh: "字" },
  { key: "tool_output_limit", title: "Most tool output fed back", titleZh: "工具结果最多回填", desc: "When a tool (search, web fetch, …) returns too much, cut it to this many characters before feeding it back to the model.", descZh: "工具(检索、抓取网页等)返回的内容太长时,回填给模型前截到这么多字。", min: 500, max: 50000, unit: "chars", unitZh: "字" },
];
const FILE_ROWS: NumRow[] = [
  { key: "upload_max_mb", title: "Biggest file a member may attach", titleZh: "单个附件大小上限", desc: "Any kind of file can be attached: a screenshot, a PDF, a spreadsheet, a video, an archive. Documents are read on this machine, so no model has to be able to see them.", descZh: "任何文件都能作为附件:截图、PDF、表格、视频、压缩包。文档在本机直接读取内容,不需要模型「看图」。", min: 1, max: 1024, unit: "MB", unitZh: "MB" },
  { key: "video_frames", title: "Stills taken from a video", titleZh: "从视频里抽几帧", desc: "A model that cannot watch a video is shown this many evenly spaced frames instead. Every frame costs context, so a few go a long way.", descZh: "看不了视频的模型会收到这么多均匀抽取的画面。每一帧都要占上下文,少抽几帧通常就够。", min: 1, max: 12, unit: "", unitZh: "帧" },
  { key: "refs_budget", title: "How much referenced content one prompt may add", titleZh: "一次引用最多带入多少内容", desc: "Files, folders, earlier messages and documents the user referred to are inlined up to this many characters in total. Anything past it says it was cut, and the member can read the file itself.", descZh: "用户 @ 引用的文件、文件夹、既往消息与资料,合计最多带入这么多字。超出部分会注明已截断,成员可以直接去读原文件。", min: 1000, max: 200000, unit: "chars", unitZh: "字" },
];
const BUDGET_ROWS: NumRow[] = [
  { key: "integration_budget", title: "How much of each task reaches the host", titleZh: "每个任务交接给群主的内容量",
    desc: "When the tasks of a plan are consolidated, their outputs go into the host's prompt up to this many characters in total. Each task keeps at least 800 of it, so a long plan is cut per task rather than draining one pot. Raise it for long reports; it costs context.",
    descZh: "分工的各个任务汇总时,它们的产出合计最多这么多字进入群主的提示词。每个任务至少保留 800 字,所以任务多时是按任务分摊,而不是先到先得。长报告可以调大;会占用上下文。",
    min: 2000, max: 200000, unit: "chars", unitZh: "字" },
];
const MEM_ROWS: NumRow[] = [
  { key: "memory_top_k", title: "Memories to include each time", titleZh: "每次带入的记忆条数", desc: "Before replying, take up to this many of the most relevant memories into the prompt. 0 means none.", descZh: "回复前按相关度取最多这么多条记忆放进提示词。0 表示不带入。", min: 0, max: 20, unit: "", unitZh: "条" },
  { key: "library_top_k", title: "Library passages returned per search", titleZh: "资料库每次检索返回的片段数", desc: "How many passages a member gets back at most when searching the library.", descZh: "成员检索资料库时最多返回几段。", min: 1, max: 10, unit: "", unitZh: "段" },
];

export default function GeneralPage() {
  const { t, pick } = useI18n();
  const { settings } = useData();
  const { set, err } = useSettingsSaver();
  const [vision, setVision] = useState<VisionStatus | null>(null);
  const [caps, setCaps] = useState<{ audio_transcribe: boolean; transcriber_install: string } | null>(null);
  useEffect(() => { void api.vision().then(setVision).catch(() => undefined); }, [settings?.vision_model_id, settings?.vision_cloud]);
  // Asked again whenever the command changes, so the chip reflects what would actually happen — and
  // on demand, because installing a transcriber changes no setting at all: without a button the
  // reader would have to restart this app to see that it had been found.
  const checkCaps = useCallback(async () => {
    try { setCaps(await api.machineCapabilities()); } catch { /* the row keeps what it had */ }
  }, []);
  useEffect(() => { void checkCaps(); }, [checkCaps, settings?.transcribe_cmd]);
  if (!settings) return <div className="empty big">{t("Loading…")}</div>;

  // Only models that can really look are offered. The list used to be every enabled model, and a
  // gateway lists chat models and image generators side by side under one name space — so
  // `gpt-image-…` looked exactly like a model that handles images, and selecting it switched vision
  // off for every group without a word. The current value is shown even when it cannot look: a
  // `<select>` whose value is not among its options renders as if nothing were chosen, so a broken
  // pick would silently turn into "automatic" the next time anything else on this page was saved.
  const canLook = vision?.candidates ?? [];
  const namedId = settings.vision_model_id;
  const namedMissing = !!namedId && !canLook.some((m) => m.id === namedId);
  const namedLabel = vision?.configured_name || namedId;
  const recId = vision?.recommended_id || "";
  const recName = vision?.recommended_name || "";
  const recWhere = vision?.recommended_local
    ? t(" (local, so pictures never leave this machine)")
    : t(" (cloud, so pictures are sent to that provider)");

  // Every reason nothing is looking, as one sentence each — the same shape the members get. They
  // are independent: a wrong pick and a switch that is off can both be true at once, and naming
  // only the first would send the reader round the loop twice.
  const visionText = [
    vision?.configured_sees === false
      ? t("The model picked above cannot look at images, so it is not the one reading them — the app falls back to a model that can. Pick one that can see, or set this back to automatic.")
      : "",
    !vision?.model_id
      ? vision?.blocked_cloud
        ? t("A model here can see, but sending images to a cloud model is switched off (see Permissions & control).")
        : t("No model here can look at images. Pull a local vision model in Ollama (for example `ollama pull qwen2.5vl:3b`) and pick it above, or connect a cloud provider and allow cloud vision.")
      : "",
    vision?.model_id
      ? t("Attached pictures and video frames are looked at by this model; members whose own model cannot see get its description.")
      : "",
  ].filter(Boolean).join(" ");

  // Before the answer arrives there is nothing to recommend *yet* — saying "nothing here can look"
  // for one frame and then changing its mind is the kind of flicker that reads as a bug.
  const visionDesc = ((!vision
    ? t("Checking what can look at images here…")
    : recName
      ? t("Recommended: {name}{where} — leave the choice on automatic to use it.", { name: recName, where: recWhere })
      : t("There is nothing to recommend yet: no model available here can look at images."))
    + " " + t("Attached images and video frames go to a model that can actually see them. Members whose own model cannot look get a description from this one instead — so a spreadsheet, a PDF or a picture all reach every member. Only models that can look are listed; a cloud one still needs cloud vision to be on."));

  // The empty state, as buttons rather than advice: each one is the thing that would actually
  // unblock it. Turning the switch on is enough even with a bad pick left in place (the search then
  // falls through to a model that can see), which is why it comes first.
  const visionFixes: { label: string; run: () => void }[] = [];
  if (!vision?.model_id && vision?.blocked_cloud) {
    visionFixes.push({ label: t("Turn on cloud vision"), run: () => void set({ vision_cloud: true }) });
  }
  if (vision?.configured_sees === false) {
    visionFixes.push({ label: t("Set it back to automatic"), run: () => void set({ vision_model_id: "" }) });
  }
  const visionChip = !vision
    ? t("Checking…")
    : vision.model_id
      ? `${vision.model_name} · ${vision.is_local ? t("local") : t("cloud")}`
      : vision.blocked_cloud
        ? t("a model can see, but the switch is off")
        : vision.configured_sees === false
          ? t("{name} — cannot look at images", { name: namedLabel })
          : t("no model here can look at images");

  const numRows = (rows: NumRow[]) =>
    rows.map((r) => {
      const title = pick(r.title, r.titleZh);
      return (
        <Row key={r.key} title={title} desc={pick(r.desc, r.descZh)}>
          <NumInput v={settings[r.key]} min={r.min} max={r.max} unit={pick(r.unit, r.unitZh)} onCommit={(n) => set({ [r.key]: n } as Partial<Settings>)} label={title} />
        </Row>
      );
    });

  return (
    <div className="sp">
      <h2 className="sp-title">{t("General")}</h2>
      <p className="sp-desc">{t("Operating parameters for collaboration and routing. Changes take effect immediately.")}</p>
      {err && <div className="ext-errbox ext-sticky-err" role="alert"><div className="err">{t("Failed to save: {error}", { error: err })}</div></div>}

      <div className="card flush">{numRows(BASE_ROWS)}</div>

      <div className="sec">{t("Context management")}</div>
      <div className="card flush">{numRows(CTX_ROWS)}</div>

      <div className="sec">{t("Memory & library")}</div>
      <div className="card flush">
        <Row title={t("Enable memory")} desc={t("Members pull in relevant preferences, decisions and lessons before replying. Each group chat can turn this off on its own.")}>
          <Switch checked={settings.memory_enabled} label={t("Enable memory")} onChange={(v) => void set({ memory_enabled: v })} />
        </Row>
        <Row title={t("Tidy up memories after a group chat")} desc={t("When a round of group chat ends, the request and the final answer go to a fast, cheap model (picked by routing, possibly a cloud provider) to distil preferences, decisions and lessons into memories. This costs one extra model call. Obvious passwords and keys are filtered out, but that is not a guarantee — please do not send secrets in a group chat.")}>
          <Switch checked={settings.memory_auto_extract} label={t("Tidy up memories after a group chat")} onChange={(v) => void set({ memory_auto_extract: v })} />
        </Row>
        {numRows(MEM_ROWS)}
      </div>

      <div className="sec">{t("Files in a group chat")}</div>
      <div className="card flush">
        {numRows(FILE_ROWS)}
        <Row title={t("Which model looks at pictures")} desc={visionDesc}>
          <select className="pm-text" value={settings.vision_model_id} aria-label={t("Which model looks at pictures")}
            onChange={(e) => void set({ vision_model_id: e.target.value })}>
            <option value="">{t("Automatic (local first)")}{recName ? ` → ${recName} ★` : ""}</option>
            {canLook.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}{m.is_local ? ` · ${t("local")}` : ""}{m.id === recId ? " ★" : ""}
              </option>
            ))}
            {namedMissing && (
              <option value={namedId}>
                {vision?.configured_sees === false
                  ? t("{name} — cannot look at images", { name: namedLabel })
                  : t("{name} — not usable right now", { name: namedLabel })}
              </option>
            )}
          </select>
        </Row>
        <Row title={t("Vision model")} desc={visionText}>
          <span style={{ display: "inline-flex", flexDirection: "column", alignItems: "flex-end", gap: 6 }}>
            <span className={"chip" + (vision?.model_id ? "" : " warn")}>{visionChip}</span>
            {visionFixes.map((f) => (
              <button key={f.label} className="btn small" onClick={f.run}>{f.label}</button>
            ))}
          </span>
        </Row>
        <Row title={t("Transcribe audio")}
             desc={t("Speech needs a program, not a model: nothing is bundled and nothing is downloaded for you. Leave this empty to use a transcriber that is already installed; write a command to use your own. {out} is the folder to write the text into, and {audio} is the file — if the command does not mention it, the path is added at the end.")}>
          <span style={{ display: "inline-flex", flexDirection: "column", alignItems: "flex-end", gap: 6 }}>
            <input className="pm-text" value={settings.transcribe_cmd} placeholder={t("(an installed transcriber, if there is one)")}
                   aria-label={t("Transcribe audio")}
                   onChange={(e) => void set({ transcribe_cmd: e.target.value })} />
            <span className={"chip" + (caps?.audio_transcribe ? "" : " warn")}>
              {caps?.audio_transcribe ? t("ready") : t("nothing found")}
            </span>
            {/* "nothing found" on its own is a dead end for someone who has never installed a speech
                model: the command that would work on *this* machine is the useful part. */}
            {!caps?.audio_transcribe && (
              <>
                <span className="muted small" style={{ maxWidth: 330, textAlign: "right" }}>
                  {t("Install one — this app will find it by itself afterwards:")}
                  {caps?.transcriber_install ? <> <code>{caps.transcriber_install}</code></> : null}
                </span>
                <span className="muted small" style={{ maxWidth: 330, textAlign: "right" }}>
                  {t("It lands in ~/.local/bin, which this app looks in. The first transcription downloads the model, a few hundred megabytes.")}
                </span>
                <button className="btn small" onClick={() => void checkCaps()}>{t("Check again")}</button>
              </>
            )}
          </span>
        </Row>
      </div>

      <div className="sec">{t("Handing work over")}</div>
      <div className="card flush">
        {numRows(BUDGET_ROWS)}
      </div>
    </div>
  );
}
