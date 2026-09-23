"""豆包 TTS 通道测试: 纯函数全测(不联网), 真实合成打 integration。"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from interview_video.config import Settings
from interview_video.tts import (
    DoubaoTTSError,
    _doubao_headers,
    _doubao_payload,
    _require_doubao_credentials,
    _split_subtitle_units,
    estimate_cues,
    parse_ndjson_line,
    synthesize_text_doubao,
)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# ---------- 凭证/请求构造 ----------

def test_require_credentials_missing():
    with pytest.raises(RuntimeError, match="豆包 TTS 缺少凭证"):
        _require_doubao_credentials(_settings())


def test_require_credentials_api_key_ok():
    _require_doubao_credentials(_settings(tts_doubao_api_key="k"))  # 不抛即过


def test_require_credentials_old_pair_ok():
    _require_doubao_credentials(
        _settings(tts_doubao_app_key="app", tts_doubao_access_key="acc")
    )


def test_headers_new_console():
    h = _doubao_headers(_settings(tts_doubao_api_key="secret-key"))
    assert h["X-Api-Key"] == "secret-key"
    assert h["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert "X-Api-App-Id" not in h


def test_headers_old_console():
    h = _doubao_headers(
        _settings(tts_doubao_app_key="app1", tts_doubao_access_key="acc1")
    )
    assert h["X-Api-App-Id"] == "app1"
    assert h["X-Api-Access-Key"] == "acc1"


def test_payload_rate_clamped():
    p = _doubao_payload("你好", _settings(tts_doubao_rate=500))
    assert p["req_params"]["audio_params"]["speech_rate"] == 100
    p = _doubao_payload("你好", _settings(tts_doubao_rate=-99))
    assert p["req_params"]["audio_params"]["speech_rate"] == -50
    assert p["req_params"]["speaker"] == "zh_male_m191_uranus_bigtts"


# ---------- NDJSON 解析 ----------

def test_parse_ndjson_ok():
    assert parse_ndjson_line('{"code":0,"data":"xx"}')["code"] == 0


def test_parse_ndjson_bad_line():
    with pytest.raises(DoubaoTTSError, match="NDJSON 坏行"):
        parse_ndjson_line("{not json")


# ---------- 字幕单元切分 ----------

def test_split_units_punctuation():
    units = _split_subtitle_units("大家好，这里是面试充电站。今天聊面试。")
    assert units == ["大家好，", "这里是面试充电站。", "今天聊面试。"]


def test_split_units_hard_break():
    units = _split_subtitle_units("一" * 40, max_chars=16)
    assert [len(u) for u in units] == [16, 16, 8]


def test_split_units_empty():
    assert _split_subtitle_units("   ") == []


# ---------- 估算时间轴 ----------

def test_estimate_cues_duration_aligned():
    cues = estimate_cues("大家好，这里是面试充电站。今天聊面试。", 12.0)
    assert cues[0].start == 0.0
    assert cues[-1].end == 12.0
    for prev, cur in zip(cues, cues[1:]):
        assert cur.start >= prev.end - 1e-6  # 单调不重叠


def test_estimate_cues_pause_gets_more_time():
    # 同样 8 个字, 带句号的那条应分到更多时间(停顿权重)
    cues = estimate_cues("一二三四五六七八。一二三四五六七八", 10.0)
    assert (cues[0].end - cues[0].start) > (cues[1].end - cues[1].start)


def test_estimate_cues_empty_or_bad_duration():
    assert estimate_cues("", 5.0) == []
    assert estimate_cues("你好", 0.0) == []


# ---------- 合成主路径(打桩网络与 ffprobe) ----------

async def test_synthesize_doubao_happy_path(tmp_path, monkeypatch):
    fake_audio = b"\xff\xfb" * 500  # 假 mp3 字节
    monkeypatch.setattr(
        "interview_video.tts._doubao_audio_bytes",
        lambda text, settings: _async_return(fake_audio),
    )
    monkeypatch.setattr("interview_video.assemble.ffprobe_duration", lambda p: 8.0)

    mp3 = tmp_path / "s.mp3"
    cues = await synthesize_text_doubao(
        "大家好，这里是面试充电站。今天聊面试。", mp3, _settings(tts_doubao_api_key="k")
    )
    assert mp3.read_bytes() == fake_audio
    assert cues and cues[-1].end == 8.0
    assert all(len(c.text) <= 16 for c in cues)


async def _async_return(value):
    return value


# ---------- 真实合成(需凭证+网络) ----------

@pytest.mark.integration
async def test_doubao_real_synthesis(tmp_path):
    from interview_video.config import load_settings

    settings = load_settings()
    if not settings.tts_doubao_api_key and not (
        settings.tts_doubao_app_key and settings.tts_doubao_access_key
    ):
        pytest.skip("未配置豆包凭证(TTS_DOUBAO_API_KEY)")
    mp3 = tmp_path / "real.mp3"
    cues = await synthesize_text_doubao("你好，这里是面试充电站。", mp3, settings)
    assert mp3.stat().st_size > 1000
    assert cues and cues[-1].end > 1.0
