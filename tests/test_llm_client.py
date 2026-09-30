"""原生 HTTP LLM 客户端直测: httpx.MockTransport 桩上游, 注入 sleep/random, 不联网不真等。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from interview_video.boundary import BoundaryRules, parse_json_lenient
from interview_video.chains._invoke import invoke_structured
from interview_video.config import Settings
from interview_video.llm import (
    AuthenticationError,
    HTTPChatClient,
    LLMAPIError,
    LLMBudgetExceededError,
    LLMTruncatedError,
    get_chat_model,
)
from interview_video.llm_audit import LLMCallAudit, run_budget, summarize_usage


def _completion(content="ok", *, finish="stop", usage=None, model=None) -> dict:
    body: dict = {"choices": [{"message": {"role": "assistant", "content": content},
                               "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = usage
    if model is not None:
        body["model"] = model
    return body


class _Upstream:
    """按请求顺序回放响应; 记录每次请求(URL/headers/body)。"""

    def __init__(self, monkeypatch, responses) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        real = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            r = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
            return r(request) if callable(r) else r

        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
        )

    @property
    def n(self) -> int:
        return len(self.requests)

    def body(self, i: int) -> dict:
        return json.loads(self.requests[i].content)


def _client(sleeps: list[float], *, model="m", base_url="https://x.test/v1", **kw) -> HTTPChatClient:
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    kw.setdefault("max_retries", 2)
    return HTTPChatClient(
        model=model, api_key="sk-test", base_url=base_url,
        sleep=fake_sleep, rand=lambda: 1.0, **kw,
    )


def _audit_lines(tmp_path) -> list[dict]:
    path = tmp_path / "llm_calls.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# ---------- happy path ----------

async def test_200_happy_path_request_shape_and_usage_audited(monkeypatch, tmp_path) -> None:
    up = _Upstream(monkeypatch, [httpx.Response(200, json=_completion(
        "你好", usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        model="m-2024",
    ))])
    sleeps: list[float] = []
    audit = LLMCallAudit(tmp_path)
    client = _client(sleeps, base_url="https://x.test/v1/", audit=audit, max_tokens=256)
    res = await client.chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi", "name": "x"}],
        temperature=0.2,
    )

    assert res.content == "你好" and str(res) == "你好"
    assert res.model == "m-2024"
    assert res.usage == {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}
    assert up.n == 1 and sleeps == []

    req = up.requests[0]
    assert str(req.url) == "https://x.test/v1/chat/completions"  # 末尾斜杠被去掉
    assert req.headers["authorization"] == "Bearer sk-test"
    body = up.body(0)
    assert body == {
        "model": "m",
        "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        "temperature": 0.2,
        "max_tokens": 256,
    }

    [rec] = _audit_lines(tmp_path)
    assert rec["model"] == "m"
    assert (rec["input_tokens"], rec["output_tokens"], rec["total_tokens"]) == (12, 3, 15)
    assert rec["response_chars"] == 2 and rec["prompt_chars"] > 0
    assert "error" not in rec
    assert summarize_usage(tmp_path)["total_tokens"] == 15


async def test_string_prompt_and_missing_usage_default_to_zero(monkeypatch, tmp_path) -> None:
    up = _Upstream(monkeypatch, [httpx.Response(200, json=_completion("ok"))])
    res = await _client([], audit=LLMCallAudit(tmp_path)).chat("hi")
    assert up.body(0)["messages"] == [{"role": "user", "content": "hi"}]
    assert "max_tokens" not in up.body(0)
    assert up.body(0)["temperature"] == 0.7
    assert res.model == "m"  # 响应无 model 字段时回落到配置名
    assert res.usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert _audit_lines(tmp_path)[0]["total_tokens"] == 0


async def test_content_parts_list_is_joined(monkeypatch) -> None:
    _Upstream(monkeypatch, [httpx.Response(200, json=_completion(
        [{"type": "text", "text": "前"}, {"type": "text", "text": "后"}],
    ))])
    assert (await _client([]).chat("hi")).content == "前后"


# ---------- 限流 / 鉴权 / 5xx + 备用模型 ----------

async def test_429_then_200_honors_retry_after(monkeypatch, tmp_path) -> None:
    up = _Upstream(monkeypatch, [
        httpx.Response(429, headers={"Retry-After": "4"}, text="slow down"),
        httpx.Response(200, json=_completion(usage={"total_tokens": 9})),
    ])
    sleeps: list[float] = []
    res = await _client(sleeps, audit=LLMCallAudit(tmp_path)).chat("hi")
    assert res.content == "ok"
    assert up.n == 2 and sleeps == [4.0]
    # 中间被限流的尝试不计费, 审计只记成功那次
    [rec] = _audit_lines(tmp_path)
    assert rec["total_tokens"] == 9 and "error" not in rec


async def test_401_is_fatal_no_retry_no_fallback(monkeypatch, tmp_path) -> None:
    up = _Upstream(monkeypatch, [httpx.Response(401, text="invalid api key")])
    sleeps: list[float] = []
    client = _client(sleeps, audit=LLMCallAudit(tmp_path),
                     fallbacks=[_client(sleeps, model="backup")])
    with pytest.raises(AuthenticationError, match="401") as ei:
        await client.chat("hi")
    assert ei.value.status_code == 401 and "invalid api key" in ei.value.response_text
    assert up.n == 1 and sleeps == []
    [rec] = _audit_lines(tmp_path)
    assert rec["error"].startswith("AuthenticationError")


async def test_5xx_exhausted_then_fallback_model_succeeds(monkeypatch, tmp_path) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "backup":
            return httpx.Response(200, json=_completion("from backup", usage={"total_tokens": 5}))
        return httpx.Response(502, text="bad gateway")

    up = _Upstream(monkeypatch, [route])
    sleeps: list[float] = []
    audit = LLMCallAudit(tmp_path)
    backup = _client(sleeps, model="backup", base_url="https://y.test/v1", audit=audit)
    client = _client(sleeps, audit=audit, fallbacks=[backup])
    res = await client.chat("hi", temperature=0.1)

    assert res.content == "from backup"
    assert [up.body(i)["model"] for i in range(up.n)] == ["m", "m", "m", "backup"]
    assert str(up.requests[-1].url).startswith("https://y.test/v1/")
    assert up.body(3)["temperature"] == 0.1  # 显式参数透传给备用模型
    assert sleeps == [0.5, 1.0]
    recs = _audit_lines(tmp_path)
    assert [r["model"] for r in recs] == ["m", "backup"]
    assert "502" in recs[0]["error"] and "error" not in recs[1]


async def test_5xx_exhausted_without_fallback_raises_last_error(monkeypatch) -> None:
    up = _Upstream(monkeypatch, [httpx.Response(500, text="boom")])
    with pytest.raises(LLMAPIError, match="HTTP 500") as ei:
        await _client([], max_retries=1).chat("hi")
    assert ei.value.status_code == 500 and up.n == 2


async def test_fallback_via_get_chat_model_settings(monkeypatch) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == "fb-model":
            assert request.headers["authorization"] == "Bearer sk-fb"
            return httpx.Response(200, json=_completion("fb"))
        return httpx.Response(503)

    up = _Upstream(monkeypatch, [route])
    s = Settings(_env_file=None, llm_api_key="sk-main", llm_base_url="https://x.test/v1",
                 llm_model="main", llm_max_retries=0, llm_fallback_model="fb-model",
                 llm_fallback_api_key="sk-fb")
    assert (await get_chat_model(s).chat("hi")).content == "fb"
    assert up.n == 2


# ---------- 异常响应体 ----------

@pytest.mark.parametrize(
    "response, match",
    [
        (httpx.Response(200, json={"choices": []}), "没有 choices"),
        (httpx.Response(200, json={"id": "x"}), "没有 choices"),
        (httpx.Response(200, json={"choices": "oops"}), "没有 choices"),
        (httpx.Response(200, json={"choices": [{"finish_reason": "stop"}]}), "缺少 message"),
        (httpx.Response(200, json={"choices": ["text"]}), "缺少 message"),
        (httpx.Response(200, json={"choices": [{"message": {"content": 42}}]}), "content 类型异常"),
        (httpx.Response(200, json=["not", "an", "object"]), "非 JSON 对象"),
        (httpx.Response(200, text="<html>gateway</html>"), "不是合法 JSON"),
    ],
)
async def test_malformed_200_bodies_raise_clear_llm_api_error(monkeypatch, response, match) -> None:
    up = _Upstream(monkeypatch, [response])
    sleeps: list[float] = []
    with pytest.raises(LLMAPIError, match=match):
        await _client(sleeps).chat("hi")
    assert up.n == 3  # 网关偶发坏包按可恢复错误重试


async def test_malformed_then_valid_recovers(monkeypatch) -> None:
    _Upstream(monkeypatch, [httpx.Response(200, text="not json"), httpx.Response(200, json=_completion())])
    assert (await _client([]).chat("hi")).content == "ok"


async def test_finish_reason_length_raises_truncation_and_audits(monkeypatch, tmp_path) -> None:
    up = _Upstream(monkeypatch, [httpx.Response(200, json=_completion(
        '{"a": "半', finish="length", usage={"total_tokens": 64},
    ))])
    sleeps: list[float] = []
    client = _client(sleeps, audit=LLMCallAudit(tmp_path), max_tokens=64,
                     fallbacks=[_client(sleeps, model="backup")])
    with run_budget(SimpleNamespace(llm_max_calls_per_run=None, llm_max_tokens_per_run=10_000)) as b:
        with pytest.raises(LLMTruncatedError, match="max_tokens=64.*LLM_MAX_TOKENS") as ei:
            await client.chat("hi")
        assert b.tokens == 64  # 截断输出同样计入 token 预算
    assert ei.value.content == '{"a": "半'
    assert up.n == 1 and sleeps == []  # 不重试, 不切备用
    [rec] = _audit_lines(tmp_path)
    assert rec["total_tokens"] == 64 and "finish_reason=length" in rec["error"]


# ---------- 预算 ----------

async def test_budget_cap_blocks_request_before_sending(monkeypatch) -> None:
    up = _Upstream(monkeypatch, [httpx.Response(200, json=_completion())])
    sleeps: list[float] = []
    client = _client(sleeps, fallbacks=[_client(sleeps, model="backup")])
    with run_budget(SimpleNamespace(llm_max_calls_per_run=1, llm_max_tokens_per_run=None)):
        await client.chat("a")
        with pytest.raises(LLMBudgetExceededError, match="LLM_MAX_CALLS_PER_RUN=1"):
            await client.chat("b")
    assert up.n == 1


async def test_budget_counts_each_retry_attempt(monkeypatch) -> None:
    up = _Upstream(monkeypatch, [httpx.Response(503)])
    with run_budget(SimpleNamespace(llm_max_calls_per_run=2, llm_max_tokens_per_run=None)):
        with pytest.raises(LLMBudgetExceededError):
            await _client([], max_retries=5).chat("a")
    assert up.n == 2


# ---------- 结构化 JSON 调用 ----------

class _Topic(BaseModel):
    title: str
    points: list[str]


def _parse_topic(text: str) -> _Topic:
    return _Topic.model_validate(parse_json_lenient(text))


async def test_invoke_structured_parses_fenced_json_over_http(monkeypatch) -> None:
    fenced = '<think>先想想</think>\n```json\n{"title": "自我介绍", "points": ["结构", "亮点",],}\n```'
    up = _Upstream(monkeypatch, [httpx.Response(200, json=_completion(fenced))])
    topic = await invoke_structured(
        "出一个选题, 只输出 JSON", _client([]), {}, _Topic, _parse_topic,
        stage="topic", boundary_rules=BoundaryRules.non_script(),
    )
    assert topic == _Topic(title="自我介绍", points=["结构", "亮点"])
    assert up.n == 1


async def test_invoke_structured_repairs_invalid_json_over_http(monkeypatch) -> None:
    up = _Upstream(monkeypatch, [
        httpx.Response(200, json=_completion("抱歉, 我不能输出 JSON")),
        httpx.Response(200, json=_completion('{"title": "薪资谈判", "points": ["底线"]}')),
    ])
    topic = await invoke_structured(
        "出一个选题", _client([]), {}, _Topic, _parse_topic,
        stage="topic", boundary_rules=BoundaryRules.non_script(),
    )
    assert topic.title == "薪资谈判"
    assert up.n == 2
    repair_msgs = up.body(1)["messages"]
    assert repair_msgs[-2] == {"role": "assistant", "content": "抱歉, 我不能输出 JSON"}
    assert repair_msgs[-1]["role"] == "user"
