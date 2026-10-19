# 声音克隆 · 有声读物工坊

本地部署的「声音克隆 + 有声读物生成」全栈应用：克隆自己/家人/朋友的声音形成独特音色（声音模型 → 数字人），把任意文稿变成由该音色朗读的有声读物，支持混音参数微调与在线试听，全部数据本地存储。

```
音频素材 ──🔪 音频分片──▶ 带标注切片 ──一键导入──▶ 🎭 数字人训练数据
                                                     │ 切片→标注→预处理→s2 音色→s1 语感
                                                     ▼
声音克隆（零样本 / 微调权重） ──▶ 🎙️ 声音库 ──▶ 🎛️ 合成工作台 ──▶ 📚 作品成品
                                    │              │
                              📄 文稿(LLM 润色)   每段合成后用 ASR 回读校对，缺字自动重试/回退
                                                     ▼
                                    全部数据仍在本地磁盘（data/）
```

## 功能

| 功能 | 说明 |
|---|---|
| 🎙️ 声音克隆 | 上传 10~60s 清晰参考音频，零样本克隆独特音色（GPT-SoVITS 本地 GPU / MiniMax / Fish Audio 云端 / Edge / OpenVINO） |
| 🎭 数字人 | 在声音之上叠加「训练数据 + 微调权重 + 推理参数」：主数据与 s1 语感 / s2 音色三套独立目录、一键训练、检查点回选、训练记录与数据查重 |
| 🔪 音频分片 | 长音频 → 切片 + 自动标注。默认**识别对齐**（刀口只落在句末，词不被劈开），可选**静音切分**；支持按段/按句粒度、热词偏置、可疑标记、单片改标/拆分/删除、打包下载与导入数字人 |
| ✅ 合成质量守门 | 每段合成完用本地 ASR 回读文本，缺句缺字换种子重试，必要时回退官方语义模型；三档校验模式设置页可调 |
| 📄 文稿润色 | 导入 txt/md/srt，调用激活的 LLM（预置 DeepSeek V4-Flash，参考 4/16 元每百万 token）润色为适合朗读的文本，费用自动统计 |
| 🔊 初版音频 | 内置 Edge 免费音色（微软神经语音，无需密钥）生成初版；GPT-SoVITS 引擎直接以「参考音频 + 文本」合成克隆音色；OpenVINO 支持 NPU/CPU 端侧合成 |
| 🎛️ 混音工作台 | 语速/音调/音量/三段 EQ/背景乐音量/闪避/淡入淡出/段落间隔，滑块实时微调 |
| 🎚️ 分段级微调 | 每个分段可独立设置语速/音调/音量/EQ，渲染时先逐段生效、再叠加全局参数；分段文本可改可删可再拆分，单段可重合成 |
| 📉 背景乐闪避 | 自动跟随人声（侧链压缩）或自定义关键帧闪避曲线（5 点，渲染时逐帧调制背景乐音量） |
| ▶️ 在线试听 | 浏览器内实时混音预览（SoundTouch 变速变调 + WebAudio EQ），零等待 |
| 👥 多人音色对比 | 同一文本用多个声音分别合成，A/B 连播对比，选出最合适音色 |
| 📑 章节书签导出 | 自动识别「第X章/回」切分章节；导出 CUE/LRC/JSON 索引或分章 mp3 ZIP |
| 🎵 词曲分离 | 人声/伴奏分离（UVR5 权重已就位，Demucs 可选），人声轨可作克隆素材 |
| 🎚️ 音频润色 | 5 段 EQ + 压缩/去齿音/滤波 + 混响 + 降噪 + 响度标准化，浏览器内实时试听、FFmpeg 高精度渲染 |
| 🔋 NPU 端侧推理 | OpenVINO 引擎：本地 NPU/CPU 合成，免联网、免费用、不占 GPU（需自备模型） |
| 🧩 模型库 | 语音/润色模型统一管理：预设模板一键添加 → 填密钥 → 单选激活 → 费用估算与连通性测试 |
| 💾 本地存储 | 声音模型、训练数据、文稿、分段音频、成品全部存于 `data/`（SQLite + 文件），该目录不入版本库 |

## 技术架构

