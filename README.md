# interview-video-bot

AI 面试求职短视频流水线: 给一条行业(如「互联网」「护理」「汽修」), 自动产出
**选题 → 口播脚本 → TTS 配音 → HTML 画面渲染 → ffmpeg 合成(带字幕条)→ B站元数据**
的完整竖向工作流, 目标是把「面试经验/求职干货」做成可持续更新的中文短视频频道。

## 架构(文字版)

```
industry ──chains/topic.py──▶ Topic ──chains/script.py──▶ Script(scenes[])
Script ──tts.py(edge-tts / 火山豆包双通道)──▶ 每场景 mp3 + cues(edge=词级时间轴, doubao=估算时间轴)
Script ──render.py(Jinja2+Playwright)──▶ cover.png + scene_XXX.png (1920x1080)
        ──assemble.py(build_scene_clips)──▶ 每场景 mp4(ffprobe 回填精确时长)
全局cues(tts.offset_cues)──subtitle_bars.py(Pillow)──▶ 每 cue 一张 1920x1080 透明字幕条 PNG
clips + bars ──assemble.py(finalize_video)──▶ final.mp4(concat + overlay + loudnorm)
Script + chapters ──chains/metadata.py──▶ BiliMeta(标题/简介/标签)
        ──▶ project.json(manifest); 可选 bilibili.py 上传 B站
```

编排层 `graph.py` 基于原生异步工作流引擎(`workflow_engine.py`, 零第三方依赖)把以上串成线性 8 节点图:
`gen_script → tts → render → clips → subs → finalize → metadata → manifest`,
`run_pipeline()` 是 async 便捷入口; `cli.py` 是 `ivb` 命令行(也是 `python -m interview_video`)。
LLM 调用完全基于原生 HTTP 客户端(`httpx`, 彻底移除 LangChain/LangGraph 依赖), 并由模型输出边界体系(`boundary.py`)提供强类型、结构与业务红线防护。

本机 ffmpeg **无 libass**(没有 subtitles 滤镜), 因此字幕不走 `subtitles` 滤镜,
而是 Pillow 生成「底部字幕条透明 PNG + overlay」方案, 兼容性更好。

## 快速开始

要求: Python ≥3.10, ffmpeg(>=5 即可, **不需要 libass**), 以及 Playwright 的 chromium。

```bash
# 1. 创建环境并安装(uv 或 venv 均可)
uv venv && uv pip install -e ".[dev]"
# 或: python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 2. 安装浏览器(画面渲染用)
.venv/bin/playwright install chromium

# 3. 配置(见下文 .env 说明)
cp .env.example .env

# 4. 环境自检: ffmpeg/ffprobe、chromium、中文字体、.env、LLM/豆包/B站配置
.venv/bin/ivb doctor            # --ping 额外发一次极小请求验证 LLM key/base_url

# 5. 无需 LLM key 的冒烟: 用现成脚本跑通全流程
.venv/bin/ivb generate --script-file examples/sample_script.json
```

`generate`/`series`/Web 任务启动时会先做廉价预检(ffmpeg/chromium/字体),
缺依赖直接报错, 不会先花掉 LLM/TTS 费用。Linux 需安装中文字体(如 `apt install fonts-noto-cjk`)
或设 `SUBTITLE_FONT_PATH`。

## 三条命令

```bash
# ① 生成选题清单(需 LLM), 存 output/topics-*.json
ivb topics --industry 护理 -n 8

# ② 跑完整流水线生成视频
#    未给 --out 时自动生成 output/<slug>-<YYYYmmdd-HHMM>/
#    --topic 给定时原样作为选题(不再随机生成选题), 选题存 topic.json
#    --script-file 模式全程不需要 LLM key(metadata 自动降级)
ivb generate --industry 互联网 \
             --topic "应届生自我介绍" \
             --voice zh-CN-XiaoxiaoNeural \
             --out output/self-intro
# 或直接复用现成脚本:
ivb generate --script-file examples/sample_script.json --out output/demo
# 中途失败(ffmpeg/渲染/TTS)后断点续跑: 复用该目录的 script.json 与 TTS 缓存, 不再付 LLM 费用
ivb generate --resume output/self-intro

# ③ 读 project.json 上传 B站(需开启 BILI_UPLOAD_ENABLED 并配置 cookie)
ivb upload output/demo --yes
```

## 系列长视频(多 agent)

一个主题 → 多 agent(原生状态图双循环)自动生成多集长视频(每集 10-20 分钟):

```bash
# 生成 3 集 × 每集约 15 分钟的系列(主题自拟)
ivb series "程序员面试通关" --episodes 3 --duration 15

# 中途失败(断网/限流/解析失败)后断点续跑: 已完成的集与章节不重跑、
# 已有成片的分集跳过媒体合成, 不再重复付 LLM/TTS 费用
ivb series "程序员面试通关" --resume output/程序员面试通关-series-20260923-1600
```

多 agent 角色与协作(`chains/series.py` + `series.py` 双循环图):

