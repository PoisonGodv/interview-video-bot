"""系列长视频链: 策划 → 分集大纲 → 章节写手 → 审稿(多 agent 角色)。

上下文切分策略(对应 SPECS §7):
- 章节是生成单元: 章节写手只看「系列圣经 + 本章规格 + 前章滚动摘要」,
  不看全集历史分镜, 控制单次上下文长度;
- 集边界全量重置: 新集只带系列圣经 + 本集规划, 不带上一集摘要;
- 滚动摘要由每章 ChapterDraft.summary 提供, 拼接后截断到 600 字。
"""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from ..config import Settings
from ..models import (
    ChapterDraft,
    ChapterSpec,
    EpisodeOutline,
    EpisodePlan,
    Scene,
    SeriesPlan,
)
from . import parse_json_lenient
from .script import MAX_NARRATION_CHARS, truncate_narration
from .topic import RED_LINES

# 滚动摘要窗口(字), 超出截断保尾
CONTEXT_WINDOW_CHARS = 600

_PLAN_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", RED_LINES),
        (
            "user",
            "请把主题「{theme}」策划成一个面试求职系列视频, 共 {episodes} 集, "
            "每集约 {episode_min} 分钟。\n"
            "要求:\n"
            "- 各集有清晰递进关系(由浅入深/按流程推进), 不重复;\n"
            "- 每集: index(从1开始) / title / summary(本集概要) / key_points(3~6条) / target_sec(秒);\n"
            "- 系列级: title(系列名) / synopsis(系列圣经: 主线+风格+受众, 200字内, "
            "后续每集每章都会共享它保持连贯);\n"
            "只输出 JSON: {{\"theme\": ..., \"title\": ..., \"synopsis\": ..., "
            "\"episodes\": [...]}}, 不要其他文字。",
        ),
    ]
)

_OUTLINE_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", RED_LINES),
        (
            "user",
            "系列圣经: {synopsis}\n\n"
            "本集规划(JSON): {episode_json}\n\n"
            "请为本集拆分章节大纲(本章是后续逐章生成的上下文切分单元)。\n"
            "要求:\n"
            "- 每章约 {chapter_sec} 秒, 章数 = 本集 target_sec / {chapter_sec} 取整;\n"
            "- 第 1 章必须含开场钩子与自我介绍式引入, 最后一章含总结与关注引导;\n"
            "- 每章: title / goal(本章讲清什么) / key_points(2~5条) / est_sec;\n"
            "- 章节间有承接关系, 不重复讲同一要点。\n"
            "只输出 JSON: {{\"episode_index\": {episode_index}, \"title\": ..., "
            "\"chapters\": [...]}}, 不要其他文字。",
        ),
    ]
)

_CHAPTER_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", RED_LINES),
        (
            "user",
            "系列圣经: {synopsis}\n\n"
            "本集标题: {episode_title}\n"
            "本章规格(JSON): {chapter_json}\n"
            "本章是全集第 {ch_no}/{ch_total} 章。\n\n"
            "前文内容摘要(供承接, 不要重复讲): {prev_summary}\n\n"
            "请为本章写分镜(画面+口播):\n"
            "- 约 {scene_count} 个分镜, kind 只用 point 或 qa(开场/结尾由框架处理);\n"
            "- 每镜: kind / header(<=12字) / bullets(每条<=14字, 0~4条) / narration(口播 "
            "{min_narr}~{max_narr} 字, 口语化); qa 镜额外填 question 与 answer;\n"
            "- 开头承接前文(若前文摘要为「无」则自然开场), 结尾留钩子给下一章;\n"
            "- summary 字段: 本章内容摘要 <=120 字(将传给下一章做上下文)。\n"
            "只输出 JSON: {{\"scenes\": [...], \"summary\": \"...\"}}, 不要其他文字。",
        ),
    ]
)

_REVIEW_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", RED_LINES),
        (
            "user",
            "你是审稿编辑。本章规格(JSON): {chapter_json}\n"
            "前文摘要: {prev_summary}\n"
            "章节草稿(JSON): {draft_json}\n\n"
            "请审校并直接输出修订后的草稿 JSON(结构不变, 只含 scenes 与 summary):\n"
            "- 触碰内容红线的表述必须改写;\n"
            "- 与前文摘要重复的整块内容删改;\n"
            "- narration 口语化、无书面语病, 超长句子拆开;\n"
            "- header/bullets 超长(12/14 字)的精简;\n"
            "- 无问题则原样返回。\n"
            "只输出 JSON, 不要其他文字。",
        ),
    ]
)


# ---------------------------------------------------------------------------
# 解析(回退路径, 可单测)
# ---------------------------------------------------------------------------

def parse_series_plan_json(text: str) -> SeriesPlan:
    return SeriesPlan.model_validate(parse_json_lenient(text))


def parse_outline_json(text: str) -> EpisodeOutline:
    return EpisodeOutline.model_validate(parse_json_lenient(text))