- **后端**：Python 3.12 / FastAPI / SQLAlchemy / SQLite / 线程任务队列（`MAX_CONCURRENT_LOCAL_JOBS=1` GPU 串行，云引擎并行 3）
- **前端**：Vue 3 + Vite（无 vue-router，`App.vue` 切视图）+ WaveSurfer.js（波形）+ SoundTouch.js（实时变速变调）+ WebAudio（EQ/混音）
- **音频处理**：FFmpeg（`imageio-ffmpeg` 自动下载静态二进制，无需系统安装）
- **两套 Python 环境严格分离**：后端跑在 `backend\.venv`（**不装 torch**）；识别/训练/词曲分离都在 `F:\envs\gpt_sovits`（Python 3.10 + CUDA PyTorch）里执行，后端只以子进程/常驻 sidecar 方式调用
- **硬件**：8GB 显存可跑 GPT-SoVITS 推理（约 4GB 占用）；训练参数有 8GB 专用预设（batch=4 + 梯度累积 + FP16 + 关 DPO）

## 快速开始

```bash
# 方式一：Windows 一键（推荐）
build_and_start.bat      # 前端未构建则先构建，再拉起 api_v2 + 后端，最后开浏览器
start_all.bat            # 已构建过：同时启动 GPT-SoVITS 引擎与后端
start.bat                # 只启动后端并打开浏览器
start_backend.bat / start_frontend_dev.bat / start_sovits.bat
```

脚本都会优先使用 `backend\.venv\Scripts\python.exe`，没有 venv 才退回系统 python。

```bash
# 方式二：手动
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe run.py        # http://127.0.0.1:8000

# 前端开发模式（可选；生产模式直接用后端挂载的 frontend/dist）
cd ../frontend
npm install
npm run dev                            # http://127.0.0.1:5173（/api 代理到 8000）
```

**Windows 注意**：pip 报 GBK 编码错误时先 `set PYTHONUTF8=1`；启动脚本已统一设置 `PYTHONUTF8=1`。

## 页面导航

🎛️ 合成工作台 · 🎙️ 声音库 · 🎭 数字人 · 🎵 歌曲分离 · 🔪 音频分片 · 🎚️ 音频润色 · 📄 文稿 · 📚 作品 · 🧩 模型库 · ⚙️ 设置

## 引擎选择与性价比

| 方案 | 用途 | 费用 | 说明 |
|---|---|---|---|
| **Edge 免费音色** | 初版音频/日常合成 | **免费** | 微软神经语音，联网即可，无需密钥 |
| **OpenVINO（NPU）** | 端侧合成 | **免费** | 本地 NPU/CPU 推理，免联网，需自备模型（MMS/VITS 系） |
| **GPT-SoVITS**（推荐克隆/数字人） | 零样本克隆 + 微调训练 | **免费**（本地 GPU） | 参考音频直接合成，可微调出专属 s1+s2 权重；8GB 显存可跑 |
| **DeepSeek V4-Flash** | 文稿润色 | 参考 4/16 元/百万 token | 峰谷定价（高峰 9-12/14-18 点，空闲半价），OpenAI 兼容 |
| MiniMax speech-02 / -hd | 云语音合成 | 参考 3~4 元/万字符 | 请求时复刻，中文音质好，无需本地 GPU |
| Fish Audio | 云语音合成 | 积分制，参考 5 元/万字符 | 支持平台训练专属音色 |
| 火山引擎 / Azure | 云语音合成 | 参考 2 / 6 元/万字符 | 模型库预置模板，填密钥即用 |
| 智谱 GLM-4-Flash | 文稿润色 | **免费** | 低成本批量润色首选 |

> 推荐组合：**GPT-SoVITS 本地克隆/训练（免费）+ Edge 初版音频 + GLM-4-Flash 或 DeepSeek 润色**，几乎零成本。
> 单价均为模型库预置模板里的可编辑参考值，按当期官网价自行校正。

## 接入 GPT-SoVITS（本地声音克隆，本机已配置）

