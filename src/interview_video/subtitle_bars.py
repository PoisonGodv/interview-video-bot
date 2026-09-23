"""字幕条渲染: 纯 Pillow, 不依赖浏览器。

按 SPECS §4: 每 cue 一张 1920x1080 透明 PNG, 底部居中白字黑描边,
可选半透明黑圆角底条; 字体按探测链找中文字体, 找不到抛带指引的 RuntimeError。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import Settings
from .models import Cue, SubtitleBar

# 系统字体探测链: (路径, 尝试的 ttc face index 列表)
# PingFang.ttc 含多个 face, 按 SPECS 在 index 0~5 中找可用的(PingFangSC)
_FONT_CANDIDATES: list[tuple[str, list[int]]] = [
    ("/System/Library/Fonts/PingFang.ttc", [0, 1, 2, 3, 4, 5]),
    ("/System/Library/Fonts/STHeiti Medium.ttc", [0]),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", [0]),
]

# 字幕描边宽度(像素, SPECS 要求 ≥3)
_STROKE_WIDTH = 3
# 底条内边距(像素)
_BOX_PADDING_X = 36
_BOX_PADDING_Y = 16
_BOX_RADIUS = 18
# 底条颜色: 半透明黑(RGBA 0,0,0,~120, SPECS 约定)
_BOX_COLOR = (0, 0, 0, 120)

_FONT_LOAD_ERR_GUIDE = (
    "未找到可用的中文字体。请设置环境变量 SUBTITLE_FONT_PATH 指向一个 .ttf/.ttc 中文字体, "
    "例如 SUBTITLE_FONT_PATH=/System/Library/Fonts/PingFang.ttc"
)


def load_subtitle_font(settings: Settings, fontsize: int) -> ImageFont.FreeTypeFont:
    """按探测链加载中文字体: settings.subtitle_font_path 优先, 其次系统探测链。"""
    # 1) 显式配置优先
    if settings.subtitle_font_path:
        path = Path(settings.subtitle_font_path)
        if not path.exists():
            raise RuntimeError(f"subtitle_font_path 不存在: {path}。{_FONT_LOAD_ERR_GUIDE}")
        try:
            return ImageFont.truetype(str(path), fontsize)
        except OSError as exc:  # 文件损坏/非字体
            raise RuntimeError(f"subtitle_font_path 无法加载: {path} ({exc})") from exc

    # 2) 系统探测链(带 face index 尝试)
    for path_str, indexes in _FONT_CANDIDATES:
        path = Path(path_str)
        if not path.exists():
            continue
        for index in indexes:
            try:
                return ImageFont.truetype(str(path), fontsize, index=index)
            except OSError:
                continue  # 该 face 不可用, 试下一个

    raise RuntimeError(_FONT_LOAD_ERR_GUIDE)


def _draw_bar(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    width: int,
    height: int,
    font: ImageFont.FreeTypeFont,
    bottom: int,
    with_box: bool,
) -> None:
    """在 (width x height) 画布上把文字画在距底 bottom 像素处(水平居中)。"""
    # 量出文字包围盒, 以包围盒中心做水平居中
    bbox = draw.textbbox(
        (0, 0), text, font=font, stroke_width=_STROKE_WIDTH
    )
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    center_x = width // 2
    # 文字整体(含描边)底边距底边 bottom 像素
    base_y = height - bottom - text_h
    # 以 "ms"(水平居中 / 基线对齐)为锚点, 补偿 bbox 的偏移
    anchor_x = center_x - bbox[0] - text_w // 2
    anchor_y = base_y - bbox[1]

    if with_box:
        box = (
            center_x - text_w // 2 - _BOX_PADDING_X,
            base_y - _BOX_PADDING_Y,
            center_x + text_w // 2 + _BOX_PADDING_X,
            base_y + text_h + _BOX_PADDING_Y,
        )
        draw.rounded_rectangle(box, radius=_BOX_RADIUS, fill=_BOX_COLOR)

    draw.text(
        (anchor_x, anchor_y),
        text,
        font=font,
        fill=(255, 255, 255, 255),          # 白色填充
        stroke_width=_STROKE_WIDTH,
        stroke_fill=(0, 0, 0, 255),         # 黑色描边
    )


def render_subtitle_bars(
    cues: list[Cue], subs_dir: Path, settings: Settings
) -> list[SubtitleBar]:
    """每 cue 渲染一张 1920x1080 透明 PNG 字幕条, 返回 SubtitleBar 列表。"""
    subs_dir = Path(subs_dir)
    subs_dir.mkdir(parents=True, exist_ok=True)

    width = settings.video_width
    height = settings.video_height
    font = load_subtitle_font(settings, settings.subtitle_fontsize)

    bars: list[SubtitleBar] = []
    for index, cue in enumerate(cues):
        # 透明底画布(RGBA)
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        _draw_bar(
            draw,
            cue.text,
            width=width,
            height=height,
            font=font,
            bottom=settings.subtitle_bottom,
            with_box=settings.subtitle_box,
        )
        png_path = subs_dir / f"bar_{index:03d}.png"
        image.save(png_path, format="PNG")
        bars.append(
            SubtitleBar(index=index, png=png_path, start=cue.start, end=cue.end, text=cue.text)
        )
    return bars
