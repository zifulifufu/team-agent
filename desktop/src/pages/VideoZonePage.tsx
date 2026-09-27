import { useCallback, useEffect, useRef, useState } from "react";
import {
  AudioLines, CircleAlert, CircleCheck, Clock3, Film, FolderOpen, Image as ImageIcon,
  LoaderCircle, Music2, Pause, Play, Sparkles, Trash2, TriangleAlert, Upload,
} from "lucide-react";
import {
  api, type MusicJob, type MusicPreset, type MusicPreview, type MusicShelf, type MusicTrack,
  type MusicVocabulary, type StudioAsset, type StudioAssets, type StudioTake,
} from "../api";
import { notify, useConfirm } from "../ui";
import { useI18n } from "../i18n";
import "../styles/videozone.css";

/** 专区的四块。音乐是唯一端到端跑通的；「我的素材」是三块共用的地基（上传 → 私有存放 →
 *  生成 → 看效果 → 修正 → 历史），它本身不依赖任何生成模型，所以先建它。 */
type Tab = "music" | "assets" | "scene" | "motion";

/** 五台推子。`mood` 与 `vocals` 的候选来自后端（`music.MOODS` / 词表），**不在前端另列一份**：
 *  曾经这里写死过 `["calm", "neutral", "tense", "triumphant", "warm", "dark"]`，而曲库校验用的
 *  是另外七个词 —— `triumphant` / `dark` 根本不是它认的情绪，两个列表各自都对，合起来就错。 */
