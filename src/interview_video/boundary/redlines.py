"""业务红线模式与扫描器。"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from .errors import Violation

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
