"""Web 操作台: FastAPI 单页应用, 本地使用(默认绑 127.0.0.1)。

功能: 选题生成 → 一键跑流水线(节点级进度) → 作品库(播放/元数据编辑/上传 B站)。
复用 graph.build_graph 的 astream 拿节点进度; 生成任务单飞(一把锁), 避免 TTS 限流。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .models import BiliMeta, ProjectManifest, Script

# 流水线节点 → 中文阶段名(与 graph.py 的节点名一一对应)
STAGES: list[tuple[str, str]] = [
    ("gen_script", "选题与脚本"),
    ("tts", "配音合成"),
    ("render", "画面渲染"),
    ("clips", "片段合成"),
    ("subs", "字幕生成"),
    ("finalize", "视频合成"),
    ("metadata", "B站元数据"),
    ("manifest", "落盘收尾"),
]

# 前端音色下拉(值即 API/模型音色 ID)
VOICES: dict[str, list[dict[str, str]]] = {
    "edge": [
        {"id": "zh-CN-YunxiNeural", "name": "云希(男·活力)"},
        {"id": "zh-CN-YunjianNeural", "name": "云健(男·沉稳)"},
        {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓(女·温暖)"},
        {"id": "zh-CN-XiaoyiNeural", "name": "晓伊(女·活泼)"},
    ],
    "doubao": [
        {"id": "zh_male_m191_uranus_bigtts", "name": "云舟(男·豆包)"},
        {"id": "zh_female_vv_uranus_bigtts", "name": "Vivi(女·豆包)"},
    ],
}

_NAME_RE = re.compile(r"^[\w\-一-鿿]+$")  # 项目目录名白名单(防路径穿越)

_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class TopicsRequest(BaseModel):
    industry: str = "互联网"
    n: int = Field(default=8, ge=1, le=20)


class JobRequest(BaseModel):
    industry: str = "互联网"
    topic_title: str | None = None
    provider: str = "edge"           # edge | doubao
    voice: str | None = None          # 不传用该 provider 默认音色
    use_sample_script: bool = False   # 免 LLM 冒烟: 用 examples/sample_script.json
    # 系列模式(多 agent 长视频)
    mode: Literal["single", "series"] = "single"
    theme: str | None = None          # 系列主题
    episodes: int = Field(default=3, ge=1, le=20)
    duration_min: float = Field(default=15.0, ge=3.0, le=60.0)  # 每集目标时长(分钟)


class MetaRequest(BaseModel):
    title: str
    desc: str = ""
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 任务管理(单飞)
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, job_id: str, params: JobRequest) -> None:
        self.id = job_id
        self.params = params
        self.status = "running"  # running | done | failed | cancelled
        self.current_stage: str | None = None
        self.done_stages: list[str] = []
        self.error: str | None = None
        self.project: str | None = None  # out_dir 名(系列模式=系列根目录名)
        self.series: dict[str, Any] | None = None  # 系列进度 {episode,episodes,chapter,chapters,phase}
        self.usage: dict[str, int] | None = None  # LLM token 汇总(llm_calls.jsonl)
        self.task: asyncio.Task | None = None  # 后台任务句柄(取消接口用)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "current_stage": self.current_stage,
            "done_stages": self.done_stages,
            "error": self.error,
            "project": self.project,
            "series": self.series,
            "usage": self.usage,
            "cancellable": self.status == "running" and self.task is not None,
            "stages": [
                {"key": k, "label": label, "done": k in self.done_stages,
                 "active": k == self.current_stage and self.status == "running"}
                for k, label in STAGES
            ],
        }


class JobManager:
    """内存任务表 + 单飞标记(同时只允许一个生成任务, 避免 TTS 限流)。

    active 在 POST 处理器里同步置位(不等后台任务跑起来), 避免并发抢入的竞态。
    """

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.active = False


# ---------------------------------------------------------------------------
# 流水线执行(可被测试替换的接缝)
# ---------------------------------------------------------------------------

async def _execute_pipeline(
    settings: Settings,
    out_dir: Path,
    industry: str,
    topic_title: str | None,
    script_file: Path | None,
    on_stage,  # Callable[[str], None], 节点完成回调
    script: Script | None = None,  # 预制脚本(系列模式), 跳过 LLM 生成
) -> ProjectManifest:
    """build_graph + astream(updates) 跑流水线, 逐节点回调进度。"""
    from . import doctor as doctor_mod
    from . import graph as graph_mod
    from .llm_audit import LLMCallAudit

    await asyncio.to_thread(doctor_mod.preflight, settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    graph = graph_mod.build_graph(settings)
    initial: dict[str, Any] = {
        "out_dir": out_dir,
        "industry": industry,
        "topic_title": topic_title,
        "script_file": Path(script_file) if script_file else None,
    }
    if script is not None:
        initial["script"] = script
    async for chunk in graph.astream(
        initial, stream_mode="updates", config={"callbacks": [LLMCallAudit(out_dir)]}
    ):
        for node_name in chunk:
            on_stage(node_name)
    return ProjectManifest.model_validate_json(
        (out_dir / "project.json").read_text(encoding="utf-8")
    )


def _sum_usage(root: Path) -> dict[str, int]:
    """系列根目录(策划/编剧) + 各分集目录(元数据)的 llm_calls.jsonl 合计。"""
    from .llm_audit import summarize_usage

    total = summarize_usage(root)
    for d in root.iterdir():
        if d.is_dir():
            for k, v in summarize_usage(d).items():
                total[k] += v
    return total


# ---------------------------------------------------------------------------
# App 工厂
# ---------------------------------------------------------------------------

def create_app(settings: Settings | None = None, *, scheduler=None) -> FastAPI:
    """创建操作台 app。

    scheduler: 任务调度器 fn(coro_func, *args), 默认 asyncio.create_task
    (uvicorn 下任务跨请求存活); 测试可注入收集器, 用 TestClient portal 手动驱动,
    规避 TestClient 请求结束即取消 request 内 create_task 的行为。
    """
    settings = settings or load_settings()
    if scheduler is None:
        scheduler = lambda fn, *a: asyncio.create_task(fn(*a))  # noqa: E731
    out_root = settings.out_root.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    manager = JobManager()

    app = FastAPI(title="interview-video-bot 操作台")
    app.mount("/files", StaticFiles(directory=out_root), name="files")

    # ---------- 页面 ----------
    @app.get("/", response_class=FileResponse)
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    # ---------- 配置 ----------
    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {
            "brand": settings.brand,
            "llm_configured": settings.llm_ready(),
            "tts_provider": settings.tts_provider,
            "upload_enabled": settings.bili_upload_enabled,
            "bili_configured": settings.bili_ready(),
            "doubao_configured": settings.doubao_ready(),
            "voices": VOICES,
        }

    @app.get("/api/doctor")
    async def get_doctor() -> dict[str, Any]:
        from . import doctor as doctor_mod

        checks = await asyncio.to_thread(doctor_mod.run_doctor, settings)
        return {
            "ok": all(c.ok for c in checks if c.required),
            "checks": [c.__dict__ for c in checks],
        }

    # ---------- 选题 ----------
    @app.post("/api/topics")
    async def post_topics(req: TopicsRequest) -> dict[str, Any]:
        from . import llm as llm_mod
        from .chains import topic as topic_mod

        try:
            llm = llm_mod.get_chat_model(settings)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        topics = await topic_mod.generate_topics(llm, req.industry, req.n)
        return {"topics": [t.model_dump() for t in topics]}

    # ---------- 生成任务 ----------
    @app.post("/api/jobs", status_code=202)
    async def post_job(req: JobRequest) -> dict[str, str]:
        if req.provider not in VOICES:
            raise HTTPException(400, f"未知 TTS 通道: {req.provider}")
        if manager.active:
            raise HTTPException(409, "已有生成任务进行中, 请等它完成")
        script_file: Path | None = None
        if req.use_sample_script:
            script_file = Path("examples/sample_script.json")
            if not script_file.exists():
                raise HTTPException(400, "examples/sample_script.json 不存在")

        job = Job(uuid.uuid4().hex[:8], req)
        manager.jobs[job.id] = job
        manager.active = True
        job.task = scheduler(_run_job, job, script_file)
        return {"job_id": job.id}

    async def _run_job(job: Job, script_file: Path | None) -> None:
        import logging
        import traceback

        from .cli import _default_out_dir  # 复用 CLI 的目录命名
        from .llm_audit import summarize_usage

        logger = logging.getLogger(__name__)

        stage_keys = [k for k, _ in STAGES]

        def _on_stage(node: str) -> None:
            """node 为刚完成的节点: 记为 done, active 前移到下一节点。"""
            if node not in job.done_stages:
                job.done_stages.append(node)
            i = stage_keys.index(node) if node in stage_keys else -1
            job.current_stage = stage_keys[i + 1] if 0 <= i < len(stage_keys) - 1 else None

        try:
            job_settings = settings.model_copy()
            job_settings.tts_provider = job.params.provider
            if job.params.voice:
                if job.params.provider == "doubao":
                    job_settings.tts_doubao_voice = job.params.voice
                else:
                    job_settings.tts_voice = job.params.voice
            try:
                if job.params.mode == "series":
                    await _run_series_job(job, job_settings, _on_stage)
                else:
                    out_dir = _default_out_dir(
                        job_settings, script_file,
                        job.params.topic_title, job.params.industry,
                    )
                    job.project = out_dir.name
                    job.current_stage = stage_keys[0]
                    await _execute_pipeline(
                        job_settings, out_dir, job.params.industry,
                        job.params.topic_title, script_file, _on_stage,
                    )
                    job.usage = summarize_usage(out_dir)
                job.current_stage = None
                job.status = "done"
            except asyncio.CancelledError:
                job.status, job.error = "cancelled", "任务被取消"
                raise
            except Exception as exc:  # noqa: BLE001 —— 任务失败原因直接回前端
                job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
                # 失败现场落盘: 堆栈 + 失败时所在阶段(重启后仍可查)
                try:
                    err_path = (
                        out_root
                        / (job.project or ".")
                        / f"error-{job.id}.log"
                    )
                    err_path.parent.mkdir(parents=True, exist_ok=True)
                    err_path.write_text(
                        f"stage={job.current_stage} series={job.series}\n"
                        f"{traceback.format_exc()}",
                        encoding="utf-8",
                    )
                except OSError:
                    pass
                logger.exception("任务 %s 失败(阶段 %s)", job.id, job.current_stage)
        finally:
            manager.active = False

    async def _run_series_job(job: Job, job_settings: Settings, on_stage) -> None:
        """系列模式: 多 agent 生成每集脚本 → 逐集走媒体流水线。

        根目录在调 LLM 前创建并作为 run_dir 传入 generate_series:
        每章/每集完成即增量落盘(planning/), 失败不丢已花掉的 token;
        已有 project.json 的分集直接跳过(重试幂等)。
        """
        from . import doctor as doctor_mod
        from . import series as series_mod
        from .cli import _slug

        await asyncio.to_thread(doctor_mod.preflight, job_settings)
        req = job.params
        theme = req.theme or req.topic_title or "面试求职"
        job.series = {
            "episodes": req.episodes, "episode": 0,
            "chapter": 0, "chapters": 0, "phase": "策划中",
        }
        root = out_root / f"{_slug(theme)}-series-{datetime.now().strftime('%Y%m%d-%H%M')}"
        root.mkdir(parents=True, exist_ok=True)
        job.project = root.name  # 先定根目录: 失败时 error.log 也能落到系列目录

        def on_event(name: str, **p) -> None:
            s = job.series
            assert s is not None
            if name == "plan_done":
                s.update(phase="策划完成", episodes=len(p["plan"].episodes))
            elif name == "outline_done":
                s.update(episode=p["ep_idx"] + 1, phase="拆分章节",
                         chapter=0, chapters=len(p["outline"].chapters))
            elif name == "chapter_done":
                s.update(phase="写章节", chapter=p["ch_idx"] + 1)
            elif name == "script_done":
                s.update(phase="脚本完成")

        plan, scripts = await series_mod.generate_series(
            job_settings, theme, episodes=req.episodes,
            episode_sec=int(req.duration_min * 60), on_event=on_event,
            run_dir=root,
        )
        (root / "series.json").write_text(
            plan.model_dump_json(indent=2), encoding="utf-8"
        )
        for i, script in enumerate(scripts):
            job.series.update(episode=i + 1, phase="媒体合成")
            job.done_stages, job.current_stage = [], STAGES[0][0]  # 每集重置节点进度
            out_dir = root / f"ep{i + 1:02d}-{_slug(script.title)}"
            if (out_dir / "project.json").exists():
                continue  # 已有成片, 重试幂等跳过
            await _execute_pipeline(
                job_settings, out_dir, theme, None, None, on_stage, script=script,
            )
        job.usage = _sum_usage(root)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = manager.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在(重启后内存任务清空)")
        return job.to_dict()

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, str]:
        """取消运行中的任务(长任务失控时的唯一中止手段, 不必杀进程)。"""
        job = manager.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        if job.status != "running" or job.task is None:
            raise HTTPException(409, f"任务不在运行中(当前状态: {job.status})")
        job.task.cancel()
        return {"ok": "已请求取消"}

    # ---------- 作品库 ----------
    def _project_dir(name: str) -> Path:
        if not _NAME_RE.match(name):
            raise HTTPException(400, "非法项目名")
        d = out_root / name
        if d.is_dir() and (d / "project.json").exists():
            return d
        # 系列分集: 在系列根目录(含 series.json)下再查一层
        for series_dir in out_root.iterdir():
            if series_dir.is_dir() and (series_dir / "series.json").exists():
                ep = series_dir / name
                if ep.is_dir() and (ep / "project.json").exists():
                    return ep
        raise HTTPException(404, "项目不存在或缺 project.json")

    def _project_summary(d: Path) -> dict[str, Any]:
        m = ProjectManifest.model_validate_json(
            (d / "project.json").read_text(encoding="utf-8")
        )
        bvid = None
        sidecar = d / "upload.json"
        if sidecar.exists():
            bvid = json.loads(sidecar.read_text("utf-8")).get("bvid")
        rel = d.relative_to(out_root).as_posix()  # 系列分集是嵌套路径(系列根/ep01-xx)
        return {
            "name": d.name,
            "title": m.script.title,
            "industry": m.industry,
            "duration": round(m.duration_total, 1),
            "created_at": m.created_at.isoformat(),
            "has_meta": m.meta is not None,
            "bvid": bvid,
            "cover_url": f"/files/{rel}/cover.png",
            "video_url": f"/files/{rel}/final.mp4",
        }

    @app.get("/api/projects")
    async def list_projects() -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for d in sorted(out_root.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            if (d / "project.json").exists():
                items.append(_project_summary(d))
            elif (d / "series.json").exists():
                # 系列根目录: 展开分集(ep01/02 正序)
                for ep in sorted(d.iterdir()):
                    if ep.is_dir() and (ep / "project.json").exists():
                        items.append(_project_summary(ep))
        return {"projects": items}

    @app.get("/api/projects/{name}")
    async def get_project(name: str) -> dict[str, Any]:
        d = _project_dir(name)
        m = ProjectManifest.model_validate_json(
            (d / "project.json").read_text(encoding="utf-8")
        )
        data = m.model_dump(mode="json")
        data.update(_project_summary(d))
        data["srt_url"] = f"/files/{d.relative_to(out_root).as_posix()}/subs/subtitles.srt"
        return data

    @app.put("/api/projects/{name}/meta")
    async def put_meta(name: str, req: MetaRequest) -> dict[str, Any]:
        d = _project_dir(name)
        m = ProjectManifest.model_validate_json(
            (d / "project.json").read_text(encoding="utf-8")
        )
        m.meta = BiliMeta(title=req.title, desc=req.desc, tags=req.tags)
        (d / "project.json").write_text(m.model_dump_json(indent=2), encoding="utf-8")
        return {"ok": True}

    @app.post("/api/projects/{name}/upload")
    async def upload_project(name: str) -> dict[str, str]:
        d = _project_dir(name)
        from . import bilibili as bili_mod

        if bili_mod.build_credential(settings) is None:
            raise HTTPException(
                400,
                "未配置 B站 cookie 三件套(BILI_SESSDATA/BILI_JCT/BILI_BUVID3)"
                "或未开启 BILI_UPLOAD_ENABLED",
            )
        try:
            bvid = await bili_mod.upload_project(d, settings)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        sidecar = d / "upload.json"
        sidecar.write_text(
            json.dumps(
                {"bvid": bvid, "uploaded_at": datetime.now().isoformat()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {"bvid": bvid, "url": f"https://www.bilibili.com/video/{bvid}"}

    return app
