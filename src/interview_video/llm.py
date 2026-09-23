"""LLM 工厂: 任意 OpenAI 兼容服务 + 测试用假模型。"""
from __future__ import annotations

try:  # langchain_core>=1.0 更名后的路径
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
except ImportError:  # langchain_core<1.0 旧路径
    from langchain_core.language_models.fake_chat import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from .config import Settings


def get_chat_model(settings: Settings) -> ChatOpenAI:
    """真实模型。未配 key 时抛 RuntimeError(调用方决定是否降级)。"""
    if settings.llm_api_key is None:
        raise RuntimeError(
            "未配置 LLM_API_KEY(任意 OpenAI 兼容服务)。"
            "请 cp .env.example .env 并填写 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL"
        )
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url or None,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
        max_retries=2,
    )


def fake_chat_model(responses: list[str]) -> GenericFakeChatModel:
    """测试用: 依次返回给定文本的假模型(不联网)。"""
    return GenericFakeChatModel(
        messages=iter([AIMessage(content=r) for r in responses])
    )
