"""测试辅助: 可观测 pid 的长跑子进程。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def sleeper_cmd(pid_file: Path, ignore_term: bool = False) -> list[str]:
    """长跑子进程: 先写 pid 文件, 再睡 30s(可选忽略 SIGTERM, 逼出 kill 升级)。"""
    code = (
        "import os, signal, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    return [sys.executable, "-c", code]


def wait_pid(pid_file: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        time.sleep(0.02)
    raise AssertionError("子进程未启动")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # 已退出但未被回收的僵尸也算死(procs 会 wait, 这里只防御)
    try:
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True
