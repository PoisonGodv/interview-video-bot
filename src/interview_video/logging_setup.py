"""日志统一配置: CLI/Web 入口调用一次, 全模块 logger 生效。

落点:
- 控制台(stderr): INFO 起, 不干扰 rich 面板输出;
- 文件 <out_root>/ivb.log: DEBUG 起, 保留完整回退/降级/重试痕迹。
"""
from __future__ import annotations

import logging
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


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
            file_handler = logging.FileHandler(out_root / "ivb.log", encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(_FMT))
            file_handler._ivb_managed = True
            handlers.append(file_handler)
        except OSError:
            # out_root 不可写等场景: 只退控制台, 不因日志挂掉主流程
            pass

    console = handlers[0]
    console.setLevel(level)
    formatter = logging.Formatter(_FMT)
    console.setFormatter(formatter)
    console._ivb_managed = True

    root.setLevel(logging.DEBUG)
    for h in handlers:
        root.addHandler(h)
    # 三方库降噪(uvicorn access log / httpx 请求行)
    logging.getLogger("httpx").setLevel(logging.WARNING)
