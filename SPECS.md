# SPECS.md —— interview-video-bot 模块契约(唯一事实来源)

> 所有实现 agent 必读。`src/interview_video/models.py`、`config.py`、`llm.py`、
> `pyproject.toml`、`tests/conftest.py`、`examples/` 为**核心契约**, 改动需同步测试与本文档。
> 本文描述当前实现(0.3.0); 历史演进见 `CHANGELOG.md`。

## 0. 环境

- 项目根: 仓库根目录(`interview-video-bot/`)
- Python ≥3.10(本机 3.14, CI 跑 3.10/3.11/3.12), venv 在 `.venv/`, `pip install -e ".[dev]"` / `uv sync --extra dev`
- 运行时依赖: httpx / edge-tts / jinja2 / playwright / Pillow / typer / rich / pydantic(-settings) /
  bilibili-api-python / fastapi / uvicorn; **不依赖任何 LLM 编排框架**
- 命令一律用 `.venv/bin/python` / `.venv/bin/pytest` / `.venv/bin/ruff`
- 本机 ffmpeg **无 libass**(没有 subtitles 滤镜) → 字幕走 **Pillow 字幕条 PNG + overlay** 方案(见 §4/§5)
- 单元测试**不得联网、不得调真实 LLM/TTS、不得依赖 chromium**(用 `llm.fake_chat_model`、httpx
  `MockTransport`、monkeypatch 子进程); 依赖外部资源的测试打 `@pytest.mark.integration`
  (默认跳过, `--run-integration` 启用, 缺 ffmpeg 时 skip)
- pytest-asyncio `asyncio_mode=auto`, async 测试直接写 `async def test_...`

## 1. 架构与数据流

```
industry ──chains/topic.py──▶ Topic ──chains/script.py──▶ Script(scenes[])
Script ──tts.py(edge / doubao, 可选备用通道)──▶ 每场景 mp3 + cues(相对场景起点) + cues.json 缓存旁车
Script ──render.py──▶ cover.png + scene_XXX.png (Jinja2 HTML→Playwright 截图, 画布 settings.frame_size())
        ──assemble.py(build_scene_clips)──▶ 每场景 mp4 + ffprobe 精确 duration 回填
全局cues(tts.offset_cues) ──subtitle_bars.py──▶ 每 cue 一张画布尺寸透明 PNG(底部字幕条)
clips + bars ──assemble.py(finalize_video)──▶ 逐 clip 烧录字幕条 → concat 直拷 → [BGM 混音] → loudnorm → final.mp4
Script + chapters ──chains/metadata.py──▶ BiliMeta(标题/简介/标签)
        ──▶ project.json(manifest); 选题库标记已用; 可选 bilibili.py 上传(可定时发布)
```

编排: `graph.py`(单集线性图)与 `series.py`(系列双循环图)都建在 `workflow_engine.py` 上;
入口 `cli.py`(`ivb`)与 `web.py`(FastAPI 单页操作台)。

输出目录结构(`out_dir`):

```
<out_dir>/
  topic.json             # 选题(LLM 生成或 --topic 原样)
  script.json            # Script.model_dump_json(indent=2)
  scenes/scene_000.mp3   scene_000.cues.json(缓存旁车: 口播指纹+通道/音色键+cues)  scene_000.png ...
  clips/scene_000.mp4 ...
  subs/bar_000.png ...   subtitles.srt
  cover.png
  final.mp4
  project.json           # ProjectManifest(含 BiliMeta + generation 溯源)
  llm_calls.jsonl        # LLM 调用审计
  upload.json            # 上传记录(bvid / dtime), 幂等依据
  error-<jobid>.log      # Web 任务失败现场(如有)
```

`<OUT_ROOT>/` 下另有 `ivb.log`(5MB 轮转×3)、`topics-*.json`、`topics_backlog.json`(选题库)。

## 2. LLM 层 `llm.py` / `prompts.py` / `chains/` / `boundary/`

### 2.1 原生 HTTP 客户端 `llm.py`

