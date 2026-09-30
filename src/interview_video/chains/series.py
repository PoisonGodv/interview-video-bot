"""系列长视频链: 策划 → 分集大纲 → 章节写手 → 审稿(多 agent 角色)。

上下文切分策略(对应 SPECS §7):
- 章节是生成单元: 章节写手只看「系列圣经 + 本章规格 + 前章滚动摘要」,
  不看全集历史分镜, 控制单次上下文长度;
- 集边界全量重置: 新集只带系列圣经 + 本集规划, 不带上一集摘要;
- 滚动摘要由每章 ChapterDraft.summary 提供, 拼接后截断到 600 字。

成本护栏(不信任 LLM 自觉的确定性 clamp):
- 单章分镜数上限 MAX_SCENES_PER_CHAPTER, 防失控大纲一章产出 300 镜。
"""
from __future__ import annotations

import logging

from ..boundary import BoundaryRules
from ..config import Settings
from ..llm import ChatModel, LLMBudgetExceededError
from ..models import (
    ChapterDraft,
    ChapterSpec,
    EpisodeOutline,
    EpisodePlan,
    Scene,
    Script,
    SeriesPlan,
)
from ..prompts import ChatPromptTemplate
from . import parse_json_lenient
from ._invoke import invoke_structured, invoke_text
from .redlines import RED_LINES
from .script import MAX_NARRATION_CHARS, truncate_narration

logger = logging.getLogger(__name__)

# 滚动摘要窗口(字), 超出截断保尾
CONTEXT_WINDOW_CHARS = 600

# 单章分镜数上限(写手 prompt 的 scene_count 也被夹在此值内)
MAX_SCENES_PER_CHAPTER = 30

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
            "- 第 1 章必须以开场钩子切入(痛点/反常识观点/具体数字, 不要问候和自报家门), "
            "最后一章含总结、和本集主题相关的评论引导与关注引导;\n"
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
            "- 开头承接前文; 若前文摘要为「无」, 第 1 镜第 1 句不超过 20 字, "
            "禁止问候和自报家门(大家好/哈喽/嗨/我是/这里是), 直接用痛点、反常识观点或具体数字开场;\n"
            "- 讲到面试回答时, 给出「错误回答 vs 正确回答」的原话示例, 不要空讲道理;\n"
            "- 结尾留钩子给下一章;\n"
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
            "- 第 1 镜以问候或自报家门(大家好/哈喽/我是/这里是)开头的, 改成直接切入痛点;\n"
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
    llm: ChatModel, theme: str, episodes: int, episode_sec: int
) -> SeriesPlan:
    """策划 agent: 主题 → 系列规划(N 集 + 系列圣经)。"""
    payload = {
        "theme": theme,
        "episodes": episodes,
        "episode_min": round(episode_sec / 60),
    }
    plan = await invoke_structured(
        _PLAN_PROMPT, llm, payload, SeriesPlan, parse_series_plan_json,
        stage="series.plan", boundary_rules=BoundaryRules.non_script(),
    )
    assert isinstance(plan, SeriesPlan)
    return plan


async def write_episode_outline(
    llm: ChatModel, plan: SeriesPlan, episode: EpisodePlan, chapter_sec: int
) -> EpisodeOutline:
    """分集编剧: 一集规划 → 章节大纲。"""
    payload = {
        "synopsis": plan.synopsis,
        "episode_json": episode.model_dump_json(),
        "chapter_sec": chapter_sec,
        "episode_index": episode.index,
    }
    outline = await invoke_structured(
        _OUTLINE_PROMPT, llm, payload, EpisodeOutline, parse_outline_json,
        stage="series.outline", boundary_rules=BoundaryRules.non_script(),
    )
    assert isinstance(outline, EpisodeOutline)
    return outline


async def write_chapter(
    llm: ChatModel,
    plan: SeriesPlan,
    outline: EpisodeOutline,
    chapter: ChapterSpec,
    ch_no: int,
    prev_summary: str,
    settings: Settings,
) -> ChapterDraft:
    """章节写手: 本章规格 + 滚动摘要 → 分镜草稿。narration 逐镜确定性截断。"""
    # 成本护栏: 分镜数夹在 [4, MAX_SCENES_PER_CHAPTER], 防失控大纲
    scene_count = min(MAX_SCENES_PER_CHAPTER, max(4, round(chapter.est_sec / 20)))
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
    draft = await invoke_structured(
        _CHAPTER_PROMPT, llm, payload, ChapterDraft, parse_chapter_json,
        stage=f"series.ch{ch_no}", boundary_rules=BoundaryRules.non_script(),
    )
    assert isinstance(draft, ChapterDraft)
    return _fix_draft(draft)


async def review_chapter(
    llm: ChatModel,
    draft: ChapterDraft,
    chapter: ChapterSpec,
    prev_summary: str,
) -> ChapterDraft:
    """审稿 agent: 红线/重复/冗长修订; 审稿失败降级为原草稿(留痕, 不静默)。"""
    payload = {
        "chapter_json": chapter.model_dump_json(),
        "prev_summary": prev_summary or "无",
        "draft_json": draft.model_dump_json(),
    }
    try:
        reviewed = await invoke_text(
            _REVIEW_PROMPT, llm, payload, parse_chapter_json,
            stage="series.review",
        )
        if isinstance(reviewed, ChapterDraft) and reviewed.scenes:
            return _fix_draft(reviewed)
        logger.warning("[series.review] 审稿返回空草稿, 使用原草稿")
    except LLMBudgetExceededError:
        raise
    except Exception as exc:
        logger.warning(
            "[series.review] 审稿未生效, 使用未审稿草稿(%s: %.200s)",
            type(exc).__name__, exc,
        )
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


def build_episode_script(episode: EpisodePlan, outline: EpisodeOutline, scenes: list[Scene]) -> Script:
    """章节分镜汇总成单集 Script: 首镜 point 开场、末镜 ending(确定性)。"""
    if not scenes:
        raise ValueError(f"第 {episode.index} 集没有任何分镜")
    scenes[0].kind = "point"
    scenes[-1].kind = "ending"
    return Script(title=episode.title, scenes=scenes, summary=episode.summary)
