"""LLM 链包: 选题(topic) / 脚本(script) / 元数据(metadata)。

共享能力: 容错 JSON 提取 —— 容忍 ```json 围栏、JSON 前后的说明文字,
供各模块的回退解析器(parse_xxx_json)复用。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 代码围栏块(``` 或 ```json 开头), DOTALL 以跨行匹配
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


def extract_json_text(text: str) -> str:
    """从模型输出文本中提取首个完整 JSON 对象/数组的原始文本。

    容忍两种常见噪音:
    1. ```json ... ``` 代码围栏(优先取含 { 或 [ 的围栏块);
    2. JSON 前后的废话文字(从首个 {/[ 括号平衡扫描到配对的 }/])。

    找不到 JSON 内容时抛 ValueError。
    """
    stripped = text.strip()
    for block in _FENCE_RE.findall(stripped):
        block = block.strip()
        if "{" in block or "[" in block:
            return block

    # 无围栏: 定位首个 { 或 [ 并做括号平衡扫描(尊重字符串与转义)
    start = -1
    open_ch = ""
    for i, ch in enumerate(stripped):
        if ch in "{[":
            start = i
            open_ch = ch
            break
    if start < 0:
        raise ValueError(f"输出中未找到 JSON 内容: {stripped[:80]!r}")

    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
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
                return stripped[start : i + 1]
    raise ValueError(f"JSON 括号不配对(缺少 {close_ch}): {stripped[:80]!r}")


def parse_json_lenient(text: str) -> Any:
    """容错解析: 先提取 JSON 文本再 json.loads, 失败抛 ValueError。"""
    raw = extract_json_text(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}; 原文: {raw[:120]!r}") from exc


# 子模块再导出(放在共享工具定义之后, 避免循环导入)
from .metadata import parse_metadata_json, write_metadata  # noqa: E402
from .script import (  # noqa: E402
    fix_script_narration,
    parse_script_json,
    truncate_narration,
    write_script,
)
from .topic import generate_topics, parse_topics_json  # noqa: E402

__all__ = [
    "extract_json_text",
    "parse_json_lenient",
    "generate_topics",
    "parse_topics_json",
    "write_script",
    "parse_script_json",
    "truncate_narration",
    "fix_script_narration",
    "write_metadata",
    "parse_metadata_json",
]
