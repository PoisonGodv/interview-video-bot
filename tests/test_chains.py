"""chains 模块单元测试: 全程 fake_chat_model, 不联网、不调真实 LLM。

注意: 已装的 langchain_core 1.6.x 中 ``langchain_core.language_models.fake_chat``
已更名为 ``fake_chat_models``, 而只读契约 llm.py 仍从旧路径导入 GenericFakeChatModel。
此处用 sys.modules shim 兼容(不改契约文件), 使 ``interview_video.llm`` 可导入。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

# ---- 兼容 shim: 必须在 import interview_video.llm 之前安装 ----
import langchain_core.language_models.fake_chat_models as _fake_chat_models

if "langchain_core.language_models.fake_chat" not in sys.modules:
    _shim = types.ModuleType("langchain_core.language_models.fake_chat")
    _shim.GenericFakeChatModel = _fake_chat_models.GenericFakeChatModel
    sys.modules["langchain_core.language_models.fake_chat"] = _shim

from interview_video.chains import (  # noqa: E402
    fix_script_narration,
    generate_topics,
    parse_metadata_json,
    parse_script_json,
    parse_topics_json,
    truncate_narration,
    write_metadata,
    write_script,
)
from interview_video.chains.metadata import AI_DECLARATION, MAX_TITLE_CHARS  # noqa: E402
from interview_video.chains.script import MAX_NARRATION_CHARS  # noqa: E402
from interview_video.config import Settings  # noqa: E402
from interview_video.llm import fake_chat_model  # noqa: E402
from interview_video.models import Chapter, Script, Topic  # noqa: E402

SAMPLE_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "examples" / "sample_script.json"
)


@pytest.fixture()
def sample_script_text() -> str:
    """与 examples/sample_script.json 同构的 JSON 串。"""
    return SAMPLE_SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.fixture()
def sample_script(sample_script_text: str) -> Script:
    return Script.model_validate_json(sample_script_text)


# ---------------- parse 容错 ----------------


def test_parse_script_json_with_fence(sample_script_text: str):
    """```json 围栏必须被容忍。"""
    fenced = f"```json\n{sample_script_text}\n```"
    script = parse_script_json(fenced)
    assert script.title == "程序员面试避坑指南"
    assert len(script.scenes) == 3


def test_parse_script_json_with_prose(sample_script_text: str):
    """JSON 前后的废话文字必须被容忍。"""
    wrapped = f"好的, 这是脚本:\n{sample_script_text}\n以上, 希望满意!"
    script = parse_script_json(wrapped)
    assert script.scenes[0].kind == "point"
    assert script.scenes[-1].kind == "ending"


def test_parse_script_json_rejects_garbage():
    with pytest.raises(ValueError):
        parse_script_json("今天天气不错, 没有 JSON")


def test_parse_topics_json_array_and_object():
    topics = [
        {"title": "护士面试三件套", "industry": "护理", "hook": "面试官第一眼看什么?"},
        {"title": "夜班面试怎么答", "industry": "护理"},
    ]
    # 裸数组
    parsed = parse_topics_json(json.dumps(topics, ensure_ascii=False))
    assert len(parsed) == 2
    assert isinstance(parsed[0], Topic)
    assert parsed[0].hook == "面试官第一眼看什么?"
    # {"topics": [...]} 对象
    parsed2 = parse_topics_json(
        f"以下是选题:\n```json\n{json.dumps({'topics': topics}, ensure_ascii=False)}\n```\n完毕"
    )
    assert [t.title for t in parsed2] == ["护士面试三件套", "夜班面试怎么答"]


def test_parse_topics_json_rejects_non_list():
    with pytest.raises(ValueError):
        parse_topics_json('{"title": "x"}')


def test_parse_metadata_json_with_noise():
    text = (
        "生成结果如下\n```json\n"
        '{"title": "程序员面试避坑指南", "desc": "三章讲透", "tags": ["面试", "程序员"]}\n'
        "```\n(完)"
    )
    meta = parse_metadata_json(text)
    assert meta.title.startswith("程序员")
    assert meta.tags == ["面试", "程序员"]


# ---------------- truncate_narration / fix_script_narration ----------------


def _long_text(ch: str, n: int) -> str:
    return ch * n


def test_truncate_narration_short_text_unchanged():
    short = "大家好, 这期很短。"
    assert truncate_narration(short) == short


def test_truncate_narration_cut_at_sentence_end():
    # 句末标点位于 65~110 之间: 应精确截断在该句号处
    text = "大家好。" + _long_text("坑", 95) + "。" + _long_text("尾巴", 30) + "。"
    result = truncate_narration(text)
    assert len(result) <= MAX_NARRATION_CHARS
    assert result.endswith("。")
    assert result == "大家好。" + _long_text("坑", 95) + "。"


def test_truncate_narration_first_sentence_too_long():
    # 首句超长(无句末标点): 退到停顿标点并补句号
    text = _long_text("很长", 60) + "，" + _long_text("尾巴", 80)
    result = truncate_narration(text)
    assert len(result) <= MAX_NARRATION_CHARS
    assert result.endswith("。")