- 源码目录 `F:\GPT-SoVITS-main`（可用 `.env` 的 `GPT_SOVITS_ROOT` 覆盖），推理环境 `F:\envs\gpt_sovits`（uv 创建的 Python 3.10 + CUDA 版 PyTorch，**不是 conda**）
- 启动：`start_sovits.bat` / `start_sovits.ps1`（或 `start_all.bat` 一并拉起）。API 地址 `http://127.0.0.1:9880`，模型加载约 30~60 秒
- `backend\.env`：
  ```
  GPT_SOVITS_URL=http://127.0.0.1:9880
  GPT_SOVITS_ROOT=F:/GPT-SoVITS-main
  GPT_SOVITS_REF_DIR=F:/GPT-SoVITS-main/refs
  ```
- 实测 api_v2 可用端点只有 `/tts`、`/control`、`/set_gpt_weights`、`/set_sovits_weights`、`/set_refer_audio`。**没有 `/vc`**：新版 api_v2 移除了音色转换，克隆音色直接用「参考音频 + 目标文本」走 `/tts` 合成（`/api/synthesis/vc` 作为遗留端点保留，当前引擎下不可用，界面未暴露）
- **零样本必须显式绑定官方底模**（`GPT_SOVITS_BASE_GPT` / `GPT_SOVITS_BASE_SOVITS`，缺省指向 `gsv-v2final-pretrained`）。不绑定时 api_v2 会沿用上一次的权重——通常是某个微调模型，会让所有零样本音色都掺进那个人的音色
- 切 s1 权重后 api_v2 固定返回一次约 0.74 秒的废音频，引擎已用完整请求体做一次暖机丢弃，别把这段当故障

**环境要点（踩坑记录）**：
- PyTorch 2.10.0+cu126（本地 wheel 安装）；**torchcodec 必须 0.10.0**（与 torch 2.10 ABI 匹配，0.13+/0.16 会 DLL 加载失败）
- 子进程统一用 `F:\envs\gpt_sovits\Library\bin\ffmpeg.exe`（gyan 7.1 build）：`FFMPEG_BINARY` 与 PATH 都显式指向它，避免撞上系统里没有 ffmpeg 的情况
- `pyopenjtalk`/`jieba_fast` 缺 Windows SDK 无法编译，用兼容 stub（中文/英文/粤语不受影响，仅日文不可用）
- `TTS.py` 打了 `backend="soundfile"` 补丁，绕过 torchcodec 音频解码路径
- `NUMBA_CACHE_DIR` 在训练子进程里重定向到用户可写临时目录：site-packages 对当前用户只读时，librosa/numba 写 `__pycache__` 被拒会在 Windows 上陷入 PermissionError 重试死循环，表现为训练在 `loaded pretrained` 之后 100% CPU 空转卡死
- 调 funasr / 官方脚本时**必须带 `cwd=GPT_SOVITS_ROOT`**，否则它会把 1.1GB 的 ASR 模型下到当前仓库目录里（已经踩过一次）

## 数字人与微调训练

零样本克隆是带参考音频的模仿；微调训练会采集音色特征产出专属 s1/s2 权重，之后任意长度文本都稳定像真人朗读。数字人 = 声音 + 训练数据 + 权重 + 推理参数，完全独立于声音库的简单训练面板。

