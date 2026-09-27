import { useCallback, useEffect, useRef, useState } from "react";
import {
  AudioLines, CircleAlert, CircleCheck, Clock3, Film, FolderOpen, Image as ImageIcon,
  LoaderCircle, Music2, Pause, Play, Sparkles, Trash2, TriangleAlert, Upload,
} from "lucide-react";
import {
  api, type MusicJob, type MusicPreview, type MusicRead, type MusicShelf, type MusicTrack,
  type MusicVocabulary, type StudioAsset, type StudioAssets, type StudioTake,
} from "../api";
import { notify, useConfirm } from "../ui";
import { useI18n } from "../i18n";
import "../styles/videozone.css";

/** 专区的四块。音乐是唯一端到端跑通的；「我的素材」是三块共用的地基（上传 → 私有存放 →
 *  生成 → 看效果 → 修正 → 历史），它本身不依赖任何生成模型，所以先建它。 */
type Tab = "music" | "assets" | "scene" | "motion";

export default function VideoZonePage() {
  const { t } = useI18n();
  const [tab, setTab] = useState<Tab>("music");
  const TABS: { id: Tab; label: string; icon: typeof Music2 }[] = [
    { id: "music", label: t("Music"), icon: Music2 },
    { id: "assets", label: t("My material"), icon: FolderOpen },
    { id: "scene", label: t("Scenes"), icon: Sparkles },
    { id: "motion", label: t("Expressions and motion"), icon: Play },
  ];
  return (
    <div className="vz">
      {/* ⚠️ 这里**没有标题**：标题、说明和状态徽标现在由专区外壳画，内容来自后端 `zones.py`。
          一份标题写在两个地方，改一处就会剩下一处旧文案 —— 而且用户在侧栏点进来时读到的
          应该是同一个名字。 */}
      <nav className="vz-tabs" role="tablist">
        {TABS.map((x) => (
          <button key={x.id} role="tab" aria-selected={tab === x.id}
                  className={"vz-tab" + (tab === x.id ? " on" : "")} onClick={() => setTab(x.id)}>
            <x.icon size={15} /> {x.label}
          </button>
        ))}
      </nav>
      {tab === "music" && <MusicBlock />}
      {tab === "assets" && <StudioBlock />}
      {tab === "scene" && <SceneBlock />}
      {tab === "motion" && <MotionBlock />}
    </div>
  );
}

// ------------------------------------------------------------------ music

