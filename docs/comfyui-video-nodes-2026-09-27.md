# ComfyUI 视频能力盘点：搜了什么、装了什么、以及画质到底卡在哪（2026-09-27）

用户的要求：**去 GitHub 搜 ComfyUI 相关、能提高视频制作能力、star > 1000 的，需要的就 fork + clone，
看能不能把画质提上去、做出大片的感觉。**

三条结论先放前面：

1. **搜到 21 个 ≥1000 星的相关仓库，fork + clone 了 4 个**（下面有逐个的取舍依据）。
2. ⚠️⚠️ **这台机器上有两个 ComfyUI，应用用的是另一个** —— 我第一次全装错了地方，已纠正。
3. ⚠️⚠️ **InfiniteTalk 的权重本来就在机器上**（在正确那个 ComfyUI 里）。我上一轮那 15.5 GB 是白下的，
   而且还落错了目录（我脚本的 bug，已修）。

---

## 一、搜索：21 个候选，怎么筛的

用 `gh` 的搜索 API（`search/repositories?q=…&sort=stars`）跑了 14 组查询，去重后 **≥1000 星的 21 个**。

⚠️ **方法上的一个坑**：`gh search repos "comfyui video" --json …` 返回的排序不对（top 只有 91 星），
换成 `gh api "search/repositories?q=comfyui+video&sort=stars&order=desc"` 才是真的按星排序。
多词查询一定要走 API。

筛选的三条尺子：**① ≥1000 星；② 真的碰画质**（视频读写 / 运动顺滑 / 分辨率 / 生成）；
③ **在这台 Apple Silicon 上有路可走**。

### fork + clone 了的（都在 `~/Documents/GitHub/`，fork 到 `zifulifufu/` 名下，`upstream` 保留）

| 星 | 仓库 | 对画质的作用 | 本机可用性的实据 |
|---|---|---|---|
| ★2872 | `numz/ComfyUI-SeedVR2_VideoUpscaler` | **视频/图像超分**，直接把分辨率和细节抬上去 | README 的 release notes 里有多条 Apple Silicon 修复：`🍎 MPS: Memory optimization`、`Fix: MPS memory leak regression`、`Fix: CLI watermark error on macOS — MPS-related`、`Fix: Mac subprocess error`。加载时自己打印 `📊 Initial MPS memory: …` |
| ★1855 | `Kosinkadink/ComfyUI-VideoHelperSuite` | 视频读写（LoadVideo / VideoCombine）—— 任何视频工作流的前提 | 依赖只有 `opencv-python` + `imageio-ffmpeg` |
| ★1077 | `Fannovel16/ComfyUI-Frame-Interpolation` | **补帧**（RIFE / GMFSS / IFRNet…）→ 24fps 升到 48/60fps，动作顺滑 | 非 CUDA 路径存在（README：`Support for non-CUDA device (experimental)`，走 taichi 后端）。实测**注册了 16 个节点** |
| ★1854 | `WASasquatch/was-node-suite-comfyui` | 图像处理 / 调色 / 滤镜 —— 让不同来源的镜头有同一种影调 | 纯图像运算，README 提到 mps |

### 没纳入的（附理由，免得下次重新判断一遍）

