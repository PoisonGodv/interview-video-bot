"""选题库(backlog): ``<OUT_ROOT>/topics_backlog.json``。

- 生成的选题去重后入库(status=new), 已入库标题作为 ``avoid`` 传给选题链, 避免反复出同一题;
- 视频生成完成(manifest 落盘)时按 topic.json 把对应选题标记为 used;
- ``ivb topics --list/--next`` 与 ``ivb batch`` 从这里取题;
- 批量生成失败时 ``mark_failed`` 记录次数与目录, 下次续跑旧目录, 连续失败 MAX_ATTEMPTS 次后默认跳过;
- 读改写经 ``fcntl.flock`` 跨进程加锁(cron 批量与 Web 同时写库不丢更新)。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import time as dtime_of_day
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from .config import Settings
from .models import Topic

try:
    import fcntl
except ImportError:  # Windows: 仅进程内线程锁
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

BACKLOG_FILE = "topics_backlog.json"
# 传给 LLM 的 avoid 列表上限(取最近的), 控制 prompt 长度
AVOID_LIMIT = 60
# 批量生成连续失败达到该次数后默认跳过(ivb batch --retry-failed 可强制重试)
MAX_ATTEMPTS = 3
BATCH_LOCK_FILE = ".batch.lock"

_lock = threading.Lock()


class BacklogItem(Topic):
    status: Literal["new", "used"] = "new"
    added_at: datetime = Field(default_factory=datetime.now)
    used_at: datetime | None = None
    project: str | None = None  # 生成该选题视频的目录(失败时为可续跑的目录)
    attempts: int = 0  # 批量生成失败次数
    last_error: str | None = None
    failed_at: datetime | None = None


def backlog_path(settings: Settings) -> Path:
    return settings.out_root / BACKLOG_FILE


@contextmanager
def _locked(settings: Settings) -> Iterator[None]:
    """进程内线程锁 + 跨进程 flock(锁文件与选题库同目录)。"""
    with _lock:
        path = backlog_path(settings).with_name(BACKLOG_FILE + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class BatchLockedError(RuntimeError):
    """已有另一个 ivb batch 进程在运行。"""


@contextmanager
def batch_lock(settings: Settings) -> Iterator[None]:
    """整批互斥(非阻塞): cron 重叠触发时后来者直接报错退出, 避免两批抢同一选题。"""
    path = settings.out_root / BATCH_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BatchLockedError(f"已有批量任务在运行(锁文件 {path}), 本次跳过") from None
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _key(title: str, industry: str) -> tuple[str, str]:
    return ("".join(title.split()).lower(), industry.strip())


def load_backlog(settings: Settings) -> list[BacklogItem]:
    """读选题库; 不存在返回空; 损坏时告警并返回空(不覆盖原文件以外的任何东西)。"""
    path = backlog_path(settings)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [BacklogItem.model_validate(x) for x in data.get("topics", [])]
    except (OSError, ValueError, ValidationError, AttributeError) as exc:
        logger.warning("选题库损坏, 按空库处理: %s (%s)", path, exc)
        return []


def _save(settings: Settings, items: list[BacklogItem]) -> None:
    path = backlog_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"topics": [i.model_dump(mode="json") for i in items]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def add_topics(settings: Settings, topics: list[Topic]) -> list[BacklogItem]:
    """去重(标题忽略空白/大小写 + 行业)后入库, 返回本次新增的条目。"""
    with _locked(settings):
        items = load_backlog(settings)
        seen = {_key(i.title, i.industry) for i in items}
        added: list[BacklogItem] = []
        for t in topics:
            k = _key(t.title, t.industry)
            if not t.title.strip() or k in seen:
                continue
            seen.add(k)
            added.append(BacklogItem(**t.model_dump()))
        if added:
            _save(settings, items + added)
        return added


def avoid_titles(settings: Settings, industry: str | None = None) -> list[str]:
    """已入库(含已用)的标题, 供选题链 avoid; 限定行业时只取该行业, 取最近 AVOID_LIMIT 条。"""
    items = [i for i in load_backlog(settings) if industry is None or i.industry == industry]
    return [i.title for i in items][-AVOID_LIMIT:]


def pending(
    settings: Settings, industry: str | None = None, *, max_attempts: int | None = None,
) -> list[BacklogItem]:
    """未使用的选题(入库顺序, 先进先出); 给 max_attempts 时排除失败次数已达上限的。"""
    return [
        i for i in load_backlog(settings)
        if i.status == "new" and (industry is None or i.industry == industry)
        and (max_attempts is None or i.attempts < max_attempts)
    ]


def find(settings: Settings, title: str, industry: str) -> Topic | None:
    """按标题+行业查库内完整选题(带 hook/要点), 供 --topic/批量生成复用。"""
    k = _key(title, industry)
    for i in load_backlog(settings):
        if _key(i.title, i.industry) == k:
            return Topic.model_validate(i.model_dump(include=set(Topic.model_fields)))
    return None


def mark_used(settings: Settings, title: str, industry: str, project: Path | None = None) -> bool:
    """把选题标为 used(不在库里则补录为 used, 以便后续 avoid); 返回是否为库内已有条目。"""
    with _locked(settings):
        items = load_backlog(settings)
        k = _key(title, industry)
        hit = next((i for i in items if _key(i.title, i.industry) == k), None)
        existed = hit is not None
        if hit is None:
            hit = BacklogItem(title=title, industry=industry)
            items.append(hit)
        hit.status = "used"
        hit.used_at = datetime.now()
        if project is not None:
            hit.project = str(project)
        _save(settings, items)
        return existed


def mark_failed(
    settings: Settings, title: str, industry: str, out_dir: Path | None, error: str,
    *, count: bool = True,
) -> int:
    """记录一次批量生成失败(目录留作续跑), 返回累计失败次数; count=False 时不计次(鉴权/预算类)。"""
    with _locked(settings):
        items = load_backlog(settings)
        k = _key(title, industry)
        hit = next((i for i in items if _key(i.title, i.industry) == k), None)
        if hit is None:
            hit = BacklogItem(title=title, industry=industry)
            items.append(hit)
        if count:
            hit.attempts += 1
        hit.last_error = error[:300]
        hit.failed_at = datetime.now()
        if out_dir is not None:
            hit.project = str(out_dir)
        _save(settings, items)
        return hit.attempts


# ---------- 批量上传定时发布时段 ----------

# 与 bilibili.DTIME_* 一致(不 import bilibili, 避免拉起 bilibili_api); 下限多留 10 分钟给上传耗时
SLOT_MIN_AHEAD_SEC = 2 * 3600 + 600
SLOT_MAX_AHEAD_SEC = 15 * 24 * 3600


def parse_slots(value: str) -> list[tuple[int, int]]:
    """``"20:00,12:00"`` → 去重排序的 [(时, 分)]; 不合法抛 ValueError。"""
    slots: set[tuple[int, int]] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            t = datetime.strptime(part, "%H:%M")
        except ValueError:
            raise ValueError(f"发布时段格式应为 HH:MM(多个用逗号分隔), 收到 {part!r}") from None
        slots.add((t.hour, t.minute))
    if not slots:
        raise ValueError("发布时段不能为空, 例如 --slots 20:00,12:00")
    return sorted(slots)


def taken_dtimes(out_root: Path) -> set[int]:
    """扫描 ``<OUT_ROOT>/*/upload.json`` 里已排期的 dtime。"""
    taken: set[int] = set()
    for rec in Path(out_root).glob("*/upload.json"):
        try:
            data = json.loads(rec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("dtime"), int):
            taken.add(data["dtime"])
    return taken


def next_slot(
    slots: list[tuple[int, int]],
    taken: set[int],
    *,
    start: date | None = None,
    now: float | None = None,
) -> int | None:
    """从 start(默认今天)起按天×时段找第一个未占用、且在 B站 2 小时~15 天窗口内的时刻; 没有返回 None。"""
    now = time.time() if now is None else now
    lo, hi = now + SLOT_MIN_AHEAD_SEC, now + SLOT_MAX_AHEAD_SEC
    day = start or datetime.fromtimestamp(now).date()
    last = datetime.fromtimestamp(hi).date()
    while day <= last:
        for h, m in slots:
            ts = int(datetime.combine(day, dtime_of_day(h, m)).timestamp())
            if lo <= ts <= hi and ts not in taken:
                return ts
        day += timedelta(days=1)
    return None
