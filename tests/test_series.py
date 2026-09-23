"""系列长视频(多 agent 编排)测试: 链解析/滚动上下文/双循环图, 全部用假模型不联网。"""
from __future__ import annotations

import json

import pytest

from interview_video.chains import series as sc
from interview_video.config import Settings
from interview_video.llm import fake_chat_model
from interview_video.models import (
    ChapterDraft,
    EpisodeOutline,
    EpisodePlan,
    Scene,
    Script,
    SeriesPlan,
)
from interview_video.series import generate_series


def _settings(**kw) -> Settings:
    kw.setdefault("series_review", True)
    return Settings(_env_file=None, **kw)


def _scene(header: str, narration: str = "大家好，这里是测试口播内容。" * 3) -> Scene:
    return Scene(kind="point", header=header, bullets=["要点"], narration=narration)


PLAN_JSON = json.dumps({
    "theme": "程序员面试通关", "title": "通关系列", "synopsis": "系列圣经",
    "episodes": [
        {"index": 1, "title": "第一集", "summary": "s1", "key_points": ["a"], "target_sec": 900},
        {"index": 2, "title": "第二集", "summary": "s2", "key_points": ["b"], "target_sec": 900},
    ],
}, ensure_ascii=False)

CHAPTER_JSON = json.dumps({
    "scenes": [
        {"kind": "point", "header": "小节", "bullets": ["p1"], "narration": "口播内容，" * 30},
        {"kind": "qa", "header": "问答", "bullets": [], "narration": "问答口播。",
         "question": "Q?", "answer": "A."},
    ],
    "summary": "本章讲了小节与问答。",
}, ensure_ascii=False)


# ---------- 解析与纯函数 ----------

def test_parse_plan_tolerates_fence():
    plan = sc.parse_series_plan_json(f"前言废话\n```json\n{PLAN_JSON}\n```\n后记")
    assert plan.title == "通关系列" and len(plan.episodes) == 2


def test_parse_chapter_json():
    draft = sc.parse_chapter_json(CHAPTER_JSON)
    assert len(draft.scenes) == 2 and draft.summary


def test_roll_context_truncates():
    prev = "旧" * 580
    merged = sc.roll_context(prev, ChapterDraft(scenes=[], summary="新摘要"))
    assert len(merged) <= sc.CONTEXT_WINDOW_CHARS
    assert merged.endswith("新摘要")


def test_build_episode_script_kinds():
    ep = EpisodePlan(index=1, title="集1")
    outline = EpisodeOutline(episode_index=1, title="集1", chapters=[])
    scenes = [_scene("a"), _scene("b"), _scene("c")]
    script = sc.build_episode_script(ep, outline, scenes)
    assert script.scenes[0].kind == "point"
    assert script.scenes[-1].kind == "ending"
    assert script.title == "集1"


def test_build_episode_script_empty_raises():
    with pytest.raises(ValueError):
        sc.build_episode_script(
            EpisodePlan(index=1, title="x"),
            EpisodeOutline(episode_index=1, title="x", chapters=[]),
            [],
        )


def test_fix_draft_normalizes_kind_and_truncates():
    draft = ChapterDraft(
        scenes=[Scene(kind="ending", header="h", narration="长" * 200)], summary="s"
    )
    fixed = sc._fix_draft(draft)
    assert fixed.scenes[0].kind == "point"  # 章节内不允许 ending
    assert len(fixed.scenes[0].narration) <= 110


# ---------- 链(假模型走回退路径) ----------

async def test_plan_series_fallback_path():
    llm = fake_chat_model([PLAN_JSON])
    plan = await sc.plan_series(llm, "程序员面试通关", episodes=2, episode_sec=900)
    assert plan.title == "通关系列" and plan.episodes[1].title == "第二集"


async def test_write_chapter_fallback_and_fix():
    llm = fake_chat_model([CHAPTER_JSON])
    draft = await sc.write_chapter(
        llm,
        SeriesPlan(theme="t", title="t", synopsis="s", episodes=[]),
        EpisodeOutline(episode_index=1, title="集1", chapters=[]),
        sc.ChapterSpec(title="章1", est_sec=150),
        ch_no=1, prev_summary="", settings=_settings(),
    )
    assert len(draft.scenes) == 2
    assert all(len(s.narration) <= 110 for s in draft.scenes)