- **数据目录三选一**：主数据 `data_dir`、s1 语感 `s1_data_dir`、s2 音色 `s2_data_dir`（后两者留空时自动创建 `<data_dir>_s1` / `_s2`）。可以直接把音频丢进目录后点「处理目录音频」，也可上传或从「🔪 音频分片」一键导入
- **处理流程**：切片 → FunASR 中文自动标注 → 文本音素/HuBERT/语义特征预处理 → SoVITS 音色模型训练 → GPT 语义模型训练（官方脚本，与 webui 同源；首次训练需下载 ASR 模型）
- **训练参数**：8GB 优化预设 `batch=4 / epochs=15 / lr=1e-4 / FP16 / 梯度累积 4 / DPO=关闭`，另有 16GB 预设；轮次可在 2~100 间自定
- **耗时参考**（本机 8GB 显存、`training_records` 实测）：s2+s1 双模型 15 轮普遍在 **16~28 分钟** 区间，素材从 6 分钟到 24 分钟的几次训练都落在这个量级——大头是预处理与固定轮次训练，不完全随音频时长线性增长
- **样本时长硬约束**：s2 训练只收 **0.6~54 秒**的样本，越线样本会在 `module/data_utils.py` 里被**静默丢弃**（不报错）。所以分片缺省压到「段 ≤20 秒 / 句 ≤10 秒」，并留 45 秒硬护栏，超线会在任务提示里点名
- **产物与追溯**：`SoVITS_weights_v2/<exp>.pth`（音色）+ `GPT_weights_v2/<exp>.ckpt`（语义）；每次训练落一条 `training_records`（数据目录、文件 SHA256、总时长、起点权重、参数、耗时），支持逐 epoch 检查点回选与跨目录数据重复检测
- **推理参数预设**：文学朗读（temp 0.72 / top_p 0.85 / top_k 15）、稳定朗读、生动演绎
- **混合音色（推理级）**：合成时勾选其他克隆声音作「辅助参考」（`aux_refs` / `aux_ref_paths`，后者可以直接指向数字人训练数据里的切片），api_v2 以 `aux_ref_audio_paths` 多参考即时融合，无需训练
- 标注可修正：`F:\GPT-SoVITS-main\logs\<exp>\1-name2text.txt`，或在数字人「训练数据」页逐条改（改动会写回 `train.list`）
- **Windows 训练补丁**（本机已打，勿覆盖）：`s2_train.py`/`s1_train.py` 单卡直训（绕开 torch 2.10 的 gloo/DDP Windows 崩溃）、`AR/data/bucket_sampler.py` 单卡回退、`s1_train.py` 放行 pathlib 的 `weights_only` 加载

## 音频分片（长音频 → 训练素材）

上传长音频 → 切成片段并自动标注 → 试听/改标 → 下载、导出或导入数字人。

- **识别对齐（默认）**：先把整条音频识别一次，拿 FunASR 的标点级时间戳（VAD-Punc Paraformer；填了热词则改用 SeACo-Paraformer），刀口**只落在引号已闭合的句末**，再吸附到附近静音的中点。对照实测（同一段 180 秒评书）：静音模式 15 个刀口里 8 个切在句子中间，识别对齐偏离标点边界 **0 刀**；在 1403 秒的长素材上，旧静音模式有 47/223（21%）个刀口既不在任何 ≥0.5 秒静音内、也不在标点处（来自对长区间的等分），会切出「攻 ‖ 杀战守」「杏黄色 ‖ 丝绦」这类劈开词片的样本，直接教坏 s1 的断句
- **静音切分（可选）**：只认声学停顿（ffmpeg silencedetect），快得多。三档预设：训练素材（3~10 秒）/ 句子级（便于校对）/ TTS 参考音频（6~10 秒）；关键参数 `noise_db`（低于视为静音）、`min_silence`、`merge_gap`（两刀间距小于它就并段，**并不是丢**）、`min_piece`、`max_piece`
- **粒度**：按段（若干完整句，适合 s1 语感，缺省 ≤300 字 / ≤20 秒）或按句（一句一片，适合 s2 音色，缺省 ≤120 字 / ≤10 秒）。切换粒度不用重识别——识别结果缓存在任务目录的 `sentences.json`
- **热词偏置**：空格分隔的人名/地名/术语，走 SeACo-Paraformer。实测能纠正「岳河→岳和」「片着瓦无存→片瓦无存」，但不是全对（「王员外→王外外」这类仍在），关键名词仍建议抽检
- **可疑标记**：`no_break`（末尾找不到句末断点）、`quote`（引号不配对）、`cap_comma`（为不超过时长上限在逗号处收尾）。列表页有「只看可疑」视图，人工优先校对这批
- **单片操作**：试听、改标注（改过的记 `edited`）、在指定秒处再拆两段（文本先按字数比例粗分，勾选重标则用 ASR 覆盖前后两片，走常驻 sidecar 约 0.2 秒/片）、软删除与恢复、整任务重标
- **一键导入数字人**：把任务切片复制进数字人训练目录并写 `training_slices`（记录来源任务 id）。重名自动加 6 位 id 后缀、绝不覆盖他人数据；支持追加/覆盖两种模式，覆盖时文本未变算「保留」；导入后从数据库重写 `train.list`