```python
class ChatModel(Protocol):
    async def chat(self, messages, *, temperature=None, max_tokens=None) -> ChatResult: ...
@dataclass
class ChatResult: content: str; model: str | None; usage: dict[str, int]
class HTTPChatClient:        # httpx.AsyncClient, OpenAI 兼容 /chat/completions
    def __init__(self, *, model, api_key, base_url, temperature, timeout, max_tokens, max_retries,
                 fallbacks=None, audit=None, connect_timeout=10.0, sleep=None, rand=None)
def get_chat_model(settings, audit=None) -> HTTPChatClient   # 无 LLM_API_KEY 抛 RuntimeError
def fake_chat_model(responses: list[str]) -> FakeChatModel     # 测试替身, 记录 call_history
def is_fatal_error(exc) -> bool                                 # 唯一的致命错误分类
```

- 错误类型: `LLMAPIError`(含状态码/响应片段) ⊃ `AuthenticationError`(401) / `PermissionDeniedError`(403) /
  `NotFoundError`(404) / `RateLimitError`(429); `LLMConnectionError`(建连失败, 报错带 base_url);
  `LLMTruncatedError`(`finish_reason=length`, 提示调大 `LLM_MAX_TOKENS`)
- 重试: 429/5xx/网络错误按 `LLM_MAX_RETRIES` 重试, 优先 `Retry-After`(秒数或 HTTP-date, 上限 60s),
  否则指数退避+抖动(429 基数 2s); 每次重试 WARNING 留痕。401/403/404、截断、预算超限不重试、不切备用
- 备用模型: `LLM_FALLBACK_MODEL/BASE_URL/API_KEY` → 构造参数 `fallbacks`, 主模型可恢复错误耗尽后依次尝试
- 超时: 建连 `LLM_CONNECT_TIMEOUT` 与读 `LLM_TIMEOUT` 分开
- 响应体非 JSON/非对象/`choices` 异常统一为 `LLMAPIError`; `message.content` 为分片列表时拼接;
  响应 `model: null` 回落到配置模型名
- 审计: 未显式传 `audit` 时取当前工作流 `RunContext.audit`; 预算: 每次发请求前 `llm_audit.current_budget().acquire()`

### 2.2 Prompt 与链

- `prompts.ChatPromptTemplate.from_messages([(role, template), ...])` → `format_messages(**kw)`;
  变量**单次插值**(值里的 `{}` 不会被二次格式化)
- **中文 system prompt**, 角色=资深求职内容策划; 红线文本 `chains/redlines.RED_LINES` 写进所有生成 prompt:
  不编造具体薪资数字(用"因城市/公司差异大"类表述)、不歧视(年龄/性别/学历)、不承诺"包过/必上岸"、不诋毁具体公司
- `narration` 口语化, 每场景 65~110 字(约 15~25 秒); 全片时长向 `settings.target_duration_sec` 靠
- 场景结构: 第 1 镜 point 开场(含 hook), 中间 point/qa 混排, 最后一镜 kind=ending(关注引导)

`chains/_invoke.py` 统一调用策略:

```python
async def invoke_structured(prompt, llm, payload, schema, fallback_parser, *, stage="llm",
                            boundary_rules: BoundaryRules | None = None) -> Any
async def invoke_text(prompt, llm, payload, fallback_parser, *, stage="llm") -> Any
```

- 结构化: `boundary.enforce_boundary`(Schema → 结构/字数 → 红线, 违规时低温带反馈重问一次) →
  失败时本地 `fallback_parser` 重解析 → 仍失败抛错保留第一现场
- 非脚本 schema 调用方显式传 `BoundaryRules.non_script()`(缺省为完整脚本规则)
- 致命错误(`is_fatal_error`)、`LLMTruncatedError`、`LLMBudgetExceededError` 直接上抛, 不进回退/修复;
  客户端重试耗尽的传输层错误不再整轮文本回退
- 任何回退路径的结果都对象级重扫红线, 命中抛 `RedlineGateError`(ValueError 子类)

### 2.3 签名

