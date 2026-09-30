"""TTS 幂等缓存测试: 旁车命中跳过合成 / 文本变更失效。"""
from __future__ import annotations

from pathlib import Path

from interview_video import tts as tts_mod
from interview_video.config import Settings
from interview_video.models import Cue, Scene, Script


def _script(narration: str) -> Script:
    return Script(title="缓存测试", scenes=[
        Scene(kind="point", header="镜", narration=narration),
    ])


async def test_synthesize_scenes_reuses_cache(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    async def fake_synth(index, text, mp3_path, settings, provider=None):
        calls.append(text)
        mp3_path.write_bytes(b"FAKE_MP3")
        return [Cue(start=0.0, end=1.5, text="字幕")]

    monkeypatch.setattr(tts_mod, "_synthesize_with_retry", fake_synth)
    scenes_dir = tmp_path / "scenes"
    s1 = await tts_mod.synthesize_scenes(_script("第一版口播。"), scenes_dir, Settings(_env_file=None))
    assert calls == ["第一版口播。"]
    assert (scenes_dir / "scene_000.cues.json").exists()

    # 第二次跑同文本: 命中缓存, 不再调合成
    s2 = await tts_mod.synthesize_scenes(_script("第一版口播。"), scenes_dir, Settings(_env_file=None))
    assert calls == ["第一版口播。"]  # 无新增调用
    assert s2[0].cues == s1[0].cues and s2[0].duration == s1[0].duration
    assert s2[0].mp3 == scenes_dir / "scene_000.mp3"


async def test_cache_invalidated_by_text_change(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    async def fake_synth(index, text, mp3_path, settings, provider=None):
        calls.append(text)
        mp3_path.write_bytes(b"FAKE_MP3")
        return [Cue(start=0.0, end=1.5, text="字幕")]

    monkeypatch.setattr(tts_mod, "_synthesize_with_retry", fake_synth)
    scenes_dir = tmp_path / "scenes"
    await tts_mod.synthesize_scenes(_script("第一版口播。"), scenes_dir, Settings(_env_file=None))
    await tts_mod.synthesize_scenes(_script("第二版口播, 内容变了。"), scenes_dir, Settings(_env_file=None))
    assert calls == ["第一版口播。", "第二版口播, 内容变了。"]


async def test_cache_invalidated_by_missing_mp3(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    async def fake_synth(index, text, mp3_path, settings, provider=None):
        calls.append(text)
        mp3_path.write_bytes(b"FAKE_MP3")
        return [Cue(start=0.0, end=1.5, text="字幕")]

    monkeypatch.setattr(tts_mod, "_synthesize_with_retry", fake_synth)
    scenes_dir = tmp_path / "scenes"
    await tts_mod.synthesize_scenes(_script("口播。"), scenes_dir, Settings(_env_file=None))
    (scenes_dir / "scene_000.mp3").unlink()  # 删掉 mp3 强制重合成
    await tts_mod.synthesize_scenes(_script("口播。"), scenes_dir, Settings(_env_file=None))
    assert len(calls) == 2


async def test_cache_invalidated_by_voice_change(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    async def fake_synth(index, text, mp3_path, settings, provider=None):
        calls.append(settings.tts_voice)
        mp3_path.write_bytes(b"FAKE_MP3")
        return [Cue(start=0.0, end=1.5, text="字幕")]

    monkeypatch.setattr(tts_mod, "_synthesize_with_retry", fake_synth)
    scenes_dir = tmp_path / "scenes"
    await tts_mod.synthesize_scenes(_script("口播。"), scenes_dir, Settings(_env_file=None))
    await tts_mod.synthesize_scenes(
        _script("口播。"), scenes_dir,
        Settings(_env_file=None, tts_voice="zh-CN-XiaoxiaoNeural"),
    )
    assert calls == ["zh-CN-YunxiNeural", "zh-CN-XiaoxiaoNeural"]


async def test_legacy_sidecar_without_voice_key_still_hits(tmp_path: Path, monkeypatch):
    import json

    async def boom(*a, **kw):
        raise AssertionError("不应重合成")

    monkeypatch.setattr(tts_mod, "_synthesize_with_retry", boom)
    scenes_dir = tmp_path / "scenes"
    scenes_dir.mkdir()
    (scenes_dir / "scene_000.mp3").write_bytes(b"MP3")
    (scenes_dir / "scene_000.cues.json").write_text(json.dumps({
        "narration_sha1": tts_mod._narration_fingerprint("口播。"),
        "duration": 2.0, "cues": [{"start": 0.0, "end": 1.5, "text": "字幕"}],
    }), encoding="utf-8")
    media = await tts_mod.synthesize_scenes(_script("口播。"), scenes_dir, Settings(_env_file=None))
    assert media[0].duration == 2.0


async def test_duration_uses_audio_length_when_cues_short(tmp_path: Path, monkeypatch):
    """cue 只覆盖一小段音频: 时长按音频算, 字幕改用估算, 不截断口播。"""
    async def fake_synth(index, text, mp3_path, settings, provider=None):
        mp3_path.write_bytes(b"FAKE_MP3")
        return [Cue(start=0.0, end=1.0, text="字幕")]

    monkeypatch.setattr(tts_mod, "_synthesize_with_retry", fake_synth)
    monkeypatch.setattr(tts_mod, "_probe_audio_duration", lambda p: 10.0)
    media = await tts_mod.synthesize_scenes(
        _script("第一句口播。第二句口播。"), tmp_path / "scenes", Settings(_env_file=None)
    )
    assert media[0].duration == 10.0 + tts_mod._TAIL_PAD
    assert media[0].cues[-1].end == 10.0


async def test_cached_duration_corrected_by_audio_length(tmp_path: Path, monkeypatch):
    """旧缓存 duration 比 mp3 短: 命中缓存时按音频修正。"""
    scenes_dir = tmp_path / "scenes"
    scenes_dir.mkdir()
    mp3 = scenes_dir / "scene_000.mp3"
    mp3.write_bytes(b"FAKE_MP3")
    settings = Settings(_env_file=None)
    tts_mod._save_scene_cache(
        mp3, "口播。", [Cue(start=0.0, end=1.5, text="口播")], 1.9,
        tts_mod._voice_key(settings.tts_provider, settings),
    )
    monkeypatch.setattr(tts_mod, "_probe_audio_duration", lambda p: 6.0)
    media = await tts_mod.synthesize_scenes(_script("口播。"), scenes_dir, settings)
    assert media[0].duration == 6.0 + tts_mod._TAIL_PAD


async def test_duration_falls_back_to_cues_without_ffprobe(tmp_path: Path, monkeypatch):
    async def fake_synth(index, text, mp3_path, settings, provider=None):
        mp3_path.write_bytes(b"FAKE_MP3")
        return [Cue(start=0.0, end=1.5, text="字幕")]

    monkeypatch.setattr(tts_mod, "_synthesize_with_retry", fake_synth)
    monkeypatch.setattr(tts_mod, "_probe_audio_duration", lambda p: None)
    media = await tts_mod.synthesize_scenes(_script("口播。"), tmp_path / "s", Settings(_env_file=None))
    assert media[0].duration == 1.5 + tts_mod._TAIL_PAD
