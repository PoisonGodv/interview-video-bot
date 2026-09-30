"""日志统一配置: CLI/Web 入口调用一次, 全模块 logger 生效。

落点:
- 控制台(stderr): INFO 起, 不干扰 rich 面板输出;
- 文件 <out_root>/ivb.log: DEBUG 起, 保留完整回退/降级/重试痕迹; 按 5MB 轮转, 保留 3 份。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
# 三方库 DEBUG/INFO 噪声(请求行/连接池/事件循环/websocket 帧/图片插件/浏览器驱动)
NOISY_LOGGERS = ("httpx", "httpcore", "asyncio", "websockets", "PIL", "playwright")


class _ConsoleFormatter(logging.Formatter):
    """控制台不打印堆栈(完整堆栈只进 ivb.log); IVB_DEBUG=1 时保留。"""

    def format(self, record: logging.LogRecord) -> str:
        if os.environ.get("IVB_DEBUG") or not (record.exc_info or record.exc_text):
            return super().format(record)
        saved = record.exc_info, record.exc_text, record.stack_info
        record.exc_info = record.exc_text = record.stack_info = None
        try:
            return super().format(record) + "  (堆栈见 ivb.log)"
        finally:
            record.exc_info, record.exc_text, record.stack_info = saved


def setup_logging(out_root: Path | None = None, level: int = logging.INFO) -> None:
    """入口调用一次; 重复调用幂等(先清掉自己加的 handler)。"""
    root = logging.getLogger()

    # 幂等: 移除本函数上一次加的 handler, 保留 uvicorn/pytest 等外部 handler
    for h in list(root.handlers):
        if getattr(h, "_ivb_managed", False):
            root.removeHandler(h)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if out_root is not None:
        try:
            out_root.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                out_root / "ivb.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(_FMT))
            file_handler._ivb_managed = True
            handlers.append(file_handler)
        except OSError:
            # out_root 不可写等场景: 只退控制台, 不因日志挂掉主流程
            pass

    console = handlers[0]
    console.setLevel(level)
    console.setFormatter(_ConsoleFormatter(_FMT))
    console._ivb_managed = True

    root.setLevel(logging.DEBUG)
    for h in handlers:
        root.addHandler(h)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