| 仓库 | 为什么不要 |
|---|---|
| ★6711 `kijai/ComfyUI-WanVideoWrapper` | 已在 `custom_nodes` 里。⚠️ 且 **InfiniteTalk 并不需要它** —— 那三个节点是 ComfyUI 自带的 |
| ★4144 `Lightricks/ComfyUI-LTXVideo` | ⚠️ 它自己的前置条件写着 **`CUDA-compatible GPU with 32GB+ VRAM` + `100GB+ free disk space`** → 这台机器不在范围内。（另外 clone 失败：仓库用 git-lfs，本机没装） |
| ★6135 `cubiq/ComfyUI_IPAdapter_plus` | 要 SD1.5/SDXL 底模；本机只有 `z_image_turbo`（不是 SD 系）→ 装上也没模型可用 |
| ★2590 `kijai/ComfyUI-HunyuanVideoWrapper` | 13B 视频模型，README 不提 Mac，权重几十 GB |
| ★2317 `kijai/ComfyUI-SUPIR` | 静图超分，要 SDXL + SUPIR 权重，本机没有 SDXL |
| ★1141 `smthemex/ComfyUI_Sonic` | 另一条音频驱口型路，与正在试的 InfiniteTalk 重复 |
| ★28449 `ATH-MaaS/Pixelle-Video` | 独立引擎/应用，不是节点包 |
| ★7144 `ddean2009/MoneyPrinterPlus` | 另一套短视频自动化应用 |
| ★5686 `wiltodelta/remove-ai-watermarks` | 用途是抹掉 AI 水印/来源标记，与画质无关 |
| ★2346 `6174/comflowyspace` / ★1350 `zanllp/infinite-image-browsing` | 独立应用，不产出画质 |
| ★2767 `FurkanGozukara/Stable-Diffusion` / ★1359 `fofr/cog-face-to-many` | 文档仓库 / Replicate cog |
| ★1043 `kijai/ComfyUI-Hunyuan3DWrapper` | 3D 生成，与视频无关 |

---

## 二、⚠️⚠️ 这台机器上有两个 ComfyUI，应用用的是另一个

这是这一轮最重要的发现，而且它让第一轮安装全部落空。

| | `~/Documents/GitHub/ComfyUI` | `~/Documents/Codex/2026-09-12/new-chat/work/ComfyUI` |
|---|---|---|
| 模型总量 | 35 GB | **69 GB** |
| custom_nodes | 我装的 4 个 + Manager + WanVideoWrapper + whisperX | 只有 AnimateDiff-Evolved |
| 自带 venv | `.venv`（有 pip） | `../comfy-venv`（**uv 建的，没有 pip 模块**） |
| 应用指向哪个 | ❌ 不是它 | ✅ **是它** |

应用自己的设置（`~/.team-agent` 里）说得很清楚：

```
comfyui_dir    = /Users/neuroextra/Documents/Codex/2026-09-12/new-chat/work/ComfyUI
comfyui_python = …/work/comfy-venv/bin/python
base_url       = http://127.0.0.1:8188     comfyui_auto_start = true
```

8188 上正在跑的就是它（`/system_stats` 的 `argv` 直接报出了路径），设备是 **mps**，
ComfyUI **0.35.0**，venv 里 **torch 2.14.0 + MPS True**。

**已纠正**：4 个节点包改成符号链接到**这个** ComfyUI 的 `custom_nodes/`
（仓库仍在 `~/Documents/GitHub/` 是唯一出处），依赖装进**这个** venv。
⚠️ 它没有 pip → 用 `uv pip install --python <那个 venv 的 python>`。

---

## 三、⚠️ InfiniteTalk 的权重本来就在机器上

应用真正用的那个 ComfyUI 的 `models/` 里，**6 个文件里有 4 个已经在位**（而且 `diffusion_models` 有 29 GB）：

| 已装 | 路径 | 而 `needs` 要的是 |
|---|---|---|
| ✅ `Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors` | `diffusion_models/`（16,643,349,018 字节） | 同一个 ✅ |
| ✅ `lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors` | `loras/` | 同一个 ✅ |
| ✅ `Wan2_1_VAE_bf16.safetensors` | `vae/` | 同一个 ✅ |
| ✅ `wav2vec2-chinese-base_fp16.safetensors` | `audio_encoders/` | 同一个 ✅ |
| ⚠️ `wan2.1_infiniteTalk_**single**_fp16.safetensors` | `model_patches/` | 要的是 **`multi`** 那个 |
| ⚠️ `umt5_xxl_**fp16**.safetensors` | `text_encoders/` | 要的是 **`fp8_e4m3fn_scaled`** |

**两条推论：**

1. `zones.py` 里说的「权重全齐」**比 `comfyui.py` 里说的「六个文件都不在这台机器上」更接近事实** ——
   但两句都是**没有说清指哪一个 ComfyUI** 才写错的。已经改成不替机器下结论（只写「它需要什么」+「缺哪个由『测试』指名」）。
