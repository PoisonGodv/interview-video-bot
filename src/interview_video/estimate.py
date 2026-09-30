"""系列长视频费用预估: 集数 × 每集分钟 → LLM 调用次数 / token / TTS 字符数(+可选金额)。

调用结构与 series.py / chains/series.py 一致:
    策划 1 次 → 每集: 分集编剧 1 次 + 每章(写手 1 次 + 审稿 1 次, SERIES_REVIEW 可关)
    → 每集媒体流水线: B站元数据 1 次。
章数 = 每集秒数 / SERIES_CHAPTER_SEC 取整(与大纲 prompt 一致); 每章分镜数同写手的 clamp。
token 按中文字符粗估(prompt 模板 + 上下文 + 预期输出), 仅作量级参考;
「最坏」按每次结构化调用都触发一次修复重问(×2)计。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from .config import Settings

# CLI 与 Web 共用的系列参数上限(20 集 × 60 分钟这类输入基本是误操作)
SERIES_MAX_EPISODES = 12
SERIES_MIN_EPISODE_MIN = 3.0
SERIES_MAX_EPISODE_MIN = 30.0

# 中文口播语速(字/分钟), TTS 计费字符数据此估算
SPEECH_CHARS_PER_MIN = 270
# 中文字符 → token 粗略换算(各家分词器不同, 偏保守)
TOKENS_PER_CHAR = 0.7

# 各角色 prompt 规模(字符): 模板 + 系列圣经/规格/滚动摘要等上下文
_PLAN_IN, _PLAN_OUT_PER_EP, _PLAN_OUT_BASE = 600, 250, 300
_OUTLINE_IN, _OUTLINE_OUT_PER_CH, _OUTLINE_OUT_BASE = 1100, 150, 100
_CHAPTER_IN, _SCENE_OUT, _CHAPTER_OUT_BASE = 1700, 260, 150
_REVIEW_IN = 1300
_META_IN, _META_OUT, _META_PER_SCENE = 700, 400, 20
_SCENES_MIN, _SCENES_MAX = 4, 30


@dataclass
class SeriesEstimate:
    episodes: int
    duration_min: float
    chapters_per_episode: int
    scenes_per_chapter: int
    review: bool
    llm_calls: int
    llm_calls_worst: int
    input_tokens: int
    output_tokens: int
    tts_chars: int
    tts_provider: str
    llm_cost: float | None = None
    tts_cost: float | None = None
    total_cost: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "total_tokens": self.total_tokens}

    def lines(self) -> list[str]:
        """人类可读的预估摘要(CLI 面板用)。"""
        out = [
            f"规模: {self.episodes} 集 × {self.duration_min:g} 分钟"
            f"(每集约 {self.chapters_per_episode} 章 × {self.scenes_per_chapter} 镜, "
            f"审稿{'开' if self.review else '关'})",
            f"LLM: 约 {self.llm_calls} 次调用(最坏 {self.llm_calls_worst} 次), "
            f"tokens 入 ~{self.input_tokens} / 出 ~{self.output_tokens} / 共 ~{self.total_tokens}",
            f"TTS({self.tts_provider}): 约 {self.tts_chars} 字",
        ]
        if self.total_cost is not None:
            parts = []
            if self.llm_cost is not None:
                parts.append(f"LLM {self.llm_cost:.2f}")
            if self.tts_cost is not None:
                parts.append(f"TTS {self.tts_cost:.2f}")
            out.append(f"费用: 约 {self.total_cost:.2f}({' + '.join(parts)}, 按 .env 单价)")
        else:
            out.append("费用: 未配置单价(LLM_PRICE_INPUT_PER_1K 等), 仅显示用量")
        return out


def _tokens(chars: float) -> int:
    return math.ceil(chars * TOKENS_PER_CHAR)


def estimate_series(settings: Settings, episodes: int, duration_min: float) -> SeriesEstimate:
    """按系列调用结构估算一次完整系列运行(含每集元数据)的用量与可选费用。"""
    episode_sec = int(duration_min * 60)
    chapter_sec = max(1, settings.series_chapter_sec)
    chapters = max(1, round(episode_sec / chapter_sec))
    ch_est_sec = episode_sec / chapters
    scenes = min(_SCENES_MAX, max(_SCENES_MIN, round(ch_est_sec / 20)))
    review = settings.series_review

    chapter_out = _CHAPTER_OUT_BASE + scenes * _SCENE_OUT
    per_ep_calls = 1 + chapters * (2 if review else 1) + 1  # 编剧 + 章节(+审稿) + 元数据
    calls = 1 + episodes * per_ep_calls

    in_chars = _PLAN_IN + episodes * (
        _OUTLINE_IN
        + chapters * (_CHAPTER_IN + ((_REVIEW_IN + chapter_out) if review else 0))
        + _META_IN + chapters * scenes * _META_PER_SCENE
    )
    out_chars = (_PLAN_OUT_BASE + episodes * _PLAN_OUT_PER_EP) + episodes * (
        _OUTLINE_OUT_BASE + chapters * _OUTLINE_OUT_PER_CH
        + chapters * chapter_out * (2 if review else 1)
        + _META_OUT
    )
    input_tokens, output_tokens = _tokens(in_chars), _tokens(out_chars)
    tts_chars = math.ceil(episodes * duration_min * SPEECH_CHARS_PER_MIN)

    llm_cost = tts_cost = None
    pin, pout = settings.llm_price_input_per_1k, settings.llm_price_output_per_1k
    if pin is not None or pout is not None:
        llm_cost = input_tokens / 1000 * (pin or 0) + output_tokens / 1000 * (pout or 0)
    if settings.tts_price_per_10k_chars is not None:
        tts_cost = tts_chars / 10000 * settings.tts_price_per_10k_chars
    total = None
    if llm_cost is not None or tts_cost is not None:
        total = (llm_cost or 0) + (tts_cost or 0)

    return SeriesEstimate(
        episodes=episodes,
        duration_min=duration_min,
        chapters_per_episode=chapters,
        scenes_per_chapter=scenes,
        review=review,
        llm_calls=calls,
        llm_calls_worst=calls * 2,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tts_chars=tts_chars,
        tts_provider=settings.tts_provider,
        llm_cost=llm_cost,
        tts_cost=tts_cost,
        total_cost=total,
    )


def validate_series_bounds(episodes: int, duration_min: float) -> str | None:
    """CLI/Web 共用的参数边界; 越界返回错误文案, 合法返回 None。"""
    if not 1 <= episodes <= SERIES_MAX_EPISODES:
        return f"集数需在 1~{SERIES_MAX_EPISODES} 之间, 当前 {episodes}"
    if not SERIES_MIN_EPISODE_MIN <= duration_min <= SERIES_MAX_EPISODE_MIN:
        return (
            f"每集时长需在 {SERIES_MIN_EPISODE_MIN:g}~{SERIES_MAX_EPISODE_MIN:g} 分钟之间, "
            f"当前 {duration_min:g}"
        )
    return None