def test_truncate_narration_hard_cut_fallback():
    # 完全没有标点: 硬截断且补句号
    text = _long_text("a", 200)
    result = truncate_narration(text)
    assert len(result) <= MAX_NARRATION_CHARS
    assert result.endswith("。")


def test_fix_script_narration_and_ending_kind(sample_script: Script):
    """narration 超长的场景应被截断; 最后一镜应被修正为 ending。"""
    sample_script.scenes[0].narration = "开场白。" + _long_text("超", 150)
    sample_script.scenes[-1].kind = "point"  # 故意违反契约
    fixed = fix_script_narration(sample_script)
    assert fixed is sample_script  # 就地修改并返回
    assert len(fixed.scenes[0].narration) <= MAX_NARRATION_CHARS
    assert fixed.scenes[-1].kind == "ending"


def test_fix_script_narration_empty_raises():
    with pytest.raises(ValueError):
        fix_script_narration(Script(title="空", scenes=[]))


# ---------------- 全链路(fake 模型走回退路径) ----------------


def test_generate_topics_fallback():
    """假模型不支持 with_structured_output(NotImplementedError) → 回退 JSON 路径。"""
    topics = [
        {
            "title": f"选题{i}",
            "industry": "互联网",
            "audience": "3年经验程序员",
            "hook": f"钩子{i}",
            "key_points": ["a", "b", "c"],
        }
        for i in range(4)
    ]
    fake = fake_chat_model([json.dumps(topics, ensure_ascii=False)])
    result = asyncio_run(generate_topics(fake, "互联网", n=3))
    assert len(result) == 3  # n=3, 截断
    assert all(isinstance(t, Topic) for t in result)
    assert all(t.industry == "互联网" for t in result)


def test_write_script_fallback(sample_script_text: str, sample_script: Script):
    """假模型回退路径: prompt→fake→parse→model_validate→修正。"""
    topic = Topic(
        title="程序员面试避坑指南",
        industry="互联网",
        hook="很多人第一轮就挂了",
        key_points=["坑1", "坑2", "坑3"],
    )
    fake = fake_chat_model([sample_script_text])
    script = asyncio_run(write_script(fake, topic, Settings()))
    assert script.title == "程序员面试避坑指南"
    assert len(script.scenes) == 3
    assert script.scenes[-1].kind == "ending"
    assert all(len(s.narration) <= MAX_NARRATION_CHARS for s in script.scenes)


def test_write_script_truncates_overlong_narration():
    """LLM 返回超长 narration 时, write_script 必须修正。"""
    overlong = {
        "title": "测试",
        "summary": "测试",
        "scenes": [
            {
                "kind": "point",
                "header": "开场",
                "bullets": [],
                "narration": "开场。" + "超长" * 80,
            },
            {
                "kind": "ending",
                "header": "结尾",
                "bullets": [],
                "narration": "关注我。",
            },
        ],
    }
    fake = fake_chat_model(
        [f"```json\n{json.dumps(overlong, ensure_ascii=False)}\n```"]
    )
    topic = Topic(title="测试", industry="互联网")
    script = asyncio_run(write_script(fake, topic, Settings()))
    assert len(script.scenes[0].narration) <= MAX_NARRATION_CHARS
    assert script.scenes[0].narration.endswith("。")
    assert script.scenes[-1].kind == "ending"


def test_write_metadata_fallback(sample_script: Script):
    """desc 缺 AI 声明 / 标题超长 / 缺「面试」标签 → _finalize_meta 修正。"""
    chapters = [
        Chapter(index=0, title="开场", start=0.0),
        Chapter(index=1, title="高频题", start=12.5),
        Chapter(index=2, title="下期预告", start=87.0),
    ]
    raw = {
        "title": "超长" * 60,  # 120 字 > 80
        "desc": "这期讲面试避坑\n00:00 开场\n00:12 高频题\n01:27 下期预告",
        "tags": ["程序员", "求职", "干货", "大厂"],
    }
    fake = fake_chat_model(
        [f"这是元数据:\n{json.dumps(raw, ensure_ascii=False)}\n以上"]
    )
    meta = asyncio_run(write_metadata(fake, sample_script, chapters, Settings()))
    # 标题截断
    assert len(meta.title) <= MAX_TITLE_CHARS
    # 章节行原样保留 + 末尾固定 AI 声明行
    assert "00:00 开场" in meta.desc
    assert "00:12 高频题" in meta.desc
    assert "01:27 下期预告" in meta.desc
    assert meta.desc.splitlines()[-1] == AI_DECLARATION
    # 必含「面试」且数量合规
    assert "面试" in meta.tags
    assert len(meta.tags) >= 5
    assert len(meta.tags) <= 10
    assert len(set(meta.tags)) == len(meta.tags)  # 去重


# ---------------- 工具 ----------------


def asyncio_run(coro):
    """asyncio.run 的薄包装(asyncio_mode=auto 下亦可直接 async def)。"""
    import asyncio

    return asyncio.run(coro)
