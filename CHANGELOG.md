# Changelog

本文件记录值得注意的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循语义化版本(单文件未发布前以 0.x 滚动)。

## [Unreleased]

### 新增

- `ivb doctor [--ping]` / `GET /api/doctor` 环境自检; generate/series/Web 任务启动前预检 ffmpeg/chromium/字体;
- `ivb generate --resume <dir>`: 复用 script.json + TTS 缓存续跑, 失败时打印续跑命令;
- 脚本审改闭环: `ivb generate --stop-after script` 只生成 script.json 并打印 `--resume` 续跑命令;
  Web 生成表单「只生成脚本」, 作品库详情内置脚本编辑器(保存 / 保存并重新生成),
  新增 `GET/PUT /api/projects/{name}/script`、`POST /api/projects/{name}/regenerate`; 作品库显示仅有脚本的草稿;
- `ivb --version`, `ivb topics -n`; CLI/Web 完成时显示 LLM 调用次数与 token 合计。
- Web 系列续跑: `GET /api/series` 列出系列完成度, `POST /api/series/{root}/resume` 同 `ivb series --resume`;
  作品库顶部「未完成系列」带续跑按钮;
- `TTS_FALLBACK_PROVIDER`(默认不回退): 主通道重试耗尽或鉴权失败时该场景改用备用通道合成(常用 doubao→edge), 日志告警;
- `GET /api/jobs/active`: 刷新页面后自动接回运行中任务进度; 单飞冲突的 409 响应体带 `job_id`。
- `LLM_CONNECT_TIMEOUT`(默认 10s): 建连超时与读超时(`LLM_TIMEOUT`)分开, 网络不通时快速失败重试。
- 系列费用预估与护栏: `ivb series` 开跑前打印预估(LLM 调用次数/token、TTS 字数; 配置 `LLM_PRICE_INPUT_PER_1K` /
  `LLM_PRICE_OUTPUT_PER_1K` / `TTS_PRICE_PER_10K_CHARS` 后显示金额)并要求确认, `--yes` 跳过、`--dry-run` 只看预估;
  Web 新增 `GET/POST /api/series/estimate`, 系列表单实时显示预估并在开始前确认;
  单次运行硬上限 `LLM_MAX_CALLS_PER_RUN` / `LLM_MAX_TOKENS_PER_RUN`(默认不限), 超限抛 `LLMBudgetExceededError`
  干净停下(不重试/不切备用/不进修复重问, 审稿与元数据降级不吞), 已落盘产物可续跑。

### 变更

- 系列参数边界收紧并统一(CLI 此前无上限, Web 允许 20 集 × 60 分钟): 集数 1~12、每集 3~30 分钟。

### 修复

- `.env.example` 行内注释被读成配置值(LLM_BASE_URL/BILI_JCT 等); 空值/`#...` 视为未配置, base_url 校验协议;
- LLM 连接失败(非法 URL/拒连)不再进入解析修复链, 直接报错并给出 base_url;
- LLM 审计从未生效(llm_calls.jsonl 为空): 客户端自动取工作流 config 中的 LLMCallAudit;
- 手填/点击的选题被忽略(重新生成选题并常取第 1 条); 现原样作为选题并存 topic.json;
- Web 节点进度落后一格; Web 徽标对空 key/豆包旧版凭证判断错误; 作品库/选题 innerHTML 未转义;
- 顶层错误只打印一行原因, 堆栈进日志; 字体探测增加 Linux Noto CJK / 文泉驿路径。
- 红线闸门可被绕过: 边界检查失败后 fallback_parser 救回/结构化桩本地重解析/纯文本路径的结果未重扫红线; 现统一对象级重扫, 命中抛 `RedlineGateError`(ValueError 子类);
- TTS 重试静默且对鉴权错误照样重试: 现每次重试 WARNING 记录通道/场景/次数/错误; 401/403、凭证无效、额度耗尽(并发限流除外)不重试直接报错;
  TTS 缓存旁车增加通道/音色键, 换音色或备用通道产物不再误命中(旧旁车兼容);
- 两套红线扫描器规则名不一致(评测用 `chains/redlines.py`「承诺结果」, 生成用 `boundary.py`「包过承诺」): 统一以 `boundary.REDLINE_PATTERNS` 为唯一来源。
- LLM 限流退避: 原固定 0.5/1s 无抖动且忽略 `Retry-After`; 现 429/5xx 优先遵循 `Retry-After`(秒数/HTTP-date, 上限 60s),
  否则指数退避+抖动(429 基数 2s, 上限 60s), 每次重试 WARNING 记录模型/状态码/次数/等待;
  客户端重试耗尽后结构化调用不再退回纯文本路径整轮重发(原先 429 时请求数翻倍);
- 成片音频采样率被 loudnorm 抬到 96kHz: 终版输出钉回 44.1kHz;
- LLM 输出截断被静默吞掉: `finish_reason=length` 现抛 `LLMTruncatedError`(提示调大 `LLM_MAX_TOKENS`),
  不重试、不切备用模型、不进修复重问(同参必然再截断), 审计记录标注 error。
