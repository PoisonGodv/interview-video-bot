"""B站上传封装(可选, 默认关): bilibili_api video_uploader。

已安装版本实测(bilibili-api-python ≥16.1.0):
- ``VideoUploader(pages, meta, credential, cover='', line=None)`` + ``await uploader.start() -> {"bvid", "aid"}``
  (start 内部自行上传 meta.cover; abort/取消后 start 返回 None 而不是抛异常)
- ``VideoUploaderPage(path: str, title: str, description: str = "")``
- ``VideoMeta(tid, title, desc, cover: Picture | 本地路径, tags, ...)``: title≤80, desc≤2000, tags≤10
- tid 必须是子分区, 可选值见包内 ``data/video_uploader_meta_pre.json``
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import bilibili_api
from bilibili_api import Credential, video_uploader
from bilibili_api.exceptions import ApiException, NetworkException, ResponseCodeException
from bilibili_api.utils.picture import Picture

from .config import Settings
from .models import BiliMeta, ProjectManifest

# 简介末尾固定补上的 AI 生成声明(与 chains/metadata.py 的约定一致)
AI_DECLARATION = "* 本视频由 AI 辅助生成"

# 抓 cookie 指引(三件套缺一不可)
_COOKIE_GUIDE = """\
未配置完整的 B站 cookie 三件套(BILI_SESSDATA / BILI_JCT / BILI_BUVID3)。

获取步骤(桌面 Chrome):
  1. 登录 www.bilibili.com 后按 F12 打开开发者工具 → Application(应用)
     → Cookies → https://www.bilibili.com;
  2. 复制 SESSDATA、bili_jct、buvid3 三个值;
  3. 填入 .env:
       BILI_UPLOAD_ENABLED=true
       BILI_SESSDATA=...
       BILI_JCT=...
       BILI_BUVID3=...
