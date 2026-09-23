"""_invoke 健壮层与输出边界测试: 三级策略/修复重问/异常分类/边界护栏, 全部假模型不联网。"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from interview_video.chains import parse_json_lenient
from interview_video.chains._invoke import _fatal_llm_error, invoke_structured, invoke_text
from interview_video.llm import AuthenticationError, ChatResponse, fake_chat_model
from interview_video.prompts import ChatPromptTemplate

GOOD = '{"name": "正常"}'
BAD = "抱歉我无法输出 JSON"


class _Item(BaseModel):
    name: str


PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", "你是 JSON 输出机器。"),
        ("user", "为 {x} 起名, 只输出 JSON: {{\"name\": \"...\"}}"),
    ]
)


def _parse(text: str) -> _Item:
    return _Item.model_validate(parse_json_lenient(text))


# ---------- 文本路径: 解析 + 修复重问 ----------

async def test_invoke_text_parses_directly():
    llm = fake_chat_model([GOOD])
    item = await invoke_text(PROMPT, llm, {"x": "测试"}, _parse, stage="t")
    assert item.name == "正常"


async def test_invoke_text_repairs_once():
    """第一次输出坏 JSON → 修复重问消费第二次输出。"""
    llm = fake_chat_model([BAD, GOOD])
    item = await invoke_text(PROMPT, llm, {"x": "测试"}, _parse, stage="t")
    assert item.name == "正常"


async def test_invoke_text_all_failed_raises_with_cause():
    llm = fake_chat_model([BAD, BAD])
    with pytest.raises(ValueError) as excinfo:
        await invoke_text(PROMPT, llm, {"x": "测试"}, _parse, stage="t")
    assert "解析失败" in str(excinfo.value)
    assert excinfo.value.__cause__ is not None  # 第一现场保留


# ---------- 结构化路径与输出边界 ----------

async def test_invoke_structured_falls_back_to_text():
    llm = fake_chat_model([GOOD])
    item = await invoke_structured(PROMPT, llm, {"x": "测试"}, _Item, _parse, stage="t")
    assert item.name == "正常"


class _StubStructuredLLM:
    """模拟 provider with_structured_output 报错并包含 raw 文本。"""

    def __init__(self, bad_text: str = BAD, fixed_text: str = GOOD) -> None:
        self.bad_text = bad_text
        self.fixed_text = fixed_text

    async def ainvoke(self, messages, **kwargs):
        return ChatResponse(content=self.fixed_text)

    def with_structured_output(self, schema, **kwargs):
        return lambda payload: {
            "raw": self.bad_text,
            "parsed": None,
            "parsing_error": ValueError("输出与 schema 不匹配"),
        }


async def test_invoke_structured_local_reparse_zero_cost():
    """raw 文本本身可解析 → 本地重解析成功, 不再调 LLM。"""
    stub = _StubStructuredLLM(bad_text=GOOD)
    item = await invoke_structured(PROMPT, stub, {"x": "测试"}, _Item, _parse, stage="t")
    assert item.name == "正常"


async def test_invoke_structured_repair_reask():
    """raw 不可解析 → 修复重问一次, 用 ainvoke 的输出。"""
    stub = _StubStructuredLLM(bad_text=BAD, fixed_text=GOOD)
    item = await invoke_structured(PROMPT, stub, {"x": "测试"}, _Item, _parse, stage="t")
    assert item.name == "正常"


async def test_invoke_structured_self_repair_via_boundary():
    """通过 boundary 的自修复回路自动修复。"""
    llm = fake_chat_model([BAD, GOOD])
    item = await invoke_structured(PROMPT, llm, {"x": "测试"}, _Item, _parse, stage="t")
    assert item.name == "正常"


# ---------- 异常分类 ----------

def test_fatal_llm_error():
    # 原生错误
    assert _fatal_llm_error(AuthenticationError("401")) is True
    assert _fatal_llm_error(ValueError("普通错误")) is False
    assert _fatal_llm_error(Exception()) is False


def test_fatal_llm_error_with_openai_shim(monkeypatch: pytest.MonkeyPatch):
    import sys
    import types

    # 模拟外部库抛出的 AuthenticationError
    fake_mod = types.ModuleType("openai")

    class _FakeAuthError(Exception):
        pass

    fake_mod.AuthenticationError = _FakeAuthError
    monkeypatch.setitem(sys.modules, "openai", fake_mod)
    assert _fatal_llm_error(_FakeAuthError("401")) is True
