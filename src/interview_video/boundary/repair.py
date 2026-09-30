"""自修复回路 (Self-Repair Loop) 与调用器适配: enforce_boundary 统一入口。"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from ..llm import LLMBudgetExceededError, LLMTruncatedError, is_fatal_error
from .errors import BoundaryViolationError, Violation
from .rules import BoundaryRules
from .structure import BoundaryChecker, clamp_chapters, fix_script_deterministic

logger = logging.getLogger(__package__)

T = TypeVar("T")


def build_repair_prompt(
    violations: list[Violation],
    raw_output: str,
    max_raw_chars: int = 2000,
) -> str:
    """构造带违规反馈的 user 消息，用于向模型发起精准的自修复重问。"""
    items = []
    for i, v in enumerate(violations, 1):
        loc = f" (字段位置: `{v.field}`)" if v.field else ""
        item_lines = [f"{i}. 【{v.category.upper()} 违规】{loc}"]
        item_lines.append(f"   - 违规描述: {v.message}")
        if v.fix_hint:
            item_lines.append(f"   - 修正指令: {v.fix_hint}")
        items.append("\n".join(item_lines))

    violations_str = "\n\n".join(items)

    snippet = raw_output[:max_raw_chars]
    if len(raw_output) > max_raw_chars:
        snippet += "\n...(原始输出过长，已截断)..."

    return (
        "【模型输出边界违规诊断与自修复指令】\n"
        "你上一轮的输出未能通过系统的安全与结构边界校验。\n"
        f"检测到共 {len(violations)} 项硬性边界违规，违规清单如下:\n\n"
        f"{violations_str}\n\n"
        "--------------------\n"
        f"上一轮原始输出内容如下:\n```\n{snippet}\n```\n"
        "--------------------\n\n"
        "请严格对照上述违规项执行纠偏，重新生成完整结果并遵守以下硬性边界约束:\n"
        "1. [Schema 规范] 必须且仅输出一个合法的 JSON 对象，严禁包裹任何 markdown 解释或非 JSON 文字;\n"
        "2. [结构与字数] 严格遵守长度限制(分镜数 5~8、每场景口播 65~110 字、画面标题 <=12 字、要点 <=14 字、末镜 kind='ending');\n"
        "3. [业务红线] 严禁编造薪资具体数字(请使用『因城市/公司差异较大』)、严禁包过/保上岸承诺、严禁年龄/性别/学历歧视、严禁诋毁企业或个人;\n"
        "4. 务必纠正上述列出的所有违规项，确保输出可直接通过系统校验。"
    )


def _extract_text_content(res: Any) -> str:
    """从各类模型返回值形态中自洽提取纯文本字符串。"""
    if res is None:
        return ""
    if isinstance(res, str):
        return res
    if hasattr(res, "content"):
        content = res.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part if isinstance(part, str) else str(part.get("text", ""))
                for part in content
            )
    if isinstance(res, dict):
        if "choices" in res and res["choices"]:
            choice = res["choices"][0]
            if isinstance(choice, dict):
                msg = choice.get("message", {})
                if isinstance(msg, dict):
                    return str(msg.get("content", ""))
        if "content" in res:
            return str(res["content"])
    return str(res)


def _messages_to_prompt_string(messages: list[dict[str, str]]) -> str:
    """把 OpenAI 格式的 messages 列表转换为纯文本 prompt。"""
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"[System]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        else:
            parts.append(f"[User]\n{content}")
    return "\n\n".join(parts)


async def _invoke_caller(
    caller: Any,
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    **kwargs: Any,
) -> str:
    """调用 LLM: 具备 chat() 的模型客户端(llm.ChatModel) 或普通函数(同步/异步均可)。

    异常原样上抛(致命错误由调用方用 llm.is_fatal_error 分类)。
    """
    # 1. 模型客户端: await caller.chat(messages, temperature=...)
    chat = getattr(caller, "chat", None)
    if callable(chat):
        return _extract_text_content(await chat(messages, temperature=temperature, **kwargs))

    # 2. 支持普通 callable (函数/方法)
    if callable(caller):
        sig = inspect.signature(caller) if not isinstance(caller, type) else None
        accepts_temp = False
        accepts_prompt_str = False
        if sig:
            params = sig.parameters
            accepts_temp = "temperature" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            if len(params) == 1 and ("prompt" in params or "text" in params):
                accepts_prompt_str = True

        call_args = (
            _messages_to_prompt_string(messages)
            if accepts_prompt_str
            else messages
        )
        call_kwargs = dict(kwargs)
        if accepts_temp and temperature is not None:
            call_kwargs["temperature"] = temperature

        try:
            if inspect.iscoroutinefunction(caller):
                res = await caller(call_args, **call_kwargs)
            else:
                res = caller(call_args, **call_kwargs)
                if inspect.iscoroutine(res):
                    res = await res
        except TypeError:
            # 退避到仅传位置参数
            if inspect.iscoroutinefunction(caller):
                res = await caller(call_args)
            else:
                res = caller(call_args)
                if inspect.iscoroutine(res):
                    res = await res

        return _extract_text_content(res)

    raise TypeError(f"caller 必须是可调用对象或具备 chat() 的模型客户端, 当前为: {type(caller)}")


async def enforce_boundary(
    prompt: str | list[dict[str, str]],
    caller: Callable[..., Any],
    schema: type[T],
    boundary_rules: BoundaryRules | None = None,
    *,
    max_repairs: int = 1,
    temperature: float = 0.7,
    repair_temperature: float = 0.2,
    system_prompt: str | None = None,
    **kwargs: Any,
) -> T:
    """模型输出边界强制函数 (自修复回路统一入口)。

    完整流程:
    1. 标准化构建输入消息序列 (System / User);
    2. 发起首次模型调用 (初温 temperature);
    3. 执行 Schema、长度结构、业务红线综合边界检查;
    4. 若合规，直接返回 Pydantic 校验实例;
    5. 若违规且尚有修复次数，捕获具体违背项 (Violation)，构造带错误反馈的纠错 user 消息;
    6. 将温度下调至 repair_temperature 发起单次有边界的重问修复;
    7. 若全失败，抛出完整保留第一现场原始输出的 BoundaryViolationError。
    """
    rules = boundary_rules or BoundaryRules()
    checker = BoundaryChecker(rules)

    # 1. 规整 messages
    if isinstance(prompt, str):
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
    elif isinstance(prompt, list):
        messages = list(prompt)
    else:
        raise TypeError(f"prompt 必须为 str 或 list[dict[str, str]], 当前类型为 {type(prompt)}")

    # 2. 首次调用
    raw_output = await _invoke_caller(caller, messages, temperature=temperature, **kwargs)

    # 3. 边界检查
    instance, violations = checker.check(raw_output, schema)
    if not violations and instance is not None:
        return instance

    logger.warning(
        "模型初次输出突破边界 (共 %d 项违规: %s), 启动自修复回路 (max_repairs=%d, repair_temp=%.2f)",
        len(violations),
        ", ".join(v.message for v in violations[:3]),
        max_repairs,
        repair_temperature,
    )

    # 4. 自修复回路
    first_instance = instance
    repaired_output: str | None = None
    repaired_instance: T | None = None
    first_raw = raw_output

    for attempt in range(max_repairs):
        repair_user_msg = build_repair_prompt(violations, raw_output)
        repair_messages = list(messages)
        # 将上一轮错误输出截断保留在对话上下文中
        repair_messages.append({"role": "assistant", "content": raw_output[:3000]})
        repair_messages.append({"role": "user", "content": repair_user_msg})

        try:
            repaired_output = await _invoke_caller(
                caller, repair_messages, temperature=repair_temperature, **kwargs
            )
        except Exception as exc:
            if is_fatal_error(exc) or isinstance(exc, (LLMTruncatedError, LLMBudgetExceededError)):
                raise
            logger.warning("自修复重问调用未完成(%s: %s)，退出修复循环", type(exc).__name__, exc)
            break

        repaired_instance, repaired_violations = checker.check(repaired_output, schema)
        if not repaired_violations and repaired_instance is not None:
            logger.info("模型输出在第 %d 次自修复重问后成功合规", attempt + 1)
            return repaired_instance

        violations = repaired_violations
        raw_output = repaired_output

    # 5. 兜底保护: 若存在已解析的实例且无红线违规，执行确定性修正算子收敛后采用
    has_redlines = any(v.category == "redline" for v in violations)
    candidate_instance = repaired_instance if repaired_instance is not None else first_instance
    if candidate_instance is not None and not has_redlines:
        logger.warning(
            "自修复结束 (剩余 %d 项违规)，执行确定性边界算子收敛返回",
            len(violations),
        )
        candidate_instance = fix_script_deterministic(candidate_instance, rules)
        candidate_instance = clamp_chapters(
            candidate_instance, rules.min_chapter_sec, rules.max_chapter_sec
        )
        return candidate_instance

    # 6. 自修复全失败且无法安全兜底: 抛出保留第一现场的 BoundaryViolationError
    raise BoundaryViolationError(
        f"模型输出未能满足边界约束 (经 {max_repairs} 次重问修复后仍有 {len(violations)} 项违规)",
        violations=violations,
        raw_output=first_raw,
        repaired_output=repaired_output,
    )


def enforce_boundary_sync(
    prompt: str | list[dict[str, str]],
    caller: Callable[..., Any],
    schema: type[T],
    boundary_rules: BoundaryRules | None = None,
    *,
    max_repairs: int = 1,
    temperature: float = 0.7,
    repair_temperature: float = 0.2,
    system_prompt: str | None = None,
    **kwargs: Any,
) -> T:
    """enforce_boundary 的同步封装，便于同步脚本与单测调用。"""
    coro = enforce_boundary(
        prompt,
        caller,
        schema,
        boundary_rules,
        max_repairs=max_repairs,
        temperature=temperature,
        repair_temperature=repair_temperature,
        system_prompt=system_prompt,
        **kwargs,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()
