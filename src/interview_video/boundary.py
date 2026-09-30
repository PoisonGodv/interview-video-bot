"""模型输出边界防护体系 (Model Output Boundary Guardrails).

在脱离 LangChain 之后提供纯净、自洽、确定性的输出边界防护，涵盖 4 个核心维度:
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
   - 构造精确、带第一现场和违规反馈的 UserMessage 重问指令;
   - 温度调低发起单次有边界的重问修复;
   - 全失败时抛出保留完整第一现场 (raw_output, repaired_output, violations, cause) 的 BoundaryViolationError;
   - 统一入口函数: enforce_boundary(prompt, caller, schema, boundary_rules...).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T")

ViolationCategory = Literal["schema", "length", "structure", "redline"]


# ===========================================================================
# 1. 违规定义与核心异常 (保留第一现场)
# ===========================================================================

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


# ===========================================================================
# 2. 边界规则集定义
# ===========================================================================

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


# ===========================================================================
# 3. 业务红线模式与扫描器
# ===========================================================================

REDLINE_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # (规则名, 正则, 修复指导)
    (
        "编造薪资数字",
        re.compile(r"(?:月薪|月入|年薪|起薪|日薪|时薪|底薪|薪水|工资)[约莫为达:：\s]*\d[\d,.]*\s*[wWkK万千元块]?"),
        "严禁编造具体薪资数字，请使用『因城市/公司差异较大』等客观表述",
    ),
    (
        "编造薪资数字",
        re.compile(r"\d[\d,.]*\s*[wWkK万千元块]\s*[的]?(?:月薪|年薪|起薪|日薪|时薪|工资|包|package)"),
        "严禁编造具体薪资数字，请使用『因城市/公司差异较大』等客观表述",
    ),
    (
        "编造薪资数字",
        re.compile(r"\d+\s*[-~至到]\s*\d+\s*[kKwW万千](?:/月|/年|月薪|年薪)?"),
        "严禁编造具体薪资范围，涉及薪资请用泛指或面议代替",
    ),
    (
        "包过承诺",
        re.compile(r"(包过|必上岸|必定上岸|稳过|保\s?offer|稳拿\s?offer|保过|保证通过|一次过|100%通过|保录取|包录取|包就业)"),
        "严禁做出『包过/必上岸』等绝对化结果承诺，可改为『帮助提高通过率』或『提升面试竞争力』",
    ),
    (
        "歧视性表述",
        re.compile(
            r"(?:只要|不要|限制|限|只招|优先招|只录用|必须是)[^。,，\n]{0,8}"
            r"(?:35岁|三十[一二三四五]?岁|男性|女性|男生|女生|985|211|双一流|本科以上|大专以下|非全日制不要|已婚已育)"
        ),
        "严禁含有年龄/性别/学历等任何歧视性表述，应保持客观中立与职业包容",
    ),
    (
        "歧视性表述",
        re.compile(r"(?:35岁|三十[一二三四五]?岁)[^。,，\n]{0,6}(?:不要|pass|淘汰|劝退|下岗|危机不要|不招)"),
        "严禁年龄歧视表述，探讨年龄应聚焦于经验优势与转型方法",
    ),
    (
        "诋毁贬损",
        re.compile(
            r"(?:垃圾|黑心|骗子|坑爹|坑人|恶心|无良|吸血|作恶|避坑这家|千万别去这家)[^。,，\n]{0,8}"
            r"(?:公司|企业|大厂|用人单位|机构|老板|HR)"
        ),
        "严禁诋毁或攻击任何具体企业或个人，应以专业客观角度分析职场避坑",
    ),
    (
        "诋毁贬损",
        re.compile(
            r"(?:公司|企业|大厂|用人单位|机构|老板|HR)[^。,，\n]{0,8}"
            r"(?:太垃圾|是骗子|全都是坑|是黑心|太恶心|是吸血鬼|坑人|毫无诚信)"
        ),
        "严禁诋毁或攻击任何具体企业或个人，应以专业客观角度分析职场避坑",
    ),
]


def scan_text_redlines(text: str) -> list[tuple[str, str, str]]:
    """扫描单段纯文本中的红线，返回 (规则名, 匹配到的文本片段, 修复建议) 列表。"""
    hits: list[tuple[str, str, str]] = []
    seen_rules: set[str] = set()
    for rule_name, pattern, hint in REDLINE_PATTERNS:
        match = pattern.search(text)
        if match:
            # 记录匹配到的具体文字，便于在反馈中展示
            matched_snippet = match.group(0)
            hits.append((rule_name, matched_snippet, hint))
            seen_rules.add(rule_name)
    return hits


def scan_redlines(text: str) -> list[str]:
    """向后兼容的纯文本红线扫描器，返回命中的红线规则名列表 (去重)。"""
    hits: list[str] = []
    for rule_name, pattern, _ in REDLINE_PATTERNS:
        if rule_name not in hits and pattern.search(text):
            hits.append(rule_name)
    return hits


def scan_object_redlines(obj: Any, path: str = "") -> list[Violation]:
    """递归遍历对象/字典/列表中的所有文本字段，进行业务红线扫描，精确定位违规字段。"""
    violations: list[Violation] = []

    if obj is None:
        return violations

    if isinstance(obj, str):
        hits = scan_text_redlines(obj)
        for rule_name, matched_text, hint in hits:
            violations.append(
                Violation(
                    category="redline",
                    field=path or "text",
                    message=f"命中业务红线[{rule_name}]: 发现违规内容 '{matched_text}'",
                    value=matched_text,
                    fix_hint=hint,
                )
            )
        return violations

    if isinstance(obj, BaseModel):
        for field_name in type(obj).model_fields:
            val = getattr(obj, field_name)
            sub_path = f"{path}.{field_name}" if path else field_name
            violations.extend(scan_object_redlines(val, sub_path))
        return violations

    if isinstance(obj, dict):
        for k, v in obj.items():
            sub_path = f"{path}.{k}" if path else str(k)
            violations.extend(scan_object_redlines(v, sub_path))
        return violations

    if isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            sub_path = f"{path}[{idx}]"
            violations.extend(scan_object_redlines(item, sub_path))
        return violations

    return violations


# ===========================================================================
# 4. 容错 JSON 提取与解析 (Schema 边界)
# ===========================================================================

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?([\s\S]*?)```", re.DOTALL)


def extract_json_text(text: str) -> str:
    """从模型输出中抽取纯净的 JSON 文本。

    容错策略:
    1. 剔除模型推理思维链标签 (<think>...</think>);
    2. 优先匹配 ```json ... ``` 或 ``` ... ``` 围栏代码块;
    3. 支持前后废话文字剥离，基于括号平衡算法精确定位首个闭合的 JSON 对象 {} 或数组 [].
    """
    cleaned = _THINK_RE.sub("", text).strip()

    # 1. 优先尝试从代码围栏中提取
    for block in _FENCE_RE.findall(cleaned):
        block = block.strip()
        if "{" in block or "[" in block:
            cleaned = block
            break

    # 2. 定位首个 { 或 [
    start = -1
    open_ch = ""
    for i, ch in enumerate(cleaned):
        if ch in "{[":
            start = i
            open_ch = ch
            break

    if start < 0:
        raise ValueError(f"输出中未找到 JSON 起始标记 ('{{' 或 '['): {cleaned[:80]!r}")

    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escape = False

    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return cleaned[start : i + 1]

    raise ValueError(f"JSON 括号不配对或未闭合 (缺少配对的 '{close_ch}'): {cleaned[:80]!r}")


def parse_json_lenient(text: str, allow_trailing_commas: bool = True) -> Any:
    """容错 JSON 解析: 提取 JSON 文本后反序列化，支持尾部逗号容错。"""
    raw = extract_json_text(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        if allow_trailing_commas:
            # 容忍对象或数组尾部的悬挂逗号
            no_trailing = re.sub(r",\s*([\]}])", r"\1", raw)
            try:
                return json.loads(no_trailing)
            except json.JSONDecodeError:
                pass
        raise ValueError(
            f"JSON 解析失败: {exc.msg} (第 {exc.lineno} 行, 第 {exc.colno} 列); 提取文本: {raw[:100]!r}"
        ) from exc


# ===========================================================================
# 5. 长度与结构确定性修正算子 (Clamp / Truncate)
# ===========================================================================

def clamp_duration(sec: int | float, min_sec: int = 60, max_sec: int = 600) -> int:
    """章节时长 clamp 算子: [min_sec, max_sec] 范围夹紧。"""
    try:
        val = int(round(float(sec)))
    except (ValueError, TypeError):
        return min_sec
    return max(min_sec, min(val, max_sec))


def clamp_chapters(obj: Any, min_sec: int = 60, max_sec: int = 600) -> Any:
    """对对象包含的所有章节规格的时长执行 clamp [min_sec, max_sec]。"""
    chapters = getattr(obj, "chapters", None)
    if chapters is None and isinstance(obj, dict):
        chapters = obj.get("chapters")

    if isinstance(chapters, list):
        for ch in chapters:
            if hasattr(ch, "est_sec"):
                ch.est_sec = clamp_duration(ch.est_sec, min_sec, max_sec)
            elif hasattr(ch, "target_sec"):
                ch.target_sec = clamp_duration(ch.target_sec, min_sec, max_sec)
            elif isinstance(ch, dict):
                if "est_sec" in ch:
                    ch["est_sec"] = clamp_duration(ch["est_sec"], min_sec, max_sec)
                elif "target_sec" in ch:
                    ch["target_sec"] = clamp_duration(ch["target_sec"], min_sec, max_sec)
    return obj


_SENTENCE_END = "。！？!?…"
_PAUSE_END = "，、,;；:："


def truncate_narration(
    text: str,
    max_chars: int = 110,
    min_chars: int = 65,
) -> str:
    """把超长口播截断到自然句读处, 保证结果 <= max_chars 且以句末标点收尾。

    截断策略:
    1. 在 max_chars 窗口内优先找最后一个句末标点 (。！？!?) 截断;
    2. 首句仍超长时退到窗口内最后一个停顿标点 (，、;；等), 并补为「。」;
    3. 找不到合适标点时硬截断到 max_chars - 1 并补「。」。
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text

    window = text[:max_chars]
    # 1) 句末标点截断
    cut = max((window.rfind(p) for p in _SENTENCE_END), default=-1)
    if cut + 1 >= min_chars:
        return window[: cut + 1]

    # 2) 停顿标点截断(收为句末)
    cut = max((window.rfind(p) for p in _PAUSE_END), default=-1)
    if cut + 1 >= min_chars:
        return window[: cut] + "。"

    # 3) 硬截断兜底(预留 1 字给收尾句号)
    hard = window[: max_chars - 1].rstrip(_PAUSE_END + _SENTENCE_END)
    if not hard:
        hard = window[:max_chars].rstrip(_PAUSE_END)
    return hard + "。"


