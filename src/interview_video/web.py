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

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .config import Settings, load_settings
from .estimate import (
    SERIES_MAX_EPISODE_MIN,
    SERIES_MAX_EPISODES,
    SERIES_MIN_EPISODE_MIN,
    estimate_series,
)
from .models import BiliMeta, ProjectManifest, Script, SeriesPlan

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
    episodes: int = Field(default=3, ge=1, le=SERIES_MAX_EPISODES)
    duration_min: float = Field(  # 每集目标时长(分钟), 边界与 CLI 共用 estimate.py
        default=15.0, ge=SERIES_MIN_EPISODE_MIN, le=SERIES_MAX_EPISODE_MIN
    )
    stop_after: Literal["script"] | None = None  # 只生成脚本(审改后再 regenerate)


class EstimateRequest(BaseModel):
    episodes: int = Field(default=3, ge=1, le=SERIES_MAX_EPISODES)
    duration_min: float = Field(
        default=15.0, ge=SERIES_MIN_EPISODE_MIN, le=SERIES_MAX_EPISODE_MIN
    )
    provider: str | None = None  # 预估 TTS 字符所属通道(不传用 .env 默认)


class RegenerateRequest(BaseModel):
    provider: str | None = None
    voice: str | None = None


class MetaRequest(BaseModel):
    title: str
    desc: str = ""
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 任务管理(单飞)
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, job_id: str, params: JobRequest, resume_dir: Path | None = None) -> None:
        self.id = job_id
        self.params = params
        # 单集: 复用该目录的 script.json(同 CLI --resume); 系列: 系列根目录(同 series --resume)
        self.resume_dir = resume_dir
        self.out_dir: Path | None = resume_dir
        self.status = "running"  # running | done | failed | cancelled
        self.current_stage: str | None = None
        self.done_stages: list[str] = []
        self.error: str | None = None
        self.project: str | None = None  # out_dir 名(系列模式=系列根目录名)
        self.series: dict[str, Any] | None = None  # 系列进度 {episode,episodes,chapter,chapters,phase}
        self.usage: dict[str, int] | None = None  # LLM token 汇总(llm_calls.jsonl)
        self.task: asyncio.Task | None = None  # 后台任务句柄(取消接口用)
        self.cancel_requested = False  # 已请求取消, 正在等子进程(ffmpeg/chromium)退出

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
            "stop_after": self.params.stop_after,
            "cancellable": (
                self.status == "running" and self.task is not None and not self.cancel_requested
            ),
            "cancelling": self.status == "running" and self.cancel_requested,
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
        self.active_id: str | None = None

    @property
    def active(self) -> bool:
        return self.active_id is not None


class _Busy(Exception):
    """409: 已有任务在跑; 响应体带 job_id, 前端可据此重新挂上进度轮询。"""

    def __init__(self, message: str, job_id: str | None) -> None:
        self.message, self.job_id = message, job_id


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
    from .llm_audit import LLMCallAudit, run_budget

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
    with run_budget(settings):
        async for chunk in graph.astream(
            initial, stream_mode="updates", config={"callbacks": [LLMCallAudit(out_dir)]}
        ):
            for node_name in chunk:
                on_stage(node_name)
    return ProjectManifest.model_validate_json(
        (out_dir / "project.json").read_text(encoding="utf-8")
    )


async def _execute_script_stage(
    settings: Settings,
    out_dir: Path,
    industry: str,
    topic_title: str | None,
    script_file: Path | None,
) -> Script:
    """只生成脚本(同 CLI --stop-after script)。"""
    from . import graph as graph_mod

    return await graph_mod.run_script_stage(
        settings, out_dir, industry=industry,
        topic_title=topic_title, script_file=script_file,
    )


def _format_validation_error(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(根)'}: {e['msg']}" for e in exc.errors()
    )


