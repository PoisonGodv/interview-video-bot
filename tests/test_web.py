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
    assert d["doubao_configured"] is False and d["bili_configured"] is False
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


def test_job_stage_progress_not_lagging(portal_app, monkeypatch):
    """节点回调是"刚完成"语义: active 应指向下一个节点, 首节点运行中即为 active。"""
    import asyncio

    snapshots: list[tuple[list[str], str | None]] = []
    gate = asyncio.Event()

    async def fake_execute(settings, out_dir, industry, topic_title, script_file, on_stage):
        await gate.wait()
        on_stage("gen_script")
        await asyncio.sleep(0.3)
        for node, _ in STAGES[1:]:
            on_stage(node)
        return _write_manifest(out_dir, settings, industry)

    monkeypatch.setattr("interview_video.web._execute_pipeline", fake_execute)
    app, scheduled = portal_app
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", json={"industry": "护理"}).json()["job_id"]
        _drive(client, scheduled)
        time.sleep(0.1)
        j = client.get(f"/api/jobs/{job_id}").json()
        snapshots.append((j["done_stages"], j["current_stage"]))
        client.portal.call(gate.set)
        time.sleep(0.15)
        j = client.get(f"/api/jobs/{job_id}").json()
        snapshots.append((j["done_stages"], j["current_stage"]))
        final = _wait_job(client, job_id)
    assert snapshots[0] == ([], "gen_script")
    assert snapshots[1] == (["gen_script"], "tts")
    assert final["current_stage"] is None and len(final["done_stages"]) == len(STAGES)


def test_doctor_endpoint(client, monkeypatch):
    from interview_video import doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "check_chromium", lambda: doctor_mod.Check("chromium", True, "x"))
    d = client.get("/api/doctor").json()
    names = [c["name"] for c in d["checks"]]
    assert "ffmpeg" in names and "LLM_API_KEY" in names


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
        err_logs = list(settings.out_root.rglob("error-*.log"))
        assert err_logs and "TTS boom" in err_logs[0].read_text("utf-8")
        assert err_logs[0].read_text("utf-8").startswith("stage=gen_script")
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


# ---------- 脚本审改 → 重新生成 ----------

def _sample_script_dict() -> dict:
    return {"title": "改后标题", "scenes": [{"header": "开场", "narration": "大家好。"}]}


def test_script_get_put_and_validation(portal_app, monkeypatch):
    monkeypatch.setattr("interview_video.web._execute_pipeline", _fake_execute)
    app, scheduled = portal_app
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", json={"industry": "汽修"}).json()["job_id"]
        _drive(client, scheduled)
        name = _wait_job(client, job_id)["project"]

        # 老项目无 script.json: 回落到 project.json 里的脚本
        assert client.get(f"/api/projects/{name}/script").json()["title"] == "测试标题"

        r = client.put(f"/api/projects/{name}/script", json={"scenes": "bad"})
        assert r.status_code == 422
        assert "脚本格式不合法" in r.json()["detail"] and "title" in r.json()["detail"]
        r = client.put(f"/api/projects/{name}/script", json={"title": "x", "scenes": []})
        assert r.status_code == 422

        assert client.put(f"/api/projects/{name}/script", json=_sample_script_dict()).json()["ok"]
        assert client.get(f"/api/projects/{name}/script").json()["title"] == "改后标题"

        assert client.get("/api/projects/..%2Fx/script").status_code in (400, 404)
        assert client.put("/api/projects/nope/script", json=_sample_script_dict()).status_code == 404


