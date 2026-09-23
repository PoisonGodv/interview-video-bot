"""脚本链: 由选题生成 5~8 个分镜的完整口播脚本(含 narration 字数校验修正)。"""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from ..config import Settings
from ..models import Script, Topic
from . import parse_json_lenient
from .topic import RED_LINES

# narration 字数约束(与 models.Scene.narration 描述一致: 每场景 65~110 字)
MAX_NARRATION_CHARS = 110
MIN_NARRATION_CHARS = 65

# 句末标点(优先截断点) 与 停顿标点(兜底截断点)
_SENTENCE_END = "。！？!?…"
_PAUSE_END = "，、,;；:："


def truncate_narration(
    text: str,
    max_chars: int = MAX_NARRATION_CHARS,
    min_chars: int = MIN_NARRATION_CHARS,
) -> str:
    """把超长口播截断到句读处, 保证结果 <= max_chars 且以句末标点收尾。

    截断策略(按优先级):
    1. 在 max_chars 窗口内找最后一个句末标点(。！？!?)截断;
    2. 首句仍超长时退到窗口内最后一个停顿标点(，、;；等), 并补为「。」;
    3. 找不到合适句读时硬截断到 max_chars, 去掉尾部停顿符并补「。」。

    不超过上限的文本原样返回。
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text

    window = text[:max_chars]
    # 1) 句末标点截断
    cut = max(window.rfind(p) for p in _SENTENCE_END)
    if cut + 1 >= min_chars:
        return window[: cut + 1]

    # 2) 停顿标点截断(收为句末)
    cut = max(window.rfind(p) for p in _PAUSE_END)
    if cut + 1 >= min_chars:
        return window[:cut] + "。"

    # 3) 硬截断兜底(预留 1 字给收尾句号, 保证总长度 <= max_chars)
    hard = window[: max_chars - 1].rstrip(_PAUSE_END + _SENTENCE_END)
    if not hard:
        hard = window[:max_chars].rstrip(_PAUSE_END)
    return hard + "。"


def fix_script_narration(
    script: Script,
    max_chars: int = MAX_NARRATION_CHARS,
    min_chars: int = MIN_NARRATION_CHARS,
) -> Script:
    """脚本校验修正(就地修改并返回同一对象):

    - 每个场景的 narration 超长时截断到句读处(见 truncate_narration);
    - 保证最后一镜 kind=ending(关注引导, 与 models.Script 契约一致)。

    空脚本抛 ValueError。
    """
    if not script.scenes:
        raise ValueError("脚本至少需要 1 个分镜")
    for scene in script.scenes:
        scene.narration = truncate_narration(
            scene.narration, max_chars=max_chars, min_chars=min_chars
        )
    if script.scenes[-1].kind != "ending":
        script.scenes[-1].kind = "ending"
    return script


def _build_prompt(brand: str, target_sec: int) -> ChatPromptTemplate:
    """脚本 prompt: 中文 system(角色+红线) + 分镜结构/字数/JSON 格式要求。"""
    return ChatPromptTemplate.from_messages(
        [
            ("system", RED_LINES),
            (
                "user",
                "选题信息(JSON):\n{topic_json}\n\n"
                f"请围绕该选题为「{brand}」写一条约 {target_sec} 秒的面试求职短视频脚本。\n"
                "要求:\n"
                "- 5~8 个分镜; 第 1 镜为 point 开场(必须包含选题 hook);\n"
                "  中间 point/qa 混排; 最后一镜 kind=ending(关注/下期引导);\n"
                "- 每镜字段: kind, header(画面标题 <=12字), "
                "bullets(屏显要点, 每条 <=14字, 0~4 条), narration(口播);\n"
                "- qa 镜额外填 question(面试问题)与 answer(答案要点屏显);\n"
                f"- narration 口语化, 每场景 {MIN_NARRATION_CHARS}~{MAX_NARRATION_CHARS} 字"
                f"(约 15~25 秒), 全片时长向 {target_sec} 秒靠拢;\n"
                "- 遵守 system 中的内容红线。\n\n"
                "只输出如下结构的 JSON(不要任何其他文字):\n"
                '{{"title": "...", "summary": "...", "scenes": ['
                '{{"kind": "point", "header": "...", "bullets": ["..."], '
                '"narration": "...", "question": null, "answer": null}}]}}',
            ),
        ]
    )


async def write_script(llm: BaseChatModel, topic: Topic, settings: Settings) -> Script:
    """由选题写脚本。主路径 with_structured_output, 失败回退文本 JSON 解析,
    两条路径都经过 fix_script_narration 校验修正。"""
    prompt = _build_prompt(settings.brand, settings.target_duration_sec)
    payload = {"topic_json": topic.model_dump_json()}

    try:
        structured = prompt | llm.with_structured_output(Script)
        result = await structured.ainvoke(payload)
        if isinstance(result, Script):
            return fix_script_narration(result)
    except Exception:
        pass  # 回退到文本 JSON 路径

    chain = prompt | llm | StrOutputParser()
    text = await chain.ainvoke(payload)
    return fix_script_narration(parse_script_json(text))


def parse_script_json(text: str) -> Script:
    """回退解析: 容忍围栏/前后废话, 直接 model_validate 为 Script。"""
    return Script.model_validate(parse_json_lenient(text))