```python
# chains/topic.py
async def generate_topics(llm, industry: str, n: int = 5, avoid: list[str] | None = None) -> list[Topic]
def parse_topics_json(text: str) -> list[Topic]
# chains/script.py
async def write_script(llm, topic: Topic, settings: Settings) -> Script
def parse_script_json(text: str) -> Script            # 回退解析(可测)
# chains/metadata.py
async def write_metadata(llm, script: Script, chapters: list[Chapter], settings: Settings) -> BiliMeta
def parse_metadata_json(text: str) -> BiliMeta
```

- metadata: 标题 ≤80 字、B站风格(数字/悬念/利益点, 不做标题党造谣); desc 含章节行
  `MM:SS 标题` + 结尾固定一行 `* 本视频由 AI 辅助生成`; tags 5~10 个, 必含行业词与"面试"
- `chains/__init__.py` 的 `extract_json_text / parse_json_lenient`: 容忍 ```json 围栏与前后废话

### 2.4 边界体系 `boundary/` 包

`errors`(`Violation` / `ViolationCategory` / `BoundaryViolationError`) · `rules`(`BoundaryRules`,
分镜 5~8、口播 65~110 字、header ≤12、bullet ≤14、末镜 ending、章节时长 clamp [60,600]) ·
`redlines`(`REDLINE_PATTERNS` **唯一红线规则源**, 生成闸门与 `ivb eval` 共用; `scan_redlines /
scan_text_redlines / scan_object_redlines`) · `parsing`(容错 JSON: 围栏、`<think>`、平衡括号、末尾逗号) ·
`structure`(`BoundaryChecker`) · `repair`(`enforce_boundary` / `enforce_boundary_sync` / `build_repair_prompt`)。
对外导入路径统一为 `interview_video.boundary`。

### 2.5 审计与预算 `llm_audit.py`

- `LLMCallAudit(out_dir)`: 每次调用追加一行到 `llm_calls.jsonl`(模型/耗时/token/输入输出规模/错误)
- `summarize_usage(out_dir)`: CLI 完成面板与 Web 完成提示的调用次数/token 合计
- `run_budget(settings)` 上下文 + `RunBudget`: `LLM_MAX_CALLS_PER_RUN` / `LLM_MAX_TOKENS_PER_RUN` 单次运行硬上限
  (系列整体共用一个计数器), 超限抛 `LLMBudgetExceededError`, 审稿/元数据降级不吞此错

## 3. TTS `tts.py`

```python
async def synthesize_text(text, mp3_path, voice, rate) -> list[Cue]            # edge
async def synthesize_text_doubao(text, mp3_path, settings) -> list[Cue]         # 豆包
async def synthesize_scenes(script: Script, scenes_dir: Path, settings: Settings) -> list[SceneMedia]
def merge_word_cues(words, max_chars=16, max_gap=0.6, max_dur=4.0) -> list[Cue]
def split_long_cues(cues, max_chars=16) -> list[Cue]
def estimate_cues(text, total_duration, max_chars=16) -> list[Cue]
def offset_cues(scene_media: list[SceneMedia]) -> list[Cue]   # 按 duration 累加平移成全局时间轴
def build_srt(cues: list[Cue]) -> str
```

- 通道 `TTS_PROVIDER=edge|doubao`(CLI `--tts` 临时覆盖):
  - edge: `edge_tts.Communicate(..., boundary="WordBoundary")`, 词级时间戳(100ns ticks / 1e7 = 秒)
  - doubao: seed-tts-2.0 单向流式(NDJSON), 无词级时间戳 → `estimate_cues` 按字数+标点权重分配 ffprobe 总时长;
    凭证 `TTS_DOUBAO_API_KEY`(新版)或 `TTS_DOUBAO_APP_KEY + TTS_DOUBAO_ACCESS_KEY`(旧版)
- 字幕切分: 每条 ≤`SUBTITLE_MAX_CHARS`(默认 16, 6~16), 超长优先在逗号/顿号处断开, 不切断英文单词;
  中文句读处优先断条, gap/时长超限断条
- `synthesize_scenes`: 顺序合成(避免限流); `scene_%03d.mp3` + `scene_%03d.cues.json` 旁车
  (口播指纹 + 通道/音色键)命中即跳过; duration 兜底 = 最后 cue end + 0.4s
- 重试: 每次 WARNING 留痕; 401/403、凭证无效、额度耗尽(并发限流除外)不重试(`TTSAuthError`)
- 回退: `TTS_FALLBACK_PROVIDER`(默认空=不回退)在主通道重试耗尽/鉴权失败时对该场景改用备用通道并告警;
  备用通道产物的缓存键不同, 下次重跑仍先试主通道

## 4. 画面渲染 `render.py` + `subtitle_bars.py` + `templates/`

```python
def render_project(script: Script, out_dir: Path, settings: Settings, industry: str = "") -> RenderResult
def cover_title_px(title: str, width: int = 1920, portrait: bool = False) -> int
def render_subtitle_bars(cues: list[Cue], subs_dir: Path, settings: Settings) -> list[SubtitleBar]
```

- 模板 `templates/`: `base.html / cover.html / scene.html / qa.html / ending.html`;
  kind→模板: point→scene, qa→qa, ending→ending; 每场景一帧 `scenes/scene_%03d.png` + `cover.png`
- 画幅: `VIDEO_ASPECT=16:9|9:16`(CLI `--aspect`), `settings.frame_size()` 返回实际画布
  (竖屏时把 `VIDEO_WIDTH/HEIGHT` 对调, 默认 1080x1920); 模板带竖屏版式 CSS
- 视觉: 深色渐变底(#0f172a→#1e293b)+点缀色(#38bdf8), 卡片式 bullets; 封面标题字号按长度自动缩放
  (108→56px, 最多 3 行截断); **底部留白给字幕条**; 中文字体栈 `"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif`