def fix_script_deterministic(script: Any, rules: BoundaryRules | None = None) -> Any:
    """对 Script 对象的 narration 和 ending 属性执行确定性收敛修正。"""
    r = rules or BoundaryRules()
    scenes = getattr(script, "scenes", None)
    if not isinstance(scenes, list) or not scenes:
        return script

    for scene in scenes:
        narr = getattr(scene, "narration", None)
        if narr:
            scene.narration = truncate_narration(
                narr, max_chars=r.max_narration_chars, min_chars=r.min_narration_chars
            )

    if r.require_ending_scene and getattr(scenes[-1], "kind", None) != "ending":
        scenes[-1].kind = "ending"

    return script


# ===========================================================================
# 6. 边界检查类 (BoundaryChecker)
# ===========================================================================

class BoundaryChecker:
    """模型输出边界检查器: 组合 Schema、长度与结构、业务红线三个维度的硬核校验。"""

    def __init__(self, rules: BoundaryRules | None = None) -> None:
        self.rules = rules or BoundaryRules()

    def check(
        self,
        raw_output: str,
        schema: type[T],
    ) -> tuple[T | None, list[Violation]]:
        """对原始模型输出执行全维度边界检查。

        返回 (validated_instance, violations):
        - 完全合规: violations 为空列表，返回解析后的 schema 实例;
        - 违背边界: 返回具体的所有 Violation 列表，instance 可能为 None。
        """
        violations: list[Violation] = []

        # 1. 尝试解析 JSON
        try:
            data = parse_json_lenient(
                raw_output,
                allow_trailing_commas=self.rules.allow_trailing_commas,
            )
        except Exception as exc:
            violations.append(
                Violation(
                    category="schema",
                    field="json",
                    message=f"JSON解析失败: {exc}",
                    fix_hint="必须只输出格式正确的 JSON，不要添加解释文字或围栏，注意引号、逗号及括号闭合。",
                )
            )
            # JSON 损坏时，仍然对 raw_output 进行红线检查以提供完整诊断反馈
            if self.rules.check_redlines:
                raw_hits = scan_text_redlines(raw_output)
                for rule_name, matched_text, hint in raw_hits:
                    violations.append(
                        Violation(
                            category="redline",
                            field="raw_text",
                            message=f"命中业务红线[{rule_name}]: 发现违规内容 '{matched_text}'",
                            value=matched_text,
                            fix_hint=hint,
                        )
                    )
            return None, violations

        # 2. Pydantic BaseModel 校验
        instance: T | None = None
        try:
            if isinstance(schema, type) and issubclass(schema, BaseModel):
                fields = schema.model_fields
                if isinstance(data, list) and len(fields) == 1:
                    sole_field = next(iter(fields.keys()))
                    try:
                        instance = schema.model_validate({sole_field: data})
                    except ValidationError:
                        instance = schema.model_validate(data)
                else:
                    instance = schema.model_validate(data)
            else:
                instance = data
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", [])) or "root"
                msg = err.get("msg", "")
                err_type = err.get("type", "")
                violations.append(
                    Violation(
                        category="schema",
                        field=loc,
                        message=f"字段校验失败 ({err_type}): {msg}",
                        fix_hint=f"请按照 Schema 要求补齐或修正字段 '{loc}' 的值与类型。",
                    )
                )
        except Exception as exc:
            violations.append(
                Violation(
                    category="schema",
                    field="schema",
                    message=f"数据模型转换失败: {exc}",
                    fix_hint="请确保输出的数据结构与要求的 Schema 字段完全对应。",
                )
            )

        if instance is None:
            # Schema 结构未通过时，也对已解析的 dict 数据扫描红线
            if self.rules.check_redlines and isinstance(data, (dict, list)):
                violations.extend(scan_object_redlines(data))
            return None, violations

        # 3. 长度与结构边界检查 (Length & Structure)
        struct_violations = self.check_length_and_structure(instance)
        violations.extend(struct_violations)

        # 4. 业务红线边界检查 (Redlines)
        if self.rules.check_redlines:
            redline_violations = scan_object_redlines(instance)
            violations.extend(redline_violations)

        return instance, violations

    def check_length_and_structure(self, obj: Any) -> list[Violation]:
        """检查长度与结构边界约束: 分镜数、口播字数、标题字数、要点字数、末镜类型与章节时长。"""
        violations: list[Violation] = []
        rules = self.rules

        # ---- A. 分镜结构 (scenes) 校验 ----
        scenes = getattr(obj, "scenes", None)
        if scenes is None and isinstance(obj, dict):
            scenes = obj.get("scenes")

        if isinstance(scenes, list):
            obj_type = type(obj).__name__
            is_script_like = obj_type == "Script" or (
                hasattr(obj, "title") and hasattr(obj, "scenes") and hasattr(obj, "summary")
            )

            # 1. 分镜数量 (5~8 镜) - 对完整脚本执行
            if (is_script_like or rules.enforce_scene_count) and obj_type != "ChapterDraft":
                if len(scenes) < rules.min_scenes:
                    violations.append(
                        Violation(
                            category="structure",
                            field="scenes",
                            message=f"分镜数量不足: 当前 {len(scenes)} 镜 (必须在 {rules.min_scenes}~{rules.max_scenes} 镜之间)",
                            value=len(scenes),
                            fix_hint=f"请增加分镜数量至 {rules.min_scenes}~{rules.max_scenes} 镜。",
                        )
                    )
                elif len(scenes) > rules.max_scenes:
                    violations.append(
                        Violation(
                            category="structure",
                            field="scenes",
                            message=f"分镜数量过多: 当前 {len(scenes)} 镜 (必须在 {rules.min_scenes}~{rules.max_scenes} 镜之间)",
                            value=len(scenes),
                            fix_hint=f"请精简并合并分镜至 {rules.min_scenes}~{rules.max_scenes} 镜。",
                        )
                    )

            # 2. 逐镜检查 (header, bullets, narration)
            for i, scene in enumerate(scenes):
                header = (
                    getattr(scene, "header", None)
                    if hasattr(scene, "header")
                    else (scene.get("header") if isinstance(scene, dict) else None)
                )
                if header is not None and len(str(header)) > rules.max_header_chars:
                    violations.append(
                        Violation(
                            category="length",
                            field=f"scenes[{i}].header",
                            message=f"场景 {i+1} 画面标题超长: 当前 {len(str(header))} 字 (上限 {rules.max_header_chars} 字) -> '{header}'",
                            value=header,
                            fix_hint=f"画面标题 header 必须 <= {rules.max_header_chars} 字，请精简。",
                        )
                    )

                bullets = (
                    getattr(scene, "bullets", None)
                    if hasattr(scene, "bullets")
                    else (scene.get("bullets") if isinstance(scene, dict) else None)
                )
                if isinstance(bullets, list):
                    for j, b in enumerate(bullets):
                        b_str = str(b)
                        if len(b_str) > rules.max_bullet_chars:
                            violations.append(
                                Violation(
                                    category="length",
                                    field=f"scenes[{i}].bullets[{j}]",
                                    message=f"场景 {i+1} 要点 {j+1} 超长: 当前 {len(b_str)} 字 (上限 {rules.max_bullet_chars} 字) -> '{b_str}'",
                                    value=b_str,
                                    fix_hint=f"屏显要点 bullet 必须 <= {rules.max_bullet_chars} 字，请精简。",
                                )
                            )

                narration = (
                    getattr(scene, "narration", None)
                    if hasattr(scene, "narration")
                    else (scene.get("narration") if isinstance(scene, dict) else None)
                )
                if narration is not None:
                    narr_len = len(str(narration))
                    if narr_len > rules.max_narration_chars:
                        violations.append(
                            Violation(
                                category="length",
                                field=f"scenes[{i}].narration",
                                message=f"场景 {i+1} 口播字数超长: 当前 {narr_len} 字 (上限 {rules.max_narration_chars} 字)",
                                value=narr_len,
                                fix_hint=f"口播 narration 必须精简至 {rules.min_narration_chars}~{rules.max_narration_chars} 字。",
                            )
                        )
                    elif narr_len < rules.min_narration_chars:
                        violations.append(
                            Violation(
                                category="length",
                                field=f"scenes[{i}].narration",
                                message=f"场景 {i+1} 口播字数不足: 当前 {narr_len} 字 (下限 {rules.min_narration_chars} 字)",
                                value=narr_len,
                                fix_hint=f"口播 narration 必须扩充至 {rules.min_narration_chars}~{rules.max_narration_chars} 字。",
                            )
                        )

            # 3. 末镜必须是 ending (针对完整 Script)
            if scenes and rules.require_ending_scene and obj_type != "ChapterDraft":
                last_scene = scenes[-1]
                kind = (
                    getattr(last_scene, "kind", None)
                    if hasattr(last_scene, "kind")
                    else (last_scene.get("kind") if isinstance(last_scene, dict) else None)
                )
                if kind != "ending":
                    violations.append(
                        Violation(
                            category="structure",
                            field=f"scenes[{len(scenes)-1}].kind",
                            message=f"末镜 kind 必须为 'ending' (当前为 '{kind}')",
                            value=kind,
                            fix_hint="最后一镜 kind 必须设为 'ending'，并包含关注/下期引导内容。",
                        )
                    )

        # ---- B. 章节结构 (chapters) 校验与 clamp ----
        chapters = getattr(obj, "chapters", None)
        if chapters is None and isinstance(obj, dict):
            chapters = obj.get("chapters")

        if isinstance(chapters, list):
            if len(chapters) < rules.min_chapters:
                violations.append(
                    Violation(
                        category="structure",
                        field="chapters",
                        message=f"章节数量不足: 当前 {len(chapters)} 章 (最少 {rules.min_chapters} 章)",
                        value=len(chapters),
                        fix_hint=f"请规划至少 {rules.min_chapters} 个章节。",
                    )
                )

            for i, ch in enumerate(chapters):
                sec = getattr(ch, "est_sec", None) or getattr(ch, "target_sec", None)
                if sec is None and isinstance(ch, dict):
                    sec = ch.get("est_sec") or ch.get("target_sec")

                if sec is not None:
                    try:
                        sec_val = int(sec)
                        if sec_val < rules.min_chapter_sec or sec_val > rules.max_chapter_sec:
                            if rules.clamp_chapter_sec:
                                clamped = clamp_duration(
                                    sec_val, rules.min_chapter_sec, rules.max_chapter_sec
                                )
                                if hasattr(ch, "est_sec"):
                                    ch.est_sec = clamped
                                elif hasattr(ch, "target_sec"):
                                    ch.target_sec = clamped
                                elif isinstance(ch, dict) and "est_sec" in ch:
                                    ch["est_sec"] = clamped
                                elif isinstance(ch, dict) and "target_sec" in ch:
                                    ch["target_sec"] = clamped
                            else:
                                violations.append(
                                    Violation(
                                        category="length",
                                        field=f"chapters[{i}].duration",
                                        message=f"章节 {i+1} 时长 {sec_val}s 超出 [{rules.min_chapter_sec}, {rules.max_chapter_sec}]s 范围",
                                        value=sec_val,
                                        fix_hint=f"章节时长必须在 {rules.min_chapter_sec}~{rules.max_chapter_sec} 秒内。",
                                    )
                                )
                    except (ValueError, TypeError):
                        pass

        return violations


