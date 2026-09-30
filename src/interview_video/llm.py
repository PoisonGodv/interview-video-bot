"""原生 HTTP LLM 客户端: 直接基于 httpx 的 OpenAI 兼容接口 + 测试用假模型。

对外只暴露 ``await client.chat(messages) -> ChatResult``(messages 为 str 或 OpenAI 格式 dict 列表):
1. 纯净异步 HTTP 通信 (POST /chat/completions);
2. 异常分类与快速失败: 401/403/404 鉴权与配置错误立即抛出，不浪费重试;
3. 抖动与指数退避重试: 针对 429 限流 / 5xx 服务端错误 / 超时网络波动自动重试,
   优先遵循 Retry-After(秒数或 HTTP-date, 上限 60s), 429 退避基数更大;
   finish_reason=length 抛 LLMTruncatedError(不重试, 提示调大 LLM_MAX_TOKENS);
4. 备用模型降级 (构造参数 fallbacks): 主模型彻底失败时切换到备用模型;
5. 审计打点集成 (LLMCallAudit): 精确捕获耗时、Prompt/Response 字符数及 token 消耗;
6. 测试替身 (fake_chat_model): 离线内存按序出参, 同样实现 chat()。
"""
from __future__ import annotations

import asyncio
import datetime
import email.utils
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from .config import Settings
from .llm_audit import LLMBudgetExceededError, LLMCallAudit, current_budget

logger = logging.getLogger(__name__)


# ===========================================================================
# 1. 核心异常定义
# ===========================================================================

class LLMError(Exception):
    """LLM 客户端基础异常。"""


class LLMAPIError(LLMError):
    """上游 API 响应异常。"""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        response_text: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text
        self.retry_after = retry_after


class AuthenticationError(LLMAPIError):
    """401 鉴权失败: API Key 错误或失效。"""

    def __init__(self, message: str = "LLM API 凭证无效 (401)", response_text: str = "") -> None:
        super().__init__(message, status_code=401, response_text=response_text)


class PermissionDeniedError(LLMAPIError):
    """403 权限不足或账户被冻结。"""

    def __init__(self, message: str = "LLM 访问权限不足 (403)", response_text: str = "") -> None:
        super().__init__(message, status_code=403, response_text=response_text)


class NotFoundError(LLMAPIError):
    """404 接口或指定模型不存在。"""

    def __init__(self, message: str = "LLM 接口或指定模型不存在 (404)", response_text: str = "") -> None:
        super().__init__(message, status_code=404, response_text=response_text)


class RateLimitError(LLMAPIError):
    """429 访问受限或触发并发/RPM配额。"""

    def __init__(
        self,
        message: str = "LLM 访问受限或配额耗尽 (429)",
        response_text: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status_code=429, response_text=response_text, retry_after=retry_after)


class LLMTruncatedError(LLMError):
    """finish_reason=length: 输出触顶 max_tokens 被截断。同参重试/修复重问大概率再次截断, 不重试。"""

    def __init__(self, message: str, content: str = "") -> None:
        super().__init__(message)
        self.content = content


class LLMConnectionError(LLMError):
    """base_url 非法或服务不可达(配置类错误, 不重试)。"""


_MAX_RETRY_AFTER = 60.0
_BACKOFF_CAP = 60.0
_BACKOFF_BASE = 0.5
_RATE_LIMIT_BACKOFF_BASE = 2.0
_CONNECT_TIMEOUT = 10.0


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """解析 Retry-After(秒数或 HTTP-date) → 等待秒数, 截断到 [0, 60]; 无法解析返回 None。"""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:  # 规范要求 GMT; 无时区的按 UTC 处理, 避免按本地时区偏移数小时
            when = when.replace(tzinfo=datetime.timezone.utc)
        seconds = when.timestamp() - (time.time() if now is None else now)
    if not math.isfinite(seconds):  # "nan"/"inf" 能被 float 解析, 不能当等待时长
        return None
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER)


def retry_delay(exc: BaseException, attempt: int, rand: Callable[[], float] = random.random) -> float:
    """第 attempt(0 起) 次失败后的等待: Retry-After 优先, 否则指数退避 + 抖动(429 基数更大)。"""
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        return retry_after
    base = _RATE_LIMIT_BACKOFF_BASE if isinstance(exc, RateLimitError) else _BACKOFF_BASE
    ceiling = min(_BACKOFF_CAP, base * (2 ** attempt))
    return ceiling / 2 + rand() * ceiling / 2