- 截图在帧间调用 `procs.checkpoint()`, 任务取消时抛出并在 finally 关闭浏览器
- 字幕条: 每 cue 一张画布尺寸透明 PNG, 字号 `SUBTITLE_FONTSIZE`, 距底 `SUBTITLE_BOTTOM`(竖屏自动上移避开平台操作栏),
  白字黑描边, `SUBTITLE_BOX` 时加半透明圆角底条; 超宽文本自动缩字号
- 字体探测: `SUBTITLE_FONT_PATH` 优先; 否则 macOS PingFang/STHeiti/Hiragino → Linux Noto CJK/文泉驿; 都找不到抛带指引的 RuntimeError

## 5. 合成 `assemble.py` + 可取消子进程 `procs.py`

```python
def ffprobe_duration(path: Path) -> float
def build_scene_clips(scene_media: list[SceneMedia], clips_dir: Path, settings: Settings) -> list[Path]
def finalize_video(clips: list[Path], bars: list[SubtitleBar], out_path: Path, settings: Settings) -> Path
def bgm_filter(total: float, settings: Settings, loudnorm: str) -> str
def build_chapters(script: Script, scene_media: list[SceneMedia]) -> list[Chapter]
```

- `build_scene_clips`: 静态图 + mp3 → H.264/yuv420p/AAC clip(scale+pad 到画布), 然后 ffprobe 回填精确 duration
- `finalize_video`: 有字幕条时**逐 clip 烧录**(cue 平移到场景本地时间, 跨场景的条按边界切开; x264 `-preset faster`)
  → concat demuxer `-c copy` → 视频直拷 + 音频处理: 设 `BGM_FILE`(CLI `--bgm`)时循环铺满、`BGM_VOLUME_DB` 压低、
  `BGM_DUCKING` sidechaincompress 闪避、结尾 3s 淡出后与人声混音; 最后 `loudnorm`(`LOUDNESS_I/TP/LRA`), 输出钉 44.1kHz
- ffmpeg 一律 `cwd=out_dir` + 相对路径; 单次超时随媒体时长伸缩(下限 300s); 非零退出抛 RuntimeError 附 stderr 末尾
- 子进程统一走 `procs.run`(替代 `subprocess.run`); 阻塞媒体步骤由 graph 以 `procs.run_in_thread` 执行:
  协程取消 → `CancelScope.cancel()` → terminate 子进程(3s 宽限后 kill) → 等工作线程退出后才抛 CancelledError;
  纯 Python 循环以 `procs.checkpoint()` 响应取消(抛 `ProcessCancelled`)