def test_stop_after_script_then_regenerate(portal_app, settings, monkeypatch):
    """只生成脚本 → 作品库出现草稿 → 改脚本 → regenerate 复用同目录与改后的脚本。"""
    calls: dict = {}

    async def fake_script_stage(settings_, out_dir, industry, topic_title, script_file):
        out_dir.mkdir(parents=True, exist_ok=True)
        s = Script(title="草稿", scenes=[{"header": "开场", "narration": "你好。"}])
        (out_dir / "script.json").write_text(s.model_dump_json(), encoding="utf-8")
        (out_dir / "topic.json").write_text(
            Topic(title="草稿", industry=industry).model_dump_json(), encoding="utf-8")
        return s

    async def fake_execute(settings_, out_dir, industry, topic_title, script_file,
                           on_stage, script=None):
        calls.update(out_dir=out_dir, industry=industry, script=script, voice=settings_.tts_voice)
        for node, _ in STAGES:
            on_stage(node)
        return _write_manifest(out_dir, settings_, industry)

    monkeypatch.setattr("interview_video.web._execute_script_stage", fake_script_stage)
    monkeypatch.setattr("interview_video.web._execute_pipeline", fake_execute)
    app, scheduled = portal_app
    with TestClient(app) as client:
        r = client.post("/api/jobs", json={"industry": "护理", "stop_after": "script"})
        _drive(client, scheduled)
        job = _wait_job(client, r.json()["job_id"])
        assert job["status"] == "done" and job["stop_after"] == "script"
        assert not calls, "只生成脚本不应跑媒体流水线"
        name = job["project"]
        proj_dir = settings.out_root.resolve() / name
        assert not (proj_dir / "project.json").exists()

        draft = [p for p in client.get("/api/projects").json()["projects"] if p["name"] == name]
        assert draft and draft[0]["draft"] is True and draft[0]["video_url"] is None
        assert client.get(f"/api/projects/{name}").json()["script"]["title"] == "草稿"
        assert client.post(f"/api/projects/{name}/upload").status_code == 404

        client.put(f"/api/projects/{name}/script", json=_sample_script_dict())
        r = client.post(f"/api/projects/{name}/regenerate",
                        json={"provider": "edge", "voice": "zh-CN-XiaoxiaoNeural"})
        assert r.status_code == 202
        # 单飞: 重新生成任务未结束前, 新任务/再次重新生成都被拒
        assert client.post(f"/api/projects/{name}/regenerate").status_code == 409
        assert client.post("/api/jobs", json={}).status_code == 409
        assert client.put(f"/api/projects/{name}/script",
                          json=_sample_script_dict()).status_code == 409
        _drive(client, scheduled)
        job = _wait_job(client, r.json()["job_id"])
        assert job["status"] == "done", job["error"]
        assert job["project"] == name
        assert calls["out_dir"] == proj_dir
        assert calls["script"].title == "改后标题"
        assert calls["industry"] == "护理"  # 沿用 topic.json 记录的行业
        assert calls["voice"] == "zh-CN-XiaoxiaoNeural"
        assert (proj_dir / "project.json").exists()


def test_regenerate_guards(client):
    assert client.post("/api/projects/nope/regenerate").status_code == 404
    assert client.post("/api/projects/..%2Fx/regenerate").status_code in (400, 404)
    r = client.post("/api/jobs", json={"mode": "series", "stop_after": "script"})
    assert r.status_code == 400


# ---------- 活动任务 / 系列续跑 ----------

def test_active_job_and_409_carries_job_id(portal_app, monkeypatch):
    monkeypatch.setattr("interview_video.web._execute_pipeline", _fake_execute)
    app, scheduled = portal_app
    with TestClient(app) as client:
        assert client.get("/api/jobs/active").json() is None
        job_id = client.post("/api/jobs", json={"topic_title": "t"}).json()["job_id"]
        active = client.get("/api/jobs/active").json()
        assert active["id"] == job_id and active["status"] == "running"
        r = client.post("/api/jobs", json={})
        assert r.status_code == 409
        assert r.json()["job_id"] == job_id and r.json()["detail"]
        _drive(client, scheduled)
        _wait_job(client, job_id)
        assert client.get("/api/jobs/active").json() is None


def _write_series_root(settings: Settings, name: str, n_eps: int, done: int) -> Path:
    from interview_video.models import EpisodePlan, SeriesPlan

    root = settings.out_root.resolve() / name
    root.mkdir(parents=True, exist_ok=True)
    plan = SeriesPlan(
        theme="程序员面试", title="通关", synopsis="s",
        episodes=[EpisodePlan(index=i + 1, title=f"第{i + 1}集", target_sec=600)
                  for i in range(n_eps)],
    )
    (root / "series.json").write_text(plan.model_dump_json(), encoding="utf-8")
    for i in range(done):
        _write_manifest(root / f"ep{i + 1:02d}-第{i + 1}集", settings, "程序员面试")
    return root


