"""命令行入口: ``ivb topics / generate / upload``(console_scripts 需要 main 包装)。"""
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
    app()


if __name__ == "__main__":
    main()
