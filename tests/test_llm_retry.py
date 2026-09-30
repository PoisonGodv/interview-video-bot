"""LLM 客户端限流/退避/超时/截断测试: 桩 transport + 注入 sleep/random, 不联网不真等。"""
from __future__ import annotations

import email.utils
import logging

import httpx
import pytest
from pydantic import BaseModel

from interview_video.chains._invoke import invoke_structured, invoke_text
from interview_video.config import Settings
from interview_video.llm import (
    HTTPChatClient,
    LLMAPIError,
    LLMTruncatedError,
    RateLimitError,
    get_chat_model,
    parse_retry_after,
    retry_delay,
)

OK = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


def _patch_transport(monkeypatch, responses, seen_timeouts=None):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = calls["n"]
        calls["n"] += 1
        return responses[min(i, len(responses) - 1)]

    real = httpx.AsyncClient

    def factory(**kw):
        if seen_timeouts is not None:
            seen_timeouts.append(kw.get("timeout"))
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


def _client(sleeps, **kw):
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    kw.setdefault("max_retries", 2)
    return HTTPChatClient(
        model="m", api_key="k", base_url="https://x.test/v1",
        sleep=fake_sleep, rand=lambda: 1.0, **kw,
    )


def test_parse_retry_after_seconds_date_and_cap() -> None:
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("9999") == 60.0
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    date = email.utils.formatdate(1_000_030, usegmt=True)
    assert parse_retry_after(date, now=1_000_000) == pytest.approx(30.0)


def test_retry_delay_jitter_and_rate_limit_base() -> None:
    assert retry_delay(LLMAPIError("x"), 0, rand=lambda: 0.0) == 0.25
    assert retry_delay(LLMAPIError("x"), 0, rand=lambda: 1.0) == 0.5
    assert retry_delay(RateLimitError(), 0, rand=lambda: 1.0) == 2.0
    assert retry_delay(RateLimitError(), 2, rand=lambda: 1.0) == 8.0
    assert retry_delay(RateLimitError(), 10, rand=lambda: 1.0) == 60.0
    assert retry_delay(RateLimitError(retry_after=5.0), 3) == 5.0


async def test_429_honors_retry_after(monkeypatch, caplog) -> None:
    calls = _patch_transport(monkeypatch, [
        httpx.Response(429, headers={"Retry-After": "3"}, text="slow down"),
        httpx.Response(200, json=OK),
    ])
    sleeps: list[float] = []
    with caplog.at_level(logging.WARNING, logger="interview_video.llm"):
        res = await _client(sleeps).ainvoke("hi")
    assert res.content == "ok"
    assert calls["n"] == 2
    assert sleeps == [3.0]
    msg = caplog.records[-1].getMessage()
    assert "status=429" in msg and "1/3" in msg and "3.0s" in msg


async def test_429_without_header_uses_larger_backoff(monkeypatch) -> None:
    _patch_transport(monkeypatch, [httpx.Response(429), httpx.Response(429), httpx.Response(200, json=OK)])
    sleeps: list[float] = []
    await _client(sleeps).ainvoke("hi")
    assert sleeps == [2.0, 4.0]


async def test_5xx_retries_with_small_backoff_then_raises(monkeypatch) -> None:
    calls = _patch_transport(monkeypatch, [httpx.Response(503, text="overloaded")])
    sleeps: list[float] = []
    with pytest.raises(LLMAPIError, match="503"):
        await _client(sleeps).ainvoke("hi")
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]


async def test_timeout_splits_connect_and_read(monkeypatch) -> None:
    seen: list = []
    _patch_transport(monkeypatch, [httpx.Response(200, json=OK)], seen_timeouts=seen)
    s = Settings(_env_file=None, llm_api_key="sk", llm_base_url="https://x.test/v1",
                 llm_timeout=120, llm_connect_timeout=5)
    await get_chat_model(s).ainvoke("hi")
    t = seen[0]
    assert isinstance(t, httpx.Timeout)
    assert t.connect == 5 and t.read == 120


async def test_finish_reason_length_raises_truncated_without_retry(monkeypatch) -> None:
    calls = _patch_transport(monkeypatch, [httpx.Response(200, json={
        "choices": [{"message": {"content": '{"name": "半'}, "finish_reason": "length"}],
    })])
    sleeps: list[float] = []
    with pytest.raises(LLMTruncatedError, match="LLM_MAX_TOKENS") as ei:
        await _client(sleeps, max_tokens=64).ainvoke("hi")
    assert calls["n"] == 1 and sleeps == []
    assert ei.value.content == '{"name": "半'


async def test_truncation_skips_fallback_model(monkeypatch) -> None:
    calls = _patch_transport(monkeypatch, [httpx.Response(200, json={
        "choices": [{"message": {"content": "x"}, "finish_reason": "length"}],
    })])
    sleeps: list[float] = []
    client = _client(sleeps).with_fallbacks([_client(sleeps)])
    with pytest.raises(LLMTruncatedError):
        await client.ainvoke("hi")
    assert calls["n"] == 1


class _Item(BaseModel):
    name: str


class _TruncLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages, **kw):
        self.calls += 1
        raise LLMTruncatedError("截断, 请调大 LLM_MAX_TOKENS")


async def test_invoke_layers_propagate_truncation_without_repair() -> None:
    llm = _TruncLLM()
    with pytest.raises(LLMTruncatedError):
        await invoke_structured("p", llm, {}, _Item, lambda t: _Item(name=t), stage="t")
    assert llm.calls == 1
    llm = _TruncLLM()
    with pytest.raises(LLMTruncatedError):
        await invoke_text("p", llm, {}, lambda t: _Item(name=t), stage="t")
    assert llm.calls == 1


def test_parse_retry_after_rejects_non_finite_and_naive_date_is_utc() -> None:
    assert parse_retry_after("nan") is None
    assert parse_retry_after("inf") is None
    # "-0000" 无时区信息: 必须按 UTC 解析, 不能按本机时区偏移
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:10 -0000", now=1445412480.0) == 10.0


class _Item(BaseModel):
    x: str


async def test_rate_limit_exhausted_not_retried_again_by_invoke(monkeypatch) -> None:
    """客户端重试耗尽后, invoke_structured 不得再走 invoke_text 发起新一轮请求(双重重试)。"""
    sleeps: list[float] = []
    calls = _patch_transport(monkeypatch, [httpx.Response(429, text="busy")])
    client = _client(sleeps, max_retries=2)
    with pytest.raises(RateLimitError):
        await invoke_structured("p", client, {}, _Item, lambda t: _Item(x=t), stage="t")
    assert calls["n"] == 3
