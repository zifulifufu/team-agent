# 流程协作里查出来的问题（2026-09-28）

用**程序自己的账本**当证据查的：每个群的 `流程日志.md` 是流程工程师按 `proclog.py` 的规则写的，
一条记录 = 流程本身的一个毛病。查了 4 个群：

| 群 | 记录 | 唯一 key | 重复率 | open | fixed | verified | `seen=1` 的占比 |
|---|---|---|---|---|---|---|---|
| `a54f9f874a82`（视频制作，正在跑） | 125 | 122 | 2.4% | **124** | 1 | **0** | 119/125 |
| `31dd6fef6e27` | 88 | 87 | 1.1% | **86** | 1 | **1** | 84/88 |
| `fe957e1bda39` | 50 | 48 | 4.0% | **49** | 1 | 0 | 47/50 |
| `88fb0742d5b8`（新群） | 2 | 2 | 0% | 2 | 0 | 0 | 2/2 |

⚠️ 结论先摆在这里：**账本只增不减，永远收敛不了**。四个群合计 **265 条记录、261 条还挂着 `open`**，
一共只 `fixed` 过 3 条、`verified` 过 1 条、`wontfix` 0 条。`a54f9f874a82` 那份已经 196 KB，
72 条按定义是 `blocker`。

---

## 问题 1（根因）· 判据记的是「这一次」，不是「这个毛病」

`proclog.py` 自己写着 key 是「同一个毛病的计数单位」：

> `auto_task_defects`：「One key for one underlying defect, whatever shape it arrived in: the file is
> missing. … keying on the status would file two rows for it and **reset the count the moment the
> shape changed, which is exactly the number that says "this keeps happening"**.」

但同一个函数**最后两行**又给 key 加了一个**每轮必然不同**的指纹：

```python
for defect in out:
    defect["check"] = task_check(task)
    defect["key"] += ":" + defect["check"]["scope"]      # scope = signature(负责人+标题+交付物+指令+参数)
```

`task_check` 的口径是 `signature([owner, title, deliverable, instruction, arguments])` —— 而**每一轮
都会重新分工**，标题/交付物/参数都不可能与上一轮一模一样。所以：

- `missing-file:数字人-短样片-v5-1080p.mp4:40a786a92a65ea95871bee21`
- `tool-failed:6bfbc1a8b363:generate_video:18950e53ff970b2b6741cf42`（`sig` = 那次调用的参数 hash）

**两条自动 key 都按构造唯一**。实测：125 条记录 122 个不同 key、`seen` 里 119 条是 `1`。
那个「说明它老是犯」的计数**一次都没涨过**——它想数的东西，被它自己加的那个后缀抹掉了。

> 这是本项目的老病根换了个地方发作：**同一个判断写了两份**（「毛病是谁」在注释里按成因，
> 在两行后的代码里按实例），而**消费者**（去重、`seen`、复核匹配）信的是代码那份。

**修法（最小改动）**：`scope` 只用来做 `check`（复核时匹配「同一操作重跑成功」），**不要拼进 key**。
key 按成因取：`missing-file`、`tool-failed:<工具>`、`task-failed`、`plan-invalid`……实例信息
（哪个文件、哪次调用）放进 `evidence`。这样 69 条 missing-file 会当场收敛成 1 条、`seen` 变成 69，
而「这个坑踩了 69 次」才是使用者能据此行动的那句话。

---

## 问题 2 · 复核路径是死的，而且**越是老毛病越不会再被提起**

`feedback(entries, limit=6)` 每轮只挑 6 条喂回群里，排序是：

```python
pending.sort(key=lambda e: (SEVERITIES.index(e.severity), e.advised, -e.seen, e.last))
```

⚠️ 中间那个 `e.advised` 是**升序**：**先挑「被提醒得最少」的**。于是：

1. 每轮新产生的记录 `advised=0`，**永远排在所有旧记录前面** → 老毛病被新鲜事挤掉。
   实测：20 条已经到 `advised: 3` 的记录，最后一次被提停在 **09-28 00:15–02:15**；
   而到 **06:32** 账本还在往外产新记录 —— 中间这几小时里，新记录（`advised=0`）一直在占满那 6 个名额。
   **一个毛病被提过三次之后，系统基本就不再提它了 —— 而「一直在犯」恰恰是它最该被提的理由。**
2. 复核只有在**观测到的 `scope` 与记录里存的一模一样**时才算通过：
   `match = next(o for o in observations if o.get("check") == e.check and o.get("ok"))`。
   既然 `scope` 每轮都变，这条几乎不可能命中 —— 125 条里有 **94 条是带 `check` 的**（本来够格自动复核），
   实际 `verified` 数是 **0**。设计里那个「只有重跑过才算数」的状态，从来没被用过。

**修法**：排序键把 `e.advised` 换成「按 `seen` 降序 + 按 `last` 升序」（最久没提、最常犯的先提），
并且给记录一个**退出机制**：`advised` 到上限仍不收敛的，自动升格为「需要人决策」而不是继续静静挂着。
复核的 `check` 要能与 key 解耦（见问题 1），否则永远匹配不上。

