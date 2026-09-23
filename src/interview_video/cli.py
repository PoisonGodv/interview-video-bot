"""命令行入口: ``ivb topics / generate / upload / series / eval / web``。"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from .config import Settings, load_settings
from .graph import run_pipeline
from .logging_setup import setup_logging
from .models import ProjectManifest, Topic

app = typer.Typer(
    name="ivb",
    help="AI 面试求职短视频流水线: 选题 → 脚本 → TTS → 渲染 → 合成 → B站。",
    add_completion=False,
)
console = Console()


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
    n: int = typer.Option(8, "--n", help="生成选题条数"),
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
    industry: str = typer.Option("互联网", "--industry", "-i", help="目标行业"),
    topic: str | None = typer.Option(None, "--topic", "-t", help="选题标题提示"),
    script_file: Path | None = typer.Option(None, "--script-file", help="跳过 LLM, 直接用现成脚本 JSON"),
    voice: str | None = typer.Option(None, "--voice", help="edge-tts 音色, 如 zh-CN-XiaoxiaoNeural"),
    out: Path | None = typer.Option(None, "--out", "-o", help="输出目录(默认 output/<slug>-<YYYYmmdd-HHMM>/)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="BILI_UPLOAD_ENABLED 时跳过上传确认"),
) -> None:
    """跑完整流水线生成视频。--script-file 模式全程不需要 LLM key。"""
    settings = load_settings()
    if voice:
        settings.tts_voice = voice
    out_dir = Path(out) if out else _default_out_dir(settings, script_file, topic, industry)
    out_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]输出目录:[/] {out_dir}")

    manifest: ProjectManifest = asyncio.run(
        run_pipeline(
            settings,
            out_dir,
            industry=industry,
            topic_title=topic,
            script_file=script_file,
        )
    )

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
    episodes: int = typer.Option(3, "--episodes", "-e", help="集数"),
    duration: float = typer.Option(15.0, "--duration", "-d", help="每集目标时长(分钟)"),
    resume: Path | None = typer.Option(
        None, "--resume",
        help="断点续跑: 传上次系列的根目录(含 series.json), 已完成的集/章不重跑",
    ),
) -> None:
    """多 agent 系列长视频: 一个主题 → N 集 × M 分钟(策划/编剧/章节写手/审稿)。"""
    settings = load_settings()
    if resume is not None and not (resume / "series.json").exists():
        console.print(f"[red]--resume 目录缺 series.json: {resume}[/]")
        raise typer.Exit(1)
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
    console.print(
        Panel(f"共 {len(scripts)} 集 → {root}", title="系列生成完成", border_style="green")
    )
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
    from .config import load_settings as _ls

    try:  # 日志文件目录跟随 out_root; 失败退化为仅控制台
        setup_logging(_ls().out_root)
    except Exception:  # noqa: BLE001
        setup_logging()
    try:
        app()
    except (typer.Exit, typer.Abort):
        raise
    except Exception as exc:  # noqa: BLE001 —— 顶层兜底: 红字替代裸 traceback
        console.print(f"[bold red]失败:[/] {type(exc).__name__}: {exc}")
        console.print("[dim]详情见 output/ivb.log(或上方日志)[/]")
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    main()
