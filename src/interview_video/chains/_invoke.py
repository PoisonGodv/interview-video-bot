"""LLM 调用统一入口: 模型输出边界防护 + 结构化自修复 + 异常分类与回退留痕。

模型只需实现 ``await llm.chat(messages)``(见 llm.ChatModel); prompt 为 prompts.ChatPromptTemplate、
str 或 OpenAI 格式消息列表。契约与策略:
1. 边界与结构化防护 (invoke_structured):
   - 首选 enforce_boundary: 执行 Schema、长度与结构、业务红线三重边界校验;
     长度/结构规则由调用方显式传入 boundary_rules(默认为完整脚本规则);
   - 初次违规时自动下调温度发起带第一现场与违规反馈的自修复重问;
   - 容错本地回退解析: 若触发边界违规但本地 fallback_parser 可补救，留痕日志后采用;
   - 红线闸门: 任何回退解析结果都会重扫业务红线, 命中即抛 RedlineGateError(ValueError);
   - 致命错误 (401/403/404) 熔断拦截，抛出 RuntimeError 不浪费重试;
   - 输出截断 (LLMTruncatedError, finish_reason=length) 原样抛出: 同参修复重问必然再截断;
   - 运行预算超限 (LLMBudgetExceededError) 原样抛出, 不进修复/回退。
2. 纯文本与审稿调用 (invoke_text):
   - 纯文本请求 + 本地容错解析 + 一次带错误反馈的修复重问。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel

from ..boundary import (
    BoundaryRules,
    BoundaryViolationError,
    enforce_boundary,
    scan_object_redlines,
)
from ..llm import (
    ChatModel,
    LLMBudgetExceededError,
    LLMError,
    LLMTruncatedError,
    is_fatal_error,
    normalize_messages,
)
from ..prompts import ChatPromptTemplate

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


def _transport_exhausted(exc: BaseException) -> bool:
    """HTTP 客户端重试耗尽后抛出的传输层错误(429/5xx/超时): 上层不应再发起新一轮调用。"""
    return isinstance(exc, (LLMError, httpx.HTTPError))


class RedlineGateError(ValueError):
    """回退解析结果命中业务红线; __cause__ 为带 violations 的 BoundaryViolationError。"""


def _redline_gate(value: Any, stage: str, raw: str = "") -> Any:
    """对回退/结构化解析结果做对象级红线重扫, 命中即抛 RedlineGateError。"""
    violations = scan_object_redlines(value)
    if not violations:
        return value
    detail = "; ".join(v.message for v in violations[:3])
    err = BoundaryViolationError(
        f"[{stage}] 输出命中业务红线: {detail}", violations=violations, raw_output=raw
    )
    raise RedlineGateError(f"[{stage}] LLM 输出命中业务红线: {detail}") from err


def _format_messages(prompt: Any, payload: dict[str, Any]) -> list[dict[str, str]]:
    """ChatPromptTemplate / str / 消息列表 → OpenAI 格式 list[dict[str, str]]。"""
    if isinstance(prompt, ChatPromptTemplate):
        return [dict(m) for m in prompt.format_messages(**payload)]
    return normalize_messages(prompt)


async def _repair_reask(
    llm: ChatModel,
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
    return (await llm.chat(repair_msgs)).content


async def invoke_text(
    prompt: Any,
    llm: ChatModel,
    payload: dict[str, Any],
    fallback_parser: Callable[[str], Any],
    *,
    stage: str = "llm",
) -> Any:
    """纯文本调用 + 容错解析 + 一次修复重问; 全失败抛 ValueError(保留第一现场)。"""
    messages = _format_messages(prompt, payload)
    try:
        text = (await llm.chat(messages)).content
    except (LLMTruncatedError, LLMBudgetExceededError):
        raise
    except Exception as exc:
        if is_fatal_error(exc):
            raise RuntimeError(f"[{stage}] LLM 鉴权/配置错误, 不重试: {exc}") from exc
        raise

    try:
        parsed = fallback_parser(text)
    except Exception as exc:
        logger.warning(
            "[%s] 文本解析失败(%s: %.200s), 修复重问一次",
            stage,
            type(exc).__name__,
            exc,
        )
        try:
            repaired = await _repair_reask(llm, messages, text, exc)
            parsed = fallback_parser(repaired)
            text = repaired
        except (LLMTruncatedError, LLMBudgetExceededError):
            raise
        except Exception as exc2:
            if is_fatal_error(exc2):
                raise RuntimeError(f"[{stage}] LLM 鉴权/配置错误: {exc2}") from exc2
            raise ValueError(f"[{stage}] LLM 输出解析失败(文本+修复重试均失败)") from exc
    return _redline_gate(parsed, stage, text)


async def invoke_structured(
    prompt: Any,
    llm: ChatModel,
    payload: dict[str, Any],
    schema: type[BaseModel],
    fallback_parser: Callable[[str], Any],
    *,
    stage: str = "llm",
    boundary_rules: BoundaryRules | None = None,
) -> Any:
    """结构化调用边界防护统一入口。

    1. 主路径走 boundary.enforce_boundary(规则取 boundary_rules, 缺省为完整脚本规则),
       具备 Schema/字数结构/红线自修复机制;
    2. 边界失败但有原始输出时执行 fallback_parser 本地重解析(结果仍过红线闸门);
    3. 其他非传输层异常退到 invoke_text(文本解析 + 一次修复重问);
    4. 全流程对 401/403/404 等致命鉴权错误快速失败。
    """
    messages = _format_messages(prompt, payload)
    rules = boundary_rules or BoundaryRules()

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
            except Exception:
                val = None
            if val is not None:
                _redline_gate(val, stage, target)
                logger.info("[%s] 本地回退解析成功救回", stage)
                return val
        raise ValueError(
            f"[{stage}] LLM 输出解析失败(边界检查与自修复重试均失败)"
        ) from b_err
    except (LLMTruncatedError, LLMBudgetExceededError):
        raise
    except Exception as exc:
        if is_fatal_error(exc):
            raise RuntimeError(f"[{stage}] LLM 鉴权/配置错误, 不重试: {exc}") from exc
        if _transport_exhausted(exc):
            raise  # 客户端已按 Retry-After/退避重试并尝试过备用模型, 不再整轮重来
        # 尝试本地 fallback
        try:
            return await invoke_text(messages, llm, payload, fallback_parser, stage=stage)
        except (RedlineGateError, LLMBudgetExceededError):
            raise
        except Exception as exc2:
            if is_fatal_error(exc2):
                raise RuntimeError(f"[{stage}] LLM 鉴权/配置错误: {exc2}") from exc2
            raise ValueError(
                f"[{stage}] LLM 输出解析失败(structured/本地重解析/修复重试均失败)"
            ) from exc


__all__ = [
    "RedlineGateError",
    "invoke_structured",
    "invoke_text",
]
