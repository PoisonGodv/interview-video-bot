"""系列长视频编排: LangGraph 多 agent 双循环(集循环 × 章节循环)。

图结构:
    plan → episode_setup ──(还有集)──▶ outline → chapter ──(还有章)──▶ chapter
                  ▲                        │(本章含 review 子步骤)      │
                  └──── build_script ◀─────┴──(章完)─────────────────────┘
                  │(集完) → END

事件回调 on_event(name, **payload) 供 CLI/Web 显示进度:
    plan_done / outline_done / chapter_done / script_done
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .chains import series as series_chains
from .config import Settings
from .models import ChapterDraft, EpisodeOutline, Scene, Script, SeriesPlan

logger = logging.getLogger(__name__)

EventFn = Callable[..., None]


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


def build_series_graph(
    settings: Settings, on_event: EventFn | None = None
) -> CompiledStateGraph:
    """构建系列生成图。settings/on_event 由闭包捕获, 不入状态。"""

    def emit(name: str, **payload: Any) -> None:
        if on_event:
            on_event(name, **payload)

    def _llm():
        from . import llm as llm_mod

        return llm_mod.get_chat_model(settings)

    async def plan_node(state: SeriesState) -> SeriesState:
        """策划 agent: 主题 → 系列规划。"""
        plan = await series_chains.plan_series(
            _llm(), state["theme"], state["episodes"], state["episode_sec"]
        )
        # 目标时长以用户参数为准(LLM 可能自由发挥)
        plan.episodes = plan.episodes[: state["episodes"]]
        for ep in plan.episodes:
            ep.target_sec = state["episode_sec"]
        emit("plan_done", plan=plan)
        return {"plan": plan, "ep_idx": 0, "scripts": []}

    async def episode_setup_node(state: SeriesState) -> SeriesState:
        """集边界: 重置集内上下文(滚动摘要/分镜), 只带系列圣经进新集。"""
        return {"prev_summary": "", "scenes": [], "ch_idx": 0}

    async def outline_node(state: SeriesState) -> SeriesState:
        """分集编剧: 本集规划 → 章节大纲。"""
        plan = state["plan"]
        ep = plan.episodes[state["ep_idx"]]
        outline = await series_chains.write_episode_outline(
            _llm(), plan, ep, settings.series_chapter_sec
        )
        outline.episode_index = ep.index
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
        emit("script_done", ep_idx=state["ep_idx"], script=script)
        return {"scripts": state["scripts"] + [script], "ep_idx": state["ep_idx"] + 1}

    def _has_episode(state: SeriesState) -> str:
        return "outline" if state["ep_idx"] < len(state["plan"].episodes) else END

    def _has_chapter(state: SeriesState) -> str:
        return "chapter" if state["ch_idx"] < len(state["outline"].chapters) else "build_script"

    graph = StateGraph(SeriesState)
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


async def generate_series(
    settings: Settings,
    theme: str,
    episodes: int = 3,
    episode_sec: int = 900,
    on_event: EventFn | None = None,
) -> tuple[SeriesPlan, list[Script]]:
    """跑系列生成图, 返回 (系列规划, 每集脚本)。"""
    graph = build_series_graph(settings, on_event)
    final = await graph.ainvoke(
        {
            "theme": theme,
            "episodes": episodes,
            "episode_sec": episode_sec,
            "ep_idx": 0,
            "prev_summary": "",
            "scenes": [],
            "scripts": [],
        }
    )
    return final["plan"], final["scripts"]
