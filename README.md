# interview-video-bot

AI 面试求职短视频流水线: 给一条行业(如「互联网」「护理」「汽修」), 自动产出
**选题 → 口播脚本 → TTS 配音 → HTML 画面渲染 → ffmpeg 合成(带字幕条)→ B站元数据**
的完整竖向工作流, 目标是把「面试经验/求职干货」做成可持续更新的中文短视频频道。

## 架构(文字版)

```
industry ──chains/topic.py──▶ Topic ──chains/script.py──▶ Script(scenes[])
Script ──tts.py(edge-tts / 火山豆包双通道)──▶ 每场景 mp3 + cues(edge=词级时间轴, doubao=估算时间轴)
Script ──render.py(Jinja2+Playwright)──▶ cover.png + scene_XXX.png (1920x1080; VIDEO_ASPECT=9:16 时 1080x1920)
        ──assemble.py(build_scene_clips)──▶ 每场景 mp4(ffprobe 回填精确时长)
全局cues(tts.offset_cues)──subtitle_bars.py(Pillow)──▶ 每 cue 一张与画幅同尺寸的透明字幕条 PNG
clips + bars ──assemble.py(finalize_video)──▶ final.mp4(逐 clip overlay 字幕条 → concat 直拷 → [可选 BGM 混音] → loudnorm)
Script + chapters ──chains/metadata.py──▶ BiliMeta(标题/简介/标签)
        ──▶ project.json(manifest); 可选 bilibili.py 上传 B站
```

编排层 `graph.py` 基于原生异步工作流引擎(`workflow_engine.py`, 零第三方依赖)把以上串成线性 8 节点图:
`gen_script → tts → render → clips → subs → finalize → metadata → manifest`,
`run_pipeline()` 是 async 便捷入口; `cli.py` 是 `ivb` 命令行(也是 `python -m interview_video`)。
LLM 调用基于原生 HTTP 客户端(`llm.py`, `httpx` 直连任意 OpenAI 兼容接口, 不依赖 LLM 编排框架), 并由模型输出边界体系(`boundary/` 包: errors / rules / redlines / parsing / structure / repair)提供强类型、结构与业务红线防护。
媒体步骤(ffmpeg/ffprobe/Playwright/Pillow)在可取消工作线程(`procs.py`)中执行, 任务取消时子进程会被终止。
模块契约与各层接口见 `SPECS.md`。

本机 ffmpeg **无 libass**(没有 subtitles 滤镜), 因此字幕不走 `subtitles` 滤镜,
而是 Pillow 生成「底部字幕条透明 PNG + overlay」方案, 兼容性更好。

## 快速开始

前置依赖(需自行安装):

