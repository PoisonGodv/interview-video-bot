"""tts 模块测试: 纯函数全测 + mock 流式合成; 真实 edge-tts 打 integration。"""
from __future__ import annotations

from pathlib import Path

import pytest

from interview_video import tts
from interview_video.models import Cue, Scene, SceneMedia, Script


def _w(start: float, end: float, text: str) -> Cue:
    """造一个词级 cue。"""
    return Cue(start=start, end=end, text=text)


# ---------- merge_word_cues ----------

def test_merge_empty():
    assert tts.merge_word_cues([]) == []


def test_merge_single_word():
    bars = tts.merge_word_cues([_w(0.0, 0.5, "你好")])
    assert len(bars) == 1
    assert bars[0].start == 0.0
    assert bars[0].end == 0.5
    assert bars[0].text == "你好"


def test_merge_accumulates_within_limits():
    words = [_w(i * 0.5, i * 0.5 + 0.4, "字") for i in range(4)]
    bars = tts.merge_word_cues(words, max_chars=16, max_gap=0.6, max_dur=4.0)
    assert len(bars) == 1
    assert bars[0].text == "字字字字"
    assert bars[0].start == 0.0
    assert bars[0].end == 1.9


def test_merge_breaks_on_max_chars():
    words = [_w(i * 0.5, i * 0.5 + 0.4, "字") for i in range(5)]
    bars = tts.merge_word_cues(words, max_chars=4)
    assert len(bars) == 2
    assert bars[0].text == "字字字字"
    assert bars[1].text == "字"


def test_merge_breaks_on_gap():
    words = [_w(0.0, 0.4, "甲"), _w(0.5, 0.9, "乙"), _w(2.0, 2.4, "丙")]
    bars = tts.merge_word_cues(words, max_gap=0.6)
    assert len(bars) == 2
    assert bars[0].text == "甲乙"
    assert bars[1].text == "丙"


def test_merge_gap_boundary_not_breaking():
    """gap == max_gap 不断条(规则是 > max_gap 才断)。"""
    words = [_w(0.0, 0.4, "甲"), _w(1.0, 1.4, "乙")]  # gap = 0.6
    bars = tts.merge_word_cues(words, max_gap=0.6)
    assert len(bars) == 1
    assert bars[0].text == "甲乙"


def test_merge_breaks_on_max_dur():
    words = [_w(0.0, 1.0, "甲"), _w(1.5, 3.0, "乙")]  # 合并后 3.0s > 2.5
    bars = tts.merge_word_cues(words, max_dur=2.5)
    assert len(bars) == 2


def test_merge_breaks_after_chinese_punct():
    words = [_w(0.0, 0.4, "你好，"), _w(0.6, 1.0, "世界")]
    bars = tts.merge_word_cues(words, max_chars=6)
    assert len(bars) == 2
    assert bars[0].text == "你好，"
    assert bars[1].text == "世界"


@pytest.mark.parametrize("punct", ["。", "！", "？"])
def test_merge_sentence_end_always_breaks(punct: str):
    words = [_w(0.0, 0.4, f"甲{punct}"), _w(0.6, 1.0, "乙")]
    bars = tts.merge_word_cues(words)
    assert len(bars) == 2


@pytest.mark.parametrize("punct", ["，", "；", "："])
def test_merge_comma_breaks_only_when_half_full(punct: str):
    words = [_w(0.0, 0.4, f"甲{punct}"), _w(0.6, 1.0, "乙")]
    assert len(tts.merge_word_cues(words, max_chars=16)) == 1  # 2 < 8, 不断
    assert len(tts.merge_word_cues(words, max_chars=4)) == 2  # 2 >= 2, 断


def test_merge_non_punct_no_prefer_break():
    """非中文标点结尾不触发优先断, 正常累计。"""
    words = [_w(0.0, 0.4, "abc"), _w(0.6, 1.0, "def")]
    bars = tts.merge_word_cues(words)
    assert len(bars) == 1


def test_merge_skips_empty_text():
    words = [_w(0.0, 0.4, ""), _w(0.5, 0.9, "  "), _w(1.0, 1.4, "甲")]
    bars = tts.merge_word_cues(words)
    assert len(bars) == 1
    assert bars[0].text == "甲"


