"""画面渲染: Jinja2 模板 → Playwright(chromium)截图 PNG。

按 SPECS §4: viewport 与 settings.frame_size() 一致(默认 1920x1080, VIDEO_ASPECT=9:16 时 1080x1920),
kind→模板映射 point→scene.html / qa→qa.html / ending→ending.html,
产出 scenes/scene_%03d.png + cover.png, 返回 RenderResult。
任务取消时在帧间检查点中止, 并确保关闭 chromium。
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from . import procs
from .config import Settings
from .models import RenderResult, Script

# 模板目录(随包发布, 见 pyproject package-data)
TEMPLATES_DIR = Path(__file__).parent / "templates"

# kind → 模板文件名
KIND_TEMPLATES = {
    "point": "scene.html",
    "qa": "qa.html",
    "ending": "ending.html",
}

# 视觉规范点缀色(SPECS §4)
ACCENT = "#38bdf8"

# 封面标题字号候选(大→小)与左右留白: 按标题长度挑能在 _COVER_MAX_LINES 行内放下的最大字号
_COVER_TITLE_SIZES = (108, 96, 88, 80, 72, 64, 56)
_COVER_MAX_LINES = 3
_PAGE_PAD_X = {False: 130, True: 72}


def _visual_len(text: str) -> float:
    """按显示宽度估字数: 全角字符记 1, ASCII 记 0.55。"""
    return sum(0.55 if ord(ch) < 128 else 1.0 for ch in text)


def cover_title_px(title: str, width: int = 1920, portrait: bool = False) -> int:
    """封面标题自适应字号: 长标题逐级缩小, 保证不超过 3 行(横竖屏通用)。"""
    content_w = width - 2 * _PAGE_PAD_X[portrait]
    n = max(_visual_len(title.strip()), 1.0)
    for px in _COVER_TITLE_SIZES:
        per_line = content_w / (px * 1.02)  # letter-spacing 2px 近似
        if n / per_line <= _COVER_MAX_LINES:
            return px
    return _COVER_TITLE_SIZES[-1]


def _build_env() -> Environment:
    """Jinja2 环境: 从包内 templates/ 加载, HTML 自动转义。"""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render_html(
    env: Environment,
    template_name: str,
    *,
    brand: str,
    title: str,
    script: Script,
    scene,
    index: int,
    total: int,
    industry: str = "",
    width: int = 1920,
    height: int = 1080,
) -> str:
    """渲染单个模板为 HTML 字符串(纯函数, 便于单测)。竖屏由 width < height 推断。"""
    template = env.get_template(template_name)
    portrait = width < height
    return template.render(
        brand=brand,
        title=title,
        script=script,
        scene=scene,
        index=index,
        total=total,
        accent=ACCENT,
        industry=industry,
        width=width,
        height=height,
        portrait=portrait,
        title_px=cover_title_px(title, width, portrait),
    )


def _screenshot_html(page, html: str, path: Path) -> None:
    """把 HTML 载入页面并截图, 等待字体就绪保证中文渲染完整。"""
    page.set_content(html, wait_until="load")
    # 等待 webfont/系统字体加载完成, 避免中文缺字截图
    page.evaluate("document.fonts.ready.then(() => true)")
    page.screenshot(path=str(path), full_page=False)


def render_project(
    script: Script,
    out_dir: Path,
    settings: Settings,
    industry: str = "",
) -> RenderResult:
    """渲染封面 + 每场景一帧 PNG。

    - scenes/scene_%03d.png 与 script.scenes 一一对应(有序)
    - cover.png 为封面(大标题 + hook + 行业 chip + brand)
    - industry 为可选行业词(封面 chip); Script 模型无 industry 字段,
      由调用方(如 graph.py, 其 PipelineState 里有 industry)传入
    """
    out_dir = Path(out_dir)
    scenes_dir = out_dir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    env = _build_env()
    width, height = settings.frame_size()
    total = len(script.scenes)

    frames: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            # 复用同一页面顺序截图, 避免每场景开关浏览器
            for index, scene in enumerate(script.scenes):
                procs.checkpoint()  # 任务取消 → 抛出, finally 关浏览器
                template_name = KIND_TEMPLATES[scene.kind]
                html = _render_html(
                    env,
                    template_name,
                    brand=settings.brand,
                    title=script.title,
                    script=script,
                    scene=scene,
                    index=index,
                    total=total,
                    industry=industry,
                    width=width,
                    height=height,
                )
                frame_path = scenes_dir / f"scene_{index:03d}.png"
                _screenshot_html(page, html, frame_path)
                frames.append(frame_path)

            # 封面帧
            procs.checkpoint()
            cover_html = _render_html(
                env,
                "cover.html",
                brand=settings.brand,
                title=script.title,
                script=script,
                scene=None,
                index=0,
                total=total,
                industry=industry,
                width=width,
                height=height,
            )
            cover_path = out_dir / "cover.png"
            _screenshot_html(page, cover_html, cover_path)
        finally:
            browser.close()

    return RenderResult(cover=cover_path, frames=frames)
