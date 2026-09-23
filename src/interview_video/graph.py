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
import logging
import warnings
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .config import Settings
from .models import (
    BiliMeta,
    Chapter,
    ProjectManifest,
    RenderResult,
    SceneMedia,
    Script,
    SubtitleBar,
)

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


def build_graph(settings: Settings) -> CompiledStateGraph:
    """构建编译后的线性流水线图。节点闭包捕获 settings。"""

    async def gen_script(state: PipelineState) -> PipelineState:
        """第 1 节点: 拿到 Script 并落盘 script.json。

        有 script_file 直接 ``Script.model_validate_json``(全程不需要 LLM);
        否则 LLM 生成选题(给了 topic_title 则作为选题提示) → write_script。
        """
        out_dir: Path = state["out_dir"]
        script_file = state.get("script_file")
        if script_file is not None:
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
            topics = await topic_mod.generate_topics(llm, industry)
            topic_title = state.get("topic_title")
            topic = topics[0]
            if topic_title:
                # 有标题提示时优先匹配包含该提示的选题, 找不到退回第一条
                for t in topics:
                    if topic_title in t.title:
                        topic = t
                        break
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
        result = await asyncio.to_thread(
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
        clips = await asyncio.to_thread(
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
        bars = await asyncio.to_thread(
            bars_mod.render_subtitle_bars, cues, subs_dir, settings
        )
        return {"bars": bars}

    async def finalize_node(state: PipelineState) -> PipelineState:
        """第 6 节点: concat + overlay 字幕条 + loudnorm → final.mp4。"""
        from . import assemble as assemble_mod

        out_path = state["out_dir"] / "final.mp4"
        video = await asyncio.to_thread(
            assemble_mod.finalize_video,
            state["clips"], state["bars"], out_path, settings,
        )
        return {"video": video}

    async def metadata_node(state: PipelineState) -> PipelineState:
        """第 7 节点: 章节 + BiliMeta。

        get_chat_model 抛 RuntimeError(未配 LLM key)时降级 meta=None 并 warning,
        不中断流水线(如 --script-file 模式)。
        """
        from . import assemble as assemble_mod
        from . import llm as llm_mod
        from .chains import metadata as metadata_mod

        chapters = assemble_mod.build_chapters(
            state["script"], state["scene_media"]
        )
        try:
            llm = llm_mod.get_chat_model(settings)
        except RuntimeError as exc:
            warnings.warn(
                f"metadata 降级为 meta=None(无法初始化 LLM): {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return {"chapters": chapters, "meta": None}
        meta = await metadata_mod.write_metadata(
            llm, state["script"], chapters, settings
        )
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


async def run_pipeline(
    settings: Settings,
    out_dir: Path,
    industry: str = "互联网",
    topic_title: str | None = None,
    script_file: Path | None = None,
) -> ProjectManifest:
    """async 便捷入口: 跑完整流水线, 返回 ProjectManifest(已落盘 project.json)。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    graph = build_graph(settings)
    initial: PipelineState = {
        "out_dir": out_dir,
        "industry": industry,
        "topic_title": topic_title,
        "script_file": Path(script_file) if script_file is not None else None,
    }
    final = await graph.ainvoke(initial)
    manifest_path = Path(final["manifest_path"])
    return ProjectManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
