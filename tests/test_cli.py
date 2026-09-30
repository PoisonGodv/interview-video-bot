"""CLI 测试: --help 与 generate --script-file happy path(媒体层 monkeypatch, 不联网/不起浏览器)。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from interview_video import assemble as assemble_mod
from interview_video import llm as llm_mod
from interview_video import render as render_mod
from interview_video import subtitle_bars as bars_mod
from interview_video import tts as tts_mod
from interview_video.cli import app
from interview_video.config import Settings
from interview_video.models import (
    Cue,
    RenderResult,
    SceneMedia,
    SubtitleBar,
)

SAMPLE_SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "sample_script.json"

runner = CliRunner()


def _png_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def fake_media(monkeypatch: pytest.MonkeyPatch) -> None:
    """与 test_graph.py 相同的媒体层假实现(造小文件)。"""

    async def fake_synthesize_scenes(script, scenes_dir, settings):
        scenes_dir.mkdir(parents=True, exist_ok=True)
        media = []
        for i, scene in enumerate(script.scenes):
            mp3 = scenes_dir / f"scene_{i:03d}.mp3"
            mp3.write_bytes(b"FAKE_MP3")
            media.append(
                SceneMedia(index=i, mp3=mp3, duration=2.0, cues=[Cue(start=0, end=1.5, text=scene.header)])
            )
        return media

    def fake_render_project(script, out_dir, settings, industry: str = ""):
        frames = []
        for i in range(len(script.scenes)):
            png = out_dir / "scenes" / f"scene_{i:03d}.png"
            png.parent.mkdir(parents=True, exist_ok=True)
            png.write_bytes(_png_bytes())
            frames.append(png)
        cover = out_dir / "cover.png"
        cover.write_bytes(_png_bytes())
        return RenderResult(cover=cover, frames=frames)

    def fake_build_scene_clips(scene_media, clips_dir, settings):
        clips_dir.mkdir(parents=True, exist_ok=True)
        return [(_write(clips_dir / f"scene_{m.index:03d}.mp4", b"FAKE_MP4")) for m in scene_media]

    def fake_render_subtitle_bars(cues, subs_dir, settings):
        subs_dir.mkdir(parents=True, exist_ok=True)
        bars = []
        for i, cue in enumerate(cues):
            png = _write(subs_dir / f"bar_{i:03d}.png", _png_bytes())
            bars.append(SubtitleBar(index=i, png=png, start=cue.start, end=cue.end, text=cue.text))
        return bars

    def fake_finalize_video(clips, bars, out_path, settings):
        return _write(out_path, b"FAKE_FINAL")

    def _write(path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    # 不允许真实 LLM 调用: 项目根 .env 可能配了真 key, 强制走降级路径
    def _no_llm(settings: Settings, audit=None):
        raise RuntimeError("未配置 LLM_API_KEY(测试注入)")

    from interview_video import doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "preflight", lambda settings: None)
    monkeypatch.setattr(llm_mod, "get_chat_model", _no_llm)
    monkeypatch.setattr(tts_mod, "synthesize_scenes", fake_synthesize_scenes)
    monkeypatch.setattr(render_mod, "render_project", fake_render_project)
    monkeypatch.setattr(assemble_mod, "build_scene_clips", fake_build_scene_clips)
    monkeypatch.setattr(assemble_mod, "finalize_video", fake_finalize_video)
    monkeypatch.setattr(bars_mod, "render_subtitle_bars", fake_render_subtitle_bars)


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("topics", "generate", "upload"):
        assert cmd in result.output


def test_generate_help_shows_script_file() -> None:
    result = runner.invoke(app, ["generate", "--help"])
    assert result.exit_code == 0
    assert "--script-file" in result.output


def test_generate_with_script_file(fake_media: None, tmp_path: Path) -> None:
    """generate --script-file --out <tmp>: 全程不需要 LLM key, 产物齐全。"""
    out_dir = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "generate",
            "--industry", "互联网",
            "--script-file", str(SAMPLE_SCRIPT),
            "--out", str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    # 产物落盘
    assert (out_dir / "project.json").exists()
    assert (out_dir / "script.json").exists()
    assert (out_dir / "final.mp4").exists()
    assert (out_dir / "subs" / "subtitles.srt").exists()
    # rich 打印了产物路径(无 LLM key → BiliMeta 降级提示)
    assert "final.mp4" in result.output
    assert "cover.png" in result.output
    # project.json 内容正确
    manifest = json.loads((out_dir / "project.json").read_text(encoding="utf-8"))
    assert manifest["script"]["title"] == "程序员面试避坑指南"
    assert manifest["video"].endswith("final.mp4")
    assert manifest["meta"] is None  # 无 LLM key, metadata 降级


def test_generate_auto_out_dir(fake_media: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未给 --out 时自动生成 output/<slug>-<YYYYmmdd-HHMM>/。"""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["generate", "--script-file", str(SAMPLE_SCRIPT)],
    )
    assert result.exit_code == 0, result.output
    dirs = list((tmp_path / "output").glob("*-*"))
    # slug 来自脚本标题(中文保留) + 时间戳
    assert len(dirs) == 1
    assert re.match(r"^.+-\d{8}-\d{4}$", dirs[0].name), dirs[0].name
    assert (dirs[0] / "project.json").exists()


def test_version_flag() -> None:
    from interview_video import __version__

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and __version__ in result.output


def test_topics_short_n_option() -> None:
    result = runner.invoke(app, ["topics", "--help"])
    assert result.exit_code == 0 and "-n" in result.output


