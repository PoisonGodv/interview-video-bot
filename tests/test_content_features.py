"""内容/产品能力: 竖屏画幅、BGM、封面标题自适应、字幕细分、选题库、定时发布、批量生产。

全部离线: ffmpeg/上传/LLM/媒体层均打桩; 真实 ffmpeg 混音打 integration。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from interview_video import assemble as assemble_mod
from interview_video import backlog as backlog_mod
from interview_video import tts
from interview_video.bilibili import parse_publish_at
from interview_video.cli import app
from interview_video.config import Settings
from interview_video.models import Cue, Script, SubtitleBar, Topic
from interview_video.render import _build_env, _render_html, cover_title_px

SAMPLE_SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "sample_script.json"
runner = CliRunner()


def _s(tmp_path: Path, **kw) -> Settings:
    kw.setdefault("out_root", tmp_path / "output")
    return Settings(_env_file=None, **kw)


# ---------- (a) 9:16 竖屏 ----------

def test_frame_size_default_and_portrait(tmp_path: Path) -> None:
    assert _s(tmp_path).frame_size() == (1920, 1080)
    assert _s(tmp_path, video_aspect="9:16").frame_size() == (1080, 1920)
    # 用户若已按竖屏填了 VIDEO_WIDTH/HEIGHT, 不再二次对调
    assert _s(tmp_path, video_aspect="9:16", video_width=1080, video_height=1920).frame_size() == (1080, 1920)
    with pytest.raises(ValueError):
        _s(tmp_path, video_aspect="4:3")


@pytest.mark.parametrize("name", ["cover.html", "scene.html", "qa.html", "ending.html"])
def test_templates_portrait_body_class_and_viewport(name: str) -> None:
    script = Script.model_validate_json(SAMPLE_SCRIPT.read_text(encoding="utf-8"))
    scene = {
        "cover.html": None,
        "scene.html": script.scenes[0],
        "qa.html": next(s for s in script.scenes if s.kind == "qa"),
        "ending.html": script.scenes[-1],
    }[name]
    kw = dict(brand="b", title=script.title, script=script, scene=scene, index=0, total=3)
    portrait = _render_html(_build_env(), name, width=1080, height=1920, **kw)
    assert 'class="portrait"' in portrait
    assert "width: 1080px" in portrait and "height: 1920px" in portrait
    assert "body.portrait .page" in portrait
    landscape = _render_html(_build_env(), name, **kw)
    assert 'class="landscape"' in landscape and "width: 1920px" in landscape
    # 竖屏专用 CSS 块只在竖屏时输出
    assert portrait.count("{") > landscape.count("{")


def test_build_scene_clips_uses_portrait_size(tmp_path: Path, monkeypatch) -> None:
    from interview_video.models import SceneMedia

    cmds: list[list[str]] = []
    monkeypatch.setattr(assemble_mod, "_run", lambda cmd, cwd, timeout=300: cmds.append(cmd))
    monkeypatch.setattr(assemble_mod, "ffprobe_duration", lambda p: 2.0)
    sm = SceneMedia(index=0, png=tmp_path / "a.png", mp3=tmp_path / "a.mp3", duration=2.0)
    assemble_mod.build_scene_clips([sm], tmp_path / "clips", _s(tmp_path, video_aspect="9:16", video_motion=False))
    vf = cmds[0][cmds[0].index("-vf") + 1]
    assert "scale=1080:1920" in vf and "pad=1080:1920" in vf


def _font_or_skip(settings: Settings) -> None:
    from interview_video.subtitle_bars import load_subtitle_font

    try:
        load_subtitle_font(settings, settings.subtitle_fontsize)
    except RuntimeError:
        pytest.skip("本机未探测到中文字体")


def test_subtitle_bar_portrait_geometry(tmp_path: Path) -> None:
    from interview_video.subtitle_bars import (
        PORTRAIT_EXTRA_BOTTOM,
        render_subtitle_bars,
        subtitle_bottom,
    )

    settings = _s(tmp_path, video_aspect="9:16")
    _font_or_skip(settings)
    assert subtitle_bottom(settings) == settings.subtitle_bottom + PORTRAIT_EXTRA_BOTTOM
    bars = render_subtitle_bars([Cue(start=0, end=1, text="一" * 16)], tmp_path / "subs", settings)
    with Image.open(bars[0].png) as img:
        assert img.size == (1080, 1920)
        alpha = img.getchannel("A")
        bbox = alpha.getbbox()
        assert bbox is not None
        # 整条落在画布内(16 字全宽被缩放), 且高于竖屏底部平台操作栏区域
        assert bbox[0] > 0 and bbox[2] < 1080
        assert bbox[3] <= 1920 - subtitle_bottom(settings) + 20


# ---------- (b) BGM ----------

def _bar(i: int, start: float, end: float) -> SubtitleBar:
    return SubtitleBar(index=i, png=Path(f"subs/bar_{i:03d}.png"), start=start, end=end, text="x")


def test_finalize_without_bgm_keeps_simple_loudnorm(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(assemble_mod, "_run", lambda cmd, cwd, timeout=300: calls.append(cmd))
    monkeypatch.setattr(assemble_mod, "ffprobe_duration", lambda p: 10.0)
    assemble_mod.finalize_video([tmp_path / "c.mp4"], [], tmp_path / "final.mp4", _s(tmp_path))
    final = calls[-1]
    assert "-af" in final and "-filter_complex" not in final and "-stream_loop" not in final


def test_finalize_mixes_bgm_loop_duck_fade(tmp_path: Path, monkeypatch) -> None:
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"x")
    calls: list[list[str]] = []
    monkeypatch.setattr(assemble_mod, "_run", lambda cmd, cwd, timeout=300: calls.append(cmd))
    monkeypatch.setattr(assemble_mod, "ffprobe_duration", lambda p: 10.0)
    assemble_mod.finalize_video(
        [tmp_path / "c0.mp4", tmp_path / "c1.mp4"], [], tmp_path / "final.mp4",
        _s(tmp_path, bgm_file=bgm, bgm_volume_db=-20),
    )
    final = calls[-1]
    assert final[final.index("-stream_loop") + 1] == "-1"
    assert str(bgm.resolve()) in final
    fc = final[final.index("-filter_complex") + 1]
    assert "atrim=0:20" in fc and "volume=-20.0dB" in fc
    assert "sidechaincompress" in fc and "afade=t=out:st=17:d=3" in fc
    assert "amix=inputs=2:duration=first" in fc and "loudnorm=" in fc
    assert final[final.index("-c:v") + 1] == "copy" and "[aout]" in final


def test_bgm_filter_without_ducking_and_short_fade(tmp_path: Path) -> None:
    fc = assemble_mod.bgm_filter(4.0, _s(tmp_path, bgm_ducking=False), "loudnorm")
    assert "sidechaincompress" not in fc and "afade=t=out:st=3:d=1" in fc


def test_finalize_missing_bgm_skips_with_warning(tmp_path: Path, monkeypatch, caplog) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(assemble_mod, "_run", lambda cmd, cwd, timeout=300: calls.append(cmd))
    monkeypatch.setattr(assemble_mod, "ffprobe_duration", lambda p: 3.0)
    with caplog.at_level("WARNING"):
        assemble_mod.finalize_video(
            [tmp_path / "c.mp4"], [], tmp_path / "final.mp4",
            _s(tmp_path, bgm_file=tmp_path / "nope.mp3"),
        )
    assert "-stream_loop" not in calls[-1]
    assert any("BGM_FILE" in r.message for r in caplog.records)


def test_bgm_blank_env_is_none(tmp_path: Path) -> None:
    assert _s(tmp_path, bgm_file="  # 说明").bgm_file is None


def test_doctor_warns_missing_bgm(tmp_path: Path) -> None:
    from interview_video import doctor as doctor_mod

    checks = {c.name: c for c in doctor_mod.check_config(_s(tmp_path, bgm_file=tmp_path / "x.mp3"))}
    assert not checks["BGM"].ok and not checks["BGM"].required and "不存在" in checks["BGM"].detail
    assert "BGM" not in {c.name for c in doctor_mod.check_config(_s(tmp_path))}


HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.skipif(not HAS_FFMPEG, reason="本机无 ffmpeg/ffprobe")
@pytest.mark.integration
def test_finalize_with_bgm_real_ffmpeg(tmp_path: Path) -> None:
    def ff(*args: str) -> None:
        p = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, p.stderr[-500:]

    clip = tmp_path / "clips" / "scene_000.mp4"
    clip.parent.mkdir()
    ff("-f", "lavfi", "-i", "testsrc2=size=320x180:duration=4", "-f", "lavfi",
       "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-shortest", str(clip))
    bgm = tmp_path / "bgm.mp3"
    ff("-f", "lavfi", "-i", "sine=frequency=220:duration=1", str(bgm))  # 短于成片, 需循环
    out = assemble_mod.finalize_video([clip], [], tmp_path / "final.mp4", _s(tmp_path, bgm_file=bgm))
    assert abs(assemble_mod.ffprobe_duration(out) - 4.0) < 0.3


# ---------- (c) 封面标题自适应 ----------

def test_cover_title_px_scales_with_length() -> None:
    assert cover_title_px("程序员面试避坑指南") == 108
    long_title = "应届生第一次面试大厂技术岗到底该怎么准备才不会被面试官当场问倒"
    assert cover_title_px(long_title) == 108  # 3 行内放得下
    assert cover_title_px(long_title * 2) < 108
    assert cover_title_px(long_title * 3) == 56  # 下限
    # 竖屏更窄: 同一标题字号更小
    assert cover_title_px(long_title, 1080, True) < cover_title_px(long_title)
    # ASCII 按半宽计
    assert cover_title_px("A" * 60) > cover_title_px("字" * 60)


def test_cover_html_uses_fitted_font_size() -> None:
    script = Script.model_validate_json(SAMPLE_SCRIPT.read_text(encoding="utf-8"))
    title = "应届生第一次面试大厂技术岗到底该怎么准备才不会被面试官当场问倒" * 2
    html = _render_html(_build_env(), "cover.html", brand="b", title=title, script=script,
                        scene=None, index=0, total=3)
    assert f"font-size: {cover_title_px(title)}px" in html and "font-size: 108px" not in html


# ---------- (d) 字幕细分 ----------

def test_split_long_text_prefers_soft_punct() -> None:
    parts = tts.split_long_text("我们今天聊聊、面试中最常见的问题,以及如何回答它们才能拿到offer", 16)
    assert parts == ["我们今天聊聊、", "面试中最常见的问题,", "以及如何回答它们才能拿到", "offer"]
    assert all(len(p) <= 16 for p in parts)
    assert tts.split_long_text("一" * 40, 16) == ["一" * 16, "一" * 16, "一" * 8]
    assert tts.split_long_text("短句", 16) == ["短句"]
    # 硬切点正好是标点时, 标点随前段, 不让下一段以标点开头
    assert tts.split_long_text("一" * 16 + "，二", 16) == ["一" * 16 + "，", "二"]


def test_split_long_cues_distributes_time() -> None:
    cues = tts.split_long_cues([Cue(start=1.0, end=5.0, text="一" * 20 + "二" * 20)], 16)
    assert [len(c.text) for c in cues] == [16, 16, 8]
    assert cues[0].start == 1.0 and cues[-1].end == 5.0
    assert cues[0].end == pytest.approx(2.6) and cues[1].start == cues[0].end
    short = [Cue(start=0, end=1, text="你好")]
    assert tts.split_long_cues(short, 16) == short


async def test_synthesize_scenes_splits_overlong_edge_cues(tmp_path: Path, monkeypatch) -> None:
    async def fake_synth(text, mp3_path, voice, rate):
        mp3_path.write_bytes(b"x")
        return [Cue(start=0.0, end=4.0, text="一二三四五六七八九十一二三四五六七八九十")]

    monkeypatch.setattr(tts, "synthesize_text", fake_synth)
    script = Script(title="t", scenes=[{"header": "h", "narration": "口播"}])
    media = await tts.synthesize_scenes(script, tmp_path, _s(tmp_path, subtitle_max_chars=10))
    assert [c.text for c in media[0].cues] == ["一二三四五六七八九十", "一二三四五六七八九十"]


# ---------- (e) 选题库 ----------

def test_backlog_add_dedupe_pending_mark_used(tmp_path: Path) -> None:
    s = _s(tmp_path)
    added = backlog_mod.add_topics(s, [
        Topic(title="自我介绍怎么说", industry="互联网"),
        Topic(title="自我 介绍怎么说", industry="互联网"),  # 空白差异视为重复
        Topic(title="离职原因", industry="护理"),
    ])
    assert [a.title for a in added] == ["自我介绍怎么说", "离职原因"]
    assert backlog_mod.add_topics(s, [Topic(title="自我介绍怎么说", industry="互联网")]) == []
    assert backlog_mod.avoid_titles(s, "互联网") == ["自我介绍怎么说"]
    assert [p.title for p in backlog_mod.pending(s)] == ["自我介绍怎么说", "离职原因"]

    assert backlog_mod.mark_used(s, "自我介绍怎么说", "互联网", tmp_path / "proj") is True
    assert [p.title for p in backlog_mod.pending(s)] == ["离职原因"]
    item = backlog_mod.load_backlog(s)[0]
    assert item.status == "used" and item.used_at and item.project == str(tmp_path / "proj")
    assert backlog_mod.mark_used(s, "库外选题", "互联网") is False  # 补录为已用
    assert "库外选题" in backlog_mod.avoid_titles(s, "互联网")


def test_backlog_corrupt_file_treated_as_empty(tmp_path: Path) -> None:
    s = _s(tmp_path)
    backlog_mod.backlog_path(s).parent.mkdir(parents=True)
    backlog_mod.backlog_path(s).write_text("{bad", encoding="utf-8")
    assert backlog_mod.load_backlog(s) == []


async def test_pipeline_passes_avoid_and_marks_used(fake_media, tmp_path: Path, monkeypatch) -> None:
    from interview_video import llm as llm_mod
    from interview_video.chains import metadata as metadata_mod
    from interview_video.chains import script as script_mod
    from interview_video.chains import topic as topic_mod
    from interview_video.graph import run_pipeline

    s = _s(tmp_path, llm_api_key="sk-fake")
    backlog_mod.add_topics(s, [Topic(title="旧选题", industry="互联网")])
    backlog_mod.mark_used(s, "旧选题", "互联网")
    seen: dict = {}

    async def fake_topics(llm, industry, n=5, avoid=None):
        seen["avoid"] = avoid
        return [Topic(title="新选题A", industry=industry), Topic(title="新选题B", industry=industry)]

    async def fake_script(llm, topic, settings):
        return Script.model_validate_json(SAMPLE_SCRIPT.read_text(encoding="utf-8"))

    async def fake_meta(llm, script, chapters, settings):
        return None

    monkeypatch.setattr(llm_mod, "get_chat_model", lambda settings, audit=None: object())
    monkeypatch.setattr(topic_mod, "generate_topics", fake_topics)
    monkeypatch.setattr(script_mod, "write_script", fake_script)
    monkeypatch.setattr(metadata_mod, "write_metadata", fake_meta)

    await run_pipeline(s, tmp_path / "out", industry="互联网")
    assert seen["avoid"] == ["旧选题"]
    status = {i.title: i.status for i in backlog_mod.load_backlog(s)}
    assert status == {"旧选题": "used", "新选题A": "used", "新选题B": "new"}


async def test_pipeline_topic_title_reuses_backlog_details(fake_media, tmp_path: Path, monkeypatch) -> None:
    from interview_video import llm as llm_mod
    from interview_video.chains import metadata as metadata_mod
    from interview_video.chains import script as script_mod
    from interview_video.graph import run_pipeline

    s = _s(tmp_path, llm_api_key="sk-fake")
    backlog_mod.add_topics(s, [Topic(title="库内题", industry="护理", hook="钩子", key_points=["a"])])
    picked: list[Topic] = []

    async def fake_script(llm, topic, settings):
        picked.append(topic)
        return Script.model_validate_json(SAMPLE_SCRIPT.read_text(encoding="utf-8"))

    async def fake_meta(llm, script, chapters, settings):
        return None

    monkeypatch.setattr(llm_mod, "get_chat_model", lambda settings, audit=None: object())
    monkeypatch.setattr(script_mod, "write_script", fake_script)
    monkeypatch.setattr(metadata_mod, "write_metadata", fake_meta)
    await run_pipeline(s, tmp_path / "out", industry="护理", topic_title="库内题")
    assert picked[0].hook == "钩子" and picked[0].key_points == ["a"]
    assert backlog_mod.pending(s) == []


def _cli_settings(monkeypatch, tmp_path: Path, **kw) -> Settings:
    from interview_video import cli as cli_mod

    s = _s(tmp_path, **kw)
    monkeypatch.setattr(cli_mod, "load_settings", lambda: s)
    return s


def test_cli_topics_list_next_and_avoid(monkeypatch, tmp_path: Path) -> None:
    from interview_video import llm as llm_mod
    from interview_video.chains import topic as topic_mod

    s = _cli_settings(monkeypatch, tmp_path)
    r = runner.invoke(app, ["topics", "--next"])
    assert r.exit_code == 1 and "没有未用选题" in r.output

    backlog_mod.add_topics(s, [Topic(title="已有题", industry="护理")])
    seen: dict = {}

    async def fake_topics(llm, industry, n=5, avoid=None):
        seen["avoid"] = avoid
        return [Topic(title="已有题", industry=industry), Topic(title="新题", industry=industry)]

    monkeypatch.setattr(llm_mod, "get_chat_model", lambda settings, audit=None: object())
    monkeypatch.setattr(topic_mod, "generate_topics", fake_topics)
    r = runner.invoke(app, ["topics", "-i", "护理", "-n", "2"])
    assert r.exit_code == 0, r.output
    assert seen["avoid"] == ["已有题"] and "新入选题库 1 条" in r.output

    r = runner.invoke(app, ["topics", "--next", "-i", "护理"])
    assert r.exit_code == 0 and "已有题" in r.output
    backlog_mod.mark_used(s, "已有题", "护理")
    r = runner.invoke(app, ["topics", "--list"])
    assert "新题" in r.output and "已有题" not in r.output
    r = runner.invoke(app, ["topics", "--list", "--all"])
    assert "已有题" in r.output and "已用" in r.output


# ---------- 批量生产 ----------

def test_cli_batch_continues_on_failure(monkeypatch, tmp_path: Path) -> None:
    from interview_video import cli as cli_mod

    s = _cli_settings(monkeypatch, tmp_path)
    backlog_mod.add_topics(s, [Topic(title=f"题{i}", industry="互联网") for i in range(4)])
    backlog_mod.add_topics(s, [Topic(title="别的行业", industry="护理")])
    ran: list[str] = []

    async def fake_run(settings, out_dir, industry="互联网", topic_title=None, **kw):
        ran.append(topic_title)
        if topic_title == "题1":
            raise RuntimeError("渲染炸了")
        backlog_mod.mark_used(settings, topic_title, industry, out_dir)
        from types import SimpleNamespace

        return SimpleNamespace(duration_total=12.0)

    monkeypatch.setattr(cli_mod, "run_pipeline", fake_run)
    r = runner.invoke(app, ["batch", "-i", "互联网", "-n", "3", "--aspect", "9:16"])
    assert ran == ["题0", "题1", "题2"]
    assert r.exit_code == 1
    assert "批量生成汇总" in r.output and "渲染炸了" in r.output and "失败" in r.output
    assert s.video_aspect == "9:16"
    assert [p.title for p in backlog_mod.pending(s, "互联网")] == ["题1", "题3"]


def test_cli_batch_empty_backlog(monkeypatch, tmp_path: Path) -> None:
    _cli_settings(monkeypatch, tmp_path)
    r = runner.invoke(app, ["batch", "-n", "2"])
    assert r.exit_code == 1 and "没有未用选题" in r.output


def test_cli_generate_aspect_bgm_validation(monkeypatch, tmp_path: Path) -> None:
    _cli_settings(monkeypatch, tmp_path)
    r = runner.invoke(app, ["generate", "--aspect", "4:3", "--script-file", str(SAMPLE_SCRIPT)])
    assert r.exit_code == 2 and "--aspect" in r.output
    r = runner.invoke(app, ["generate", "--bgm", str(tmp_path / "nope.mp3"), "--script-file", str(SAMPLE_SCRIPT)])
    assert r.exit_code == 2 and "--bgm" in r.output


# ---------- (f) 定时发布 ----------

def test_parse_publish_at_window() -> None:
    now = time.time()
    ok = datetime.fromtimestamp(now) + timedelta(days=1)
    assert parse_publish_at(ok.strftime("%Y-%m-%d %H:%M"), now=now) == int(
        datetime.strptime(ok.strftime("%Y-%m-%d %H:%M"), "%Y-%m-%d %H:%M").timestamp()
    )
    soon = datetime.fromtimestamp(now) + timedelta(minutes=90)
    with pytest.raises(ValueError, match="2 小时"):
        parse_publish_at(soon.strftime("%Y-%m-%d %H:%M"), now=now)
    far = datetime.fromtimestamp(now) + timedelta(days=16)
    with pytest.raises(ValueError, match="15 天"):
        parse_publish_at(far.strftime("%Y-%m-%d %H:%M"), now=now)
    with pytest.raises(ValueError, match="格式"):
        parse_publish_at("明天下午", now=now)


def _fake_project(tmp_path: Path) -> Path:
    from interview_video.models import ProjectManifest

    d = tmp_path / "proj"
    d.mkdir()
    (d / "cover.png").write_bytes(b"x")
    (d / "final.mp4").write_bytes(b"x")
    m = ProjectManifest(
        created_at=datetime.now(), industry="互联网", script=Script(title="标题", scenes=[]),
        video=d / "final.mp4", cover=d / "cover.png",
    )
    (d / "project.json").write_text(m.model_dump_json(), encoding="utf-8")
    return d


def test_upload_passes_delay_time_and_records_it(tmp_path: Path, monkeypatch) -> None:
    from interview_video import bilibili as bili_mod

    metas: list[dict] = []

    class FakeUploader:
        def __init__(self, pages, meta, credential):
            pass

        async def start(self):
            return {"bvid": "BV9"}

    async def fake_cover(pic, credential):
        return "https://cover"

    vu = bili_mod.video_uploader
    monkeypatch.setattr(vu, "upload_cover", fake_cover)
    monkeypatch.setattr(vu, "VideoUploader", FakeUploader)
    monkeypatch.setattr(vu, "VideoUploaderPage", lambda *a, **k: None)
    monkeypatch.setattr(vu, "VideoMeta", lambda **k: metas.append(k))
    monkeypatch.setattr(bili_mod.Picture, "from_file", staticmethod(lambda p: None))
    s = _s(tmp_path, bili_upload_enabled=True, bili_sessdata="s", bili_jct="j", bili_buvid3="b")
    d = _fake_project(tmp_path)
    dtime = int(time.time()) + 86400

    assert asyncio.run(bili_mod.upload_project(d, s, dtime=dtime)) == "BV9"
    assert metas[0]["delay_time"] == dtime
    rec = json.loads((d / "upload.json").read_text("utf-8"))
    assert rec["dtime"] == dtime and "publish_at" in rec
    asyncio.run(bili_mod.upload_project(d, s, force=True))
    assert "delay_time" not in metas[1]


def test_cli_upload_publish_at_invalid_rejected_before_upload(monkeypatch, tmp_path: Path) -> None:
    from interview_video import bilibili as bili_mod

    _cli_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(bili_mod, "upload_project", None)  # 不应被调用
    r = runner.invoke(app, ["upload", str(tmp_path), "-y", "--publish-at", "2000-01-01 10:00"])
    assert r.exit_code == 2 and "2 小时" in r.output


def test_web_upload_publish_at(monkeypatch, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from interview_video import bilibili as bili_mod
    from interview_video.web import create_app

    s = _s(tmp_path, bili_upload_enabled=True, bili_sessdata="s", bili_jct="j", bili_buvid3="b")
    d = s.out_root / "proj"
    d.mkdir(parents=True)
    _fake = _fake_project(tmp_path)
    for f in _fake.iterdir():
        shutil.copy(f, d / f.name)
    seen: dict = {}

    async def fake_upload(project_dir, settings_, *, force=False, dtime=None):
        seen["dtime"] = dtime
        return "BV1"

    monkeypatch.setattr(bili_mod, "upload_project", fake_upload)
    with TestClient(create_app(s), base_url="http://localhost") as client:
        r = client.post("/api/projects/proj/upload", json={"publish_at": "2000-01-01 10:00"})
        assert r.status_code == 400 and "2 小时" in r.json()["detail"]
        when = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        r = client.post("/api/projects/proj/upload", json={"publish_at": when})
        assert r.status_code == 200 and r.json()["dtime"] == seen["dtime"] > time.time()


def test_web_topics_backlog_and_avoid(monkeypatch, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from interview_video.web import create_app

    s = _s(tmp_path)
    backlog_mod.add_topics(s, [Topic(title="已有", industry="护理")])
    seen: dict = {}

    async def fake_topics(llm, industry, n, avoid=None):
        seen["avoid"] = avoid
        return [Topic(title="新的", industry=industry)]

    monkeypatch.setattr("interview_video.llm.get_chat_model", lambda s: object())
    monkeypatch.setattr("interview_video.chains.topic.generate_topics", fake_topics)
    with TestClient(create_app(s), base_url="http://localhost") as client:
        assert client.post("/api/topics", json={"industry": "护理", "n": 1}).status_code == 200
        assert seen["avoid"] == ["已有"]
        titles = [t["title"] for t in client.get("/api/topics/backlog?industry=护理").json()["topics"]]
        assert titles == ["已有", "新的"]
        assert client.get("/api/settings").json()["video_aspect"] == "16:9"
