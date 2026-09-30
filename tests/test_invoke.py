"""_invoke 健壮层与输出边界测试: 三级策略/修复重问/异常分类/边界护栏, 全部假模型不联网。"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from interview_video.chains import parse_json_lenient
from interview_video.chains._invoke import invoke_structured, invoke_text
from interview_video.llm import AuthenticationError, fake_chat_model, is_fatal_error
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


def _parse_nm(text: str) -> _Item:
    """宽松回退解析: 接受 {"nm": ...} 旧字段名(schema 校验必然失败, 只有回退能救)。"""
    return _Item(name=parse_json_lenient(text)["nm"])


async def test_invoke_structured_boundary_failure_rescued_by_fallback_parser():
    """边界检查 + 自修复均失败 → fallback_parser 对原始输出本地重解析救回, 不再多调 LLM。"""
    llm = fake_chat_model(['{"nm": "正常"}', '{"nm": "正常"}'])
    item = await invoke_structured(PROMPT, llm, {"x": "测试"}, _Item, _parse_nm, stage="t")
    assert item.name == "正常"
    assert len(llm.call_history) == 2  # 首次 + 一次边界修复重问


async def test_invoke_structured_self_repair_via_boundary():
    """通过 boundary 的自修复回路自动修复。"""
    llm = fake_chat_model([BAD, GOOD])
    item = await invoke_structured(PROMPT, llm, {"x": "测试"}, _Item, _parse, stage="t")
    assert item.name == "正常"


# ---------- 异常分类 ----------

def test_is_fatal_error_only_native_types():
    assert is_fatal_error(AuthenticationError("401")) is True
    assert is_fatal_error(ValueError("普通错误")) is False
    assert is_fatal_error(Exception()) is False

    class AuthenticationError_(Exception):
        pass

    AuthenticationError_.__name__ = "AuthenticationError"
    assert is_fatal_error(AuthenticationError_("同名外部异常")) is False  # 不再按类名鸭子匹配


class _FatalLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        raise AuthenticationError("401")


async def test_fatal_error_fails_fast_in_both_layers():
    for call in (
        lambda llm: invoke_structured(PROMPT, llm, {"x": "测试"}, _Item, _parse, stage="t"),
        lambda llm: invoke_text(PROMPT, llm, {"x": "测试"}, _parse, stage="t"),
    ):
        llm = _FatalLLM()
        with pytest.raises(RuntimeError, match="鉴权"):
            await call(llm)
        assert llm.calls == 1  # 不修复重问、不回退


# ---------- 边界规则由调用方显式传入 ----------

_NARR = "这是一段足够长的口播文案, 用来满足每个场景六十五到一百一十字的口播长度要求, 同时保持口语化表达, 让观众听得懂记得住, 继续补充一些内容凑够字数。"


async def test_boundary_rules_explicit_per_caller():
    from interview_video.boundary import BoundaryRules
    from interview_video.models import ChapterDraft

    draft = ChapterDraft.model_validate(
        {"scenes": [{"kind": "point", "header": "开场", "narration": _NARR}], "summary": "摘要"}
    ).model_dump_json()
    parse = ChapterDraft.model_validate_json

    llm = fake_chat_model([draft])
    out = await invoke_structured(
        "p", llm, {}, ChapterDraft, parse, stage="t", boundary_rules=BoundaryRules.non_script()
    )
    assert len(out.scenes) == 1 and len(llm.call_history) == 1

    llm = fake_chat_model([draft, draft])  # 默认完整脚本规则: 1 镜/无 ending → 触发修复重问
    await invoke_structured("p", llm, {}, ChapterDraft, parse, stage="t")
    assert len(llm.call_history) == 2


# ---------- 红线闸门: 回退解析结果同样重扫 ----------

REDLINE = '{"name": "跟着学三天包过"}'


async def test_invoke_text_redline_gate():
    from interview_video.chains._invoke import RedlineGateError

    llm = fake_chat_model([REDLINE])
    with pytest.raises(RedlineGateError) as excinfo:
        await invoke_text(PROMPT, llm, {"x": "测试"}, _parse, stage="t")
    assert "包过承诺" in str(excinfo.value)


async def test_invoke_structured_local_reparse_redline_gate():
    """边界失败后本地回退解析救回的结果命中红线 → 不得绕过闸门。"""
    from interview_video.chains._invoke import RedlineGateError

    bad = '{"nm": "跟着学三天包过"}'
    llm = fake_chat_model([bad, bad])
    with pytest.raises(RedlineGateError, match="红线"):
        await invoke_structured(PROMPT, llm, {"x": "测试"}, _Item, _parse_nm, stage="t")


async def test_invoke_structured_boundary_fallback_redline_gate():
    """enforce_boundary 因红线失败后, fallback_parser 救回的结果也必须拦截。"""
    from interview_video.boundary import BoundaryViolationError

    llm = fake_chat_model([REDLINE, REDLINE])
    with pytest.raises(ValueError, match="红线") as excinfo:
        await invoke_structured(PROMPT, llm, {"x": "测试"}, _Item, _parse, stage="t")
    assert isinstance(excinfo.value.__cause__, BoundaryViolationError)
    assert any(v.category == "redline" for v in excinfo.value.__cause__.violations)