1. **策划**(`plan_series`): 主题 → 系列圣经(总标题/简介) + 分集规划(每集标题/要点/目标时长);
2. **分集编剧**(`write_episode_outline`): 每集 → 章节大纲(按 `SERIES_CHAPTER_SEC` 切章, 默认 150s/章);
3. **章节写手**(`write_chapter`): 逐章写场景与口播, **滚动摘要**(`roll_context`, 600 字窗口)
   把上一章摘要传给下一章——上下文在章节边界切分, 集与集之间全量重置, 避免超长上下文漂移;
4. **审稿**(`review_chapter`): 逐章审校(红线/口语化/与摘要一致性), 可用 `SERIES_REVIEW=false` 关闭。

**崩溃恢复**: 每章/每集完成即增量落盘到系列根目录的 `planning/`
(`series.json` / `epXX/outline.json` / `epXX/drafts/chXX.json` / `epXX/script.json`),
配 `--resume` 从磁盘重建图状态续跑; 失败的 LLM 调用由
`chains/_invoke.py` 统一兜底(结构化输出 → 本地重解析 → 带错误反馈的修复重问,
鉴权类错误立即失败, 所有回退留痕日志)。

每集脚本写完即走完整媒体流水线(配音→渲染→合成→字幕→元数据),
产物在 `output/<主题>-series-<时间戳>/ep01-*/ …`, 系列规划存 `series.json`;
作品库会把系列分集展开列出, 可逐集播放/编辑元数据/上传 B站。

生成完成后会 rich 打印产物路径(final.mp4 / cover.png / subtitles.srt)与 BiliMeta
(标题/标签); 若 `BILI_UPLOAD_ENABLED=true`, 会询问确认后上传(`--yes` 跳过)。

## 可观测性与成本

- **LLM 审计**: 每次调用的 token/耗时/输入输出规模追加到产物目录 `llm_calls.jsonl`
  (单集写在产物目录, 系列策划/编剧写在系列根目录); CLI 完成面板与 Web 完成提示直接显示调用次数与 token 合计;
- **日志**: 统一 INFO 控制台 + `output/ivb.log` 文件双通道, 回退/降级/重试全部留痕;
  命令失败只打印一行原因, 完整堆栈写入日志(设 `IVB_DEBUG=1` 直接打印);
- **Web 失败诊断**: 任务失败时堆栈与所在阶段落盘 `error-<jobid>.log`;
- **生成溯源**: `project.json` 的 `generation` 块记录模型/温度/TTS 通道/prompt 版本;
- **TTS 幂等缓存**: mp3 与 `scene_XXX.cues.json` 旁车(口播指纹)命中则跳过合成,
  重跑不重复付费; **备用模型**: 配 `LLM_FALLBACK_*` 三个变量即可主备自动切换。

## 生成质量评测

```bash
ivb eval --limit 3            # 跑 golden 用例(需 LLM_API_KEY): 契约+红线检查
ivb eval --no-judge           # 关掉 LLM-as-judge(钩子/口语化/连贯 1~5 打分)
ivb eval -o eval-report.json  # 报告落盘, 改 prompt 后对比基线
```

红线词表(`chains/redlines.py`)prompt 与评测共用: prompt 只是叮嘱, 扫描器才是闸门。

## Web 操作台

```bash
ivb web                 # 默认 http://127.0.0.1:8099(--host/--port 可改)
```

单页操作台(FastAPI + 原生 JS, 无构建步骤), 三段式:

1. **选题**: 输入行业 → 生成选题清单(LLM), 点击选题自动填入生成框;
2. **生成视频**: 模式可选**单集短视频**或**系列长视频(多agent)**(填系列主题、集数、每集分钟数),
   选 TTS 通道(edge-tts 免费 / 豆包火山)与音色 → 开始生成,
   8 个节点进度实时可见(选题→配音→渲染→合成→字幕→合成→元数据→落盘), 完成后显示 LLM token 用量;
   系列模式额外显示「第 x/N 集 · 阶段 · 第 y/M 章」;
   生成单飞(同时只跑一个任务, 避免 TTS 限流), 并发提交返回 409;
3. **作品库**: 封面墙(系列分集自动展开为独立条目) → 详情里可播放成片、
   编辑 B站标题/简介/标签(写回 project.json)、一键上传(未配 cookie 三件套会返回指引)。

生成中的长任务可点**取消任务**(或 `POST /api/jobs/{id}/cancel`), 不必杀进程;
任务失败时堆栈与所在阶段落盘到该任务产物目录的 `error-<jobid>.log`;
`GET /api/doctor` 返回与 `ivb doctor` 相同的环境自检结果。

> 本地工具, 默认只绑 127.0.0.1; 上传成功后 bvid 记录在项目目录 upload.json。

## .env 说明

环境变量名与字段名大写一致, **可复用 drivecast 的 `.env`**(同一套 OpenAI 兼容配置)。

