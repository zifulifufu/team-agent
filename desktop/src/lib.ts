export const APP_VERSION = "0.5.0";

import { modelLabel, type Model, type RoutePreview } from "./api";
import { tr } from "./i18n";

/** Permission level of an external-agent engine, as a label in the current language.
 * Shared by the message bubble and the member card so the two cannot drift apart. */
export const levelLabel = (level: string): string =>
  ({ read: tr("Read-only"), edit: tr("Can edit files"), full: tr("Full access") })[level] ?? level;

export function routeText(preview: RoutePreview | null, models: Model[]): { text: string; offline: boolean } {
  if (!preview) return { text: "", offline: false };
  if (!preview.external_calls_enabled) return { text: tr("Offline mode · local models only"), offline: true };
  const names = preview.chain.map((c) => modelLabel(c, models));
  return { text: names.length ? names.join(" → ") : tr("No model available"), offline: false };
}

/**
 * The three scenes on the home page: which members to pull into a new group by default, plus a set of
 * one-click prompt templates.
 *
 * These are *content*, not interface: every string carries an English and a Chinese variant and the UI
 * picks one with `pick()` based on the current language, so the two must be kept in sync.
 */
interface Scene {
  id: string;
  label: string;
  labelZh: string;
  /**
   * Members to pull in, listed per language.
   *
   * Both lists are needed because a member is looked up by its *displayed* name and
   * `/api/agents` returns whichever spelling the request language asks for (the seed members
   * are Aide / Copywriter / Proofreader / Storyboard in English, 小助 / 文案 / 校对 / 分镜 in
   * Chinese). The prompts below @mention the same names, so each one names its own language.
   */
  members: string[];
  membersZh: string[];
  chips: { label: string; labelZh: string; prompt: string; promptZh: string }[];
}

export const SCENES: Scene[] = [
  {
    id: "office",
    label: "Everyday office",
    labelZh: "日常办公",
    members: ["Aide", "Copywriter", "Proofreader"],
    membersZh: ["小助", "文案", "校对"],
    chips: [
      {
        label: "Meeting minutes",
        labelZh: "会议纪要",
        prompt: "@Aide Turn the meeting notes below into minutes: decisions, action items (owner + due date) and risks.\n\nMeeting notes:\n",
        promptZh: "@小助 帮我把下面的会议记录整理成会议纪要,包含:结论、待办事项(负责人+截止时间)、风险点。\n\n会议记录:\n",
      },
      {
        label: "Weekly report",
        labelZh: "周报",
        prompt: "@Aide Write a weekly report from my notes below, split into: done this week, blockers, plan for next week.\n\nNotes:\n",
        promptZh: "@小助 根据我这周的工作要点写一份周报,分为:本周完成、遇到的问题、下周计划。\n\n工作要点:\n",
      },
      {
        label: "Notice / memo",
        labelZh: "通知公文",
        prompt: "@Copywriter Draft a formal notice. Subject:\nAudience:\nKey details (time / place / requirements):\nUse standard memo formatting; @Proofreader will review it.",
        promptZh: "@文案 起草一份通知,主题:\n发文对象:\n关键信息(时间/地点/要求):\n请按公文格式撰写,@校对 负责审校。",
      },
      {
        label: "Polish an email",
        labelZh: "邮件润色",
        prompt: "@Proofreader Polish the email below: keep the meaning, make the tone professional and concise, and list what you changed.\n\n",
        promptZh: "@校对 请润色下面这封邮件,语气专业、简洁,保留原意,并指出你改动的地方。\n\n",
      },
    ],
  },
  {
    id: "video",
    label: "Video production",
    labelZh: "视频制作",
    members: ["Aide", "Storyboard", "Copywriter"],
    membersZh: ["小助", "分镜", "文案"],
    chips: [
      {
        label: "Short-video script",
        labelZh: "短视频脚本",
        prompt: "@Aide I want to make a short video. Topic:\nLength: 60 s\nPlatform: TikTok\nHave @Copywriter write the voice-over first, then @Storyboard produce the shot list.",
        promptZh: "@小助 我要做一条短视频。主题:\n时长:60 秒\n平台:抖音\n请先让 @文案 写口播文案,再让 @分镜 出分镜表。",
      },
      {
        label: "Shot list",
        labelZh: "分镜表",
        prompt: "@Storyboard Turn the copy below into a shot list (shot no., picture, voice-over, duration, camera move, sound).\n\nCopy:\n",
        promptZh: "@分镜 请把下面的文案拆成分镜表(镜号、画面、旁白、时长、镜头运动、音效)。\n\n文案:\n",
      },
      {
        label: "Product promo",
        labelZh: "产品宣传片",
        prompt: "@Aide Make a 30-second promo for the product below: settle the key selling point first, then the copy and the shot list.\n\nProduct:\n",
        promptZh: "@小助 为下面的产品做一条 30 秒宣传片:先定核心卖点,再出文案和分镜。\n\n产品介绍:\n",
      },
      {
        label: "Titles & thumbnails",
        labelZh: "标题与封面文案",
        prompt: "@Copywriter Give me 10 click-worthy titles and 3 thumbnail lines for this video. Style:\n\nVideo:\n",
        promptZh: "@文案 给这条视频想 10 个吸引点击的标题和 3 个封面文案,风格:\n\n视频内容:\n",
      },
    ],
  },
  {
    id: "write",
    label: "Creative writing",
    labelZh: "创作写作",
    members: ["Aide", "Copywriter", "Proofreader"],
    membersZh: ["小助", "文案", "校对"],
    chips: [
      {
        label: "Blog post",
        labelZh: "公众号文章",
        prompt: "@Aide Write a blog post. Topic:\nAudience:\nStyle:\nGive me an outline first, then @Copywriter writes it and @Proofreader reviews.",
        promptZh: "@小助 写一篇公众号文章。选题:\n目标读者:\n风格:\n请先给大纲,确认后由 @文案 成文,@校对 审稿。",
      },
      {
        label: "Social post",
        labelZh: "小红书笔记",
        prompt: "@Copywriter Write a social-media post about:\nRequirements: a hook in the title, short readable paragraphs, 5 hashtags at the end.",
        promptZh: "@文案 写一篇小红书种草笔记,产品/主题:\n要求:标题带钩子、正文分段清晰、结尾加 5 个话题标签。",
      },
      {
        label: "Story ideas",
        labelZh: "故事创意",
        prompt: "@Copywriter Using the premise below, give me 3 story outlines in different directions, each under 200 words.\n\nPremise:\n",
        promptZh: "@文案 围绕下面的设定,给我 3 个不同方向的故事梗概,各 200 字以内。\n\n设定:\n",
      },
      {
        label: "Proofread a draft",
        labelZh: "文稿校对",
        prompt: "@Proofreader Check the draft below for typos, logical gaps and inconsistencies; list the issues and give a corrected version.\n\n",
        promptZh: "@校对 请检查下面文稿的错别字、逻辑漏洞和前后不一致,列出问题并给出修改后的版本。\n\n",
      },
    ],
  },
];
