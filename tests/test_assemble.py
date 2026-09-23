"""assemble 模块测试。

纯函数(concat list 内容 / chapters 计算 / 时间格式化)不打标;
依赖 ffmpeg 的用 lavfi 造素材(testsrc2 图 + sine 音频), 打 integration 并在缺二进制时 skip。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from interview_video.assemble import (
    _fmt_t,
    _write_concat_list,
    build_chapters,
    build_scene_clips,
    ffprobe_duration,
    finalize_video,
)
from interview_video.config import Settings
from interview_video.models import Scene, SceneMedia, Script, SubtitleBar

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="本机无 ffmpeg/ffprobe")


# ---------- 纯函数(不打标) ----------

def test_write_concat_list_content(tmp_path: Path) -> None:
    """concat 列表文件每行是 ``file '<相对路径>'``, 相对 anchor 目录。"""
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clips = [clips_dir / "scene_000.mp4", clips_dir / "scene_001.mp4"]
    for c in clips:
        c.write_bytes(b"placeholder")
    list_path = tmp_path / "list.txt"
    _write_concat_list(clips, list_path, tmp_path)
    lines = list_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["file 'clips/scene_000.mp4'", "file 'clips/scene_001.mp4'"]


def test_write_concat_list_empty(tmp_path: Path) -> None:
    """空 clip 列表写出空文件(内容只有结尾换行)。"""
    list_path = tmp_path / "list.txt"
    _write_concat_list([], list_path, tmp_path)
    assert list_path.read_text(encoding="utf-8") == "\n"


def test_build_chapters_accumulates_exact_duration() -> None:
    """章节起点按精确 duration 累加, title 取 scene.header。"""
    script = Script(
        title="t",
        scenes=[
            Scene(header="开场", narration="a"),
            Scene(header="要点", narration="b"),
            Scene(header="结尾", kind="ending", narration="c"),
        ],
    )
    media = [
        SceneMedia(index=0, mp3=Path("a.mp3"), duration=2.0),
        SceneMedia(index=1, mp3=Path("b.mp3"), duration=3.5),
        SceneMedia(index=2, mp3=Path("c.mp3"), duration=1.5),
    ]
    chapters = build_chapters(script, media)
    assert [c.index for c in chapters] == [0, 1, 2]
    assert [c.title for c in chapters] == ["开场", "要点", "结尾"]
    assert [c.start for c in chapters] == [0.0, 2.0, 5.5]


def test_fmt_t_keeps_two_to_three_decimals() -> None:
    """enable=between 的时间参数保留 2~3 位小数, 去多余尾零。"""
    assert _fmt_t(0.0) == "0"
    assert _fmt_t(2.5) == "2.5"
    assert _fmt_t(2.05) == "2.05"
    assert _fmt_t(12.345) == "12.345"
    assert _fmt_t(1.0) == "1"


# ---------- integration(需 ffmpeg; lavfi 造素材, 不联网) ----------

def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", *args], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr[-500:]


def _make_png(path: Path, size: str = "1920x1080") -> None:
    """lavfi testsrc2 取一帧当场景静态图。"""
    _ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size={size}:duration=1", "-frames:v", "1", str(path)])


def _make_mp3(path: Path, duration: float = 2.0) -> None:
    """lavfi sine 生成定长音频(mp3)。"""
    _ffmpeg([
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:a", "libmp3lame", str(path),
    ])


def _make_bar_png(path: Path) -> None:
    """1920x1080 全透明 PNG 字幕条(Pillow, 不依赖 ffmpeg)。"""
    from PIL import Image

    Image.new("RGBA", (1920, 1080), (0, 0, 0, 0)).save(path)


def _ffprobe_stream(path: Path, selector: str) -> dict[str, str]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", selector,
            "-show_entries", "stream=codec_name,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    return json.loads(proc.stdout)["streams"][0]


@requires_ffmpeg
@pytest.mark.integration
def test_build_scene_clips_duration(tmp_path: Path) -> None:
    """单场景 clip 时长 ≈ 预期(2s), 且 SceneMedia.duration 被精确回填。"""
    scenes_dir = tmp_path / "scenes"
    clips_dir = tmp_path / "clips"
    scenes_dir.mkdir()
    png, mp3 = scenes_dir / "scene_000.png", scenes_dir / "scene_000.mp3"
    _make_png(png)
    _make_mp3(mp3)
    sm = SceneMedia(index=0, png=png, mp3=mp3, duration=2.0)
    settings = Settings()

    clips = build_scene_clips([sm], clips_dir, settings)

    assert len(clips) == 1 and clips[0].exists()
    assert abs(ffprobe_duration(clips[0]) - 2.0) < 0.5
    assert abs(sm.duration - 2.0) < 0.5  # 回填值同样接近 2s


@requires_ffmpeg
@pytest.mark.integration
def test_finalize_concat_total_duration(tmp_path: Path) -> None:
    """无 bars: concat 总时长 ≈ N × 2s。"""
    scenes_dir = tmp_path / "scenes"
    clips_dir = tmp_path / "clips"
    scenes_dir.mkdir()
    media: list[SceneMedia] = []
    for i in range(3):
        png, mp3 = scenes_dir / f"scene_{i:03d}.png", scenes_dir / f"scene_{i:03d}.mp3"
        _make_png(png)
        _make_mp3(mp3)
        media.append(SceneMedia(index=i, png=png, mp3=mp3, duration=2.0))
    clips = build_scene_clips(media, clips_dir, Settings())

    out = finalize_video(clips, [], tmp_path / "final.mp4", Settings())

    assert out.exists()
    assert abs(ffprobe_duration(out) - 6.0) < 0.9


@requires_ffmpeg
@pytest.mark.integration
def test_finalize_with_bars_stream_params(tmp_path: Path) -> None:
    """带 bars 的 finalize: 输出存在, 视频流 h264/1920x1080, 音频流 aac。"""
    scenes_dir = tmp_path / "scenes"
    clips_dir = tmp_path / "clips"
    subs_dir = tmp_path / "subs"
    for d in (scenes_dir, clips_dir, subs_dir):
        d.mkdir()
    media: list[SceneMedia] = []
    for i in range(2):
        png, mp3 = scenes_dir / f"scene_{i:03d}.png", scenes_dir / f"scene_{i:03d}.mp3"
        _make_png(png)
        _make_mp3(mp3)
        media.append(SceneMedia(index=i, png=png, mp3=mp3, duration=2.0))
    clips = build_scene_clips(media, clips_dir, Settings())

    bars: list[SubtitleBar] = []
    for i, (start, end) in enumerate([(0.1, 1.0), (2.0, 3.0)]):
        png = subs_dir / f"bar_{i:03d}.png"
        _make_bar_png(png)
        bars.append(SubtitleBar(index=i, png=png, start=start, end=end, text=f"字幕{i}"))

    out = finalize_video(clips, bars, tmp_path / "final.mp4", Settings())

    assert out.exists()
    v = _ffprobe_stream(out, "v:0")
    assert v["codec_name"] == "h264"
    assert (v["width"], v["height"]) == (1920, 1080)
    a = _ffprobe_stream(out, "a:0")
    assert a["codec_name"] == "aac"
    # overlay 不应明显改变总时长(≈ 4s)
    assert abs(ffprobe_duration(out) - 4.0) < 0.9
