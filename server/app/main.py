"""FastAPI 入口。

对外接口：
- GET/POST /wecom/callback      企业微信回调（自建应用消息 + 微信客服事件）
- /api/agent/*                  家里电脑 Agent 调用（Bearer AGENT_TOKEN）
- GET /files/{id}/{name}        签名下载链接（安装包、截图）
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from .config import Settings
from .db import Database
from .dispatcher import Dispatcher, Incoming, Messenger
from .files import FileStore
from .triage import Triage
from .wecom.client import WeComClient
from .wecom.crypto import WeComCrypto, WeComCryptoError, parse_xml

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bridge")

KF_MAX_AGE_SECONDS = 600  # 首次同步微信客服时会拉到 3 天内的旧消息，忽略 10 分钟前的


class Hello(BaseModel):
    projects: list[dict]
    startup: bool = False


class Progress(BaseModel):
    message: str


class App:
    """把各组件装配在一起，方便测试时替换。"""

    def __init__(self, settings: Settings, http: httpx.AsyncClient, wecom: WeComClient | None = None):
        os.makedirs(settings.data_dir, exist_ok=True)
        self.settings = settings
        self.http = http
        self.db = Database(os.path.join(settings.data_dir, "bridge.db"))
        self.crypto = WeComCrypto(settings.callback_token, settings.encoding_aes_key, settings.corp_id)
        self.wecom = wecom or WeComClient(settings.corp_id, settings.app_secret, settings.app_agent_id, http)
        self.files = FileStore(
            os.path.join(settings.data_dir, "files"), self.db, settings.agent_token, settings.public_base_url, settings.file_link_ttl_hours
        )
        owner_uid = f"app:{settings.owner_userid}"
        self.db.upsert_user(owner_uid, "owner", (self.db.get_user(owner_uid) or {}).get("name") or "负责人")
        self.messenger = Messenger(self.wecom, self.db, owner_uid)
        self.triage = Triage(settings.dashscope_api_key, settings.dashscope_base_url, settings.qwen_model, http)
        self.dispatcher = Dispatcher(
            self.db, self.messenger, self.triage, self.files, settings.merge_window_seconds, settings.agent_offline_alert_minutes
        )

    # ---- 回调处理 ----
    async def handle_callback_xml(self, xml_text: str) -> None:
        msg = parse_xml(xml_text)
        mtype = msg.get("MsgType")
        if mtype == "event" and msg.get("Event") == "kf_msg_or_event":
            await self.sync_kf(msg.get("OpenKfId", ""), msg.get("Token"))
            return
        if mtype == "event":
            return
        msgid = msg.get("MsgId") or f"{msg.get('FromUserName')}:{msg.get('CreateTime')}"
        if not self.db.mark_seen(f"app:{msgid}"):
            return
        uid = f"app:{msg.get('FromUserName')}"
        if mtype == "text":
            await self.dispatcher.handle_incoming(Incoming(uid=uid, text=msg.get("Content", "")))
        elif mtype == "image":
            file_id, name = await self._save_media(msg.get("MediaId"), msg.get("PicUrl"))
            await self.dispatcher.handle_incoming(Incoming(uid=uid, file_id=file_id, filename=name))
        else:
            await self.messenger.send(uid, "暂时只支持文字和图片消息～")

    async def sync_kf(self, open_kfid: str, token: str | None) -> None:
        cursor_key = f"kf_cursor:{open_kfid}"
        cursor = self.db.kv_get(cursor_key)
        while True:
            data = await self.wecom.kf_sync_msg(open_kfid, token, cursor)
            for m in data.get("msg_list", []):
                await self._handle_kf_msg(open_kfid, m)
            cursor = data.get("next_cursor") or cursor
            if cursor:
                self.db.kv_set(cursor_key, cursor)
            if not data.get("has_more"):
                break

    async def _handle_kf_msg(self, open_kfid: str, m: dict) -> None:
        if m.get("origin") != 3:  # 3 = 微信客户发送的消息
            return
        if not self.db.mark_seen(f"kf:{m.get('msgid')}"):
            return
        if time.time() - m.get("send_time", time.time()) > KF_MAX_AGE_SECONDS:
            return
        uid = f"kf:{m['external_userid']}"
        if self.db.get_user(uid):
            self.db.update_user_meta(uid, {"open_kfid": open_kfid})
        else:
            # 先记住 open_kfid，授权后才能回复
            self.db.kv_set(f"pending_kfid:{uid}", open_kfid)
        mtype = m.get("msgtype")
        if mtype == "text":
            await self.dispatcher.handle_incoming(Incoming(uid=uid, text=m["text"]["content"]))
        elif mtype == "image":
            file_id, name = await self._save_media(m["image"]["media_id"], None)
            await self.dispatcher.handle_incoming(Incoming(uid=uid, file_id=file_id, filename=name))

    async def _save_media(self, media_id: str | None, pic_url: str | None) -> tuple[str, str]:
        if media_id:
            data, name, ctype = await self.wecom.download_media(media_id)
        else:
            data, ctype = await self.wecom.download_url(pic_url)
            name = "image.jpg"
        return self.files.save_bytes(data, name, ctype), name


def create_app(settings: Settings | None = None, wecom: WeComClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    missing = settings.missing()
    if missing:
        raise RuntimeError(f"缺少环境变量：{', '.join(missing)}（参考 deploy/.env.example）")

    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(timeout=30)
        core = App(settings, http, wecom)
        state["core"] = core
        app.state.core = core
        monitor = asyncio.create_task(core.dispatcher.monitor_loop())
        log.info("服务启动，项目数：%d", len(core.dispatcher.projects()))
        yield
        monitor.cancel()
        await http.aclose()

    app = FastAPI(title="Auto-response bridge", lifespan=lifespan)

    def core() -> App:
        return state["core"]

    def require_agent(authorization: str = Header("")) -> None:
        expected = f"Bearer {settings.agent_token}"
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, "invalid agent token")

    # ------------------------------------------------ 企业微信回调
    @app.get("/wecom/callback", response_class=PlainTextResponse)
    async def wecom_verify(msg_signature: str, timestamp: str, nonce: str, echostr: str):
        try:
            return core().crypto.verify_url(msg_signature, timestamp, nonce, echostr)
        except WeComCryptoError as e:
            raise HTTPException(403, str(e)) from e

    @app.post("/wecom/callback", response_class=PlainTextResponse)
    async def wecom_callback(request: Request, background: BackgroundTasks, msg_signature: str, timestamp: str, nonce: str):
        body = await request.body()
        try:
            xml_text = core().crypto.decrypt_callback(msg_signature, timestamp, nonce, body)
        except WeComCryptoError as e:
            log.warning("回调校验失败：%s", e)
            raise HTTPException(403, str(e)) from e

        async def process():
            try:
                await core().handle_callback_xml(xml_text)
            except Exception:  # noqa: BLE001
                log.exception("处理回调失败")

        # 企业微信要求 5 秒内响应，实际处理放到后台
        background.add_task(process)
        return "success"

    # ------------------------------------------------ Agent 接口
    @app.post("/api/agent/hello", dependencies=[Depends(require_agent)])
    async def agent_hello(body: Hello):
        await core().dispatcher.agent_hello(body.projects, body.startup)
        return {"ok": True}

    @app.get("/api/agent/jobs/next", dependencies=[Depends(require_agent)])
    async def agent_next(wait: float = Query(25, le=55)):
        job = await core().dispatcher.next_job(wait)
        return {"job": job}

    @app.post("/api/agent/jobs/{job_id}/progress", dependencies=[Depends(require_agent)])
    async def agent_progress(job_id: int, body: Progress):
        return core().dispatcher.job_progress(job_id, body.message)

    @app.post("/api/agent/jobs/{job_id}/result", dependencies=[Depends(require_agent)])
    async def agent_result(job_id: int, request: Request):
        await core().dispatcher.job_result(job_id, await request.json())
        return {"ok": True}

    @app.post("/api/agent/files", dependencies=[Depends(require_agent)])
    async def agent_upload(file: UploadFile = File(...), filename: str = Form("")):
        file_id = await core().files.save_upload(file, filename or file.filename or "file", file.content_type or "application/octet-stream")
        return {"file_id": file_id}

    @app.get("/api/agent/files/{file_id}", dependencies=[Depends(require_agent)])
    async def agent_download(file_id: str):
        f = core().db.get_file(file_id)
        if not f:
            raise HTTPException(404)
        return FileResponse(f["path"], media_type=f["content_type"], filename=f["filename"])

    # ------------------------------------------------ 公开下载
    @app.get("/files/{file_id}/{name}")
    async def public_download(file_id: str, name: str, exp: int, sig: str):
        if not core().files.check_sig(file_id, exp, sig):
            raise HTTPException(403, "链接无效或已过期")
        f = core().db.get_file(file_id)
        if not f:
            raise HTTPException(404)
        return FileResponse(f["path"], media_type=f["content_type"], filename=f["filename"])

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "agent_online": core().dispatcher.agent_online()}

    return app
