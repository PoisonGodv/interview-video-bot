"""系列长视频编排: 原生工作流引擎多 agent 双循环(集循环 × 章节循环)。

图结构:
    plan → episode_setup ──(还有集)──▶ outline → chapter ──(还有章)──▶ chapter
                  ▲                        │(本章含 review 子步骤)      │
                  └──── build_script ◀─────┴──(章完)─────────────────────┘
                  │(集完) → END

事件回调 on_event(name, **payload) 供 CLI/Web 显示进度:
    plan_done / outline_done / chapter_done / script_done

崩溃恢复(增量落盘 + resume):
- 传 run_dir 时, 每个节点完成即把产物写到 ``<run_dir>/planning/`` 下
  (series.json / epXX/outline.json / epXX/drafts/chXX.json / epXX/script.json),
  中途失败不丢已完成章节的 LLM 成本;
- ``generate_series(..., resume=True)`` 从磁盘重建图状态:
  已有 script.json 的集直接跳过, 写到一半的集按 outline+drafts 续写,
  plan 沿用磁盘版本(不再调策划 LLM)。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

from .chains import series as series_chains
from .config import Settings
from .models import ChapterDraft, EpisodeOutline, Scene, Script, SeriesPlan
from .workflow_engine import END, START, CompiledWorkflow, RunContext, WorkflowEngine

logger = logging.getLogger(__name__)

EventFn = Callable[..., None]

# 章节数/时长护栏: 失控大纲会被确定性截断, 防单章 300 镜成本失控
_MIN_CHAPTERS = 4
_EST_SEC_RANGE = (60, 600)


class SeriesState(TypedDict, total=False):
    theme: str
    episodes: int          # 目标集数
    episode_sec: int       # 每集目标时长(秒)
    plan: SeriesPlan
    ep_idx: int            # 当前集下标(0 起)
    outline: EpisodeOutline
    ch_idx: int            # 当前章下标(0 起)
    prev_summary: str      # 滚动摘要(集内)
    scenes: list[Scene]    # 当前集已生成分镜
    scripts: list[Script]  # 已完成集的脚本


def _ep_plan_dir(run_dir: Path, ep_idx: int) -> Path:
    """第 ep_idx(0 起) 集的规划产物目录。"""
    return run_dir / "planning" / f"ep{ep_idx + 1:02d}"


def _clamp_outline(outline: EpisodeOutline, ep_target_sec: int, chapter_sec: int) -> EpisodeOutline:
    """确定性护栏: 章数截断 / est_sec 夹范围。"""
    max_chapters = max(_MIN_CHAPTERS, math.ceil(ep_target_sec / max(1, chapter_sec)) + 2)
    if len(outline.chapters) > max_chapters:
        logger.warning(
            "大纲章数 %d 超上限 %d, 截断(失控大纲成本护栏)",
            len(outline.chapters), max_chapters,
        )
        outline.chapters = outline.chapters[:max_chapters]
    lo, hi = _EST_SEC_RANGE
    for ch in outline.chapters:
        ch.est_sec = min(hi, max(lo, ch.est_sec))
    return outline


def build_series_graph(
    settings: Settings, on_event: EventFn | None = None, run_dir: Path | None = None
) -> CompiledWorkflow:
    """构建系列生成图。settings/on_event/run_dir 由闭包捕获, 不入状态。"""

    def emit(name: str, **payload: Any) -> None:
        if on_event:
            on_event(name, **payload)

    def _llm():
        from . import llm as llm_mod

        return llm_mod.get_chat_model(settings)

    async def plan_node(state: SeriesState) -> SeriesState:
        """策划 agent: 主题 → 系列规划。resume 时沿用磁盘上的规划, 不再调 LLM。"""
        if state.get("plan") is not None:  # resume: 状态已从磁盘重建
            logger.info("resume: 沿用磁盘上的系列规划《%s》", state["plan"].title)
            return {}
        plan = await series_chains.plan_series(
            _llm(), state["theme"], state["episodes"], state["episode_sec"]
        )
        # 目标时长以用户参数为准(LLM 可能自由发挥)
        plan.episodes = plan.episodes[: state["episodes"]]
        for ep in plan.episodes:
            ep.target_sec = state["episode_sec"]
        if run_dir is not None:  # plan 一出即落盘, 后续任何失败都不丢策划成本
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "series.json").write_text(
                plan.model_dump_json(indent=2), encoding="utf-8"
            )
        emit("plan_done", plan=plan)
        return {"plan": plan, "ep_idx": 0, "scripts": []}

    async def episode_setup_node(state: SeriesState) -> SeriesState:
        """集边界: 重置集内上下文(滚动摘要/分镜), 只带系列圣经进新集。"""
        return {"prev_summary": "", "scenes": [], "ch_idx": 0}

    async def outline_node(state: SeriesState) -> SeriesState:
        """分集编剧: 本集规划 → 章节大纲(带章数/时长确定性护栏)。"""
        plan = state["plan"]
        ep = plan.episodes[state["ep_idx"]]
        outline = await series_chains.write_episode_outline(
            _llm(), plan, ep, settings.series_chapter_sec
        )
        outline.episode_index = ep.index
        outline = _clamp_outline(outline, ep.target_sec, settings.series_chapter_sec)
        if run_dir is not None:
            d = _ep_plan_dir(run_dir, state["ep_idx"])
            d.mkdir(parents=True, exist_ok=True)
            (d / "outline.json").write_text(
                outline.model_dump_json(indent=2), encoding="utf-8"
            )
        emit("outline_done", ep_idx=state["ep_idx"], outline=outline)
        return {"outline": outline}

    async def chapter_node(state: SeriesState) -> SeriesState:
        """章节写手 + 审稿(可关): 规格+滚动摘要 → 分镜, 追加并更新滚动摘要。"""
        plan = state["plan"]
        outline = state["outline"]
        ch_idx = state["ch_idx"]
        chapter = outline.chapters[ch_idx]
        llm = _llm()
        draft = await series_chains.write_chapter(
            llm, plan, outline, chapter, ch_idx + 1, state["prev_summary"], settings
        )
        if settings.series_review:
            draft = await series_chains.review_chapter(
                llm, draft, chapter, state["prev_summary"]
            )
        if run_dir is not None:  # 每章落盘(审稿后版本), 失败可从此续
            drafts_dir = _ep_plan_dir(run_dir, state["ep_idx"]) / "drafts"
            drafts_dir.mkdir(parents=True, exist_ok=True)
            (drafts_dir / f"ch{ch_idx + 1:02d}.json").write_text(
                draft.model_dump_json(indent=2), encoding="utf-8"
            )
        scenes = state["scenes"] + draft.scenes
        prev = series_chains.roll_context(state["prev_summary"], draft)
        emit(
            "chapter_done",
            ep_idx=state["ep_idx"], ch_idx=ch_idx,
            ch_total=len(outline.chapters), draft=draft,
        )
        return {"scenes": scenes, "prev_summary": prev, "ch_idx": ch_idx + 1}

    async def build_script_node(state: SeriesState) -> SeriesState:
        """集收尾: 汇总分镜成 Script(首镜开场/末镜 ending), 追加到 scripts。"""
        plan = state["plan"]
        ep = plan.episodes[state["ep_idx"]]
        script = series_chains.build_episode_script(ep, state["outline"], state["scenes"])
        if run_dir is not None:
            d = _ep_plan_dir(run_dir, state["ep_idx"])
            d.mkdir(parents=True, exist_ok=True)
            (d / "script.json").write_text(
                script.model_dump_json(indent=2), encoding="utf-8"
            )
        emit("script_done", ep_idx=state["ep_idx"], script=script)
        return {"scripts": state["scripts"] + [script], "ep_idx": state["ep_idx"] + 1}

    def _has_episode(state: SeriesState) -> str:
        return "outline" if state["ep_idx"] < len(state["plan"].episodes) else END

    def _has_chapter(state: SeriesState) -> str:
        return "chapter" if state["ch_idx"] < len(state["outline"].chapters) else "build_script"

    graph = WorkflowEngine(SeriesState)
    graph.add_node("plan", plan_node)
    graph.add_node("episode_setup", episode_setup_node)
    graph.add_node("outline", outline_node)
    graph.add_node("chapter", chapter_node)
    graph.add_node("build_script", build_script_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "episode_setup")
    graph.add_conditional_edges("episode_setup", _has_episode)
    graph.add_edge("outline", "chapter")
    graph.add_conditional_edges("chapter", _has_chapter)
    graph.add_edge("build_script", "episode_setup")
    return graph.compile()


def _restore_state(run_dir: Path, episodes: int) -> dict[str, Any]:
    """从 run_dir 的 planning/ 产物重建图状态(供 generate_series resume)。

    - series.json 缺失 → 抛 FileNotFoundError(调用方应走全新生成);
    - epXX/script.json 存在 → 该集完成, 进 scripts;
    - epXX/outline.json 存在但缺 script.json → 写到一半:
      按 drafts 重建 scenes 与滚动摘要, 从下一章续写。
    """
    run_dir = Path(run_dir)
    plan = SeriesPlan.model_validate_json(
        (run_dir / "series.json").read_text(encoding="utf-8")
    )
    plan.episodes = plan.episodes[:episodes]
    scripts: list[Script] = []
    state: dict[str, Any] = {"plan": plan, "scripts": scripts, "ep_idx": 0}

    for ep_idx in range(len(plan.episodes)):
        d = _ep_plan_dir(run_dir, ep_idx)
        script_file = d / "script.json"
        if script_file.exists():
            scripts.append(
                Script.model_validate_json(script_file.read_text(encoding="utf-8"))
            )
            state["ep_idx"] = ep_idx + 1
            continue
        outline_file = d / "outline.json"
        if not outline_file.exists():
            break  # 这集还没开始写
        outline = EpisodeOutline.model_validate_json(
            outline_file.read_text(encoding="utf-8")
        )
        scenes: list[Scene] = []
        prev = ""
        ch_idx = 0
        drafts_dir = d / "drafts"
        while (drafts_dir / f"ch{ch_idx + 1:02d}.json").exists() and ch_idx < len(outline.chapters):
            draft = ChapterDraft.model_validate_json(
                (drafts_dir / f"ch{ch_idx + 1:02d}.json").read_text(encoding="utf-8")
            )
            scenes.extend(draft.scenes)
            prev = series_chains.roll_context(prev, draft)
            ch_idx += 1
        state.update(
            ep_idx=ep_idx, outline=outline, scenes=scenes,
            prev_summary=prev, ch_idx=ch_idx,
        )
        logger.info(
            "resume: 第 %d 集续写(已写 %d/%d 章)",
            ep_idx + 1, ch_idx, len(outline.chapters),
        )
        break  # 只恢复到第一个未完成集, 后面的集重新生成

    logger.info(
        "resume: 已完成 %d 集, 从第 %d 集继续", len(scripts), len(scripts) + 1
    )
    return state


async def generate_series(
    settings: Settings,
    theme: str,
    episodes: int = 3,
    episode_sec: int = 900,
    on_event: EventFn | None = None,
    run_dir: Path | None = None,
    resume: bool = False,
) -> tuple[SeriesPlan, list[Script]]:
    """跑系列生成图, 返回 (系列规划, 每集脚本)。

    run_dir: 系列根目录; 传入后每个节点完成即增量落盘(planning/)。
    resume=True 时从 run_dir 重建状态续跑(需要 series.json 存在)。
    """
    run_dir = Path(run_dir) if run_dir is not None else None
    initial: dict[str, Any] = {
        "theme": theme,
        "episodes": episodes,
        "episode_sec": episode_sec,
        "ep_idx": 0,
        "prev_summary": "",
        "scenes": [],
        "scripts": [],
    }
    if resume and run_dir is not None and (run_dir / "series.json").exists():
        initial.update(_restore_state(run_dir, episodes))
    graph = build_series_graph(settings, on_event, run_dir=run_dir)
    from .llm_audit import LLMCallAudit, run_budget

    context = RunContext(audit=LLMCallAudit(run_dir) if run_dir is not None else None)
    with run_budget(settings):  # 超限抛 LLMBudgetExceededError; 已完成章节已落盘, 可 resume
        final = await graph.run(initial, context=context)
    return final["plan"], final["scripts"]
