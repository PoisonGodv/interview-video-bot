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
