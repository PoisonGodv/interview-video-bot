"""内容红线: prompt 共用文本 + 确定性扫描器(评测/质检共用)。

RED_LINES 是写进所有生成 prompt 的红线表述;
scan_redlines 用保守正则对成品文本做机械复核(prompt 不是闸门, 这里才是)。
两者同源维护, evals 与单测直接复用 scan_redlines。
"""
from __future__ import annotations

import re

# 内容红线(全链共用, 与 SPECS §2 一致)
RED_LINES = (
    "你是一名资深求职内容策划, 擅长为短视频平台策划高转化的求职面试内容。\n"
    "选题与文案必须真实、积极、可执行, 并严格遵守以下红线:\n"
    "1. 不编造具体薪资数字, 涉及待遇时用『因城市/公司差异大』类表述;\n"
    "2. 不含有年龄/性别/学历等歧视性内容;\n"
    "3. 不承诺『包过/必上岸』等结果;\n"
    "4. 不诋毁任何具体公司或个人。"
)

# 确定性红线模式: (规则名, 正则)。刻意保守——宁可漏报也不误伤正常表述。
_REDLINE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 具体薪资数字: 月薪2万 / 月薪 25k / 年薪30W / 月入3万 (「因城市差异大」类不算)
    ("编造薪资数字", re.compile(r"(?:月薪|月入|年薪|起薪)[约莫]?\s*\d[\d,.]*\s*[wWkK万K]?")),
    ("编造薪资数字", re.compile(r"\d[\d,.]*\s*[wWkK]\s*[的]?(?:月薪|年薪|起薪)")),
    # 结果承诺: 包过/必上岸/稳过/保offer
    ("承诺结果", re.compile(r"(包过|必上|稳过|保\s?offer|保证通过|一次过)")),
    # 歧视性表述: 只要/不要 + 年龄性别学历限定
    ("歧视性表述", re.compile(r"(?:只要|不要|限制|限)[^。,，\n]{0,6}(?:35岁|三十[一二三]?岁|男性|女性|男生|女生|男生女生|985|211|本科以上|大专以下)")),
    ("歧视性表述", re.compile(r"(?:35岁|三十[一二三]?岁)[^。,，\n]{0,4}(?:不要|pass|淘汰|劝退)")),
]


def scan_redlines(text: str) -> list[str]:
    """对成品文本做红线扫描, 返回命中的规则名列表(去重, 保持出现顺序)。

    用于 evals 与生成后质检; 空列表 = 未命中已知红线模式。
    """
    hits: list[str] = []
    for name, pattern in _REDLINE_PATTERNS:
        if name not in hits and pattern.search(text):
            hits.append(name)
    return hits
