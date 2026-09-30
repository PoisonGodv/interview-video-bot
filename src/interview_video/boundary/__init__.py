"""模型输出边界防护体系 (Model Output Boundary Guardrails).

提供纯净、自洽、确定性的模型输出边界防护，涵盖 4 个核心维度:
1. Schema 边界:
   - 容错 JSON 提取: 剥离 markdown 围栏 (```json / ```)、剔除思维链标签 (<think>...</think>)、
     平衡括号匹配 (支持嵌套、转义与字符串识别)、容忍末尾逗号;
   - Pydantic BaseModel 强类型校验与字段缺失/类型违例感知。
2. 长度与结构边界:
   - 分镜数量: 5~8 镜 (针对 Script 契约);
   - 口播字数: 65~110 字 (单场景 narration);
   - 画面标题: <=12 字 (header);
   - 要点字数: <=14 字 (bullets 每条);
   - 末镜结构: 末镜必须为 kind="ending";
   - 章节数量与时长: 章节数量 >= 1，时长确定性 clamp [60, 600]s (clamp_duration).
3. 业务红线边界 (Redlines):
   - 严禁编造薪资数字 (如 "月薪2万", "年薪30W", "起薪25k");
   - 严禁包过承诺 (如 "三天包过", "必上岸", "保offer");
   - 严禁年龄/性别/学历歧视 (如 "只要985", "35岁不要", "限男性");
   - 严禁诋毁 (如 "黑心企业", "某大厂太垃圾", "骗子公司")。
4. 自修复回路 (Self-Repair Loop):
   - 边界违背项结构化捕获 (Violation: category, field, message, fix_hint);
   - 构造精确、带第一现场和违规反馈的 user 消息重问指令;
   - 温度调低发起单次有边界的重问修复;
   - 全失败时抛出保留完整第一现场 (raw_output, repaired_output, violations, cause) 的 BoundaryViolationError;
   - 统一入口函数: enforce_boundary(prompt, caller, schema, boundary_rules...).
"""
from __future__ import annotations

from .errors import BoundaryViolationError, Violation, ViolationCategory
from .parsing import extract_json_text, parse_json_lenient
from .redlines import (
    REDLINE_PATTERNS,
    scan_object_redlines,
    scan_redlines,
    scan_text_redlines,
)
from .repair import build_repair_prompt, enforce_boundary, enforce_boundary_sync
from .rules import BoundaryRules
from .structure import (
    BoundaryChecker,
    clamp_chapters,
    clamp_duration,
    fix_script_deterministic,
    truncate_narration,
)

__all__ = [
    "ViolationCategory",
    "Violation",
    "BoundaryViolationError",
    "BoundaryRules",
    "BoundaryChecker",
    "REDLINE_PATTERNS",
    "scan_text_redlines",
    "scan_redlines",
    "scan_object_redlines",
    "extract_json_text",
    "parse_json_lenient",
    "clamp_duration",
    "clamp_chapters",
    "truncate_narration",
    "fix_script_deterministic",
    "build_repair_prompt",
    "enforce_boundary",
    "enforce_boundary_sync",
]
