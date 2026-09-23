"""CLI 测试: --help 与 generate --script-file happy path(媒体层 monkeypatch, 不联网/不起浏览器)。"""
from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

# --- 兼容 shim(不改只读契约) ---
# 已装 langchain_core 1.6.4 中 GenericFakeChatModel 位于 fake_chat_models 子模块,
# 而只读契约 llm.py 从旧路径 fake_chat 导入(已不存在); 详见 tests/test_graph.py 注释。
try:
    import langchain_core.language_models.fake_chat as _fake_chat  # noqa: F401
except ModuleNotFoundError:
    from langchain_core.language_models import fake_chat_models as _fake_chat_models

    _shim = types.ModuleType("langchain_core.language_models.fake_chat")
    _shim.GenericFakeChatModel = _fake_chat_models.GenericFakeChatModel
    sys.modules["langchain_core.language_models.fake_chat"] = _shim

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
    def _no_llm(settings: Settings):
        raise RuntimeError("未配置 LLM_API_KEY(测试注入)")

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

