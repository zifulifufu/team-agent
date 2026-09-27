import { useCallback, useEffect, useRef, useState } from "react";
import {
  CircleAlert, CircleCheck, Clock3, LoaderCircle, Music2, Pause, Play, Sparkles, Trash2,
} from "lucide-react";
import { api, type MusicJob, type MusicShelf, type MusicTrack } from "../api";
import { notify, useConfirm } from "../ui";
import { useI18n } from "../i18n";
import "../styles/videozone.css";

/** 专区的三块。音乐是唯一现在就能用的 —— 它的引擎（本机 ACE-Step）已经在跑；另两块要先把
 *  「用什么生成」定下来，所以它们如实写着还差什么，而不是给一个点进去什么都没发生的空壳。 */
type Tab = "music" | "scene" | "motion";

export default function VideoZonePage() {
  const { t } = useI18n();
  const [tab, setTab] = useState<Tab>("music");
  const TABS: { id: Tab; label: string; icon: typeof Music2 }[] = [
    { id: "music", label: t("Music"), icon: Music2 },
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
      {tab === "scene" && <SceneBlock />}
      {tab === "motion" && <MotionBlock />}
    </div>
  );
}

// ------------------------------------------------------------------ music
const MOODS = ["calm", "neutral", "tense", "triumphant", "warm", "dark"];

function MusicBlock() {
  const { t } = useI18n();
  const confirm = useConfirm();
  const [shelf, setShelf] = useState<MusicShelf | null>(null);
  const [loading, setLoading] = useState(true);
  const [problem, setProblem] = useState("");
  const [prompt, setPrompt] = useState("");
  const [seconds, setSeconds] = useState(0);
  const [bpm, setBpm] = useState(120);
  const [mood, setMood] = useState("calm");
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
      const got = await api.videoZoneMusic();
      setShelf(got);
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
    const text = prompt.trim();
    if (!text) { notify(t("Describe the music first — genre, instruments, mood, tempo.")); return; }
    setBusy(true);
    try {
      const { job: started } = await api.composeMusic({
        prompt: text, seconds, bpm, mood, name: title.trim(), lyrics: lyrics.trim(),
        language, seed,
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

        <label className="vz-field">
          <span>{t("Style tags")}</span>
          <textarea rows={3} value={prompt} disabled={!ready}
                    placeholder={t("e.g. calm piano, soft strings, slow build, no drums")}
                    onChange={(e) => setPrompt(e.target.value)} />
        </label>
        <div className="vz-row">
          <label className="vz-field">
            <span>{t("Seconds")} <b>{seconds}s</b></span>
            <input type="range" min={limits.min_seconds} max={limits.max_seconds} step={5}
                   value={seconds} disabled={!ready}
                   onChange={(e) => setSeconds(Number(e.target.value))} />
          </label>
          <label className="vz-field narrow">
            <span>{t("Tempo (BPM)")}</span>
            <input type="number" min={40} max={220} value={bpm} disabled={!ready}
                   onChange={(e) => setBpm(Number(e.target.value))} />
          </label>
          <label className="vz-field narrow">
            <span>{t("Mood")}</span>
            <select value={mood} disabled={!ready} onChange={(e) => setMood(e.target.value)}>
              {MOODS.map((m) => <option key={m} value={m}>{t(m)}</option>)}
            </select>
          </label>
        </div>
        <label className="vz-field">
          <span>{t("Title (optional)")}</span>
          <input value={title} disabled={!ready} placeholder={t("what this theme is for")}
                 onChange={(e) => setTitle(e.target.value)} />
        </label>
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
          <p className="vz-note"><LoaderCircle size={14} className="vz-spin" /> {t("Composing… {n}s elapsed. It runs on this machine and takes minutes.", { n: elapsed })}</p>
        ) : (
          <button className="btn primary vz-go" disabled={!ready || busy || !prompt.trim()} onClick={() => void compose()}>
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

// ------------------------------------------------------- not built yet
/** 这两块**故意**先只写清现状与门槛，而不是搭一个能点但生成不出来的空壳。
 *
 *  用户要的场景是 (b)：画面里的环境（手术室、血管内视角、诊室）—— 那需要文生图；要的动作是
 *  (b)：数字人的表情与动作 —— 那需要口型/表情驱动。两者的瓶颈都不是界面，是把渲染链接上，
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