# ---------- 编排(假 chains, 验证双循环与上下文切分) ----------

async def test_series_orchestration_context_flow(monkeypatch):
    """2 集(2章+1章): 验证集内滚动摘要传递、集边界上下文重置、脚本汇总。"""
    plan = SeriesPlan(
        theme="t", title="系列", synopsis="圣经",
        episodes=[
            EpisodePlan(index=1, title="集1", target_sec=300),
            EpisodePlan(index=2, title="集2", target_sec=300),
        ],
    )
    outlines = {
        0: EpisodeOutline(episode_index=1, title="集1", chapters=[
            sc.ChapterSpec(title="e1c1", est_sec=150),
            sc.ChapterSpec(title="e1c2", est_sec=150),
        ]),
        1: EpisodeOutline(episode_index=2, title="集2", chapters=[
            sc.ChapterSpec(title="e2c1", est_sec=150),
        ]),
    }
    seen_prev: list[str] = []
    reviewed: list[str] = []

    async def fake_plan(llm, theme, episodes, episode_sec):
        return plan

    async def fake_outline(llm, plan, episode, chapter_sec):
        return outlines[episode.index - 1]

    async def fake_write(llm, plan, outline, chapter, ch_no, prev_summary, settings):
        seen_prev.append(f"{outline.title}/{chapter.title}::[{prev_summary}]")
        return ChapterDraft(
            scenes=[_scene(f"{chapter.title}镜")], summary=f"{chapter.title}摘要"
        )

    async def fake_review(llm, draft, chapter, prev_summary):
        reviewed.append(chapter.title)
        return draft

    monkeypatch.setattr(sc, "plan_series", fake_plan)
    monkeypatch.setattr(sc, "write_episode_outline", fake_outline)
    monkeypatch.setattr(sc, "write_chapter", fake_write)
    monkeypatch.setattr(sc, "review_chapter", fake_review)
    monkeypatch.setattr("interview_video.llm.get_chat_model", lambda s: object())

    events: list[str] = []
    final_plan, scripts = await generate_series(
        _settings(), "t", episodes=2, episode_sec=300,
        on_event=lambda name, **p: events.append(name),
    )
    assert final_plan.title == "系列"
    assert [len(s.scenes) for s in scripts] == [2, 1]
    # 上下文切分: 集1第2章看到第1章摘要; 集2第1章摘要被重置为空
    assert seen_prev[0] == "集1/e1c1::[]"
    assert "e1c1摘要" in seen_prev[1]
    assert seen_prev[2] == "集2/e2c1::[]"
    # 审稿每章都被调用
    assert reviewed == ["e1c1", "e1c2", "e2c1"]
    # 末镜 ending
    assert scripts[0].scenes[-1].kind == "ending"
    # 事件流完整
    assert events[0] == "plan_done" and events.count("script_done") == 2


async def test_series_review_off(monkeypatch):
    """series_review=False 时审稿 agent 不被调用。"""
    plan = SeriesPlan(
        theme="t", title="s", synopsis="s",
        episodes=[EpisodePlan(index=1, title="集1", target_sec=300)],
    )

    async def fake_plan(llm, theme, episodes, episode_sec):
        return plan

    async def fake_outline(llm, plan, episode, chapter_sec):
        return EpisodeOutline(episode_index=1, title="集1",
                              chapters=[sc.ChapterSpec(title="c", est_sec=150)])

    async def fake_write(llm, plan, outline, chapter, ch_no, prev_summary, settings):
        return ChapterDraft(scenes=[_scene("镜")], summary="摘要")

    async def fake_review(llm, draft, chapter, prev_summary):  # pragma: no cover
        raise AssertionError("review 不应被调用")

    monkeypatch.setattr(sc, "plan_series", fake_plan)
    monkeypatch.setattr(sc, "write_episode_outline", fake_outline)
    monkeypatch.setattr(sc, "write_chapter", fake_write)
    monkeypatch.setattr(sc, "review_chapter", fake_review)
    monkeypatch.setattr("interview_video.llm.get_chat_model", lambda s: object())

    _, scripts = await generate_series(
        _settings(series_review=False), "t", episodes=1, episode_sec=300
    )
    assert len(scripts) == 1


