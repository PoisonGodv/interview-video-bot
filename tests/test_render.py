"""render.py / subtitle_bars.py 测试。

- Jinja2 渲染 HTML 字符串断言(不启浏览器)
- Pillow 字幕条生成(探测到字体才跑, 否则 skip)
- Playwright 截图打 integration 标记(渲染 examples/sample_script.json 3 场景)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from interview_video.config import Settings
from interview_video.models import Cue, Script
from interview_video.render import ACCENT, KIND_TEMPLATES, _build_env, _render_html
from interview_video.subtitle_bars import load_subtitle_font, render_subtitle_bars

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_SCRIPT = PROJECT_ROOT / "examples" / "sample_script.json"


@pytest.fixture()
def script() -> Script:
    return Script.model_validate_json(SAMPLE_SCRIPT.read_text(encoding="utf-8"))


@pytest.fixture()
def settings() -> Settings:
    return Settings()


# ---------- Jinja2 HTML 字符串渲染(不启浏览器) ----------

def test_kind_template_mapping_covers_all_kinds():
    """kind→模板映射覆盖 SceneKind 全部取值。"""
    assert set(KIND_TEMPLATES) == {"point", "qa", "ending"}
    for name in KIND_TEMPLATES.values():
        assert (Path(__file__).resolve().parent.parent / "src" / "interview_video" / "templates" / name).exists()


def test_cover_html_contains_title_brand_hook_and_industry(script, settings):
    cover_html = _render_html(
        _build_env(), "cover.html",
        brand=settings.brand, title=script.title, script=script,
        scene=None, index=0, total=len(script.scenes), industry="互联网",
    )
    assert script.title in cover_html
    assert settings.brand in cover_html
    assert "互联网" in cover_html                    # 行业 chip
    assert script.summary in cover_html              # hook 行
    assert ACCENT in cover_html                      # 点缀色注入
    assert "1080px" in cover_html and "180px" in cover_html  # 底部 180px 留白


def test_cover_html_without_industry_omits_chip(script, settings):
    cover_html = _render_html(
        _build_env(), "cover.html",
        brand=settings.brand, title=script.title, script=script,
        scene=None, index=0, total=len(script.scenes), industry="",
    )
    assert 'class="chip"' not in cover_html


def test_scene_html_contains_header_bullets_progress(script, settings):
    scene = script.scenes[0]
    html = _render_html(
        _build_env(), "scene.html",
        brand=settings.brand, title=script.title, script=script,
        scene=scene, index=0, total=len(script.scenes),
    )
    assert scene.header in html
    for b in scene.bullets:
        assert b in html
    assert "1 / 3" in html                             # 进度 index+1/total
    assert "72px" in html                              # header ≥56px 视觉规范
    assert "40px" in html                              # bullets ≥34px 视觉规范


def test_qa_html_contains_question_and_answer(script, settings):
    qa_scene = next(s for s in script.scenes if s.kind == "qa")
    html = _render_html(
        _build_env(), "qa.html",
        brand=settings.brand, title=script.title, script=script,
        scene=qa_scene, index=1, total=len(script.scenes),
    )
    assert qa_scene.question in html
    assert qa_scene.answer in html
    assert ">Q<" in html and ">A<" in html


def test_ending_html_contains_cta(script, settings):
    ending = script.scenes[-1]
    assert ending.kind == "ending"
    html = _render_html(
        _build_env(), "ending.html",
        brand=settings.brand, title=script.title, script=script,
        scene=ending, index=2, total=len(script.scenes),
    )
    assert ending.header in html
    assert "关注" in html                               # 关注引导 CTA
    assert settings.brand in html


# ---------- Pillow 字幕条(探测到字体才跑) ----------

def _load_font_or_skip(settings: Settings) -> None:
    try:
        load_subtitle_font(settings, settings.subtitle_fontsize)
    except RuntimeError:
        pytest.skip("本机未探测到中文字体, 跳过字幕条生成测试")


def test_load_font_from_settings_path(settings):
    """subtitle_font_path 显式配置优先; 配置错误须报错。"""
    from interview_video.subtitle_bars import _FONT_CANDIDATES

    existing = next(
        (p for p, _ in _FONT_CANDIDATES if Path(p).exists()), None
    )
    if existing is None:
        pytest.skip("本机无任何候选中文字体")
    settings = Settings(subtitle_font_path=existing)
    font = load_subtitle_font(settings, 52)
    assert font.size == 52

    bad = Settings(subtitle_font_path="/nonexistent/font.ttf")
    with pytest.raises(RuntimeError, match="subtitle_font_path"):
        load_subtitle_font(bad, 52)


def test_subtitle_bar_png(tmp_path, settings):
    """字幕条 PNG: 1920x1080 / RGBA / 非空; 关闭底条后全图应完全透明。"""
    _load_font_or_skip(settings)
    cues = [Cue(start=0.0, end=2.5, text="大家好, 这里是面试充电站")]

    bars = render_subtitle_bars(cues, tmp_path, settings)
    assert len(bars) == 1
    bar = bars[0]
    assert bar.png.exists() and bar.png.stat().st_size > 0
    assert bar.start == 0.0 and bar.end == 2.5 and bar.text == cues[0].text

    with Image.open(bar.png) as img:
        assert img.size == (1920, 1080)
        assert img.mode == "RGBA"                     # 透明通道
        # 有文字/底条 → 画面下部应有非透明像素, 上部应保持透明
        bottom = img.crop((0, 1080 - 300, 1920, 1080))
        top = img.crop((0, 0, 1920, 300))
        bottom_alpha = bottom.getchannel("A").getextrema()
        top_alpha = top.getchannel("A").getextrema()
        assert bottom_alpha[0] < 255                   # 最小 alpha < 255 → 有透明背景
        assert bottom_alpha[1] > 0                     # 下部有内容
        assert top_alpha == (0, 0)                     # 上部全透明


def test_subtitle_bar_without_box_fully_transparent_background(tmp_path, settings):
    _load_font_or_skip(settings)
    settings = Settings(subtitle_box=False)
    cues = [Cue(start=0.0, end=1.0, text="没有底条的字幕")]
    bars = render_subtitle_bars(cues, tmp_path, settings)
    with Image.open(bars[0].png) as img:
        alphas = img.getchannel("A").getextrema()
        # 无 box 时除文字/描边外全部透明; 全图 alpha 不应整片铺满 255
        assert alphas[0] == 0


def test_subtitle_bars_numbering_and_dir(tmp_path, settings):
    _load_font_or_skip(settings)
    cues = [Cue(start=float(i), end=float(i) + 1, text=f"第{i}条字幕") for i in range(3)]
    bars = render_subtitle_bars(cues, tmp_path / "subs", settings)
    assert [b.index for b in bars] == [0, 1, 2]
    for i, bar in enumerate(bars):
        assert bar.png.name == f"bar_{i:03d}.png"
        assert bar.png.parent == tmp_path / "subs"


# ---------- Playwright 截图(integration, 需 chromium) ----------

@pytest.mark.integration
def test_render_project_screenshots(script, settings, tmp_path):
    from interview_video.render import render_project

    result = render_project(script, tmp_path, settings, industry="互联网")
    # cover + 3 场景帧
    assert result.cover.exists()
    assert len(result.frames) == 3
    for path in [result.cover, *result.frames]:
        with Image.open(path) as img:
            assert img.size == (settings.video_width, settings.video_height)
    # scenes/scene_%03d.png 命名
    assert [p.name for p in result.frames] == ["scene_000.png", "scene_001.png", "scene_002.png"]
    assert result.frames[0].parent.name == "scenes"


def test_qa_html_long_text_steps_down_font(script, settings):
    """超长问题/答案: 加 len-l 类(字号下调 + 行数截断), 短文本不加。"""
    qa_scene = next(s for s in script.scenes if s.kind == "qa").model_copy(
        update={"question": "问" * 45, "answer": "答" * 70}
    )
    html = _render_html(
        _build_env(), "qa.html",
        brand=settings.brand, title=script.title, script=script,
        scene=qa_scene, index=1, total=len(script.scenes),
    )
    assert html.count('class="body len-l"') == 2
    assert "-webkit-line-clamp" in html
    short = qa_scene.model_copy(update={"question": "为什么离职？", "answer": "讲成长"})
    html = _render_html(
        _build_env(), "qa.html",
        brand=settings.brand, title=script.title, script=script,
        scene=short, index=1, total=len(script.scenes),
    )
    assert 'class="body"' in html and "len-l" not in html.split("</style>")[-1]