# ===========================================================================
# 7. 自修复回路 (Self-Repair Loop) 与调用器适配
# ===========================================================================

def build_repair_prompt(
    violations: list[Violation],
    raw_output: str,
    max_raw_chars: int = 2000,
) -> str:
    """构造带违规反馈的 UserMessage，用于向模型发起精准的自修复重问。"""
    items = []
    for i, v in enumerate(violations, 1):
        loc = f" (字段位置: `{v.field}`)" if v.field else ""
        item_lines = [f"{i}. 【{v.category.upper()} 违规】{loc}"]
        item_lines.append(f"   - 违规描述: {v.message}")
        if v.fix_hint:
            item_lines.append(f"   - 修正指令: {v.fix_hint}")
        items.append("\n".join(item_lines))

    violations_str = "\n\n".join(items)

    snippet = raw_output[:max_raw_chars]
    if len(raw_output) > max_raw_chars:
        snippet += "\n...(原始输出过长，已截断)..."

    return (
        "【模型输出边界违规诊断与自修复指令】\n"
        "你上一轮的输出未能通过系统的安全与结构边界校验。\n"
        f"检测到共 {len(violations)} 项硬性边界违规，违规清单如下:\n\n"
        f"{violations_str}\n\n"
        "--------------------\n"
        f"上一轮原始输出内容如下:\n```\n{snippet}\n```\n"
        "--------------------\n\n"
        "请严格对照上述违规项执行纠偏，重新生成完整结果并遵守以下硬性边界约束:\n"
        "1. [Schema 规范] 必须且仅输出一个合法的 JSON 对象，严禁包裹任何 markdown 解释或非 JSON 文字;\n"
        "2. [结构与字数] 严格遵守长度限制(分镜数 5~8、每场景口播 65~110 字、画面标题 <=12 字、要点 <=14 字、末镜 kind='ending');\n"
        "3. [业务红线] 严禁编造薪资具体数字(请使用『因城市/公司差异较大』)、严禁包过/保上岸承诺、严禁年龄/性别/学历歧视、严禁诋毁企业或个人;\n"
        "4. 务必纠正上述列出的所有违规项，确保输出可直接通过系统校验。"
    )


