import type { Settings } from "../api";
import { useData } from "../data";
import { Switch } from "../ui";
import { NumInput, Row, useSettingsSaver } from "./rows";

type NumKey = "history_clip" | "tool_output_limit" | "history_limit" | "request_timeout" | "circuit_threshold" | "circuit_cooldown" | "memory_top_k" | "library_top_k";
interface NumRow { key: NumKey; title: string; desc: string; min: number; max: number; unit?: string }

const BASE_ROWS: NumRow[] = [
  { key: "request_timeout", title: "单次请求超时", desc: "超过这个时间还没响应,就视为失败并回退到下一个模型。", min: 5, max: 600, unit: "秒" },
  { key: "circuit_threshold", title: "熔断:连续失败次数", desc: "某个模型连续失败达到这个次数后,暂时不再尝试它。", min: 1, max: 10, unit: "次" },
  { key: "circuit_cooldown", title: "熔断:冷却时间", desc: "熔断后隔多久再给这个模型一次机会。", min: 5, max: 600, unit: "秒" },
];
const CTX_ROWS: NumRow[] = [
  { key: "history_limit", title: "带入的历史消息条数", desc: "每次调用模型时附带的最近群聊消息数量。越多越连贯,也越耗 token。", min: 4, max: 100, unit: "条" },
  { key: "history_clip", title: "单条历史消息最多带入", desc: "过去的某条消息太长时,只保留开头和结尾、省掉中间,防止一篇长文占满上下文。最新的一条不受影响。", min: 200, max: 20000, unit: "字" },
  { key: "tool_output_limit", title: "工具结果最多回填", desc: "工具(检索、抓取网页等)返回的内容太长时,回填给模型前截到这么多字。", min: 500, max: 50000, unit: "字" },
];
const MEM_ROWS: NumRow[] = [
  { key: "memory_top_k", title: "每次带入的记忆条数", desc: "回复前按相关度取最多这么多条记忆放进提示词。0 表示不带入。", min: 0, max: 20, unit: "条" },
  { key: "library_top_k", title: "资料库每次检索返回的片段数", desc: "成员检索资料库时最多返回几段。", min: 1, max: 10, unit: "段" },
];

export default function GeneralPage() {
  const { settings } = useData();
  const { set, err } = useSettingsSaver();
  if (!settings) return <div className="empty big">加载中…</div>;

  const numRows = (rows: NumRow[]) =>
    rows.map((r) => (
      <Row key={r.key} title={r.title} desc={r.desc}>
        <NumInput v={settings[r.key]} min={r.min} max={r.max} unit={r.unit} onCommit={(n) => set({ [r.key]: n } as Partial<Settings>)} label={r.title} />
      </Row>
    ));

  return (
    <div className="sp">
      <h2 className="sp-title">通用</h2>
      <p className="sp-desc">协作与路由的运行参数。修改后即时生效。</p>
      {err && <div className="ext-errbox ext-sticky-err" role="alert"><div className="err">保存失败:{err}</div></div>}

      <div className="card flush">{numRows(BASE_ROWS)}</div>

      <div className="sec">上下文管理</div>
      <div className="card flush">{numRows(CTX_ROWS)}</div>

      <div className="sec">记忆与资料库</div>
      <div className="card flush">
        <Row title="启用记忆" desc="成员回复前会带入相关的偏好、决定和教训。每个群可以单独关闭。">
          <Switch checked={settings.memory_enabled} label="启用记忆" onChange={(v) => void set({ memory_enabled: v })} />
        </Row>
        <Row title="群聊后自动整理记忆" desc="一轮群聊结束后,把这次的请求和最终答复交给一个又快又省的模型(按路由选,可能是云端服务商)提炼偏好、决定和教训存为记忆。会额外调用一次模型;程序会过滤明显的密码、密钥,但不保证万无一失,请不要在群里发送密钥。">
          <Switch checked={settings.memory_auto_extract} label="群聊后自动整理记忆" onChange={(v) => void set({ memory_auto_extract: v })} />
        </Row>
        {numRows(MEM_ROWS)}
      </div>
    </div>
  );
}
