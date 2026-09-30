"""ffmpeg/ffprobe 合成模块(SPECS §5)。

要点:
- 本机 ffmpeg 无 libass, 字幕绝不烧录, 只走 overlay 透明 PNG 字幕条方案
- 所有 ffmpeg 调用以 ``cwd=out_dir`` + 相对路径传参, 避免转义地狱
- 子进程经 procs.run 启动: 所在任务被取消时 terminate/kill, 不留孤儿 ffmpeg
- 单次调用超时随媒体时长伸缩(下限 300s); 非零退出抛 RuntimeError 附 stderr 末尾 500 字
- 字幕条按场景 clip 逐段烧录(cue 平移到场景本地时间), 再 concat(-c copy), 避免长视频单条巨型 filter
- 可选画面动效(VIDEO_MOTION): 要点按口播时间逐条出现、缓慢放大、场景首尾各 0.15s 黑场过渡(不改时长)
- 可选 BGM(BGM_FILE): 在最后一遍音频处理里循环铺满、压低音量、口播时 sidechain 闪避、结尾淡出
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path

from . import procs
from .config import Settings
from .models import Chapter, Cue, SceneMedia, Script, SubtitleBar

logger = logging.getLogger(__name__)

# 单次 ffmpeg 调用超时(秒): max(下限, 固定开销 + 媒体时长 × 系数), SPECS 要求 >= 300
_FFMPEG_TIMEOUT_FLOOR = 300.0
_FFMPEG_TIMEOUT_BASE = 60.0
_FFMPEG_TIMEOUT_PER_SEC = 3.0
# 错误信息里保留的 stderr 末尾长度
_STDERR_TAIL = 500
# 所有重编码共用同一组视频编码参数, 保证烧录后的 clip 可 concat -c copy
_X264_ARGS = [
    "-c:v", "libx264", "-preset", "faster", "-tune", "stillimage", "-pix_fmt", "yuv420p",
]
# 跨场景边界的字幕条切片后, 短于此值(秒)的碎片丢弃
_MIN_SLICE = 1e-3
# BGM 结尾淡出时长(秒), 短片按总长 1/4 封顶
_BGM_FADE_SEC = 3.0
_AUDIO_FMT = "aformat=sample_rates=44100:channel_layouts=stereo"
# 画面动效: 每场景从 1.00 缓慢放大到 1+_ZOOM; 首尾黑场淡入淡出各 _FADE_SEC(相邻场景合计 0.3s 过渡)
_ZOOM = 0.04
_FADE_SEC = 0.15
# 要点匹配口播时忽略的字符(标点/空白)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def _timeout_for(duration: float) -> float:
    """按媒体时长估算单次 ffmpeg 调用超时(秒), 不低于下限。"""
    return max(
        _FFMPEG_TIMEOUT_FLOOR,
        _FFMPEG_TIMEOUT_BASE + max(duration, 0.0) * _FFMPEG_TIMEOUT_PER_SEC,
    )


def _run(cmd: list[str], cwd: Path, timeout: float = _FFMPEG_TIMEOUT_FLOOR) -> None:
    """执行 ffmpeg 命令; 非零退出抛 RuntimeError(附 stderr 末尾 500 字)。"""
    proc = procs.run(cmd, cwd=cwd, timeout=timeout)
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
    proc = procs.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=60,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-_STDERR_TAIL:]
        raise RuntimeError(f"ffprobe 失败: {path}\nstderr 末尾: {tail}")
    return float(proc.stdout.strip())


def _norm(text: str) -> str:
    return _NON_WORD.sub("", text).lower()


def bullet_times(bullets: list[str], cues: list[Cue], duration: float) -> list[float]:
    """每条要点在口播里首次出现的时间(场景本地秒), 单调不减。

    在拼接后的 cue 文本里顺序查找要点(整条, 找不到再退到前 4/3 个字), 命中位置按所在
    cue 内字数线性插值成时间; 找不到的要点在前后已知时间点之间均分(全找不到即整体均分)。
    """
    k = len(bullets)
    if k == 0:
        return []
    text, spans = "", []  # spans: (起始字符位置, 字数, cue)
    for c in cues:
        t = _norm(c.text)
        if t:
            spans.append((len(text), len(t), c))
            text += t

    def time_at(pos: int) -> float:
        for start, n, c in spans:
            if pos < start + n:
                return c.start + (c.end - c.start) * (pos - start) / n
        return duration

    found: list[float | None] = []
    cursor = 0
    for b in bullets:
        nb, hit = _norm(b), -1
        for key in [nb, nb[:4], nb[:3]] if len(nb) > 4 else [nb]:
            hit = text.find(key, cursor) if key else -1
            if hit >= 0:
                break
        if hit >= 0:
            found.append(min(max(time_at(hit), 0.0), duration))
            cursor = hit + 1
        else:
            found.append(None)

    anchors = [(-1, 0.0)] + [(i, t) for i, t in enumerate(found) if t is not None] + [(k, duration)]
    out = list(found)
    for (i0, t0), (i1, t1) in zip(anchors, anchors[1:]):
        for j in range(i0 + 1, i1):
            out[j] = t0 + (t1 - t0) * (j - i0) / (i1 - i0)
    return out  # type: ignore[return-value]


def reveal_frame_counts(times: list[float], duration: float, fps: int) -> list[int]:
    """分步帧(0..k 条要点)各自的帧数; 总和恒等于 round(duration*fps), 可为 0(跳过该步)。"""
    total = max(round(duration * fps), 1)
    cuts = [0] + [min(max(round(t * fps), 0), total) for t in times] + [total]
    for i in range(1, len(cuts)):
        cuts[i] = max(cuts[i], cuts[i - 1])
    return [b - a for a, b in zip(cuts, cuts[1:])]


def _reveal_steps(png: Path) -> tuple[list[str], list[Path]] | None:
    """读 render 阶段写的分步帧清单; 缺失/损坏/帧文件不全返回 None(按单帧处理)。"""
    sidecar = Path(png).with_suffix(".reveal.json")
    if not sidecar.is_file():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        bullets = [str(b) for b in data["bullets"]]
        frames = [sidecar.parent / str(f) for f in data["frames"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if len(frames) != len(bullets) + 1 or not all(f.is_file() for f in frames):
        return None
    return bullets, frames


def _motion_filter(counts: list[int], w: int, h: int, fps: int) -> str:
    """分步帧(counts 均 > 0)按帧数截取 → concat → 缓慢放大(scale+居中 crop) → 首尾黑场淡入淡出, 出口 [vout]。"""
    fit = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    parts = [
        f"[{i}:v]{fit},trim=end_frame={n},setpts=PTS-STARTPTS[s{i}]" for i, n in enumerate(counts)
    ]
    labels = [f"[s{i}]" for i in range(len(counts))]
    total = sum(counts)
    denom = max(total - 1, 1)
    fade_n = min(round(_FADE_SEC * fps), total // 4)
    chain = (
        f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,"
        f"scale=w='trunc({w}*(1+{_ZOOM}*n/{denom})/2)*2':h='trunc({h}*(1+{_ZOOM}*n/{denom})/2)*2'"
        f":eval=frame,crop={w}:{h},setsar=1"
    )
    if fade_n > 0:
        chain += f",fade=t=in:s=0:n={fade_n},fade=t=out:s={total - fade_n}:n={fade_n}"
    parts.append(chain + ",format=yuv420p[vout]")
    return ";".join(parts)


def _motion_clip_cmd(
    sm: SceneMedia, clip: Path, dur: float, anchor: Path, settings: Settings
) -> list[str]:
    (w, h), fps = settings.frame_size(), settings.fps
    steps = _reveal_steps(sm.png)
    if steps is None:
        frames, counts = [Path(sm.png)], [max(round(dur * fps), 1)]
    else:
        bullets, frames = steps
        counts = reveal_frame_counts(bullet_times(bullets, sm.cues, dur), dur, fps)
        kept = [(f, n) for f, n in zip(frames, counts) if n > 0]
        frames, counts = [f for f, _ in kept], [n for _, n in kept]
    inputs: list[str] = []
    for f in frames:
        inputs += ["-loop", "1", "-framerate", str(fps), "-i", _rel(f, anchor)]
    return [
        "ffmpeg", "-y", *inputs,
        "-i", _rel(sm.mp3, anchor),
        "-filter_complex", _motion_filter(counts, w, h, fps),
        "-map", "[vout]", "-map", f"{len(frames)}:a",
        *_X264_ARGS,
        "-r", str(fps),
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
        "-t", f"{dur:.3f}",
        _rel(clip, anchor),
    ]


def build_scene_clips(
    scene_media: list[SceneMedia], clips_dir: Path, settings: Settings
) -> list[Path]:
    """每场景: 静态图 + mp3 → h264/aac/yuv420p clip(scale+pad 到目标分辨率)。

    之后用 ffprobe 精确时长回填 ``SceneMedia.duration``。
    """
    clips_dir = Path(clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)
    anchor = clips_dir.parent.resolve()  # 视为 out_dir, 输入输出均相对它传参
    (w, h), fps = settings.frame_size(), settings.fps
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
        if settings.video_motion:
            _run(_motion_clip_cmd(sm, clip, dur, anchor, settings), anchor, _timeout_for(dur))
            sm.duration = ffprobe_duration(clip)
            clips.append(clip)
            continue
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(fps), "-i", _rel(sm.png, anchor),
            "-i", _rel(sm.mp3, anchor),
            *_X264_ARGS,
            "-r", str(fps),
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
            "-vf", vf,
            "-t", f"{dur:.3f}",
            _rel(clip, anchor),
        ]
        _run(cmd, anchor, _timeout_for(dur))
        sm.duration = ffprobe_duration(clip)  # 回填精确值, 供后续 chapters/overlay 用
        clips.append(clip)
    return clips


def _write_concat_list(clips: list[Path], list_path: Path, anchor: Path) -> Path:
    """写 concat demuxer 列表文件: 每行 ``file '<相对 anchor 路径>'``。"""
    lines = [f"file '{_rel(c, anchor)}'" for c in clips]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


def _bars_by_clip(
    bars: list[SubtitleBar], durations: list[float]
) -> list[list[tuple[SubtitleBar, float, float]]]:
    """把全局字幕条分配到各 clip, 时间平移为 clip 本地; 跨边界的条按边界切开。"""
    out: list[list[tuple[SubtitleBar, float, float]]] = []
    offset = 0.0
    for dur in durations:
        clip_end = offset + dur
        slot: list[tuple[SubtitleBar, float, float]] = []
        for bar in bars:
            start, end = max(bar.start, offset), min(bar.end, clip_end)
            if end - start > _MIN_SLICE:
                slot.append((bar, start - offset, end - offset))
        out.append(slot)
        offset = clip_end
    return out


def _overlay_filter(slices: list[tuple[SubtitleBar, float, float]]) -> str:
    """单个 clip 的链式 overlay filter(每条一个 overlay, enable 限定本地时间窗), 出口 [vout]。"""
    if not slices:
        return "[0:v]format=yuv420p[vout]"
    parts: list[str] = []
    prev = "0:v"
    for i, (_, start, end) in enumerate(slices, start=1):
        label = "vout" if i == len(slices) else f"v{i}"
        chain = (
            f"[{prev}][{i}:v]overlay=0:0:enable='between(t,{_fmt_t(start)},{_fmt_t(end)})'"
        )
        chain += f",format=yuv420p[{label}]" if label == "vout" else f"[{label}]"
        parts.append(chain)
        prev = label
    return ";".join(parts)


def _burn_clip(
    clip: Path,
    slices: list[tuple[SubtitleBar, float, float]],
    dest: Path,
    duration: float,
    out_dir: Path,
) -> None:
    """单 clip 烧录字幕条: 视频按统一参数重编码, 音频直拷。"""
    inputs = ["-i", _rel(clip, out_dir)]
    for bar, _, _ in slices:
        inputs += ["-i", _rel(bar.png, out_dir)]
    _run(
        ["ffmpeg", "-y", *inputs,
         "-filter_complex", _overlay_filter(slices),
         "-map", "[vout]", "-map", "0:a",
         *_X264_ARGS,
         "-c:a", "copy",
         _rel(dest, out_dir)],
        out_dir,
        _timeout_for(duration),
    )


def finalize_video(
    clips: list[Path], bars: list[SubtitleBar], out_path: Path, settings: Settings
) -> Path:
    """有 bars 时逐 clip 烧录字幕条 → concat(-c copy) → 视频直拷 + loudnorm → final.mp4。

    每个 clip 只叠加落在自身时间窗内的字幕条(平移为本地时间), 与在整片上按全局时间
    overlay 等价; 所有 clip 统一重编码参数, 保证 concat 可直拷。
    """
    out_path = Path(out_path)
    out_dir = out_path.parent.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    durations = [ffprobe_duration(c) for c in clips]
    total = sum(durations)

    sources = list(clips)
    burn_dir = out_dir / "subbed"
    if bars:
        burn_dir.mkdir(parents=True, exist_ok=True)
        sources = []
        for clip, dur, slices in zip(clips, durations, _bars_by_clip(bars, durations)):
            dest = burn_dir / f"{Path(clip).stem}.mp4"
            _burn_clip(clip, slices, dest, dur, out_dir)
            sources.append(dest)

    # concat demuxer 出 base.mp4(不重编码)
    list_path = out_dir / "concat.list.txt"
    _write_concat_list(sources, list_path, out_dir)
    base = out_dir / "base.mp4"
    _run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path.name,
         "-c", "copy", base.name],
        out_dir,
        _timeout_for(total),
    )
    if bars:
        shutil.rmtree(burn_dir, ignore_errors=True)

    loudnorm = (
        f"loudnorm=I={settings.loudness_i}:TP={settings.loudness_tp}"
        f":LRA={settings.loudness_lra}"
    )
    bgm = settings.bgm_file
    if bgm is not None and not Path(bgm).is_file():
        logger.warning("BGM_FILE 不存在, 跳过背景音乐: %s", bgm)
        bgm = None
    if bgm is None:
        # 视频直拷, 仅音频响度归一(loudnorm 内部上采样到 192k, 输出钉回 44.1k)
        audio_args = ["-i", base.name, "-c:v", "copy", "-af", loudnorm]
    else:
        audio_args = [
            "-i", base.name, "-stream_loop", "-1", "-i", str(Path(bgm).resolve()),
            "-filter_complex", bgm_filter(total, settings, loudnorm),
            "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
        ]
    _run(
        ["ffmpeg", "-y", *audio_args,
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", out_path.name],
        out_dir,
        _timeout_for(total),
    )
    return out_path


def cleanup_intermediates(out_dir: Path, keep_clips: bool = False) -> None:
    """成片落盘后删除中间文件: base.mp4、concat.list.txt, 以及(除非 keep_clips)clips/。

    scenes/*.mp3 与 cue 缓存保留; clips/ 在 --resume 时由 clips 节点重新生成。
    """
    out_dir = Path(out_dir)
    for name in ("base.mp4", "concat.list.txt"):
        try:
            (out_dir / name).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理中间文件失败 %s: %s", name, exc)
    if not keep_clips:
        shutil.rmtree(out_dir / "clips", ignore_errors=True)


def bgm_filter(total: float, settings: Settings, loudnorm: str) -> str:
    """口播 [0:a] + 循环 BGM [1:a] → [aout]。

    BGM 截到成片时长、按 BGM_VOLUME_DB 压低、(可选)口播时 sidechain 闪避、结尾淡出;
    与口播混合后整体 loudnorm。
    """
    fade = min(_BGM_FADE_SEC, max(total, 0.0) / 4)
    fade_start = max(total - fade, 0.0)
    bg = (
        f"[1:a]{_AUDIO_FMT},atrim=0:{_fmt_t(total)},asetpts=PTS-STARTPTS,"
        f"volume={settings.bgm_volume_db}dB"
    )
    if settings.bgm_ducking:
        parts = [
            f"[0:a]{_AUDIO_FMT},asplit=2[voice][sc]",
            f"{bg}[bgraw]",
            "[bgraw][sc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[bgduck]",
            f"[bgduck]afade=t=out:st={_fmt_t(fade_start)}:d={_fmt_t(fade)}[bg]",
        ]
    else:
        parts = [
            f"[0:a]{_AUDIO_FMT}[voice]",
            f"{bg},afade=t=out:st={_fmt_t(fade_start)}:d={_fmt_t(fade)}[bg]",
        ]
    parts.append(f"[voice][bg]amix=inputs=2:duration=first:normalize=0,{loudnorm}[aout]")
    return ";".join(parts)


def build_chapters(script: Script, scene_media: list[SceneMedia]) -> list[Chapter]:
    """章节: 场景起点按精确 duration 累加, title 取 scene.header。"""
    chapters: list[Chapter] = []
    cursor = 0.0
    for i, (scene, sm) in enumerate(zip(script.scenes, scene_media)):
        chapters.append(Chapter(index=i, title=scene.header, start=cursor))
        cursor += sm.duration
    return chapters
