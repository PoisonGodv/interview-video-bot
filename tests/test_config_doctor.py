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


def test_env_example_documents_every_setting() -> None:
    """Settings 的每个字段都应在 .env.example 出现(可注释)。"""
    import re

    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))
    missing = [n.upper() for n in Settings.model_fields if n.upper() not in keys]
    assert not missing, f".env.example 缺少: {missing}"


def test_inline_comment_value_becomes_none(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("LLM_BASE_URL=   # 例 https://api.deepseek.com\nBILI_JCT=  # x\n", encoding="utf-8")
    s = Settings(_env_file=str(env))
    assert s.llm_base_url is None and s.bili_jct is None


def test_base_url_scheme_auto_prefixed_or_rejected() -> None:
    assert Settings(_env_file=None, llm_base_url="api.deepseek.com").llm_base_url == "https://api.deepseek.com"
    with pytest.raises(ValueError, match="http"):
        Settings(_env_file=None, llm_base_url="ftp://api.deepseek.com")


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
        await client.chat("hi")
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
        await client.chat("hi")
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


# ---------- .env 查找顺序 / OUT_ROOT / 日志轮转 ----------

def _isolate_env(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("IVB_CONFIG_DIR", raising=False)  # 本组测试走 HOME 下的用户级目录
    monkeypatch.delenv("IVB_ENV_FILE", raising=False)
    monkeypatch.delenv("OUT_ROOT", raising=False)
    monkeypatch.delenv("BRAND", raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return home


def test_env_file_resolution_order(monkeypatch, tmp_path: Path) -> None:
    from interview_video.config import load_settings, resolve_env_file

    home = _isolate_env(monkeypatch, tmp_path)
    assert resolve_env_file() is None
    assert load_settings()._env_path is None

    user_env = home / ".config" / "ivb" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("BRAND=用户级\n", encoding="utf-8")
    s = load_settings()
    assert s.brand == "用户级" and s._env_path == user_env.resolve()

    Path(".env").write_text("BRAND=当前目录\n", encoding="utf-8")
    assert load_settings().brand == "当前目录"

    explicit = tmp_path / "custom.env"
    explicit.write_text("BRAND=显式\n", encoding="utf-8")
    monkeypatch.setenv("IVB_ENV_FILE", str(explicit))
    s = load_settings()
    assert s.brand == "显式" and s._env_path == explicit.resolve()

    monkeypatch.setenv("IVB_ENV_FILE", str(tmp_path / "missing.env"))  # 不存在则继续往下找
    assert load_settings().brand == "当前目录"


def test_out_root_absolute_and_tilde(monkeypatch, tmp_path: Path) -> None:
    from interview_video.config import describe_paths, load_settings

    home = _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OUT_ROOT", str(tmp_path / "abs-out"))
    s = load_settings()
    assert s.out_root == tmp_path / "abs-out"
    assert str(tmp_path / "abs-out") in describe_paths(s)
    monkeypatch.setenv("OUT_ROOT", "~/videos")
    assert load_settings().out_root == home / "videos"


def test_doctor_reports_env_and_out_root(monkeypatch, tmp_path: Path) -> None:
    from interview_video.config import load_settings

    _isolate_env(monkeypatch, tmp_path)
    Path(".env").write_text(f"OUT_ROOT={tmp_path / 'o'}\n", encoding="utf-8")
    checks = {c.name: c for c in doctor_mod.check_config(load_settings())}
    assert checks[".env"].ok and checks[".env"].detail == str((tmp_path / "cwd" / ".env").resolve())
    assert checks["输出目录"].ok and checks["输出目录"].detail == str(tmp_path / "o")

    Path(".env").unlink()
    checks = {c.name: c for c in doctor_mod.check_config(load_settings())}
    assert not checks[".env"].ok and "IVB_ENV_FILE" in checks[".env"].detail


def test_logging_rotates_and_quiets_noise(tmp_path: Path) -> None:
    import logging
    import logging.handlers

    from interview_video.logging_setup import LOG_BACKUPS, LOG_MAX_BYTES, setup_logging

    root = logging.getLogger()
    try:
        setup_logging(tmp_path)
        managed = [h for h in root.handlers if getattr(h, "_ivb_managed", False)]
        files = [h for h in managed if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(files) == 1
        assert files[0].maxBytes == LOG_MAX_BYTES and files[0].backupCount == LOG_BACKUPS
        for name in ("httpx", "httpcore", "asyncio", "websockets", "PIL", "playwright"):
            assert logging.getLogger(name).level == logging.WARNING
    finally:
        for h in list(root.handlers):
            if getattr(h, "_ivb_managed", False):
                root.removeHandler(h)
                h.close()


# ---------- 友好配置错误 / 大小写容错 / DeepSeek base_url ----------

def test_tts_provider_case_and_space_insensitive() -> None:
    s = Settings(_env_file=None, tts_provider=" Doubao ", tts_fallback_provider="EDGE")
    assert s.tts_provider == "doubao" and s.tts_fallback_provider == "edge"


def test_load_settings_config_error_lists_fields_and_env_path(monkeypatch, tmp_path: Path) -> None:
    from interview_video.config import ConfigError, load_settings

    _isolate_env(monkeypatch, tmp_path)
    Path(".env").write_text(
        "TTS_PROVIDER=azure\nFPS=abc\nLLM_API_KEY=sk-secret-value\n", encoding="utf-8",
    )
    with pytest.raises(ConfigError) as ei:
        load_settings()
    exc = ei.value
    assert exc.env_path == (tmp_path / "cwd" / ".env").resolve()
    assert any(x.startswith("TTS_PROVIDER:") and "azure" in x for x in exc.lines)
    assert any(x.startswith("FPS:") for x in exc.lines)
    assert "sk-secret-value" not in str(exc)


def test_cli_bad_config_prints_fields_exit_2(monkeypatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from interview_video.cli import app

    _isolate_env(monkeypatch, tmp_path)
    Path(".env").write_text("TTS_PROVIDER=azure\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["topics", "--list"])
    assert r.exit_code == 2 and "TTS_PROVIDER" in r.output and "Traceback" not in r.output


def test_doctor_still_runs_with_bad_config(monkeypatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from interview_video.cli import app

    _isolate_env(monkeypatch, tmp_path)
    Path(".env").write_text("TTS_PROVIDER=azure\n", encoding="utf-8")
    monkeypatch.setattr(doctor_mod, "check_chromium", lambda: doctor_mod.Check("chromium", True, "x"))
    r = CliRunner().invoke(app, ["doctor"])
    assert r.exit_code == 1
    assert "ffmpeg" in r.output and "中文字体" in r.output
    assert "FAIL" in r.output and "TTS_PROVIDER" in r.output


def test_doctor_warns_deepseek_without_base_url() -> None:
    checks = {c.name: c for c in doctor_mod.check_config(
        Settings(_env_file=None, llm_model="deepseek-chat", llm_api_key="k"))}
    c = checks["LLM_BASE_URL"]
    assert not c.ok and not c.required and "https://api.deepseek.com" in c.detail
    ok = {c.name: c for c in doctor_mod.check_config(
        Settings(_env_file=None, llm_model="gpt-4o-mini", llm_api_key="k"))}
    assert ok["LLM_BASE_URL"].ok


def test_doctor_lists_features_needing_llm_key() -> None:
    checks = {c.name: c for c in doctor_mod.check_config(Settings(_env_file=None))}
    detail = checks["LLM_API_KEY"].detail
    assert "ivb topics" in detail and "ivb batch" in detail and "--sample" in detail


def test_missing_key_message_points_to_loaded_env(tmp_path: Path) -> None:
    from interview_video.llm import get_chat_model, missing_key_message

    s = Settings(_env_file=None)
    s._env_path = tmp_path / "my.env"
    msg = missing_key_message(s)
    assert str(tmp_path / "my.env") in msg and "--sample" in msg and "cp .env.example" not in msg
    s._env_path = None
    assert "ivb init" in missing_key_message(s)
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        get_chat_model(s)
