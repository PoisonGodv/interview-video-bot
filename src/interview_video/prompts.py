"""轻量级原生 Prompt 模板系统 (替换 LangChain ChatPromptTemplate)。

设计特性:
1. 零第三方依赖，纯标准库实现;
2. 接口对齐 ChatPromptTemplate: from_messages、format_messages;
3. 变量安全替换:
   - 支持 {{ 和 }} 转义还原为 { 和 };
   - 仅对匹配到的 kwargs 变量进行安全单次替换，不进行二次求值;
   - 彻底免疫用户内容中的花括号(如 HashMap{K,V});
4. 消息对象 ChatMessage 兼容字典访问、属性访问及 str() 输出。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class ChatMessage(dict):
    """单条对话消息，继承 dict，兼容字典访问与属性访问。"""

    def __init__(self, role: str, content: str) -> None:
        super().__init__(role=role, content=content)

    @property
    def role(self) -> str:
        return self["role"]

    @property
    def content(self) -> str:
        return self["content"]

    def __str__(self) -> str:
        return self["content"]

    def __repr__(self) -> str:
        return f"ChatMessage(role={self.role!r}, content={self.content!r})"


class MessagePrompt:
    """单条消息的提示模板节点。"""

    def __init__(self, role: str, template: str) -> None:
        self.role = role
        self.template = template
        # 兼容部分测试检查 .prompt.template 的契约
        self.prompt = self

    def format(self, **kwargs: Any) -> ChatMessage:
        content = safe_format_prompt(self.template, kwargs)
        return ChatMessage(role=self.role, content=content)


_ESCAPED_OPEN = "\x00OPEN_BRACE\x00"
_ESCAPED_CLOSE = "\x00CLOSE_BRACE\x00"
_VAR_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def safe_format_prompt(template: str, kwargs: Mapping[str, Any]) -> str:
    """安全单次变量替换算法:

    1. 暂存 Python 转义双花括号 {{ 和 }};
    2. 匹配 {var_name}，仅当 var_name 在 kwargs 中时执行单次求值替换;
    3. 还原双花括号为字面量 { 和 };
    4. 变量值中的花括号 (如 HashMap{K,V}) 绝不触发二次求值。
    """
    text = template.replace("{{", _ESCAPED_OPEN).replace("}}", _ESCAPED_CLOSE)

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in kwargs:
            val = kwargs[key]
            return str(val) if val is not None else ""
        return match.group(0)

    formatted = _VAR_RE.sub(_repl, text)
    return formatted.replace(_ESCAPED_OPEN, "{").replace(_ESCAPED_CLOSE, "}")


class ChatPromptTemplate:
    """轻量级原生聊天提示模板集合。"""

    def __init__(self, messages: list[MessagePrompt]) -> None:
        self.messages = list(messages)

    @classmethod
    def from_messages(
        cls,
        messages: list[tuple[str, str] | MessagePrompt],
    ) -> ChatPromptTemplate:
        prompts: list[MessagePrompt] = []
        for item in messages:
            if isinstance(item, MessagePrompt):
                prompts.append(item)
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                prompts.append(MessagePrompt(str(item[0]), str(item[1])))
            else:
                raise TypeError(f"无法识别的消息模板格式: {item!r}")
        return cls(prompts)

    def format_messages(self, **kwargs: Any) -> list[ChatMessage]:
        return [msg.format(**kwargs) for msg in self.messages]

    def format(self, **kwargs: Any) -> list[ChatMessage]:
        return self.format_messages(**kwargs)


__all__ = [
    "ChatMessage",
    "MessagePrompt",
    "ChatPromptTemplate",
    "safe_format_prompt",
]
