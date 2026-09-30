"""TTS 重试日志 / 鉴权错误不重试 / 备用通道回退测试(打桩通道, 不联网)。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from interview_video import tts
from interview_video.config import Settings
from interview_video.models import Cue, Scene, Script


def _script() -> Script:
    return Script(title="t", scenes=[Scene(header="h", narration="口播。")])


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, tts_provider="doubao", tts_doubao_api_key="k", **kw)


@pytest.fixture
def sleeps(monkeypatch):
    out: list[float] = []

    async def fake_sleep(delay):
        out.append(delay)

    monkeypatch.setattr(tts.asyncio, "sleep", fake_sleep)
    return out


def _stub(monkeypatch, doubao_err: Exception | None, edge_err: Exception | None = None):
    calls: list[str] = []

    async def fake_doubao(text, mp3_path, settings):
        calls.append("doubao")
        if doubao_err:
            raise doubao_err
        mp3_path.write_bytes(b"DOUBAO")
        return [Cue(start=0.0, end=1.0, text=text)]

    async def fake_edge(text, mp3_path, voice, rate):
        calls.append("edge")
        if edge_err:
            raise edge_err
        mp3_path.write_bytes(b"EDGE")
        return [Cue(start=0.0, end=2.0, text=text)]

    monkeypatch.setattr(tts, "synthesize_text_doubao", fake_doubao)
    monkeypatch.setattr(tts, "synthesize_text", fake_edge)
    return calls


async def test_retry_logged_at_warning(tmp_path: Path, monkeypatch, sleeps, caplog):
    calls = _stub(monkeypatch, ConnectionError("网络抖动"))
    caplog.set_level(logging.WARNING, logger="interview_video.tts")
    with pytest.raises(RuntimeError, match=r"场景 0 TTS\[doubao\] 合成失败"):
        await tts.synthesize_scenes(_script(), tmp_path, _settings())
    assert calls == ["doubao"] * 3
    assert sleeps == [1.0, 2.0]
    retries = [r for r in caplog.records if "后重试" in r.getMessage()]
    assert len(retries) == 2
    msg = retries[0].getMessage()
    assert retries[0].levelno == logging.WARNING
    assert "doubao" in msg and "场景 0" in msg and "1/3" in msg and "网络抖动" in msg


@pytest.mark.parametrize("err", [
    tts.TTSAuthError("HTTP 401: unauthorized"),
    tts.DoubaoTTSError("HTTP 403: Forbidden"),
    tts.DoubaoTTSError("status=45000010 invalid api key"),
    tts.DoubaoTTSError("status=45000292 quota exceeded for types: text_words"),
])
async def test_auth_errors_fail_fast(tmp_path: Path, monkeypatch, sleeps, err):
    calls = _stub(monkeypatch, err)
    with pytest.raises(tts.TTSAuthError, match="鉴权/权限/额度"):
        await tts.synthesize_scenes(_script(), tmp_path, _settings())
    assert calls == ["doubao"]
    assert sleeps == []


def test_concurrency_quota_is_retryable():
    assert not tts._is_fatal_tts_error(
        tts.DoubaoTTSError("status=45000292 quota exceeded for types: concurrency")
    )
    assert not tts._is_fatal_tts_error(ConnectionError("reset"))


async def test_missing_credentials_fail_fast(tmp_path: Path, monkeypatch, sleeps):
    monkeypatch.setattr(tts, "synthesize_text", None)  # 不应被调用
    with pytest.raises(tts.TTSAuthError, match="缺少凭证"):
        await tts.synthesize_scenes(
            _script(), tmp_path, Settings(_env_file=None, tts_provider="doubao")
        )
    assert sleeps == []


async def test_no_fallback_by_default(tmp_path: Path, monkeypatch, sleeps):
    calls = _stub(monkeypatch, tts.TTSAuthError("HTTP 401"))
    with pytest.raises(tts.TTSAuthError):
        await tts.synthesize_scenes(_script(), tmp_path, _settings())
    assert "edge" not in calls


async def test_fallback_after_retries_exhausted(tmp_path: Path, monkeypatch, sleeps, caplog):
    calls = _stub(monkeypatch, ConnectionError("down"))
    caplog.set_level(logging.WARNING, logger="interview_video.tts")
    media = await tts.synthesize_scenes(
        _script(), tmp_path, _settings(tts_fallback_provider="edge")
    )
    assert calls == ["doubao"] * 3 + ["edge"]
    assert media[0].mp3.read_bytes() == b"EDGE"
    assert media[0].duration == pytest.approx(2.4)
    assert any("回退到 edge" in r.getMessage() for r in caplog.records)
    sidecar = json.loads((tmp_path / "scene_000.cues.json").read_text(encoding="utf-8"))
    assert sidecar["voice_key"].startswith("edge:")


async def test_fallback_on_auth_error_skips_retries(tmp_path: Path, monkeypatch, sleeps):
    calls = _stub(monkeypatch, tts.TTSAuthError("HTTP 403"))
    await tts.synthesize_scenes(_script(), tmp_path, _settings(tts_fallback_provider="edge"))
    assert calls == ["doubao", "edge"]
    assert sleeps == []


async def test_fallback_audio_not_reused_as_primary(tmp_path: Path, monkeypatch, sleeps):
    calls = _stub(monkeypatch, ConnectionError("down"))
    s = _settings(tts_fallback_provider="edge")
    await tts.synthesize_scenes(_script(), tmp_path, s)
    calls.clear()
    # 主通道恢复: 备用通道产物不命中主通道缓存, 重新用豆包合成
    calls = _stub(monkeypatch, None)
    media = await tts.synthesize_scenes(_script(), tmp_path, s)
    assert calls == ["doubao"]
    assert media[0].mp3.read_bytes() == b"DOUBAO"
    # 再跑一次命中主通道缓存
    calls = _stub(monkeypatch, None)
    await tts.synthesize_scenes(_script(), tmp_path, s)
    assert calls == []


async def test_both_providers_fail(tmp_path: Path, monkeypatch, sleeps):
    _stub(monkeypatch, tts.TTSAuthError("HTTP 401"), ConnectionError("edge down"))
    with pytest.raises(RuntimeError, match="主通道 doubao 与备用通道 edge 均失败"):
        await tts.synthesize_scenes(_script(), tmp_path, _settings(tts_fallback_provider="edge"))


def test_fallback_blank_env_is_none():
    assert Settings(_env_file=None, tts_fallback_provider="").tts_fallback_provider is None
    assert Settings(_env_file=None, tts_fallback_provider="edge").tts_fallback_provider == "edge"


async def test_local_permission_oserror_is_retried(tmp_path: Path, monkeypatch, sleeps):
    calls = _stub(monkeypatch, PermissionError(13, "Permission denied"))
    with pytest.raises(RuntimeError, match="合成失败"):
        await tts.synthesize_scenes(_script(), tmp_path, _settings())
    assert calls == ["doubao"] * 3
