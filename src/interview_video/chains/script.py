"""脚本链: 由选题生成 5~8 个分镜的完整口播脚本(含 narration 字数校验修正)。"""
from __future__ import annotations

from ..config import Settings
from ..llm import ChatModel
from ..models import Script, Topic
from ..prompts import ChatPromptTemplate
from . import parse_json_lenient
from ._invoke import invoke_structured
from .redlines import RED_LINES

# narration 字数约束(与 models.Scene.narration 描述一致: 每场景 65~110 字)
MAX_NARRATION_CHARS = 110
MIN_NARRATION_CHARS = 65
# qa 镜屏显字数上限(与 boundary.BoundaryRules.max_question_chars/max_answer_chars 一致)
MAX_QUESTION_CHARS = 40
MAX_ANSWER_CHARS = 60

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
        return window[: cut] + "。"

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


# 与 boundary.structure.GREETING_OPENING_RE 同步
OPENING_GREETINGS_HINT = "大家好/哈喽/嗨/我是/这里是"

# few-shot 片段: 值直接进模板, 花括号必须写成 {{ }}
FEW_SHOT_EXAMPLE = (
    "参考示例(只学写法, 不要照抄内容):\n"
    '开场镜: {{"kind": "point", "header": "一开口就输了", '
    '"bullets": ["自我介绍", "别背简历"], '
    '"narration": "自我介绍背简历，等于直接放弃。面试官手里就拿着你的简历，'
    '你再念一遍只会让他走神。今天用三句话，教你把自我介绍讲成一段让人记住的故事。"}}\n'
    '要点镜: {{"kind": "qa", "header": "为什么离职", '
    '"bullets": ["不抱怨", "讲成长"], '
    '"question": "你为什么从上家公司离职？", "answer": "不吐槽前东家, 讲想要的成长", '
    '"narration": "被问为什么离职，错误回答是：“领导太难相处，天天加班。”'
    '正确回答是：“我在上家把订单系统从零做到稳定运行，现在想去更大的业务里挑战高并发。”'
    '一个在抱怨，一个在讲成长。"}}\n'
    '结尾镜: {{"kind": "ending", "header": "评论区见", "bullets": ["说说你的答案"], '
    '"narration": "你被问离职原因时是怎么答的？把你的原话写在评论区，我每期挑几条逐句帮你改。'
    '下期聊面试最后怎么反问面试官才加分，记得关注一下，别错过。"}}'
)


def _build_prompt() -> ChatPromptTemplate:
    """脚本 prompt: 中文 system(角色+红线) + 分镜结构/字数/JSON 格式要求。

    brand/target_sec 一律走模板变量: 值里的 ``{}`` 不会被二次格式化
    (旧实现 f-string 预拼, BRAND 含花括号时 KeyError)。
    """
    return ChatPromptTemplate.from_messages(
        [
            ("system", RED_LINES),
            (
                "user",
                "选题信息(JSON):\n{topic_json}\n\n"
                "请围绕该选题为「{brand}」写一条约 {target_sec} 秒的面试求职短视频脚本。\n"
                "要求:\n"
                "- 5~8 个分镜; 第 1 镜为 point 开场(必须包含选题 hook);\n"
                "  中间 point/qa 混排; 最后一镜 kind=ending;\n"
                "- 开头钩子(硬性): 第 1 镜 narration 的第 1 句不超过 20 字, "
                f"禁止问候和自报家门({OPENING_GREETINGS_HINT} 开头一律不合格), "
                "必须是痛点、反常识观点或具体数字, 让人 3 秒内想看下去;\n"
                "- 具体例子(硬性): 除开场镜外, 每个 point/qa 镜的 narration 都要给一组"
                "「错误回答 vs 正确回答」的原话示例, 用引号写出候选人会说的原话, 不要空讲道理;\n"
                "- 结尾: ending 镜给出和本选题相关的具体评论引导(抛一个观众能直接回答的问题), "
                "再做关注/下期引导;\n"
                "- 每镜字段: kind, header(画面标题 <=12字), "
                "bullets(屏显要点, 每条 <=14字, 0~4 条), narration(口播);\n"
                f"- qa 镜额外填 question(面试问题, <={MAX_QUESTION_CHARS}字)与 "
                f"answer(答案要点屏显, <={MAX_ANSWER_CHARS}字, 细节放进 narration);\n"
                f"- narration 口语化, 每场景 {MIN_NARRATION_CHARS}~{MAX_NARRATION_CHARS} 字"
                "(约 15~25 秒), 全片时长向 {target_sec} 秒靠拢;\n"
                "- 遵守 system 中的内容红线。\n\n"
                f"{FEW_SHOT_EXAMPLE}\n\n"
                "只输出如下结构的 JSON(不要任何其他文字):\n"
                '{{"title": "...", "summary": "...", "scenes": ['
                '{{"kind": "point", "header": "...", "bullets": ["..."], '
                '"narration": "...", "question": null, "answer": null}}]}}',
            ),
        ]
    )


async def write_script(llm: ChatModel, topic: Topic, settings: Settings) -> Script:
    """由选题写脚本。invoke_structured 三级策略, 结果经 fix_script_narration 修正。"""
    payload = {
        "topic_json": topic.model_dump_json(),
        "brand": settings.brand,
        "target_sec": settings.target_duration_sec,
    }
    script = await invoke_structured(
        _build_prompt(), llm, payload, Script, parse_script_json,
        stage="script",
    )
    assert isinstance(script, Script)
    return fix_script_narration(script)


def parse_script_json(text: str) -> Script:
    """回退解析: 容忍围栏/前后废话, 直接 model_validate 为 Script。"""
    return Script.model_validate(parse_json_lenient(text))
