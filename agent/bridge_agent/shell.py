"""跨平台执行命令（主要面向 Windows）：超时、杀进程树、中文输出解码。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"


def decode(data: bytes) -> str:
    """Windows 控制台输出常是 GBK，先试 UTF-8 再退回 GBK。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def popen_kwargs() -> dict:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        proc.kill()


@dataclass
class CmdResult:
    code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def tail(self, n: int = 3000) -> str:
        return self.output[-n:]


def run(cmd: str | list[str], cwd: str, timeout: float, shell: bool | None = None, env: dict | None = None) -> CmdResult:
    """执行命令并合并 stdout/stderr。字符串命令走 shell（用户在配置里写的 test/deploy/package）。"""
    if shell is None:
        shell = isinstance(cmd, str)
    proc = subprocess.Popen(
        cmd, cwd=cwd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env=env, **popen_kwargs(),
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return CmdResult(proc.returncode, decode(out or b""))
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, _ = proc.communicate()
        return CmdResult(-1, decode(out or b"") + f"\n[超时 {int(timeout)} 秒，已终止]", timed_out=True)


def git(cwd: str, *args: str, timeout: float = 120) -> CmdResult:
    return run(["git", *args], cwd=cwd, timeout=timeout, shell=False)
