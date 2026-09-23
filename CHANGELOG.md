# Changelog

本文件记录值得注意的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循语义化版本(单文件未发布前以 0.x 滚动)。

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