def test_merge_overlong_word_own_bar():
    """单个词超 max_chars 独占一条(无法进一步切分)。"""
    words = [_w(0.0, 0.5, "一二三四五六七八九十"), _w(1.0, 1.4, "甲")]
    bars = tts.merge_word_cues(words, max_chars=4)
    assert len(bars) == 2
    assert bars[0].text == "一二三四五六七八九十"
    assert bars[1].text == "甲"


def test_merge_multiple_breaks_order():
    words = [
        _w(0.0, 0.3, "首先，"),
        _w(0.4, 0.7, "我们"),
        _w(0.8, 1.1, "来看"),
        _w(3.0, 3.3, "其次。"),  # 大 gap + 标点
        _w(3.5, 3.8, "最后"),
    ]
    bars = tts.merge_word_cues(words, max_chars=6, max_gap=0.6, max_dur=4.0)
    texts = [b.text for b in bars]
    assert texts == ["首先，", "我们来看", "其次。", "最后"]


# ---------- offset_cues ----------

def test_offset_cues_accumulates_by_duration():
    m0 = SceneMedia(
        index=0,
        mp3=Path("a.mp3"),
        duration=10.0,
        cues=[_w(0.0, 1.0, "甲"), _w(1.5, 2.5, "乙")],
    )
    m1 = SceneMedia(
        index=1,
        mp3=Path("b.mp3"),
        duration=5.0,
        cues=[_w(0.0, 1.0, "丙")],
    )
    m2 = SceneMedia(index=2, mp3=Path("c.mp3"), duration=3.0, cues=[])
    out = tts.offset_cues([m0, m1, m2])
    assert [(c.start, c.end, c.text) for c in out] == [
        (0.0, 1.0, "甲"),
        (1.5, 2.5, "乙"),
        (10.0, 11.0, "丙"),
    ]


def test_offset_cues_empty():
    assert tts.offset_cues([]) == []


# ---------- build_srt ----------

def test_build_srt_format():
    cues = [
        _w(0.0, 1.5, "第一句"),
        _w(3661.25, 3662.0, "第二句"),
    ]
    srt = tts.build_srt(cues)
    expected = (
        "1\n"
        "00:00:00,000 --> 00:00:01,500\n"
        "第一句\n"
        "\n"
        "2\n"
        "01:01:01,250 --> 01:01:02,000\n"
        "第二句\n"
    )
    assert srt == expected


def test_build_srt_empty():
    assert tts.build_srt([]) == "\n"


def test_build_srt_negative_clamped():
    srt = tts.build_srt([_w(-1.0, 0.0, "x")])
    assert "00:00:00,000" in srt


# ---------- synthesize_text(mock Communicate, 测 ticks 换算与写文件) ----------

class _FakeStream:
    """模拟 edge_tts stream(): 交替吐 audio / WordBoundary chunk。"""

    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c

        return _gen()


class _FakeCommunicate:
    last_kwargs: dict = {}

    def __init__(self, text: str, voice: str, **kwargs) -> None:
        self._chunks = [
            {"type": "WordBoundary", "offset": 0, "duration": 50_000_000, "text": "你好"},
            {"type": "audio", "data": b"mp3-bytes-1"},
            {"type": "WordBoundary", "offset": 55_000_000, "duration": 50_000_000, "text": "世界"},
            {"type": "audio", "data": b"mp3-bytes-2"},
        ]
        _FakeCommunicate.last_kwargs = {"text": text, "voice": voice, **kwargs}

    def stream(self):
        return _FakeStream(self._chunks)


@pytest.mark.asyncio
async def test_synthesize_text_ticks_and_mp3(tmp_path, monkeypatch):
    """offset/duration 是 100ns ticks: 50_000_000 ticks = 5.0s。"""
    monkeypatch.setattr(tts.edge_tts, "Communicate", _FakeCommunicate)
    mp3 = tmp_path / "sub" / "x.mp3"
    cues = await tts.synthesize_text("你好世界", mp3, "zh-CN-XiaoxiaoNeural", "+6%")
    assert mp3.exists()
    assert mp3.read_bytes() == b"mp3-bytes-1mp3-bytes-2"
    # 词级 cue 已换算成秒: 50_000_000 ticks = 5.0s; 两条间隔 0.5s 但
    # 合并时长 10.5s > max_dur=4.0, 故断成两条
    assert [(c.start, c.end, c.text) for c in cues] == [
        (0.0, 5.0, "你好"),
        (5.5, 10.5, "世界"),
    ]
    # 词级 boundary 需显式开启(默认 SentenceBoundary 拿不到词)
    assert _FakeCommunicate.last_kwargs["boundary"] == "WordBoundary"
    assert _FakeCommunicate.last_kwargs["rate"] == "+6%"


