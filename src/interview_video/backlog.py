"""选题库(backlog): ``<OUT_ROOT>/topics_backlog.json``。

- 生成的选题去重后入库(status=new), 已入库标题作为 ``avoid`` 传给选题链, 避免反复出同一题;
- 视频生成完成(manifest 落盘)时按 topic.json 把对应选题标记为 used;
- ``ivb topics --list/--next`` 与 ``ivb batch`` 从这里取题。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from .config import Settings
from .models import Topic

logger = logging.getLogger(__name__)

BACKLOG_FILE = "topics_backlog.json"
# 传给 LLM 的 avoid 列表上限(取最近的), 控制 prompt 长度
AVOID_LIMIT = 60

_lock = threading.Lock()


class BacklogItem(Topic):
    status: Literal["new", "used"] = "new"
    added_at: datetime = Field(default_factory=datetime.now)
    used_at: datetime | None = None
    project: str | None = None  # 生成该选题视频的目录


def backlog_path(settings: Settings) -> Path:
    return settings.out_root / BACKLOG_FILE


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
    with _lock:
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


def pending(settings: Settings, industry: str | None = None) -> list[BacklogItem]:
    """未使用的选题(入库顺序, 先进先出)。"""
    return [
        i for i in load_backlog(settings)
        if i.status == "new" and (industry is None or i.industry == industry)
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
    with _lock:
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
