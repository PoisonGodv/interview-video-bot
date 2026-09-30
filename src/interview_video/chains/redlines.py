"""内容红线: prompt 共用文本; 扫描器统一来自 boundary(唯一规则源)。

RED_LINES 是写进所有生成 prompt 的红线表述;
scan_redlines 由 boundary.REDLINE_PATTERNS 驱动(prompt 不是闸门, 这里才是)。
"""
from __future__ import annotations

from ..boundary import REDLINE_PATTERNS, scan_object_redlines, scan_redlines

# 内容红线(全链共用, 与 SPECS §2 一致)
RED_LINES = (
    "你是一名资深求职内容策划, 擅长为短视频平台策划高转化的求职面试内容。\n"
    "选题与文案必须真实、积极、可执行, 并严格遵守以下红线:\n"
    "1. 不编造具体薪资数字, 涉及待遇时用『因城市/公司差异大』类表述;\n"
    "2. 不含有年龄/性别/学历等歧视性内容;\n"
    "3. 不承诺『包过/必上岸』等结果;\n"
    "4. 不诋毁任何具体公司或个人。"
)

__all__ = ["RED_LINES", "REDLINE_PATTERNS", "scan_object_redlines", "scan_redlines"]
