"""编排层: LangGraph 线性 8 节点流水线 + run_pipeline 便捷入口。

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
)
from .workflow_engine import END, START, CompiledStateGraph, StateGraph

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
    manifest_path: Path


STOP_AFTER_CHOICES = ("script",)


def build_graph(settings: Settings, stop_after: str | None = None) -> CompiledStateGraph:
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
        if preset is not None:
            # 系列模式 / --resume: 脚本已预生成, 本节点直通
            script = preset
            logger.info("使用预生成脚本: %s", script.title)
        elif script_file is not None:
            script = Script.model_validate_json(
                Path(script_file).read_text(encoding="utf-8")
            )
            logger.info("使用脚本文件: %s", script_file)
        else:
            from . import llm as llm_mod
            from .chains import script as script_mod
            from .chains import topic as topic_mod

            llm = llm_mod.get_chat_model(settings)
            industry = state.get("industry", "互联网")
            topic_title = (state.get("topic_title") or "").strip()
            if topic_title:
                # 用户指定的选题原样使用, 不再随机生成再模糊匹配
                topic = Topic(title=topic_title, industry=industry)
            else:
                topic = (await topic_mod.generate_topics(llm, industry))[0]
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
        """
        from . import assemble as assemble_mod
        from . import llm as llm_mod
        from .chains import metadata as metadata_mod

        chapters = assemble_mod.build_chapters(
            state["script"], state["scene_media"]
        )
        try:
            llm = llm_mod.get_chat_model(settings)
        except Exception as exc:  # noqa: BLE001 —— 客户端构造失败也走降级(空 key/坏配置)
            logger.warning("metadata 降级为 meta=None(无法初始化 LLM): %s", exc)
            return {"chapters": chapters, "meta": None}
        try:
            meta = await metadata_mod.write_metadata(
                llm, state["script"], chapters, settings
            )
        except llm_mod.LLMBudgetExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 元数据是可降级项
            logger.warning(
                "metadata 生成失败, 降级为 meta=None(%s: %s)",
                type(exc).__name__, exc,
            )
            meta = None
        return {"chapters": chapters, "meta": meta}

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
        manifest = ProjectManifest(
            created_at=datetime.now(),
            industry=state.get("industry", ""),
            script=script,
            video=state["video"],
            cover=state["render"].cover,
            srt=srt_path,
            chapters=state.get("chapters", []),
            meta=state.get("meta"),
            voice=settings.tts_voice,
            duration_total=sum(m.duration for m in scene_media),
            generation={
                "llm_model": settings.llm_model,
                "llm_temperature": str(settings.llm_temperature),
                "tts_provider": settings.tts_provider,
                "prompt_versions": "topic1/script2/metadata2/series2",
            },
        )
        manifest_path = out_dir / "project.json"
        manifest_path.write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        return {"manifest_path": manifest_path}

    graph = StateGraph(PipelineState)
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


def resume_industry(out_dir: Path) -> str | None:
    """续跑目录里记录的行业: project.json 优先, 其次 topic.json; 都没有返回 None。"""
    for fname in ("project.json", "topic.json"):
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
        final = await graph.ainvoke(
            initial, config={"callbacks": [LLMCallAudit(out_dir)]}
        )
    return final["script"]


async def run_pipeline(
    settings: Settings,
    out_dir: Path,
    industry: str = "互联网",
    topic_title: str | None = None,
    script_file: Path | None = None,
    script: Script | None = None,
) -> ProjectManifest:
    """async 便捷入口: 跑完整流水线, 返回 ProjectManifest(已落盘 project.json)。

    script 预制传入时跳过 LLM 生成(系列模式 / --resume 用)。
    启动前先跑 doctor.preflight(ffmpeg/chromium/字体), 避免付费步骤后才发现缺依赖。
    """
    from . import doctor as doctor_mod

    await asyncio.to_thread(doctor_mod.preflight, settings)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    graph = build_graph(settings)
    initial: PipelineState = {
        "out_dir": out_dir,
        "industry": industry,
        "topic_title": topic_title,
        "script_file": Path(script_file) if script_file is not None else None,
    }
    if script is not None:
        initial["script"] = script
    from .llm_audit import LLMCallAudit, run_budget

    with run_budget(settings):
        final = await graph.ainvoke(
            initial, config={"callbacks": [LLMCallAudit(out_dir)]}
        )
    manifest_path = Path(final["manifest_path"])
    return ProjectManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