def is_fatal_error(exc: BaseException) -> bool:
    """判断是否为致命鉴权/权限/配置错误 (此类错误重试毫无意义，应直接熔断)。"""
    return isinstance(
        exc, (AuthenticationError, PermissionDeniedError, NotFoundError, LLMConnectionError)
    )


# ===========================================================================
# 2. 响应数据模型
# ===========================================================================

@dataclass
class ChatResult:
    """一次 chat 调用的结果: 文本 content + 实际模型名 + token 用量。"""

    content: str
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.content

    def __repr__(self) -> str:
        snippet = self.content[:60] + "..." if len(self.content) > 60 else self.content
        return f"ChatResult(content={snippet!r}, model={self.model!r})"


Messages = str | Sequence[Mapping[str, Any]]


class ChatModel(Protocol):
    """chains 层依赖的最小模型接口(HTTPChatClient / FakeChatModel / 测试桩均满足)。"""

    async def chat(
        self,
        messages: Messages,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult: ...


def normalize_messages(messages: Messages) -> list[dict[str, str]]:
    """str → 单条 user 消息; dict 序列 → 仅保留 role/content 的 OpenAI 格式列表。"""
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    out: list[dict[str, str]] = []
    for m in messages:
        if not isinstance(m, Mapping):
            raise TypeError(f"消息必须是 {{role, content}} 字典, 收到 {type(m).__name__}")
        out.append({"role": str(m.get("role", "user")), "content": str(m.get("content", ""))})
    return out


def _parse_completion(resp: httpx.Response) -> tuple[dict[str, Any], dict[str, Any], str]:
    """200 响应体 → (data, choices[0], content); 非 JSON / 结构异常统一抛 LLMAPIError(可重试)。"""
    snippet = resp.text[:200]
    try:
        data = resp.json()
    except ValueError:
        raise LLMAPIError(
            f"API 响应不是合法 JSON(HTTP 200): {snippet}", response_text=resp.text
        ) from None
    if not isinstance(data, dict):
        raise LLMAPIError(f"API 响应结构异常(非 JSON 对象): {snippet}", response_text=resp.text)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMAPIError(f"API 响应中没有 choices 内容: {snippet}", response_text=resp.text)
    first_choice = choices[0]
    msg = first_choice.get("message") if isinstance(first_choice, dict) else None
    if not isinstance(msg, dict):
        raise LLMAPIError(f"API 响应 choices[0] 缺少 message: {snippet}", response_text=resp.text)
    content = msg.get("content")
    if isinstance(content, list):  # 部分网关返回内容分片 [{"type": "text", "text": ...}]
        content = "".join(
            str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content
        )
    elif content is None:
        content = ""
    elif not isinstance(content, str):
        raise LLMAPIError(
            f"API 响应 message.content 类型异常({type(content).__name__}): {snippet}",
            response_text=resp.text,
        )
    return data, first_choice, content


def _parse_usage(raw: Any) -> dict[str, int]:
    raw = raw if isinstance(raw, dict) else {}

    def _int(key: str) -> int:
        try:
            return max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "input_tokens": _int("prompt_tokens"),
        "output_tokens": _int("completion_tokens"),
        "total_tokens": _int("total_tokens"),
    }


# ===========================================================================
# 3. 原生 HTTP LLM 客户端
# ===========================================================================

