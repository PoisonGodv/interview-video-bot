"""边界检查配置规则集。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class BoundaryRules(BaseModel):
    """边界检查配置规则集。"""

    # 分镜与结构限制 (Script 核心契约)
    min_scenes: int = 5
    max_scenes: int = 8
    min_narration_chars: int = 65
    max_narration_chars: int = 110
    max_header_chars: int = 12
    max_bullet_chars: int = 14
    require_ending_scene: bool = True
    enforce_scene_count: bool = True

    # 章节限制 (系列长视频 / EpisodeOutline 契约)
    min_chapters: int = 1
    min_chapter_sec: int = 60
    max_chapter_sec: int = 600
    clamp_chapter_sec: bool = True

    # 业务红线开关
    check_redlines: bool = True

    # JSON 容错配置
    allow_trailing_commas: bool = True

    @classmethod
    def non_script(cls, **overrides: Any) -> BoundaryRules:
        """非完整脚本产物(选题/元数据/系列规划/大纲/章节草稿): 不校验分镜数与末镜 ending。"""
        return cls(**{"enforce_scene_count": False, "require_ending_scene": False, **overrides})
