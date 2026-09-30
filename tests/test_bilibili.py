"""bilibili.py: 封面/分区/异常映射/重试/路径回退/元数据裁剪(不联网, 不真实上传)。"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from pathlib import Path

import pytest
from bilibili_api import video_uploader as real_vu
from bilibili_api.exceptions import NetworkException, ResponseCodeException
from bilibili_api.utils.picture import Picture

from interview_video import bilibili as bili_mod
from interview_video import doctor as doctor_mod
from interview_video.bilibili import (
    AI_DECLARATION,
    BiliUploadError,
    _clip_meta,
    _resolve_artifact,
    upload_project,
    valid_tids,
)
from interview_video.config import Settings
from interview_video.models import BiliMeta, ProjectManifest, Script

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _settings(**kw) -> Settings:
    kw.setdefault("bili_upload_enabled", True)
    return Settings(_env_file=None, bili_sessdata="s", bili_jct="j", bili_buvid3="b", **kw)


def _project(tmp_path: Path, *, video: Path | None = None, cover: Path | None = None,
             meta: BiliMeta | None = None) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "cover.png").write_bytes(_PNG)
    (d / "final.mp4").write_bytes(b"x")
    m = ProjectManifest(
        created_at=datetime.now(), industry="互联网", script=Script(title="标题", scenes=[]),
        video=video or d / "final.mp4", cover=cover or d / "cover.png", meta=meta,
    )
    (d / "project.json").write_text(m.model_dump_json(), encoding="utf-8")
    return d


class _Uploader:
    """只替换 VideoUploader, VideoMeta/Picture/VideoUploaderPage 用真实实现。"""

    instances: list = []
    behaviors: list = []

    def __init__(self, pages, meta, credential):
        self.pages, self.meta = pages, meta
        self.handlers: dict = {}
        self.aborted = False
        _Uploader.instances.append(self)

    def add_event_listener(self, name, handler):
        self.handlers.setdefault(name, []).append(handler)

    async def abort(self):
        self.aborted = True

    async def start(self):
        b = _Uploader.behaviors.pop(0)
        if isinstance(b, BaseException):
            raise b
        if b == "chunk_fail":
            for _ in range(bili_mod.MAX_CHUNK_FAILURES + 1):
                for h in self.handlers.get("CHUNK_FAILED", []):
                    await h({})
            return None  # 库 abort 后 start() 返回 None
        return b


@pytest.fixture
def uploader(monkeypatch: pytest.MonkeyPatch):
    _Uploader.instances = []
    _Uploader.behaviors = []
    monkeypatch.setattr(bili_mod.video_uploader, "VideoUploader", _Uploader)
    sleeps: list = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(bili_mod.asyncio, "sleep", fake_sleep)
    _Uploader.sleeps = sleeps
    return _Uploader


def test_real_video_meta_accepts_picture_cover(tmp_path: Path) -> None:
    png = tmp_path / "c.png"
    png.write_bytes(_PNG)
    m = real_vu.VideoMeta(
        tid=209, title="t", desc="d", cover=Picture.from_file(str(png)), tags=["面试"],
    )
    assert isinstance(m.cover, Picture) and m.cover.content == _PNG


def test_valid_tids_children_only() -> None:
    tids = valid_tids()
    assert 209 in tids and "职业职场" in tids[209]
    assert 36 not in tids


def test_default_tid_valid_and_doctor_checks() -> None:
    assert Settings(_env_file=None).bili_tid == 209
    ok = {c.name: c for c in doctor_mod.check_config(_settings())}
    assert ok["B站分区"].ok
    bad = {c.name: c for c in doctor_mod.check_config(_settings(bili_tid=36))}
    assert not bad["B站分区"].ok and "209" in bad["B站分区"].detail


def test_upload_rejects_parent_tid_before_network(tmp_path: Path, uploader) -> None:
    d = _project(tmp_path)
    with pytest.raises(BiliUploadError, match="子分区"):
        asyncio.run(upload_project(d, _settings(bili_tid=36)))
    assert uploader.instances == []


def test_upload_success_uses_real_meta_with_picture(tmp_path: Path, uploader) -> None:
    d = _project(tmp_path)
    uploader.behaviors = [{"bvid": "BV1", "aid": 1}]
    assert asyncio.run(upload_project(d, _settings())) == "BV1"
    meta = uploader.instances[0].meta
    assert isinstance(meta, real_vu.VideoMeta) and meta.tid == 209
    assert isinstance(meta.cover, Picture) and meta.cover.content == _PNG
    assert meta.desc.endswith(AI_DECLARATION)
    assert json.loads((d / "upload.json").read_text("utf-8"))["bvid"] == "BV1"


def test_cookie_expired_maps_to_friendly_error(tmp_path: Path, uploader) -> None:
    d = _project(tmp_path)
    uploader.behaviors = [ResponseCodeException(-101, "账号未登录")]
    with pytest.raises(BiliUploadError, match="cookie") as ei:
        asyncio.run(upload_project(d, _settings()))
    assert "SESSDATA" in str(ei.value)
    assert isinstance(ei.value, RuntimeError)
    assert len(uploader.instances) == 1 and uploader.sleeps == []  # 不重试
    assert not (d / "upload.json").exists()


def test_network_error_retries_then_succeeds(tmp_path: Path, uploader) -> None:
    d = _project(tmp_path)
    uploader.behaviors = [NetworkException(502, "Bad Gateway"), "chunk_fail", {"bvid": "BV3"}]
    assert asyncio.run(upload_project(d, _settings())) == "BV3"
    assert uploader.sleeps == [5, 20]
    assert uploader.instances[1].aborted


def test_retries_exhausted_raises(tmp_path: Path, uploader) -> None:
    d = _project(tmp_path)
    uploader.behaviors = [NetworkException(502, "x")] * 3
    with pytest.raises(BiliUploadError, match="网络错误.*已重试 2 次"):
        asyncio.run(upload_project(d, _settings()))
    assert len(uploader.instances) == 3


def test_timeout_is_transient(tmp_path: Path, uploader, monkeypatch) -> None:
    d = _project(tmp_path)

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(bili_mod.asyncio, "wait_for", fake_wait_for)
    with pytest.raises(BiliUploadError, match="超时"):
        asyncio.run(upload_project(d, _settings()))
    assert len(uploader.instances) == 3 and all(u.aborted for u in uploader.instances)


def test_resolve_artifact_relative_fallback(tmp_path: Path) -> None:
    d = tmp_path / "moved"
    d.mkdir()
    (d / "final.mp4").write_bytes(b"x")
    assert _resolve_artifact(d, "/old/place/final.mp4", "视频") == d / "final.mp4"
    assert _resolve_artifact(d, Path("final.mp4"), "视频") == d / "final.mp4"
    assert _resolve_artifact(d, d / "final.mp4", "视频") == d / "final.mp4"
    with pytest.raises(BiliUploadError, match="封面"):
        _resolve_artifact(d, "/old/cover.png", "封面")


def test_upload_after_project_moved(tmp_path: Path, uploader) -> None:
    d = _project(tmp_path, video=Path("/nonexistent/final.mp4"), cover=Path("/nonexistent/cover.png"))
    uploader.behaviors = [{"bvid": "BV1"}]
    assert asyncio.run(upload_project(d, _settings())) == "BV1"
    assert uploader.instances[0].pages[0].path == str(d / "final.mp4")


def test_clip_meta_limits() -> None:
    long_desc = "简介" * 2000 + f"\n{AI_DECLARATION}"
    tags = [f"标签{i}" * 10 for i in range(15)] + ["", "标签0" * 10]
    title, desc, out_tags = _clip_meta(BiliMeta(title="标" * 100, desc=long_desc, tags=tags))
    assert len(title) == 80
    assert len(desc) <= 2000 and desc.endswith(AI_DECLARATION) and desc.count(AI_DECLARATION) == 1
    assert len(out_tags) == 10 and all(0 < len(t) <= 20 for t in out_tags)
    with pytest.raises(BiliUploadError, match="标题为空"):
        _clip_meta(BiliMeta(title="  ", desc="", tags=[]))
