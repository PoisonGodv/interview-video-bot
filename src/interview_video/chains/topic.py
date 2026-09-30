"""选题链: 按行业生成求职面试短视频选题清单。"""
from __future__ import annotations

from pydantic import BaseModel

from ..boundary import BoundaryRules
from ..llm import ChatModel
from ..models import Topic
from ..prompts import ChatPromptTemplate
from . import parse_json_lenient
from ._invoke import invoke_structured
from .redlines import RED_LINES

__all__ = ["RED_LINES", "generate_topics", "parse_topics_json"]


class _TopicList(BaseModel):
    """结构化输出的包装模型(部分 provider 不支持 list[Model] 直出)。"""

    topics: list[Topic]


def _build_prompt() -> ChatPromptTemplate:
    """选题 prompt: 中文 system(含红线) + 用户任务说明。"""
    return ChatPromptTemplate.from_messages(
        [
            ("system", RED_LINES),
            (
                "user",
                "请为「{industry}」行业策划 {n} 个求职面试短视频选题。\n"
                "要求:\n"
                "- 每个选题输出 title / industry / audience(目标观众一句话) /\n"
                "  hook(3 秒内抓住观众的开场钩子) / key_points(内容要点 3~6 条);\n"
                "- 选题要具体、有差异化, 避免空泛;\n"
                "- 需要避开的已有选题: {avoid}\n"
                "只输出 JSON(数组, 或 {{\"topics\": [...]}} 对象), 不要输出其他文字。",
            ),
        ]
    )


def _parse_topic_list(text: str) -> _TopicList:
    """回退解析(包装成与结构化路径同型, 供 invoke_structured 统一处理)。"""
    return _TopicList(topics=parse_topics_json(text))


async def generate_topics(
    llm: ChatModel,
    industry: str,
    n: int = 5,
    avoid: list[str] | None = None,
) -> list[Topic]:
    """生成 n 个选题。统一走 invoke_structured: 结构化 → 本地重解析 → 修复重问。"""
    payload = {
        "industry": industry,
        "n": n,
        "avoid": "、".join(avoid) if avoid else "无",
    }
    result = await invoke_structured(
        _build_prompt(), llm, payload, _TopicList, _parse_topic_list,
        stage="topics", boundary_rules=BoundaryRules.non_script(),
    )
    assert isinstance(result, _TopicList)  # 两条路径都保证同型
    return result.topics[:n]


def parse_topics_json(text: str) -> list[Topic]:
    """回退解析: 容忍围栏/前后废话, 兼容数组或 {\"topics\": [...]} 两种结构。"""
    data = parse_json_lenient(text)
    if isinstance(data, dict):
        data = data.get("topics")
    if not isinstance(data, list) or not data:
        raise ValueError(f"选题 JSON 应为非空数组: {text[:80]!r}")
    return [Topic.model_validate(item) for item in data]
