"""B站上传封装(可选, 默认关): bilibili_api video_uploader。

已安装版本实测(bilibili-api-python ≥16.1.0):
- ``VideoUploader(pages, meta, credential, cover='', line=None)`` + ``await uploader.start() -> {"bvid", "aid"}``
- ``VideoUploaderPage(path: str, title: str, description: str = "")``
- ``VideoMeta(tid, title, desc, cover, tags, ...)``(cover 为已上传封面 URL)
- 封面上传: ``Picture.from_file(path)`` → ``await upload_cover(pic, credential) -> url``
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from bilibili_api import Credential, video_uploader
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
    已有 upload.json(bvid) 且未 force 时抛 AlreadyUploadedError(不发任何网络请求)。
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

    project_dir = Path(project_dir)
    if not force and (record := read_upload_record(project_dir)):
        raise AlreadyUploadedError(record["bvid"])
    manifest = ProjectManifest.model_validate_json(
        (project_dir / "project.json").read_text(encoding="utf-8")
    )
    meta = _resolve_meta(manifest)

    # 简介末尾确保含 AI 生成声明(合规)
    desc = meta.desc or ""
    if AI_DECLARATION not in desc:
        desc = f"{desc.rstrip()}\n{AI_DECLARATION}".strip()

    # 先传封面拿 URL, 再组装 meta
    cover_url = await video_uploader.upload_cover(
        Picture.from_file(str(manifest.cover)), credential
    )
    page = video_uploader.VideoUploaderPage(
        str(manifest.video), title=meta.title, description=desc
    )
    video_meta = video_uploader.VideoMeta(
        tid=settings.bili_tid,
        title=meta.title,
        desc=desc,
        cover=cover_url,
        tags=meta.tags,
        **({"delay_time": dtime} if dtime is not None else {}),
    )
    uploader = video_uploader.VideoUploader(
        [page], video_meta, credential
    )
    result = await uploader.start()
    bvid = result.get("bvid", "")
    if not bvid:
        raise RuntimeError(f"B站返回结果缺 bvid: {result!r}")
    write_upload_record(project_dir, bvid, dtime)
    return bvid