def parse_chapter_json(text: str) -> ChapterDraft:
    return ChapterDraft.model_validate(parse_json_lenient(text))


# ---------------------------------------------------------------------------
# 链
# ---------------------------------------------------------------------------

async def plan_series(
    llm: BaseChatModel, theme: str, episodes: int, episode_sec: int
) -> SeriesPlan:
    """策划 agent: 主题 → 系列规划(N 集 + 系列圣经)。"""
    payload = {
        "theme": theme,
        "episodes": episodes,
        "episode_min": round(episode_sec / 60),
    }
    try:
        structured = _PLAN_PROMPT | llm.with_structured_output(SeriesPlan)
        result = await structured.ainvoke(payload)
        if isinstance(result, SeriesPlan) and result.episodes:
            return result
    except Exception:
        pass
    text = await (_PLAN_PROMPT | llm | StrOutputParser()).ainvoke(payload)
    return parse_series_plan_json(text)


async def write_episode_outline(
    llm: BaseChatModel, plan: SeriesPlan, episode: EpisodePlan, chapter_sec: int
) -> EpisodeOutline:
    """分集编剧: 一集规划 → 章节大纲。"""
    payload = {
        "synopsis": plan.synopsis,
        "episode_json": episode.model_dump_json(),
        "chapter_sec": chapter_sec,
        "episode_index": episode.index,
    }
    try:
        structured = _OUTLINE_PROMPT | llm.with_structured_output(EpisodeOutline)
        result = await structured.ainvoke(payload)
        if isinstance(result, EpisodeOutline) and result.chapters:
            return result
    except Exception:
        pass
    text = await (_OUTLINE_PROMPT | llm | StrOutputParser()).ainvoke(payload)
    return parse_outline_json(text)


async def write_chapter(
    llm: BaseChatModel,
    plan: SeriesPlan,
    outline: EpisodeOutline,
    chapter: ChapterSpec,
    ch_no: int,
    prev_summary: str,
    settings: Settings,
) -> ChapterDraft:
    """章节写手: 本章规格 + 滚动摘要 → 分镜草稿。narration 逐镜确定性截断。"""
    scene_count = max(4, round(chapter.est_sec / 20))
    payload = {
        "synopsis": plan.synopsis,
        "episode_title": outline.title,
        "chapter_json": chapter.model_dump_json(),
        "ch_no": ch_no,
        "ch_total": len(outline.chapters),
        "prev_summary": prev_summary or "无",
        "scene_count": scene_count,
        "min_narr": 65,
        "max_narr": MAX_NARRATION_CHARS,
    }
    draft: ChapterDraft | None = None
    try:
        structured = _CHAPTER_PROMPT | llm.with_structured_output(ChapterDraft)
        result = await structured.ainvoke(payload)
        if isinstance(result, ChapterDraft) and result.scenes:
            draft = result
    except Exception:
        pass
    if draft is None:
        text = await (_CHAPTER_PROMPT | llm | StrOutputParser()).ainvoke(payload)
        draft = parse_chapter_json(text)
    return _fix_draft(draft)


async def review_chapter(
    llm: BaseChatModel,
    draft: ChapterDraft,
    chapter: ChapterSpec,
    prev_summary: str,
) -> ChapterDraft:
    """审稿 agent: 红线/重复/冗长修订; 审稿失败降级为原草稿。"""
    payload = {
        "chapter_json": chapter.model_dump_json(),
        "prev_summary": prev_summary or "无",
        "draft_json": draft.model_dump_json(),
    }
    try:
        text = await (_REVIEW_PROMPT | llm | StrOutputParser()).ainvoke(payload)
        reviewed = parse_chapter_json(text)
        if reviewed.scenes:
            return _fix_draft(reviewed)
    except Exception:
        pass
    return draft


def _fix_draft(draft: ChapterDraft) -> ChapterDraft:
    """确定性修正: narration 截断; kind 只留 point/qa。"""
    for scene in draft.scenes:
        scene.narration = truncate_narration(scene.narration)
        if scene.kind not in ("point", "qa"):
            scene.kind = "point"
    return draft


def roll_context(prev_summary: str, draft: ChapterDraft) -> str:
    """滚动摘要: 拼接本章摘要并保尾截断到 CONTEXT_WINDOW_CHARS。"""
    merged = (prev_summary + " | " + draft.summary).strip(" |")
    return merged[-CONTEXT_WINDOW_CHARS:]


def build_episode_script(episode: EpisodePlan, outline: EpisodeOutline, scenes: list[Scene]) -> "Script":
    """章节分镜汇总成单集 Script: 首镜 point 开场、末镜 ending(确定性)。"""
    from ..models import Script

    if not scenes:
        raise ValueError(f"第 {episode.index} 集没有任何分镜")
    scenes[0].kind = "point"
    scenes[-1].kind = "ending"
    return Script(title=episode.title, scenes=scenes, summary=episode.summary)
