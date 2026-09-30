"""命令行入口: ``ivb topics / generate / batch / upload / series / eval / web``。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from . import __version__
from .config import Settings, describe_paths, load_settings
from .estimate import SERIES_MAX_EPISODE_MIN, SERIES_MAX_EPISODES, SERIES_MIN_EPISODE_MIN
from .graph import STOP_AFTER_CHOICES, resume_industry, run_pipeline, run_script_stage
from .logging_setup import setup_logging
from .models import ProjectManifest, Topic

app = typer.Typer(
    name="ivb",
    help="AI 面试求职短视频流水线: 选题 → 脚本 → TTS → 渲染 → 合成 → B站。",
    add_completion=False,
)
console = Console()
logger = logging.getLogger(__name__)


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"ivb {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_cb, is_eager=True, help="显示版本并退出",
    ),
) -> None:
    """AI 面试求职短视频流水线: 选题 → 脚本 → TTS → 渲染 → 合成 → B站。"""


def _announce_paths(settings: Settings) -> None:
    line = describe_paths(settings)
    console.print(f"[dim]{line}[/]")
    logger.debug(line)


_TTS_HELP = "TTS 通道 edge|doubao(默认 .env 的 TTS_PROVIDER)"
_VOICE_HELP = "音色, 作用于当前 TTS 通道(edge 如 zh-CN-XiaoxiaoNeural, 豆包如 zh_female_vv_uranus_bigtts)"


def _apply_tts(settings: Settings, tts: str | None, voice: str | None) -> None:
    """--tts 切通道; --voice 写入当前通道对应的音色字段(豆包是 tts_doubao_voice)。"""
    if tts is not None:
        if tts not in ("edge", "doubao"):
            console.print("[red]--tts 仅支持: edge, doubao[/]")
            raise typer.Exit(2)
        settings.tts_provider = tts
    if voice:
        if settings.tts_provider == "doubao":
            settings.tts_doubao_voice = voice
        else:
            settings.tts_voice = voice


_ASPECT_HELP = "画幅 16:9(横屏, 默认 .env 的 VIDEO_ASPECT) | 9:16(竖屏 1080x1920)"
_BGM_HELP = "背景音乐文件(循环铺满、口播时自动压低、结尾淡出); 默认 .env 的 BGM_FILE"
_PUBLISH_AT_HELP = "B站定时发布(本地时间 'YYYY-mm-dd HH:MM', 须在 2 小时后~15 天内)"


def _apply_video(settings: Settings, aspect: str | None, bgm: Path | None) -> None:
    """--aspect 切画幅; --bgm 指定背景音乐(文件须存在)。"""
    if aspect is not None:
        if aspect not in ("16:9", "9:16"):
            console.print("[red]--aspect 仅支持: 16:9, 9:16[/]")
            raise typer.Exit(2)
        settings.video_aspect = aspect
    if bgm is not None:
        bgm = Path(bgm).expanduser()
        if not bgm.is_file():
            console.print(f"[red]--bgm 文件不存在: {bgm}[/]")
            raise typer.Exit(2)
        settings.bgm_file = bgm


def _parse_publish_at(value: str | None) -> int | None:
    if value is None:
        return None
    from .bilibili import parse_publish_at

    try:
        return parse_publish_at(value)
    except ValueError as exc:
        console.print(f"[red]--publish-at: {exc}[/]")
        raise typer.Exit(2) from None


def _upload(out_dir: Path, settings: Settings, force: bool, dtime: int | None = None) -> None:
    from .bilibili import AlreadyUploadedError, upload_project

    try:
        bvid = asyncio.run(upload_project(out_dir, settings, force=force, dtime=dtime))
    except AlreadyUploadedError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1) from None
    console.print(f"[bold green]上传成功[/]: https://www.bilibili.com/video/{bvid}")
    if dtime is not None:
        when = datetime.fromtimestamp(dtime).strftime("%Y-%m-%d %H:%M")
        console.print(f"[cyan]定时发布[/]: {when}(审核通过后按时公开)")


def _usage_line(out_dir: Path) -> str | None:
    from .llm_audit import summarize_usage

    u = summarize_usage(out_dir)
    if not u["calls"]:
        return None
    return (
        f"LLM: {u['calls']} 次调用, tokens 入 {u['input_tokens']} / 出 {u['output_tokens']}"
        f" / 共 {u['total_tokens']}(明细 {Path(out_dir) / 'llm_calls.jsonl'})"
    )


def _slug(text: str) -> str:
    """把标题/行业转成目录名: 保留中英文与数字, 其余折叠成 '-'。"""
    slug = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", text).strip("-").lower()
    return slug or "video"


def _default_out_dir(settings: Settings, script_file: Path | None, topic: str | None, industry: str) -> Path:
    """未给 --out 时自动生成 output/<slug>-<YYYYmmdd-HHMM>/。"""
    base = topic or ""
    if not base and script_file is not None:
        try:
            title = json.loads(Path(script_file).read_text(encoding="utf-8")).get("title", "")
            base = str(title)
        except (OSError, json.JSONDecodeError):
            base = ""
    if not base:
        base = industry
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return settings.out_root / f"{_slug(base)}-{stamp}"


@app.command()
def topics(
    industry: str | None = typer.Option(
        None, "--industry", "-i", help="目标行业, 如 护理/互联网/汽修(生成时默认 互联网; --list/--next 不给则不限行业)",
    ),
    n: int = typer.Option(8, "--n", "-n", help="生成选题条数"),
    list_: bool = typer.Option(False, "--list", help="列出选题库(不调 LLM)"),
    show_all: bool = typer.Option(False, "--all", help="--list 时包含已用选题"),
    next_: bool = typer.Option(False, "--next", help="打印选题库中下一条未用选题(不调 LLM)"),
) -> None:
    """生成选题清单(需 LLM), 存 output/topics-<slug>-<时间戳>.json 并入选题库。

    选题库 output/topics_backlog.json: 已有标题会作为「需避开」传给 LLM, 生成视频后自动标记已用。
    """
    settings = load_settings()
    from . import backlog as backlog_mod

    if list_ or next_:
        if next_:
            items = backlog_mod.pending(settings, industry)
            if not items:
                console.print("[yellow]选题库没有未用选题, 先运行 ivb topics 生成[/]")
                raise typer.Exit(1)
            t = items[0]
            console.print(f"{t.title}  [dim]{t.industry} · {t.hook}[/]")
            console.print(f"[dim]生成: ivb generate -i {t.industry} -t '{t.title}'[/]")
            return
        items = [
            i for i in backlog_mod.load_backlog(settings)
            if (industry is None or i.industry == industry) and (show_all or i.status == "new")
        ]
        console.print(f"[bold]选题库[/] {backlog_mod.backlog_path(settings)}  共 {len(items)} 条")
        for i, t in enumerate(items, 1):
            mark = "[green]新[/]" if t.status == "new" else "[dim]已用[/]"
            console.print(f"  {i:>3}. {mark} [{t.industry}] {t.title}")
        return

    industry = industry or "互联网"
    from .chains import topic as topic_mod
    from .llm import get_chat_model

    llm = get_chat_model(settings)  # 未配 key 直接抛错, 不静默失败
    avoid = backlog_mod.avoid_titles(settings, industry)
    result: list[Topic] = asyncio.run(
        topic_mod.generate_topics(llm, industry, n, avoid=avoid or None)
    )
    settings.out_root.mkdir(parents=True, exist_ok=True)
    path = settings.out_root / f"topics-{_slug(industry)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(
        json.dumps([t.model_dump() for t in result], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    added = backlog_mod.add_topics(settings, result)
    console.print(
        f"[bold green]已生成 {len(result)} 条选题[/] → {path}"
        f"  [dim](新入选题库 {len(added)} 条, 避开已有 {len(avoid)} 条)[/]"
    )
    for i, t in enumerate(result, 1):
        console.print(f"  {i}. {t.title}  [dim]{t.hook}[/]")


@app.command()
def generate(
    industry: str | None = typer.Option(
        None, "--industry", "-i", help="目标行业(默认 互联网; --resume 时默认沿用原目录记录)",
    ),
    topic: str | None = typer.Option(None, "--topic", "-t", help="选题标题提示"),
    script_file: Path | None = typer.Option(None, "--script-file", help="跳过 LLM, 直接用现成脚本 JSON"),
    tts: str | None = typer.Option(None, "--tts", help=_TTS_HELP),
    voice: str | None = typer.Option(None, "--voice", help=_VOICE_HELP),
    out: Path | None = typer.Option(None, "--out", "-o", help="输出目录(默认 output/<slug>-<YYYYmmdd-HHMM>/)"),
    resume: Path | None = typer.Option(
        None, "--resume",
        help="断点续跑: 传上次的输出目录, 复用其 script.json(不再调 LLM)与 TTS 缓存",
    ),
    stop_after: str | None = typer.Option(
        None, "--stop-after",
        help="只跑到指定阶段后停下; script=只生成 script.json, 人工审改后用 --resume 续跑",
    ),
    aspect: str | None = typer.Option(None, "--aspect", help=_ASPECT_HELP),
    bgm: Path | None = typer.Option(None, "--bgm", help=_BGM_HELP),
    publish_at: str | None = typer.Option(None, "--publish-at", help=_PUBLISH_AT_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="BILI_UPLOAD_ENABLED 时跳过上传确认"),
) -> None:
    """跑完整流水线生成视频。--script-file 模式全程不需要 LLM key。"""
    if stop_after is not None and stop_after not in STOP_AFTER_CHOICES:
        console.print(f"[red]--stop-after 仅支持: {', '.join(STOP_AFTER_CHOICES)}[/]")
        raise typer.Exit(2)
    if stop_after is not None and resume is not None:
        console.print("[red]--stop-after 与 --resume 不能同时使用[/]")
        raise typer.Exit(2)
    dtime = _parse_publish_at(publish_at)  # 先校验, 不合法时不白跑流水线
    settings = load_settings()
    _apply_tts(settings, tts, voice)
    _apply_video(settings, aspect, bgm)
    _announce_paths(settings)
    preset = None
    if resume is not None:
        out_dir = Path(resume)
        script_json = out_dir / "script.json"
        if script_json.exists():
            from .models import Script

            preset = Script.model_validate_json(script_json.read_text(encoding="utf-8"))
            console.print(f"[yellow]断点续跑[/]: 复用 {script_json}")
        elif not out_dir.is_dir():
            console.print(f"[red]--resume 目录不存在: {out_dir}[/]")
            raise typer.Exit(1)
        else:
            console.print(f"[yellow]断点续跑[/]: {out_dir} 无 script.json, 重新生成脚本")
    else:
        out_dir = Path(out) if out else _default_out_dir(settings, script_file, topic, industry or "互联网")
    industry = industry or (resume_industry(out_dir) if resume is not None else None) or "互联网"
    out_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]输出目录:[/] {out_dir}")

    if stop_after == "script":
        script = asyncio.run(
            run_script_stage(
                settings, out_dir, industry=industry,
                topic_title=topic, script_file=script_file,
            )
        )
        console.print(
            Panel(
                f"脚本: {out_dir / 'script.json'}  ({script.title}, {len(script.scenes)} 个场景)\n"
                f"审改后继续生成视频:\n  ivb generate --resume {out_dir}",
                title="脚本已生成(已停在配音前)", border_style="yellow",
            )
        )
        return

    try:
        manifest: ProjectManifest = asyncio.run(
            run_pipeline(
                settings,
                out_dir,
                industry=industry,
                topic_title=topic,
                script_file=script_file,
                script=preset,
            )
        )
    except Exception:
        if (out_dir / "script.json").exists():
            console.print(
                f"[yellow]脚本已保存, 修复问题后可续跑(不再付 LLM 费用):[/] "
                f"ivb generate --resume {out_dir}"
            )
        raise

    # rich 打印产物清单
    lines = [
        f"视频: {manifest.video}",
        f"封面: {manifest.cover}",
        f"字幕: {manifest.srt or '(无)'}",
        f"总时长: {manifest.duration_total:.1f}s  音色: {manifest.voice}",
    ]
    if manifest.meta is not None:
        lines += [
            "",
            "[bold cyan]BiliMeta[/]",
            f"标题: {manifest.meta.title}",
            f"标签: {' '.join('#' + t for t in manifest.meta.tags)}",
        ]
    else:
        lines += ["", "[yellow]BiliMeta: 未生成(无 LLM key, 已降级)[/]"]
    usage = _usage_line(out_dir)
    if usage:
        lines += ["", usage]
    console.print(Panel("\n".join(lines), title="生成完成", border_style="green"))

    if settings.bili_upload_enabled:
        if yes or typer.confirm("检测到 BILI_UPLOAD_ENABLED=true, 现在上传到B站?"):
            _upload(out_dir, settings, force=False, dtime=dtime)
    elif dtime is not None:
        console.print(
            f"[yellow]未开启 BILI_UPLOAD_ENABLED, 未上传; 稍后可运行:[/] "
            f"ivb upload {out_dir} --publish-at '{publish_at}'"
        )


@app.command()
def batch(
    industry: str | None = typer.Option(None, "--industry", "-i", help="只取该行业的选题(默认不限)"),
    n: int = typer.Option(3, "--n", "-n", min=1, help="本批生成条数(取选题库前 N 条未用选题)"),
    tts: str | None = typer.Option(None, "--tts", help=_TTS_HELP),
    voice: str | None = typer.Option(None, "--voice", help=_VOICE_HELP),
    aspect: str | None = typer.Option(None, "--aspect", help=_ASPECT_HELP),
    bgm: Path | None = typer.Option(None, "--bgm", help=_BGM_HELP),
) -> None:
    """批量生产: 按选题库顺序逐条生成视频; 单条失败记录后继续, 结束打印汇总表。"""
    from rich.table import Table

    from . import backlog as backlog_mod

    settings = load_settings()
    _apply_tts(settings, tts, voice)
    _apply_video(settings, aspect, bgm)
    _announce_paths(settings)
    items = backlog_mod.pending(settings, industry)[:n]
    if not items:
        console.print("[yellow]选题库没有未用选题, 先运行 ivb topics -i <行业> 生成[/]")
        raise typer.Exit(1)
    console.print(f"[bold]批量生成 {len(items)} 条[/](选题库剩余 {len(backlog_mod.pending(settings, industry))} 条)")

    rows: list[tuple[str, str, str, str]] = []
    for idx, item in enumerate(items, 1):
        out_dir = _default_out_dir(settings, None, item.title, item.industry)
        console.print(f"[bold cyan]({idx}/{len(items)})[/] {item.title} → {out_dir}")
        try:
            manifest = asyncio.run(run_pipeline(
                settings, out_dir, industry=item.industry, topic_title=item.title,
            ))
            rows.append((item.title, "成功", f"{manifest.duration_total:.0f}s", str(out_dir)))
        except KeyboardInterrupt:
            rows.append((item.title, "中断", "", str(out_dir)))
            break
        except Exception as exc:  # noqa: BLE001 —— 单条失败不影响后续
            logger.exception("批量生成失败: %s", item.title)
            rows.append((item.title, "失败", f"{type(exc).__name__}: {exc}"[:80], str(out_dir)))

    table = Table(title="批量生成汇总")
    for col in ("选题", "结果", "时长/原因", "目录"):
        table.add_column(col)
    for title, status, detail, path in rows:
        color = "green" if status == "成功" else "red"
        table.add_row(title, f"[{color}]{status}[/]", detail, path)
    console.print(table)
    failed = sum(1 for r in rows if r[1] != "成功")
    if failed:
        console.print(
            f"[yellow]{failed} 条未完成; 已有 script.json 的可 ivb generate --resume <目录> 续跑[/]"
        )
        raise typer.Exit(1)


@app.command()
def upload(
    project_dir: Path = typer.Argument(..., help="含 project.json 的产物目录"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    force: bool = typer.Option(False, "--force", help="已有 upload.json(bvid) 时仍重新投稿"),
    publish_at: str | None = typer.Option(None, "--publish-at", help=_PUBLISH_AT_HELP),
) -> None:
    """读 project.json 上传 B站(需 BILI_UPLOAD_ENABLED + cookie 三件套); 成功后写 upload.json。"""
    dtime = _parse_publish_at(publish_at)
    settings = load_settings()
    if not yes:
        typer.confirm(f"确认上传 {project_dir} 到B站?", abort=True)
    _upload(project_dir, settings, force=force, dtime=dtime)
    console.print(
        "[yellow]合规提醒: 发布后请到创作中心为该稿件补勾\"AI生成\"标识。[/]"
    )


@app.command()
def series(
    theme: str | None = typer.Argument(
        None, help="系列主题, 如 程序员面试通关(--resume 时可省略, 沿用 series.json)",
    ),
    episodes: int = typer.Option(
        3, "--episodes", "-e", help=f"集数(1~{SERIES_MAX_EPISODES})",
    ),
    duration: float = typer.Option(
        15.0, "--duration", "-d",
        help=f"每集目标时长(分钟, {SERIES_MIN_EPISODE_MIN:g}~{SERIES_MAX_EPISODE_MIN:g})",
    ),
    resume: Path | None = typer.Option(
        None, "--resume",
        help="断点续跑: 传上次系列的根目录(含 series.json), 已完成的集/章不重跑",
    ),
    tts: str | None = typer.Option(None, "--tts", help=_TTS_HELP),
    voice: str | None = typer.Option(None, "--voice", help=_VOICE_HELP),
    aspect: str | None = typer.Option(None, "--aspect", help=_ASPECT_HELP),
    bgm: Path | None = typer.Option(None, "--bgm", help=_BGM_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过费用预估确认"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印费用预估, 不调用 LLM/TTS"),
) -> None:
    """多 agent 系列长视频: 一个主题 → N 集 × M 分钟(策划/编剧/章节写手/审稿)。"""
    settings = load_settings()
    _apply_tts(settings, tts, voice)
    _apply_video(settings, aspect, bgm)
    _announce_paths(settings)
    if resume is not None:
        theme = _resume_series_theme(resume, theme)
    elif not theme:
        console.print("[red]缺少系列主题: ivb series <主题>, 或用 --resume <目录> 续跑[/]")
        raise typer.Exit(2)
    if resume is None:
        from .estimate import estimate_series, validate_series_bounds

        err = validate_series_bounds(episodes, duration)
        if err:
            console.print(f"[red]{err}[/]")
            raise typer.Exit(2)
        est = estimate_series(settings, episodes, duration)
        console.print(Panel("\n".join(est.lines()), title="费用预估(粗估)", border_style="cyan"))
        if dry_run:
            return
        if not yes and not typer.confirm("按以上预估开始生成?"):
            raise typer.Abort()
    elif dry_run:
        console.print("[yellow]--dry-run 仅对新系列生成预估, 续跑无需预估[/]")
        return
    from .doctor import preflight

    preflight(settings)
    asyncio.run(_series_main(settings, theme, episodes, duration, resume_dir=resume))


def _resume_series_theme(resume: Path, theme: str | None) -> str:
    """--resume 时从 series.json 读取主题; 命令行给了主题则以命令行为准。"""
    plan_file = resume / "series.json"
    if not plan_file.exists():
        console.print(f"[red]--resume 目录缺 series.json: {resume}[/]")
        raise typer.Exit(1)
    if theme:
        return theme
    try:
        saved = json.loads(plan_file.read_text(encoding="utf-8")).get("theme")
    except (OSError, json.JSONDecodeError, AttributeError):
        saved = None
    if not saved:
        console.print(f"[red]series.json 缺少 theme, 请显式传入主题: ivb series <主题> --resume {resume}[/]")
        raise typer.Exit(1)
    return str(saved)


async def _series_main(
    settings: Settings,
    theme: str,
    episodes: int,
    duration_min: float,
    resume_dir: Path | None = None,
) -> Path:
    """系列生成主流程: 多 agent 出脚本 → 逐集走媒体流水线。

    根目录在调用 LLM 前创建, 每章/每集完成即增量落盘; 失败后可用
    ``--resume <root>`` 续跑(已完成集的媒体产物按 project.json 跳过)。
    """
    from .series import generate_series

    def on_event(name: str, **p) -> None:
        if name == "plan_done":
            plan = p["plan"]
            console.print(f"[bold cyan]系列策划[/]《{plan.title}》共 {len(plan.episodes)} 集:")
            for ep in plan.episodes:
                console.print(f"  {ep.index}. {ep.title} [dim]{ep.summary[:40]}[/]")
        elif name == "outline_done":
            console.print(
                f"[dim]第 {p['ep_idx'] + 1} 集大纲: {len(p['outline'].chapters)} 章[/]"
            )
        elif name == "chapter_done":
            console.print(
                f"[dim]  第 {p['ep_idx'] + 1} 集 第 {p['ch_idx'] + 1}/{p['ch_total']} 章完成[/]"
            )
        elif name == "script_done":
            console.print(
                f"[green]第 {p['ep_idx'] + 1} 集脚本完成({len(p['script'].scenes)} 镜)[/]"
            )

    if resume_dir is not None:
        root = Path(resume_dir)
        console.print(f"[yellow]断点续跑[/] → {root}")
    else:
        root = (
            settings.out_root
            / f"{_slug(theme)}-series-{datetime.now().strftime('%Y%m%d-%H%M')}"
        )
    root.mkdir(parents=True, exist_ok=True)
    from .llm_audit import run_budget

    try:
        with run_budget(settings):  # 整个系列(脚本+各集元数据)共用一个 LLM 硬上限计数器
            plan, scripts = await generate_series(
                settings, theme, episodes=episodes,
                episode_sec=int(duration_min * 60), on_event=on_event,
                run_dir=root, resume=resume_dir is not None,
            )
            for i, script in enumerate(scripts):
                out_dir = root / f"ep{i + 1:02d}-{_slug(script.title)}"
                if (out_dir / "project.json").exists():
                    console.print(f"[yellow]第 {i + 1} 集已有成片, 跳过[/] → {out_dir}")
                    continue
                console.print(f"[bold]第 {i + 1}/{len(scripts)} 集媒体合成[/] → {out_dir}")
                await run_pipeline(settings, out_dir, industry=theme, script=script)
    except Exception:
        if (root / "series.json").exists():
            console.print(
                f"[yellow]已完成的集/章已落盘, 修复问题后续跑:[/] ivb series --resume {root}"
            )
        raise
    body = f"共 {len(scripts)} 集 → {root}"
    usage = _usage_line(root)
    if usage:
        body += f"\n策划/编剧 {usage}"
    console.print(Panel(body, title="系列生成完成", border_style="green"))
    return root


@app.command()
def eval(
    limit: int = typer.Option(3, "--limit", "-l", help="评测用例数(截取 golden 集)"),
    judge: bool = typer.Option(True, "--judge/--no-judge", help="是否跑 LLM-as-judge 打分"),
    out: Path | None = typer.Option(None, "--out", "-o", help="报告 JSON 落盘路径"),
) -> None:
    """生成质量评测: 真实模型跑 golden 用例 → 契约/红线检查(+可选 judge)。"""
    settings = load_settings()
    from .evals import GOLDEN_CASES, run_evals

    report = asyncio.run(run_evals(settings, GOLDEN_CASES[:limit], judge=judge))
    for r in report["results"]:
        status = "OK" if not (r.get("issues") or r.get("fatal")) else "FAIL"
        console.print(
            f"[{'green' if status == 'OK' else 'red'}]{status}[/] "
            f"{r['industry']}/{r['topic']}"
            + (f" [dim]{'; '.join(r.get('issues') or [r.get('fatal', '')])}[/]" if status == "FAIL" else "")
        )
    console.print(
        f"评测完成: {report['total'] - report['failed']}/{report['total']} 通过"
        f"(模型 {report['llm_model']}, judge={'on' if report['judge'] else 'off'})"
    )
    if out is not None:
        out.write_text(report["report_json"], encoding="utf-8")
        console.print(f"报告 → {out}")
    if report["failed"]:
        raise typer.Exit(1)


@app.command()
def doctor(
    ping: bool = typer.Option(False, "--ping", help="额外发一次极小 LLM 请求验证 key/base_url"),
) -> None:
    """环境自检: ffmpeg/ffprobe、chromium、中文字体、.env、LLM/TTS/B站配置。"""
    from . import doctor as doctor_mod

    settings = load_settings()
    checks = doctor_mod.run_doctor(settings)
    if ping and settings.llm_ready():
        checks.append(asyncio.run(doctor_mod.ping_llm(settings)))
    for c in checks:
        mark = "[green]OK[/]" if c.ok else ("[red]FAIL[/]" if c.required else "[yellow]WARN[/]")
        console.print(f"{mark:<18} {c.name:<14} [dim]{c.detail}[/]")
    if any(c.required and not c.ok for c in checks):
        raise typer.Exit(1)
    console.print("[bold green]必需项全部通过[/]")


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址(本地工具, 默认仅回环)"),
    port: int = typer.Option(8099, "--port", "-p", help="端口"),
) -> None:
    """启动 Web 操作台(选题/生成进度/作品库/元数据编辑/上传)。"""
    import uvicorn

    from .web import create_app

    settings = load_settings()
    _announce_paths(settings)
    web_app = create_app(settings, host=host)
    url = f"http://{host}:{port}/"
    token = web_app.state.web_token
    if token:
        url += f"#token={token}"
        console.print("[yellow]写操作需令牌(X-IVB-Token), 请用下方带 #token= 的链接打开; 固定令牌可设 WEB_TOKEN[/]")
    console.print(f"[bold green]Web 操作台[/] → {url}")
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


def main() -> None:
    """console_scripts 入口(ivb = interview_video.cli:main)。"""
    log_hint = "上方日志"
    try:  # 日志文件目录跟随 out_root; 失败退化为仅控制台
        out_root = load_settings().out_root
        setup_logging(out_root)
        log_hint = str(out_root / "ivb.log")
    except Exception:  # noqa: BLE001
        setup_logging()
    try:
        app()  # standalone: 用法错误/Exit/Abort 由 click 自行处理并 sys.exit
    except Exception as exc:  # noqa: BLE001 —— 顶层兜底: 红字替代裸 traceback
        if os.environ.get("IVB_DEBUG"):
            raise
        logger.debug("顶层异常", exc_info=exc)
        console.print(f"[bold red]失败:[/] {type(exc).__name__}: {exc}")
        console.print(f"[dim]完整堆栈见 {log_hint}(或设 IVB_DEBUG=1 直接打印)[/]")
        sys.exit(1)


if __name__ == "__main__":
    main()