def test_series_list_and_resume(portal_app, monkeypatch, settings):
    from interview_video.models import Scene, Script

    root = _write_series_root(settings, "程序员面试-series-20260101-0000", 2, 1)
    _write_series_root(settings, "done-series-20260101-0000", 1, 1)
    calls: dict = {}

    async def fake_generate_series(settings_, theme, episodes, episode_sec,
                                   on_event=None, run_dir=None, resume=False):
        calls.update(theme=theme, episodes=episodes, episode_sec=episode_sec,
                     run_dir=run_dir, resume=resume)
        from interview_video.models import SeriesPlan

        plan = SeriesPlan.model_validate_json((run_dir / "series.json").read_text("utf-8"))
        return plan, [Script(title=ep.title, scenes=[Scene(header="h", narration="n。")])
                      for ep in plan.episodes]

    executed: list[str] = []

    async def fake_execute(settings_, out_dir, industry, topic_title, script_file,
                           on_stage, script=None):
        executed.append(out_dir.name)
        return _write_manifest(out_dir, settings_, industry)

    monkeypatch.setattr("interview_video.series.generate_series", fake_generate_series)
    monkeypatch.setattr("interview_video.web._execute_pipeline", fake_execute)
    app, scheduled = portal_app
    with TestClient(app) as client:
        series = {s["root"]: s for s in client.get("/api/series").json()["series"]}
        assert series[root.name]["complete"] is False
        assert series[root.name]["done_episodes"] == 1
        assert series["done-series-20260101-0000"]["complete"] is True

        r = client.post(f"/api/series/{root.name}/resume")
        assert r.status_code == 202
        _drive(client, scheduled)
        job = _wait_job(client, r.json()["job_id"])
        assert job["status"] == "done", job["error"]
        assert job["project"] == root.name
        assert calls == {"theme": "程序员面试", "episodes": 2, "episode_sec": 600,
                         "run_dir": root, "resume": True}
        assert executed == ["ep02-第2集"]  # 已出片的第 1 集跳过
        series = {s["root"]: s for s in client.get("/api/series").json()["series"]}
        assert series[root.name]["complete"] is True


def test_series_resume_guards(client, settings):
    assert client.post("/api/series/nope/resume").status_code == 404
    assert client.post("/api/series/..%2F..%2Fetc/resume").status_code in (400, 404)
    assert client.post("/api/series/a.b/resume").status_code == 400
    (settings.out_root / "plain").mkdir(parents=True)
    assert client.post("/api/series/plain/resume").status_code == 404


def test_corrupt_draft_does_not_break_library(client, settings):
    good = settings.out_root.resolve() / "good"
    _write_manifest(good, settings, "互联网")
    bad = settings.out_root.resolve() / "bad"
    bad.mkdir(parents=True)
    (bad / "script.json").write_text('{"title": "x", "scenes": [', encoding="utf-8")
    r = client.get("/api/projects")
    assert r.status_code == 200
    assert [p["name"] for p in r.json()["projects"]] == ["good"]
    assert client.get("/api/projects/bad/script").status_code == 422


def test_put_script_blocked_for_episode_of_running_series(portal_app, settings):
    root = _write_series_root(settings, "s-series-20260101-0000", 2, 1)
    ep = next(root.glob("ep01-*"))
    app, scheduled = portal_app
    with TestClient(app) as client:
        job_id = client.post(f"/api/series/{root.name}/resume", json={}).json()["job_id"]
        # 系列任务锁的是根目录, 其下分集的脚本同样不可改
        r = client.put(f"/api/projects/{ep.name}/script", json=_sample_script_dict())
        assert r.status_code == 409 and r.json()["job_id"] == job_id
        assert scheduled


