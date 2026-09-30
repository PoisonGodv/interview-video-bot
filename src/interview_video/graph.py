"""编排层: 原生工作流引擎线性 8 节点流水线 + run_pipeline 便捷入口。

节点顺序(见 SPECS §6):
    gen_script → tts → render → clips → subs → finalize → metadata → manifest

约定:
- 节点内一律通过模块属性调用(``from . import tts as tts_mod`` 再
  ``tts_mod.synthesize_scenes(...)``), 便于测试 monkeypatch;
- import 延迟到节点内部, 使本模块即使媒体层依赖(如 playwright)不可用时也能被导入;
- ``settings`` 由 ``build_graph`` 闭包捕获, 不进入图状态。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from . import procs
from .config import Settings
from .models import (
    BiliMeta,
    Chapter,
    ProjectManifest,
    RenderResult,
    SceneMedia,
    Script,
    SubtitleBar,
    Topic,
    load_script_file,
)
from .workflow_engine import END, START, CompiledWorkflow, RunContext, WorkflowEngine

logger = logging.getLogger(__name__)


class PipelineState(TypedDict, total=False):
    """流水线共享状态。total=False: 节点按需写入自己产出的键。"""

    out_dir: Path
    industry: str
    topic_title: str | None
    script_file: Path | None
    script: Script
    scene_media: list[SceneMedia]
    render: RenderResult
    clips: list[Path]
    bars: list[SubtitleBar]
    video: Path
    chapters: list[Chapter]
    meta: BiliMeta | None
    meta_error: str | None
    regen_meta: bool
    manifest_path: Path


STOP_AFTER_CHOICES = ("script",)

# 节点 → 中文阶段名(按执行顺序; CLI 进度展示用)
PIPELINE_STAGES: list[tuple[str, str]] = [
    ("gen_script", "选题与脚本"),
    ("tts", "配音合成"),
    ("render", "画面渲染"),
    ("clips", "片段合成"),
    ("subs", "字幕生成"),
    ("finalize", "视频合成"),
    ("metadata", "B站元数据"),
    ("manifest", "落盘收尾"),
]


def build_graph(settings: Settings, stop_after: str | None = None) -> CompiledWorkflow:
    """构建编译后的线性流水线图。节点闭包捕获 settings。

    stop_after="script": gen_script 后直接结束(落盘 script.json 供人工审改, 再 --resume 续跑)。
    """
    if stop_after is not None and stop_after not in STOP_AFTER_CHOICES:
        raise ValueError(f"stop_after 仅支持 {STOP_AFTER_CHOICES}, 收到 {stop_after!r}")

    async def gen_script(state: PipelineState) -> PipelineState:
        """第 1 节点: 拿到 Script 并落盘 script.json。

        有 script_file 直接 ``Script.model_validate_json``(全程不需要 LLM);
        否则给了 topic_title 直接以其为选题, 未给则 LLM 生成选题取首条 → write_script。
        """
        out_dir: Path = state["out_dir"]
        script_file = state.get("script_file")
        preset = state.get("script")
        if preset is not None or script_file is not None:
            if preset is not None:
                # 系列模式 / --resume: 脚本已预生成, 本节点直通
                script = preset
                logger.info("使用预生成脚本: %s", script.title)
            else:
                script = load_script_file(script_file)
                logger.info("使用脚本文件: %s", script_file)
            # 人工编辑/外部导入的脚本不经 LLM 边界闸门: 做一次非阻断体检(红线/超长)并告警
            from .boundary import lint_script

            for warning in lint_script(script):
                logger.warning("脚本体检: %s", warning)
        else:
            from . import llm as llm_mod
            from .chains import script as script_mod
            from .chains import topic as topic_mod

            llm = llm_mod.get_chat_model(settings)
            industry = state.get("industry", "互联网")
            topic_title = (state.get("topic_title") or "").strip()
            from . import backlog as backlog_mod

            if topic_title:
                # 用户指定的选题原样使用(选题库有同名条目时带上其 hook/要点), 不再随机生成再模糊匹配
                topic = _backlog_find(settings, topic_title, industry) or Topic(
                    title=topic_title, industry=industry
                )
            else:
                # 已入库/用过的标题作为 avoid, 生成结果入库(未选中的留作后续选题)
                generated = await topic_mod.generate_topics(
                    llm, industry, avoid=backlog_mod.avoid_titles(settings, industry) or None,
                )
                topic = generated[0]
                _safe_backlog(backlog_mod.add_topics, settings, generated)
            (out_dir / "topic.json").write_text(
                topic.model_dump_json(indent=2), encoding="utf-8"
            )
            script = await script_mod.write_script(llm, topic, settings)
        (out_dir / "script.json").write_text(
            script.model_dump_json(indent=2), encoding="utf-8"
        )
        return {"script": script}

    async def tts_node(state: PipelineState) -> PipelineState:
        """第 2 节点: 每场景 mp3 + 词级 cue 合并后的字幕 cue。"""
        from . import tts as tts_mod

        scenes_dir = state["out_dir"] / "scenes"
        scenes_dir.mkdir(parents=True, exist_ok=True)
        scene_media = await tts_mod.synthesize_scenes(
            state["script"], scenes_dir, settings
        )
        return {"scene_media": scene_media}

    async def render_node(state: PipelineState) -> PipelineState:
        """第 3 节点: cover.png + 每场景一帧 PNG(Jinja2 HTML → Playwright 截图)。"""
        from . import render as render_mod

        # playwright 同步 API 不能在 async 节点的事件循环里跑, 放到工作线程
        result = await procs.run_in_thread(
            render_mod.render_project,
            state["script"], state["out_dir"], settings,
            industry=state.get("industry", ""),
        )
        # 回填 SceneMedia.png, 供 clips 节点合成
        scene_media = state["scene_media"]
        if len(result.frames) != len(scene_media):
            raise RuntimeError(
                f"渲染帧数({len(result.frames)})与场景数({len(scene_media)})不一致"
            )
        for sm, frame in zip(scene_media, result.frames):
            sm.png = frame
        return {"render": result, "scene_media": scene_media}

    async def clips_node(state: PipelineState) -> PipelineState:
        """第 4 节点: 每场景 mp4, ffprobe 精确 duration 回填 SceneMedia。"""
        from . import assemble as assemble_mod

        clips_dir = state["out_dir"] / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        clips = await procs.run_in_thread(
            assemble_mod.build_scene_clips,
            state["scene_media"], clips_dir, settings,
        )
        return {"clips": clips}

    async def subs_node(state: PipelineState) -> PipelineState:
        """第 5 节点: 全局 cue 平移后, 每 cue 一张 1920x1080 透明字幕条 PNG。"""
        from . import subtitle_bars as bars_mod
        from . import tts as tts_mod

        cues = tts_mod.offset_cues(state["scene_media"])
        subs_dir = state["out_dir"] / "subs"
        subs_dir.mkdir(parents=True, exist_ok=True)
        bars = await procs.run_in_thread(
            bars_mod.render_subtitle_bars, cues, subs_dir, settings
        )
        return {"bars": bars}

    async def finalize_node(state: PipelineState) -> PipelineState:
        """第 6 节点: concat + overlay 字幕条 + loudnorm → final.mp4。"""
        from . import assemble as assemble_mod

        out_path = state["out_dir"] / "final.mp4"
        video = await procs.run_in_thread(
            assemble_mod.finalize_video,
            state["clips"], state["bars"], out_path, settings,
        )
        return {"video": video}

    async def metadata_node(state: PipelineState) -> PipelineState:
        """第 7 节点: 章节 + BiliMeta。

        两级降级, 均不中断流水线(final.mp4 已在手, 不能因元数据白做):
        1. get_chat_model 抛 RuntimeError(未配 key) → meta=None;
        2. write_metadata 本身失败(LLM 输出解析/网络) → logger.warning + meta=None。
        续跑时: 旧 project.json 有 meta 且脚本未变 → 直接复用不调 LLM(regen_meta 强制重生成);
        生成失败时有旧 meta 则保留旧的。失败原因写入 meta_error。
        """
        from . import assemble as assemble_mod
        from . import llm as llm_mod
        from .chains import metadata as metadata_mod

        script = state["script"]
        chapters = assemble_mod.build_chapters(script, state["scene_media"])
        old_meta, old_script = _previous_meta(state["out_dir"])
        if (
            old_meta is not None and not state.get("regen_meta")
            and old_script == script.model_dump(mode="json")
        ):
            logger.info("脚本未变, 复用已有 B站元数据(不调 LLM)")
            return {"chapters": chapters, "meta": old_meta, "meta_error": None}
        try:
            llm = llm_mod.get_chat_model(settings)
        except Exception as exc:  # noqa: BLE001 —— 客户端构造失败也走降级(空 key/坏配置)
            logger.warning("metadata 降级为 meta=None(无法初始化 LLM): %s", exc)
            return {
                "chapters": chapters, "meta": old_meta,
                "meta_error": f"无法初始化 LLM: {exc}",
            }
        try:
            meta = await metadata_mod.write_metadata(llm, script, chapters, settings)
        except llm_mod.LLMBudgetExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 元数据是可降级项
            logger.warning(
                "metadata 生成失败, 降级为 meta=None(%s: %s)",
                type(exc).__name__, exc,
            )
            return {
                "chapters": chapters, "meta": old_meta,
                "meta_error": f"{type(exc).__name__}: {exc}",
            }
        return {"chapters": chapters, "meta": meta, "meta_error": None}

    async def manifest_node(state: PipelineState) -> PipelineState:
        """第 8 节点: 落盘 project.json + subs/subtitles.srt(全局 cue 的标准 srt)。"""
        from . import tts as tts_mod

        out_dir: Path = state["out_dir"]
        script = state["script"]
        scene_media = state["scene_media"]
        cues = tts_mod.offset_cues(scene_media)
        subs_dir = out_dir / "subs"
        subs_dir.mkdir(parents=True, exist_ok=True)
        srt_path = subs_dir / "subtitles.srt"
        srt_path.write_text(tts_mod.build_srt(cues), encoding="utf-8")
        generation = {
            "llm_model": settings.llm_model,
            "llm_temperature": str(settings.llm_temperature),
            "tts_provider": settings.tts_provider,
            "video_aspect": settings.video_aspect,
            "prompt_versions": "topic1/script2/metadata2/series2",
        }
        if state.get("meta_error"):
            generation["meta_error"] = str(state["meta_error"])
        manifest = ProjectManifest(
            created_at=datetime.now(),
            industry=state.get("industry", ""),
            script=script,
            video=Path(state["video"]).resolve(),
            cover=Path(state["render"].cover).resolve(),
            srt=srt_path.resolve(),
            chapters=state.get("chapters", []),
            meta=state.get("meta"),
            voice=settings.tts_doubao_voice if settings.tts_provider == "doubao" else settings.tts_voice,
            duration_total=sum(m.duration for m in scene_media),
            generation=generation,
        )
        manifest_path = out_dir / "project.json"
        _atomic_write_text(manifest_path, manifest.model_dump_json(indent=2))
        (out_dir / PREV_MANIFEST).unlink(missing_ok=True)  # 续跑成功, 旧清单备份作废
        from . import assemble as assemble_mod

        assemble_mod.cleanup_intermediates(out_dir, keep_clips=settings.keep_intermediates)
        topic_json = out_dir / "topic.json"
        if topic_json.exists():  # 选题来自 LLM/--topic/选题库时, 成片后在选题库标记已用
            from . import backlog as backlog_mod

            try:
                used = Topic.model_validate_json(topic_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                used = None
            if used is not None:
                _safe_backlog(backlog_mod.mark_used, settings, used.title, used.industry, out_dir)
        return {"manifest_path": manifest_path}

    graph = WorkflowEngine(PipelineState)
    graph.add_node("gen_script", gen_script)
    graph.add_node("tts", tts_node)
    graph.add_node("render", render_node)
    graph.add_node("clips", clips_node)
    graph.add_node("subs", subs_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("metadata", metadata_node)
    graph.add_node("manifest", manifest_node)
    # 线性边
    graph.add_edge(START, "gen_script")
    if stop_after == "script":
        graph.add_edge("gen_script", END)
        return graph.compile()
    for prev, nxt in [
        ("gen_script", "tts"),
        ("tts", "render"),
        ("render", "clips"),
        ("clips", "subs"),
        ("subs", "finalize"),
        ("finalize", "metadata"),
        ("metadata", "manifest"),
    ]:
        graph.add_edge(prev, nxt)
    graph.add_edge("manifest", END)
    return graph.compile()


def _backlog_find(settings: Settings, title: str, industry: str) -> Topic | None:
    from . import backlog as backlog_mod

    try:
        return backlog_mod.find(settings, title, industry)
    except OSError:
        return None


def _safe_backlog(fn, *args) -> None:
    """选题库读写失败只告警, 不影响成片。"""
    try:
        fn(*args)
    except OSError as exc:
        logger.warning("选题库更新失败(不影响本次生成): %s", exc)


# 续跑开始时旧 project.json 改名为此(跑的过程中项目不再显示为已完成; 成功后删除)
PREV_MANIFEST = "project.prev.json"


def _atomic_write_text(path: Path, text: str) -> None:
    """先写同目录临时文件再 os.replace: 中途崩溃/取消不会留下半截 JSON。"""
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _stash_previous_manifest(out_dir: Path) -> None:
    """续跑前把旧 project.json 挪成 project.prev.json(失败/取消时保留作参考, 成功后删除)。"""
    cur = Path(out_dir) / "project.json"
    if not cur.exists():
        return
    try:
        os.replace(cur, Path(out_dir) / PREV_MANIFEST)
    except OSError as exc:
        logger.warning("续跑前备份 project.json 失败(不影响生成): %s", exc)


def _previous_meta(out_dir: Path) -> tuple[BiliMeta | None, dict | None]:
    """旧 project.json(或续跑备份 project.prev.json)里的 (meta, script dump); 都没有返回 (None, None)。"""
    for fname in ("project.json", PREV_MANIFEST):
        try:
            old = ProjectManifest.model_validate_json(
                (Path(out_dir) / fname).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        return old.meta, old.script.model_dump(mode="json")
    return None, None


def resume_industry(out_dir: Path) -> str | None:
    """续跑目录里记录的行业: project.json 优先, 其次 topic.json; 都没有返回 None。"""
    for fname in ("project.json", PREV_MANIFEST, "topic.json"):
        f = Path(out_dir) / fname
        if f.exists():
            try:
                industry = json.loads(f.read_text(encoding="utf-8")).get("industry")
            except (OSError, ValueError):
                continue
            if industry:
                return str(industry)
    return None


async def run_script_stage(
    settings: Settings,
    out_dir: Path,
    industry: str = "互联网",
    topic_title: str | None = None,
    script_file: Path | None = None,
) -> Script:
    """只跑 gen_script 节点(落盘 topic.json/script.json)后停下, 不需要媒体依赖。"""
    from .llm_audit import LLMCallAudit

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    graph = build_graph(settings, stop_after="script")
    initial: PipelineState = {
        "out_dir": out_dir,
        "industry": industry,
        "topic_title": topic_title,
        "script_file": Path(script_file) if script_file is not None else None,
    }
    from .llm_audit import run_budget

    with run_budget(settings):
        final = await graph.run(initial, context=RunContext(audit=LLMCallAudit(out_dir)))
    return final["script"]


async def run_pipeline(
    settings: Settings,
    out_dir: Path,
    industry: str = "互联网",
    topic_title: str | None = None,
    script_file: Path | None = None,
    script: Script | None = None,
    on_stage: Callable[[str, str], None] | None = None,
    regen_meta: bool = False,
) -> ProjectManifest:
    """async 便捷入口: 跑完整流水线, 返回 ProjectManifest(已落盘 project.json)。

    script 预制传入时跳过 LLM 生成(系列模式 / --resume 用)。
    on_stage(node, "start"|"done"): 逐节点进度回调(CLI 进度条用)。
    regen_meta: 续跑时强制重新生成 B站元数据(默认脚本未变则复用旧 meta)。
    启动前先跑 doctor.preflight(ffmpeg/chromium/字体), 避免付费步骤后才发现缺依赖。
    """
    from . import doctor as doctor_mod

    await asyncio.to_thread(doctor_mod.preflight, settings)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _stash_previous_manifest(out_dir)
    graph = build_graph(settings)
    initial: PipelineState = {
        "out_dir": out_dir,
        "industry": industry,
        "topic_title": topic_title,
        "script_file": Path(script_file) if script_file is not None else None,
    }
    if script is not None:
        initial["script"] = script
    if regen_meta:
        initial["regen_meta"] = True
    from .llm_audit import LLMCallAudit, run_budget

    order = [k for k, _ in PIPELINE_STAGES]

    def _emit(node: str, status: str) -> None:
        if on_stage is None:
            return
        try:
            on_stage(node, status)
        except Exception:  # noqa: BLE001 —— 进度展示失败不影响流水线
            logger.debug("on_stage 回调失败", exc_info=True)

    final: dict = dict(initial)
    with run_budget(settings):
        _emit(order[0], "start")
        async for chunk in graph.stream(
            initial, stream_mode="updates", context=RunContext(audit=LLMCallAudit(out_dir)),
        ):
            for node, output in chunk.items():
                final.update(output)
                _emit(node, "done")
                i = order.index(node) if node in order else -1
                if 0 <= i < len(order) - 1:
                    _emit(order[i + 1], "start")
    manifest_path = Path(final["manifest_path"])
    return ProjectManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
