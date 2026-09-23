"""LLM 调用统一入口: 模型输出边界防护 + 结构化自修复 + 异常分类与回退留痕。

彻底移除 LangChain 依赖，原生对接 boundary.enforce_boundary 与 prompts.ChatPromptTemplate。
契约与策略:
1. 边界与结构化防护 (invoke_structured):
   - 首选 enforce_boundary: 执行 Schema、长度与结构、业务红线三重边界校验;
   - 初次违规时自动下调温度发起带第一现场与违规反馈的自修复重问;
   - 容错本地回退解析: 若触发边界违规但本地 fallback_parser 可补救，留痕日志后采用;
   - 致命错误 (401/403/404) 熔断拦截，抛出 RuntimeError 不浪费重试。
2. 纯文本与审稿调用 (invoke_text):
   - 纯文本请求 + 本地容错解析 + 一次带错误反馈的修复重问。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ..boundary import (
    BoundaryRules,
    BoundaryViolationError,
    enforce_boundary,
)
from ..llm import is_fatal_error

logger = logging.getLogger(__name__)

# 修复重问的指令模板
_REPAIR_INSTRUCTION = (
    "你刚才的输出无法解析为要求的 JSON 结构。\n"
    "解析错误: {error}\n"
    "原始输出(截断):\n{output}\n\n"
    "请重新输出——只输出一个合法的 JSON(结构与之前要求一致), "
    "不要任何解释、围栏或其他文字。"
)

_MAX_REPAIR_OUTPUT_CHARS = 4000


def _fatal_llm_error(exc: BaseException) -> bool:
    """鉴权/权限/模型不存在类错误: 重试与回退都无意义, 应当立即失败。"""
    if is_fatal_error(exc):
        return True
    try:
        import openai

        fatal = tuple(
            getattr(openai, attr)
            for attr in ("AuthenticationError", "PermissionDeniedError", "NotFoundError")
            if hasattr(openai, attr)
        )
        return isinstance(exc, fatal) if fatal else False
    except ImportError:
        return False


def _msg_text(message: Any) -> str:
    """自各类返回形态提取纯文本。"""
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    try:
        return "".join(
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in content
        )
    except Exception:  # noqa: BLE001
        return str(content)


def _format_messages(prompt: Any, payload: dict[str, Any]) -> list[dict[str, str]]:
    """将 PromptTemplate 格式化为 list[dict[str, str]]。"""
    if hasattr(prompt, "format_messages"):
        raw_msgs = prompt.format_messages(**payload)
        return [
            {"role": m.role if hasattr(m, "role") else m.get("role", "user"),
             "content": str(m.content if hasattr(m, "content") else m.get("content", ""))}
            for m in raw_msgs
        ]
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    if isinstance(prompt, list):
        return [
            {"role": m.get("role", "user") if isinstance(m, dict) else getattr(m, "role", "user"),
             "content": str(m.get("content", "")) if isinstance(m, dict) else str(getattr(m, "content", ""))}
            for m in prompt
        ]
    return [{"role": "user", "content": str(prompt)}]


async def _repair_reask(
    llm: Any,
    messages: list[dict[str, str]],
    bad_text: str,
    error: BaseException,
) -> str:
    """带错误反馈的修复重问: 原对话 + 坏输出(AI) + 修正指令(user) → 新文本。"""
    repair_msgs = list(messages)
    if bad_text:
        repair_msgs.append({"role": "assistant", "content": bad_text[:_MAX_REPAIR_OUTPUT_CHARS]})
    repair_msgs.append(
        {
            "role": "user",
            "content": _REPAIR_INSTRUCTION.format(
                error=error,
                output=bad_text[:_MAX_REPAIR_OUTPUT_CHARS] or "(空)",
            ),
        }
    )
    if hasattr(llm, "ainvoke"):
        response = await llm.ainvoke(repair_msgs)
    elif callable(llm):
        response = await llm(repair_msgs)
    else:
        raise TypeError(f"llm 必须支持 ainvoke 或 callable: {type(llm)}")
    return _msg_text(response)


async def invoke_text(
    prompt: Any,
    llm: Any,
    payload: dict[str, Any],
    fallback_parser: Callable[[str], Any],
    *,
    stage: str = "llm",
) -> Any:
    """纯文本调用 + 容错解析 + 一次修复重问; 全失败抛 ValueError(保留第一现场)。"""
    messages = _format_messages(prompt, payload)
    try:
        if hasattr(llm, "ainvoke"):
            raw_res = await llm.ainvoke(messages)
        elif callable(llm):
            raw_res = await llm(messages)
        else:
            raise TypeError(f"llm 不支持调用: {type(llm)}")
        text = _msg_text(raw_res)
    except Exception as exc:
        if _fatal_llm_error(exc):
            raise RuntimeError(f"[{stage}] LLM 鉴权/配置错误, 不重试: {exc}") from exc
        raise

    try:
        return fallback_parser(text)
    except Exception as exc:
        logger.warning(
            "[%s] 文本解析失败(%s: %.200s), 修复重问一次",
            stage,
            type(exc).__name__,
            exc,
        )
        try:
            repaired = await _repair_reask(llm, messages, text, exc)
            return fallback_parser(repaired)
        except Exception as exc2:
            if _fatal_llm_error(exc2):
                raise RuntimeError(f"[{stage}] LLM 鉴权/配置错误: {exc2}") from exc2
            raise ValueError(f"[{stage}] LLM 输出解析失败(文本+修复重试均失败)") from exc


async def invoke_structured(
    prompt: Any,
    llm: Any,
    payload: dict[str, Any],
    schema: type[BaseModel],
    fallback_parser: Callable[[str], Any],
    *,
    stage: str = "llm",
    boundary_rules: BoundaryRules | None = None,
) -> Any:
    """结构化调用边界防护统一入口。

    1. 若提供了 with_structured_output (如遗留测试桩)，优先走结构化适配;
    2. 主路径走 boundary.enforce_boundary，具备 Schema/字数结构/红线自修复机制;
    3. 解析失败但有原始输出时执行 fallback_parser 本地重解析;
    4. 全流程对 401/403/404 等致命鉴权错误快速失败。
    """
    messages = _format_messages(prompt, payload)

    # 1. 兼容具备 with_structured_output 的测试桩或三方适配器
    if hasattr(llm, "with_structured_output") and callable(llm.with_structured_output):
        try:
            runner = llm.with_structured_output(schema, include_raw=True)
            if hasattr(runner, "ainvoke"):
                res = await runner.ainvoke(payload if hasattr(prompt, "|") else messages)
            else:
                res = runner(messages)
            if isinstance(res, dict):
                if res.get("parsed") is not None:
                    return res["parsed"]
                raw_text = _msg_text(res.get("raw"))
                first_exc = res.get("parsing_error") or ValueError("结构化输出 parsed 为空")
                if raw_text:
                    try:
                        return fallback_parser(raw_text)
                    except Exception as err:
                        first_exc = err
                # 修复重问
                repaired = await _repair_reask(llm, messages, raw_text, first_exc)
                return fallback_parser(repaired)
            return res
        except Exception as exc:
            if _fatal_llm_error(exc):
                raise RuntimeError(f"[{stage}] LLM 鉴权/配置错误, 不重试: {exc}") from exc
            logger.warning(
                "[%s] with_structured_output 失败(%s: %.200s), 回退到边界检查主路径",
                stage,
                type(exc).__name__,
                exc,
            )

    # 2. 原生输出边界主路径: enforce_boundary
    rules = boundary_rules or BoundaryRules()
    # 根据目标 Schema 适配边界规则
    schema_name = getattr(schema, "__name__", "")
    if schema_name in ("_TopicList", "BiliMeta", "SeriesPlan", "EpisodeOutline"):
        rules.enforce_scene_count = False
        rules.require_ending_scene = False
    elif schema_name == "ChapterDraft":
        rules.enforce_scene_count = False
        rules.require_ending_scene = False

    try:
        result = await enforce_boundary(
            messages,
            llm,
            schema=schema,
            boundary_rules=rules,
            max_repairs=1,
        )
        return result
    except BoundaryViolationError as b_err:
        logger.warning("[%s] 模型输出超出边界: %s", stage, b_err)
        # 尝试本地 fallback_parser 补救
        target = b_err.repaired_output or b_err.raw_output
        if target:
            try:
                val = fallback_parser(target)
                logger.info("[%s] 本地回退解析成功救回", stage)
                return val
            except Exception:
                pass
        raise ValueError(
            f"[{stage}] LLM 输出解析失败(边界检查与自修复重试均失败)"
        ) from b_err
    except Exception as exc:
        if _fatal_llm_error(exc):
            raise RuntimeError(f"[{stage}] LLM 鉴权/配置错误, 不重试: {exc}") from exc
        # 尝试本地 fallback
        try:
            return await invoke_text(messages, llm, payload, fallback_parser, stage=stage)
        except Exception as exc2:
            if _fatal_llm_error(exc2):
                raise RuntimeError(f"[{stage}] LLM 鉴权/配置错误: {exc2}") from exc2
            raise ValueError(
                f"[{stage}] LLM 输出解析失败(structured/本地重解析/修复重试均失败)"
            ) from exc


__all__ = [
    "invoke_structured",
    "invoke_text",
    "_fatal_llm_error",
]