## 合成质量守门（ASR 回读校对）

每段合成完，用本地 Paraformer 把音频回读成文本，与目标文本比对字数：

- 三档模式（设置页 > `.env` 的 `ASR_VERIFY` > 缺省 `smart`）：`full` 每句都校验（最准最慢）、`smart` 只校验短句与含引号的对话句、`off` 退回旧的总时长判据
- 缺字/缺句时换种子重试，仍失败则该段回退官方语义模型并标记 `fallback_sentences`
- 常驻 sidecar（`backend/asr_sidecar.py`）由 `F:\envs\gpt_sovits` 的 python 启动，协议是 stdin/stdout 每行一个 JSON；后端 venv 里没有 torch，也不该装
- 改了分段文本：该段自动置为待合成并删除旧音频，若成品已渲染则清空成品指针并在响应里回 `final_invalidated`，界面提示重新渲染
- 注意：`off` 模式下「丢句首」是查不出来的——总时长看着完全正常

## 歌曲词曲分离（人声/伴奏）

- **UVR5**（默认）：GPT-SoVITS 内置，权重已下载（HP2 通用 / BS-Roformer 高质量 / HP5 主唱 / VR-DeEcho 去回响）
- **Demucs**（可选）：Meta 开源 htdemucs，首次使用自动下载模型
- 结果存 `data/separation/`，支持收藏、波形预览、在线播放、下载；人声轨可送去「音频分片」当纯净克隆素材，伴奏轨可作背景乐

## 音频润色（EQ/混响/压缩）

上传自己朗读的初稿，做专业声学加工：5 段 EQ（厚度/胸腔共鸣/清晰度/临场感/空气感）、压缩器（阈值/比率/启动/释放/增益补偿）+ 去齿音 + 高通/低通、混响（干湿比 + 时长）、FFT 降噪、目标 LUFS 响度标准化。浏览器内 Web Audio 实时试听，渲染时 FFmpeg 高精度输出，原声/成品波形对比与下载。

## 接入 OpenVINO（NPU 端侧合成，可选）

1. `pip install openvino openvino-genai optimum[openvino]`，然后 `optimum-cli export openvino --model facebook/mms-tts-zho E:/models/mms-tts-zho-ov`
2. 模型库「OpenVINO（NPU 端侧）」条目的扩展配置里填模型目录（含 `openvino.xml`）与设备（默认 NPU）。目录放 `voice.json`（`{"name":"我的端侧音色"}`）可自定义显示名
3. 该引擎是固定音色本地合成（无克隆功能），适合初版音频、隐私敏感与断网场景

## 商用模型配置中心（模型库）

所有模型统一在「模型库」管理：**添加模型 → 填密钥 → 激活切换**，合成与润色自动使用当前激活项。

- **缺省预置**（首次启动自动写入，并从旧「设置」页迁移密钥）：Edge 免费音色（默认激活）、OpenVINO、MiniMax、Fish Audio、DeepSeek
- **预设模板**（一键追加，价格为参考值、可编辑）：MiniMax speech-02 / -hd、Fish、火山引擎、Azure、DeepSeek V4-Flash / V4-Pro、通义 Qwen-Plus / Turbo、智谱 GLM-4-Flash（免费）、Kimi、豆包、GPT-4o-mini
- **激活规则**：语音类 / 润色类各只有一个激活（单选自动切换）；删除时保护最后一个激活项
- **性价比估算**：输入预计字数，按单价升序对比各模型费用（标记免费最优 / 最便宜 / 当前使用）
- **连通性测试**：LLM 发真实请求验证；语音类检查密钥完整性
- 密钥只存本机 SQLite，列表打码，保存时不覆盖已配置密钥

## 配置云 API（可选）

在「模型库」添加/激活并填密钥（或 `backend/.env`，旧键自动迁移为缺省条目）：

| 平台 | 用途 | 获取地址 |
|---|---|---|
| MiniMax | 语音合成 | https://platform.minimaxi.com （API Key + GroupId，填在扩展配置） |
| Fish Audio | 语音合成 | https://fish.audio （API Key） |
| DeepSeek V4 | 文稿润色 | https://platform.deepseek.com （`deepseek-v4-flash` / `deepseek-v4-pro`） |
| 通义/智谱/Kimi/豆包/OpenAI | 文稿润色 | 各平台控制台 API Key |

