"""可取消的外部进程 / 工作线程。

阻塞的媒体步骤(ffmpeg/ffprobe/Playwright/Pillow)放在工作线程里跑; 协程被取消时
``asyncio.to_thread`` 只丢弃结果, 线程和子进程会继续写文件。这里用一个随 contextvar
传进线程的 ``CancelScope`` 把两者串起来:

- ``run_in_thread``: 协程被取消 → scope.cancel()(置位 + terminate 已登记子进程)
  → 等工作线程真正退出后才向上抛 CancelledError, 调用方的 finally(如 Web 单飞锁)
  因此总在子进程退出之后执行;
- ``run``: 替代 subprocess.run, 子进程登记到当前 scope, 取消时 terminate,
  宽限期后仍未退出则 kill; 超时同样先杀再抛 TimeoutExpired;
- ``checkpoint``: 纯 Python 循环(截图/字幕条)在迭代间调用, 已取消则抛 ProcessCancelled。
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

# terminate 后等待子进程自行退出的宽限期(秒), 超过则 kill
TERMINATE_GRACE = 3.0
# communicate 轮询间隔(秒): 决定取消/超时的响应延迟
_POLL = 0.2


class ProcessCancelled(RuntimeError):
    """所在任务已被取消(子进程已被终止)。"""


class CancelScope:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        """置位并向在跑的子进程发 SIGTERM(不阻塞; kill 升级由工作线程负责)。"""
        self._event.set()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except OSError:
                    pass

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)
        if self.cancelled:
            proc.terminate()

    def discard(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)


_current: contextvars.ContextVar[CancelScope | None] = contextvars.ContextVar(
    "ivb_cancel_scope", default=None
)


def current_scope() -> CancelScope | None:
    return _current.get()


def checkpoint() -> None:
    scope = _current.get()
    if scope is not None and scope.cancelled:
        raise ProcessCancelled("任务已取消")


def _stop(proc: subprocess.Popen) -> None:
    """terminate → 宽限期 → kill, 并回收管道。"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(TERMINATE_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    try:
        proc.communicate(timeout=1)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def run(
    cmd: list[str], *, cwd: Any = None, timeout: float
) -> subprocess.CompletedProcess[str]:
    """同 ``subprocess.run(capture_output=True, text=True)``, 但可被所在 scope 取消。"""
    scope = _current.get()
    checkpoint()
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if scope is not None:
        scope.add(proc)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=_POLL)
                break
            except subprocess.TimeoutExpired:
                if scope is not None and scope.cancelled:
                    raise ProcessCancelled("任务已取消") from None
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout) from None
    except BaseException:
        _stop(proc)
        raise
    finally:
        if scope is not None:
            scope.discard(proc)
    checkpoint()  # 被 cancel() terminate 掉的进程也按取消处理, 不当成普通失败
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


async def run_in_thread(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``asyncio.to_thread`` 的可取消版: 取消时终止子进程并等线程退出后再抛 CancelledError。"""
    scope = CancelScope()
    ctx = contextvars.copy_context()
    ctx.run(_current.set, scope)
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, functools.partial(ctx.run, fn, *args, **kwargs))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        scope.cancel()
        while not fut.done():
            try:
                await asyncio.shield(fut)
            except asyncio.CancelledError:
                continue  # 重复取消也要等清理完成
            except BaseException:  # noqa: BLE001 —— 线程内 ProcessCancelled 等, 以取消为准
                break
        if fut.done() and not fut.cancelled():
            fut.exception()  # 标记已取出, 避免 "exception was never retrieved"
        raise
