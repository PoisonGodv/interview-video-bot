"""procs: 取消时终止子进程/工作线程, 且等其退出后才抛 CancelledError。"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest
from proc_helpers import alive, sleeper_cmd, wait_pid

from interview_video import procs


def test_run_captures_output_and_returncode():
    cp = procs.run([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"], timeout=10)
    assert cp.returncode == 3 and cp.stdout.strip() == "hi"


def test_run_timeout_kills_child(tmp_path):
    pid_file = tmp_path / "pid"
    with pytest.raises(subprocess.TimeoutExpired):
        procs.run(sleeper_cmd(pid_file), timeout=0.5)
    assert not alive(wait_pid(pid_file))


@pytest.mark.parametrize("ignore_term", [False, True])
def test_cancel_kills_child_before_raising(tmp_path, monkeypatch, ignore_term):
    monkeypatch.setattr(procs, "TERMINATE_GRACE", 0.5)
    pid_file = tmp_path / "pid"
    exited: list[bool] = []

    def work() -> None:
        try:
            procs.run(sleeper_cmd(pid_file, ignore_term), timeout=60)
        finally:
            exited.append(True)

    async def main() -> float:
        task = asyncio.create_task(procs.run_in_thread(work))
        pid = await asyncio.to_thread(wait_pid, pid_file)
        t0 = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # CancelledError 抛出时: 工作线程已退出, 子进程已被回收
        assert exited == [True]
        assert not alive(pid)
        return time.monotonic() - t0

    elapsed = asyncio.run(main())
    assert elapsed < 10
    if ignore_term:
        assert elapsed >= 0.5  # 忽略 SIGTERM → 宽限期后 kill


def test_checkpoint_stops_python_loop():
    progressed: list[int] = []

    def loop() -> None:
        for i in range(1000):
            procs.checkpoint()
            progressed.append(i)
            time.sleep(0.01)

    async def main() -> None:
        task = asyncio.create_task(procs.run_in_thread(loop))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        n = len(progressed)
        await asyncio.sleep(0.1)
        assert len(progressed) == n < 1000  # 线程已停, 不再推进

    asyncio.run(main())


def test_run_in_thread_passes_result_and_errors():
    async def main() -> None:
        assert await procs.run_in_thread(lambda a, b=0: a + b, 1, b=2) == 3
        with pytest.raises(ValueError):
            await procs.run_in_thread(lambda: (_ for _ in ()).throw(ValueError("x")))

    asyncio.run(main())
    procs.checkpoint()  # 无 scope 时是空操作


def test_render_closes_browser_on_cancel(monkeypatch, tmp_path):
    """render_project: 帧间检查点命中取消 → 抛出且 browser.close() 被调用。"""
    from interview_video import render as render_mod
    from interview_video.config import Settings
    from interview_video.models import Scene, Script

    closed: list[bool] = []
    scope = procs.CancelScope()

    class FakeBrowser:
        def new_page(self, **_):
            return object()

        def close(self):
            closed.append(True)

    class FakePW:
        chromium = type("C", (), {"launch": staticmethod(lambda: FakeBrowser())})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    shots: list[Path] = []

    def fake_shot(page, html, path):
        shots.append(path)
        scope.cancel()  # 第一帧后任务被取消

    monkeypatch.setattr(render_mod, "sync_playwright", lambda: FakePW())
    monkeypatch.setattr(render_mod, "_screenshot_html", fake_shot)
    script = Script(title="t", scenes=[Scene(header="a", narration="x。"),
                                       Scene(header="b", narration="y。")])
    token = procs._current.set(scope)
    try:
        with pytest.raises(procs.ProcessCancelled):
            render_mod.render_project(script, tmp_path, Settings(_env_file=None))
    finally:
        procs._current.reset(token)
    assert len(shots) == 1 and closed == [True]