- 测试: 纯函数(concat list、overlay 滤镜、chapters、bgm_filter)不打标; 真实 ffmpeg 用 lavfi 素材打 integration

## 6. 编排与入口

### 6.1 工作流引擎 `workflow_engine.py`(纯标准库)

```python
engine = WorkflowEngine(StateType)
engine.add_node(name, fn)                 # fn(state) 或 fn(state, ctx), 同步/异步均可, 返回部分状态合并
engine.add_edge(START | name, name | END)
engine.add_conditional_edges(name, router)  # router(state) -> 下一节点名 / END
wf = engine.compile()                      # 编译期拓扑/死链检查, 失败抛 WorkflowGraphError
await wf.run(state, context=RunContext(audit=..., recursion_limit=..., extra={...}))
async for update in wf.stream(state, stream_mode="updates", context=...): ...  # {node: output}; "values" 产出全量 state
wf.run_sync(...) / wf.stream_sync(...)
current_run_context() -> RunContext | None   # 执行期间任意深度可取(如 LLM 客户端取审计器)
```

异常: `WorkflowRecursionError`(超步数上限)、`WorkflowExecutionError`(包装节点异常, 带节点名与状态快照)。

### 6.2 单集图 `graph.py`

```python
def build_graph(settings: Settings, stop_after: str | None = None) -> CompiledWorkflow
    # 线性: gen_script → tts → render → clips → subs → finalize → metadata → manifest; stop_after="script" 时 gen_script→END
async def run_script_stage(settings, out_dir, industry="互联网", topic_title=None, script_file=None) -> Script
async def run_pipeline(settings, out_dir, industry="互联网", topic_title=None, script_file=None,
                       script: Script | None = None) -> ProjectManifest
def resume_industry(out_dir: Path) -> str | None
```

- 节点内**通过模块属性调用**(`tts_mod.synthesize_scenes(...)` 等)便于 monkeypatch
- `run_pipeline` 先在线程中跑 `doctor.preflight`(ffmpeg/chromium/字体), 再以 `run_budget(settings)` +
  `RunContext(audit=LLMCallAudit(out_dir))` 执行
- gen_script: 预置 `script` 直通; 有 script_file 直接校验; 给了 topic_title 原样作为选题(优先匹配选题库条目);
  否则 LLM 生成选题(选题库标题作 avoid)取首条 → write_script; 落盘 topic.json/script.json
- metadata: 无 key 或普通失败降级 `meta=None` 并 warning(预算超限不降级)
- manifest: 写 project.json(含 generation: 模型/温度/prompt 版本/TTS 通道/画幅)、`subs/subtitles.srt`,
  并在选题库把对应选题标记已用

### 6.3 系列 `chains/series.py` + `series.py`

- 链: `plan_series` → `write_episode_outline` → `write_chapter` → `review_chapter`(`SERIES_REVIEW` 可关);
  `roll_context` 600 字滚动摘要(章节间传递, 集边界重置); `build_episode_script` 汇总(首镜 point / 末镜 ending 确定性修正)
- 图: `plan → episode_setup ⇄(条件) outline → chapter ⇄(条件) build_script → episode_setup`;
  `generate_series(settings, theme, episodes, episode_sec, on_event, run_dir=None, resume=False) -> (SeriesPlan, list[Script])`
- 增量落盘 `planning/`(series.json / epXX/outline.json / epXX/drafts/chXX.json / epXX/script.json), `resume=True` 从磁盘重建状态
- 成本护栏: 章数截断 `max(4, ceil(target/chapter_sec)+2)`、单章分镜 ≤30; 集数 1~12、每集 3~30 分钟
  (`estimate.validate_series_bounds`, CLI 与 Web 共用)

### 6.4 费用预估 `estimate.py` / 自检 `doctor.py` / 选题库 `backlog.py`