- 长系列合成易触发 ffmpeg 超时(整片单条命令叠加数百个字幕条 overlay, 固定 600s): 现逐场景 clip 烧录字幕条(cue 平移到场景本地时间,
  跨场景的条按边界切开)后 concat 直拷, 最后仅音频 loudnorm; 单次 ffmpeg 超时随媒体时长伸缩(下限 300s), 重编码用 x264 `-preset faster`。
- Web 取消任务后 ffmpeg/chromium 仍在后台运行并写文件, 且单飞锁已释放可再开新任务: 媒体步骤改为可取消工作线程(`procs.run_in_thread`),
  取消时 terminate→(3s 宽限)→kill 子进程、渲染/字幕条在帧间中止并关闭浏览器, 子进程退出后才置 `cancelled` 并释放锁;
  取消中任务显示「正在取消」(`cancelling`); 系列任务取消后不再开下一集, 产物保留可续跑/重新生成。

## [0.2.0] - 2026-09-23

从「LLM 脚本流水线」向「可恢复的 agent 项目」补齐关键件: 可靠性 / 断点续跑 / 可观测性 / 评测 / 工程化。

### 新增

- **LLM 调用统一健壮层**(`chains/_invoke.py`): 结构化输出三级策略
  (structured → 本地重解析 → 带错误反馈的修复重问), 全失败抛错保留第一现场;
  鉴权类错误(401/403/模型不存在)立即失败不再浪费回退调用; 所有回退/降级留痕日志。
- **系列任务崩溃恢复**: 每章/每集完成即增量落盘(`planning/` 目录),
  `ivb series --resume <系列根目录>` 断点续跑(已完成集与章节不再重复调用 LLM,
  已有成片的分集按 project.json 跳过媒体合成)。
- **LLM 调用审计**: 每次调用的 token/耗时/输入输出规模追加 `llm_calls.jsonl`
  (CLI 与 Web 两条入口都注入), `llm_audit.summarize_usage` 汇总成本。
- **统一日志**: CLI 入口配置 INFO 控制台 + `output/ivb.log` 文件双通道。
- **生成质量评测**: `ivb eval` 跑 golden 用例集 → 确定性契约检查(分镜数/字数/结尾镜)
  + 红线词表扫描(`chains/redlines.py`, prompt 与评测共用) + 可选 LLM-as-judge 打分。
- **TTS 幂等缓存**: mp3 + `scene_XXX.cues.json` 旁车(口播文本指纹)命中则跳过合成,
  失败重试/断点续跑不重复付 TTS 费用。
- **Web 任务取消**: `POST /api/jobs/{id}/cancel` + 前端「取消任务」按钮,
  失控长任务不再只能杀 uvicorn 进程; 失败现场(堆栈+阶段)落盘 `error-<jobid>.log`。
- **备用模型**: `LLM_FALLBACK_MODEL/BASE_URL/API_KEY`, 主模型限流/宕机自动切换
  (`primary.with_fallbacks`, chains 层零改动)。
- **生成溯源**: project.json 新增 `generation` 块(模型/温度/prompt 版本/TTS 通道)。
- **CI + lint**: GitHub Actions(Gitea 兼容镜像可直接用)跑 ruff + 全量离线单测;
  Makefile 提供本地 test/lint 入口; 版本号单源(`__version__`, pyproject dynamic)。

### 修复

- **元数据失败炸掉整图**: final.mp4 已合成后 metadata 节点异常不再中断流水线,
  降级 meta=None 并留痕(此前几分钟媒体产物从可用性角度白做)。
- **模板花括号崩溃**: 章节标题/品牌含 `HashMap{K,V}` 类花括号时
  ChatPromptTemplate 二次格式化抛 KeyError(已端到端复现), brand/chapter_lines
  改为模板变量传入, 值不再被二次格式化。
- **失控大纲成本护栏**: 章数截断(`max(4, ceil(target_sec/chapter_sec)+2)`)、
  est_sec 夹 [60,600]、单章分镜数上限 30, 防一章 300 镜。
- **配置枚举校验**: `TTS_PROVIDER` 拼错直接配置报错, 不再静默回退 edge 通道;
  `TTS_DOUBAO_RATE` 用 Field 约束范围。
- B站 cookie 三件套与豆包旧版 Access Key 改 `SecretStr`。

## [0.1.3] - 2026-09-23

- 多 agent 系列长视频: 策划 → 分集编剧 → 章节写手 → 审稿双循环
  (LangGraph 集循环 × 章节循环, 集边界重置 + 600 字滚动摘要)。

## [0.1.2] - 2026-09-23

- Web 操作台(FastAPI 单页, `ivb web` 启动): 选题 → 生成进度 → 作品库/上传。

## [0.1.1] - 2026-09-23

- TTS 双通道: 新增火山豆包 seed-tts-2.0, 与 edge-tts 可切换。

## [0.1.0] - 2026-09-23

- 首个可用版本: LangGraph 编排 + edge-tts + Playwright 渲染 + ffmpeg 合成
  + B站元数据/可选上传; 110 个离线单测。
