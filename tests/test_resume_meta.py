"""--resume 复用 meta / meta_error / 绝对路径 / voice 按通道 / 用量 since 过滤 / CLI 分阶段进度。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from interview_video import llm as llm_mod
from interview_video.chains import metadata as metadata_mod
from interview_video.cli import app
from interview_video.config import Settings
from interview_video.graph import PIPELINE_STAGES, run_pipeline
from interview_video.llm_audit import summarize_usage
from interview_video.models import BiliMeta, ProjectManifest, Script

SAMPLE_SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "sample_script.json"
runner = CliRunner()


def _no_llm(settings, audit=None):
    raise RuntimeError("未配置 LLM_API_KEY(测试注入)")


async def _first_run_with_meta(out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectManifest:
    monkeypatch.setattr(llm_mod, "get_chat_model", lambda settings, audit=None: object())

    async def meta_ok(llm, script, chapters, settings):
        return BiliMeta(title="旧标题", desc="旧简介", tags=["面试"])

    monkeypatch.setattr(metadata_mod, "write_metadata", meta_ok)
    return await run_pipeline(Settings(_env_file=None), out_dir, script_file=SAMPLE_SCRIPT)


async def test_resume_reuses_meta_without_llm(fake_media, tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    first = await _first_run_with_meta(out_dir, monkeypatch)
    assert first.meta is not None
    monkeypatch.setattr(llm_mod, "get_chat_model", _no_llm)
    script = Script.model_validate_json((out_dir / "script.json").read_text("utf-8"))
    again = await run_pipeline(Settings(_env_file=None), out_dir, script=script)
    assert again.meta == first.meta
    assert "meta_error" not in (again.generation or {})


async def test_resume_llm_failure_keeps_old_meta(fake_media, tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    first = await _first_run_with_meta(out_dir, monkeypatch)
    monkeypatch.setattr(llm_mod, "get_chat_model", _no_llm)
    script = Script.model_validate_json((out_dir / "script.json").read_text("utf-8"))
    script.title = "改过的标题"  # 脚本变了 → 要重新生成, 但 LLM 不可用
    again = await run_pipeline(Settings(_env_file=None), out_dir, script=script)
    assert again.meta == first.meta
    assert "LLM" in again.generation["meta_error"]


async def test_resume_changed_script_regenerates(fake_media, tmp_path: Path, monkeypatch) -> None:
    out_dir = tmp_path / "out"
    await _first_run_with_meta(out_dir, monkeypatch)
    calls: list[str] = []

    async def meta_new(llm, script, chapters, settings):
        calls.append(script.title)
        return BiliMeta(title="新标题", desc="", tags=["面试"])

    monkeypatch.setattr(metadata_mod, "write_metadata", meta_new)
    script = Script.model_validate_json((out_dir / "script.json").read_text("utf-8"))
    script.scenes[0].narration += "补一句。"
    again = await run_pipeline(Settings(_env_file=None), out_dir, script=script)
    assert calls and again.meta.title == "新标题"
    # 脚本不变但 regen_meta=True 也会重新生成
    await run_pipeline(Settings(_env_file=None), out_dir, script=script, regen_meta=True)
    assert len(calls) == 2


async def test_manifest_paths_absolute_and_voice_by_provider(fake_media, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(llm_mod, "get_chat_model", _no_llm)
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, tts_provider="doubao", tts_doubao_voice="zh_female_x")
    m = await run_pipeline(settings, Path("rel-out"), script_file=SAMPLE_SCRIPT)
    assert m.video.is_absolute() and m.cover.is_absolute() and m.srt.is_absolute()
    assert m.voice == "zh_female_x"
    assert m.meta is None and "LLM" in m.generation["meta_error"]


def test_summarize_usage_since(tmp_path: Path) -> None:
    now = datetime.now().astimezone()
    lines = [
        {"ts": (now - timedelta(hours=1)).isoformat(), "total_tokens": 100, "input_tokens": 60},
        {"ts": now.isoformat(), "total_tokens": 10, "input_tokens": 6},
    ]
    (tmp_path / "llm_calls.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )
    assert summarize_usage(tmp_path)["calls"] == 2
    u = summarize_usage(tmp_path, since=datetime.now() - timedelta(minutes=5))
    assert u["calls"] == 1 and u["total_tokens"] == 10


def test_run_pipeline_on_stage_order(fake_media, tmp_path: Path, monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(llm_mod, "get_chat_model", _no_llm)
    events: list[tuple[str, str]] = []
    asyncio.run(run_pipeline(
        Settings(_env_file=None), tmp_path / "o", script_file=SAMPLE_SCRIPT,
        on_stage=lambda n, s: events.append((n, s)),
    ))
    done = [n for n, s in events if s == "done"]
    assert done == [k for k, _ in PIPELINE_STAGES]
    assert events[0] == ("gen_script", "start")


def test_cli_generate_prints_stage_progress(fake_media, tmp_path: Path, monkeypatch) -> None:
    from interview_video import cli as cli_mod

    monkeypatch.setattr(llm_mod, "get_chat_model", _no_llm)
    monkeypatch.setattr(cli_mod, "load_settings", lambda: Settings(_env_file=None, out_root=tmp_path))
    result = runner.invoke(
        app, ["generate", "--script-file", str(SAMPLE_SCRIPT), "--out", str(tmp_path / "o")]
    )
    assert result.exit_code == 0, result.output
    for i, (_, label) in enumerate(PIPELINE_STAGES, 1):
        assert f"[{i}/{len(PIPELINE_STAGES)}] {label}" in result.output
    assert "未生成(已降级" in result.output and "无法初始化 LLM" in result.output
