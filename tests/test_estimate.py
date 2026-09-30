"""费用预估 + 单次运行 LLM 硬上限测试: 桩 transport/假链, 不联网。"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from interview_video.chains import series as sc
from interview_video.cli import app
from interview_video.config import Settings
from interview_video.estimate import (
    SERIES_MAX_EPISODE_MIN,
    SERIES_MAX_EPISODES,
    estimate_series,
    validate_series_bounds,
)
from interview_video.llm import HTTPChatClient, LLMBudgetExceededError
from interview_video.llm_audit import RunBudget, current_budget, run_budget
from interview_video.models import ChapterDraft, EpisodeOutline, EpisodePlan, Scene, SeriesPlan
from interview_video.web import create_app

runner = CliRunner()


def _settings(tmp_path=None, **kw) -> Settings:
    if tmp_path is not None:
        kw.setdefault("out_root", tmp_path / "output")
    return Settings(_env_file=None, **kw)


# ---------- 预估 ----------

def test_estimate_call_structure_and_quantities():
    est = estimate_series(_settings(series_chapter_sec=150), episodes=2, duration_min=10)
    # 10 分钟 / 150s = 4 章; 每集 编剧1 + 4×(写手+审稿) + 元数据1 = 10; 加策划 1
    assert est.chapters_per_episode == 4
    assert est.llm_calls == 1 + 2 * 10
    assert est.llm_calls_worst == est.llm_calls * 2
    assert est.tts_chars == 2 * 10 * 270
    assert est.input_tokens > 0 and est.output_tokens > 0
    assert est.total_cost is None
    assert any("未配置单价" in line for line in est.lines())

    no_review = estimate_series(
        _settings(series_chapter_sec=150, series_review=False), episodes=2, duration_min=10
    )
    assert no_review.llm_calls == 1 + 2 * 6
    assert no_review.total_tokens < est.total_tokens


def test_estimate_money_only_when_prices_configured():
    est = estimate_series(
        _settings(llm_price_input_per_1k=0.002, llm_price_output_per_1k=0.008,
                  tts_price_per_10k_chars=2.0),
        episodes=1, duration_min=5,
    )
    expected_llm = est.input_tokens / 1000 * 0.002 + est.output_tokens / 1000 * 0.008
    assert est.llm_cost == pytest.approx(expected_llm)
    assert est.tts_cost == pytest.approx(est.tts_chars / 10000 * 2.0)
    assert est.total_cost == pytest.approx(expected_llm + est.tts_cost)
    assert any("费用: 约" in line for line in est.lines())


def test_series_bounds():
    assert validate_series_bounds(3, 15) is None
    assert validate_series_bounds(0, 15)
    assert validate_series_bounds(SERIES_MAX_EPISODES + 1, 15)
    assert validate_series_bounds(3, SERIES_MAX_EPISODE_MIN + 1)
    assert validate_series_bounds(3, 1)


def test_price_settings_blank_is_none():
    s = Settings(_env_file=None, llm_price_input_per_1k="  # 说明", llm_max_calls_per_run="")
    assert s.llm_price_input_per_1k is None and s.llm_max_calls_per_run is None


# ---------- 运行预算 ----------

def test_run_budget_scoping_and_nesting():
    assert current_budget() is None
    with run_budget(_settings()) as b:
        assert b is None  # 未配置上限: 不计数
    with run_budget(_settings(llm_max_calls_per_run=5)) as outer:
        assert isinstance(outer, RunBudget)
        with run_budget(_settings(llm_max_calls_per_run=99)) as inner:
            assert inner is outer  # 系列内逐集流水线沿用外层计数
    assert current_budget() is None


def _patch_ok_transport(monkeypatch, total_tokens=100):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": total_tokens // 2, "completion_tokens": total_tokens // 2,
                      "total_tokens": total_tokens},
        })

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return calls


def _client(**kw) -> HTTPChatClient:
    return HTTPChatClient(model="m", api_key="k", base_url="https://x.test/v1", **kw)


async def test_budget_max_calls_blocks_before_request(monkeypatch):
    calls = _patch_ok_transport(monkeypatch)
    fallback = _client()
    client = _client(fallbacks=[fallback])
    with run_budget(_settings(llm_max_calls_per_run=2)):
        await client.chat("a")
        await client.chat("b")
        with pytest.raises(LLMBudgetExceededError, match="LLM_MAX_CALLS_PER_RUN=2"):
            await client.chat("c")
    assert calls["n"] == 2  # 超限不发请求、不切备用模型


async def test_budget_max_tokens(monkeypatch):
    calls = _patch_ok_transport(monkeypatch, total_tokens=600)
    client = _client()
    with run_budget(_settings(llm_max_tokens_per_run=1000)) as budget:
        await client.chat("a")
        await client.chat("b")  # 600 < 1000, 放行
        assert budget.tokens == 1200
        with pytest.raises(LLMBudgetExceededError, match="LLM_MAX_TOKENS_PER_RUN"):
            await client.chat("c")
    assert calls["n"] == 2


async def test_budget_error_not_swallowed_by_invoke_or_review(monkeypatch):
    """预算错误不进修复重问/回退解析, 审稿降级也不吞它。"""
    from interview_video.chains._invoke import invoke_structured

    _patch_ok_transport(monkeypatch)
    client = _client()
    with run_budget(_settings(llm_max_calls_per_run=1)):
        await client.chat("warmup")
        with pytest.raises(LLMBudgetExceededError):
            await invoke_structured("p", client, {}, SeriesPlan, sc.parse_series_plan_json)
        with pytest.raises(LLMBudgetExceededError):
            await sc.review_chapter(
                client, ChapterDraft(scenes=[], summary=""), sc.ChapterSpec(title="c"), ""
            )


def _fake_series_chains(monkeypatch, client):
    """每个假 agent 各真实调用一次 HTTP 客户端(走预算计数), 返回固定产物。"""
    plan = SeriesPlan(theme="t", title="系列", synopsis="s", episodes=[
        EpisodePlan(index=1, title="集1", target_sec=150),
        EpisodePlan(index=2, title="集2", target_sec=150),
    ])

    async def fake_plan(llm, theme, episodes, episode_sec):
        await llm.chat("plan")
        return plan

    async def fake_outline(llm, plan_, episode, chapter_sec):
        await llm.chat("outline")
        return EpisodeOutline(episode_index=episode.index, title=episode.title,
                              chapters=[sc.ChapterSpec(title="c", est_sec=150)])

    async def fake_write(llm, plan_, outline, chapter, ch_no, prev, settings):
        await llm.chat("chapter")
        return ChapterDraft(scenes=[Scene(header="h", narration="口播。")], summary="摘要")

    monkeypatch.setattr(sc, "plan_series", fake_plan)
    monkeypatch.setattr(sc, "write_episode_outline", fake_outline)
    monkeypatch.setattr(sc, "write_chapter", fake_write)
    monkeypatch.setattr("interview_video.llm.get_chat_model", lambda s: client)


async def test_series_budget_stops_cleanly_and_resume_works(monkeypatch, tmp_path):
    from interview_video import series as series_mod

    calls = _patch_ok_transport(monkeypatch)
    _fake_series_chains(monkeypatch, _client())
    root = tmp_path / "run"
    # 策划1 + 集1(编剧1+章1) = 3 次; 第 4 次(集2 编剧)超限
    tight = _settings(series_review=False, llm_max_calls_per_run=3)
    with pytest.raises(LLMBudgetExceededError):
        await series_mod.generate_series(tight, "t", episodes=2, episode_sec=150, run_dir=root)
    assert calls["n"] == 3
    assert (root / "planning" / "ep01" / "script.json").exists()
    assert not (root / "planning" / "ep02").exists()

    # 续跑重新计数: 只补集2(编剧+章 = 2 次), 上限 3 足够
    _, scripts = await series_mod.generate_series(
        tight, "t", episodes=2, episode_sec=150, run_dir=root, resume=True,
    )
    assert len(scripts) == 2 and calls["n"] == 5


# ---------- CLI ----------

def test_cli_series_dry_run_prints_estimate_without_llm(monkeypatch, tmp_path):
    monkeypatch.setattr("interview_video.cli.load_settings", lambda: _settings(tmp_path))
    ran = []
    monkeypatch.setattr("interview_video.cli._series_main", lambda *a, **k: ran.append(a))
    r = runner.invoke(app, ["series", "主题", "-e", "2", "-d", "10", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "费用预估" in r.output and "LLM: 约 21 次调用" in r.output
    assert not ran


def test_cli_series_requires_confirmation(monkeypatch, tmp_path):
    monkeypatch.setattr("interview_video.cli.load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("interview_video.doctor.preflight", lambda s: None)
    ran = []

    async def fake_main(*a, **k):
        ran.append(a)

    monkeypatch.setattr("interview_video.cli._series_main", fake_main)
    r = runner.invoke(app, ["series", "主题", "-e", "2"], input="n\n")
    assert r.exit_code != 0 and not ran
    r = runner.invoke(app, ["series", "主题", "-e", "2"], input="y\n")
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["series", "主题", "-e", "2", "--yes"])
    assert r.exit_code == 0 and len(ran) == 2


def test_cli_series_rejects_absurd_bounds(monkeypatch, tmp_path):
    monkeypatch.setattr("interview_video.cli.load_settings", lambda: _settings(tmp_path))
    r = runner.invoke(app, ["series", "主题", "-e", "50", "--yes"])
    assert r.exit_code == 2 and "集数需在" in r.output
    r = runner.invoke(app, ["series", "主题", "-d", "120", "--yes"])
    assert r.exit_code == 2 and "每集时长" in r.output


# ---------- Web ----------

def test_web_series_estimate_endpoints(tmp_path):
    client = TestClient(create_app(_settings(tmp_path, llm_max_calls_per_run=50)), base_url="http://localhost")
    r = client.get("/api/series/estimate", params={"episodes": 2, "duration_min": 10})
    assert r.status_code == 200
    d = r.json()
    assert d["llm_calls"] == 21 and d["tts_chars"] == 5400 and d["lines"]
    assert d["limits"]["max_calls"] == 50
    r = client.post("/api/series/estimate",
                    json={"episodes": 1, "duration_min": 5, "provider": "doubao"})
    assert r.status_code == 200 and r.json()["tts_provider"] == "doubao"
    assert client.get("/api/series/estimate", params={"episodes": 99}).status_code == 422
    assert client.post("/api/series/estimate", json={"provider": "nope"}).status_code == 400
    assert client.get("/api/settings").json()["series_limits"]["max_episodes"] == SERIES_MAX_EPISODES


def test_web_job_bounds_match_cli(tmp_path):
    client = TestClient(create_app(_settings(tmp_path)), base_url="http://localhost")
    r = client.post("/api/jobs", json={"mode": "series", "episodes": SERIES_MAX_EPISODES + 1})
    assert r.status_code == 422
    r = client.post("/api/jobs", json={"mode": "series", "duration_min": SERIES_MAX_EPISODE_MIN + 1})
    assert r.status_code == 422
