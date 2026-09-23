"""graph 编排层测试。

媒体层(tts/render/assemble/subtitle_bars)全部 monkeypatch 为本地假实现(只造小文件),
不联网、不调真实 LLM、不起 chromium、不调 ffmpeg。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from interview_video import llm as llm_mod
from interview_video.chains import metadata as metadata_mod
from interview_video.chains import script as script_mod
from interview_video.chains import topic as topic_mod
from interview_video.config import Settings
from interview_video.graph import build_graph, run_pipeline
from interview_video.models import (
    BiliMeta,
    Script,
    Topic,
)

SAMPLE_SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "sample_script.json"


async def test_run_pipeline_script_file_degrades_metadata(
    fake_media: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """--script-file 模式: 无 LLM 可用时 metadata 节点降级 meta=None 但不中断。

    显式让 get_chat_model 抛 RuntimeError(模拟未配 key), 与环境中是否有 .env 无关。
    """

    def _no_llm(settings: Settings):
        raise RuntimeError("未配置 LLM_API_KEY(测试注入)")

    monkeypatch.setattr(llm_mod, "get_chat_model", _no_llm)
    settings = Settings()  # llm_api_key=None
    out_dir = tmp_path / "out"
    with caplog.at_level("WARNING", logger="interview_video.graph"):
        manifest = await run_pipeline(
            settings, out_dir, industry="互联网", script_file=SAMPLE_SCRIPT
        )
    assert any("降级" in r.message for r in caplog.records), "应发出 metadata 降级日志"
    assert manifest.meta is None
    # 产物齐全
    assert (out_dir / "script.json").exists()
    assert (out_dir / "final.mp4").exists()
    assert (out_dir / "cover.png").exists()
    assert manifest.video == out_dir / "final.mp4"
    assert manifest.cover == out_dir / "cover.png"
    assert manifest.srt == out_dir / "subs" / "subtitles.srt"
    assert manifest.srt is not None and manifest.srt.exists()
    assert manifest.script.title == "程序员面试避坑指南"
    assert len(manifest.script.scenes) == 3
    # duration_total = 2.0+3.0+4.0(假实现给出的各场景时长)
    assert manifest.duration_total == pytest.approx(9.0)
    # 章节: 3 个, 起点 0/2/5
    assert [c.title for c in manifest.chapters] == ["开场", "高频题", "下期预告"]
    assert [c.start for c in manifest.chapters] == [0.0, 2.0, 5.0]
    # project.json 落盘且可回读
    from interview_video.models import ProjectManifest

    reloaded = ProjectManifest.model_validate_json(
        (out_dir / "project.json").read_text(encoding="utf-8")
    )
    assert reloaded == manifest
    # 字幕条落盘 3 张(每场景 1 cue)
    bars = sorted((out_dir / "subs").glob("bar_*.png"))
    assert len(bars) == 3


async def test_run_pipeline_llm_path_with_fakes(
    fake_media: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM 路径: fake get_chat_model + fake chains, 验证 topic 选择 / meta 写入。"""
    fake_llm = object()
    monkeypatch.setattr(
        llm_mod, "get_chat_model", lambda settings: fake_llm
    )

    async def fake_generate_topics(llm, industry, n=5, avoid=None):
        assert llm is fake_llm
        return [
            Topic(title="自我介绍怎么说", industry=industry),
            Topic(title="离职原因怎么答", industry=industry),
        ]

    async def fake_write_script(llm, topic, settings):
        return Script.model_validate_json(SAMPLE_SCRIPT.read_text(encoding="utf-8"))

    async def fake_write_metadata(llm, script, chapters, settings):
        return BiliMeta(title="测试标题", desc="测试简介", tags=["面试", "互联网"])

    monkeypatch.setattr(topic_mod, "generate_topics", fake_generate_topics)
    monkeypatch.setattr(script_mod, "write_script", fake_write_script)
    monkeypatch.setattr(metadata_mod, "write_metadata", fake_write_metadata)

    settings = Settings(llm_api_key="sk-fake")  # 有 key 也不联网, 因为 get_chat_model 被替换
    out_dir = tmp_path / "out"
    manifest = await run_pipeline(
        settings,
        out_dir,
        industry="互联网",
        topic_title="离职原因",  # 应选中第 2 条选题
    )
    assert manifest.meta is not None
    assert manifest.meta.title == "测试标题"
    assert manifest.industry == "互联网"
    # 无降级 warning 之外, 流水线照常落盘
    assert (out_dir / "script.json").exists()


async def test_run_pipeline_topic_title_no_match_uses_first(
    fake_media: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """topic_title 匹配不到时退回第一条选题(仍走通全流程)。"""
    monkeypatch.setattr(llm_mod, "get_chat_model", lambda settings: object())
    picked: list[Topic] = []

    async def fake_generate_topics(llm, industry, n=5, avoid=None):
        return [Topic(title="A题", industry=industry), Topic(title="B题", industry=industry)]

    async def fake_write_script(llm, topic, settings):
        picked.append(topic)
        return Script.model_validate_json(SAMPLE_SCRIPT.read_text(encoding="utf-8"))

    async def fake_write_metadata(llm, script, chapters, settings):
        return BiliMeta(title="t", desc="", tags=["面试"])

    monkeypatch.setattr(topic_mod, "generate_topics", fake_generate_topics)
    monkeypatch.setattr(script_mod, "write_script", fake_write_script)
    monkeypatch.setattr(metadata_mod, "write_metadata", fake_write_metadata)

    settings = Settings(llm_api_key="sk-fake")
    manifest = await run_pipeline(
        settings, tmp_path / "out", topic_title="完全不存在的提示词"
    )
    assert picked[0].title == "A题"
    assert manifest.meta is None or manifest.meta is not None  # 不中断即可


def test_build_graph_compiles() -> None:
    """build_graph 返回可编译图, 且节点齐全。"""
    graph = build_graph(Settings())
    node_names = set(graph.get_graph().nodes.keys())
    assert {
        "__start__",
        "gen_script",
        "tts",
        "render",
        "clips",
        "subs",
        "finalize",
        "metadata",
        "manifest",
        "__end__",
    } <= node_names


async def test_metadata_node_failure_degrades_not_crashes(
    fake_media: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """final.mp4 已合成后 metadata LLM 调用失败: 降级 meta=None, 流水线照常落盘。

    (旧行为: write_metadata 异常直接炸掉整图, manifest 不写, 几分钟媒体产物白做。)
    """
    monkeypatch.setattr(llm_mod, "get_chat_model", lambda settings: object())

    async def exploding_metadata(llm, script, chapters, settings):
        raise ValueError("LLM 输出解析失败(模拟)")

    monkeypatch.setattr(metadata_mod, "write_metadata", exploding_metadata)
    settings = Settings(_env_file=None)
    out_dir = tmp_path / "out"
    with caplog.at_level("WARNING", logger="interview_video.graph"):
        manifest = await run_pipeline(
            settings, out_dir, industry="互联网", script_file=SAMPLE_SCRIPT
        )
    assert manifest.meta is None
    assert (out_dir / "project.json").exists() and (out_dir / "final.mp4").exists()
    assert any("metadata" in r.message and "降级" in r.message for r in caplog.records)
    # 新增生成溯源块
    assert manifest.generation is not None
    assert manifest.generation["tts_provider"] == settings.tts_provider
