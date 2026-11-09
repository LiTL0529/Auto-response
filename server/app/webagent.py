"""网页 Agent：在网站上和家里电脑的 Claude Code 对话（/agent/）。

浏览器 ──HTTPS──▶ 服务器（本模块，存对话、推送事件）◀──长轮询── 家里电脑 Agent（webchat.py，用 Claude Agent SDK 跑）

- 登录：页面上点「发送登录请求」，服务器把确认链接发到负责人的企业微信，负责人确认后这个浏览器获得 7 天登录
- 浏览器收事件用 SSE（/agent/api/chats/{id}/stream），发消息/审批/回退用 POST
- 家里电脑：GET /api/agent/web/next 拿指令，POST /api/agent/web/events 回传事件（Bearer AGENT_TOKEN）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "webagent_static"
COOKIE = "wa_session"
SESSION_DAYS = 7
LOGIN_TTL = 300  # 登录请求 5 分钟有效
MODES = ("default", "acceptEdits", "bypassPermissions", "plan")
MAX_UPLOAD = 50 * 1024 * 1024
AGENT_ONLINE_SECONDS = 90


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class WebStore:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, folder TEXT NOT NULL, mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'idle', session_id TEXT NOT NULL DEFAULT '',
                turns INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, turn INTEGER NOT NULL,
                type TEXT NOT NULL, data TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS events_chat ON events(chat_id, id);
            CREATE TABLE IF NOT EXISTS turns (
                chat_id TEXT NOT NULL, turn INTEGER NOT NULL, session_before TEXT NOT NULL DEFAULT '',
                snapshot TEXT NOT NULL DEFAULT '', PRIMARY KEY(chat_id, turn));
            CREATE TABLE IF NOT EXISTS logins (
                id TEXT PRIMARY KEY, code TEXT NOT NULL, token_hash TEXT NOT NULL, ip TEXT, ua TEXT,
                status TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, ip TEXT, ua TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL);
            """
        )

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, args)

    # chats
    def chat(self, chat_id: str) -> dict | None:
        return self.one("SELECT * FROM chats WHERE id=?", (chat_id,))

    def update_chat(self, chat_id: str, **fields) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE chats SET {sets} WHERE id=?", (*fields.values(), chat_id))

    # events
    def add_event(self, chat_id: str, turn: int, etype: str, data: dict) -> dict:
        now = time.time()
        cur = self.x(
            "INSERT INTO events(chat_id, turn, type, data, created_at) VALUES(?,?,?,?,?)",
            (chat_id, turn, etype, json.dumps(data, ensure_ascii=False), now),
        )
        return {"id": cur.lastrowid, "turn": turn, "type": etype, "data": data, "hidden": 0, "created_at": now}

    def events(self, chat_id: str, after: int = 0) -> list[dict]:
        rows = self.q("SELECT id, turn, type, data, hidden, created_at FROM events WHERE chat_id=? AND id>? ORDER BY id", (chat_id, after))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows


class WebHub:
    """对话状态、给家里电脑的指令队列、给浏览器的实时推送。"""

    def __init__(self, store: WebStore, files, notify: Callable[[str], Awaitable[bool]], base_url: str):
        self.store = store
        self.files = files
        self.notify = notify
        self.base_url = base_url.rstrip("/")
        self.commands: list[dict] = []
        self.command_event = asyncio.Event()
        self.subscribers: dict[str, set[asyncio.Queue]] = {}
        self.list_subscribers: set[asyncio.Queue] = set()
        self.folders: list[dict] = []
        self.agent_seen = 0.0
        self.last_viewed: dict[str, float] = {}
        self.notified: set[tuple[str, int, str]] = set()

    # ---------------------------------------------------------------- 家里电脑
    def agent_online(self) -> bool:
        return time.time() - self.agent_seen < AGENT_ONLINE_SECONDS

    def send_command(self, cmd: dict) -> None:
        self.commands.append(cmd)
        self.command_event.set()

    async def next_commands(self, wait: float) -> list[dict]:
        self.agent_seen = time.time()
        deadline = time.monotonic() + wait
        while not self.commands:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.command_event.clear()
            try:
                await asyncio.wait_for(self.command_event.wait(), timeout=min(remaining, 10))
            except asyncio.TimeoutError:
                pass
        self.agent_seen = time.time()
        cmds, self.commands = self.commands, []
        return cmds

    async def agent_events(self, chat_id: str, events: list[dict]) -> None:
        chat = self.store.chat(chat_id)
        if not chat:
            return
        turn = chat["turns"]
        for ev in events:
            etype = ev.get("type", "")
            data = {k: v for k, v in ev.items() if k != "type"}
            if etype == "turn_started":
                self.store.x(
                    "INSERT OR REPLACE INTO turns(chat_id, turn, session_before, snapshot) VALUES(?,?,?,?)",
                    (chat_id, data.get("turn", turn), data.get("session_before", ""), data.get("snapshot", "")),
                )
                continue
            if etype == "session":
                self.store.update_chat(chat_id, session_id=data.get("session_id", ""))
                continue
            if etype == "status":
                await self.set_status(chat_id, data.get("status", "idle"))
                if data.get("status") == "idle":
                    await self._maybe_notify(chat_id, turn, "done", f"✅ 网页 Agent「{chat['title']}」这一轮做完了")
                continue
            if etype == "rewound":
                await self._finish_rewind(chat_id, data)
                continue
            if etype == "file":
                data["url"] = f"api/files/{data.get('file_id')}"
            row = self.store.add_event(chat_id, turn, etype, data)
            await self.publish(chat_id, row)
            if etype == "permission":
                await self.set_status(chat_id, "waiting")
                tool = data.get("tool", "")
                await self._maybe_notify(chat_id, turn, f"perm:{data.get('request_id')}", f"🟡 网页 Agent「{chat['title']}」需要你批准：{tool}")

    async def set_status(self, chat_id: str, status: str) -> None:
        self.store.update_chat(chat_id, status=status)
        await self.publish(chat_id, {"id": 0, "type": "chat_status", "data": {"status": status}})
        await self.publish_list()

    async def _maybe_notify(self, chat_id: str, turn: int, kind: str, text: str) -> None:
        """页面没人在看（1 分钟内没有连接）时，发企业微信提醒。"""
        if self.subscribers.get(chat_id) or time.time() - self.last_viewed.get(chat_id, 0) < 60:
            return
        key = (chat_id, turn, kind)
        if key in self.notified:
            return
        self.notified.add(key)
        await self.notify(f"{text}\n打开：{self.base_url}/agent/#{chat_id}")

    # ---------------------------------------------------------------- 推送
    async def publish(self, chat_id: str, row: dict) -> None:
        for q in list(self.subscribers.get(chat_id, ())):
            q.put_nowait(row)

    async def publish_list(self) -> None:
        for q in list(self.list_subscribers):
            q.put_nowait({"type": "chats"})

    # ---------------------------------------------------------------- 对话操作
    def chats(self) -> list[dict]:
        return self.store.q(
            "SELECT id, title, folder, mode, status, turns, updated_at FROM chats WHERE archived=0 ORDER BY updated_at DESC LIMIT 200"
        )

    def create_chat(self, folder: str, mode: str, title: str = "") -> dict:
        if folder not in {f["path"] for f in self.folders}:
            raise HTTPException(400, "这个目录不在家里电脑允许的列表里")
        if mode not in MODES:
            raise HTTPException(400, "未知模式")
        now = time.time()
        chat_id = uuid.uuid4().hex[:16]
        self.store.x(
            "INSERT INTO chats(id, title, folder, mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (chat_id, title or "新对话", folder, mode, now, now),
        )
        return self.store.chat(chat_id)

    async def user_message(self, chat_id: str, text: str, file_ids: list[str]) -> dict:
        chat = self._chat(chat_id)
        if chat["status"] in ("running", "waiting", "rewinding"):
            raise HTTPException(409, "Claude 还在处理上一条消息，可以先点「停止」")
        if not self.agent_online():
            raise HTTPException(503, "家里电脑的 Agent 不在线")
        files = []
        for fid in file_ids[:20]:
            f = self.files.db.get_file(fid)
            if f:
                files.append({"file_id": fid, "name": f["filename"], "content_type": f["content_type"]})
        turn = chat["turns"] + 1
        title = chat["title"] if chat["turns"] else (text.strip().splitlines() or ["新对话"])[0][:40] or "新对话"
        self.store.update_chat(chat_id, turns=turn, title=title, status="running")
        row = self.store.add_event(chat_id, turn, "user", {"text": text, "files": files})
        await self.publish(chat_id, row)
        await self.set_status(chat_id, "running")
        self.send_command(
            {"op": "turn", "chat_id": chat_id, "folder": chat["folder"], "mode": chat["mode"], "turn": turn,
             "text": text, "files": files, "resume": chat["session_id"]}
        )
        return row

    def stop(self, chat_id: str) -> None:
        self._chat(chat_id)
        self.send_command({"op": "stop", "chat_id": chat_id})

    async def permission(self, chat_id: str, request_id: str, allow: bool, always: bool) -> None:
        chat = self._chat(chat_id)
        self.send_command({"op": "permission", "chat_id": chat_id, "request_id": request_id, "allow": allow, "always": always})
        row = self.store.add_event(chat_id, chat["turns"], "permission_done", {"request_id": request_id, "allow": allow, "always": always})
        await self.publish(chat_id, row)
        await self.set_status(chat_id, "running")

    async def set_mode(self, chat_id: str, mode: str) -> None:
        self._chat(chat_id)
        if mode not in MODES:
            raise HTTPException(400, "未知模式")
        self.store.update_chat(chat_id, mode=mode)
        self.send_command({"op": "mode", "chat_id": chat_id, "mode": mode})
        await self.publish_list()

    async def rewind(self, chat_id: str, turn: int) -> None:
        """回到第 turn 条消息发出之前：文件恢复到当时的快照，对话从当时的会话继续。"""
        chat = self._chat(chat_id)
        if chat["status"] != "idle":
            raise HTTPException(409, "先等 Claude 停下来再回退")
        t = self.store.one("SELECT * FROM turns WHERE chat_id=? AND turn=?", (chat_id, turn))
        if not t or not t["snapshot"]:
            raise HTTPException(400, "这条消息之前没有文件快照，无法回退")
        await self.set_status(chat_id, "rewinding")
        self.send_command({"op": "rewind", "chat_id": chat_id, "folder": chat["folder"], "turn": turn, "snapshot": t["snapshot"]})

    async def _finish_rewind(self, chat_id: str, data: dict) -> None:
        turn = int(data.get("turn") or 0)
        if data.get("ok"):
            t = self.store.one("SELECT * FROM turns WHERE chat_id=? AND turn=?", (chat_id, turn)) or {}
            self.store.x("UPDATE events SET hidden=1 WHERE chat_id=? AND turn>=?", (chat_id, turn))
            self.store.x("DELETE FROM turns WHERE chat_id=? AND turn>=?", (chat_id, turn))
            self.store.update_chat(chat_id, turns=turn - 1, session_id=t.get("session_before", ""))
            row = self.store.add_event(chat_id, turn - 1, "rewound", {"turn": turn, "changed": data.get("changed", [])})
        else:
            row = self.store.add_event(chat_id, self._chat(chat_id)["turns"], "error", {"text": f"回退失败：{data.get('error', '')}"})
        await self.publish(chat_id, {**row, "reload": bool(data.get("ok"))})
        await self.set_status(chat_id, "idle")

    def fetch_file(self, chat_id: str, path: str) -> None:
        chat = self._chat(chat_id)
        self.send_command({"op": "fetch", "chat_id": chat_id, "folder": chat["folder"], "path": path})

    def archive(self, chat_id: str) -> None:
        self._chat(chat_id)
        self.store.update_chat(chat_id, archived=1)

    def _chat(self, chat_id: str) -> dict:
        chat = self.store.chat(chat_id)
        if not chat or chat["archived"]:
            raise HTTPException(404, "对话不存在")
        return chat

    # ---------------------------------------------------------------- 登录
    def create_login(self, ip: str, ua: str) -> tuple[dict, str]:
        now = time.time()
        recent = self.store.one("SELECT COUNT(*) n FROM logins WHERE created_at>? AND status='pending'", (now - LOGIN_TTL,))["n"]
        same_ip = self.store.one("SELECT COUNT(*) n FROM logins WHERE created_at>? AND ip=?", (now - 60, ip))["n"]
        if recent >= 5 or same_ip >= 3:
            raise HTTPException(429, "登录请求太频繁，请稍后再试")
        login = {"id": secrets.token_urlsafe(16), "code": f"{secrets.randbelow(1000000):06d}"}
        token = secrets.token_urlsafe(32)
        self.store.x(
            "INSERT INTO logins(id, code, token_hash, ip, ua, status, created_at) VALUES(?,?,?,?,?, 'pending', ?)",
            (login["id"], login["code"], _h(token), ip, ua[:300], now),
        )
        return login, token

    def login_by_token(self, token: str) -> dict | None:
        row = self.store.one("SELECT * FROM logins WHERE token_hash=?", (_h(token or ""),))
        if not row or row["status"] != "pending" or time.time() - row["created_at"] > LOGIN_TTL:
            return None
        return row

    def approve_login(self, token: str) -> bool:
        row = self.login_by_token(token)
        if not row:
            return False
        self.store.x("UPDATE logins SET status='approved' WHERE id=?", (row["id"],))
        return True

    def claim_login(self, login_id: str, ip: str, ua: str) -> str | None:
        """浏览器轮询：确认后换成会话（只能领取一次）。"""
        row = self.store.one("SELECT * FROM logins WHERE id=?", (login_id,))
        if not row or time.time() - row["created_at"] > LOGIN_TTL + 60:
            return None
        if row["status"] != "approved":
            return None
        self.store.x("UPDATE logins SET status='used' WHERE id=?", (login_id,))
        session = secrets.token_urlsafe(32)
        now = time.time()
        self.store.x(
            "INSERT INTO sessions(token_hash, ip, ua, created_at, expires_at) VALUES(?,?,?,?,?)",
            (_h(session), ip, ua[:300], now, now + SESSION_DAYS * 86400),
        )
        return session

    def login_status(self, login_id: str) -> str:
        row = self.store.one("SELECT status, created_at FROM logins WHERE id=?", (login_id,))
        if not row:
            return "missing"
        if row["status"] == "pending" and time.time() - row["created_at"] > LOGIN_TTL:
            return "expired"
        return row["status"]

    def valid_session(self, token: str | None) -> bool:
        if not token:
            return False
        row = self.store.one("SELECT expires_at FROM sessions WHERE token_hash=?", (_h(token),))
        return bool(row and row["expires_at"] > time.time())

    def logout(self, token: str | None) -> None:
        if token:
            self.store.x("DELETE FROM sessions WHERE token_hash=?", (_h(token),))


