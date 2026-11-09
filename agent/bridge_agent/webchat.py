"""网页 Agent：把服务器上网页里的对话交给本机 Claude Code（Claude Agent SDK）执行。

在独立线程里运行，和企业微信任务互不阻塞。每条消息：
1. 给工作目录拍快照（snapshots.py，用于回退）
2. 下载附件到 Agent 数据目录
3. 用 SDK 从上一条消息的会话 fork 出新会话执行（每条消息一个会话 ID，回退时直接接上当时的会话）
4. 需要权限的操作发到网页上等用户点「允许 / 拒绝」
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .snapshots import Snapshots

log = logging.getLogger(__name__)

MAX_FIELD = 4000  # 发到网页的单个字段最多这么长
DISALLOWED = ["AskUserQuestion"]  # 网页上没法回答选择题，让 Claude 直接在回复里问


def resolve_cli(claude_path: str) -> str | None:
    """SDK 不能直接启动 npm 的 claude.cmd，换成它背后的 claude.exe。"""
    p = shutil.which(claude_path) or claude_path
    if p.lower().endswith((".cmd", ".bat")) or not p.lower().endswith(".exe"):
        exe = Path(p).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.is_file():
            return str(exe)
        found = shutil.which("claude.exe")
        return found
    return p


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_FIELD else value[:MAX_FIELD] + f"…（共 {len(value)} 字）"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value[:50]]
    return value


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif isinstance(c, dict) and c.get("type") == "image":
            parts.append("[图片]")
        else:
            parts.append(str(c))
    return "\n".join(parts)


@dataclass
class Turn:
    chat_id: str
    client: Any = None
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    stopped: bool = False


class WebChat:
    def __init__(self, cfg, api, data_dir: Path):
        self.cfg = cfg
        self.api = api
        self.data_dir = Path(data_dir)
        self.folders = {_norm(f["path"]): f for f in cfg.web_folders}
        self.snaps = Snapshots(self.data_dir / "snapshots")
        self.turns: dict[str, Turn] = {}
        self.always: dict[str, set[str]] = {}
        self.cli_path = resolve_cli(cfg.claude_path)
        self.loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ 主循环
    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._thread, name="webchat", daemon=True)
        t.start()
        return t

    def _thread(self) -> None:
        while True:
            try:
                asyncio.run(self.main())
            except Exception:  # noqa: BLE001
                log.exception("网页 Agent 循环异常，10 秒后重启")
                time.sleep(10)

    async def main(self) -> None:
        self.loop = asyncio.get_running_loop()
        if not self.cfg.use_api_key:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        delay = 2
        while True:
            try:
                await asyncio.to_thread(self.api.web_hello, list(self.folders.values()), True)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("网页 Agent 连接服务器失败，%ds 后重试：%s", delay, e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        log.info("网页 Agent 已就绪，目录：%s（CLI：%s）", ", ".join(f["path"] for f in self.folders.values()), self.cli_path)
        last_hello = time.time()
        delay = 2
        while True:
            try:
                if time.time() - last_hello > 60:
                    await asyncio.to_thread(self.api.web_hello, list(self.folders.values()), False)
                    last_hello = time.time()
                cmds = await asyncio.to_thread(self.api.web_next, 25)
                delay = 2
            except Exception as e:  # noqa: BLE001
                log.warning("网页 Agent 拉取指令失败，%ds 后重试：%s", delay, e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            for cmd in cmds:
                try:
                    await self.handle(cmd)
                except Exception:  # noqa: BLE001
                    log.exception("处理网页指令失败：%s", cmd.get("op"))

    async def handle(self, cmd: dict) -> None:
        op, chat_id = cmd.get("op"), cmd.get("chat_id", "")
        if op == "turn":
            asyncio.create_task(self.run_turn(cmd))
        elif op == "stop":
            await self.stop(chat_id)
        elif op == "permission":
            turn = self.turns.get(chat_id)
            fut = turn.pending.pop(cmd["request_id"], None) if turn else None
            if fut and not fut.done():
                fut.set_result((bool(cmd.get("allow")), bool(cmd.get("always"))))
        elif op == "mode":
            turn = self.turns.get(chat_id)
            if turn and turn.client:
                try:
                    await turn.client.set_permission_mode(cmd["mode"])
                except Exception as e:  # noqa: BLE001
                    log.warning("切换权限模式失败：%s", e)
        elif op == "rewind":
            asyncio.create_task(self.rewind(cmd))
        elif op == "fetch":
            asyncio.create_task(self.fetch(cmd))

    async def emit(self, chat_id: str, *events: dict) -> None:
        await asyncio.to_thread(self.api.web_events, chat_id, list(events))

    def _check_folder(self, folder: str) -> str:
        if _norm(folder) not in self.folders:
            raise PermissionError(f"目录不在 web_folders 里：{folder}")
        return folder

    # ------------------------------------------------------------------ 一条消息
    async def run_turn(self, cmd: dict) -> None:
        chat_id, turn_no = cmd["chat_id"], int(cmd["turn"])
        state = Turn(chat_id)
        self.turns[chat_id] = state
        try:
            folder = self._check_folder(cmd["folder"])
            snapshot = await asyncio.to_thread(self.snaps.take, folder, f"chat {chat_id} turn {turn_no}")
            await self.emit(chat_id, {"type": "turn_started", "turn": turn_no, "session_before": cmd.get("resume") or "", "snapshot": snapshot})
            prompt, attach_dir = await self._prompt(cmd)
            await self._run_claude(state, cmd, folder, prompt, attach_dir)
        except Exception as e:  # noqa: BLE001
            log.exception("网页对话执行失败")
            await self.emit(chat_id, {"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            for fut in state.pending.values():
                if not fut.done():
                    fut.set_result((False, False))
            self.turns.pop(chat_id, None)
            try:
                await self.emit(chat_id, {"type": "status", "status": "idle"})
            except Exception:  # noqa: BLE001
                log.exception("上报对话结束失败")

    async def _prompt(self, cmd: dict) -> tuple[str, Path]:
        attach_dir = self.data_dir / "attachments" / cmd["chat_id"]
        attach_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, f in enumerate(cmd.get("files") or [], 1):
            name = os.path.basename(f.get("name") or "file").replace(" ", "_")
            dest = attach_dir / f"{cmd['turn']}-{i}-{name}"
            await asyncio.to_thread(self.api.download, f["file_id"], dest)
            paths.append(str(dest))
        text = cmd.get("text") or ""
        if paths:
            text += "\n\n用户上传了这些文件（绝对路径，用 Read 工具查看；图片可以直接看）：\n" + "\n".join(f"- {p}" for p in paths)
        return text, attach_dir

    async def _run_claude(self, state: Turn, cmd: dict, folder: str, prompt: str, attach_dir: Path) -> None:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            PermissionResultAllow,
            PermissionResultDeny,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        chat_id = state.chat_id
        always = self.always.setdefault(chat_id, set())

        async def can_use_tool(tool: str, tool_input: dict, ctx) -> Any:
            if tool in always:
                return PermissionResultAllow()
            rid = uuid.uuid4().hex[:12]
            fut = asyncio.get_running_loop().create_future()
            state.pending[rid] = fut
            await self.emit(chat_id, {"type": "permission", "request_id": rid, "tool": tool, "input": _clip(tool_input)})
            allow, keep = await fut
            if allow:
                if keep:
                    always.add(tool)
                return PermissionResultAllow()
            return PermissionResultDeny(message="用户在网页上拒绝了这个操作", interrupt=state.stopped)

        resume = cmd.get("resume") or None
        options = ClaudeAgentOptions(
            cwd=folder,
            cli_path=self.cli_path,
            permission_mode=cmd.get("mode") or "acceptEdits",
            resume=resume,
            fork_session=bool(resume),
            can_use_tool=can_use_tool,
            disallowed_tools=DISALLOWED,
            add_dirs=[str(attach_dir)],
            model=self.cfg.claude_model or None,
            effort=self.cfg.claude_effort or None,
            setting_sources=["user", "project", "local"],
            max_turns=self.cfg.max_turns,
        )
        session_id = ""
        async with ClaudeSDKClient(options=options) as client:
            state.client = client
            await client.query(prompt)
            async for msg in client.receive_response():
                events: list[dict] = []
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    session_id = msg.data.get("session_id", session_id)
                elif isinstance(msg, AssistantMessage) and not msg.parent_tool_use_id:
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            events.append({"type": "text", "text": b.text})
                        elif isinstance(b, ToolUseBlock):
                            events.append({"type": "tool_use", "id": b.id, "name": b.name, "input": _clip(b.input)})
                    if msg.error:
                        events.append({"type": "error", "text": f"模型返回错误：{msg.error}"})
                elif isinstance(msg, UserMessage) and not msg.parent_tool_use_id and isinstance(msg.content, list):
                    for b in msg.content:
                        if isinstance(b, ToolResultBlock):
                            events.append({"type": "tool_result", "tool_use_id": b.tool_use_id,
                                           "content": _clip(_result_text(b.content)), "is_error": bool(b.is_error)})
                elif isinstance(msg, ResultMessage):
                    session_id = msg.session_id or session_id
                    ok = not msg.is_error and not state.stopped
                    err = "" if ok else ("已停止" if state.stopped else (msg.result or msg.subtype))
                    events.append({"type": "session", "session_id": session_id})
                    events.append({"type": "result", "ok": ok, "duration_ms": msg.duration_ms,
                                   "cost_usd": msg.total_cost_usd, "error": _clip(err)})
                if events:
                    await self.emit(chat_id, *events)
            state.client = None
        if session_id:
            await self.emit(chat_id, {"type": "session", "session_id": session_id})

    async def stop(self, chat_id: str) -> None:
        state = self.turns.get(chat_id)
        if not state:
            return
        state.stopped = True
        for fut in list(state.pending.values()):
            if not fut.done():
                fut.set_result((False, False))
        if state.client:
            try:
                await state.client.interrupt()
            except Exception as e:  # noqa: BLE001
                log.warning("中断 Claude 失败：%s", e)

    # ------------------------------------------------------------------ 回退 / 取文件
    async def rewind(self, cmd: dict) -> None:
        chat_id, turn = cmd["chat_id"], int(cmd["turn"])
        try:
            if chat_id in self.turns:
                raise RuntimeError("这个对话还在执行，先停止再回退")
            folder = self._check_folder(cmd["folder"])
            changed, backup = await asyncio.to_thread(self.snaps.restore, folder, cmd["snapshot"])
            log.info("对话 %s 回退到第 %d 条之前，变动 %d 个文件，回退前的状态保存在快照 %s", chat_id, turn, len(changed), backup)
            await self.emit(chat_id, {"type": "rewound", "turn": turn, "ok": True, "changed": changed[:200], "backup": backup})
        except Exception as e:  # noqa: BLE001
            log.exception("回退失败")
            await self.emit(chat_id, {"type": "rewound", "turn": turn, "ok": False, "error": str(e)})

    async def fetch(self, cmd: dict) -> None:
        chat_id = cmd["chat_id"]
        try:
            folder = Path(self._check_folder(cmd["folder"])).resolve()
            raw = Path(cmd.get("path", "").strip().strip('"'))
            path = (raw if raw.is_absolute() else folder / raw).resolve()
            if path != folder and folder not in path.parents:
                raise PermissionError("只能取回项目目录里的文件")
            if not path.is_file():
                raise FileNotFoundError(f"找不到文件：{path}")
            if path.stat().st_size > 200 * 1024 * 1024:
                raise ValueError("文件超过 200 MB")
            file_id = await asyncio.to_thread(self.api.upload, path)
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            await self.emit(chat_id, {"type": "file", "file_id": file_id, "name": path.name, "content_type": ctype,
                                      "path": str(path.relative_to(folder))})
        except Exception as e:  # noqa: BLE001
            await self.emit(chat_id, {"type": "error", "text": f"取回文件失败：{e}"})
