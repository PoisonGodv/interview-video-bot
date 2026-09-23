"""数据模型 —— 全流水线共享契约。

⚠️ 本文件是各模块之间的唯一事实来源, 字段名/类型不要随意改动。
   如需变更, 必须同步修改 SPECS.md 并通知所有模块。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

SceneKind = Literal["point", "qa", "ending"]


# ---------- 内容层(LLM 产出) ----------

class Topic(BaseModel):
    """选题: 一条视频的主线。"""

    title: str = Field(description="选题标题")
    industry: str = Field(description="行业, 如 互联网/护理/汽修/教师")
    audience: str = Field(default="", description="目标观众一句话")
    hook: str = Field(default="", description="开场钩子一句话")
    key_points: list[str] = Field(default_factory=list, description="内容要点 3-6 条")


class Scene(BaseModel):
    """一个分镜 = 一张画面 + 一段口播。"""

    kind: SceneKind = Field(
        default="point",
        description="point=要点页 / qa=面试问答页 / ending=结尾CTA页",
    )
    header: str = Field(description="画面标题(<=12字)")
    bullets: list[str] = Field(
        default_factory=list, description="屏显要点, 每条<=14字, 0-4条"
    )
    narration: str = Field(description="口播文本(TTS朗读), 每场景约65~110字")
    question: str | None = Field(default=None, description="kind=qa 时的面试问题")
    answer: str | None = Field(default=None, description="kind=qa 时的答案要点(屏显)")


class Script(BaseModel):
    title: str
    scenes: list[Scene] = Field(description="5~8 个分镜, 第一镜开场, 最后一镜 kind=ending")
    summary: str = ""


# ---------- 媒体层 ----------

class Cue(BaseModel):
    """一条字幕: 秒级时间轴(相对所属片段起点)。"""

    start: float
    end: float
    text: str


class SceneMedia(BaseModel):
    """一个场景的媒体产物。tts 阶段填 mp3/cues; render 阶段填 png;
    assemble 阶段用 ffprobe 回填精确 duration。"""

    index: int
    png: Path | None = None
    mp3: Path
    duration: float = 0.0
    cues: list[Cue] = Field(default_factory=list)


class SubtitleBar(BaseModel):
    """一条全局字幕条: 1920x1080 透明 PNG, overlay 到最终视频。"""

    index: int
    png: Path
    start: float  # 相对整个视频
    end: float
    text: str


class RenderResult(BaseModel):
    cover: Path
    frames: list[Path]  # 与 script.scenes 一一对应, 有序


class Chapter(BaseModel):
    """B站简介里的章节跳转。"""

    index: int
    title: str
    start: float


# ---------- 发布层 ----------

class BiliMeta(BaseModel):
    title: str = Field(description="B站标题, <=80字")
    desc: str = Field(default="", description="简介, 含章节时间戳")
    tags: list[str] = Field(default_factory=list, description="5~10 个标签")


class ProjectManifest(BaseModel):
    """一次生成的完整产物清单, 落盘为 project.json。"""

    created_at: datetime
    industry: str = ""
    script: Script
    video: Path
    cover: Path
    srt: Path | None = None
    chapters: list[Chapter] = Field(default_factory=list)
    meta: BiliMeta | None = None
    voice: str = ""
    duration_total: float = 0.0
