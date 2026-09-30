"""生成质量评测(evals): 确定性契约检查 + 红线扫描 + 可选 LLM-as-judge。

单测(fake 模型)保障的是"代码不崩", 本模块保障的是"内容合格":
- check_script_contract: 对 write_script 产物做机械契约检查(分镜数/字数/结尾镜);
- scan_redlines: 红线扫描(规则源 boundary.REDLINE_PATTERNS, 与生成闸门同源);
- judge_script: 同模型 LLM-as-judge, 对钩子强度/口语化 1~5 打分(可选);
- run_evals: 跑 golden 用例集, 汇总报告(供 ``ivb eval`` 与回归对比)。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from .chains.redlines import RED_LINES, scan_redlines
from .chains.script import (
    MAX_NARRATION_CHARS,
    MIN_NARRATION_CHARS,
    write_script,
)
from .config import Settings
from .models import Script, Topic
from .prompts import ChatPromptTemplate

logger = logging.getLogger(__name__)

# golden 用例集: (industry, topic_title)。真实模型跑, 结果与基线对比。
GOLDEN_CASES: list[dict[str, str]] = [
    {"industry": "互联网", "topic_title": "程序员面试自我介绍"},
    {"industry": "互联网", "topic_title": "应届生没有项目经验怎么办"},
    {"industry": "护理", "topic_title": "护士面试常见问题"},
    {"industry": "汽修", "topic_title": "转行学汽修值得吗"},
    {"industry": "教师", "topic_title": "教师结构化面试技巧"},
    {"industry": "互联网", "topic_title": "被问离职原因怎么答"},
]

_JUDGE_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", RED_LINES),
        (
            "user",
            "你是短视频内容评委。以下是生成的口播脚本(JSON):\n{script_json}\n\n"
            "请严格打分(1~5 整数):\n"
            "- hook: 第 1 镜口播 3 秒内抓住目标观众的能力;\n"
            "- colloquial: 口播口语化程度(5=像人在说话, 1=书面语);\n"
            "- coherence: 分镜间逻辑连贯性。\n"
            '只输出 JSON: {{"hook": n, "colloquial": n, "coherence": n}}',
        ),
    ]
)


def check_script_contract(script: Script) -> list[str]:
    """机械契约检查, 返回违规描述列表(空 = 全部通过)。"""
    issues: list[str] = []
    if not (5 <= len(script.scenes) <= 8):
        issues.append(f"分镜数 {len(script.scenes)} 不在 5~8 契约范围")
    for i, scene in enumerate(script.scenes):
        if len(scene.header) > 12:
            issues.append(f"场景{i} header 超 12 字: {scene.header!r}")
        for b in scene.bullets:
            if len(b) > 14:
                issues.append(f"场景{i} bullet 超 14 字: {b!r}")
        if len(scene.narration) > MAX_NARRATION_CHARS:
            issues.append(f"场景{i} narration 超 {MAX_NARRATION_CHARS} 字")
        elif len(scene.narration) < MIN_NARRATION_CHARS:
            issues.append(f"场景{i} narration 不足 {MIN_NARRATION_CHARS} 字(口播过短)")
    if script.scenes and script.scenes[-1].kind != "ending":
        issues.append("末镜 kind 不是 ending")
    full_text = "\n".join(
        [script.title] + [s.narration for s in script.scenes]
    )
    issues.extend(f"红线[{r}]" for r in scan_redlines(full_text))
    return issues


async def judge_script(llm: Any, script: Script) -> dict[str, int]:
    """LLM-as-judge 打分(hook/colloquial/coherence, 各 1~5)。解析失败抛 ValueError。"""
    messages = _JUDGE_PROMPT.format_messages(script_json=script.model_dump_json())
    if hasattr(llm, "ainvoke"):
        resp = await llm.ainvoke(messages)
    elif callable(llm):
        resp = await llm(messages)
    else:
        raise TypeError(f"llm 不支持调用: {type(llm)}")
    text = str(getattr(resp, "content", resp))
    from .chains import parse_json_lenient

    data = parse_json_lenient(text)
    return {k: int(data[k]) for k in ("hook", "colloquial", "coherence")}


async def eval_case(
    settings: Settings, industry: str, topic_title: str, *, judge: bool = False
) -> dict[str, Any]:
    """单用例: 真实模型生成脚本 → 契约检查 + 红线扫描 (+ 可选 judge)。"""
    from .llm import get_chat_model

    llm = get_chat_model(settings)
    topic = Topic(title=topic_title, industry=industry)
    script = await write_script(llm, topic, settings)
    result: dict[str, Any] = {
        "industry": industry,
        "topic": topic_title,
        "title": script.title,
        "scenes": len(script.scenes),
        "issues": check_script_contract(script),
    }
    if judge:
        try:
            result["judge"] = await judge_script(llm, script)
        except Exception as exc:  # noqa: BLE001 —— judge 失败不算用例失败
            result["judge_error"] = f"{type(exc).__name__}: {exc}"
    return result


async def run_evals(
    settings: Settings,
    cases: list[dict[str, str]] | None = None,
    *,
    judge: bool = True,
) -> dict[str, Any]:
    """跑完整评测, 返回报告 dict(调用方负责落盘/展示)。"""
    cases = cases if cases is not None else GOLDEN_CASES
    results = []
    for case in cases:
        try:
            r = await eval_case(
                settings, case["industry"], case["topic_title"], judge=judge
            )
        except Exception as exc:  # noqa: BLE001 —— 单用例失败不中断整轮
            r = {
                "industry": case["industry"], "topic": case["topic_title"],
                "fatal": f"{type(exc).__name__}: {exc}",
            }
        logger.info("eval: %s/%s → %s", case["industry"], case["topic_title"],
                    r.get("issues") or "OK")
        results.append(r)
    failed = [r for r in results if r.get("fatal") or r.get("issues")]
    return {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "llm_model": settings.llm_model,
        "judge": judge,
        "total": len(results),
        "failed": len(failed),
        "results": results,
        "report_json": json.dumps(results, ensure_ascii=False, indent=2),
    }