def test_generate_resume_reuses_script(fake_media: None, tmp_path: Path) -> None:
    """--resume <dir>: 复用 script.json(不调 LLM), 产物写回同一目录。"""
    out_dir = tmp_path / "prev"
    out_dir.mkdir()
    (out_dir / "script.json").write_text(SAMPLE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    result = runner.invoke(app, ["generate", "--resume", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "断点续跑" in result.output
    assert (out_dir / "final.mp4").exists() and (out_dir / "project.json").exists()


def test_generate_failure_prints_resume_hint(
    fake_media: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a, **kw):
        raise RuntimeError("ffmpeg 挂了")

    monkeypatch.setattr(assemble_mod, "finalize_video", boom)
    out_dir = tmp_path / "out"
    result = runner.invoke(
        app, ["generate", "--script-file", str(SAMPLE_SCRIPT), "--out", str(out_dir)]
    )
    assert result.exit_code != 0
    assert "--resume" in result.output and str(out_dir) in result.output.replace("\n", "")


def test_main_short_error_no_traceback(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path) -> None:
    from interview_video import cli as cli_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IVB_DEBUG", raising=False)
    monkeypatch.setattr("sys.argv", ["ivb", "topics", "-n", "2"])

    def _no_llm(settings, audit=None):
        raise RuntimeError("未配置 LLM_API_KEY(测试注入)")

    monkeypatch.setattr(llm_mod, "get_chat_model", _no_llm)
    with pytest.raises(SystemExit) as ei:
        cli_mod.main()
    assert ei.value.code == 1
    out = capsys.readouterr().out
    assert "LLM_API_KEY" in out and "Traceback" not in out


def test_slug_ascii() -> None:
    from interview_video.cli import _slug

    assert _slug("Hello World! 2024") == "hello-world-2024"
    assert _slug("程序员面试") == "程序员面试"
    assert _slug("  ") == "video"


# ---------- bilibili.py: 仅测 credential 组装与缺参报错(不联网, 不真实上传) ----------


def test_build_credential_complete() -> None:
    from bilibili_api import Credential

    from interview_video.bilibili import build_credential

    settings = Settings(
        bili_sessdata="s", bili_jct="j", bili_buvid3="b",
        bili_upload_enabled=True,
    )
    cred = build_credential(settings)
    assert isinstance(cred, Credential)
    assert cred.sessdata == "s" and cred.bili_jct == "j" and cred.buvid3 == "b"


@pytest.mark.parametrize("missing", ["bili_sessdata", "bili_jct", "bili_buvid3"])
def test_build_credential_incomplete_returns_none(missing: str) -> None:
    from interview_video.bilibili import build_credential

    kwargs = {"bili_sessdata": "s", "bili_jct": "j", "bili_buvid3": "b"}
    kwargs.pop(missing)
    assert build_credential(Settings(**kwargs)) is None


def test_upload_project_disabled_raises_with_guide() -> None:
    import pytest

    from interview_video.bilibili import upload_project

    settings = Settings(bili_upload_enabled=False)
    with pytest.raises(RuntimeError, match="BILI_UPLOAD_ENABLED"):
        asyncio_run(upload_project(Path("whatever"), settings))


def test_upload_project_missing_creds_raises_cookie_guide() -> None:
    from interview_video.bilibili import upload_project

    settings = Settings(bili_upload_enabled=True)  # 三件套全空
    with pytest.raises(RuntimeError, match="SESSDATA"):
        asyncio_run(upload_project(Path("whatever"), settings))


def asyncio_run(coro):
    """小工具: 在同步测试里跑协程。"""
    import asyncio

    return asyncio.run(coro)



def test_generate_stop_after_script_then_resume(
    fake_media: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--stop-after script: 只落盘 script.json(不跑 TTS/预检), 提示续跑命令; 审改后 --resume 出片。"""
    from interview_video import doctor as doctor_mod

    def no_preflight(settings):
        raise AssertionError("只生成脚本不应做媒体预检")

    monkeypatch.setattr(doctor_mod, "preflight", no_preflight)
    real_tts = tts_mod.synthesize_scenes

    async def no_tts(*a, **kw):
        raise AssertionError("只生成脚本不应调用 TTS")

    monkeypatch.setattr(tts_mod, "synthesize_scenes", no_tts)
    out_dir = tmp_path / "draft"
    result = runner.invoke(
        app, ["generate", "--script-file", str(SAMPLE_SCRIPT), "--out", str(out_dir),
              "--stop-after", "script"],
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "script.json").exists()
    assert not (out_dir / "project.json").exists() and not (out_dir / "scenes").exists()
    flat = result.output.replace("\n", "").replace("│", "").replace(" ", "")
    assert f"ivbgenerate--resume{out_dir}".replace(" ", "") in flat

    data = json.loads((out_dir / "script.json").read_text(encoding="utf-8"))
    data["title"] = "人工改过的标题"
    (out_dir / "script.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(doctor_mod, "preflight", lambda settings: None)
    monkeypatch.setattr(tts_mod, "synthesize_scenes", real_tts)
    result = runner.invoke(app, ["generate", "--resume", str(out_dir)])
    assert result.exit_code == 0, result.output
    manifest = json.loads((out_dir / "project.json").read_text(encoding="utf-8"))
    assert manifest["script"]["title"] == "人工改过的标题"


def test_generate_stop_after_invalid_choice(tmp_path: Path) -> None:
    result = runner.invoke(app, ["generate", "--stop-after", "tts", "--out", str(tmp_path / "x")])
    assert result.exit_code == 2 and "script" in result.output
    result = runner.invoke(
        app, ["generate", "--stop-after", "script", "--resume", str(tmp_path)]
    )
    assert result.exit_code == 2