## 使用流程

1. **模型库**：确认激活的语音/润色模型（缺省 Edge；LLM 需自行激活）
2. **声音库**：新建声音 → 上传 10~60s 参考音频 + 对应文本 → 克隆
3. **音频分片**（可选）：上传长音频 → 识别对齐切分（按段给 s1、按句给 s2，需要时填热词）→ 抽检「只看可疑」→ 一键导入数字人
4. **数字人**：绑定声音 → 准备主数据 / s1 / s2 目录 → 处理目录音频（切片 + 标注）→ 选 8GB 预设 → 训练 → 回选检查点
5. **文稿**：新建/导入 →（可选）选风格 LLM 润色 → 载入润色稿
6. **合成工作台**：选文稿 + 声音/数字人 → 新建项目 → 生成声音轨（自动按自然段分段合成，每段 ASR 回读校对）→ 分段级微调 / 实时试听
7. 设置背景乐闪避（自动/曲线）→ 渲染最终作品 → 波形区播放 / 下载 mp3
8. **章节导出**：调整章节范围 → 导出 CUE / LRC / JSON / 分章 mp3 ZIP
9. **多人对比**：工作台底部选多个声音 + 对比文本 → A/B 连播选音色

## 数据目录

```
data/                     # 全部本地，整目录不入版本库
├── app.db                # SQLite：声音/文稿/项目/分段/任务/设置/模型库/数字人/训练数据/分片
├── voices/               # 声音（参考音频、训练数据、引擎配置）
├── training_data/        # 数字人训练数据目录（含 train.list、切片、_align 缓存）
├── uploads/              # 上传的原始文件
├── drafts/               # 初版音频 / 单段合成
├── projects/             # 作品（分段音频、声音轨、成品 mp3）
├── slicing/              # 分片任务（源音频、片段、sentences.json 识别缓存）
├── separation/           # 词曲分离结果
├── polish/               # 音频润色（初稿 + 成品）
└── cache/                # 波形峰值缓存
```

GPT-SoVITS 侧产物在仓库外：`F:\GPT-SoVITS-main\logs\<exp>\`（数据集与 `1-name2text.txt`）、`SoVITS_weights_v2\`、`GPT_weights_v2\`。

## 开发与测试

- API 文档：http://127.0.0.1:8000/docs （当前 83 个路径 / 104 个操作）
- 前端 `npm run build` 后需重启后端（`main.py` 只在启动时挂载 `frontend/dist`）
- **CORS**：服务只绑 127.0.0.1，但浏览器里任意网页仍能对本机端口发请求，所以来源是白名单（本机 8000/5173），需要额外来源时用环境变量 `CORS_ALLOW_ORIGINS` 逗号分隔覆盖
- **重启后端前先查队列**：`GET /api/jobs` 有 `running`/`queued` 就别动（训练中的进程绝不能杀）；启动时 `sweep_interrupted()` 会把上个进程遗留的任务标为中断
- **用 venv 解释器**：`backend\.venv\Scripts\python.exe`。系统/uv 裸 python 里没有 uvicorn，直接 `python run.py` 会 `ModuleNotFoundError`
- 测试（都在 `backend/` 下，用 venv python 跑）：
  - `test_regression.py` — 只读回归，35 项（路由、轻量迁移、训练目录、分片任务、项目指针、服务与仓库卫生），需要后端在跑
  - `test_split_quotes.py` / `test_group_units.py` — 纯逻辑单测：自然段分片、引号栈、段/句分组与 54 秒硬护栏、刀口吸附
  - `e2e_test.py` 及 `e2e_models/polish/sep/train/gptsovits.py` — 会写数据的流水线测试，跑前知道会落新项目/训练记录

## 免责声明

请仅克隆自己、家人、朋友等获得授权的声音。克隆名人声音用于公开发布可能侵犯肖像权/声音权，请遵守法律法规与平台规则。


## 版权声明
本软件源代码可以用于个人学习、教育，任何商用须获得作者授权。