- Python ≥3.10 与 [uv](https://docs.astral.sh/uv/);
- ffmpeg/ffprobe(>=5 即可, **不需要 libass**): macOS `brew install ffmpeg`, Debian/Ubuntu `apt install ffmpeg`;
- 中文字体(字幕/画面用): macOS 自带; Linux 如 `apt install fonts-noto-cjk`, 或设 `SUBTITLE_FONT_PATH`。

```bash
# 1+2. 安装依赖(按 uv.lock 锁定版本, 含 dev) + 画面渲染用的 chromium
make install
# 等价于: uv sync --extra dev && uv run playwright install chromium
# 不用 uv 时: python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/playwright install chromium

# 3. 配置(见下文 .env 说明): 生成 ~/.config/ivb/.env(权限 0600, OUT_ROOT=~/ivb-output 绝对路径),
#    然后编辑填写 LLM_API_KEY 等; 之后在任意目录运行 ivb 都读这份配置
.venv/bin/ivb init
# 或仅在本项目目录使用: cp .env.example .env(当前目录的 ./.env 优先于用户级配置)

# 4. 环境自检: ffmpeg/ffprobe、chromium、中文字体、.env、LLM/豆包/B站配置
.venv/bin/ivb doctor            # --ping 额外发一次极小请求验证 LLM key/base_url

# 5. 无需 LLM key 的冒烟: 用内置示例脚本跑通全流程(Web 端对应「用示例脚本」勾选框)
.venv/bin/ivb generate --sample
```

未配置 `LLM_API_KEY` 时, `topics`/`generate`(不带 `--sample`/`--script-file`/`--resume`)/`batch`/`series`
在创建输出目录之前就退出(码 2), 并提示实际读取的 .env 路径; Web 端同样在提交任务时直接返回原因。
`.env` 写错(如 `TTS_PROVIDER=azure`)时逐字段列出变量名与原因, `ivb doctor` 仍会照常检查 ffmpeg/浏览器/字体。
`TTS_PROVIDER` 不区分大小写; `LLM_BASE_URL` 省略协议时自动补 `https://`。

`generate`/`series`/Web 任务启动时会先做廉价预检(ffmpeg/chromium/字体),
缺依赖直接报错, 不会先花掉 LLM/TTS 费用。Linux 需安装中文字体(如 `apt install fonts-noto-cjk`)
或设 `SUBTITLE_FONT_PATH`。

## 常用命令

| 命令 | 作用 |
|---|---|
| `ivb topics` | 生成选题 / 查看选题库(`--list [--all]`、`--next`) |
| `ivb generate` | 单集完整流水线(`--topic`/`--sample`/`--script-file`/`--resume`/`--regen-meta`/`--stop-after script`/`--tts`/`--voice`/`--aspect`/`--bgm`/`--publish-at`/`-y`) |
| `ivb batch` | 按选题库顺序批量生成 N 条 |
| `ivb series` | 多 agent 系列长视频(`--episodes`/`--duration`/`--resume`/`--dry-run`/`-y`) |
| `ivb upload` | 上传 B站(`-y`/`--force`/`--publish-at`) |
| `ivb eval` | 生成质量评测 |
| `ivb doctor` | 环境自检(`--ping`) |
| `ivb web` | Web 操作台(`--host`/`--port`) |
| `ivb --version` | 显示版本 |

各命令完整参数见 `ivb <命令> --help`。

```bash
# ① 生成选题清单(需 LLM), 存 output/topics-*.json, 并去重入选题库 output/topics_backlog.json
#    选题库已有/用过的标题会作为 avoid 传给 LLM, 避免反复出同一题
ivb topics --industry 护理 -n 8
ivb topics --list [--industry 护理] [--all]   # 查看选题库(默认只列未用; --all 含已用)
ivb topics --next --industry 护理             # 取下一条未用选题并打印对应 generate 命令
# 视频生成完成后, 对应选题自动在选题库标记为已用

# ② 跑完整流水线生成视频
#    未给 --out 时自动生成 output/<slug>-<YYYYmmdd-HHMM>/
#    --topic 给定时原样作为选题(不再随机生成选题), 选题存 topic.json
#    --script-file 模式全程不需要 LLM key(metadata 自动降级)
ivb generate --industry 互联网 \
             --topic "应届生自我介绍" \
             --voice zh-CN-XiaoxiaoNeural \
             --out output/self-intro
# 临时切豆包: --voice 作用于当前通道(豆包音色写入 TTS_DOUBAO_VOICE), series 同样支持
ivb generate --topic "应届生自我介绍" --tts doubao --voice zh_female_vv_uranus_bigtts
# 或直接复用现成脚本:
ivb generate --script-file examples/sample_script.json --out output/demo
# 中途失败(ffmpeg/渲染/TTS)后断点续跑: 复用该目录的 script.json 与 TTS 缓存, 不再付 LLM 费用
ivb generate --resume output/self-intro
# 先审稿再出片: 只生成 script.json(停在配音前), 手改后用 --resume 续跑
ivb generate --topic "应届生自我介绍" --stop-after script --out output/self-intro
ivb generate --resume output/self-intro

# ③ 读 project.json 上传 B站(需开启 BILI_UPLOAD_ENABLED 并配置 cookie)
#    成功后 bvid 写入 upload.json; 已上传过的项目默认拒绝重复投稿, 确需重投加 --force
ivb upload output/demo --yes
# 定时发布: 本地时间, 需在 2 小时后 ~ 15 天内(B站 dtime 限制), 不合法直接报错不上传
ivb upload output/demo --yes --publish-at "2026-10-02 20:00"
```

### 竖屏 / BGM / 批量

```bash
# 9:16 竖屏(1080x1920): 版式、字幕位置(避开底部操作栏)、封面随之调整; 默认仍是 16:9
ivb generate --topic "应届生自我介绍" --aspect 9:16
# 背景音乐: 循环铺满、低音量(BGM_VOLUME_DB)、口播时自动压低(BGM_DUCKING)、结尾淡出; 不设则不加
ivb generate --topic "应届生自我介绍" --bgm ~/music/lofi.mp3
# 批量: 从选题库按顺序取 N 条未用选题逐个生成, 单条失败不影响后续, 最后打印汇总表
ivb batch --industry 护理 -n 3 [--aspect 9:16] [--bgm bgm.mp3]
# 无人值守: 选题不足 5 条先自动补充, 成片后上传并按每日 12:00/20:00 排定时发布
ivb batch -i 护理 -n 2 --refill 5 --upload --slots 12:00,20:00 [--start 2026-10-05]
```

批量的失败处理:

- 失败的选题会在选题库里记次数(`attempts`)和目录; 下次 `ivb batch` 自动续跑旧目录(复用 script.json, 不再付脚本费用);
  连续失败 3 次后默认跳过, 加 `--retry-failed` 强制重试。
- TTS 鉴权/额度、LLM 鉴权/预算超限、B站 cookie 失效这类「每条都会失败」的错误立即中止整批, 且不计入选题失败次数。
- `--slots` 从 2 小时后到 15 天内挑下一个空闲时段, 已占用的时段读 `OUTPUT/*/upload.json` 的 `dtime`;
  窗口内排满时该条只生成不上传, 之后可 `ivb upload <目录> --publish-at ...` 补传。
- 选题库读写带跨进程文件锁; 同一时刻只允许一个 `ivb batch` 运行, cron 重叠触发时后来者直接退出。

crontab 示例(每天 09:30 生成 2 条并排期发布; cron 环境 PATH 很少, 用绝对路径并 cd 到项目目录以读取 .env):

```cron
30 9 * * * cd /path/to/interview-video-bot && /path/to/interview-video-bot/.venv/bin/ivb batch -i 护理 -n 2 --refill 5 --upload --slots 12:00,20:00 >> output/cron.log 2>&1
```

封面标题按长度自动缩放字号(108px → 最小 56px, 最多 3 行), 长标题不再溢出;
字幕每条最多 `SUBTITLE_MAX_CHARS`(默认 16)字, 超长句优先在逗号/顿号处断开, 时间按字数比例分配。
Web 操作台同样支持画幅选择、选题库浏览与定时发布。

## 系列长视频(多 agent)

一个主题 → 多 agent(原生状态图双循环)自动生成多集长视频(每集 10-20 分钟):

```bash
# 生成 3 集 × 每集约 15 分钟的系列(主题自拟)
ivb series "程序员面试通关" --episodes 3 --duration 15

# 开跑前会打印费用预估(LLM 调用次数/token、TTS 字数, 配了单价再显示金额)并要求确认;
# --dry-run 只看预估不调用任何服务, --yes 跳过确认(脚本/CI 用)
ivb series "程序员面试通关" --episodes 3 --duration 15 --dry-run

# 中途失败(断网/限流/解析失败)后断点续跑: 已完成的集与章节不重跑、
# 已有成片的分集跳过媒体合成, 不再重复付 LLM/TTS 费用
ivb series --resume output/程序员面试通关-series-20260923-1600   # 主题从 series.json 读取, 可省略
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

**成本护栏**: 集数限 1~12、每集 3~30 分钟(CLI 与 Web 同一套边界, 越界直接拒绝);
预估按实际调用结构计算(策划 1 次 + 每集编剧 1 次 + 每章写手/审稿各 1 次 + 每集元数据 1 次,
「最坏」按每次都触发一次修复重问 ×2), token 按中文字符粗估, 仅作量级参考。
另可在 `.env` 设单次运行硬上限 `LLM_MAX_CALLS_PER_RUN` / `LLM_MAX_TOKENS_PER_RUN`(默认不限):
整个系列(含各集元数据)共用一个计数器, 超限时在发出下一次请求前抛 `LLMBudgetExceededError` 停下,
不重试也不切备用模型; 已完成的集/章已落盘, 调大上限后 `--resume` 续跑(续跑重新计数)。

每集脚本写完即走完整媒体流水线(配音→渲染→合成→字幕→元数据),
产物在 `output/<主题>-series-<时间戳>/ep01-*/ …`, 系列规划存 `series.json`;
作品库会把系列分集展开列出, 可逐集播放/编辑元数据/上传 B站。

生成完成后会 rich 打印产物路径(final.mp4 / cover.png / subtitles.srt)与 BiliMeta
(标题/标签); 若 `BILI_UPLOAD_ENABLED=true`, 会询问确认后上传(`--yes` 跳过)。

## 可观测性与成本

- **LLM 审计**: 每次调用的 token/耗时/输入输出规模追加到产物目录 `llm_calls.jsonl`
  (单集写在产物目录, 系列策划/编剧写在系列根目录); CLI 完成面板与 Web 完成提示直接显示调用次数与 token 合计;
- **日志**: 统一 INFO 控制台 + `<OUT_ROOT>/ivb.log` 文件双通道(5MB 轮转保留 3 份), 回退/降级/重试全部留痕,
  httpx/httpcore/asyncio/websockets/PIL/playwright 的 DEBUG 噪声已压到 WARNING;
  命令失败只打印一行原因, 完整堆栈写入日志(设 `IVB_DEBUG=1` 直接打印);
- **Web 失败诊断**: 任务失败时堆栈与所在阶段落盘 `error-<jobid>.log`;
- **生成溯源**: `project.json` 的 `generation` 块记录模型/温度/TTS 通道/prompt 版本;
- **TTS 幂等缓存**: mp3 与 `scene_XXX.cues.json` 旁车(口播指纹)命中则跳过合成,
  重跑不重复付费(缓存键含通道/音色, 换音色或备用通道产物不会误命中);
  **TTS 重试与回退**: 每次重试 WARNING 留痕; 401/403/凭证无效/额度耗尽不重试直接报错;
  配 `TTS_FALLBACK_PROVIDER=edge` 后豆包失败的场景自动改用 edge-tts 合成并告警;
  **备用模型**: 配 `LLM_FALLBACK_*` 三个变量即可主备自动切换;
  **LLM 限流与超时**: 429/5xx 优先遵循 `Retry-After`(秒数或 HTTP-date, 单次上限 60s), 否则指数退避+抖动
  (429 基数 2s), 每次重试 WARNING 记录状态码/次数/等待; 建连超时 `LLM_CONNECT_TIMEOUT`(默认 10s)与读超时
  `LLM_TIMEOUT` 分开; 输出触顶 `max_tokens`(finish_reason=length)直接报错提示调大 `LLM_MAX_TOKENS`,
  不再把截断 JSON 送去修复重问。

## 生成质量评测

```bash
ivb eval --limit 3            # 跑 golden 用例(需 LLM_API_KEY): 契约+红线检查
ivb eval --no-judge           # 关掉 LLM-as-judge(钩子/口语化/连贯 1~5 打分)
ivb eval -o eval-report.json  # 报告落盘, 改 prompt 后对比基线
```

红线规则唯一来源为 `boundary.REDLINE_PATTERNS`(`chains/redlines.py` 仅保留 prompt 文本并复用该扫描器), 生成闸门与评测共用: prompt 只是叮嘱, 扫描器才是闸门。任何回退解析路径的结果都会重扫红线, 命中即报错。

## Web 操作台

```bash
ivb web                 # 默认 http://127.0.0.1:8099(--host/--port 可改)
```

安全: 所有请求校验 Host 头(回环名 / `--host` / `WEB_ALLOWED_HOSTS`, 防 DNS 重绑定);
POST/PUT/DELETE/PATCH 的 Origin(无则 Referer)必须与 Host 同源, 跨站表单/脚本提交返回 403。
`--host` 为非回环地址(如 `0.0.0.0`)时写操作还需令牌 `X-IVB-Token`: 未设置 `WEB_TOKEN` 则启动时生成,
终端打印带 `#token=` 的链接, 页面读取后存入 sessionStorage 并自动附带。

单页操作台(FastAPI + 原生 JS, 无构建步骤), 三段式:

1. **选题**: 输入行业 → 生成选题清单(LLM), 点击选题自动填入生成框;
2. **生成视频**: 模式可选**单集短视频**或**系列长视频(多agent)**(填系列主题、集数、每集分钟数),
   选 TTS 通道(edge-tts 免费 / 豆包火山)与音色 → 开始生成,
   8 个节点进度实时可见(选题→配音→渲染→合成→字幕→合成→元数据→落盘), 完成后显示 LLM token 用量;
   系列模式在表单下实时显示费用预估(`GET/POST /api/series/estimate`), 点开始时弹窗确认预估后才提交,
   运行中额外显示「第 x/N 集 · 阶段 · 第 y/M 章」;
   生成单飞(同时只跑一个任务, 避免 TTS 限流), 并发提交返回 409(响应体带运行中任务的 `job_id`);
   刷新页面后自动通过 `GET /api/jobs/active` 接回运行中任务的进度;
   勾选「只生成脚本」则停在配音前(同 `--stop-after script`), 作品库出现「仅脚本」草稿;
3. **作品库**: 封面墙(系列分集自动展开为独立条目) → 详情里可播放成片、
   编辑 B站标题/简介/标签(写回 project.json)、一键上传(未配 cookie 三件套会返回指引);
   按创建时间倒序(系列分集成组); 失败的任务(即使脚本前就失败)标「生成失败」并带错误日志链接,
   project.json/script.json 损坏的目录标「文件损坏」而不拖垮整个作品库;
   详情里还有**脚本编辑器**(script.json 格式化 JSON): 「保存脚本」/「保存并重新生成」,
   重新生成复用原目录与改后的脚本(同 `--resume`, 未改动场景复用配音缓存), 占用同一把单飞锁。
   未全部出片的系列列在作品库顶部「未完成系列」, 点**续跑**同 `ivb series --resume <root>`
   (沿用磁盘上的主题/集数/时长, 已完成的集/章不重跑, 已出片分集跳过)。

脚本相关接口: `GET/PUT /api/projects/{name}/script`(PUT 按 Script 模型校验, 不合法返回 422 与字段级原因)、
`POST /api/projects/{name}/regenerate`(body 可选 `{provider, voice}`, 返回 job_id)。
系列续跑接口: `GET /api/series`(系列根目录及完成度)、`POST /api/series/{root}/resume`(body 可选 `{provider, voice}`);
`GET /api/jobs/active` 返回运行中任务或 `null`。

生成中的长任务可点**取消任务**(或 `POST /api/jobs/{id}/cancel`), 不必杀进程:
后台 ffmpeg/ffprobe 会被终止(SIGTERM, 3s 不退则 SIGKILL), 渲染中的 chromium 在帧间关闭, 系列任务不再开下一集;
子进程全部退出前任务显示「正在取消」并继续占用单飞锁, 之后状态变为 `cancelled`。
已完成的产物保留: 单集可在作品库「重新生成」, 系列可在「未完成系列」续跑;
任务失败时堆栈与所在阶段落盘到该任务产物目录的 `error-<jobid>.log`;
`GET /api/doctor` 返回与 `ivb doctor` 相同的环境自检结果。

> 本地工具, 默认只绑 127.0.0.1; 上传成功后 bvid 记录在项目目录 upload.json(CLI 与 Web 共用),
> 已有 bvid 时 `POST /api/projects/{name}/upload` 返回 409(`already_uploaded`), 页面确认后带 `{"force": true}` 重投。

## .env 说明

环境变量名与字段名大写一致, **可复用 drivecast 的 `.env`**(同一套 OpenAI 兼容配置)。
完整清单(含全部可选项与说明)以 `.env.example` 为准, 下面是常用项速览(注释行仅作说明, 实际 `.env` 里注释须单独成行)。

`.env` 按以下顺序查找, 首个存在者生效: `$IVB_ENV_FILE` → 当前目录 `./.env` → `~/.config/ivb/.env`
(不在项目目录运行时放用户级配置即可, `ivb init` 会生成它; 用户级目录可用 `IVB_CONFIG_DIR` 覆盖)。
`OUT_ROOT` 默认 `output`(相对当前目录), 可写绝对路径或 `~/...`; 若读取的是用户级 `.env`,
其中的相对 `OUT_ROOT` 按主目录解析(如 `OUT_ROOT=videos` → `~/videos`), 不随当前目录变化。
实际读取的 `.env` 与输出目录会在 `ivb doctor` 以及 generate/series/web 启动时打印。

> 注释请单独成行, 不要写在 `KEY=` 后面。空值与 `#` 开头的值会被视为未配置;
> `LLM_BASE_URL` 必须以 `http(s)://` 开头, 连不上时报错会直接给出所用 base_url。

```bash
# LLM(任意 OpenAI 兼容服务, 如 deepseek)
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
# LLM_TEMPERATURE=0.7
# LLM_TIMEOUT=300            # 读超时(秒)
# LLM_CONNECT_TIMEOUT=10     # 建连超时(秒)
# LLM_MAX_TOKENS=8192        # 输出被截断(finish_reason=length)时调大
# LLM_MAX_RETRIES=2          # 客户端层网络/限流重试次数
# LLM_FALLBACK_MODEL=        # 备用模型(可选), BASE_URL/API_KEY 不填则沿用主模型
# LLM_FALLBACK_BASE_URL=
# LLM_FALLBACK_API_KEY=
# LLM_MAX_CALLS_PER_RUN=     # 单次运行 LLM 调用硬上限(默认不限), 超限停下可续跑
# LLM_MAX_TOKENS_PER_RUN=    # 单次运行 token 硬上限(默认不限)
# LLM_PRICE_INPUT_PER_1K=    # 费用预估单价(可选): 输入/输出每千 token、TTS 每万字
# LLM_PRICE_OUTPUT_PER_1K=
# TTS_PRICE_PER_10K_CHARS=

# TTS(均有默认值, 可省略)
TTS_PROVIDER=edge          # edge(免费) | doubao(火山引擎豆包 seed-tts-2.0); 命令行 --tts 临时覆盖
TTS_VOICE=zh-CN-YunxiNeural
TTS_RATE=+6%
# TTS_FALLBACK_PROVIDER=edge  # 主通道重试耗尽/鉴权失败时该场景改用备用通道(默认不回退)
# doubao 通道凭证(TTS_PROVIDER=doubao 时必填, 火山引擎控制台→豆包语音→API Key):
# TTS_DOUBAO_API_KEY=
# TTS_DOUBAO_APP_KEY=        # 旧版控制台: App ID + Access Key 一对, 替代 API Key
# TTS_DOUBAO_ACCESS_KEY=
# TTS_DOUBAO_VOICE=zh_male_m191_uranus_bigtts   # 女声: zh_female_vv_uranus_bigtts
# TTS_DOUBAO_RATE=0          # speech_rate [-50,100]
# TTS_DOUBAO_RESOURCE_ID=seed-tts-2.0           # 1.0 音色改 seed-tts-1.0
# 高级项(一般不改): TTS_DOUBAO_HTTP_URL / TTS_DOUBAO_UID / TTS_DOUBAO_FORMAT / TTS_DOUBAO_SAMPLE_RATE / TTS_DOUBAO_RECV_TIMEOUT

# 内容 / 画面 / 音频(均有默认值, 可省略)
BRAND=面试充电站
TARGET_DURATION_SEC=100
VIDEO_ASPECT=16:9          # 16:9 | 9:16(竖屏); 命令行 --aspect 临时覆盖
# VIDEO_WIDTH=1920 / VIDEO_HEIGHT=1080 / FPS=30   # 横屏基准分辨率(竖屏自动对调)与帧率
# BGM_FILE=~/music/bgm.mp3 # 背景音乐(可选), 命令行 --bgm 临时覆盖
# BGM_VOLUME_DB=-18        # BGM 音量(dB)
# BGM_DUCKING=true         # 口播时压低 BGM
# SUBTITLE_MAX_CHARS=16    # 每条字幕最多字数(6-16)
# SUBTITLE_FONTSIZE=52 / SUBTITLE_BOTTOM=96 / SUBTITLE_BOX=true   # 字幕条字号/距底像素/半透明底条
# SUBTITLE_FONT_PATH=      # 自动探测不到中文字体时指定
# LOUDNESS_I=-16 / LOUDNESS_TP=-1.5 / LOUDNESS_LRA=11             # 成片 loudnorm 参数

# 系列长视频(均有默认值, 可省略)
SERIES_REVIEW=true        # 章节审稿 agent 开关
SERIES_CHAPTER_SEC=150    # 每章目标时长(秒), 决定上下文切分粒度

# B站上传(默认关闭)
BILI_UPLOAD_ENABLED=false
BILI_SESSDATA=
BILI_JCT=
BILI_BUVID3=
BILI_TID=209              # 必须是子分区号(父分区如 36 会被拒); 209=知识·职业职场, 其它见 bilibili.py

# 输出目录 / Web 操作台(可选)
# OUT_ROOT=output          # 可写绝对路径或 ~/...
# WEB_TOKEN=               # 非回环 --host 时写操作令牌(不设则启动时生成)
# WEB_ALLOWED_HOSTS=       # 额外允许的 Host 头(逗号分隔)
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
.venv/bin/pytest -q               # 单元测试(不联网/不调真实 LLM/TTS/不起 chromium)
.venv/bin/pytest --run-integration  # 额外跑需要 ffmpeg/网络/chromium 的集成测试
make test                         # 等价入口(make lint / make integration / make eval)
.venv/bin/ruff check src tests    # 静态检查(CI 同款)
```

单元测试将 tts/render/assemble/subtitle_bars monkeypatch 成本地假实现;
依赖 ffmpeg 的素材合成测试打 `@pytest.mark.integration`, 缺二进制时自动 skip。
CI(GitHub Actions / Gitea 镜像, `.github/workflows/ci.yml`)在每次 push 跑
ruff + Python 3.10/3.11/3.12 全量离线单测, 另有安装 ffmpeg 的集成测试任务; 依赖由 `uv.lock` 锁定(`uv sync --extra dev` 复现环境)。
变更记录见 `CHANGELOG.md`, 模块契约见 `SPECS.md`。

## 路线图

- ~~Web 操作台~~(已落地: `ivb web`)
- ~~系列断点续跑 / LLM 调用健壮化 / 可观测性 / evals~~(0.2.0 已落地)
- ~~定时发布 / 竖屏 / BGM / 选题库与批量生产~~(0.3.0 已落地: `--publish-at` / `--aspect` / `--bgm` / `ivb batch`)
- 双音色对话(面试官/求职者交替配音, 更强代入感)
- 真人数字人形象(替代 HTML 画面帧)
- ~~无人值守定时投递~~(已落地: `ivb batch --upload --slots` + crontab)
- 系列策划人工审稿介入(策划确认后再跑全集)
