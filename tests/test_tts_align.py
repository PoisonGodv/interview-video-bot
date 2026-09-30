"""P0-2: edge 词级 cue 按原文标点对齐与断行。"""
from __future__ import annotations

import pytest

from interview_video import tts
from interview_video.models import Cue

_SRC = "第一个坑：面试官让你讲最难的技术难题，记住用STAR法则"
_TOKENS = ["第一个", "坑", "面试官", "让", "你", "讲", "最难", "的", "技术", "难题",
           "记住", "用", "STAR", "法则"]


def _words(tokens: list[str], step: float = 0.3) -> list[Cue]:
    return [Cue(start=i * step, end=i * step + step - 0.02, text=t) for i, t in enumerate(tokens)]


def _assert_no_mid_word_split(texts: list[str]) -> None:
    for a, b in zip(texts, texts[1:]):
        assert not (a.endswith("技术") and b.startswith("难题")), texts


@pytest.mark.parametrize("max_chars", [8, 10, 12, 16])
def test_breaks_on_source_punct_not_mid_word(max_chars: int):
    aligned = tts.align_punctuation(_words(_TOKENS), _SRC)
    bars = tts.merge_word_cues(aligned, max_chars=max_chars)
    texts = [b.text for b in bars]
    _assert_no_mid_word_split(texts)
    assert texts[0] == "第一个坑："
    assert any(t.endswith("难题，") for t in texts)
    assert "".join(texts) == _SRC


def test_align_attaches_trailing_punct():
    aligned = tts.align_punctuation(_words(_TOKENS), _SRC)
    assert [w.text for w in aligned][:2] == ["第一个", "坑："]
    assert aligned[9].text == "难题，"
    assert [(w.start, w.end) for w in aligned] == [(w.start, w.end) for w in _words(_TOKENS)]


def test_align_english_case_and_spaces():
    src = "Hello, world! 我用 Redis 做缓存。「真的」吗？"
    words = [Cue(start=0, end=1, text=t)
             for t in ["Hello", "world", "我", "用", "redis", "做", "缓存", "真的", "吗"]]
    out = [w.text for w in tts.align_punctuation(words, src)]
    assert out == ["Hello, ", "world! ", "我", "用 ", "redis ", "做", "缓存。", "「真的」", "吗？"]


def test_align_idempotent_when_words_already_punctuated():
    once = tts.align_punctuation(_words(_TOKENS), _SRC)
    twice = tts.align_punctuation(once, _SRC)
    assert [w.text for w in twice] == [w.text for w in once]


def test_align_mismatch_falls_back_to_old_behavior():
    words = _words(_TOKENS)
    out = tts.align_punctuation(words, "完全不同的一段文本")
    assert [w.text for w in out] == _TOKENS
    assert [b.text for b in tts.merge_word_cues(out)] == [
        b.text for b in tts.merge_word_cues(words)
    ]


def test_align_skips_unmatched_word_and_resyncs():
    src = "今天，我们聊面试。"
    words = [Cue(start=0, end=1, text=t) for t in ["今天", "XX", "我们", "聊", "面试"]]
    out = [w.text for w in tts.align_punctuation(words, src)]
    assert out == ["今天，", "XX", "我们", "聊", "面试。"]


def test_align_empty_inputs():
    assert tts.align_punctuation([], "abc") == []
    words = _words(["甲"])
    assert tts.align_punctuation(words, "")[0].text == "甲"


def test_merge_over_limit_splits_back_at_last_punct():
    words = [Cue(start=i * 0.2, end=i * 0.2 + 0.18, text=t)
             for i, t in enumerate(["一二", "三、", "四五", "六七", "八九"])]
    bars = tts.merge_word_cues(words, max_chars=8)
    assert [b.text for b in bars] == ["一二三、", "四五六七八九"]


def test_merge_hard_cut_when_no_punct():
    words = _words(["一二", "三四", "五六"])
    assert [b.text for b in tts.merge_word_cues(words, max_chars=4)] == ["一二三四", "五六"]


def test_display_text_rules():
    assert tts.display_text("第一个坑：") == "第一个坑"
    assert tts.display_text("首先，我们来看，") == "首先 我们来看"
    assert tts.display_text("真的吗？") == "真的吗？"
    assert tts.display_text("缓存。") == "缓存。"
    assert tts.display_text("Hello, world! ") == "Hello, world!"
    assert tts.display_text(tts.display_text("首先，我们来看，")) == "首先 我们来看"


def test_format_display_cues_drops_empty():
    cues = [Cue(start=0, end=1, text="，"), Cue(start=1, end=2, text="好的，")]
    out = tts.format_display_cues(cues)
    assert [(c.start, c.text) for c in out] == [(1, "好的")]


class _FakeCommunicate:
    def __init__(self, text: str, voice: str, **kwargs) -> None:
        self._chunks = []
        for i, t in enumerate(_TOKENS):
            self._chunks.append({"type": "WordBoundary", "offset": i * 3_000_000,
                                 "duration": 2_800_000, "text": t})
        self._chunks.append({"type": "audio", "data": b"x"})

    def stream(self):
        chunks = self._chunks

        class _S:
            def __aiter__(self):
                async def gen():
                    for c in chunks:
                        yield c
                return gen()

        return _S()


@pytest.mark.asyncio
async def test_synthesize_scenes_end_to_end_display(tmp_path, monkeypatch):
    from interview_video.config import Settings
    from interview_video.models import Scene, Script

    monkeypatch.setattr(tts.edge_tts, "Communicate", _FakeCommunicate)
    script = Script(title="t", scenes=[Scene(header="h", narration=_SRC)])
    settings = Settings(_env_file=None, tts_provider="edge", subtitle_max_chars=10)
    media = await tts.synthesize_scenes(script, tmp_path, settings)
    texts = [c.text for c in media[0].cues]
    _assert_no_mid_word_split(texts)
    assert texts[0] == "第一个坑"
    assert not any(t.endswith(("，", "：")) for t in texts)
    assert all(len(t) <= 10 for t in texts)


def test_merge_breaks_after_period_inside_closing_quote():
    src = "错误回答是：“就是一个bug，查了很久修好了。”正确回答是"
    toks = ["错误", "回答", "是", "就是", "一个", "bug", "查", "了", "很久", "修好", "了",
            "正确", "回答", "是"]
    aligned = tts.align_punctuation(_words(toks), src)
    texts = [b.text for b in tts.merge_word_cues(aligned, max_chars=16)]
    assert any(t.endswith("修好了。”") for t in texts), texts
    assert texts[-1] == "正确回答是"
