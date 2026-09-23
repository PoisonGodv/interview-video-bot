"""B站上传封装(可选, 默认关): bilibili_api video_uploader。

已安装版本实测(bilibili-api-python ≥16.1.0):
- ``VideoUploader(pages, meta, credential, cover='', line=None)`` + ``await uploader.start() -> {"bvid", "aid"}``
- ``VideoUploaderPage(path: str, title: str, description: str = "")``
- ``VideoMeta(tid, title, desc, cover, tags, ...)``(cover 为已上传封面 URL)
- 封面上传: ``Picture.from_file(path)`` → ``await upload_cover(pic, credential) -> url``
"""
from __future__ import annotations

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


def build_credential(settings: Settings) -> Credential | None:
    """凭证三件套齐全才返回 Credential, 否则 None。"""
    if settings.bili_sessdata and settings.bili_jct and settings.bili_buvid3:
        return Credential(
            sessdata=settings.bili_sessdata,
            bili_jct=settings.bili_jct,
            buvid3=settings.bili_buvid3,
        )
    return None


def _resolve_meta(manifest: ProjectManifest) -> BiliMeta:
    """manifest.meta 缺失(降级)时, 用脚本标题拼一个最小可用元数据。"""
    if manifest.meta is not None:
        return manifest.meta
    tags = [t for t in [manifest.industry, "面试"] if t]
    return BiliMeta(title=manifest.script.title[:80], desc="", tags=tags)


async def upload_project(project_dir: Path, settings: Settings) -> str:
    """读 project.json 上传 final.mp4 到 B站, 返回 bvid。

    未启用 / 缺凭证时抛带抓 cookie 指引的 RuntimeError。
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
    )
    uploader = video_uploader.VideoUploader(
        [page], video_meta, credential
    )
    result = await uploader.start()
    bvid = result.get("bvid", "")
    return bvid
