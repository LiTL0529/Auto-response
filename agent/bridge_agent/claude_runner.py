"""以无界面模式调用本机的 Claude Code：claude -p --output-format stream-json。

使用 Claude Code 自己的登录态（订阅账号）。提示词通过 stdin 传入，避免 Windows 命令行长度限制。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .shell import decode, kill_tree, popen_kwargs

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 15  # 秒


@dataclass
class ClaudeResult:
    ok: bool
    text: str = ""
    session_id: str = ""
    error: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    canceled: bool = False
    timed_out: bool = False


def describe_tool(name: str, inp: dict) -> str:
    base = lambda p: os.path.basename(str(p or ""))  # noqa: E731
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"编辑 {base(inp.get('file_path') or inp.get('notebook_path'))}"
    if name == "Read":
        return f"阅读 {base(inp.get('file_path'))}"
    if name == "Bash":
        return f"运行 {str(inp.get('command', ''))[:50]}"
    if name in ("Grep", "Glob", "LS"):
        return "搜索代码"
    if name == "TodoWrite":
        return "规划步骤"
    return f"使用 {name}"


def resolve_claude(path: str) -> str:
    found = shutil.which(path)
    if not found:
        raise FileNotFoundError(f"找不到 Claude Code 命令：{path}（在 config.yaml 里设置 claude_path 为完整路径）")
    return found


class ClaudeRunner:
    def __init__(self, claude_path: str = "claude", model: str = "", use_api_key: bool = False, max_turns: int = 100):
        self.claude_path = claude_path
        self.model = model
        self.use_api_key = use_api_key
        self.max_turns = max_turns

    def build_args(self, allowed: list[str], disallowed: list[str], resume: str | None) -> list[str]:
        args = [
            resolve_claude(self.claude_path), "-p",
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits",
            "--max-turns", str(self.max_turns),
        ]
        if self.model:
            args += ["--model", self.model]
        if resume:
            args += ["--resume", resume]
        if allowed:
            args += ["--allowedTools", ",".join(allowed)]
        if disallowed:
            args += ["--disallowedTools", ",".join(disallowed)]
        return args

    def env(self) -> dict:
        env = dict(os.environ)
        if not self.use_api_key:
            # 确保走订阅账号登录，而不是按量计费的 API Key
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        return env

    def run(
        self,
        prompt: str,
        cwd: str,
        allowed: list[str],
        disallowed: list[str],
        timeout: float,
        resume: str | None = None,
        on_progress: Callable[[str], bool] | None = None,
    ) -> ClaudeResult:
        """on_progress(描述) 返回 True 表示需要取消。"""
        args = self.build_args(allowed, disallowed, resume)
        log.info("启动 Claude：cwd=%s resume=%s", cwd, resume)
        proc = subprocess.Popen(
            args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.env(), **popen_kwargs(),
        )
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()

        lines: queue.Queue = queue.Queue()
        done = object()
        stderr_chunks: list[bytes] = []

        def read_stdout():
            for raw in proc.stdout:
                lines.put(raw)
            lines.put(done)

        def read_stderr():
            for raw in proc.stderr:
                stderr_chunks.append(raw)

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()

        result = ClaudeResult(ok=False)
        final: dict | None = None
        last_text = ""
        latest = "Claude 开始分析需求"
        start = last_report = time.monotonic()

        while True:
            try:
                item = lines.get(timeout=1)
            except queue.Empty:
                item = None
            if item is done:
                break
            if item:
                try:
                    ev = json.loads(decode(item))
                except json.JSONDecodeError:
                    ev = {}
                etype = ev.get("type")
                if ev.get("session_id"):
                    result.session_id = ev["session_id"]
                if etype == "assistant":
                    for block in (ev.get("message") or {}).get("content") or []:
                        if block.get("type") == "tool_use":
                            latest = describe_tool(block.get("name", ""), block.get("input") or {})
                        elif block.get("type") == "text" and block.get("text"):
                            last_text = block["text"]
                elif etype == "result":
                    final = ev

            now = time.monotonic()
            if now - start > timeout:
                kill_tree(proc)
                result.timed_out = True
                result.error = f"Claude 运行超过 {int(timeout // 60)} 分钟，已终止"
                break
            if on_progress and now - last_report >= PROGRESS_INTERVAL:
                last_report = now
                if on_progress(latest):
                    kill_tree(proc)
                    result.canceled = True
                    result.error = "已取消"
                    break

        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
        if result.canceled or result.timed_out:
            return result

        stderr = decode(b"".join(stderr_chunks)).strip()
        if final is None:
            result.error = f"Claude 异常退出（code={proc.returncode}）：{stderr[-1500:] or '无输出'}"
            return result
        result.cost_usd = float(final.get("total_cost_usd") or 0)
        result.num_turns = int(final.get("num_turns") or 0)
        result.text = (final.get("result") or last_text or "").strip()
        if final.get("is_error") or final.get("subtype") != "success":
            result.error = result.text or final.get("subtype") or "Claude 返回错误"
            if final.get("subtype") == "error_max_turns":
                result.error = f"达到最大轮数 {self.max_turns}，任务可能过大，请拆分需求"
            return result
        result.ok = True
        return result