# ---------- 单集流水线接入: 预制脚本直通 ----------

async def test_pipeline_preset_script_skips_llm(fake_media, tmp_path):
    """gen_script 直通: state 预置 script 时不需要 LLM, 也不读 script_file。"""
    from interview_video.graph import run_pipeline

    settings = Settings(_env_file=None)  # 无 LLM key: 若直通失败会在 metadata 降级前崩
    script = Script(title="预制脚本", scenes=[_scene("唯一镜")])
    manifest = await run_pipeline(settings, tmp_path / "out", script=script)
    assert manifest.script.title == "预制脚本"
    assert (tmp_path / "out" / "script.json").exists()
    assert manifest.meta is None  # 无 key → metadata 降级, 不中断


# ---------- 章节护栏(clamp) ----------

def test_clamp_outline_truncates_and_clamps_est_sec():
    from interview_video.series import _clamp_outline

    outline = EpisodeOutline(
        episode_index=1, title="集1",
        chapters=[sc.ChapterSpec(title=f"章{i}", est_sec=150) for i in range(50)],
    )
    outline.chapters[0].est_sec = 99999
    outline.chapters[1].est_sec = 1
    clamped = _clamp_outline(outline, ep_target_sec=900, chapter_sec=150)
    # 900/150 + 2 = 8 章(≥ _MIN_CHAPTERS=4)
    assert len(clamped.chapters) == 8
    assert all(60 <= c.est_sec <= 600 for c in clamped.chapters)


def test_clamp_outline_keeps_min_chapters():
    from interview_video.series import _clamp_outline

    outline = EpisodeOutline(
        episode_index=1, title="集1",
        chapters=[sc.ChapterSpec(title=f"章{i}", est_sec=150) for i in range(3)],
    )
    clamped = _clamp_outline(outline, ep_target_sec=120, chapter_sec=150)
    assert len(clamped.chapters) == 3  # 不足下限不补章, 只截上界


def test_write_chapter_scene_count_cap():
    """est_sec 异常大时, 分镜数被夹在 MAX_SCENES_PER_CHAPTER 内。"""
    from interview_video.chains.series import MAX_SCENES_PER_CHAPTER

    big = sc.ChapterSpec(title="大章", est_sec=6000)
    scene_count = min(MAX_SCENES_PER_CHAPTER, max(4, round(big.est_sec / 20)))
    assert scene_count == MAX_SCENES_PER_CHAPTER


# ---------- 增量落盘 + 断点续跑 ----------

