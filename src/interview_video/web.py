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
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .models import BiliMeta, ProjectManifest

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
        self.status = "running"  # running | done | failed
        self.current_stage: str | None = None
        self.done_stages: list[str] = []
        self.error: str | None = None
        self.project: str | None = None  # out_dir 名

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "current_stage": self.current_stage,
            "done_stages": self.done_stages,
            "error": self.error,
            "project": self.project,
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
) -> ProjectManifest:
    """build_graph + astream(updates) 跑流水线, 逐节点回调进度。"""
    from . import graph as graph_mod

    out_dir.mkdir(parents=True, exist_ok=True)
    graph = graph_mod.build_graph(settings)
    initial: dict[str, Any] = {
        "out_dir": out_dir,
        "industry": industry,
        "topic_title": topic_title,
        "script_file": Path(script_file) if script_file else None,
    }
    async for chunk in graph.astream(initial, stream_mode="updates"):
        for node_name in chunk:
            on_stage(node_name)
    return ProjectManifest.model_validate_json(
        (out_dir / "project.json").read_text(encoding="utf-8")
    )


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
            "llm_configured": settings.llm_api_key is not None,
            "tts_provider": settings.tts_provider,
            "upload_enabled": settings.bili_upload_enabled,
            "doubao_configured": bool(settings.tts_doubao_api_key),
            "voices": VOICES,
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
        scheduler(_run_job, job, script_file)
        return {"job_id": job.id}

    async def _run_job(job: Job, script_file: Path | None) -> None:
        from .cli import _default_out_dir  # 复用 CLI 的目录命名

        try:
            job_settings = settings.model_copy()
            job_settings.tts_provider = job.params.provider
            if job.params.voice:
                if job.params.provider == "doubao":
                    job_settings.tts_doubao_voice = job.params.voice
                else:
                    job_settings.tts_voice = job.params.voice
            out_dir = _default_out_dir(
                job_settings, script_file, job.params.topic_title, job.params.industry
            )
            try:
                def _on_stage(node: str) -> None:
                    if job.current_stage and job.current_stage not in job.done_stages:
                        job.done_stages.append(job.current_stage)
                    job.current_stage = node

                await _execute_pipeline(
                    job_settings, out_dir, job.params.industry,
                    job.params.topic_title, script_file, _on_stage,
                )
                if job.current_stage and job.current_stage not in job.done_stages:
                    job.done_stages.append(job.current_stage)
                job.status = "done"
                job.project = out_dir.name
            except asyncio.CancelledError:
                job.status, job.error = "failed", "任务被取消"
                raise
            except Exception as exc:  # noqa: BLE001 —— 任务失败原因直接回前端
                job.status, job.error = "failed", str(exc)
        finally:
            manager.active = False

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = manager.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在(重启后内存任务清空)")
        return job.to_dict()

    # ---------- 作品库 ----------
    def _project_dir(name: str) -> Path:
        if not _NAME_RE.match(name):
            raise HTTPException(400, "非法项目名")
        d = out_root / name
        if not d.is_dir() or not (d / "project.json").exists():
            raise HTTPException(404, "项目不存在或缺 project.json")
        return d

    def _project_summary(d: Path) -> dict[str, Any]:
        m = ProjectManifest.model_validate_json(
            (d / "project.json").read_text(encoding="utf-8")
        )
        bvid = None
        sidecar = d / "upload.json"
        if sidecar.exists():
            bvid = json.loads(sidecar.read_text("utf-8")).get("bvid")
        return {
            "name": d.name,
            "title": m.script.title,
            "industry": m.industry,
            "duration": round(m.duration_total, 1),
            "created_at": m.created_at.isoformat(),
            "has_meta": m.meta is not None,
            "bvid": bvid,
            "cover_url": f"/files/{d.name}/cover.png",
            "video_url": f"/files/{d.name}/final.mp4",
        }

    @app.get("/api/projects")
    async def list_projects() -> dict[str, Any]:
        items = [
            _project_summary(d)
            for d in sorted(out_root.iterdir(), reverse=True)
            if d.is_dir() and (d / "project.json").exists()
        ]
        return {"projects": items}

    @app.get("/api/projects/{name}")
    async def get_project(name: str) -> dict[str, Any]:
        d = _project_dir(name)
        m = ProjectManifest.model_validate_json(
            (d / "project.json").read_text(encoding="utf-8")
        )
        data = m.model_dump(mode="json")
        data.update(_project_summary(d))
        data["srt_url"] = f"/files/{d.name}/subs/subtitles.srt"
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