class HTTPChatClient:
    """基于 httpx.AsyncClient 的原生 OpenAI 兼容 Chat 客户端。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | SecretStr,
        base_url: str = "https://api.openai.com/v1",
        temperature: float = 0.7,
        timeout: float = 60.0,
        max_tokens: int | None = None,
        max_retries: int = 2,
        fallbacks: Sequence[HTTPChatClient] | None = None,
        audit: LLMCallAudit | None = None,
        connect_timeout: float = _CONNECT_TIMEOUT,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        rand: Callable[[], float] | None = None,
    ) -> None:
        self.model = model
        self._raw_key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        # 去除末尾斜杠
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.fallbacks: list[HTTPChatClient] = list(fallbacks) if fallbacks else []
        self.audit = audit
        self.connect_timeout = connect_timeout
        self._sleep = sleep
        self._rand = rand

    async def chat(
        self,
        messages: Messages,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """发起一次 chat 调用: 本模型按退避重试, 可恢复错误耗尽后依次尝试备用模型。"""
        try:
            return await self._call_with_retries(
                messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            )
        except Exception as exc:
            if isinstance(exc, (LLMTruncatedError, LLMBudgetExceededError)) or (
                is_fatal_error(exc) and not isinstance(exc, LLMConnectionError)
            ):
                raise
            if self.fallbacks:
                logger.warning(
                    "主模型 %s 失败(%s: %s)，正在尝试备用模型 (%d 个备用)...",
                    self.model,
                    type(exc).__name__,
                    exc,
                    len(self.fallbacks),
                )
                for fallback in self.fallbacks:
                    try:
                        return await fallback.chat(
                            messages, temperature=temperature, max_tokens=max_tokens
                        )
                    except Exception as fb_exc:
                        if is_fatal_error(fb_exc) or isinstance(fb_exc, LLMBudgetExceededError):
                            raise
                        logger.warning("备用模型 %s 同样失败: %s", fallback.model, fb_exc)
            raise

    async def _call_with_retries(
        self,
        messages: Messages,
        *,
        temperature: float,
        max_tokens: int | None,
    ) -> ChatResult:
        norm_messages = normalize_messages(messages)
        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._raw_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self.model,
            "messages": norm_messages,
            "temperature": temperature,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens

        prompt_str = "\n".join(f"{m['role']}: {m['content']}" for m in norm_messages)
        last_error: BaseException | None = None
        timeout = httpx.Timeout(self.timeout, connect=min(self.connect_timeout, self.timeout))

        budget = current_budget()
        for attempt in range(self.max_retries + 1):
            if budget is not None:
                budget.acquire()  # 超限直接抛出, 不进重试/备用模型
            t0 = time.perf_counter()
            try:
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        resp = await client.post(endpoint, json=body, headers=headers)
                except (httpx.InvalidURL, httpx.UnsupportedProtocol,
                        httpx.ConnectError, httpx.ConnectTimeout) as conn_exc:
                    # 连接失败/超时可能是瞬时网络抖动: 未到最后一次先按普通错误重试;
                    # URL 非法/协议不支持属配置错误, 立即熔断
                    transient = isinstance(conn_exc, (httpx.ConnectError, httpx.ConnectTimeout))
                    if transient and attempt < self.max_retries:
                        raise
                    raise LLMConnectionError(
                        f"无法连接 LLM 服务 base_url={self.base_url!r}"
                        f"({type(conn_exc).__name__}: {conn_exc}); 请检查 .env 的 LLM_BASE_URL 与网络"
                    ) from conn_exc

                elapsed_ms = (time.perf_counter() - t0) * 1000

                if resp.status_code == 200:
                    data, first_choice, content = _parse_completion(resp)
                    usage_dict = _parse_usage(data.get("usage"))

                    if budget is not None:
                        budget.add_tokens(
                            usage_dict["total_tokens"]
                            or usage_dict["input_tokens"] + usage_dict["output_tokens"]
                        )
                    truncated = first_choice.get("finish_reason") == "length"
                    if self.audit:
                        self.audit.record_call(
                            model=self.model,
                            elapsed_ms=elapsed_ms,
                            prompt=prompt_str,
                            response=content,
                            error="LLMTruncatedError: finish_reason=length" if truncated else None,
                            **usage_dict,
                        )
                    if truncated:
                        raise LLMTruncatedError(
                            f"LLM 输出触顶被截断(finish_reason=length, model={self.model}, "
                            f"max_tokens={max_tokens or '网关默认'}, 已输出 {len(content)} 字); "
                            "请调大 .env 的 LLM_MAX_TOKENS 后重试",
                            content=content,
                        )

                    return ChatResult(
                        content=content,
                        model=data.get("model") or self.model,
                        usage=usage_dict,
                    )

                # 状态码异常分支
                resp_text = resp.text
                if resp.status_code == 401:
                    raise AuthenticationError(f"API Key 无效 (401): {resp_text[:200]}", response_text=resp_text)
                if resp.status_code == 403:
                    raise PermissionDeniedError(f"权限不足 (403): {resp_text[:200]}", response_text=resp_text)
                if resp.status_code == 404:
                    raise NotFoundError(f"模型或地址不存在 (404): {resp_text[:200]}", response_text=resp_text)
                retry_after = parse_retry_after(resp.headers.get("retry-after"))
                if resp.status_code == 429:
                    raise RateLimitError(
                        f"触发限流 (429): {resp_text[:200]}",
                        response_text=resp_text,
                        retry_after=retry_after,
                    )

                raise LLMAPIError(
                    f"上游服务异常 (HTTP {resp.status_code}): {resp_text[:200]}",
                    status_code=resp.status_code,
                    response_text=resp_text,
                    retry_after=retry_after,
                )

            except LLMTruncatedError:
                raise
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                last_error = exc
                if is_fatal_error(exc):
                    if self.audit:
                        self.audit.record_call(
                            model=self.model,
                            elapsed_ms=elapsed_ms,
                            prompt=prompt_str,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    raise

                if attempt < self.max_retries:
                    backoff = retry_delay(exc, attempt, self._rand or random.random)
                    logger.warning(
                        "LLM[%s] 第 %d/%d 次失败(status=%s, %s: %.200s)，%.1fs 后重试%s",
                        self.model,
                        attempt + 1,
                        self.max_retries + 1,
                        getattr(exc, "status_code", "-"),
                        type(exc).__name__,
                        exc,
                        backoff,
                        "(遵循 Retry-After)" if getattr(exc, "retry_after", None) is not None else "",
                    )
                    await (self._sleep or asyncio.sleep)(backoff)
                else:
                    if self.audit:
                        self.audit.record_call(
                            model=self.model,
                            elapsed_ms=elapsed_ms,
                            prompt=prompt_str,
                            error=f"{type(exc).__name__}: {exc}",
                        )

        raise last_error or LLMError("LLM 重试次数耗尽仍未成功")


# ===========================================================================
# 4. 测试替身 (FakeChatModel)
# ===========================================================================

class FakeChatModel:
    """测试用假模型: 纯内存出参，不依赖网络与外部库。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self._cursor = 0
        self.call_history: list[list[dict[str, str]]] = []

    async def chat(
        self,
        messages: Messages,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        self.call_history.append(normalize_messages(messages))
        if self._cursor < len(self.responses):
            resp = self.responses[self._cursor]
            self._cursor += 1
            return ChatResult(content=resp, model="fake-model")
        raise RuntimeError("FakeChatModel 预设回复已用尽")


def fake_chat_model(responses: list[str]) -> FakeChatModel:
    """测试用: 依次返回给定文本的假模型。"""
    return FakeChatModel(responses)


# ===========================================================================
# 5. LLM 工厂
# ===========================================================================

def _audit_from_run_context() -> LLMCallAudit | None:
    """未显式传 audit 时, 取当前工作流 RunContext 上的审计器。"""
    from .workflow_engine import current_run_context

    ctx = current_run_context()
    return ctx.audit if ctx is not None else None


def get_chat_model(settings: Settings, audit: LLMCallAudit | None = None) -> HTTPChatClient:
    """根据 settings 构建真实模型客户端 (支持主备模型降级)。"""
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value().strip():
        raise RuntimeError(
            "未配置 LLM_API_KEY(任意 OpenAI 兼容服务)。"
            "请 cp .env.example .env 并填写 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL"
        )

    if audit is None:
        audit = _audit_from_run_context()
    base_url = settings.llm_base_url or "https://api.openai.com/v1"
    primary = HTTPChatClient(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=base_url,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
        connect_timeout=settings.llm_connect_timeout,
        max_tokens=settings.llm_max_tokens,
        max_retries=settings.llm_max_retries,
        audit=audit,
    )

    if settings.llm_fallback_model:
        fallback_key = settings.llm_fallback_api_key or settings.llm_api_key
        fallback_base = settings.llm_fallback_base_url or base_url
        fallback = HTTPChatClient(
            model=settings.llm_fallback_model,
            api_key=fallback_key,
            base_url=fallback_base,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
            connect_timeout=settings.llm_connect_timeout,
            max_tokens=settings.llm_max_tokens,
            max_retries=settings.llm_max_retries,
            audit=audit,
        )
        primary.fallbacks = [fallback]

    return primary


__all__ = [
    "LLMError",
    "LLMAPIError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "RateLimitError",
    "LLMConnectionError",
    "LLMTruncatedError",
    "LLMBudgetExceededError",
    "is_fatal_error",
    "parse_retry_after",
    "retry_delay",
    "ChatResult",
    "ChatModel",
    "Messages",
    "normalize_messages",
    "HTTPChatClient",
    "FakeChatModel",
    "fake_chat_model",
    "get_chat_model",
]
