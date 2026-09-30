"""prompt 契约测试: 模板渲染后必须包含机械承重条款, 且对花括号免疫。

背景: 章节标题来自 LLM 生成的 scene.header, 含 ``HashMap{K,V}`` 类花括号时
旧实现(值预拼进模板)会 KeyError 崩溃, 已改为模板变量传入。
"""
from __future__ import annotations

from interview_video.chains.metadata import AI_DECLARATION
from interview_video.chains.metadata import _build_prompt as _meta_prompt
from interview_video.chains.redlines import RED_LINES
from interview_video.chains.script import (
    MAX_NARRATION_CHARS,
    MIN_NARRATION_CHARS,
)
from interview_video.chains.script import (
    _build_prompt as _script_prompt,
)
from interview_video.chains.topic import _build_prompt as _topic_prompt
from interview_video.models import Chapter, Topic


def _render_script(brand: str = "面试充电站", target_sec: int = 100) -> str:
    prompt = _script_prompt()
    topic = Topic(title="测试选题", industry="互联网")
    msgs = prompt.format_messages(
        topic_json=topic.model_dump_json(), brand=brand, target_sec=target_sec,
    )
    return "\n".join(getattr(m, "content", "") for m in msgs)


def test_script_prompt_contract():
    text = _render_script()
    assert str(MIN_NARRATION_CHARS) in text and str(MAX_NARRATION_CHARS) in text
    assert "只输出" in text and "JSON" in text
    assert "ending" in text  # 结尾镜要求
    assert RED_LINES in text  # 红线全文


def test_script_prompt_braces_in_brand_immune():
    """BRAND 含花括号不再触发模板二次格式化(旧实现 KeyError)。"""
    text = _render_script(brand="品牌{K,V}测试")
    assert "品牌{K,V}测试" in text


def test_metadata_prompt_braces_in_chapter_title_immune():
    """章节标题含花括号(如 HashMap{K,V})不再崩模板。"""
    chapters = [
        Chapter(index=0, title="HashMap{K,V} 是什么", start=0.0),
        Chapter(index=1, title="复杂度 O(n^2)", start=12.5),
    ]
    prompt = _meta_prompt()
    payload = {
        "brand": "面试{充电}站",
        "title": "测试",
        "summary": "摘要",
        "chapter_lines": "\n".join(f"00:00 {c.title}" for c in chapters),
    }
    msgs = prompt.format_messages(**payload)
    text = "\n".join(getattr(m, "content", "") for m in msgs)
    assert "HashMap{K,V} 是什么" in text
    assert AI_DECLARATION in text
    assert RED_LINES in text


def test_topic_prompt_contract():
    prompt = _topic_prompt()
    msgs = prompt.format_messages(industry="护理", n=5, avoid="无")
    text = "\n".join(getattr(m, "content", "") for m in msgs)
    assert "只输出 JSON" in text
    assert RED_LINES in text


def test_metadata_template_variables_not_prefilled():
    """模板本体保留 {brand}/{chapter_lines} 占位(值不得烤进模板字符串)。"""
    template_text = _meta_prompt().messages[1].template
    assert "{chapter_lines}" in template_text
    assert "{brand}" in template_text
