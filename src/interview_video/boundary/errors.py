"""边界违规定义与核心异常 (保留第一现场)。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ViolationCategory = Literal["schema", "length", "structure", "redline"]


class Violation(BaseModel):
    """单条边界违规定义。"""

    category: ViolationCategory = Field(
        description="违规类别: schema | length | structure | redline"
    )
    field: str = Field(default="", description="违规字段路径, 如 scenes[1].header / json")
    message: str = Field(description="违规事实描述")
    value: Any = Field(default=None, description="违规值或问题片段(可选)")
    fix_hint: str = Field(default="", description="具体的修复建议与要求")

    def __str__(self) -> str:
        loc = f"[{self.field}] " if self.field else ""
        hint = f" -> 建议: {self.fix_hint}" if self.fix_hint else ""
        return f"[{self.category.upper()}] {loc}{self.message}{hint}"


class BoundaryViolationError(Exception):
    """模型输出冲出边界且自修复回路耗尽后抛出的核心异常，保留第一现场完整凭证。"""

    def __init__(
        self,
        message: str,
        *,
        violations: list[Violation],
        raw_output: str,
        repaired_output: str | None = None,
        original_exception: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.violations = violations
        self.raw_output = raw_output
        self.repaired_output = repaired_output
        self.original_exception = original_exception

    def summary(self) -> dict[str, Any]:
        """结构化违规诊断摘要。"""
        return {
            "message": self.message,
            "violations_count": len(self.violations),
            "categories": sorted({v.category for v in self.violations}),
            "violations": [v.model_dump() for v in self.violations],
            "raw_output_snippet": self.raw_output[:300] if self.raw_output else "",
            "repaired_output_snippet": self.repaired_output[:300] if self.repaired_output else "",
        }

    def __str__(self) -> str:
        lines = [
            f"BoundaryViolationError: {self.message}",
            f"检测到违规项共 {len(self.violations)} 条:",
        ]
        for i, v in enumerate(self.violations, 1):
            lines.append(f"  {i}. {v}")
        lines.append("--- [第一现场] 原始输出 (截断前500字符) ---")
        lines.append(self.raw_output[:500] if self.raw_output else "(空)")
        if self.repaired_output is not None:
            lines.append("--- [修复现场] 修复后输出 (截断前500字符) ---")
            lines.append(self.repaired_output[:500] if self.repaired_output else "(空)")
        return "\n".join(lines)