def _sleeper_execute(pid_file: Path, calls: list | None = None):
    """假流水线: 在工作线程里跑一个忽略 SIGTERM 的长子进程(模拟卡住的 ffmpeg)。"""
    from proc_helpers import sleeper_cmd

    from interview_video import procs

    async def execute(settings_, out_dir, industry, topic_title, script_file,
                      on_stage, script=None):
        if calls is not None:
            calls.append(out_dir.name)
        out_dir.mkdir(parents=True, exist_ok=True)
        await procs.run_in_thread(
            procs.run, sleeper_cmd(pid_file, ignore_term=True), cwd=out_dir, timeout=60
        )

    return execute


def _real_task_scheduler(fn, *a):
    import asyncio

    return asyncio.get_running_loop().create_task(fn(*a))


def test_cancel_kills_child_process_and_holds_lock_until_exit(settings, monkeypatch, tmp_path):
    """取消: 子进程被 terminate→kill; 退出前锁仍被占(409), 退出后才释放。"""
    from proc_helpers import alive, wait_pid

    from interview_video import procs

    monkeypatch.setattr(procs, "TERMINATE_GRACE", 1.0)
    pid_file = tmp_path / "pid"
    monkeypatch.setattr("interview_video.web._execute_pipeline", _sleeper_execute(pid_file))
    app = create_app(settings, scheduler=_real_task_scheduler)
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", json={"industry": "互联网"}).json()["job_id"]
        pid = wait_pid(pid_file)
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        # 宽限期内: 子进程(忽略 SIGTERM)仍活着 → 任务仍占锁, 新任务 409
        j = client.get(f"/api/jobs/{job_id}").json()
        assert j["status"] == "running" and j["cancelling"] and not j["cancellable"]
        assert client.get("/api/jobs/active").json()["id"] == job_id
        assert client.post("/api/jobs", json={"industry": "x"}).status_code == 409

        job = _wait_job(client, job_id, timeout=10)
        assert job["status"] == "cancelled" and "取消" in job["error"]
        assert not alive(pid)
        assert client.get("/api/jobs/active").json() is None
        monkeypatch.setattr("interview_video.web._execute_pipeline", _fake_execute)
        assert client.post("/api/jobs", json={"industry": "x"}).status_code == 202


def test_cancel_series_stops_between_episodes(settings, monkeypatch, tmp_path):
    """系列取消: 第 1 集媒体合成中被取消 → 子进程被杀, 第 2 集不再开跑, 规划保留可续跑。"""
    from proc_helpers import alive, wait_pid

    from interview_video import procs
    from interview_video.models import EpisodePlan, Scene, SeriesPlan

    monkeypatch.setattr(procs, "TERMINATE_GRACE", 0.3)
    plan = SeriesPlan(theme="t", title="T", synopsis="s", episodes=[
        EpisodePlan(index=1, title="一"), EpisodePlan(index=2, title="二")])

    async def fake_generate_series(settings_, theme, episodes, episode_sec,
                                   on_event=None, run_dir=None, resume=False):
        return plan, [Script(title=e.title, scenes=[Scene(header="h", narration="n。")])
                      for e in plan.episodes]

    pid_file = tmp_path / "pid"
    calls: list[str] = []
    monkeypatch.setattr("interview_video.series.generate_series", fake_generate_series)
    monkeypatch.setattr("interview_video.web._execute_pipeline", _sleeper_execute(pid_file, calls))
    app = create_app(settings, scheduler=_real_task_scheduler)
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", json={"mode": "series", "theme": "t"}).json()["job_id"]
        pid = wait_pid(pid_file)
        client.post(f"/api/jobs/{job_id}/cancel")
        job = _wait_job(client, job_id, timeout=10)
        assert job["status"] == "cancelled"
        assert not alive(pid)
        assert len(calls) == 1 and calls[0].startswith("ep01-")
        root = settings.out_root.resolve() / job["project"]
        assert (root / "series.json").exists()
        st = {s["root"]: s for s in client.get("/api/series").json()["series"]}
        assert st[root.name]["complete"] is False