def _fatal_llm_error(exc: BaseException) -> bool:
    """检测鉴权/权限/模型不存在等致命错误，此类错误不可通过重试修复，应直接抛出。"""
    try:
        from .llm import is_fatal_error

        if is_fatal_error(exc):
            return True
    except ImportError:
        pass
    try:
        import openai

        fatal = tuple(
            getattr(openai, attr)
            for attr in ("AuthenticationError", "PermissionDeniedError", "NotFoundError")
            if hasattr(openai, attr)
        )
        return isinstance(exc, fatal) if fatal else False
    except ImportError:
        return False


def _extract_text_content(res: Any) -> str:
    """从各类模型返回值形态中自洽提取纯文本字符串。"""
    if res is None:
        return ""
    if isinstance(res, str):
        return res
    if hasattr(res, "content"):
        content = res.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part if isinstance(part, str) else str(part.get("text", ""))
                for part in content
            )
    if isinstance(res, dict):
        if "choices" in res and res["choices"]:
            choice = res["choices"][0]
            if isinstance(choice, dict):
                msg = choice.get("message", {})
                if isinstance(msg, dict):
                    return str(msg.get("content", ""))
        if "content" in res:
            return str(res["content"])
    return str(res)


def _messages_to_prompt_string(messages: list[dict[str, str]]) -> str:
    """把 OpenAI 格式的 messages 列表转换为纯文本 prompt。"""
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"[System]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        else:
            parts.append(f"[User]\n{content}")
    return "\n\n".join(parts)


