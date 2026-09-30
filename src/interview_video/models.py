"""数据模型 —— 全流水线共享契约。

⚠️ 本文件是各模块之间的唯一事实来源, 字段名/类型不要随意改动。
   如需变更, 必须同步修改 SPECS.md 并通知所有模块。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

SceneKind = Literal["point", "qa", "ending"]

_STRICT_KEY = "strict_script"


def _strict(info: ValidationInfo) -> bool:
    return bool(info.context and info.context.get(_STRICT_KEY))


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

    @field_validator("narration")
    @classmethod
    def _narration_not_blank(cls, v: str, info: ValidationInfo) -> str:
        if _strict(info) and not v.strip():
            raise ValueError("口播 narration 不能为空")
        return v

    @model_validator(mode="after")
    def _qa_fields(self, info: ValidationInfo) -> Scene:
        if _strict(info) and self.kind == "qa":
            missing = [f for f in ("question", "answer") if not (getattr(self, f) or "").strip()]
            if missing:
                raise ValueError(f"kind=qa 时 {'/'.join(missing)} 不能为空")
        return self


class Script(BaseModel):
    title: str
    scenes: list[Scene] = Field(description="5~8 个分镜, 第一镜开场, 最后一镜 kind=ending")
    summary: str = ""

    @field_validator("scenes")
    @classmethod
    def _scenes_not_empty(cls, v: list[Scene], info: ValidationInfo) -> list[Scene]:
        if _strict(info) and not v:
            raise ValueError("不能为空(至少 1 个分镜)")
        return v


def validate_script(data: object) -> Script:
    """外部输入(脚本文件/Web 编辑/续跑)的严格校验: scenes 非空、口播非空、qa 须有问答。

    直接构造 Script(...) 或解析 LLM 输出不走严格模式(由边界规则负责修复)。
    """
    if isinstance(data, (str, bytes)):
        return Script.model_validate_json(data, context={_STRICT_KEY: True})
    return Script.model_validate(data, context={_STRICT_KEY: True})


class ScriptFileError(ValueError):
    """脚本文件不存在 / JSON 语法错误 / 字段不合法。"""


_ZH_MSG = {
    "missing": "缺少必填字段",
    "too_short": "不能为空(至少 1 项)",
    "string_type": "须为字符串",
    "list_type": "须为数组",
    "model_type": "须为对象",
    "model_attributes_type": "须为对象",
}


def format_validation_error(exc: ValidationError) -> str:
    """ValidationError → 'scenes[1].kind: ...; ...'(下标用 [i], 便于对照 JSON)。"""
    parts = []
    for e in exc.errors():
        loc = ""
        for p in e["loc"]:
            loc += f"[{p}]" if isinstance(p, int) else (f".{p}" if loc else str(p))
        msg = _ZH_MSG.get(e["type"]) or e["msg"].removeprefix("Value error, ")
        if e["type"] == "literal_error":
            msg = "须为 " + "/".join(re.findall(r"'([^']*)'", str(e.get("ctx", {}).get("expected", ""))))
        parts.append(f"{loc or '(根)'}: {msg}")
    return "; ".join(parts)


# 免 LLM 冒烟脚本: 随包分发(package data), 源码仓库里退回 examples/ 原件
SAMPLE_SCRIPT_CANDIDATES = (
    Path(__file__).parent / "data" / "sample_script.json",
    Path(__file__).resolve().parents[2] / "examples" / "sample_script.json",
)


def sample_script_path() -> Path | None:
    """内置示例脚本(ivb generate --sample / Web「用示例脚本」); 安装包不完整时返回 None。"""
    return next((p for p in SAMPLE_SCRIPT_CANDIDATES if p.is_file()), None)


def load_script_file(path: Path | str) -> Script:
    """读取并校验脚本 JSON; 任何问题都转成带绝对路径与字段位置的 ScriptFileError。"""
    p = Path(path).expanduser()
    where = p.resolve()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ScriptFileError(f"脚本文件不存在: {where}") from None
    except OSError as exc:
        raise ScriptFileError(f"脚本文件读取失败 ({where}): {exc}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScriptFileError(
            f"{p.name} 不是合法 JSON ({where}): 第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}"
        ) from None
    try:
        return validate_script(data)
    except ValidationError as exc:
        raise ScriptFileError(
            f"{p.name} 格式错误 ({where}): {format_validation_error(exc)}"
        ) from None


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


# ---------- 系列长视频(多 agent 编排产物) ----------

class EpisodePlan(BaseModel):
    """系列中的一集规划。"""

    index: int = Field(description="集序号, 从 1 开始")
    title: str
    summary: str = Field(default="", description="本集内容概要")
    key_points: list[str] = Field(default_factory=list)
    target_sec: int = Field(default=900, description="本集目标时长(秒)")


class SeriesPlan(BaseModel):
    """系列总规划(策划 agent 产出)。"""

    theme: str
    title: str = Field(description="系列名")
    synopsis: str = Field(default="", description="系列圣经: 主线/风格/受众, 供所有章节共享")
    episodes: list[EpisodePlan]


class ChapterSpec(BaseModel):
    """一章的规格(上下文切分单元: 章节写手只看本章规格+滚动摘要)。"""

    title: str
    goal: str = Field(default="", description="本章要解决的问题/讲清的点")
    key_points: list[str] = Field(default_factory=list)
    est_sec: int = Field(default=150, description="本章预计时长(秒)")


class EpisodeOutline(BaseModel):
    """一集的章节大纲(分集编剧产出)。"""

    episode_index: int
    title: str
    chapters: list[ChapterSpec]


class ChapterDraft(BaseModel):
    """一章的分镜草稿(章节写手产出) + 供下一章用的滚动摘要。"""

    scenes: list[Scene]
    summary: str = Field(default="", description="本章内容摘要(<=120字), 传给下一章做上下文")


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
    # 生成溯源(新产物都写; 旧 project.json 无此字段, 向后兼容)
    generation: dict[str, str] | None = Field(
        default=None,
        description="llm_model/temperature/prompt 版本等溯源信息",
    )