def _series_status(root: Path) -> dict[str, Any] | None:
    """系列根目录进度: 规划集数 vs 已出片集数; 无 series.json 返回 None。"""
    try:
        plan = SeriesPlan.model_validate_json((root / "series.json").read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None
    done = sum(
        1 for i in range(len(plan.episodes))
        if any((d / "project.json").exists() for d in root.glob(f"ep{i + 1:02d}-*"))
    )
    return {
        "root": root.name, "title": plan.title, "theme": plan.theme,
        "episodes": len(plan.episodes), "done_episodes": done,
        "complete": done >= len(plan.episodes),
    }


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

    @app.exception_handler(_Busy)
    async def _busy_handler(_request, exc: _Busy) -> JSONResponse:
        return JSONResponse({"detail": exc.message, "job_id": exc.job_id}, status_code=409)

    def _ensure_idle() -> None:
        if manager.active:
            raise _Busy("已有生成任务进行中, 请等它完成", manager.active_id)

    def _start(job: Job, script_file: Path | None = None) -> dict[str, str]:
        manager.jobs[job.id] = job
        manager.active_id = job.id
        job.task = scheduler(_run_job, job, script_file)
        return {"job_id": job.id}
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
            "series_limits": {"max_episodes": SERIES_MAX_EPISODES,
                              "min_duration_min": SERIES_MIN_EPISODE_MIN,
                              "max_duration_min": SERIES_MAX_EPISODE_MIN},
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
        if req.stop_after and req.mode == "series":
            raise HTTPException(400, "系列模式不支持只生成脚本")
        _ensure_idle()
        script_file: Path | None = None
        if req.use_sample_script:
            script_file = Path("examples/sample_script.json")
            if not script_file.exists():
                raise HTTPException(400, "examples/sample_script.json 不存在")

        return _start(Job(uuid.uuid4().hex[:8], req), script_file)

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
                    preset: Script | None = None
                    industry = job.params.industry
                    if job.resume_dir is not None:
                        from .graph import resume_industry

                        out_dir = job.resume_dir
                        preset = Script.model_validate_json(
                            (out_dir / "script.json").read_text(encoding="utf-8")
                        )
                        industry = resume_industry(out_dir) or industry
                    else:
                        out_dir = _default_out_dir(
                            job_settings, script_file,
                            job.params.topic_title, industry,
                        )
                    job.out_dir = out_dir.resolve()
                    job.project = out_dir.name
                    job.current_stage = stage_keys[0]
                    if job.params.stop_after == "script":
                        await _execute_script_stage(
                            job_settings, out_dir, industry,
                            job.params.topic_title, script_file,
                        )
                        _on_stage(stage_keys[0])
                    else:
                        await _execute_pipeline(
                            job_settings, out_dir, industry,
                            job.params.topic_title, script_file, _on_stage,
                            **({"script": preset} if preset is not None else {}),
                        )
                    job.usage = summarize_usage(out_dir)
                job.current_stage = None
                job.status = "done"
            except asyncio.CancelledError:
                # 走到这里时子进程已退出(procs.run_in_thread 等线程清理完才抛), 锁在 finally 才释放
                job.status = "cancelled"
                job.error = "任务被取消(已完成的产物保留, 可在作品库重新生成/续跑)"
                raise
            except Exception as exc:  # noqa: BLE001 —— 任务失败原因直接回前端
                job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
                # 失败现场落盘: 堆栈 + 失败时所在阶段(重启后仍可查)
                try:
                    err_path = (
                        job.out_dir or out_root / (job.project or ".")
                    ) / f"error-{job.id}.log"
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
            if manager.active_id == job.id:
                manager.active_id = None

    async def _run_series_job(job: Job, job_settings: Settings, on_stage) -> None:
        """系列模式: 多 agent 生成每集脚本 → 逐集走媒体流水线。

        根目录在调 LLM 前创建并作为 run_dir 传入 generate_series:
        每章/每集完成即增量落盘(planning/), 失败不丢已花掉的 token;
        已有 project.json 的分集直接跳过(重试幂等)。
        """
        from . import doctor as doctor_mod
        from . import series as series_mod
        from .cli import _slug
        from .llm_audit import run_budget

        await asyncio.to_thread(doctor_mod.preflight, job_settings)
        req = job.params
        theme = req.theme or req.topic_title or "面试求职"
        episodes, episode_sec = req.episodes, int(req.duration_min * 60)
        if job.resume_dir is not None:  # 续跑: 主题/集数/时长以磁盘上的规划为准
            root = job.resume_dir
            plan = SeriesPlan.model_validate_json(
                (root / "series.json").read_text(encoding="utf-8")
            )
            theme, episodes = plan.theme, len(plan.episodes)
            if plan.episodes:
                episode_sec = plan.episodes[0].target_sec
        else:
            root = out_root / f"{_slug(theme)}-series-{datetime.now().strftime('%Y%m%d-%H%M')}"
        job.series = {
            "episodes": episodes, "episode": 0,
            "chapter": 0, "chapters": 0,
            "phase": "续跑中" if job.resume_dir is not None else "策划中",
        }
        root.mkdir(parents=True, exist_ok=True)
        job.out_dir = root.resolve()
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

        with run_budget(job_settings):  # 整个系列共用 LLM 硬上限, 超限失败后可续跑
            plan, scripts = await series_mod.generate_series(
                job_settings, theme, episodes=episodes,
                episode_sec=episode_sec, on_event=on_event,
                run_dir=root, resume=job.resume_dir is not None,
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

    @app.get("/api/jobs/active")
    async def get_active_job() -> dict[str, Any] | None:
        """当前运行中的任务(页面刷新后据此重新挂上进度); 无则 null。"""
        job = manager.jobs.get(manager.active_id) if manager.active_id else None
        return job.to_dict() if job is not None else None

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = manager.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在(重启后内存任务清空)")
        return job.to_dict()

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, str]:
        """取消运行中的任务: 终止 ffmpeg/chromium 子进程, 等它们退出后才释放单飞锁。"""
        job = manager.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        if job.status != "running" or job.task is None:
            raise HTTPException(409, f"任务不在运行中(当前状态: {job.status})")
        job.cancel_requested = True
        job.task.cancel()
        return {"ok": "已请求取消"}

    # ---------- 系列费用预估 ----------
    def _estimate(req: EstimateRequest) -> dict[str, Any]:
        est_settings = settings
        if req.provider:
            if req.provider not in VOICES:
                raise HTTPException(400, f"未知 TTS 通道: {req.provider}")
            est_settings = settings.model_copy(update={"tts_provider": req.provider})
        est = estimate_series(est_settings, req.episodes, req.duration_min)
        return {**est.to_dict(), "lines": est.lines(),
                "limits": {"max_calls": settings.llm_max_calls_per_run,
                           "max_tokens": settings.llm_max_tokens_per_run}}

    @app.get("/api/series/estimate")
    async def get_series_estimate(
        episodes: int = 3, duration_min: float = 15.0, provider: str | None = None
    ) -> dict[str, Any]:
        try:
            req = EstimateRequest(episodes=episodes, duration_min=duration_min, provider=provider)
        except ValidationError as exc:
            raise HTTPException(422, _format_validation_error(exc)) from exc
        return _estimate(req)

    @app.post("/api/series/estimate")
    async def post_series_estimate(req: EstimateRequest) -> dict[str, Any]:
        return _estimate(req)

    # ---------- 系列续跑 ----------
    def _series_root(root: str) -> Path:
        if not _NAME_RE.match(root):
            raise HTTPException(400, "非法系列目录名")
        d = (out_root / root).resolve()
        if d.parent != out_root or not (d / "series.json").is_file():
            raise HTTPException(404, "系列目录不存在或缺 series.json")
        return d

    @app.get("/api/series")
    async def list_series() -> dict[str, Any]:
        """out_root 下的系列根目录及完成度(complete=False 的可续跑)。"""
        items = [
            st for d in sorted(out_root.iterdir(), reverse=True)
            if d.is_dir() and (st := _series_status(d)) is not None
        ]
        return {"series": items}

    @app.post("/api/series/{root}/resume", status_code=202)
    async def resume_series(root: str, req: RegenerateRequest | None = None) -> dict[str, str]:
        """同 CLI ``ivb series --resume <root>``: 已完成的集/章不重跑, 已出片的分集跳过。"""
        d = _series_root(root)
        req = req or RegenerateRequest()
        provider = req.provider or settings.tts_provider
        if provider not in VOICES:
            raise HTTPException(400, f"未知 TTS 通道: {provider}")
        _ensure_idle()
        job = Job(
            uuid.uuid4().hex[:8],
            JobRequest(mode="series", provider=provider, voice=req.voice),
            resume_dir=d,
        )
        job.project = d.name
        return _start(job)

    # ---------- 作品库 ----------
    def _is_project(d: Path, allow_draft: bool) -> bool:
        return d.is_dir() and (
            (d / "project.json").exists() or (allow_draft and (d / "script.json").exists())
        )

    def _project_dir(name: str, allow_draft: bool = False) -> Path:
        """按名字找项目目录; allow_draft=True 时只有 script.json(未出片)的草稿也算。"""
        if not _NAME_RE.match(name):
            raise HTTPException(400, "非法项目名")
        d = out_root / name
        if _is_project(d, allow_draft):
            return d
        # 系列分集: 在系列根目录(含 series.json)下再查一层
        for series_dir in out_root.iterdir():
            if series_dir.is_dir() and (series_dir / "series.json").exists():
                ep = series_dir / name
                if _is_project(ep, allow_draft):
                    return ep
        raise HTTPException(
            404, "项目不存在或缺 script.json" if allow_draft else "项目不存在或缺 project.json"
        )

    def _read_script(d: Path) -> Script:
        try:
            if (d / "script.json").exists():
                return Script.model_validate_json((d / "script.json").read_text(encoding="utf-8"))
            return ProjectManifest.model_validate_json(
                (d / "project.json").read_text(encoding="utf-8")
            ).script
        except (OSError, ValidationError) as exc:
            raise HTTPException(422, f"{d.name} 的脚本文件损坏: {exc}") from exc

    def _draft_summary(d: Path) -> dict[str, Any]:
        """只有 script.json 的草稿(只生成脚本 / 中途失败): 无视频与封面。"""
        from .graph import resume_industry

        return {
            "name": d.name,
            "title": _read_script(d).title,
            "industry": resume_industry(d) or "",
            "duration": 0,
            "created_at": datetime.fromtimestamp((d / "script.json").stat().st_mtime).isoformat(),
            "has_meta": False,
            "bvid": None,
            "draft": True,
            "cover_url": None,
            "video_url": None,
        }

    def _project_summary(d: Path) -> dict[str, Any]:
        if not (d / "project.json").exists():
            return _draft_summary(d)
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
            "draft": False,
            "cover_url": f"/files/{rel}/cover.png",
            "video_url": f"/files/{rel}/final.mp4",
        }

    @app.get("/api/projects")
    async def list_projects() -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for d in sorted(out_root.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            if (d / "series.json").exists():
                # 系列根目录: 展开分集(ep01/02 正序)
                candidates = [ep for ep in sorted(d.iterdir()) if _is_project(ep, allow_draft=True)]
            else:
                candidates = [d] if _is_project(d, allow_draft=True) else []
            for c in candidates:
                try:
                    items.append(_project_summary(c))
                except (HTTPException, OSError, ValidationError, ValueError):
                    continue  # 单个损坏目录不拖垮整个作品库
        return {"projects": items}

    @app.get("/api/projects/{name}")
    async def get_project(name: str) -> dict[str, Any]:
        d = _project_dir(name, allow_draft=True)
        if not (d / "project.json").exists():
            data = _draft_summary(d)
            data.update(meta=None, script=_read_script(d).model_dump(mode="json"))
            return data
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

    @app.get("/api/projects/{name}/script")
    async def get_script(name: str) -> dict[str, Any]:
        d = _project_dir(name, allow_draft=True)
        return _read_script(d).model_dump(mode="json")

    @app.put("/api/projects/{name}/script")
    async def put_script(name: str, payload: Any = Body(...)) -> dict[str, Any]:
        """整体替换 script.json(按 Script 模型校验); 视频需 regenerate 才会更新。"""
        d = _project_dir(name, allow_draft=True)
        d_res = d.resolve()
        if manager.active and any(
            j.status == "running" and j.out_dir is not None
            and (j.out_dir == d_res or j.out_dir in d_res.parents)
            for j in manager.jobs.values()
        ):
            raise _Busy("该项目正在生成中, 请等任务结束再改脚本", manager.active_id)
        try:
            script = Script.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(422, f"脚本格式不合法: {_format_validation_error(exc)}") from exc
        if not script.scenes:
            raise HTTPException(422, "脚本格式不合法: scenes 不能为空")
        (d / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")
        return {"ok": True}

    @app.post("/api/projects/{name}/regenerate", status_code=202)
    async def regenerate_project(
        name: str, req: RegenerateRequest | None = None
    ) -> dict[str, str]:
        """复用项目目录 + script.json 重跑流水线(同 CLI --resume, TTS 按口播指纹复用缓存)。"""
        d = _project_dir(name, allow_draft=True)
        if not (d / "script.json").exists():
            raise HTTPException(400, "项目缺 script.json, 无法重新生成")
        req = req or RegenerateRequest()
        provider = req.provider or settings.tts_provider
        if provider not in VOICES:
            raise HTTPException(400, f"未知 TTS 通道: {provider}")
        _ensure_idle()
        return _start(Job(
            uuid.uuid4().hex[:8],
            JobRequest(provider=provider, voice=req.voice),
            resume_dir=d,
        ))

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
