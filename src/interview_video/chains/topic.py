"""选题链: 按行业生成求职面试短视频选题清单。"""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from ..models import Topic
from . import parse_json_lenient

# 内容红线(全链共用, 与 SPECS §2 一致)
RED_LINES = (
    "你是一名资深求职内容策划, 擅长为短视频平台策划高转化的求职面试内容。\n"
    "选题与文案必须真实、积极、可执行, 并严格遵守以下红线:\n"
    "1. 不编造具体薪资数字, 涉及待遇时用『因城市/公司差异大』类表述;\n"
    "2. 不含有年龄/性别/学历等歧视性内容;\n"
    "3. 不承诺『包过/必上岸』等结果;\n"
    "4. 不诋毁任何具体公司或个人。"
)


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


async def generate_topics(
    llm: BaseChatModel,
    industry: str,
    n: int = 5,
    avoid: list[str] | None = None,
) -> list[Topic]:
    """生成 n 个选题。主路径走 with_structured_output, 不支持的模型回退文本 JSON 解析。"""
    prompt = _build_prompt()
    payload = {
        "industry": industry,
        "n": n,
        "avoid": "、".join(avoid) if avoid else "无",
    }

    # 主路径: 结构化输出(假模型/不支持的 provider 会在构造期抛异常)
    try:
        structured = prompt | llm.with_structured_output(_TopicList)
        result = await structured.ainvoke(payload)
        if isinstance(result, _TopicList) and result.topics:
            return result.topics[:n]
    except Exception:
        pass  # 回退到文本 JSON 路径

    chain = prompt | llm | StrOutputParser()
    text = await chain.ainvoke(payload)
    return parse_topics_json(text)[:n]


def parse_topics_json(text: str) -> list[Topic]:
    """回退解析: 容忍围栏/前后废话, 兼容数组或 {{\"topics\": [...]}} 两种结构。"""
    data = parse_json_lenient(text)
    if isinstance(data, dict):
        data = data.get("topics")
    if not isinstance(data, list) or not data:
        raise ValueError(f"选题 JSON 应为非空数组: {text[:80]!r}")
    return [Topic.model_validate(item) for item in data]
