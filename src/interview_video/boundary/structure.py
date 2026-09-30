"""长度与结构确定性修正算子 + 全维度边界检查器 (BoundaryChecker)。"""
from __future__ import annotations

import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .errors import Violation
from .parsing import parse_json_lenient
from .redlines import scan_object_redlines, scan_text_redlines
from .rules import BoundaryRules

T = TypeVar("T")

# 首镜口播以问候/自报家门开头判不合格(与 chains.script.OPENING_GREETINGS_HINT 同步)
GREETING_OPENING_RE = re.compile(r"^[\s\"'“‘「]*(大家好|哈喽|嗨|我是|这里是)")


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
            # 1. 分镜数量 (5~8 镜) - 由规则集开关决定(章节草稿等片段传 BoundaryRules.non_script())
            if rules.enforce_scene_count:
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

            # 3. 首镜开头钩子: 禁止问候/自报家门
            if scenes:
                first = scenes[0]
                narr0 = (
                    getattr(first, "narration", None)
                    if hasattr(first, "narration")
                    else (first.get("narration") if isinstance(first, dict) else None)
                )
                m = GREETING_OPENING_RE.match(str(narr0)) if narr0 else None
                if m:
                    violations.append(
                        Violation(
                            category="structure",
                            field="scenes[0].narration",
                            message=f"首镜口播以问候/自报家门开头: '{m.group(1)}'",
                            value=m.group(1),
                            fix_hint="第 1 镜第 1 句不超过 20 字, 删掉问候和自报家门, 直接用痛点、反常识观点或具体数字开场。",
                        )
                    )

            # 4. 末镜必须是 ending (针对完整 Script)
            if scenes and rules.require_ending_scene:
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
