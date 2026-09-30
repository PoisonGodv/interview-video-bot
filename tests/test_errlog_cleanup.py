"""P1-7: 失败写 error.log; 成片后清理中间文件(base.mp4/concat 列表/clips), 保留 mp3 与 cue 缓存。"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from interview_video import assemble as assemble_mod
from interview_video import llm as llm_mod
from interview_video.cli import app
from interview_video.config import Settings
from interview_video.graph import run_pipeline

SAMPLE_SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "sample_script.json"

runner = CliRunner()


def _no_llm(settings: Settings, audit=None):
    raise RuntimeError("未配置 LLM_API_KEY(测试注入)")


@pytest.fixture
def media(fake_media: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """在公共假媒体之上: finalize 额外造出 base.mp4/concat.list.txt, 模拟真实中间产物。"""

    def fake_finalize_video(clips, bars, out_path, settings):
        out_dir = out_path.parent
        (out_dir / "base.mp4").write_bytes(b"BASE")
        (out_dir / "concat.list.txt").write_text("file 'clips/scene_000.mp4'\n", encoding="utf-8")
        out_path.write_bytes(b"FAKE_FINAL_MP4")
        return out_path

    monkeypatch.setattr(assemble_mod, "finalize_video", fake_finalize_video)
    monkeypatch.setattr(llm_mod, "get_chat_model", _no_llm)


def _touch_cue_cache(out_dir: Path) -> Path:
    cache = out_dir / "scenes" / "scene_000.cues.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("[]", encoding="utf-8")
    return cache


async def test_cleanup_after_manifest(media: None, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    cache = _touch_cue_cache(out_dir)
    await run_pipeline(Settings(), out_dir, industry="互联网", script_file=SAMPLE_SCRIPT)
    assert (out_dir / "project.json").exists()
    assert (out_dir / "final.mp4").exists()
    assert not (out_dir / "base.mp4").exists()
    assert not (out_dir / "concat.list.txt").exists()
    assert not (out_dir / "clips").exists()
    assert list((out_dir / "scenes").glob("*.mp3")), "mp3 应保留"
    assert cache.exists(), "cue 缓存应保留"


async def test_keep_intermediates_keeps_clips(media: None, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    await run_pipeline(
        Settings(keep_intermediates=True), out_dir, industry="互联网", script_file=SAMPLE_SCRIPT,
    )
    assert list((out_dir / "clips").glob("*.mp4"))
    assert not (out_dir / "base.mp4").exists()
    assert not (out_dir / "concat.list.txt").exists()


def test_keep_intermediates_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEEP_INTERMEDIATES", "true")
    assert Settings().keep_intermediates is True


async def test_resume_after_cleanup_regenerates_clips(
    media: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "out"
    manifest = await run_pipeline(Settings(), out_dir, industry="互联网", script_file=SAMPLE_SCRIPT)
    assert not (out_dir / "clips").exists()

    seen: dict[str, list[Path]] = {}
    orig = assemble_mod.finalize_video

    def spy_finalize(clips, bars, out_path, settings):
        seen["clips"] = list(clips)
        assert all(Path(c).exists() for c in clips), "续跑时 clips 应已重新生成"
        return orig(clips, bars, out_path, settings)

    monkeypatch.setattr(assemble_mod, "finalize_video", spy_finalize)
    again = await run_pipeline(Settings(), out_dir, industry="互联网", script=manifest.script)
    assert len(seen["clips"]) == len(manifest.script.scenes)
    assert (out_dir / "final.mp4").exists()
    assert not (out_dir / "clips").exists()
    assert again.script == manifest.script


def test_cleanup_intermediates_missing_files_ok(tmp_path: Path) -> None:
    assemble_mod.cleanup_intermediates(tmp_path)  # 什么都没有也不报错


def test_generate_failure_writes_error_log(
    media: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(scene_media, clips_dir, settings):
        raise RuntimeError("ffmpeg 爆了")

    monkeypatch.setattr(assemble_mod, "build_scene_clips", boom)
    out_dir = tmp_path / "out"
    result = runner.invoke(
        app, ["generate", "--script-file", str(SAMPLE_SCRIPT), "--out", str(out_dir)],
    )
    assert result.exit_code != 0
    err = out_dir / "error.log"
    assert err.exists()
    body = err.read_text(encoding="utf-8")
    assert "node=clips" in body
    assert "ffmpeg 爆了" in body
    assert "Traceback" in body
    flat = "".join(result.output.split())
    assert "".join(str(err.resolve()).split()) in flat


def test_generate_success_removes_stale_error_log(media: None, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "error.log").write_text("old", encoding="utf-8")
    result = runner.invoke(
        app, ["generate", "--script-file", str(SAMPLE_SCRIPT), "--out", str(out_dir)],
    )
    assert result.exit_code == 0, result.output
    assert not (out_dir / "error.log").exists()
