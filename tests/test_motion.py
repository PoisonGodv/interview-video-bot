"""VIDEO_MOTION 画面动效: 要点逐条出现时间切分 / 模板 reveal / 分帧合成时长。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from interview_video import assemble as assemble_mod
from interview_video.assemble import (
    _motion_filter,
    build_scene_clips,
    bullet_times,
    ffprobe_duration,
    finalize_video,
    reveal_frame_counts,
)
from interview_video.config import Settings
from interview_video.models import Cue, Scene, SceneMedia, Script
from interview_video.render import _build_env, _render_html

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="本机无 ffmpeg/ffprobe")


def _s(tmp_path: Path, **kw) -> Settings:
    kw.setdefault("out_root", tmp_path / "output")
    return Settings(_env_file=None, **kw)


CUES = [
    Cue(start=0.0, end=2.0, text="大家好，今天聊大厂面试"),
    Cue(start=2.0, end=4.0, text="里的三大高频坑。"),
]


def test_bullet_times_from_cues() -> None:
    t = bullet_times(["大厂面试", "三大高频坑"], CUES, 5.0)
    assert t[0] == pytest.approx(2.0 * 6 / 10)   # “大厂面试”在首条 cue 第 7 字
    assert t[1] == pytest.approx(2.0 + 2.0 * 2 / 7)


def test_bullet_times_missing_interpolated() -> None:
    t = bullet_times(["大厂面试", "不存在的要点", "三大高频坑"], CUES, 5.0)
    assert t[0] < t[1] < t[2]
    assert t[1] == pytest.approx((t[0] + t[2]) / 2)


def test_bullet_times_no_match_even_split() -> None:
    assert bullet_times(["甲", "乙", "丙"], [], 8.0) == pytest.approx([2.0, 4.0, 6.0])
    assert bullet_times([], CUES, 5.0) == []


def test_bullet_times_prefix_fallback_and_order() -> None:
    cues = [Cue(start=0.0, end=4.0, text="先说量化结果，再说量化结果的来源")]
    # 要点整条不在口播里, 退到前 4 字; 第二条从上一条命中之后找
    t = bullet_times(["量化结果要写数字", "量化结果"], cues, 4.0)
    assert t[0] < t[1]


@pytest.mark.parametrize("fps", [25, 30])
@pytest.mark.parametrize("dur", [0.05, 3.337, 13.1])
def test_reveal_frame_counts_sum_equals_duration(dur: float, fps: int) -> None:
    times = bullet_times(["大厂面试", "三大高频坑", "x"], CUES, dur)
    counts = reveal_frame_counts(times, dur, fps)
    assert len(counts) == 4 and all(n >= 0 for n in counts)
    assert sum(counts) == max(round(dur * fps), 1)
    assert abs(sum(counts) / fps - dur) <= 1 / fps


def test_reveal_frame_counts_clamps_out_of_range() -> None:
    assert reveal_frame_counts([-1.0, 99.0], 2.0, 10) == [0, 20, 0]


def test_motion_filter_shape() -> None:
    f = _motion_filter([30, 45], 1920, 1080, 30)
    assert "[0:v]" in f and "[1:v]" in f and "concat=n=2" in f
    assert "trim=end_frame=30" in f and "trim=end_frame=45" in f
    assert "crop=1920:1080" in f and "fade=t=out:s=71:n=4" in f
    assert f.endswith("[vout]")


def _scene_html(reveal):
    script = Script(title="t", scenes=[Scene(header="h", bullets=["一号要点", "二号要点"], narration="n")])
    return _render_html(
        _build_env(), "scene.html", brand="b", title="t", script=script,
        scene=script.scenes[0], index=0, total=1, reveal=reveal,
    )


def test_scene_template_reveal() -> None:
    assert "visibility:hidden" not in _scene_html(None)
    html = _scene_html(1)
    assert html.count("visibility:hidden") == 1
    assert html.index("visibility:hidden") > html.index("一号要点")
    assert _scene_html(0).count("visibility:hidden") == 2


def test_motion_without_sidecar_uses_single_frame(tmp_path: Path, monkeypatch) -> None:
    cmds: list[list[str]] = []
    monkeypatch.setattr(assemble_mod, "_run", lambda cmd, cwd, timeout=300: cmds.append(cmd))
    monkeypatch.setattr(assemble_mod, "ffprobe_duration", lambda p: 2.0)
    sm = SceneMedia(index=0, png=tmp_path / "a.png", mp3=tmp_path / "a.mp3", duration=2.0)
    build_scene_clips([sm], tmp_path / "clips", _s(tmp_path, video_motion=True))
    cmd = cmds[0]
    assert cmd.count("-loop") == 1
    assert "trim=end_frame=60" in cmd[cmd.index("-filter_complex") + 1]
    assert cmd[cmd.index("-map", cmd.index("-map") + 1) + 1] == "1:a"


# ---------- integration: 真实 ffmpeg 合成, 校验时长 ----------

def _mp3(path: Path, duration: float) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
         "-c:a", "libmp3lame", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-500:]


def _pixel(video: Path, t: float, xy: tuple[int, int]) -> tuple[int, int, int]:
    frame = video.parent / f"px_{t:.2f}.png"
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{t}", "-i", str(video), "-frames:v", "1", str(frame)],
        capture_output=True, timeout=60, check=True,
    )
    return Image.open(frame).convert("RGB").getpixel(xy)


@requires_ffmpeg
@pytest.mark.integration
def test_motion_clips_duration_and_reveal(tmp_path: Path) -> None:
    """分步帧按 cue 时间切换, clip/成片时长与音频误差 < 0.1s。"""
    scenes = tmp_path / "scenes"
    scenes.mkdir()
    colors = [(0, 0, 0), (200, 0, 0), (0, 200, 0)]
    names = ["scene_000_r0.png", "scene_000_r1.png", "scene_000.png"]
    for c, n in zip(colors, names):
        Image.new("RGB", (1920, 1080), c).save(scenes / n)
    (scenes / "scene_000.reveal.json").write_text(
        json.dumps({"bullets": ["大厂面试", "三大高频坑"], "frames": names}, ensure_ascii=False),
        encoding="utf-8",
    )
    Image.new("RGB", (1920, 1080), (0, 0, 200)).save(scenes / "scene_001.png")
    _mp3(scenes / "a.mp3", 5.0)
    _mp3(scenes / "b.mp3", 3.0)
    media = [
        SceneMedia(index=0, png=scenes / "scene_000.png", mp3=scenes / "a.mp3", duration=5.0, cues=CUES),
        SceneMedia(index=1, png=scenes / "scene_001.png", mp3=scenes / "b.mp3", duration=3.0),
    ]
    settings = _s(tmp_path, video_motion=True)
    clips = build_scene_clips(media, tmp_path / "clips", settings)
    for clip, sm, want in zip(clips, media, (5.0, 3.0)):
        assert abs(ffprobe_duration(clip) - ffprobe_duration(sm.mp3)) < 0.1
        assert abs(sm.duration - want) < 0.1

    c = clips[0]
    assert _pixel(c, 0.6, (960, 540))[0] < 40            # 0 条要点
    r, g, _ = _pixel(c, 1.8, (960, 540))                  # “大厂面试”后 (1.2s)
    assert r > 150 and g < 40
    r, g, _ = _pixel(c, 3.5, (960, 540))                  # “三大高频坑”后 (~2.57s)
    assert g > 150 and r < 40

    out = finalize_video(clips, [], tmp_path / "final.mp4", settings)
    assert abs(ffprobe_duration(out) - 8.0) < 0.1
