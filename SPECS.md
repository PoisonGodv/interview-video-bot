# SPECS.md —— interview-video-bot 模块契约(唯一事实来源)

> 所有实现 agent 必读。`src/interview_video/models.py`、`config.py`、`llm.py`、
> `pyproject.toml`、`tests/conftest.py`、`examples/` 为**只读契约**;
> 发现契约问题时在最终报告中说明, **不要自行修改**。

## 0. 环境

- 项目根: `/Users/liningwei/Selfproj/interview-video-bot`
- Python 3.14, venv 在 `.venv/`, 已 `pip install -e ".[dev]"` 装好全部依赖
- 命令一律用 `.venv/bin/python` / `.venv/bin/pytest`; **禁止 pip install 任何东西**
- 本机 ffmpeg 9.0.1 **无 libass**(没有 subtitles 滤镜) → 字幕走 **Pillow 字幕条 PNG + overlay** 方案(见 §4/§5)
- 单元测试**不得联网、不得调真实 LLM、不得依赖 chromium**; 依赖外部资源的测试打 `@pytest.mark.integration`(默认跳过, `--run-integration` 启用)
- pytest-asyncio `asyncio_mode=auto`, async 测试直接写 `async def test_...`

## 1. 架构与数据流

```
industry ──chains/topic.py──▶ Topic ──chains/script.py──▶ Script(scenes[])
Script ──tts.py──▶ 每场景 mp3 + cues(词级时间轴, 相对场景起点)
Script ──render.py──▶ cover.png + scene_XXX.png (1920x1080, Jinja2 HTML→Playwright 截图)
        ──assemble.py(build_scene_clips)──▶ 每场景 mp4 + ffprobe 精确 duration 回填
全局cues(tts.offset_cues) ──subtitle_bars.py──▶ 每 cue 一张 1920x1080 透明 PNG(底部字幕条)
clips + bars ──assemble.py(finalize_video)──▶ final.mp4 (concat + overlay + loudnorm)
Script + chapters ──chains/metadata.py──▶ BiliMeta(标题/简介/标签)
        ──▶ project.json(manifest); 可选 bilibili.py 上传
```

输出目录结构(`out_dir`, 由调用方创建):

```
<out_dir>/
  script.json            # Script.model_dump_json(indent=2)
  scenes/scene_000.mp3   scene_000.png ...
  clips/scene_000.mp4 ...
  subs/bar_000.png ...   subtitles.srt
  cover.png
  final.mp4
  project.json           # ProjectManifest
```

## 2. LLM 链 `chains/`(langchain)

