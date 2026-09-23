"""TTS 配音模块(edge-tts)。

数据流: 每场景 narration → mp3 + 词级时间轴(WordBoundary, 100ns ticks)
→ merge_word_cues 合并成字幕条 → SceneMedia.cues(相对场景起点)。
全局时间轴由 offset_cues 按 SceneMedia.duration 累加平移得到。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

from .config import Settings
from .models import Cue, SceneMedia, Script

# WordBoundary 的 offset/duration 单位是 100ns ticks, 秒 = ticks / 1e7
_TICKS_PER_SECOND = 10_000_000.0

# 中文标点结尾优先断条(与 SPECS §3 一致)
_SENTENCE_END_PUNCT = "，。！？；："

# 失败重试: 2 次重试(共 3 次尝试), 指数退避基数(秒)
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0

# 兜底时长: 最后 cue 结束后再留的余量(秒), 避免画面切太早
_TAIL_PAD = 0.4


async def synthesize_text(
    text: str, mp3_path: Path, voice: str, rate: str
) -> list[Cue]:
    """合成单段文本: 写 mp3, 返回 merge 后的字幕条 cues(相对本段起点)。

    词级时间轴来自 stream() 的 WordBoundary chunk(offset/duration 为
    100ns ticks), 不经 SubMaker(其 get_srt 版本间不稳)。
    """
    communicate = edge_tts.Communicate(
        text, voice, rate=rate, boundary="WordBoundary"
    )
    words: list[Cue] = []
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    with open(mp3_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / _TICKS_PER_SECOND
                end = start + chunk["duration"] / _TICKS_PER_SECOND
                words.append(Cue(start=start, end=end, text=chunk["text"]))
    return merge_word_cues(words)


def merge_word_cues(
    words: list[Cue],
    max_chars: int = 16,
    max_gap: float = 0.6,
    max_dur: float = 4.0,
) -> list[Cue]:
    """词 cues → 字幕条 cues。

    断条规则(SPECS §3):
    - 累计字数 > max_chars 断;
    - 词间 gap > max_gap 断(边界等于不断);
    - 单条时长(首词 start → 末词 end) > max_dur 断;
    - 当前条末词以中文标点(，。！？；：)结尾, 优先断;
    - 空文本词跳过; 超长单词独占一条。
    """
    bars: list[Cue] = []
    current: list[Cue] = []

    def _close() -> None:
        nonlocal current
        if current:
            bars.append(
                Cue(
                    start=current[0].start,
                    end=current[-1].end,
                    text="".join(w.text for w in current),
                )
            )
            current = []

    for w in words:
        if not w.text.strip():
            continue  # 空文本跳过
        if current:
            gap = w.start - current[-1].end
            total_chars = sum(len(x.text) for x in current) + len(w.text)
            total_dur = w.end - current[0].start
            if gap > max_gap:
                _close()  # 词间停顿过大, 断条
            elif current[-1].text.endswith(tuple(_SENTENCE_END_PUNCT)):
                _close()  # 中文标点结尾优先断
            elif total_chars > max_chars:
                _close()  # 累计字数超限
            elif total_dur > max_dur:
                _close()  # 单条时长超限
        current.append(w)
    _close()
    return bars


def offset_cues(scene_media: list[SceneMedia]) -> list[Cue]:
    """把各场景 cues(相对场景起点)按 SceneMedia.duration 累加平移成全局时间轴。"""
    out: list[Cue] = []
    shift = 0.0
    for m in scene_media:
        for c in m.cues:
            out.append(Cue(start=c.start + shift, end=c.end + shift, text=c.text))
        shift += m.duration
    return out


def _fmt_srt_ts(t: float) -> str:
    """秒 → SRT 时间戳 HH:MM:SS,mmm。"""
    if t < 0:
        t = 0.0
    total_ms = int(round(t * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cues: list[Cue]) -> str:
    """输出标准 SRT 文本(序号 + HH:MM:SS,mmm 时间戳), 便于人工核对。"""
    blocks = [
        f"{i}\n{_fmt_srt_ts(c.start)} --> {_fmt_srt_ts(c.end)}\n{c.text}"
        for i, c in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + "\n"


async def _synthesize_with_retry(
    index: int, text: str, mp3_path: Path, settings: Settings
) -> list[Cue]:
    """单场景合成, 失败重试 2 次(指数退避), 仍失败抛带场景序号的 RuntimeError。"""
    last_err: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return await synthesize_text(
                text, mp3_path, settings.tts_voice, settings.tts_rate
            )
        except Exception as err:  # noqa: BLE001 —— 网络错误类型不可枚举, 统一重试
            last_err = err
            if attempt < _MAX_ATTEMPTS - 1:
                delay = _RETRY_BASE_DELAY * (2**attempt)
                await asyncio.sleep(delay)
    raise RuntimeError(f"场景 {index} TTS 合成失败(已重试 {_MAX_ATTEMPTS - 1} 次): {last_err}")


async def synthesize_scenes(
    script: Script, scenes_dir: Path, settings: Settings
) -> list[SceneMedia]:
    """顺序合成所有场景(避免并发被限流), 产出 scene_%03d.mp3 与 merge 后 cues。

    duration 兜底 = 最后 cue end + 0.4s; 无 cue 时仅留 0.4s。
    """
    scenes_dir.mkdir(parents=True, exist_ok=True)
    media: list[SceneMedia] = []
    for i, scene in enumerate(script.scenes):
        mp3_path = scenes_dir / f"scene_{i:03d}.mp3"
        cues = await _synthesize_with_retry(i, scene.narration, mp3_path, settings)
        duration = cues[-1].end + _TAIL_PAD if cues else _TAIL_PAD
        media.append(SceneMedia(index=i, mp3=mp3_path, cues=cues, duration=duration))
    return media
