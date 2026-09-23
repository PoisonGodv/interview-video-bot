"""元数据链: 由脚本与章节生成 B 站投稿元数据(标题/简介/标签)。"""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from ..config import Settings
from ..models import BiliMeta, Chapter, Script
from . import parse_json_lenient
from .topic import RED_LINES

# 标题长度上限(与 models.BiliMeta 描述一致)
MAX_TITLE_CHARS = 80
# 简介结尾固定声明行(SPECS §2 规定, 必须原样保留)
AI_DECLARATION = "* 本视频由 AI 辅助生成"
# 标签数量约束
MIN_TAGS = 5
MAX_TAGS = 10
# 标签兜底池(不足时按序补齐, 保证数量与「面试」必含)
_DEFAULT_TAGS = ["求职", "面试技巧", "求职经验", "干货分享", "职场"]


def _mmss(seconds: float) -> str:
    """秒 → MM:SS(B 站章节行格式)。"""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _build_prompt(brand: str, chapter_lines: str) -> ChatPromptTemplate:
    """元数据 prompt: 中文 system(角色+红线) + 标题/简介/标签要求。"""
    return ChatPromptTemplate.from_messages(
        [
            ("system", RED_LINES),
            (
                "user",
                f"品牌: {brand}\n"
                "视频标题: {title}\n"
                "视频内容概要: {summary}\n\n"
                "章节行(必须原样逐行写进 desc, 格式 MM:SS 标题):\n"
                f"{chapter_lines}\n\n"
                "请为该视频生成 B 站投稿元数据 JSON, 要求:\n"
                f"- title: <= {MAX_TITLE_CHARS} 字, B 站风格(数字/悬念/利益点), "
                "不做标题党、不造谣;\n"
                "- desc: 开头一两句介绍视频价值, 然后原样包含上面的章节行,"
                f"最后一行固定为「{AI_DECLARATION}」;\n"
                f"- tags: {MIN_TAGS}~{MAX_TAGS} 个, 必须包含「面试」及行业相关词。\n\n"
                '只输出 JSON: {{"title": "...", "desc": "...", "tags": ["..."]}}',
            ),
        ]
    )


def _finalize_meta(meta: BiliMeta) -> BiliMeta:
    """确定性修正(不依赖 LLM 自觉):

    - title 超长截断到 80 字;
    - desc 末尾确保固定 AI 声明行;
    - tags 去重去空白、必含「面试」、不足 5 个用兜底池补齐、超过 10 个截断。
    """
    meta.title = meta.title.strip()[:MAX_TITLE_CHARS]

    desc = meta.desc.rstrip()
    if AI_DECLARATION not in desc.splitlines():
        desc = f"{desc}\n{AI_DECLARATION}" if desc else AI_DECLARATION
    meta.desc = desc

    tags: list[str] = []
    for tag in meta.tags:
        tag = tag.strip()
        if tag and tag not in tags:
            tags.append(tag)
    if "面试" not in tags:
        tags.insert(0, "面试")
    for fallback in _DEFAULT_TAGS:
        if len(tags) >= MIN_TAGS:
            break
        if fallback not in tags:
            tags.append(fallback)
    meta.tags = tags[:MAX_TAGS]
    return meta


async def write_metadata(
    llm: BaseChatModel,
    script: Script,
    chapters: list[Chapter],
    settings: Settings,
) -> BiliMeta:
    """生成 B 站元数据。主路径 with_structured_output, 失败回退文本 JSON 解析,
    两条路径都经过 _finalize_meta 确定性修正。"""
    chapter_lines = "\n".join(f"{_mmss(c.start)} {c.title}" for c in chapters)
    prompt = _build_prompt(settings.brand, chapter_lines)
    payload = {
        "title": script.title,
        "summary": script.summary or script.scenes[0].narration if script.scenes else script.title,
    }

    try:
        structured = prompt | llm.with_structured_output(BiliMeta)
        result = await structured.ainvoke(payload)
        if isinstance(result, BiliMeta):
            return _finalize_meta(result)
    except Exception:
        pass  # 回退到文本 JSON 路径

    chain = prompt | llm | StrOutputParser()
    text = await chain.ainvoke(payload)
    return _finalize_meta(parse_metadata_json(text))


def parse_metadata_json(text: str) -> BiliMeta:
    """回退解析: 容忍围栏/前后废话, 直接 model_validate 为 BiliMeta。"""
    return BiliMeta.model_validate(parse_json_lenient(text))
