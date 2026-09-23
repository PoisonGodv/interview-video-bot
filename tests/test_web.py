"""Web 操作台测试: TestClient + 打桩流水线(不联网/不起浏览器)。"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from interview_video.config import Settings
from interview_video.models import ProjectManifest, Script, Topic
from interview_video.web import STAGES, create_app


@pytest.fixture()
def settings(tmp_path) -> Settings:
    # _env_file=None: 不读项目根真实 .env, 保证测试无 key 状态确定
    return Settings(_env_file=None, out_root=tmp_path / "output")


@pytest.fixture()
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture()
def portal_app(settings):
    """注入收集式调度器的 app: 任务不自动跑, 测试用 portal.start_task_soon 驱动,
    规避 TestClient 请求结束即取消 request 内 create_task 的行为。"""
    scheduled: list = []
    app = create_app(settings, scheduler=lambda fn, *a: scheduled.append((fn, a)))
    return app, scheduled


def _drive(client: TestClient, scheduled: list) -> None:
    """把最近一次被调度的任务放到 portal 上跑(跨请求存活)。"""
    fn, a = scheduled.pop(0)
    client.portal.start_task_soon(fn, *a)


def _write_manifest(out_dir: Path, settings: Settings, industry: str) -> ProjectManifest:
    out_dir.mkdir(parents=True, exist_ok=True)
    m = ProjectManifest(
        created_at=datetime.now(),
        industry=industry,
        script=Script(title="测试标题", scenes=[]),
        video=out_dir / "final.mp4",
        cover=out_dir / "cover.png",
        voice=settings.tts_voice,
        duration_total=1.0,
    )
    (out_dir / "project.json").write_text(m.model_dump_json(indent=2), encoding="utf-8")
    return m


async def _fake_execute(settings, out_dir, industry, topic_title, script_file, on_stage):
    """假流水线: 过一遍节点回调并落盘 project.json。"""
    for node, _ in STAGES:
        on_stage(node)
    return _write_manifest(out_dir, settings, industry)


# ---------- 页面与配置 ----------

def test_index(client):
    r = client.get("/")
    assert r.status_code == 200 and "操作台" in r.text


def test_settings_endpoint(client):
    d = client.get("/api/settings").json()
    assert d["llm_configured"] is False
    assert d["tts_provider"] == "edge"
    assert "edge" in d["voices"] and "doubao" in d["voices"]


# ---------- 选题 ----------

def test_topics_requires_llm_key(client):
    r = client.post("/api/topics", json={"industry": "护理", "n": 3})
    assert r.status_code == 400 and "LLM_API_KEY" in r.json()["detail"]


def test_topics_success(client, monkeypatch):
    monkeypatch.setattr("interview_video.llm.get_chat_model", lambda s: object())

    async def fake_topics(llm, industry, n, avoid=None):
        return [Topic(title=f"选题{i}", industry=industry, hook="钩子") for i in range(n)]

    monkeypatch.setattr("interview_video.chains.topic.generate_topics", fake_topics)
    d = client.post("/api/topics", json={"industry": "护理", "n": 3}).json()
    assert len(d["topics"]) == 3 and d["topics"][0]["industry"] == "护理"


# ---------- 生成任务 ----------

def _wait_job(client, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("任务超时未结束")


def test_job_lifecycle(portal_app, monkeypatch):
    monkeypatch.setattr("interview_video.web._execute_pipeline", _fake_execute)
    app, scheduled = portal_app
    with TestClient(app) as client:
        r = client.post("/api/jobs", json={"industry": "护理", "topic_title": "转行面试"})
        assert r.status_code == 202
        _drive(client, scheduled)
        job = _wait_job(client, r.json()["job_id"])
        assert job["status"] == "done"
        assert len(job["done_stages"]) == len(STAGES)
        assert job["project"]

        # 作品库出现该项目
        projects = client.get("/api/projects").json()["projects"]
        assert any(p["name"] == job["project"] for p in projects)


def test_job_single_flight(portal_app, monkeypatch):
    import asyncio

    async def slow_execute(settings, out_dir, industry, topic_title, script_file, on_stage):
        await asyncio.sleep(30)

    monkeypatch.setattr("interview_video.web._execute_pipeline", slow_execute)
    app, scheduled = portal_app
    with TestClient(app) as client:
        r1 = client.post("/api/jobs", json={"industry": "互联网"})
        assert r1.status_code == 202
        _drive(client, scheduled)
        time.sleep(0.3)  # 等任务跑起来
        r2 = client.post("/api/jobs", json={"industry": "互联网"})
        assert r2.status_code == 409
        j1 = client.get(f"/api/jobs/{r1.json()['job_id']}").json()
        assert j1["status"] == "running"


def test_job_bad_provider(client):
    r = client.post("/api/jobs", json={"provider": "xxx"})
    assert r.status_code == 400


def test_job_not_found(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_job_failure_sets_error_and_releases_lock(portal_app, settings, monkeypatch):
    """任务失败: status=failed + error 带原因, 锁释放后新任务可提交(202)。"""
    async def boom(settings, out_dir, industry, topic_title, script_file, on_stage):
        raise RuntimeError("TTS boom")

    monkeypatch.setattr("interview_video.web._execute_pipeline", boom)
    app, scheduled = portal_app
    with TestClient(app) as client:
        r1 = client.post("/api/jobs", json={"industry": "互联网"})
        assert r1.status_code == 202
        _drive(client, scheduled)
        job = _wait_job(client, r1.json()["job_id"])
        assert job["status"] == "failed"
        assert "TTS boom" in job["error"]
        # 失败现场落盘
        err_logs = list(settings.out_root.glob("error-*.log"))
        assert err_logs and "TTS boom" in err_logs[0].read_text("utf-8")
        # 锁已释放: 新任务能再进(不再 409)
        monkeypatch.setattr("interview_video.web._execute_pipeline", _fake_execute)
        r2 = client.post("/api/jobs", json={"industry": "互联网"})
        assert r2.status_code == 202


def test_job_cancel(settings, monkeypatch):
    """取消接口: 运行中任务可取消, 状态 cancelled 且锁释放。"""
    import asyncio

    async def slow_execute(settings, out_dir, industry, topic_title, script_file, on_stage):
        await asyncio.sleep(30)

    monkeypatch.setattr("interview_video.web._execute_pipeline", slow_execute)

    # 调度器在 handler(跑在 portal 的 asyncio loop)内直接 create_task,
    # 与 uvicorn 下"任务跨请求存活"等价, 拿到真 Task 句柄供取消。
    def scheduler(fn, *a):
        return asyncio.get_running_loop().create_task(fn(*a))

    app = create_app(settings, scheduler=scheduler)
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", json={"industry": "互联网"}).json()["job_id"]
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "running"

        rc = client.post(f"/api/jobs/{job_id}/cancel")
        assert rc.status_code == 200
        job = _wait_job(client, job_id, timeout=5)
        assert job["status"] == "cancelled"
        assert "取消" in job["error"]
        # 锁已释放: 新任务可提交; 已结束的任务再取消 → 409
        assert client.post("/api/jobs", json={"industry": "x"}).status_code == 202
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_cancel_not_found(client):
    assert client.post("/api/jobs/nope/cancel").status_code == 404


# ---------- 元数据与上传 ----------

def test_meta_edit_and_upload_guard(portal_app, monkeypatch):
    monkeypatch.setattr("interview_video.web._execute_pipeline", _fake_execute)
    app, scheduled = portal_app
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", json={"industry": "汽修"}).json()["job_id"]
        _drive(client, scheduled)
        name = _wait_job(client, job_id)["project"]

        r = client.put(f"/api/projects/{name}/meta",
                       json={"title": "新标题", "desc": "新简介", "tags": ["面试", "汽修"]})
        assert r.json()["ok"] is True
        detail = client.get(f"/api/projects/{name}").json()
        assert detail["meta"]["title"] == "新标题"
        assert detail["meta"]["tags"] == ["面试", "汽修"]

        # 未配置 cookie → 上传被拒并给出指引
        r = client.post(f"/api/projects/{name}/upload")
        assert r.status_code == 400 and "BILI_SESSDATA" in r.json()["detail"]


def test_project_name_traversal_blocked(client):
    assert client.get("/api/projects/..%2F..%2Fetc").status_code in (400, 404)


# ---------- 系列模式 ----------

def test_series_job(portal_app, monkeypatch, settings):
    """系列任务: 假多 agent 生成 2 集 → 每集各跑一次假流水线 → 分集目录落盘。"""
    from interview_video.chains.series import ChapterSpec
    from interview_video.models import EpisodeOutline, EpisodePlan, Scene, Script, SeriesPlan

    plan = SeriesPlan(
        theme="程序员面试", title="通关", synopsis="s",
        episodes=[EpisodePlan(index=1, title="第一集"),
                  EpisodePlan(index=2, title="第二集")],
    )

    async def fake_generate_series(settings, theme, episodes, episode_sec,
                                   on_event=None, run_dir=None, resume=False):
        scripts = []
        for i, ep in enumerate(plan.episodes):
            outline = EpisodeOutline(episode_index=ep.index, title=ep.title,
                                     chapters=[ChapterSpec(title="c", est_sec=150)])
            if on_event:
                on_event("outline_done", ep_idx=i, outline=outline)
                on_event("chapter_done", ep_idx=i, ch_idx=0, ch_total=1, draft=None)
            script = Script(title=ep.title, scenes=[Scene(header="开场", narration="你好。")])
            if on_event:
                on_event("script_done", ep_idx=i, script=script)
            scripts.append(script)
        return plan, scripts

    async def fake_execute(settings_, out_dir, industry, topic_title, script_file,
                           on_stage, script=None):
        assert script is not None, "系列模式应预制脚本直通"
        return _write_manifest(out_dir, settings_, industry)

    monkeypatch.setattr("interview_video.series.generate_series", fake_generate_series)
    monkeypatch.setattr("interview_video.web._execute_pipeline", fake_execute)
    app, scheduled = portal_app
    with TestClient(app) as client:
        r = client.post("/api/jobs", json={
            "mode": "series", "theme": "程序员面试", "episodes": 2, "duration_min": 10,
        })
        assert r.status_code == 202
        _drive(client, scheduled)
        job = _wait_job(client, r.json()["job_id"])
        assert job["status"] == "done", job["error"]
        assert job["series"]["episode"] == 2
        root = settings.out_root.resolve() / job["project"]
        assert (root / "series.json").exists()
        eps = [d for d in root.iterdir() if d.is_dir() and (d / "project.json").exists()]
        assert len(eps) == 2
        # 分集出现在作品库
        names = [p["name"] for p in client.get("/api/projects").json()["projects"]]
        assert any(n.startswith("ep01-") for n in names)
