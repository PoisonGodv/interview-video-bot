"""命令行入口: ``ivb topics / generate / upload / series / eval / web``。"""
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
from .config import Settings, load_settings
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
    industry: str = typer.Option("互联网", "--industry", "-i", help="目标行业, 如 护理/互联网/汽修"),
    n: int = typer.Option(8, "--n", "-n", help="生成选题条数"),
) -> None:
    """生成选题清单(需 LLM), 存 output/topics-<slug>-<时间戳>.json。"""
    settings = load_settings()
    from .chains import topic as topic_mod
    from .llm import get_chat_model

    llm = get_chat_model(settings)  # 未配 key 直接抛错, 不静默失败
    result: list[Topic] = asyncio.run(topic_mod.generate_topics(llm, industry, n))
    settings.out_root.mkdir(parents=True, exist_ok=True)
    path = settings.out_root / f"topics-{_slug(industry)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(
        json.dumps([t.model_dump() for t in result], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    console.print(f"[bold green]已生成 {len(result)} 条选题[/] → {path}")
    for i, t in enumerate(result, 1):
        console.print(f"  {i}. {t.title}  [dim]{t.hook}[/]")


@app.command()
def generate(
    industry: str | None = typer.Option(
        None, "--industry", "-i", help="目标行业(默认 互联网; --resume 时默认沿用原目录记录)",
    ),
    topic: str | None = typer.Option(None, "--topic", "-t", help="选题标题提示"),
    script_file: Path | None = typer.Option(None, "--script-file", help="跳过 LLM, 直接用现成脚本 JSON"),
    voice: str | None = typer.Option(None, "--voice", help="edge-tts 音色, 如 zh-CN-XiaoxiaoNeural"),
    out: Path | None = typer.Option(None, "--out", "-o", help="输出目录(默认 output/<slug>-<YYYYmmdd-HHMM>/)"),
    resume: Path | None = typer.Option(
        None, "--resume",
        help="断点续跑: 传上次的输出目录, 复用其 script.json(不再调 LLM)与 TTS 缓存",
    ),
    stop_after: str | None = typer.Option(
        None, "--stop-after",
        help="只跑到指定阶段后停下; script=只生成 script.json, 人工审改后用 --resume 续跑",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="BILI_UPLOAD_ENABLED 时跳过上传确认"),
) -> None:
    """跑完整流水线生成视频。--script-file 模式全程不需要 LLM key。"""
    if stop_after is not None and stop_after not in STOP_AFTER_CHOICES:
        console.print(f"[red]--stop-after 仅支持: {', '.join(STOP_AFTER_CHOICES)}[/]")
        raise typer.Exit(2)
    if stop_after is not None and resume is not None:
        console.print("[red]--stop-after 与 --resume 不能同时使用[/]")
        raise typer.Exit(2)
    settings = load_settings()
    if voice:
        settings.tts_voice = voice
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
            from .bilibili import upload_project

            bvid = asyncio.run(upload_project(out_dir, settings))
            console.print(f"[bold green]上传成功[/]: https://www.bilibili.com/video/{bvid}")


@app.command()
def upload(
    project_dir: Path = typer.Argument(..., help="含 project.json 的产物目录"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
) -> None:
    """读 project.json 上传 B站(需 BILI_UPLOAD_ENABLED + cookie 三件套)。"""
    settings = load_settings()
    if not yes:
        typer.confirm(f"确认上传 {project_dir} 到B站?", abort=True)
    from .bilibili import upload_project

    bvid = asyncio.run(upload_project(project_dir, settings))
    console.print(f"[bold green]上传成功[/]: https://www.bilibili.com/video/{bvid}")
    console.print(
        "[yellow]合规提醒: 发布后请到创作中心为该稿件补勾\"AI生成\"标识。[/]"
    )


@app.command()
def series(
    theme: str = typer.Argument(..., help="系列主题, 如 程序员面试通关"),
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
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过费用预估确认"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印费用预估, 不调用 LLM/TTS"),
) -> None:
    """多 agent 系列长视频: 一个主题 → N 集 × M 分钟(策划/编剧/章节写手/审稿)。"""
    settings = load_settings()
    if resume is not None and not (resume / "series.json").exists():
        console.print(f"[red]--resume 目录缺 series.json: {resume}[/]")
        raise typer.Exit(1)
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
                f"[yellow]已完成的集/章已落盘, 修复问题后续跑:[/] ivb series '{theme}' --resume {root}"
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

    console.print(f"[bold green]Web 操作台[/] → http://{host}:{port}")
    uvicorn.run(create_app(load_settings()), host=host, port=port, log_level="warning")


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
