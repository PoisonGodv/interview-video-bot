"""pytest 公共配置: integration 标记默认跳过(需网络/ffmpeg/chromium)。"""
from __future__ import annotations

from pathlib import Path

import pytest

from interview_video.config import Settings
from interview_video.models import (
    Chapter,
    Cue,
    RenderResult,
    SceneMedia,
    Script,
    SubtitleBar,
)


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="运行依赖网络/ffmpeg/chromium 的集成测试",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: 需要外部资源(网络/ffmpeg/chromium), 默认跳过, --run-integration 启用",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="integration 测试, 需要 --run-integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def _png_bytes() -> bytes:
    """最小的合法 PNG(16x16 RGBA), 供假媒体实现落盘。"""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def fake_media(monkeypatch: pytest.MonkeyPatch) -> None:
    """把媒体层重函数全部替换为本地假实现(造小文件, 不碰网络/浏览器/ffmpeg)。"""
    from interview_video import assemble as assemble_mod
    from interview_video import render as render_mod
    from interview_video import subtitle_bars as bars_mod
    from interview_video import tts as tts_mod

    async def fake_synthesize_scenes(
        script: Script, scenes_dir: Path, settings: Settings
    ) -> list[SceneMedia]:
        scenes_dir.mkdir(parents=True, exist_ok=True)
        media: list[SceneMedia] = []
        for i, scene in enumerate(script.scenes):
            mp3 = scenes_dir / f"scene_{i:03d}.mp3"
            mp3.write_bytes(b"FAKE_MP3")
            media.append(
                SceneMedia(
                    index=i,
                    mp3=mp3,
                    duration=2.0 + i,
                    cues=[Cue(start=0.0, end=1.5, text=f"{scene.header}字幕")],
                )
            )
        return media

    def fake_render_project(
        script: Script, out_dir: Path, settings: Settings, industry: str = ""
    ) -> RenderResult:
        frames: list[Path] = []
        for i in range(len(script.scenes)):
            png = out_dir / "scenes" / f"scene_{i:03d}.png"
            png.parent.mkdir(parents=True, exist_ok=True)
            png.write_bytes(_png_bytes())
            frames.append(png)
        cover = out_dir / "cover.png"
        cover.write_bytes(_png_bytes())
        return RenderResult(cover=cover, frames=frames)

    def fake_build_scene_clips(
        scene_media: list[SceneMedia], clips_dir: Path, settings: Settings
    ) -> list[Path]:
        clips_dir.mkdir(parents=True, exist_ok=True)
        clips: list[Path] = []
        for m in scene_media:
            mp4 = clips_dir / f"scene_{m.index:03d}.mp4"
            mp4.write_bytes(b"FAKE_MP4")
            clips.append(mp4)
        return clips

    def fake_render_subtitle_bars(
        cues: list[Cue], subs_dir: Path, settings: Settings
    ) -> list[SubtitleBar]:
        subs_dir.mkdir(parents=True, exist_ok=True)
        bars: list[SubtitleBar] = []
        for i, cue in enumerate(cues):
            png = subs_dir / f"bar_{i:03d}.png"
            png.write_bytes(_png_bytes())
            bars.append(
                SubtitleBar(index=i, png=png, start=cue.start, end=cue.end, text=cue.text)
            )
        return bars

    def fake_finalize_video(
        clips: list[Path], bars: list[SubtitleBar], out_path: Path, settings: Settings
    ) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"FAKE_FINAL_MP4")
        return out_path

    def fake_offset_cues(scene_media: list[SceneMedia]) -> list[Cue]:
        cues: list[Cue] = []
        t = 0.0
        for m in scene_media:
            for c in m.cues:
                cues.append(Cue(start=t + c.start, end=t + c.end, text=c.text))
            t += m.duration
        return cues

    def fake_build_srt(cues: list[Cue]) -> str:
        return "\n".join(
            f"{i}\n00:00:00,000 --> 00:00:01,500\n{cue.text}"
            for i, cue in enumerate(cues, 1)
        )

    def fake_build_chapters(
        script: Script, scene_media: list[SceneMedia]
    ) -> list[Chapter]:
        chapters: list[Chapter] = []
        t = 0.0
        for i, (scene, m) in enumerate(zip(script.scenes, scene_media)):
            chapters.append(Chapter(index=i, title=scene.header, start=t))
            t += m.duration
        return chapters

    from interview_video import doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "preflight", lambda settings: None)
    monkeypatch.setattr(tts_mod, "synthesize_scenes", fake_synthesize_scenes)
    monkeypatch.setattr(tts_mod, "offset_cues", fake_offset_cues)
    monkeypatch.setattr(tts_mod, "build_srt", fake_build_srt)
    monkeypatch.setattr(render_mod, "render_project", fake_render_project)
    monkeypatch.setattr(assemble_mod, "build_scene_clips", fake_build_scene_clips)
    monkeypatch.setattr(assemble_mod, "finalize_video", fake_finalize_video)
    monkeypatch.setattr(assemble_mod, "build_chapters", fake_build_chapters)
    monkeypatch.setattr(bars_mod, "render_subtitle_bars", fake_render_subtitle_bars)