async def _invoke_caller(
    caller: Any,
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    **kwargs: Any,
) -> str:
    """调用 LLM caller，彻底解耦具体库(LangChain/OpenAI SDK/原生函数)。"""
    try:
        # 1. 支持具备 ainvoke 的对象 (如 LangChain ChatModel 或类似适配器)
        if hasattr(caller, "ainvoke") and callable(caller.ainvoke):
            call_kwargs = dict(kwargs)
            if temperature is not None:
                call_kwargs["temperature"] = temperature
            try:
                res = await caller.ainvoke(messages, **call_kwargs)
            except TypeError:
                res = await caller.ainvoke(messages, **kwargs)
            return _extract_text_content(res)

        # 2. 支持普通 callable (函数/方法)
        if callable(caller):
            sig = inspect.signature(caller) if not isinstance(caller, type) else None
            accepts_temp = False
            accepts_prompt_str = False
            if sig:
                params = sig.parameters
                accepts_temp = "temperature" in params or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                )
                if len(params) == 1 and ("prompt" in params or "text" in params):
                    accepts_prompt_str = True

            call_args = (
                _messages_to_prompt_string(messages)
                if accepts_prompt_str
                else messages
            )
            call_kwargs = dict(kwargs)
            if accepts_temp and temperature is not None:
                call_kwargs["temperature"] = temperature

            try:
                if inspect.iscoroutinefunction(caller):
                    res = await caller(call_args, **call_kwargs)
                else:
                    res = caller(call_args, **call_kwargs)
                    if inspect.iscoroutine(res):
                        res = await res
            except TypeError:
                # 退避到仅传位置参数
                if inspect.iscoroutinefunction(caller):
                    res = await caller(call_args)
                else:
                    res = caller(call_args)
                    if inspect.iscoroutine(res):
                        res = await res

            return _extract_text_content(res)

        raise TypeError(f"caller 必须是可调用对象或具备 ainvoke 的适配器, 当前为: {type(caller)}")
    except Exception as exc:
        if _fatal_llm_error(exc):
            raise RuntimeError(f"LLM 鉴权或配置致命错误, 不重试: {exc}") from exc
        raise


