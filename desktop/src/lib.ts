export const APP_VERSION = "0.5.0";

import { modelLabel, type Model, type RoutePreview } from "./api";

export function routeText(preview: RoutePreview | null, models: Model[]): { text: string; offline: boolean } {
  if (!preview) return { text: "", offline: false };
  if (!preview.external_calls_enabled) return { text: "离线模式 · 仅本地模型", offline: true };
  const names = preview.chain.map((c) => modelLabel(c, models));
  return { text: names.length ? names.join(" → ") : "无可用模型", offline: false };
}

/** 首页的三个场景:默认拉哪些成员进群、以及一组一键填入的提示词模板。 */
interface Scene {
  id: string;
  label: string;
  members: string[]; // 按名字匹配已有成员;找不到的忽略
  chips: { label: string; prompt: string }[];
}

export const SCENES: Scene[] = [
  {
    id: "office",
    label: "日常办公",
    members: ["小助", "文案", "校对"],
    chips: [
      { label: "会议纪要", prompt: "@小助 帮我把下面的会议记录整理成会议纪要,包含:结论、待办事项(负责人+截止时间)、风险点。\n\n会议记录:\n" },
      { label: "周报", prompt: "@小助 根据我这周的工作要点写一份周报,分为:本周完成、遇到的问题、下周计划。\n\n工作要点:\n" },
      { label: "通知公文", prompt: "@文案 起草一份通知,主题:\n发文对象:\n关键信息(时间/地点/要求):\n请按公文格式撰写,@校对 负责审校。" },
      { label: "邮件润色", prompt: "@校对 请润色下面这封邮件,语气专业、简洁,保留原意,并指出你改动的地方。\n\n" },
    ],
  },
  {
    id: "video",
    label: "视频制作",
    members: ["小助", "分镜", "文案"],
    chips: [
      { label: "短视频脚本", prompt: "@小助 我要做一条短视频。主题:\n时长:60 秒\n平台:抖音\n请先让 @文案 写口播文案,再让 @分镜 出分镜表。" },
      { label: "分镜表", prompt: "@分镜 请把下面的文案拆成分镜表(镜号、画面、旁白、时长、镜头运动、音效)。\n\n文案:\n" },
      { label: "产品宣传片", prompt: "@小助 为下面的产品做一条 30 秒宣传片:先定核心卖点,再出文案和分镜。\n\n产品介绍:\n" },
      { label: "标题与封面文案", prompt: "@文案 给这条视频想 10 个吸引点击的标题和 3 个封面文案,风格:\n\n视频内容:\n" },
    ],
  },
  {
    id: "write",
    label: "创作写作",
    members: ["小助", "文案", "校对"],
    chips: [
      { label: "公众号文章", prompt: "@小助 写一篇公众号文章。选题:\n目标读者:\n风格:\n请先给大纲,确认后由 @文案 成文,@校对 审稿。" },
      { label: "小红书笔记", prompt: "@文案 写一篇小红书种草笔记,产品/主题:\n要求:标题带钩子、正文分段清晰、结尾加 5 个话题标签。" },
      { label: "故事创意", prompt: "@文案 围绕下面的设定,给我 3 个不同方向的故事梗概,各 200 字以内。\n\n设定:\n" },
      { label: "文稿校对", prompt: "@校对 请检查下面文稿的错别字、逻辑漏洞和前后不一致,列出问题并给出修改后的版本。\n\n" },
    ],
  },
];
