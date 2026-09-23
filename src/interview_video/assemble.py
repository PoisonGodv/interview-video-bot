"""ffmpeg/ffprobe 合成模块(SPECS §5)。

要点:
- 本机 ffmpeg 无 libass, 字幕绝不烧录, 只走 overlay 透明 PNG 字幕条方案
- 所有 ffmpeg 调用以 ``cwd=out_dir`` + 相对路径传参, 避免转义地狱
- 单次调用超时 >= 300s; 非零退出抛 RuntimeError 附 stderr 末尾 500 字
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import Settings
from .models import Chapter, SceneMedia, Script, SubtitleBar

# 单次 ffmpeg 调用超时(秒), SPECS 要求 >= 300
_FFMPEG_TIMEOUT = 600
# 错误信息里保留的 stderr 末尾长度
_STDERR_TAIL = 500


def _run(cmd: list[str], cwd: Path) -> None:
    """执行 ffmpeg 命令; 非零退出抛 RuntimeError(附 stderr 末尾 500 字)。"""
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-_STDERR_TAIL:]
        raise RuntimeError(
            f"ffmpeg 失败(exit={proc.returncode}): {' '.join(cmd[:6])} ...\n"
            f"stderr 末尾: {tail}"
        )


def _rel(path: Path, anchor: Path) -> str:
    """把 path 转成相对 anchor 的路径(配合 cwd=anchor 传参, 避开绝对路径)。"""
    return os.path.relpath(Path(path).resolve(), anchor.resolve())


def _fmt_t(t: float) -> str:
    """enable=between 的时间参数: 保留 2~3 位小数(去多余尾零)。"""
    return f"{t:.3f}".rstrip("0").rstrip(".")


def ffprobe_duration(path: Path) -> float:
    """ffprobe 读取媒体时长(秒, 精确值)。"""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-_STDERR_TAIL:]
        raise RuntimeError(f"ffprobe 失败: {path}\nstderr 末尾: {tail}")
    return float(proc.stdout.strip())


def build_scene_clips(
    scene_media: list[SceneMedia], clips_dir: Path, settings: Settings
) -> list[Path]:
    """每场景: 静态图 + mp3 → h264/aac/yuv420p clip(scale+pad 到目标分辨率)。

    之后用 ffprobe 精确时长回填 ``SceneMedia.duration``。
    """
    clips_dir = Path(clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)
    anchor = clips_dir.parent.resolve()  # 视为 out_dir, 输入输出均相对它传参
    w, h, fps = settings.video_width, settings.video_height, settings.fps
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
    )
    clips: list[Path] = []
    for sm in scene_media:
        if sm.png is None:
            raise RuntimeError(f"场景 {sm.index} 缺少 png(render 阶段未产出?)")
        clip = clips_dir / f"scene_{sm.index:03d}.mp4"
        dur = sm.duration
        if dur <= 0:
            dur = ffprobe_duration(sm.mp3)  # 兜底: 无估算时长时用音频时长
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(fps), "-i", _rel(sm.png, anchor),
            "-i", _rel(sm.mp3, anchor),
            "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
            "-vf", vf,
            "-t", f"{dur:.3f}",
            _rel(clip, anchor),
        ]
        _run(cmd, anchor)
        sm.duration = ffprobe_duration(clip)  # 回填精确值, 供后续 chapters/overlay 用
        clips.append(clip)
    return clips


def _write_concat_list(clips: list[Path], list_path: Path, anchor: Path) -> Path:
    """写 concat demuxer 列表文件: 每行 ``file '<相对 anchor 路径>'``。"""
    lines = [f"file '{_rel(c, anchor)}'" for c in clips]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


def finalize_video(
    clips: list[Path], bars: list[SubtitleBar], out_path: Path, settings: Settings
) -> Path:
    """concat(-c copy) → 有 bars 时单条命令链式 overlay → loudnorm → final.mp4。

    无 bars 时跳过 overlay, 视频流直拷、仅音频响度归一。
    """
    out_path = Path(out_path)
    out_dir = out_path.parent.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # ① concat demuxer 出 base.mp4(不重编码)
    list_path = out_dir / "concat.list.txt"
    _write_concat_list(clips, list_path, out_dir)
    base = out_dir / "base.mp4"
    _run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path.name,
         "-c", "copy", base.name],
        out_dir,
    )

    loudnorm = (
        f"loudnorm=I={settings.loudness_i}:TP={settings.loudness_tp}"
        f":LRA={settings.loudness_lra}"
    )

    if not bars:
        # 无字幕条: 视频直拷, 仅音频响度归一
        _run(
            ["ffmpeg", "-y", "-i", base.name,
             "-c:v", "copy", "-af", loudnorm,
             "-c:a", "aac", "-b:a", "160k", out_path.name],
            out_dir,
        )
        return out_path

    # ② 单条命令链式 overlay 全部字幕条(每 bar 一个 overlay, enable 限定时间窗)
    inputs: list[str] = ["-i", base.name]
    for bar in bars:
        inputs += ["-i", _rel(bar.png, out_dir)]
    parts: list[str] = []
    prev = "0:v"
    for i, bar in enumerate(bars, start=1):
        label = f"v{i}"
        start, end = _fmt_t(bar.start), _fmt_t(bar.end)
        chain = f"[{prev}][{i}:v]overlay=0:0:enable='between(t,{start},{end})'"
        # 末段标签接 format=yuv420p 后作为最终视频流出, 避免空链
        chain += f",format=yuv420p[{label}]" if i == len(bars) else f"[{label}]"
        parts.append(chain)
        prev = label
    filter_complex = ";".join(parts)
    _run(
        ["ffmpeg", "-y"] + inputs +
        ["-filter_complex", filter_complex,
         "-map", f"[v{len(bars)}]", "-map", "0:a",   # 最后标签 + 原音频
         "-af", loudnorm,
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k",
         out_path.name],
        out_dir,
    )
    return out_path


def build_chapters(script: Script, scene_media: list[SceneMedia]) -> list[Chapter]:
    """章节: 场景起点按精确 duration 累加, title 取 scene.header。"""
    chapters: list[Chapter] = []
    cursor = 0.0
    for i, (scene, sm) in enumerate(zip(script.scenes, scene_media)):
        chapters.append(Chapter(index=i, title=scene.header, start=cursor))
        cursor += sm.duration
    return chapters
