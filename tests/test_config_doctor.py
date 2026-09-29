"""配置清洗(.env 行内注释/空值)、LLM 连接错误、doctor/preflight。"""
from __future__ import annotations

from pathlib import Path

import pytest

from interview_video import doctor as doctor_mod
from interview_video.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def test_env_example_yields_clean_settings(tmp_path: Path) -> None:
    """cp .env.example .env 后不应把注释读成配置值。"""
    s = Settings(_env_file=str(ROOT / ".env.example"))
    assert s.llm_base_url is None
    assert s.llm_api_key is None and not s.llm_ready()
    assert s.bili_jct is None and s.bili_sessdata is None
    assert s.tts_doubao_api_key is None and not s.doubao_ready()
    assert s.tts_provider == "edge"


def test_inline_comment_value_becomes_none(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("LLM_BASE_URL=   # 例 https://api.deepseek.com\nBILI_JCT=  # x\n", encoding="utf-8")
    s = Settings(_env_file=str(env))
    assert s.llm_base_url is None and s.bili_jct is None


def test_base_url_must_be_http() -> None:
    with pytest.raises(ValueError, match="http"):
        Settings(_env_file=None, llm_base_url="api.deepseek.com")


def test_ready_helpers() -> None:
    assert Settings(_env_file=None, llm_api_key="  ").llm_ready() is False
    assert Settings(_env_file=None, tts_doubao_app_key="a", tts_doubao_access_key="b").doubao_ready()
    assert Settings(_env_file=None, bili_sessdata="s", bili_jct="j", bili_buvid3="b").bili_ready()


async def test_connect_error_retries_then_names_base_url(monkeypatch) -> None:
    import httpx

    from interview_video.llm import HTTPChatClient, LLMConnectionError

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=request)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def _no_sleep(_s: float) -> None:
        return None

    import interview_video.llm as llm_mod

    monkeypatch.setattr(llm_mod.asyncio, "sleep", _no_sleep)
    client = HTTPChatClient(model="m", api_key="k", base_url="https://nowhere.test/v1", max_retries=2)
    with pytest.raises(LLMConnectionError, match="nowhere.test"):
        await client.ainvoke("hi")
    assert calls == 3  # 瞬时连接错误按 max_retries 重试, 耗尽后才报配置/网络错误


async def test_invalid_url_is_fatal_without_retry(monkeypatch) -> None:
    import httpx

    from interview_video.llm import HTTPChatClient, LLMConnectionError

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.UnsupportedProtocol("bad scheme", request=request)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    client = HTTPChatClient(model="m", api_key="k", base_url="https://bad.test/v1", max_retries=2)
    with pytest.raises(LLMConnectionError, match="bad.test"):
        await client.ainvoke("hi")
    assert calls == 1  # 配置错误不重试


def test_preflight_reports_missing_ffmpeg(monkeypatch) -> None:
    monkeypatch.setattr(doctor_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor_mod, "check_chromium", lambda: doctor_mod.Check("chromium", True, "x"))
    with pytest.raises(RuntimeError, match="ffmpeg"):
        doctor_mod.preflight(Settings(_env_file=None))


def test_doctor_config_flags_doubao_without_creds() -> None:
    checks = doctor_mod.check_config(Settings(_env_file=None, tts_provider="doubao"))
    bad = [c for c in checks if c.name == "豆包 TTS 凭证"]
    assert bad and not bad[0].ok and bad[0].required