# ==================================================================== 路由
class NewChat(BaseModel):
    folder: str
    mode: str = "acceptEdits"


class SendMessage(BaseModel):
    text: str = ""
    files: list[str] = []


class PermissionDecision(BaseModel):
    request_id: str
    allow: bool
    always: bool = False


class ModeBody(BaseModel):
    mode: str


class RewindBody(BaseModel):
    turn: int


class FetchBody(BaseModel):
    path: str


class TokenBody(BaseModel):
    t: str


class AgentWebHello(BaseModel):
    folders: list[dict]
    startup: bool = False


class AgentEvents(BaseModel):
    chat_id: str
    events: list[dict]


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()


def register(app: FastAPI, hub_getter: Callable[[], WebHub], require_agent, send_login_request: Callable[[dict, str, str, str], Awaitable[bool]]) -> None:
    def hub() -> WebHub:
        return hub_getter()

    def require_user(request: Request) -> None:
        if not hub().valid_session(request.cookies.get(COOKIE)):
            raise HTTPException(401, "请先登录")

    def require_user_write(request: Request, x_requested_with: str = Header("")) -> None:
        require_user(request)
        if x_requested_with != "webagent":  # 防 CSRF（配合 SameSite=Strict）
            raise HTTPException(403, "bad request")

    # ------------------------------------------------ 页面
    @app.get("/agent", include_in_schema=False)
    async def agent_root():
        return Response(status_code=308, headers={"Location": "/agent/"})

    @app.get("/agent/", include_in_schema=False)
    async def agent_index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/agent/static/{name}", include_in_schema=False)
    async def agent_static(name: str):
        path = STATIC / name
        if "/" in name or "\\" in name or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------ 登录
    @app.post("/agent/api/login/request")
    async def login_request(request: Request):
        ip, ua = _client_ip(request), request.headers.get("user-agent", "")
        login, token = hub().create_login(ip, ua)
        url = f"{hub().base_url}/agent/approve?t={token}"
        if not await send_login_request(login, url, ip, ua):
            raise HTTPException(502, "发送企业微信消息失败")
        return {"id": login["id"], "code": login["code"]}

    @app.get("/agent/api/login/status")
    async def login_status(id: str, request: Request, response: Response):
        status = hub().login_status(id)
        if status == "approved":
            session = hub().claim_login(id, _client_ip(request), request.headers.get("user-agent", ""))
            if session:
                response.set_cookie(
                    COOKIE, session, max_age=SESSION_DAYS * 86400, httponly=True, secure=True, samesite="strict", path="/agent"
                )
                return {"status": "ok"}
        return {"status": status}

    @app.get("/agent/approve", include_in_schema=False)
    async def approve_page(t: str = ""):
        row = hub().login_by_token(t)
        if not row:
            body = "<p>这个登录请求已失效（5 分钟有效、只能用一次）。请在网页上重新发起。</p>"
        else:
            body = (
                f"<p>有浏览器请求登录网页 Agent：</p><p>验证码 <b>{row['code']}</b><br>IP {_esc(row['ip'])}<br>"
                f"<small>{_esc(row['ua'])}</small></p><p>请核对网页上显示的验证码一致，再确认。</p>"
                "<button id=b>确认登录</button><p id=m></p>"
                "<script>b.onclick=async()=>{b.disabled=true;const r=await fetch('api/login/approve',{method:'POST',"
                "headers:{'Content-Type':'application/json'},body:JSON.stringify({t:new URLSearchParams(location.search).get('t')})});"
                "m.textContent=r.ok?'已确认，回到电脑上的网页即可':'确认失败或已过期';}</script>"
            )
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>确认登录</title><style>body{font:16px system-ui;margin:24px;max-width:480px}"
            "button{font-size:17px;padding:10px 20px}</style><h3>网页 Agent 登录确认</h3>" + body
        )

    @app.post("/agent/api/login/approve")
    async def approve(body: TokenBody):
        if not hub().approve_login(body.t):
            raise HTTPException(400, "登录请求已失效")
        return {"ok": True}

    @app.post("/agent/api/logout")
    async def logout(request: Request, response: Response):
        hub().logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/agent")
        return {"ok": True}

    # ------------------------------------------------ 对话
    @app.get("/agent/api/me", dependencies=[Depends(require_user)])
    async def me():
        return {"agent_online": hub().agent_online(), "folders": hub().folders, "modes": MODES}

    @app.get("/agent/api/chats", dependencies=[Depends(require_user)])
    async def list_chats():
        return {"chats": hub().chats(), "agent_online": hub().agent_online()}

    @app.post("/agent/api/chats", dependencies=[Depends(require_user_write)])
    async def new_chat(body: NewChat):
        chat = hub().create_chat(body.folder, body.mode)
        await hub().publish_list()
        return chat

    @app.get("/agent/api/chats/{chat_id}", dependencies=[Depends(require_user)])
    async def get_chat(chat_id: str):
        chat = hub()._chat(chat_id)
        turns = [r["turn"] for r in hub().store.q("SELECT turn FROM turns WHERE chat_id=? AND snapshot!=''", (chat_id,))]
        return {"chat": chat, "events": hub().store.events(chat_id), "rewindable": turns}

    @app.get("/agent/api/chats/{chat_id}/stream", dependencies=[Depends(require_user)])
    async def stream(chat_id: str, after: int = 0):
        hub()._chat(chat_id)
        q: asyncio.Queue = asyncio.Queue()
        hub().subscribers.setdefault(chat_id, set()).add(q)

        async def gen():
            try:
                for row in hub().store.events(chat_id, after):
                    yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
                while True:
                    try:
                        row = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                hub().subscribers.get(chat_id, set()).discard(q)
                hub().last_viewed[chat_id] = time.time()

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/agent/api/events", dependencies=[Depends(require_user)])
    async def list_stream():
        q: asyncio.Queue = asyncio.Queue()
        hub().list_subscribers.add(q)

        async def gen():
            try:
                yield "data: {\"type\":\"chats\"}\n\n"
                while True:
                    try:
                        row = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(row)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                hub().list_subscribers.discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/agent/api/chats/{chat_id}/messages", dependencies=[Depends(require_user_write)])
    async def send(chat_id: str, body: SendMessage):
        if not body.text.strip() and not body.files:
            raise HTTPException(400, "消息是空的")
        return await hub().user_message(chat_id, body.text, body.files)

    @app.post("/agent/api/chats/{chat_id}/stop", dependencies=[Depends(require_user_write)])
    async def stop(chat_id: str):
        hub().stop(chat_id)
        return {"ok": True}

    @app.post("/agent/api/chats/{chat_id}/permission", dependencies=[Depends(require_user_write)])
    async def permission(chat_id: str, body: PermissionDecision):
        await hub().permission(chat_id, body.request_id, body.allow, body.always)
        return {"ok": True}

    @app.post("/agent/api/chats/{chat_id}/mode", dependencies=[Depends(require_user_write)])
    async def mode(chat_id: str, body: ModeBody):
        await hub().set_mode(chat_id, body.mode)
        return {"ok": True}

    @app.post("/agent/api/chats/{chat_id}/rewind", dependencies=[Depends(require_user_write)])
    async def rewind(chat_id: str, body: RewindBody):
        await hub().rewind(chat_id, body.turn)
        return {"ok": True}

    @app.post("/agent/api/chats/{chat_id}/fetch", dependencies=[Depends(require_user_write)])
    async def fetch(chat_id: str, body: FetchBody):
        hub().fetch_file(chat_id, body.path)
        return {"ok": True}

    @app.post("/agent/api/chats/{chat_id}/archive", dependencies=[Depends(require_user_write)])
    async def archive(chat_id: str):
        hub().archive(chat_id)
        await hub().publish_list()
        return {"ok": True}

    @app.post("/agent/api/upload", dependencies=[Depends(require_user_write)])
    async def upload(file: UploadFile = File(...)):
        fid = await hub().files.save_upload(file, file.filename or "file", file.content_type or "application/octet-stream")
        f = hub().files.db.get_file(fid)
        if f["size"] > MAX_UPLOAD:
            Path(f["path"]).unlink(missing_ok=True)
            raise HTTPException(413, "文件太大（上限 50 MB）")
        return {"file_id": fid, "name": f["filename"], "content_type": f["content_type"], "size": f["size"]}

    @app.get("/agent/api/files/{file_id}", dependencies=[Depends(require_user)])
    async def get_file(file_id: str):
        f = hub().files.db.get_file(file_id)
        if not f:
            raise HTTPException(404)
        inline = (f["content_type"] or "").startswith("image/")
        return FileResponse(
            f["path"], media_type=f["content_type"], filename=f["filename"],
            content_disposition_type="inline" if inline else "attachment",
        )

    # ------------------------------------------------ 家里电脑
    @app.post("/api/agent/web/hello", dependencies=[Depends(require_agent)])
    async def agent_web_hello(body: AgentWebHello):
        hub().folders = [{"path": f["path"], "name": f.get("name") or f["path"]} for f in body.folders if f.get("path")]
        hub().agent_seen = time.time()
        # 家里电脑重启过：还在跑的对话都已经中断
        for c in ([] if not body.startup else hub().store.q("SELECT id FROM chats WHERE status IN ('running','waiting','rewinding')")):
            row = hub().store.add_event(c["id"], hub().store.chat(c["id"])["turns"], "error", {"text": "家里电脑的 Agent 重启了，这一轮已中断"})
            await hub().publish(c["id"], row)
            await hub().set_status(c["id"], "idle")
        return {"ok": True}

    @app.get("/api/agent/web/next", dependencies=[Depends(require_agent)])
    async def agent_web_next(wait: float = 25):
        return {"commands": await hub().next_commands(min(wait, 30))}

    @app.post("/api/agent/web/events", dependencies=[Depends(require_agent)])
    async def agent_web_events(body: AgentEvents):
        hub().agent_seen = time.time()
        await hub().agent_events(body.chat_id, body.events)
        return {"ok": True}


def _esc(s: str | None) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