async def test_series_incremental_persist_and_resume(monkeypatch, tmp_path):
    """run_dir 模式: 每章/每集落盘; 失败后 resume 从磁盘重建状态,
    已完成的集不再调 LLM(用假链计数验证)。"""
    from interview_video import series as series_mod

    plan = SeriesPlan(
        theme="t", title="系列", synopsis="圣经",
        episodes=[
            EpisodePlan(index=1, title="集1", target_sec=300),
            EpisodePlan(index=2, title="集2", target_sec=300),
        ],
    )
    outline1 = EpisodeOutline(episode_index=1, title="集1", chapters=[
        sc.ChapterSpec(title="e1c1", est_sec=150),
        sc.ChapterSpec(title="e1c2", est_sec=150),
    ])
    outline2 = EpisodeOutline(episode_index=2, title="集2", chapters=[
        sc.ChapterSpec(title="e2c1", est_sec=150),
    ])
    outlines = {1: outline1, 2: outline2}
    calls: list[str] = []

    class _Boom(Exception):
        pass

    async def fake_plan(llm, theme, episodes, episode_sec):
        calls.append("plan")
        return plan

    async def fake_outline(llm, plan_, episode, chapter_sec):
        calls.append(f"outline{episode.index}")
        return outlines[episode.index]

    async def fake_write(llm, plan_, outline, chapter, ch_no, prev_summary, settings):
        calls.append(f"write:{outline.title}/{chapter.title}")
        return ChapterDraft(
            scenes=[_scene(f"{chapter.title}镜")], summary=f"{chapter.title}摘要"
        )

    async def fake_review(llm, draft, chapter, prev_summary):
        return draft

    monkeypatch.setattr(sc, "plan_series", fake_plan)
    monkeypatch.setattr(sc, "write_episode_outline", fake_outline)
    monkeypatch.setattr(sc, "write_chapter", fake_write)
    monkeypatch.setattr(sc, "review_chapter", fake_review)
    monkeypatch.setattr("interview_video.llm.get_chat_model", lambda s: object())

    settings = _settings()
    root = tmp_path / "run"

    # 第一次跑: 写到第 2 集大纲后崩(模拟中途失败)
    real_outline = fake_outline

    async def exploding_outline(llm, plan_, episode, chapter_sec):
        if episode.index == 2:
            raise _Boom("模拟中途失败")
        return await real_outline(llm, plan_, episode, chapter_sec)

    monkeypatch.setattr(sc, "write_episode_outline", exploding_outline)
    with pytest.raises(_Boom):
        await series_mod.generate_series(
            settings, "t", episodes=2, episode_sec=300, run_dir=root,
        )
    # 增量落盘: 规划 + 第 1 集完整产物 + 第 2 集无(大纲前就崩了)
    assert (root / "series.json").exists()
    assert (root / "planning" / "ep01" / "script.json").exists()
    assert (root / "planning" / "ep01" / "drafts" / "ch01.json").exists()
    assert not (root / "planning" / "ep02").exists()

    # resume: 只应补第 2 集(1 次大纲 + 1 次写章), 不再调 plan/第 1 集
    calls.clear()
    monkeypatch.setattr(sc, "write_episode_outline", real_outline)
    _, scripts = await series_mod.generate_series(
        settings, "t", episodes=2, episode_sec=300, run_dir=root, resume=True,
    )
    assert calls == ["outline2", "write:集2/e2c1"]
    assert [len(s.scenes) for s in scripts] == [2, 1]


async def test_restore_state_mid_episode(tmp_path):
    """第 2 集写到一半(outline + 2 章草稿, 缺 script.json): 重建到该集第 3 章。"""
    from interview_video import series as series_mod

    root = tmp_path / "run"
    root.mkdir(parents=True)
    plan = SeriesPlan(
        theme="t", title="系列", synopsis="圣经",
        episodes=[EpisodePlan(index=1, title="集1"), EpisodePlan(index=2, title="集2")],
    )
    (root / "series.json").write_text(plan.model_dump_json(), encoding="utf-8")

    # 第 1 集完整
    ep1 = root / "planning" / "ep01"
    ep1.mkdir(parents=True)
    (ep1 / "script.json").write_text(
        Script(title="集1", scenes=[_scene("a")]).model_dump_json(), encoding="utf-8"
    )
    # 第 2 集: 大纲 3 章, 写完 2 章
    ep2 = root / "planning" / "ep02"
    (ep2 / "drafts").mkdir(parents=True)
    (ep2 / "outline.json").write_text(
        EpisodeOutline(episode_index=2, title="集2", chapters=[
            sc.ChapterSpec(title="c1", est_sec=150),
            sc.ChapterSpec(title="c2", est_sec=150),
            sc.ChapterSpec(title="c3", est_sec=150),
        ]).model_dump_json(),
        encoding="utf-8",
    )
    for i in (1, 2):
        (ep2 / "drafts" / f"ch{i:02d}.json").write_text(
            ChapterDraft(scenes=[_scene(f"s{i}")], summary=f"摘要{i}").model_dump_json(),
            encoding="utf-8",
        )

    state = series_mod._restore_state(root, episodes=2)
    assert state["ep_idx"] == 1  # 第 2 集(0 起)
    assert state["ch_idx"] == 2  # 前 2 章已完成
    assert len(state["scripts"]) == 1
    assert [s.header for s in state["scenes"]] == ["s1", "s2"]
    assert "摘要1" in state["prev_summary"] and "摘要2" in state["prev_summary"]