type Pick = { genre: string; mood: string; instruments: string[]; production: string[]; vocals: string };

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
      <header className="vz-head">
        <h2>{t("Video zone")}</h2>
        <p>{t("Make the pieces a film is built from — here, by hand, and keep them. Members in a group chat can use the same engines through their tools.")}</p>
      </header>
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
   * 五台推子。它们是**结构化选择**，不是一句描述 —— ACE-Step 的 `tags` 是一组逗号分隔的关键词，
   * 官方公式是「流派, 情绪, 乐器, 人声, 制作, BPM」，流派必须放第一、超 12 个词开始互相稀释、
   * 具体名词远胜形容词。一个自由文本框保证不了这些，所以这里给出的每个选项都是**模型认的词**。
   */
  const [pick, setPick] = useState<Pick>({ genre: "", mood: "calm", instruments: [], production: [], vocals: "instrumental" });
  const [note, setNote] = useState("");               // 补充描述（追加在拼好的 tags 之后）
  const [preview, setPreview] = useState<MusicPreview | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [bpm, setBpm] = useState(72);
  const [title, setTitle] = useState("");
  const [open, setOpen] = useState(false);            // 高级选项
  const [lyrics, setLyrics] = useState("");
  const [language, setLanguage] = useState("en");
  const [seed, setSeed] = useState(0);
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
   * 实时预览：**拼串走后端**，因为拼词规则（流派第一、词数上限、每个流派的 BPM 区间）只有
   * `musicprompt.compose_tags` 一处。前端自己拼一份的话，用户看到一串、实际送出去另一串 ——
   * 这个项目已经因为"一个判断写了几份"返工过好几轮，这里一次也不再犯。
   */
  useEffect(() => {
    if (!vocab) return;
    let live = true;
    const timer = window.setTimeout(async () => {
      try {
        const got = await api.previewMusic({ ...pick, bpm, prompt: note });
        if (live) setPreview(got);
      } catch {
        /* 预览失败不影响作曲：真正的拼串在提交时还会做一次 */
      }
    }, 250);
    return () => { live = false; window.clearTimeout(timer); };
  }, [pick, bpm, note, vocab]);

  /** 「作曲中」要显示已经过去多久 —— 它是分钟级的，一个不动的转圈看起来就是死了。 */
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
    // 与后端同一条判据：**必须有流派或一句自己写的话**。mood 有默认值，所以「什么都没选」
    // 不等于「文本框是空的」—— 只看文本框会放行一次只有一个词的提交。
    if (!pick.genre && !note.trim()) {
      notify(t("Pick a genre, or describe the music yourself."));
      return;
    }
    setBusy(true);
    try {
      const { job: started } = await api.composeMusic({
        ...pick, bpm, seconds, name: title.trim(), lyrics: lyrics.trim(),
        language, seed, prompt: note,
      });
      setJob(started);
      setClock(Date.now() / 1000);
    } catch (e) {
      notify((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  /** 选流派就把速度挪到它真正的区间里 —— 官方指南明确说流派与 BPM 对不上时模型会来回摇摆。 */
  const chooseGenre = (id: string) => {
    const g = vocab?.genres.find((x) => x.id === id);
    setPick((p) => ({ ...p, genre: p.genre === id ? "" : id }));
    if (g?.bpm) setBpm(Math.round((g.bpm[0] + g.bpm[1]) / 2));
  };
  const toggleIn = (key: "instruments" | "production", id: string) =>
    setPick((p) => ({
      ...p,
      [key]: p[key].includes(id) ? p[key].filter((x) => x !== id) : [...p[key], id],
    }));
  const applyPreset = (p: MusicPreset) => {
    setPick({ genre: p.genre, mood: p.mood, instruments: p.instruments,
              production: p.production, vocals: p.vocals });
    setBpm(p.bpm);
    setTitle((v) => v || p.label);
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

        {!vocab && !loading && (
          <p className="vz-note bad"><CircleAlert size={14} /> {t("The vocabulary did not load, so only free text is available.")}</p>
        )}

        {/* 场景预设：分类真正省事的地方 —— 把「这几个词该一起用」这件经验固化下来。 */}
        {!!vocab?.presets.length && (
          <div className="vz-field">
            <span>{t("Start from a scene")}</span>
            <div className="vz-chips">
              {vocab.presets.map((p) => (
                <button key={p.id} className="vz-chip" disabled={!ready} title={p.note}
                        onClick={() => applyPreset(p)}>{p.label}</button>
              ))}
            </div>
          </div>
        )}

        {/* 流派：单选。每个都标出它真正生活的 BPM 区间 —— 选错速度是模型变糊的常见原因。 */}
        <div className="vz-field">
          <span>{t("Genre (this anchors everything else)")}</span>
          <div className="vz-chips">
            {vocab?.genres.map((g) => (
              <button key={g.id} disabled={!ready} title={g.use}
                      className={"vz-chip" + (pick.genre === g.id ? " on" : "")}
                      onClick={() => chooseGenre(g.id)}>
                {g.label}{g.bpm && <em>{g.bpm[0]}–{g.bpm[1]}</em>}
              </button>
            ))}
          </div>
          {pick.genre && (
            <p className="vz-hint">{vocab?.genres.find((g) => g.id === pick.genre)?.use}</p>
          )}
        </div>

        <div className="vz-row">
          <label className="vz-field">
            <span>{t("Tempo (BPM)")} <b>{bpm}</b></span>
            <input type="range" min={40} max={190} step={1} value={bpm} disabled={!ready}
                   onChange={(e) => setBpm(Number(e.target.value))} />
          </label>
          <label className="vz-field narrow">
            <span>{t("Mood")}</span>
            <select value={pick.mood} disabled={!ready}
                    onChange={(e) => setPick((p) => ({ ...p, mood: e.target.value }))}>
              {vocab?.moods.map((m) => <option key={m} value={m}>{t(m)}</option>)}
            </select>
          </label>
          <label className="vz-field narrow">
            <span>{t("Vocals")}</span>
            <select value={pick.vocals} disabled={!ready}
                    onChange={(e) => setPick((p) => ({ ...p, vocals: e.target.value }))}>
              {vocab?.vocals.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
            </select>
          </label>
        </div>

        {/* 乐器与制作：多选，而且**都是能被渲染出来的具体名词** ——
            `felt piano` 出来的是毡化钢琴，`sophisticated` 出来的是随机。 */}
        <div className="vz-field">
          <span>{t("Instruments (pick the ones you actually want to hear)")}</span>
          <div className="vz-chips">
            {vocab?.instruments.map((i) => (
              <button key={i.id} disabled={!ready} title={i.for}
                      className={"vz-chip" + (pick.instruments.includes(i.id) ? " on" : "")}
                      onClick={() => toggleIn("instruments", i.id)}>{i.label}</button>
            ))}
          </div>
        </div>
        <div className="vz-field">
          <span>{t("Production")}</span>
          <div className="vz-chips">
            {vocab?.production.map((p) => (
              <button key={p.id} disabled={!ready}
                      className={"vz-chip" + (pick.production.includes(p.id) ? " on" : "")}
                      onClick={() => toggleIn("production", p.id)}>{p.label}</button>
            ))}
          </div>
        </div>

        <label className="vz-field">
          <span>{t("Anything the list above has no word for (optional)")}</span>
          <input value={note} disabled={!ready}
                 placeholder={t("e.g. no drums, fades under the narration")}
                 onChange={(e) => setNote(e.target.value)} />
        </label>

        {/* 预览：让用户看见**模型真正会收到的那串词**。这不只是校对 ——
            看几次就知道提示词该怎么写，比读一段说明有用。 */}
        <div className="vz-preview">
          <span className="vz-preview-label">{t("The model will receive")}</span>
          <code>{preview?.tags || "—"}</code>
          {preview?.warnings.map((w, i) => (
            <p key={i} className="vz-warn"><TriangleAlert size={13} /> {w}</p>
          ))}
        </div>

        <div className="vz-row">
          <label className="vz-field">
            <span>{t("Seconds")} <b>{seconds}s</b></span>
            <input type="range" min={limits.min_seconds} max={limits.max_seconds} step={5}
                   value={seconds} disabled={!ready}
                   onChange={(e) => setSeconds(Number(e.target.value))} />
          </label>
          <label className="vz-field">
            <span>{t("Title (optional)")}</span>
            <input value={title} disabled={!ready} placeholder={t("what this theme is for")}
                   onChange={(e) => setTitle(e.target.value)} />
          </label>
        </div>
        <button className="vz-more" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {open ? "▾" : "▸"} {t("Advanced")}
        </button>
        {open && (
          <div className="vz-adv">
            <label className="vz-field">
              <span>{t("Lyrics (optional — leave empty for instrumental)")}</span>
              <textarea rows={2} value={lyrics} disabled={!ready} onChange={(e) => setLyrics(e.target.value)} />
            </label>
            <div className="vz-row">
              <label className="vz-field narrow">
                <span>{t("Language")}</span>
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
        {job?.state === "running" ? (
          <>
            <p className="vz-note"><LoaderCircle size={14} className="vz-spin" /> {t("Composing… {n}s elapsed. It runs on this machine and takes minutes.", { n: elapsed })}</p>
            <p className="vz-hint">{job.prompt}</p>
          </>
        ) : (
          <button className="btn primary vz-go"
                  disabled={!ready || busy || (!pick.genre && !note.trim())} onClick={() => void compose()}>
            <Sparkles size={15} /> {t("Compose")}
          </button>
        )}
        {job?.state === "failed" && (
          <p className="vz-note bad"><CircleAlert size={14} /> {job.error}</p>
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