# ---------- synthesize_scenes(模块属性 monkeypatch, 不联网) ----------

def _script(n: int = 3) -> Script:
    return Script(
        title="t",
        scenes=[Scene(header=f"h{i}", narration=f"第{i}镜口播") for i in range(n)],
    )


def _settings():
    from interview_video.config import Settings

    return Settings()


@pytest.mark.asyncio
async def test_synthesize_scenes_sequential_and_paths(tmp_path, monkeypatch):
    calls: list[tuple[str, Path]] = []

    async def fake_synth(text, mp3_path, voice, rate):
        calls.append((text, mp3_path))
        return [_w(0.0, 2.0, text)]

    monkeypatch.setattr(tts, "synthesize_text", fake_synth)
    media = await tts.synthesize_scenes(_script(), tmp_path / "scenes", _settings())
    assert len(media) == 3
    # 顺序合成
    assert [c[0] for c in calls] == ["第0镜口播", "第1镜口播", "第2镜口播"]
    for i, m in enumerate(media):
        assert m.index == i
        assert m.mp3 == tmp_path / "scenes" / f"scene_{i:03d}.mp3"
        # duration 兜底 = 最后 cue end + 0.4
        assert m.duration == pytest.approx(2.4)
        assert m.cues[0].text == f"第{i}镜口播"


@pytest.mark.asyncio
async def test_synthesize_scenes_no_cue_fallback(tmp_path, monkeypatch):
    async def fake_synth(text, mp3_path, voice, rate):
        return []

    monkeypatch.setattr(tts, "synthesize_text", fake_synth)
    media = await tts.synthesize_scenes(_script(1), tmp_path, _settings())
    assert media[0].duration == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_synthesize_scenes_retry_then_success(tmp_path, monkeypatch):
    attempts = {"n": 0}
    sleeps: list[float] = []

    async def fake_synth(text, mp3_path, voice, rate):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ConnectionError("boom")
        return [_w(0.0, 1.0, "ok")]

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(tts, "synthesize_text", fake_synth)
    monkeypatch.setattr(tts.asyncio, "sleep", fake_sleep)
    media = await tts.synthesize_scenes(_script(1), tmp_path, _settings())
    assert attempts["n"] == 3
    assert sleeps == [1.0, 2.0]  # 指数退避
    assert media[0].cues[0].text == "ok"


@pytest.mark.asyncio
async def test_synthesize_scenes_exhausted_raises_with_index(tmp_path, monkeypatch):
    # 第 1 镜(index=1)失败时, 报错须带场景序号
    async def fail_second(text, mp3_path, voice, rate):
        if text == "第1镜口播":
            raise ConnectionError("always fail")
        return [_w(0.0, 1.0, text)]

    async def fake_sleep(delay):
        return None

    monkeypatch.setattr(tts, "synthesize_text", fail_second)
    monkeypatch.setattr(tts.asyncio, "sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="场景 1"):
        await tts.synthesize_scenes(_script(3), tmp_path, _settings())


# ---------- integration: 真实 edge-tts ----------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_synthesize_short_text(tmp_path):
    """真实合成一句短文本: mp3 存在非空, cues 非空且时间递增。"""
    mp3 = tmp_path / "real.mp3"
    cues = await tts.synthesize_text(
        "大家好，今天我们聊聊面试准备。", mp3, "zh-CN-XiaoxiaoNeural", "+6%"
    )
    assert mp3.exists()
    assert mp3.stat().st_size > 0
    assert len(cues) > 0
    prev_end = -1.0
    for c in cues:
        assert c.start >= prev_end  # 时间递增(允许相邻条首尾相接)
        assert c.end > c.start
        assert c.text.strip()
        prev_end = c.end