async def enforce_boundary(
    prompt: str | list[dict[str, str]],
    caller: Callable[..., Any],
    schema: type[T],
    boundary_rules: BoundaryRules | None = None,
    *,
    max_repairs: int = 1,
    temperature: float = 0.7,
    repair_temperature: float = 0.2,
    system_prompt: str | None = None,
    **kwargs: Any,
) -> T:
    """模型输出边界强制函数 (自修复回路统一入口)。

    完整流程:
    1. 标准化构建输入消息序列 (System / User);
    2. 发起首次模型调用 (初温 temperature);
    3. 执行 Schema、长度结构、业务红线综合边界检查;
    4. 若合规，直接返回 Pydantic 校验实例;
    5. 若违规且尚有修复次数，捕获具体违背项 (Violation)，构造带错误反馈的纠错 UserMessage;
    6. 将温度下调至 repair_temperature 发起单次有边界的重问修复;
    7. 若全失败，抛出完整保留第一现场原始输出的 BoundaryViolationError。
    """
    rules = boundary_rules or BoundaryRules()
    checker = BoundaryChecker(rules)

    # 1. 规整 messages
    if isinstance(prompt, str):
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
    elif isinstance(prompt, list):
        messages = list(prompt)
    else:
        raise TypeError(f"prompt 必须为 str 或 list[dict[str, str]], 当前类型为 {type(prompt)}")

    # 2. 首次调用
    raw_output = await _invoke_caller(caller, messages, temperature=temperature, **kwargs)

    # 3. 边界检查
    instance, violations = checker.check(raw_output, schema)
    if not violations and instance is not None:
        return instance

    logger.warning(
        "模型初次输出突破边界 (共 %d 项违规: %s), 启动自修复回路 (max_repairs=%d, repair_temp=%.2f)",
        len(violations),
        ", ".join(v.message for v in violations[:3]),
        max_repairs,
        repair_temperature,
    )

    # 4. 自修复回路
    first_instance = instance
    repaired_output: str | None = None
    repaired_instance: T | None = None
    first_raw = raw_output

    for attempt in range(max_repairs):
        repair_user_msg = build_repair_prompt(violations, raw_output)
        repair_messages = list(messages)
        # 将上一轮错误输出截断保留在对话上下文中
        repair_messages.append({"role": "assistant", "content": raw_output[:3000]})
        repair_messages.append({"role": "user", "content": repair_user_msg})

        try:
            repaired_output = await _invoke_caller(
                caller, repair_messages, temperature=repair_temperature, **kwargs
            )
        except Exception as exc:
            from .llm import LLMBudgetExceededError, LLMTruncatedError

            if _fatal_llm_error(exc) or isinstance(exc, (LLMTruncatedError, LLMBudgetExceededError)):
                raise
            logger.warning("自修复重问调用未完成(%s: %s)，退出修复循环", type(exc).__name__, exc)
            break

        repaired_instance, repaired_violations = checker.check(repaired_output, schema)
        if not repaired_violations and repaired_instance is not None:
            logger.info("模型输出在第 %d 次自修复重问后成功合规", attempt + 1)
            return repaired_instance

        violations = repaired_violations
        raw_output = repaired_output

    # 5. 兜底保护: 若存在已解析的实例且无红线违规，执行确定性修正算子收敛后采用
    has_redlines = any(v.category == "redline" for v in violations)
    candidate_instance = repaired_instance if repaired_instance is not None else first_instance
    if candidate_instance is not None and not has_redlines:
        logger.warning(
            "自修复结束 (剩余 %d 项违规)，执行确定性边界算子收敛返回",
            len(violations),
        )
        candidate_instance = fix_script_deterministic(candidate_instance, rules)
        candidate_instance = clamp_chapters(
            candidate_instance, rules.min_chapter_sec, rules.max_chapter_sec
        )
        return candidate_instance

    # 6. 自修复全失败且无法安全兜底: 抛出保留第一现场的 BoundaryViolationError
    raise BoundaryViolationError(
        f"模型输出未能满足边界约束 (经 {max_repairs} 次重问修复后仍有 {len(violations)} 项违规)",
        violations=violations,
        raw_output=first_raw,
        repaired_output=repaired_output,
    )


def enforce_boundary_sync(
    prompt: str | list[dict[str, str]],
    caller: Callable[..., Any],
    schema: type[T],
    boundary_rules: BoundaryRules | None = None,
    *,
    max_repairs: int = 1,
    temperature: float = 0.7,
    repair_temperature: float = 0.2,
    system_prompt: str | None = None,
    **kwargs: Any,
) -> T:
    """enforce_boundary 的同步封装，便于同步脚本与单测调用。"""
    coro = enforce_boundary(
        prompt,
        caller,
        schema,
        boundary_rules,
        max_repairs=max_repairs,
        temperature=temperature,
        repair_temperature=repair_temperature,
        system_prompt=system_prompt,
        **kwargs,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


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