> 注释请单独成行, 不要写在 `KEY=` 后面。空值与 `#` 开头的值会被视为未配置;
> `LLM_BASE_URL` 必须以 `http(s)://` 开头, 连不上时报错会直接给出所用 base_url。

```bash
# LLM(任意 OpenAI 兼容服务, 如 deepseek)
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# TTS / 内容 / 视频(均有默认值, 可省略)
TTS_PROVIDER=edge          # edge(免费) | doubao(火山引擎豆包 seed-tts-2.0)
TTS_VOICE=zh-CN-YunxiNeural
TTS_RATE=+6%
# doubao 通道凭证(TTS_PROVIDER=doubao 时必填, 火山引擎控制台→豆包语音→API Key):
# TTS_DOUBAO_API_KEY=
# TTS_DOUBAO_VOICE=zh_male_m191_uranus_bigtts   # 女声: zh_female_vv_uranus_bigtts
BRAND=面试充电站
TARGET_DURATION_SEC=100

# 系列长视频(均有默认值, 可省略)
SERIES_REVIEW=true        # 章节审稿 agent 开关
SERIES_CHAPTER_SEC=150    # 每章目标时长(秒), 决定上下文切分粒度

# B站上传(默认关闭)
BILI_UPLOAD_ENABLED=false
BILI_SESSDATA=
BILI_JCT=
BILI_BUVID3=
BILI_TID=36
```

## B站 cookie 获取步骤

`ivb upload` 需要 cookie 三件套(`SESSDATA` / `bili_jct` / `buvid3`), 缺一即拒绝上传并给出指引:

1. 桌面 Chrome 登录 [www.bilibili.com](https://www.bilibili.com), F12 打开开发者工具;
2. 进入 **Application(应用) → Cookies → https://www.bilibili.com**;
3. 复制 `SESSDATA`、`bili_jct`、`buvid3` 三个值填入 `.env`;
4. 设 `BILI_UPLOAD_ENABLED=true` 后重试。

> ⚠️ `SESSDATA` 等价于登录态, **不要泄露给任何人**, 也不要提交进仓库。

## AI 内容合规

- 生成的 B站简介**末尾固定含** `* 本视频由 AI 辅助生成` 声明(`bilibili.py` 上传时也会兜底补全);
- 脚本 system prompt 内置内容红线: 不编造具体薪资数字、不歧视、不承诺「包过」、不诋毁具体公司;
- **发布后请到创作中心为该稿件补勾「AI生成」标识**——简介声明不能替代平台的 AI 内容标注,
  二者都做才稳妥。

## 目录结构(产物示例)

```
output/<slug>-<时间戳>/
  script.json            # Script.model_dump_json(indent=2)
  scenes/scene_000.mp3   scene_000.cues.json   # 每场景配音 + 幂等缓存旁车(口播指纹+cues)
  clips/scene_000.mp4 ...                     # 每场景合成片段
  subs/bar_000.png ...   subtitles.srt        # 字幕条 PNG + 全局 srt
  cover.png
  final.mp4                                     # 成片
  project.json                                  # ProjectManifest(含 BiliMeta + generation 溯源)
  llm_calls.jsonl                               # LLM 调用审计(token/耗时)

# 系列模式额外(增量落盘, --resume 的依据):
output/<主题>-series-<时间戳>/
  series.json                              # 策划 agent 产出(plan 一出即落盘)
  planning/ep01/outline.json               # 分集编剧产出
  planning/ep01/drafts/ch01.json ...       # 章节写手/审稿后草稿(每章落盘)
  planning/ep01/script.json                # 该集脚本完成标记
  ep01-<slug>/ ...                         # 分集媒体产物(同上单集结构)
  error-<jobid>.log                        # Web 任务失败现场(如有)
```

## 测试与 CI

```bash
.venv/bin/pytest                  # 单元测试(不联网/不调真实 LLM/不起 chromium), 147 个
.venv/bin/pytest --run-integration  # 额外跑需要 ffmpeg/网络/chromium 的集成测试
make test                         # 等价入口(make lint / make integration / make eval)
.venv/bin/ruff check src tests    # 静态检查(CI 同款)
```

单元测试将 tts/render/assemble/subtitle_bars monkeypatch 成本地假实现;
依赖 ffmpeg 的素材合成测试打 `@pytest.mark.integration`, 缺二进制时自动 skip。
CI(GitHub Actions / Gitea 镜像, `.github/workflows/ci.yml`)在每次 push 跑
ruff + 全量离线单测; 依赖由 `uv.lock` 锁定(`uv sync --extra dev` 复现环境)。
变更记录见 `CHANGELOG.md`。

## 路线图

- ~~Web 操作台~~(已落地: `ivb web`)
- ~~系列断点续跑 / LLM 调用健壮化 / 可观测性 / evals~~(0.2.0 已落地)
- 双音色对话(面试官/求职者交替配音, 更强代入感)
- 真人数字人形象(替代 HTML 画面帧)
- 自动定时发布(接 B站 delay_time / cron 定点投递)
- LangGraph interrupt() 人工审稿介入(策划确认后再跑全集)
