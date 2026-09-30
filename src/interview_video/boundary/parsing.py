"""容错 JSON 提取与解析 (Schema 边界)。"""
from __future__ import annotations

import json
import re
from typing import Any

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