- `estimate_series(settings, episodes, duration_min) -> SeriesEstimate`: 按实际调用结构估 LLM 调用数/token
  (「最坏」×2 修复重问)与 TTS 字符数; 配了 `LLM_PRICE_*` / `TTS_PRICE_PER_10K_CHARS` 再给金额
- `doctor.run_doctor(settings) -> list[Check]`: ffmpeg/ffprobe、chromium、字体、.env 路径/输出目录可写、
  LLM/TTS/B站/BGM 配置; `ping_llm` 发一次极小请求; `preflight` 只查媒体硬依赖, 缺失抛 RuntimeError
- `backlog`: `<OUT_ROOT>/topics_backlog.json`, `add_topics`(去重入库) / `avoid_titles` / `pending` / `find` / `mark_used`,
  线程锁 + 原子写

### 6.5 CLI `cli.py`(typer, `ivb` + `python -m interview_video`)

```
ivb --version
ivb topics  [-i 行业] [-n 8] [--list [--all]] [--next]
ivb generate [-i 行业] [-t 选题] [--script-file F] [--tts edge|doubao] [--voice V] [-o DIR]
             [--resume DIR] [--stop-after script] [--aspect 16:9|9:16] [--bgm F] [--publish-at "YYYY-MM-DD HH:MM"] [-y]
ivb batch   [-i 行业] [-n 3] [--tts] [--voice] [--aspect] [--bgm]     # 单条失败继续, 有失败退出码 1
ivb series  [theme] [-e 3] [-d 15] [--resume ROOT] [--tts] [--voice] [--aspect] [--bgm] [-y] [--dry-run]
ivb upload  <project_dir> [-y] [--force] [--publish-at ...]
ivb eval    [-l 3] [--judge/--no-judge] [-o report.json]
ivb doctor  [--ping]
ivb web     [--host 127.0.0.1] [-p 8099]
```

- 顶层错误只打印一行原因, 堆栈进 `ivb.log`(`IVB_DEBUG=1` 直接打印); 失败时打印续跑命令
- `.env` 查找: `$IVB_ENV_FILE` → `./.env` → `~/.config/ivb/.env`

### 6.6 Web `web.py` + `static/index.html`

FastAPI 单页, 单飞任务(并发 409 带 `job_id`), 取消走 `procs` 链路(`cancelling` → `cancelled`, 子进程退出后才释放锁)。
主要接口: `/api/settings`、`/api/doctor`、`/api/topics`、`/api/topics/backlog`、`/api/jobs`(+`/active`、`/{id}`、`/{id}/cancel`)、
`/api/series`(+`/estimate`、`/{root}/resume`)、`/api/projects`(+`/{name}`、`/meta`、`/script`、`/regenerate`、`/upload`)。
安全: Host 白名单(`WEB_ALLOWED_HOSTS`)、写请求 Origin/Referer 同源; 非回环 `--host` 时写操作需 `X-IVB-Token`(`WEB_TOKEN`)。

### 6.7 bilibili.py(可选, 默认关)

```python
def build_credential(settings) -> Credential | None
def parse_publish_at(value: str, now: float | None = None) -> int     # 2 小时后 ~ 15 天内, 否则 ValueError
async def upload_project(project_dir, settings, *, force=False, dtime=None) -> str   # 返回 bvid, 写 upload.json
```

- 未启用/缺凭证抛带抓 cookie 指引的 RuntimeError; 已有 upload.json 且未 force 抛 `AlreadyUploadedError`(不发请求)
- desc 末尾兜底 AI 生成声明; 提示用户到创作中心补勾"AI生成"标识

## 7. 纪律

1. 行为变更必须同步测试; 用户可见变更同步 README / CHANGELOG(Unreleased) / `.env.example`
2. `config.Settings` 的每个字段都要在 `.env.example` 出现(可注释)
3. 不在单元测试里联网/调真实 LLM/TTS/起 chromium; ffmpeg 测试打 integration 且 skipif 无 ffmpeg
4. 提交前 `.venv/bin/pytest -q` 与 `.venv/bin/ruff check src tests` 全绿
5. 代码风格: 中文注释、类型标注、pathlib.Path; 与 models.py 的命名保持一致