2. **那 2 个文件名不一致，会让探针报「缺文件」而拒绝渲染**，尽管机器上有功能等价的 fp16 版本
   （用户自己的 `~/.team-agent/workflows/wan2.2-reference-person.json` 用的正是 `umt5_xxl_fp16` 这一套命名）。
   这是**应用的工作流清单与机器实际安装之间的一处不一致**，不是「装不上」。

### 我上一轮那次下载的两个错（都已修进脚本）
- **下错了 ComfyUI**：脚本里 hardcode 了 `~/Documents/GitHub/ComfyUI`。现在**先读应用设置里的
  `comfyui_dir`**，读不到就直接报错退出（不许猜）。
- **落错了一层目录**：`hf_hub_download(local_dir=…)` 会把**仓库里的路径**一起复现 ——
  `I2V/Wan2_1-….safetensors` 于是落在 `models/diffusion_models/I2V/` 下（ComfyUI 的清单是按**文件名**取的）。
  现在先下到临时目录、再按文件名搬到位。
- ⚠️ 另外那 5 个文件第一次全被 **`ProxyError: 502 Bad Gateway`** 挡掉（失败 URL 指向 `cas-bridge.xethub.hf.co`），
  第一个（15.5 GB）却成功了。脚本现在设 **`HF_HUB_DISABLE_XET=1`**，绕过 Xet 后端走镜像的普通文件路径。

---

## 四、画质到底卡在哪（量出来的，不是感觉）

拿现成的成片《那四个小时》量：

| 事实 | 数字 |
|---|---|
| 插画的原生尺寸 | **1024×1536**（ImageGen 出图），原图在 `plates/raw/` |
| 到画面上的处理 | 去水印裁 4.2% → **cover 放大 1.31×** → 左右再裁掉 **19%** 宽度 → 1080×1920 |
| 因此 | **竖方向约 23% 的输出像素是 Lanczos 编出来的**；横向细节也被削 |
| 成片 | 1080×1920 / 24fps / 112 秒；每个镜头都是**静图 + 极慢的推** |

所以「大片感」上真正有提升空间的，按收益排序：

1. **镜头真的动起来**（现在每张图都是极慢的推）。这条最值钱，而**机器上已经有能干这件事的权重**：
   `Wan2_1-I2V-14B` 就是**图生视频**模型（16.6 GB，在 `diffusion_models/`），另有 `wan2.2_ti2v_5B`。
   → 这一条不需要新装任何东西，需要的是把 ComfyUI 这条路接通并真跑一次。
2. **细节/分辨率**：SeedVR2 超分（本次新装）。插画被 1.31× 放大过，这是它的用武之地。
3. **影调统一**：was-node-suite 的滤镜/调色（本次新装）。
4. **运动顺滑**：补帧（本次新装）。收益相对最小 —— 现在的运动本来就慢。

---

## 五、还动了什么、以及没做的

- **`~/.local/opt/fork-comfy-video-nodes.py`**：fork+clone 脚本，含每个仓库的取舍理由。
  ⚠️ 它里面写 `GH = "/usr/local/bin/gh"` —— 实测 `gh` 在 `/usr/local`，不在 `/opt/homebrew`。
- **没改仓库代码**（`git status` 干净）。改的是本机数据：两个群的技能清单、节点包与依赖的安装位置。
- **⚠️ 没有真跑一次生成/超分**。装上是「能不能跑」的必要条件，不是充分条件 ——
  这个项目里已经吃过一次亏（`wav2lip-onnx` 权重在、跑得动，出的片子等于静帧）。
  真正的验收是：重启 ComfyUI → 让它出一段视频 / 超分一段片子 → 量实测值。
- **⚠️ 待你确认的一件清理**：我第一次下错的 15.5 GB 还在
  `~/Documents/GitHub/ComfyUI/models/diffusion_models/I2V/`（与正确那个 ComfyUI 里的同一文件重复）。
  删不删由你定 —— 我没有擅自动它。
- **ComfyUI-Manager 会在启动时联网拉 `custom-node-list.json`（raw.githubusercontent.com），
  在这台机器上超时并把整个进程带崩**（实测：`aiohttp.ConnectionTimeoutError` 直接结束进程）。
  这是既有的问题，不是我引入的，但它会拦住「启动 ComfyUI 来验证」这件事。
