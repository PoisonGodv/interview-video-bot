"""原生 HTTP LLM 客户端: 直接基于 httpx 的 OpenAI 兼容接口 + 测试用假模型。

彻底移除 LangChain / LangGraph 依赖，具备:
1. 纯净异步 HTTP 通信 (POST /chat/completions);
2. 异常分类与快速失败: 401/403/404 鉴权与配置错误立即抛出，不浪费重试;
3. 抖动与指数退避重试: 针对 429 限流 / 5xx 服务端错误 / 超时网络波动自动重试;
4. 备用模型降级 (with_fallbacks): 主模型彻底失败时无缝切换到备用模型;
5. 审计打点集成 (LLMCallAudit): 精确捕获耗时、Prompt/Response 字符数及 token 消耗;
6. 测试替身 (fake_chat_model): 离线内存流式出参，支持 ainvoke 与 with_fallbacks。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr

from .config import Settings
from .llm_audit import LLMCallAudit

logger = logging.getLogger(__name__)


# ===========================================================================
# 1. 核心异常定义 (语义与 OpenAI SDK 对齐，无需安装 openai 库)
# ===========================================================================

class LLMError(Exception):
    """LLM 客户端基础异常。"""


class LLMAPIError(LLMError):
    """上游 API 响应异常。"""

    def __init__(self, message: str, status_code: int = 500, response_text: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


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

    def __init__(self, message: str = "LLM 访问受限或配额耗尽 (429)", response_text: str = "") -> None:
        super().__init__(message, status_code=429, response_text=response_text)


class LLMConnectionError(LLMError):
    """base_url 非法或服务不可达(配置类错误, 不重试)。"""


def is_fatal_error(exc: BaseException) -> bool:
    """判断是否为致命鉴权/权限/配置错误 (此类错误重试毫无意义，应直接熔断)。"""
    if isinstance(exc, (AuthenticationError, PermissionDeniedError, NotFoundError, LLMConnectionError)):
        return True
    # 兼容通过 monkeypatch 或环境注入的 openai 异常
    exc_type = type(exc).__name__
    if exc_type in ("AuthenticationError", "PermissionDeniedError", "NotFoundError"):
        return True
    return False


# ===========================================================================
# 2. 响应数据模型
# ===========================================================================

@dataclass
class ChatResponse:
    """模型调用响应封装，兼容 str(res) 和 res.content。"""

    content: str
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.content

    def __repr__(self) -> str:
        snippet = self.content[:60] + "..." if len(self.content) > 60 else self.content
        return f"ChatResponse(content={snippet!r}, model={self.model!r})"


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

    def with_fallbacks(self, fallbacks: Sequence[HTTPChatClient]) -> HTTPChatClient:
        """注册备用客户端序列。"""
        new_client = HTTPChatClient(
            model=self.model,
            api_key=self._raw_key,
            base_url=self.base_url,
            temperature=self.temperature,
            timeout=self.timeout,
            max_tokens=self.max_tokens,
            max_retries=self.max_retries,
            fallbacks=fallbacks,
            audit=self.audit,
        )
        return new_client

    def _normalize_messages(self, messages: Any) -> list[dict[str, str]]:
        """规整各类形态的输入消息为符合 OpenAI 契约的 list[dict[str, str]]。"""
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]

        out: list[dict[str, str]] = []
        if isinstance(messages, Iterable):
            for m in messages:
                if isinstance(m, dict):
                    role = m.get("role", "user")
                    content = str(m.get("content", ""))
                    out.append({"role": role, "content": content})
                elif hasattr(m, "role") and hasattr(m, "content"):
                    out.append({"role": str(m.role), "content": str(m.content)})
                elif hasattr(m, "content"):
                    # 兼容类似 AIMessage / HumanMessage 的对象
                    name = type(m).__name__.lower()
                    role = "system" if "system" in name else ("assistant" if "ai" in name else "user")
                    out.append({"role": role, "content": str(m.content)})
                elif isinstance(m, (tuple, list)) and len(m) == 2:
                    out.append({"role": str(m[0]), "content": str(m[1])})
                else:
                    out.append({"role": "user", "content": str(m)})
        return out

    async def ainvoke(
        self,
        messages: Any,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """异步调用主入口。"""
        try:
            return await self._call_with_retries(
                messages,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
                **kwargs,
            )
        except Exception as exc:
            if is_fatal_error(exc) and not isinstance(exc, LLMConnectionError):
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
                        return await fallback.ainvoke(
                            messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            **kwargs,
                        )
                    except Exception as fb_exc:
                        if is_fatal_error(fb_exc):
                            raise
                        logger.warning("备用模型 %s 同样失败: %s", fallback.model, fb_exc)
            raise

    async def _call_with_retries(
        self,
        messages: Any,
        *,
        temperature: float,
        max_tokens: int | None,
        **kwargs: Any,
    ) -> ChatResponse:
        norm_messages = self._normalize_messages(messages)
        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._raw_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": kwargs.get("model", self.model),
            "messages": norm_messages,
            "temperature": temperature,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens

        prompt_str = "\n".join(f"{m['role']}: {m['content']}" for m in norm_messages)
        last_error: BaseException | None = None

        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
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
                    data = resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise LLMAPIError("API 响应中没有 choices 内容", response_text=resp.text)

                    first_choice = choices[0]
                    msg = first_choice.get("message") or {}
                    content = msg.get("content") or ""

                    raw_usage = data.get("usage") or {}
                    usage_dict = {
                        "input_tokens": raw_usage.get("prompt_tokens", 0),
                        "output_tokens": raw_usage.get("completion_tokens", 0),
                        "total_tokens": raw_usage.get("total_tokens", 0),
                    }

                    if self.audit:
                        self.audit.record_call(
                            model=self.model,
                            elapsed_ms=elapsed_ms,
                            prompt=prompt_str,
                            response=content,
                            **usage_dict,
                        )

                    return ChatResponse(
                        content=content,
                        model=data.get("model", self.model),
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
                if resp.status_code == 429:
                    raise RateLimitError(f"触发限流 (429): {resp_text[:200]}", response_text=resp_text)

                raise LLMAPIError(
                    f"上游服务异常 (HTTP {resp.status_code}): {resp_text[:200]}",
                    status_code=resp.status_code,
                    response_text=resp_text,
                )

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
                    backoff = (2 ** attempt) * 0.5
                    logger.warning(
                        "LLM 调用第 %d 次失败(%s: %s)，将在 %.1fs 后重试...",
                        attempt + 1,
                        type(exc).__name__,
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
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

    def with_fallbacks(self, fallbacks: Any) -> FakeChatModel:
        return self

    async def ainvoke(self, messages: Any, **kwargs: Any) -> ChatResponse:
        norm = []
        if isinstance(messages, str):
            norm = [{"role": "user", "content": messages}]
        elif isinstance(messages, list):
            for m in messages:
                if isinstance(m, dict):
                    norm.append(m)
                elif hasattr(m, "role") and hasattr(m, "content"):
                    norm.append({"role": str(m.role), "content": str(m.content)})
                elif hasattr(m, "content"):
                    norm.append({"role": "user", "content": str(m.content)})
                else:
                    norm.append({"role": "user", "content": str(m)})
        self.call_history.append(norm)

        if self._cursor < len(self.responses):
            resp = self.responses[self._cursor]
            self._cursor += 1
            return ChatResponse(content=resp, model="fake-model")
        raise RuntimeError("FakeChatModel 预设回复已用尽")


def fake_chat_model(responses: list[str]) -> FakeChatModel:
    """测试用: 依次返回给定文本的假模型。"""
    return FakeChatModel(responses)


# ===========================================================================
# 5. LLM 工厂
# ===========================================================================

def _audit_from_workflow_config() -> LLMCallAudit | None:
    """未显式传 audit 时, 取当前工作流 config.callbacks 里的 LLMCallAudit。"""
    from .workflow_engine import get_current_workflow_config

    cfg = get_current_workflow_config() or {}
    for cb in cfg.get("callbacks") or []:
        if isinstance(cb, LLMCallAudit):
            return cb
    return None


def get_chat_model(settings: Settings, audit: LLMCallAudit | None = None) -> HTTPChatClient:
    """根据 settings 构建真实模型客户端 (支持主备模型降级)。"""
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value().strip():
        raise RuntimeError(
            "未配置 LLM_API_KEY(任意 OpenAI 兼容服务)。"
            "请 cp .env.example .env 并填写 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL"
        )

    if audit is None:
        audit = _audit_from_workflow_config()
    base_url = settings.llm_base_url or "https://api.openai.com/v1"
    primary = HTTPChatClient(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=base_url,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
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
            max_tokens=settings.llm_max_tokens,
            max_retries=settings.llm_max_retries,
            audit=audit,
        )
        return primary.with_fallbacks([fallback])

    return primary


__all__ = [
    "LLMError",
    "LLMAPIError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "RateLimitError",
    "LLMConnectionError",
    "is_fatal_error",
    "ChatResponse",
    "HTTPChatClient",
    "FakeChatModel",
    "fake_chat_model",
    "get_chat_model",
]
