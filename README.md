# interview-video-bot

AI 面试求职短视频流水线: 给一条行业(如「互联网」「护理」「汽修」), 自动产出
**选题 → 口播脚本 → TTS 配音 → HTML 画面渲染 → ffmpeg 合成(带字幕条)→ B站元数据**
的完整竖向工作流, 目标是把「面试经验/求职干货」做成可持续更新的中文短视频频道。

## 架构(文字版)

```
industry ──chains/topic.py──▶ Topic ──chains/script.py──▶ Script(scenes[])
Script ──tts.py(edge-tts)──▶ 每场景 mp3 + 词级 cues
Script ──render.py(Jinja2+Playwright)──▶ cover.png + scene_XXX.png (1920x1080)
        ──assemble.py(build_scene_clips)──▶ 每场景 mp4(ffprobe 回填精确时长)
全局cues(tts.offset_cues)──subtitle_bars.py(Pillow)──▶ 每 cue 一张 1920x1080 透明字幕条 PNG
clips + bars ──assemble.py(finalize_video)──▶ final.mp4(concat + overlay + loudnorm)
Script + chapters ──chains/metadata.py──▶ BiliMeta(标题/简介/标签)
        ──▶ project.json(manifest); 可选 bilibili.py 上传 B站
```

编排层 `graph.py` 用 LangGraph 把以上串成线性 8 节点图:
`gen_script → tts → render → clips → subs → finalize → metadata → manifest`,
`run_pipeline()` 是 async 便捷入口; `cli.py` 是 `ivb` 命令行(也是 `python -m interview_video`)。

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

# 4. 无需 LLM key 的冒烟: 用现成脚本跑通全流程
.venv/bin/ivb generate --script-file examples/sample_script.json
```

## 三条命令

```bash
# ① 生成选题清单(需 LLM), 存 output/topics-*.json
ivb topics --industry 护理 -n 8

# ② 跑完整流水线生成视频
#    未给 --out 时自动生成 output/<slug>-<YYYYmmdd-HHMM>/
#    --script-file 模式全程不需要 LLM key(metadata 自动降级)
ivb generate --industry 互联网 \
             --topic "应届生自我介绍" \
             --voice zh-CN-XiaoxiaoNeural \
             --out output/self-intro
# 或直接复用现成脚本:
ivb generate --script-file examples/sample_script.json --out output/demo

# ③ 读 project.json 上传 B站(需开启 BILI_UPLOAD_ENABLED 并配置 cookie)
ivb upload output/demo --yes
```

生成完成后会 rich 打印产物路径(final.mp4 / cover.png / subtitles.srt)与 BiliMeta
(标题/标签); 若 `BILI_UPLOAD_ENABLED=true`, 会询问确认后上传(`--yes` 跳过)。

## .env 说明

环境变量名与字段名大写一致, **可复用 drivecast 的 `.env`**(同一套 OpenAI 兼容配置):

```bash
# LLM(任意 OpenAI 兼容服务, 如 deepseek)
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# TTS / 内容 / 视频(均有默认值, 可省略)
TTS_VOICE=zh-CN-YunxiNeural
TTS_RATE=+6%
BRAND=面试充电站
TARGET_DURATION_SEC=100

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
  scenes/scene_000.mp3   scene_000.png ...   # 每场景配音 + 画面帧
  clips/scene_000.mp4 ...                     # 每场景合成片段
  subs/bar_000.png ...   subtitles.srt        # 字幕条 PNG + 全局 srt
  cover.png
  final.mp4                                     # 成片
  project.json                                  # ProjectManifest(含 BiliMeta)
```

## 测试

```bash
.venv/bin/pytest                  # 单元测试(不联网/不调真实 LLM/不起 chromium)
.venv/bin/pytest --run-integration  # 额外跑需要 ffmpeg/网络/chromium 的集成测试
```

单元测试将 tts/render/assemble/subtitle_bars monkeypatch 成本地假实现;
依赖 ffmpeg 的素材合成测试打 `@pytest.mark.integration`, 缺二进制时自动 skip。

## 路线图

- 双音色对话(面试官/求职者交替配音, 更强代入感)
- 真人数字人形象(替代 HTML 画面帧)
- 自动定时发布(接 B站 delay_time / cron 定点投递)