---

## 问题 3 · 真正最大的缺陷簇：**「复核过了」从不落盘**

69 条 missing-file 按扩展名拆：`.md 29 / .mp4 25 / .png 13 / .wav 2`，
而其中 **30 条（43%）** 的名字带「复核 / 验收 / 核验 / 诊断 / 报告 / 记录 / 对账」——
也就是说，**群里最常「承诺了却没落盘」的东西，是复核报告本身**：

```
数字人短样片-v5-独立复核.md   图卡复核-段8-v4.md   成片-300s-独立验收.md
数字人短样片-诊断记录-v3.md   图卡验收-段3至段8-医学侧.md   旁白重录与时长对账.md
```

另外两个群同形：`31dd6fef6e27` 52 条 missing-file 里 19 条（36%）是复核类；`fe957e1bda39` 30 条里
13 条（43%），且 28/30 是 `.md`。

**这是流程协作的真问题，不是内容问题**：群主派了「复核」「验收」这类任务，负责人在群里**说**了复核结论，
但**没有把复核写成文件**——而下游「终核」和「整合」按交付物判定，于是：

- 复核环节看起来做了（有对话），实际没有可核验的产物；
- `final`/`整合` 在素材没齐时照样开跑，反复失败（`a54f9f874a82` 里「任务失败:整合」记了 5 条）。

**修法**：复核类任务的交付物必须是**文件**，而且完成判定只看文件；只说不写不算完成。
（这条比多写检查代码更根本 —— 现在 `missing-file` 检查确实抓到了，但它是**事后记账**，
不是**事前拦住 `done`**。）

---

## 问题 4 · 同一个根因被记成多条，于是「第三次踩同一个坑」看不出来

「派给外部命令行引擎（WorkBuddy）的任务超过它 20 轮上限」这一件事，在 `a54f9f874a82` 里记了三次、
key 各不相同、三天后仍在 `open`：

| 记录 | 日期 | 口径 |
|---|---|---|
| `P-20260925-2`（handoff, minor） | 09-25 | 「给 WorkBuddy 派的任务超过引擎轮次上限」 |
| `P-20260924-6`（tool, blocker）的 cause② | 09-24 | 同一件事，作为另一条的根因出现 |
| `P-20260928-43`（handoff, major，`seen: 3`） | 09-28 | 「有成员这轮跑不了:WorkBuddy —— 超过其命令行引擎 20 轮上限」 |

第三条是唯一一条 `seen` 涨到 3 的（因为它的 key `member-blocked:WorkBuddy` **不含**轮次指纹）。
它恰好证明了：**key 稳定时计数是有用的**，而按现在多数 key 的写法，这个信号拿不到。

---

## 问题 5（旁证）· 工具失败很集中，且重复得很稳定

`a54f9f874a82` 里 33 条 `tool-failed` + 8 条 `tool-loop`，反复落在同几个点上：

- `generate_video` —— 服务账户欠费（HTTP 403 overdue balance），成员反复重试；
- `(格式错误)` —— 成员手写非标准标记（`＜|｜DSML｜｜ invoke＞`）并且 `arguments` JSON 被截断；
- `inspect_media` / `review_picture` / `review_audio` / `synthesize_speech` / `make_figure`。

因为 key 按「调用指纹」分发，同一个工具在三个成员身上失败会记成三条，看不出「是这个工具/这条链路坏了」。

---

## 建议的动手顺序

1. **key 与 scope 解耦**（问题 1）—— 一行改动量级，收益最大：账本立刻收敛，`seen` 开始有意义。
2. **`feedback` 的排序键改掉**（问题 2）—— 一行，让旧账和「老犯的毛病」能重新被提起。
3. **复核类任务的交付物必须是文件**（问题 3）—— 这条要改的是**派工与验收的约定**（提示词 + 完成判定），
   不是加检查代码。
4. 复核的 `check` 匹配放宽到「同一操作」而不是「同一次调用」（问题 2 后半）。

---

## 怎么复现这份统计

```bash
# 每个群的：状态计数、唯一 key 比例（= 实例化程度）、seen 分布、missing-file 分类
.venv/bin/python scripts/proclog-stats.py ~/.team-agent/workspaces/*/流程日志.md

# 单个群按 key 前缀归类（一眼看出「其实是同一个毛病」）
grep "^- key: " 流程日志.md | sed 's/^- key: //' | cut -d: -f1 | sort | uniq -c | sort -rn
```

⚠️ 要看的那个数是 **`唯一 key / 记录数`**：它接近 1 就说明 key 记的是**这一次**而不是**这个毛病**，
`seen` 也就永远涨不上去。`scripts/proclog-stats.py` 就是为这句话写的。

> ⚠️ 这份报告查的是**账本与实际记录的形态**，没有去判断「视频内容好不好」。
> 后者要人读片子，程序不该猜（`proclog.py` 的注释自己也是这么划界的）。