function MusicBlock() {
  const { t } = useI18n();
  const confirm = useConfirm();
  const [shelf, setShelf] = useState<MusicShelf | null>(null);
  const [vocab, setVocab] = useState<MusicVocabulary | null>(null);
  const [loading, setLoading] = useState(true);
  const [problem, setProblem] = useState("");
  /**
   * 用户要写的东西只有三样：**描述**（必填）、**歌词**（可选）、**人声**（可选）。
   *
   * 2026-09-27 撤掉了五台推子（流派 / 情绪 / 乐器 / 制作 / BPM）—— 用户的原话是「这些都在
   * 算法里面……不用在面板中展示出来」。它们现在由后端 `musicprompt.read()` 从这句话里读出来，
   * 面板上留一行**回执**：读成了什么。自动的东西必须说得出来它读成了什么，否则「自动」就是黑箱。
   *
   * ⚠️ 人声是唯一留着的手动项，而且默认是 `auto`：唱不唱、谁来唱，是作者真会有意见的一件事。
   */
  const [desc, setDesc] = useState("");
  const [lyrics, setLyrics] = useState("");
  const [vocals, setVocals] = useState("auto");
  const [seconds, setSeconds] = useState(0);
  const [title, setTitle] = useState("");
  const [open, setOpen] = useState(false);            // 高级：语言 / 种子
  const [language, setLanguage] = useState("en");
  const [seed, setSeed] = useState(0);
  const [preview, setPreview] = useState<MusicPreview | null>(null);
  const [job, setJob] = useState<MusicJob | null>(null);
  const [clock, setClock] = useState(0);
  const [playing, setPlaying] = useState<{ name: string; url: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const audio = useRef<HTMLAudioElement | null>(null);

  const load = useCallback(async () => {
    try {
      const [got, words] = await Promise.all([api.videoZoneMusic(), api.musicVocabulary()]);
      setShelf(got);
      setVocab(words);
      setProblem("");
      setSeconds((s) => s || got.limits.default_seconds);
    } catch (e) {
      // 读不到就明说读不到。空列表与"读失败"长得一模一样，而它们的下一步完全不同。
      setProblem((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  /**
   * 回执：描述变了就去后端问一次**它读成了什么**。
   *
   * 拼这一行放在前端，是因为它要中文的流派名（词表里有）和本地化的情绪词（`t()` 在 ZH 表里）
   * —— 后端拼出来必然是半中半英的。而「读」这件事只有后端一处（`musicprompt.read`），
   * 前端不重算。
   */
  useEffect(() => {
    const text = desc.trim();
    if (!vocab || !text) { setPreview(null); return; }
    let live = true;
    const timer = window.setTimeout(async () => {
      try {
        const got = await api.previewMusic({ prompt: text, lyrics, vocals: vocals === "auto" ? "" : vocals });
        if (live) setPreview(got);
      } catch {
        /* 预览失败不影响作曲：真正的读与拼在提交时还会做一次 */
      }
    }, 300);
    return () => { live = false; window.clearTimeout(timer); };
  }, [desc, lyrics, vocals, vocab]);

  /** 「生成中」要显示已经过去多久 —— 它是分钟级的，一个不动的转圈看起来就是死了。 */
  useEffect(() => {
    if (job?.state !== "running") return;
    const timer = window.setInterval(() => setClock(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [job?.state]);

  useEffect(() => {
    if (job?.state !== "running") return;
    let live = true;
    const timer = window.setInterval(async () => {
      try {
        const { job: now } = await api.musicJob(job.id);
        if (!live) return;
        setJob(now);
        if (now.state === "done") {
          await load();
          notify(t("Composed \"{name}\"", { name: now.name }));
        } else if (now.state === "failed") {
          notify(now.error || t("Composing failed"));
        }
      } catch {
        /* 网络抖一下就跳过这一次：任务的真实状态在后台，下一拍还能读到 */
      }
    }, 2000);
    return () => { live = false; window.clearInterval(timer); };
    // `load` 与 `t` 每次渲染都是新的，故意不列进依赖（列进去会每一拍重建定时器）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id, job?.state]);

  // blob URL 要自己回收：一次会话里试听十几首，不回收就是十几份音频一直占着内存。
  useEffect(() => () => { if (playing) URL.revokeObjectURL(playing.url); }, [playing]);

  const compose = async () => {
    if (!desc.trim()) {
      notify(t("Describe the music first — one sentence is enough."));
      return;
    }
    setBusy(true);
    try {
      const { job: started } = await api.composeMusic({
        prompt: desc.trim(), lyrics: lyrics.trim(), vocals: vocals === "auto" ? "" : vocals,
        seconds, name: title.trim(), language, seed,
      });
      setJob(started);
      setClock(Date.now() / 1000);
    } catch (e) {
      notify((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const play = async (tr: MusicTrack) => {
    if (playing?.name === tr.name) {           // 再点一次 = 停下
      audio.current?.pause();
      setPlaying(null);
      return;
    }
    try {
      if (playing) URL.revokeObjectURL(playing.url);
      const url = await api.musicAudio(tr.name);
      setPlaying({ name: tr.name, url });
      window.setTimeout(() => void audio.current?.play().catch(() => undefined), 60);
    } catch (e) {
      notify((e as Error).message);
    }
  };

  const remove = async (tr: MusicTrack) => {
    if (!(await confirm(t("Delete \"{name}\" from the shelf?", { name: tr.title || tr.name }),
                        { okText: t("Delete") }))) return;
    try {
      await api.delMusic(tr.name);
      if (playing?.name === tr.name) setPlaying(null);
      await load();
      notify(t("Deleted \"{name}\"", { name: tr.title || tr.name }));
    } catch (e) {
      notify((e as Error).message);
    }
  };

  const ready = shelf?.composer.ready ?? false;
  const limits = shelf?.limits ?? { min_seconds: 10, max_seconds: 240, default_seconds: 60 };
  const elapsed = job?.state === "running" ? Math.max(0, Math.floor(clock - job.started)) : 0;
  const label = (id: string, kind: "genres" | "instruments") =>
    vocab?.[kind].find((x) => x.id === id)?.label ?? id;
  /** 回执那一行：「你那句话我这么理解的」。 */
  const receipt = (r?: MusicRead) => !r ? "" : [
    label(r.genre, "genres"), t(r.mood),
    r.instruments.map((i) => label(i, "instruments")).join(" + "),
    `${r.bpm} BPM`,
  ].filter(Boolean).join(" · ");

  return (
    <div className="vz-two">
      <section className="vz-panel">
        <h3><Music2 size={15} /> {t("Compose")}</h3>
        {loading ? (
          <p className="vz-dim"><LoaderCircle size={14} className="vz-spin" /> {t("Checking this machine…")}</p>
        ) : ready ? (
          <p className="vz-note ok"><CircleCheck size={14} /> {t("ACE-Step is ready on this machine ({url})", { url: shelf?.composer.base_url || "" })}</p>
        ) : (
          <p className="vz-note bad"><CircleAlert size={14} /> {shelf?.composer.why}</p>
        )}

        {/* 描述：这一页唯一必填的东西，所以它拿到最大的地方。 */}
        <label className="vz-field">
          <span>{t("What is this music for?")}</span>
          <textarea rows={4} value={desc} disabled={!ready}
                    placeholder={t("One sentence: where it is used, what it is made of, how it should feel. e.g. quiet piano, no drums, spreading slowly under a narrator")}
                    onChange={(e) => setDesc(e.target.value)} />
        </label>

        <label className="vz-field">
          <span>{t("Lyrics (optional — leave empty for an instrumental)")}</span>
          <textarea rows={2} value={lyrics} disabled={!ready}
                    placeholder={t("empty = instrumental")}
                    onChange={(e) => setLyrics(e.target.value)} />
        </label>

        <div className="vz-row">
          <label className="vz-field narrow">
            <span>{t("Vocals")}</span>
            <select value={vocals} disabled={!ready} onChange={(e) => setVocals(e.target.value)}>
              <option value="auto">{t("Auto (from the description)")}</option>
              {vocab?.vocals.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
            </select>
          </label>
          <label className="vz-field">
            <span>{t("Seconds")} <b>{seconds}s</b></span>
            <input type="range" min={limits.min_seconds} max={limits.max_seconds} step={5}
                   value={seconds} disabled={!ready}
                   onChange={(e) => setSeconds(Number(e.target.value))} />
          </label>
          <label className="vz-field narrow">
            <span>{t("Title (optional)")}</span>
            <input value={title} disabled={!ready} placeholder={t("what this theme is for")}
                   onChange={(e) => setTitle(e.target.value)} />
          </label>
        </div>

        {job?.state === "running" ? (
          <>
            <p className="vz-note"><LoaderCircle size={14} className="vz-spin" /> {t("Composing… {n}s elapsed. It runs on this machine and takes minutes.", { n: elapsed })}</p>
            <p className="vz-hint">{job.prompt}</p>
          </>
        ) : (
          <button className="btn primary vz-go" disabled={!ready || busy || !desc.trim()}
                  onClick={() => void compose()}>
            <Sparkles size={15} /> {t("Compose")}
          </button>
        )}
        {job?.state === "failed" && (
          <p className="vz-note bad"><CircleAlert size={14} /> {job.error}</p>
        )}

        {/* 回执 + 模型真正收到的那串词。**收在折叠里**：用户要的是不看见那些旋钮，
            但他随时该能查「你到底按什么生成的」——所以它不是没有，是不占地方。 */}
        {!!receipt(preview?.read) && (
          <p className="vz-receipt" title={preview?.tags}>
            {t("Read as")}: <b>{receipt(preview?.read)}</b>
          </p>
        )}
        {!!preview && (
          <details className="vz-tags">
            <summary>{t("What the model receives")}</summary>
            <code>{preview?.tags || "—"}</code>
            {preview?.warnings.map((w, i) => (
              <p key={i} className="vz-warn"><TriangleAlert size={13} /> {w}</p>
            ))}
          </details>
        )}

        <button className="vz-more" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {open ? "▾" : "▸"} {t("Advanced")}
        </button>
        {open && (
          <div className="vz-adv">
            <div className="vz-row">
              <label className="vz-field narrow">
                <span>{t("Language (for the lyrics)")}</span>
                <select value={language} disabled={!ready} onChange={(e) => setLanguage(e.target.value)}>
                  <option value="en">en</option><option value="zh">zh</option>
                </select>
              </label>
              <label className="vz-field narrow">
                <span>{t("Seed (0 = random)")}</span>
                <input type="number" value={seed} disabled={!ready}
                       onChange={(e) => setSeed(Number(e.target.value))} />
              </label>
            </div>
          </div>
        )}
      </section>

      <section className="vz-panel">
        <h3>
          <Music2 size={15} /> {t("Shelf")}
          {shelf && <span className="vz-count">{shelf.tracks.length}</span>}
        </h3>
        {problem && <p className="vz-note bad"><CircleAlert size={14} /> {problem}</p>}
        {/* 坏 sidecar 点名，不静默：一首曲子看起来"没有元数据"和它的文件写坏了，是同一条线索。 */}
        {!!shelf?.errors.length && (
          <p className="vz-note bad"><CircleAlert size={14} /> {t("{n} track file(s) could not be read", { n: shelf.errors.length })}: {shelf.errors.slice(0, 3).join("；")}</p>
        )}
        {!shelf?.tracks.length && !problem && (
          <p className="vz-dim">{t("Nothing on the shelf yet. Compose one on the left — it lands here, and a film can be scored with it by name.")}</p>
        )}
        <ul className="vz-tracks">
          {shelf?.tracks.map((tr) => (
            <li key={tr.name} className={"vz-track" + (playing?.name === tr.name ? " on" : "")}>
              <button className="vz-play" onClick={() => void play(tr)}
                      aria-label={playing?.name === tr.name ? t("Stop") : t("Play")}>
                {playing?.name === tr.name ? <Pause size={14} /> : <Play size={14} />}
              </button>
              <div className="vz-track-body">
                <b>{tr.title || tr.name}</b>
                <span className="vz-meta">
                  <Clock3 size={11} /> {tr.seconds.toFixed(0)}s · {(tr.bytes / 1024 / 1024).toFixed(1)} MB
                  {!!tr.mood && <> · {t(tr.mood)}</>}
                  {!!tr.tags.length && <> · {tr.tags.slice(0, 4).join(", ")}</>}
                </span>
                {playing?.name === tr.name && (
                  <audio ref={audio} src={playing.url} controls autoPlay className="vz-audio" />
                )}
              </div>
              <button className="icon-btn" aria-label={t("Delete")} onClick={() => void remove(tr)}>
                <Trash2 size={14} />
              </button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

// ------------------------------------------------ 我的素材（私有工作室）
/**
 * 三块共用的地基：上传你自己的素材 → 私有存放 → 由它生成 → 看效果 → 修正 → 留成历史。
 *
 * ⚠️⚠️ **与其它库故意相反**：器械参考库是「拷进每个群、成员随便看」；这里是**你自己的脸和你的
 * 录像**。素材不在任何群的工作目录里、不进知识库、不参与导出，成员只能通过显式引用拿到**产出**。
 * 页面上这句话不是客套，它是这一块唯一的设计约束 —— 所以上传区旁边就写着它。
 *
 * 「生成」那一环还没接上（模型与工作流都已就位，但一次要几十分钟才出结果，接上必须真跑一遍才算数），
 * 所以这里**不放一个点了没反应的按钮**，而是把事实写在它该在的位置。
 */
function StudioBlock() {
  const { t } = useI18n();
  const confirm = useConfirm();
  const [data, setData] = useState<StudioAssets | null>(null);
  const [problem, setProblem] = useState("");
  const [kind, setKind] = useState("");
  const [sel, setSel] = useState("");
  const [takes, setTakes] = useState<StudioTake[]>([]);
  const [shots, setShots] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  // blob URL 自己回收。用 ref 记账，**不是**用 `shots` 当依赖 —— 那样每加一张缩略图就会把之前
  // 所有 URL 一起回收掉，页面上的图全变成破图。
  const urls = useRef<string[]>([]);
  const tried = useRef<Set<string>>(new Set());
  useEffect(() => () => { for (const u of urls.current) URL.revokeObjectURL(u); }, []);

  const load = useCallback(async () => {
    try {
      setData(await api.studioAssets(kind ? { kind } : {}));
      setProblem("");
    } catch (e) {
      setProblem((e as Error).message);
    }
  }, [kind]);
  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!sel) { setTakes([]); return; }
    let live = true;
    void api.studioTakes(sel).then((r) => { if (live) setTakes(r.takes); }).catch(() => undefined);
    return () => { live = false; };
  }, [sel]);

  /** 缩略图懒取：`<img src>` 带不上令牌，所以走一次带表头的 fetch 换成 blob URL。 */
  const grab = useCallback(async (id: string) => {
    if (tried.current.has(id)) return;
    tried.current.add(id);
    try {
      const u = await api.studioAssetBlob(id);
      urls.current.push(u);
      setShots((s) => ({ ...s, [id]: u }));
    } catch {
      tried.current.delete(id);       // 失败了允许下次再试，否则这张永远是空的
    }
  }, []);

  // 拿到列表后给**照片**取一次缩略图。视频和音频只显示图标 —— 为了一个方块去解一份几十 MB 的
  // 文件不值得，而用户要认的是"这是哪张脸"，照片才需要真看一眼。
  useEffect(() => {
    for (const a of data?.assets ?? []) if (a.kind === "photo") void grab(a.id);
  }, [data, grab]);

  const upload = async (files: FileList | null) => {
    const list = Array.from(files ?? []);
    if (!list.length) return;
    setBusy(true);
    let ok = 0;
    try {
      for (const f of list) {
        try {
          await api.uploadStudioAsset(f, { title: f.name.replace(/\.[^.]+$/, "") });
          ok += 1;
        } catch (e) {
          notify(`${f.name}: ${(e as Error).message}`);
        }
      }
      if (ok) await load();
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const removeAsset = async (a: StudioAsset) => {
    if (!(await confirm(t("Delete \"{name}\" and every take made from it?", { name: a.title }),
                        { okText: t("Delete") }))) return;
    try {
      await api.delStudioAsset(a.id);
      if (sel === a.id) setSel("");
      await load();
      notify(t("Deleted \"{name}\"", { name: a.title }));
    } catch (e) {
      notify((e as Error).message);
    }
  };

  const retag = async (a: StudioAsset) => {
    const raw = window.prompt(t("Tags, separated by commas"), a.tags.join(", "));
    if (raw === null) return;
    try {
      await api.patchStudioAsset(a.id, { tags: raw.split(",").map((s) => s.trim()).filter(Boolean) });
      await load();
    } catch (e) {
      notify((e as Error).message);
    }
  };

  const mark = async (take: StudioTake, patch: { reviewed?: boolean; chosen?: boolean }) => {
    try {
      const { take: now } = await api.patchStudioTake(take.id, patch);
      setTakes((list) => list.map((x) => (x.id === now.id ? now : x)));
    } catch (e) {
      notify((e as Error).message);
    }
  };

  const removeTake = async (take: StudioTake) => {
    if (!(await confirm(t("Delete this take?"), { okText: t("Delete") }))) return;
    try {
      await api.delStudioTake(take.id);
      setTakes((list) => list.filter((x) => x.id !== take.id));
    } catch (e) {
      notify((e as Error).message);
    }
  };

  const KINDS: { id: string; label: string }[] = [
    { id: "", label: t("All") },
    { id: "photo", label: t("Photos") },
    { id: "video", label: t("Video") },
    { id: "audio", label: t("Audio") },
  ];
  const current = data?.assets.find((a) => a.id === sel) ?? null;

  return (
    <div className="vz-two">
      <section className="vz-panel">
        <h3><FolderOpen size={15} /> {t("My material")}
          {data && <span className="vz-count">{data.assets.length}</span>}
        </h3>
        <p className="vz-hint">
          {t("Kept on this machine only: not in any group's workspace, not in a knowledge base, not in an export. Members reach a take through an explicit reference; they never reach the original.")}
        </p>

        <div className="vz-upload">
          <input ref={fileRef} type="file" multiple hidden
                 accept="image/*,video/*,audio/*"
                 onChange={(e) => void upload(e.target.files)} />
          <button className="btn primary vz-go" disabled={busy}
                  onClick={() => fileRef.current?.click()}>
            {busy ? <LoaderCircle size={15} className="vz-spin" /> : <Upload size={15} />}
            {t("Add photos, video or audio")}
          </button>
        </div>

        <div className="vz-chips" style={{ marginTop: 10 }}>
          {KINDS.map((k) => (
            <button key={k.id} className={"vz-chip" + (kind === k.id ? " on" : "")}
                    onClick={() => setKind(k.id)}>{k.label}</button>
          ))}
        </div>

        {problem && <p className="vz-note bad" style={{ marginTop: 10 }}>
          <CircleAlert size={14} /> {problem}</p>}
        {/* 读不出 sidecar 的素材点名，不静默 —— 不能再描述的东西就不能拿来生成。 */}
        {!!data?.errors.length && (
          <p className="vz-note bad" style={{ marginTop: 10 }}>
            <CircleAlert size={14} /> {t("{n} item(s) cannot be read", { n: data.errors.length })}: {data.errors.slice(0, 3).map((e) => e.name).join("；")}
          </p>
        )}
        {data && !data.assets.length && !problem && (
          <p className="vz-dim" style={{ marginTop: 10 }}>
            {t("Nothing here yet. Add a photo of yourself, a recorded expression, or some footage — that is what the other blocks generate from.")}
          </p>
        )}
      </section>

      <section className="vz-panel">
        <h3><Clock3 size={15} /> {current
          ? t("Takes from \"{name}\"", { name: current.title })
          : t("Pick something on the left")}</h3>

        {!data?.assets.length ? (
          <p className="vz-dim">{t("Your generation history shows up here, one list per piece of material.")}</p>
        ) : (
          <ul className="vz-tracks">
            {data.assets.map((a) => (
              <li key={a.id} className={"vz-track" + (sel === a.id ? " on" : "")}>
                <button className="vz-thumb" onClick={() => setSel(sel === a.id ? "" : a.id)}
                        title={a.title}>
                  {a.kind === "photo" && (shots[a.id]
                    ? <img src={shots[a.id]} alt="" /> : <ImageIcon size={16} />)}
                  {a.kind === "video" && <Film size={16} />}
                  {a.kind === "audio" && <AudioLines size={16} />}
                </button>
                <div className="vz-track-body">
                  <b>{a.title}</b>
                  <span className="vz-meta">
                    {t(a.kind === "photo" ? "Photos" : a.kind === "video" ? "Video" : "Audio")}
                    {" · "}{(a.bytes / 1024 / 1024).toFixed(1)} MB
                    {!!a.tags.length && <> · {a.tags.join(", ")}</>}
                  </span>
                </div>
                <button className="icon-btn" aria-label={t("Tags")} onClick={() => void retag(a)}>#</button>
                <button className="icon-btn" aria-label={t("Delete")} onClick={() => void removeAsset(a)}>
                  <Trash2 size={14} />
                </button>
              </li>
            ))}
          </ul>
        )}

        {current && (
          <>
            <div className="vz-preview" style={{ marginTop: 12 }}>
              <span className="vz-preview-label">{t("Generation — not wired up yet")}</span>
              <p className="vz-hint" style={{ margin: 0 }}>
                {t("The models and the workflow for this are already on this machine, but one run takes tens of minutes, so it gets connected only when it can be watched end to end. Until then this list is where those runs will appear.")}
              </p>
            </div>
            {!takes.length && <p className="vz-dim">{t("No takes yet.")}</p>}
            <ul className="vz-tracks">
              {takes.map((k) => (
                <li key={k.id} className={"vz-track" + (k.chosen ? " on" : "")}>
                  <div className="vz-track-body">
                    <b>{k.name || k.id}</b>
                    <span className="vz-meta">
                      {t(k.state === "done" ? "Done" : k.state === "running" ? "Running" : "Failed")}
                      {" · "}{new Date(k.created * 1000).toLocaleString()}
                      {k.seconds ? ` · ${k.seconds.toFixed(1)}s` : ""}
                      {k.reviewed ? ` · ${t("Seen")}` : ""}
                      {k.chosen ? ` · ${t("Chosen")}` : ""}
                    </span>
                    {!!k.prompt && <span className="vz-hint">{k.prompt}</span>}
                    {!!k.error && <span className="vz-warn"><TriangleAlert size={12} /> {k.error}</span>}
                  </div>
                  <button className="icon-btn" aria-label={t("Seen")}
                          onClick={() => void mark(k, { reviewed: !k.reviewed })}>👁</button>
                  <button className="icon-btn" aria-label={t("Chosen")}
                          onClick={() => void mark(k, { chosen: !k.chosen })}>★</button>
                  <button className="icon-btn" aria-label={t("Delete")} onClick={() => void removeTake(k)}>
                    <Trash2 size={14} />
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  );
}

// ------------------------------------------------------- not built yet
/**
 * 这两块**故意**先只写清现状与门槛，而不是搭一个能点但生成不出来的空壳。
 *
 *  用户要的场景是 (b)：画面里的环境（手术室、血管内视角、诊室）—— 那需要文生图；要的动作是
 *  (b)：数字人的表情与动作 —— 那需要口型/表情驱动。两者的瓶颈都不是界面，是把渲染链接上。
 *  而这一步有确定的事实：云端文生图在限流、本机没有 SDXL、InfiniteTalk 实测 9 分钟一条。
 *  把这些写在页面上，用户能据此决定先推哪条；写一个假的"生成中"不能。 */
function SceneBlock() {
  const { t } = useI18n();
  return (
    <section className="vz-panel vz-wide">
      <h3><Sparkles size={15} /> {t("Scenes")}</h3>
      <p className="vz-dim">{t("The environment a shot happens in — an angio suite, the inside of a vessel, a clinic. These are pictures, so they need a text-to-image engine.")}</p>
      <ul className="vz-facts">
        <li><b>{t("Not built yet")}</b> — {t("because the engine that would draw them is not available on this machine right now:")}</li>
        <li>{t("The cloud image service is configured and has fifteen image models, but the account is out of credit. Its own words, measured just now: \"You exceeded your current API quota. Please purchase the API points.\"")}</li>
        <li>{t("The local ComfyUI install has ACE-Step (music) and wan2.2 (video) but no image checkpoint such as SDXL.")}</li>
      </ul>
      <p className="vz-dim">{t("To turn this on, one of these has to happen: buy API points for the cloud image service, or download an image checkpoint into the local ComfyUI. Then this block gets a prompt box, a generate button and a shelf, exactly like the music one.")}</p>
    </section>
  );
}

function MotionBlock() {
  const { t } = useI18n();
  return (
    <section className="vz-panel vz-wide">
      <h3><Play size={15} /> {t("Expressions and motion")}</h3>
      <p className="vz-dim">{t("How a presenter's face and body move — lip sync, expression, gesture — driven from a still portrait.")}</p>
      <ul className="vz-facts">
        <li><b>{t("The chain exists but is slow")}</b>: {t("ComfyUI plus InfiniteTalk, with the 14B weights already downloaded and a working graph.")}</li>
        <li>{t("Measured: one 480×832, 121-frame clip takes about 9 minutes. A one-minute presenter shot is hours.")}</li>
        <li>{t("Separately, this app can already animate mechanisms (flow, coil packing, contrast) frame by frame in about two seconds per clip — that is a different tool, and it is what a mechanism shot should use.")}</li>
      </ul>
      <p className="vz-dim">{t("Before this block is worth building, the speed has to be settled: either accept minutes per clip and show a real progress bar, or pick a lighter model. Say which and it gets built.")}</p>
    </section>
  );
}