- 模型: `llm.get_chat_model(settings)`; **import 一律走 `langchain_core` / `langchain_openai`**
- 双路径: 主路径 `llm.with_structured_output(PydanticModel)`; 失败/不支持时回退
  `prompt | llm | StrOutputParser()` + 手写 `parse_xxx_json(text)`(须容忍 ```json 围栏, 用
  `langchain_core.output_parsers.JsonOutputParser` 或正则提取首个 JSON 对象/数组)
- prompt 用 `ChatPromptTemplate.from_messages`, **中文 system prompt**, 角色=资深求职内容策划
- 内容红线写进 system prompt: 不编造具体薪资数字(用"因城市/公司差异大"类表述)、
  不歧视(年龄/性别/学历)、不承诺"包过/必上岸"、不诋毁具体公司
- `narration` 口语化, 每场景 65~110 字(约 15~25 秒); 全片时长向 `settings.target_duration_sec` 靠
- 场景结构: 第 1 镜 point 开场(含 hook), 中间 point/qa 混排, 最后一镜 kind=ending(关注引导)

### 签名

```python
# chains/topic.py
async def generate_topics(llm, industry: str, n: int = 5, avoid: list[str] | None = None) -> list[Topic]
# chains/script.py
async def write_script(llm, topic: Topic, settings: Settings) -> Script
def parse_script_json(text: str) -> Script            # 回退解析(可测)
# chains/metadata.py
async def write_metadata(llm, script: Script, chapters: list[Chapter], settings: Settings) -> BiliMeta
```

- metadata: 标题 ≤80 字、B站风格(数字/悬念/利益点, 不做标题党造谣); desc 含章节行
  `MM:SS 标题`(chapters 提供) + 结尾固定一行 `* 本视频由 AI 辅助生成`;
  tags 5~10 个, 必含行业词与"面试"
- 测试(`tests/test_chains.py`): 用 `llm.fake_chat_model` 走**回退路径**全链路
  (prompt→fake→parse→model_validate); 测 parse 容忍围栏/前后废话; 测 narration 超长时
  校验截断/修正逻辑(若有)

## 3. TTS `tts.py`(edge-tts)

```python
async def synthesize_text(text: str, mp3_path: Path, voice: str, rate: str) -> list[Cue]
async def synthesize_scenes(script: Script, scenes_dir: Path, settings: Settings) -> list[SceneMedia]
def merge_word_cues(words: list[Cue], max_chars: int = 16, max_gap: float = 0.6, max_dur: float = 4.0) -> list[Cue]
def offset_cues(scene_media: list[SceneMedia]) -> list[Cue]   # 按 duration 累加平移成全局时间轴
def build_srt(cues: list[Cue]) -> str                          # 标准 srt 文本(便于人工核对)
```

- `edge_tts.Communicate(text, voice, rate=rate)`, `async for chunk in communicate.stream()`:
  `chunk["type"]=="audio"` 写 mp3; `=="WordBoundary"` 收 `(offset, duration, text)`,
  offset/duration 单位 **100ns ticks**, 秒 = ticks / 1e7
- `merge_word_cues`: 词→字幕条。累计 ≤max_chars 字; 词间 gap>max_gap 断条; 单条 >max_dur 断条;
  中文标点(，。！？；：)结尾处优先断条; 空文本跳过
- `synthesize_scenes`: 顺序合成(避免并发被限流), 产出 `scenes/scene_%03d.mp3`,
  `SceneMedia(index, mp3, cues=merge 后, duration=最后 cue end + 0.4 兜底)`
- 失败重试 2 次(指数退避), 仍失败抛 `RuntimeError` 带场景序号
- 测试(`tests/test_tts.py`): 纯函数全测(merge 规则/ticks 换算/offset/srt 格式);
  真实 edge-tts 打 integration 标记

## 4. 画面渲染 `render.py` + `subtitle_bars.py` + `templates/`

```python
# render.py(Jinja2 + Playwright 同步 API)
def render_project(script: Script, out_dir: Path, settings: Settings) -> RenderResult
# subtitle_bars.py(Pillow, 无浏览器依赖)
def render_subtitle_bars(cues: list[Cue], subs_dir: Path, settings: Settings) -> list[SubtitleBar]
```

- 模板放 `src/interview_video/templates/`: `cover.html / scene.html / qa.html / ending.html`,
  Jinja2 渲染后以 `sync_playwright` 打开(chromium, viewport=1920x1080) 截图 PNG
- 模板变量: `brand, title/script, scene, index, total, accent`。kind→模板: point→scene.html,
  qa→qa.html, ending→ending.html; 每场景一帧 `scenes/scene_%03d.png`, 另渲 `cover.png`(封面:
  大标题+hook+行业 chip+brand)
- 视觉规范: 深色渐变底(#0f172a→#1e293b)+点缀色(#38bdf8), 卡片式 bullets, 大字号
  (cover 主标题 ≥80px, header ≥56px, bullets ≥34px), **底部 180px 留白**(字幕条区域, 不得有内容),
  中文字体栈 `"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif`
- 字幕条: 每 cue 一张 1920x1080 **透明 PNG**, 底部居中文字(距底 `settings.subtitle_bottom`),
  字号 `settings.subtitle_fontsize`, 白色填充+黑色描边(stroke_width≥3),
  `settings.subtitle_box=True` 时加半透明黑圆角底条(RGBA 0,0,0,~120)
- 字体探测: `settings.subtitle_font_path` 优先; 否则按序探测
  `/System/Library/Fonts/PingFang.ttc`(index 0~5 找 PingFangSC),
  `/System/Library/Fonts/STHeiti Medium.ttc`, `/System/Library/Fonts/Hiragino Sans GB.ttc`,
  都找不到抛带指引的 RuntimeError
- 测试(`tests/test_render.py`): jinja2 渲染 HTML 字符串断言(不启浏览器);
  Pillow 字幕条生成(字体存在时, 否则 skip)断言尺寸/透明通道/文件非空; 截图打 integration

## 5. 合成 `assemble.py`(ffmpeg/ffprobe, subprocess)

```python
def ffprobe_duration(path: Path) -> float
def build_scene_clips(scene_media: list[SceneMedia], clips_dir: Path, settings: Settings) -> list[Path]
def finalize_video(clips: list[Path], bars: list[SubtitleBar], out_path: Path, settings: Settings) -> Path
def build_chapters(script: Script, scene_media: list[SceneMedia]) -> list[Chapter]
```

- `build_scene_clips`: 每场景 `ffmpeg -y -loop 1 -framerate {fps} -i scene.png -i scene.mp3
  -c:v libx264 -tune stillimage -pix_fmt yuv420p -r {fps} -c:a aac -b:a 160k -ar 44100
  -vf scale=...:force_original_aspect_ratio=decrease,pad=... -t {dur} clips/scene_%03d.mp4`;
  然后 `ffprobe_duration` 回填 `SceneMedia.duration`(精确值)
- `finalize_video`: ①concat demuxer `-f concat -safe 0 -i list.txt -c copy base.mp4`
  ②若有 bars: 一条命令 `-i base.mp4 -i bar0.png ... -filter_complex
  "[0][1]overlay=0:0:enable='between(t,{start},{end})'[v1];[v1][2]overlay=...[v2];..."`
  链式 overlay 全部字幕条(注意 enable 时间格式保留 2~3 位小数, 避免末段标签空链: 最后标签
  映射为 -map "[vN]" -map 0:a) ③音频 `loudnorm=I=..:TP=..:LRA=..`(settings), 输出
  H.264/yuv420p/AAC 的 `final.mp4`; 无 bars 时跳过②直接 loudnorm
- ffmpeg 参数里**避免转义地狱**: 以 `cwd=out_dir` + 相对路径调用; 超时≥300s; 非零退出码
  抛 RuntimeError 附带 stderr 末尾 500 字
- `build_chapters`: 场景起点累加(精确 duration), title 取 scene.header
- 测试(`tests/test_assemble.py`): integration(需 ffmpeg)用 lavfi 造测试素材
  (`testsrc2=size=1920x1080:duration=2`、`sine=frequency=440:duration=2`)验证:
  单场景 clip 时长≈2s、concat 总时长≈N×2s、带 bar 的 finalize 输出存在且流参数正确
  (ffprobe 查 codec/分辨率); 纯函数(concat list 内容、chapters 计算)不打标

## 6. 编排与入口 `graph.py / cli.py / bilibili.py / __main__.py`

### graph.py(LangGraph)

```python
class PipelineState(TypedDict, total=False):
    out_dir: Path; industry: str; topic_title: str | None; script_file: Path | None
    script: Script; scene_media: list[SceneMedia]; render: RenderResult
    clips: list[Path]; bars: list[SubtitleBar]; video: Path; chapters: list[Chapter]
    meta: BiliMeta | None; manifest_path: Path

def build_graph(settings: Settings) -> CompiledStateGraph  # 线性: gen_script→tts→render→clips→subs→finalize→metadata→manifest
async def run_pipeline(settings, out_dir, industry="互联网", topic_title=None, script_file=None) -> ProjectManifest
```

- 节点内**通过模块属性调用**(`from . import tts as tts_mod` 再 `tts_mod.synthesize_scenes(...)`),
  便于测试 monkeypatch
- gen_script: 有 script_file 直接 `Script.model_validate_json`; 否则 LLM 生成 topic(给了
  topic_title 则作为选题提示) → write_script; 落盘 script.json
- metadata: `get_chat_model` 抛 RuntimeError(没配 key)时降级 `meta=None` 并 warning, 不中断
- manifest: ProjectManifest 写 project.json; srt 落盘(`subs/subtitles.srt`, build_srt(全局cues))

### cli.py(typer, `ivb` 入口 + `python -m interview_video`)

```
ivb topics --industry 护理 [-n 8]                 # 生成选题清单(需 LLM), 存 output/topics-*.json
ivb generate [--industry 互联网] [--topic 标题提示] [--script-file examples/sample_script.json]
             [--voice zh-CN-XiaoxiaoNeural] [--out output/xxx]
ivb upload <project_dir> [--yes]                  # 读 project.json 上传 B站
```
- `--script-file` 模式无需 LLM(metadata 自动降级); 未给 --out 时自动生成
  `output/<slug>-<YYYYmmdd-HHMM>/`
- generate 完成后 rich 打印产物路径 + BiliMeta; `settings.bili_upload_enabled=True` 时
  询问确认后上传(--yes 跳过)
- `main()` 包装 `app()`(console_scripts 需要)

### bilibili.py(可选, 默认关)

```python
def build_credential(settings: Settings):            # 三件套齐全才返回 Credential, 否则 None
async def upload_project(project_dir: Path, settings: Settings) -> str   # 返回 bvid
```

- 用 `bilibili_api`(`video_uploader`); **以已安装版本实际 API 为准**(先 `python -c` 探一下再写)
- 未启用/缺凭证: 抛带指引的 RuntimeError(如何抓 cookie); desc 末尾确保含 AI 生成声明;
  提示用户发布后到创作中心补勾"AI生成"标识(合规)
- 测试: 不打 integration 的单测只做 credential 组装/缺参报错; 真实上传不写测试

### README.md(agent E 起草)

中文。内容: 项目简介+架构图(文字版)、快速开始(uv venv/安装/playwright install/ffmpeg 要求
说明无 libass 亦可)、三条命令示例、.env 说明(可复用 drivecast 配置)、B站 cookie 获取步骤、
AI 内容合规标注提示、目录结构、测试(含 --run-integration)、路线图(双音色对话/真人数字人/
自动定时发布)。

## 7. 纪律

1. 只新建/修改分派给你的文件; 只读契约文件绝不动
2. 不在单元测试里联网/调真实 LLM/起 chromium; ffmpeg 测试打 integration 且 skipif 无 ffmpeg
3. 完成后跑自己的测试文件全绿(非 integration); 最终报告输出 JSON:
   `{"files": [...], "tests": "passed|failed", "notes": "..."}`
4. 代码风格: 中文注释、类型标注、pathlib.Path; 与 models.py 的命名保持一致

## 8. 落地校订(实现后回写, 2026-09-23)

- `render_project(script, out_dir, settings, industry="")` 多了可选 industry 参数(封面行业 chip), 由 graph 传入
- `llm.py` 的 `GenericFakeChatModel` 导入已根治为 try 新路径 `fake_chat_models`(langchain_core>=1.0) fallback 旧路径
- chains 实际默认路径: prompt|llm|StrOutputParser|parse_xxx_json; with_structured_output 为 try 增强(行为等价)
- graph 节点对同步阻塞调用(playwright 截图/ffmpeg/Pillow)一律 `asyncio.to_thread`; render 节点负责把 frames 回填 `SceneMedia.png`
- edge-tts 需显式 `boundary="WordBoundary"`(默认 SentenceBoundary); 本机无 PingFang.ttc, 字幕字体落到 STHeiti/Hiragino
- 端到端已验证: script-file 冒烟 25.8s + 真实 LLM(护理行业) 96.7s 成片, BiliMeta 真实生成
- TTS 双通道(2026-09-23 增补): `tts_provider=edge|doubao`; 豆包走 seed-tts-2.0 单向流式(协议同 drivecast), 无词级时间戳 → `estimate_cues` 按字数+标点权重把 ffprobe 总时长比例分配; edge 仍为默认(时间轴精确)。doubao 凭证走 TTS_DOUBAO_API_KEY

## 9. 系列长视频(多 agent, 2026-09-23 增补)

- 新模型(models.py): `SeriesPlan/EpisodePlan/EpisodeOutline/ChapterSpec/ChapterDraft`
- `chains/series.py`: 策划 `plan_series` → 分集编剧 `write_episode_outline` → 章节写手
  `write_chapter` → 审稿 `review_chapter`; `roll_context`(600 字滚动摘要窗口)做上下文切分:
  章节间传递上一章摘要, 集边界全量重置; `build_episode_script` 汇总场景(首镜 point/末镜 ending
  确定性修正); `_fix_draft` 归一 kind + narration 截断; 解析均走 structured_output → JSON 回退
- `series.py`: `build_series_graph` 双循环(plan → episode_setup → outline → chapter
  ⇄ review → build_script → 下一集), 事件 plan_done/outline_done/chapter_done/script_done;
  `generate_series(settings, theme, episodes, episode_sec, on_event) -> (SeriesPlan, list[Script])`
- config: `series_review=true`, `series_chapter_sec=150`
- `graph.py`: state 预置 `script` 时 gen_script 直通(不碰 LLM/script_file), `run_pipeline(..., script=)`
- `cli.py`: `ivb series <theme> --episodes N --duration M`(M=分钟); web: JobRequest 加
  `mode=single|series + theme/episodes/duration_min`, `_run_series_job` 逐集跑 `_execute_pipeline`,
  系列根目录存 series.json, 分集 `ep01-*`; list_projects/_project_dir 下钻系列根一层(作品库展开分集),
  分集 URL 用相对 out_root 路径(嵌套也能播放)
- 测试: tests/test_series.py(链解析/滚动上下文/双循环编排/review 开关/预制脚本直通);
  fake_media fixture 上移至 conftest.py; 110 passed + 6 skipped 全绿