注意: SESSDATA 等价于登录态, 不要泄露给他人。"""


UPLOAD_SIDECAR = "upload.json"

# B站定时发布窗口(与 bilibili_api VideoMeta 校验一致): 距现在 ≥2 小时, ≤15 天
DTIME_MIN_AHEAD_SEC = 2 * 3600
DTIME_MAX_AHEAD_SEC = 15 * 24 * 3600
_PUBLISH_AT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


def parse_publish_at(value: str, now: float | None = None) -> int:
    """``"YYYY-mm-dd HH:MM"``(本地时区) → 定时发布 unix 时间戳(dtime), 校验 B站 2 小时~15 天窗口。

    不合法抛 ValueError(信息可直接展示给用户)。
    """
    text = value.strip()
    for fmt in _PUBLISH_AT_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"定时发布时间格式应为 YYYY-mm-dd HH:MM, 收到 {value!r}")
    dtime = int(dt.timestamp())
    now = time.time() if now is None else now
    if dtime < now + DTIME_MIN_AHEAD_SEC:
        raise ValueError(f"定时发布时间须晚于当前 2 小时以上(B站规则), 收到 {text}")
    if dtime > now + DTIME_MAX_AHEAD_SEC:
        raise ValueError(f"定时发布时间不能超过 15 天后(B站规则), 收到 {text}")
    return dtime


class AlreadyUploadedError(RuntimeError):
    """项目已有 bvid(upload.json), 未显式 force 时拒绝重复投稿。"""

    def __init__(self, bvid: str) -> None:
        self.bvid = bvid
        super().__init__(
            f"该项目已上传过(bvid={bvid}, https://www.bilibili.com/video/{bvid}); "
            "确需重新投稿请加 --force(Web 端需再次确认)"
        )


def read_upload_record(project_dir: Path) -> dict | None:
    """读 upload.json; 不存在/损坏/无 bvid 返回 None。"""
    try:
        data = json.loads((Path(project_dir) / UPLOAD_SIDECAR).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("bvid") else None


def write_upload_record(project_dir: Path, bvid: str, dtime: int | None = None) -> None:
    record: dict = {"bvid": bvid, "uploaded_at": datetime.now().isoformat()}
    if dtime is not None:
        record["dtime"] = dtime
        record["publish_at"] = datetime.fromtimestamp(dtime).isoformat(timespec="minutes")
    (Path(project_dir) / UPLOAD_SIDECAR).write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8",
    )


def build_credential(settings: Settings) -> Credential | None:
    """凭证三件套齐全才返回 Credential, 否则 None。"""
    if settings.bili_sessdata and settings.bili_jct and settings.bili_buvid3:
        return Credential(
            sessdata=settings.bili_sessdata.get_secret_value(),
            bili_jct=settings.bili_jct.get_secret_value(),
            buvid3=settings.bili_buvid3.get_secret_value(),
        )
    return None


class BiliUploadError(RuntimeError):
    """上传失败(参数/网络/B站接口错误), 信息可直接展示给用户。"""


# B站投稿元数据上限(与 bilibili_api VideoMeta 校验一致)
TITLE_MAX = 80
DESC_MAX = 2000
TAG_MAX_LEN = 20
TAGS_MAX = 10

MAX_CHUNK_FAILURES = 10
_RETRY_DELAYS = (5, 20)
_COOKIE_EXPIRED = "B站 cookie 已过期或无效(未登录), 请重新抓 SESSDATA。\n" + _COOKIE_GUIDE


@lru_cache(maxsize=1)
def valid_tids() -> dict[int, str]:
    """可投稿的子分区 {tid: "父分区·子分区"}, 取自 bilibili_api 自带的 video_uploader_meta_pre.json。"""
    path = os.path.join(os.path.dirname(bilibili_api.__file__), "data", "video_uploader_meta_pre.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out: dict[int, str] = {}
    for parent in data.get("tid_list", []):
        for child in parent.get("children", []):
            out[int(child["id"])] = f"{parent.get('name', '')}·{child.get('name', '')}"
    return out


def tid_error(tid: int) -> str | None:
    """tid 合法返回 None, 否则返回带可选子分区示例的说明。"""
    tids = valid_tids()
    if tid in tids:
        return None
    common = ", ".join(f"{k}={tids[k]}" for k in (209, 207, 208, 124, 201) if k in tids)
    return (
        f"BILI_TID={tid} 不是可投稿的子分区(父分区不能直接投稿)。"
        f"常用: {common}; 全部可选 {len(tids)} 个子分区见 bilibili_api/data/video_uploader_meta_pre.json"
    )


def _resolve_artifact(project_dir: Path, p: Path | str | None, label: str) -> Path:
    """产物路径: 绝对路径存在则直接用, 否则回退到 project_dir/文件名(项目目录被移动/拷贝过)。"""
    if not p:
        raise BiliUploadError(f"project.json 缺少{label}路径")
    path = Path(p)
    if path.is_absolute() and path.is_file():
        return path
    fallback = Path(project_dir) / path.name
    if fallback.is_file():
        return fallback
    raise BiliUploadError(f"找不到{label}文件: {path}(也不在项目目录 {project_dir} 下)")


def _clip_meta(meta: BiliMeta) -> tuple[str, str, list[str]]:
    """按 B站上限裁剪 title/desc/tags; desc 末尾保证含 AI 生成声明。"""
    title = (meta.title or "").strip()[:TITLE_MAX]
    if not title:
        raise BiliUploadError("投稿标题为空, 请检查 project.json 的 meta.title")
    body = (meta.desc or "").replace(AI_DECLARATION, "").strip()
    body = body[: DESC_MAX - len(AI_DECLARATION) - 1].rstrip()
    desc = f"{body}\n{AI_DECLARATION}".strip()
    tags: list[str] = []
    for t in meta.tags:
        t = (t or "").strip()[:TAG_MAX_LEN]
        if t and t not in tags:
            tags.append(t)
    return title, desc, (tags[:TAGS_MAX] or ["面试"])


class _TransientUploadError(Exception):
    pass


def _map_error(exc: BaseException) -> BiliUploadError:
    if isinstance(exc, BiliUploadError):
        return exc
    msg = str(exc)
    if (isinstance(exc, ResponseCodeException) and exc.code == -101) or "未登录" in msg:
        return BiliUploadError(_COOKIE_EXPIRED)
    if isinstance(exc, ValueError):
        return BiliUploadError(f"B站投稿参数不合法: {msg}")
    if isinstance(exc, _TransientUploadError):
        return BiliUploadError(f"B站上传失败: {msg}")
    if isinstance(exc, NetworkException):
        return BiliUploadError(f"B站上传网络错误: {msg}")
    if isinstance(exc, ApiException):
        return BiliUploadError(f"B站接口返回错误: {msg}")
    return BiliUploadError(f"B站上传失败: {type(exc).__name__}: {msg}")


def _retryable(exc: BaseException) -> bool:
    """参数错误 / 接口业务错误(含 cookie 失效)重试无意义, 其余(网络/超时/分块失败)可重试。"""
    return not isinstance(exc, (ValueError, ResponseCodeException, BiliUploadError))


async def _start_once(uploader, timeout: float) -> dict:
    failures = 0
    reason: list[str] = []

    async def on_chunk_failed(*_a, **_k) -> None:
        nonlocal failures
        failures += 1
        if failures > MAX_CHUNK_FAILURES and not reason:
            reason.append(f"分块上传失败超过 {MAX_CHUNK_FAILURES} 次, 已中止")
            await uploader.abort()

    add_listener = getattr(uploader, "add_event_listener", None)
    if add_listener is not None:
        add_listener(video_uploader.VideoUploaderEvents.CHUNK_FAILED.value, on_chunk_failed)
    started = time.monotonic()
    timed_out = False
    try:
        result = await asyncio.wait_for(uploader.start(), timeout=timeout)
    except TimeoutError:
        result, timed_out = None, True
    if result:
        return result
    # 库内部吞掉 CancelledError: 超时/abort 后 start() 可能返回 None 而不是抛异常
    if not reason and (timed_out or time.monotonic() - started >= timeout):
        reason.append(f"上传超时(>{int(timeout)}s)")
        with contextlib.suppress(Exception):
            await uploader.abort()
    raise _TransientUploadError(reason[0] if reason else f"上传被中断, B站返回 {result!r}")


def _resolve_meta(manifest: ProjectManifest) -> BiliMeta:
    """manifest.meta 缺失(降级)时, 用脚本标题拼一个最小可用元数据。"""
    if manifest.meta is not None:
        return manifest.meta
    tags = [t for t in [manifest.industry, "面试"] if t]
    return BiliMeta(title=manifest.script.title[:80], desc="", tags=tags)


async def upload_project(
    project_dir: Path, settings: Settings, *, force: bool = False, dtime: int | None = None,
) -> str:
    """读 project.json 上传 final.mp4 到 B站, 成功后写 upload.json, 返回 bvid。

    未启用 / 缺凭证时抛带抓 cookie 指引的 RuntimeError;
    已有 upload.json(bvid) 且未 force 时抛 AlreadyUploadedError(不发任何网络请求);
    分区/元数据/网络/接口错误统一抛 BiliUploadError(RuntimeError 子类)。
    网络类失败最多重试 2 次(退避 5s/20s), 参数错误与 cookie 失效不重试。
    dtime: 定时发布 unix 时间戳(用 parse_publish_at 生成并校验), None=立即发布。
    """
    if not settings.bili_upload_enabled:
        raise RuntimeError(
            "未启用B站上传。请在 .env 设置 BILI_UPLOAD_ENABLED=true 并配置 cookie 三件套。\n"
            + _COOKIE_GUIDE
        )
    credential = build_credential(settings)
    if credential is None:
        raise RuntimeError(_COOKIE_GUIDE)
    if err := tid_error(settings.bili_tid):
        raise BiliUploadError(err)

    project_dir = Path(project_dir)
    if not force and (record := read_upload_record(project_dir)):
        raise AlreadyUploadedError(record["bvid"])
    manifest = ProjectManifest.model_validate_json(
        (project_dir / "project.json").read_text(encoding="utf-8")
    )
    meta = _resolve_meta(manifest)
    title, desc, tags = _clip_meta(meta)
    video_path = _resolve_artifact(project_dir, manifest.video, "视频")
    cover_path = _resolve_artifact(project_dir, manifest.cover, "封面")
    size_mb = video_path.stat().st_size / (1024 * 1024)
    timeout = max(300.0, size_mb * 20)

    try:
        video_meta = video_uploader.VideoMeta(
            tid=settings.bili_tid,
            title=title,
            desc=desc,
            cover=Picture.from_file(str(cover_path)),
            tags=tags,
            **({"delay_time": dtime} if dtime is not None else {}),
        )
    except ValueError as exc:
        raise _map_error(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise BiliUploadError(f"封面读取失败({cover_path}): {exc}") from exc

    attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        page = video_uploader.VideoUploaderPage(str(video_path), title=title, description=desc)
        uploader = video_uploader.VideoUploader([page], video_meta, credential)
        try:
            result = await _start_once(uploader, timeout)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt + 1 >= attempts or not _retryable(exc):
                err = _map_error(exc)
                if attempt:
                    err = BiliUploadError(f"{err}(已重试 {attempt} 次)")
                raise err from exc
            await asyncio.sleep(_RETRY_DELAYS[attempt])
    bvid = result.get("bvid", "")
    if not bvid:
        raise BiliUploadError(f"B站返回结果缺 bvid: {result!r}")
    write_upload_record(project_dir, bvid, dtime)
    return bvid
