"""企业微信「智能机器人」长连接客户端（群里 @机器人 提需求）。

协议：https://developer.work.weixin.qq.com/document/path/101463
- 连接 wss://openws.work.weixin.qq.com，发 aibot_subscribe（bot_id + secret）完成订阅
- 每 30 秒 ping 一次；同一机器人新连接会踢掉旧连接（旧连接收到 disconnected_event）
- 收消息：aibot_msg_callback（群里只有 @机器人 的文字 / 图文混排会推过来）
- 发消息：aibot_send_msg 主动推送到会话（chatid；需要该会话里有人先给机器人发过消息），
  每个会话 30 条/分钟、1000 条/小时
- 所有请求的应答：{"headers": {"req_id"}, "errcode", "errmsg"}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger(__name__)

WS_URL = "wss://openws.work.weixin.qq.com"
PING_SECONDS = 30
REQUEST_TIMEOUT = 15
MAX_MARKDOWN_BYTES = 20000  # 文档上限 20480 字节，留点余量

MessageHandler = Callable[[str, dict], Awaitable[None]]  # (机器人渠道名, 消息 body)


class AiBotError(Exception):
    pass


def _key_bytes(aeskey: str) -> bytes:
    """aeskey 可能是 base64（和 EncodingAESKey 一样 43 位不带 =）或 32 字节原文。"""
    if len(aeskey) == 32:
        return aeskey.encode()
    padded = aeskey + "=" * (-len(aeskey) % 4)
    key = base64.b64decode(padded)
    if len(key) != 32:
        raise AiBotError(f"aeskey 长度不对：{len(key)}")
    return key


def decrypt_media(data: bytes, aeskey: str) -> bytes:
    """AES-256-CBC，IV = key 前 16 字节，PKCS#7 填充（块大小按 32 字节）。"""
    key = _key_bytes(aeskey)
    dec = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = dec.update(data) + dec.finalize()
    pad = plain[-1]
    if 1 <= pad <= 32 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    return plain


def _truncate(content: str) -> str:
    raw = content.encode()
    if len(raw) <= MAX_MARKDOWN_BYTES:
        return content
    return raw[: MAX_MARKDOWN_BYTES - 30].decode(errors="ignore") + "\n…（内容过长已截断）"


class AiBotConnection:
    """一个智能机器人的长连接：自动订阅、心跳、断线重连；提供主动发消息。"""

    def __init__(self, key: str, bot_id: str, secret: str, on_message: MessageHandler, url: str = WS_URL):
        self.key = key
        self.bot_id = bot_id
        self.secret = secret
        self.on_message = on_message
        self.url = url
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ready = asyncio.Event()
        self._stopped = False
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"aibot-{self.key}")

    async def stop(self) -> None:
        self._stopped = True
        if self._ws is not None:
            await self._ws.close()
        if self._task:
            self._task.cancel()

    @property
    def connected(self) -> bool:
        return self._ready.is_set()

    async def _run(self) -> None:
        import websockets  # uvicorn[standard] 自带

        delay = 1
        while not self._stopped:
            kicked = False
            try:
                async with websockets.connect(self.url, ping_interval=None, max_size=8 * 1024 * 1024) as ws:
                    self._ws = ws
                    reader = asyncio.create_task(self._reader(ws))
                    try:
                        await self._request("aibot_subscribe", {"bot_id": self.bot_id, "secret": self.secret}, ws=ws)
                    except Exception:
                        reader.cancel()
                        raise
                    self._ready.set()
                    log.info("智能机器人 %s 已连接", self.key)
                    delay = 1
                    pinger = asyncio.create_task(self._pinger())
                    try:
                        kicked = await reader
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("智能机器人 %s 连接断开：%s", self.key, e)
            finally:
                self._ready.clear()
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(AiBotError("连接已断开"))
                self._pending.clear()
            if self._stopped:
                break
            # 被新连接踢下线时等久一点，避免两个进程互相踢
            wait = 60 if kicked else delay
            delay = min(delay * 2, 30)
            await asyncio.sleep(wait)

    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(PING_SECONDS)
            try:
                await self._request("ping", None)
            except Exception as e:  # noqa: BLE001
                log.warning("智能机器人 %s 心跳失败：%s", self.key, e)
                if self._ws is not None:
                    await self._ws.close()
                return

    async def _reader(self, ws) -> bool:
        """读到连接关闭为止；返回是否被新连接踢下线。"""
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            cmd = frame.get("cmd")
            req_id = (frame.get("headers") or {}).get("req_id", "")
            if not cmd and req_id in self._pending:
                fut = self._pending.pop(req_id)
                if not fut.done():
                    fut.set_result(frame)
                continue
            body = frame.get("body") or {}
            if cmd == "aibot_msg_callback":
                asyncio.create_task(self._dispatch(body))
            elif cmd == "aibot_event_callback":
                if (body.get("event") or {}).get("eventtype") == "disconnected_event":
                    log.warning("智能机器人 %s 被新的连接踢下线", self.key)
                    return True
        return False

    async def _dispatch(self, body: dict) -> None:
        try:
            await self.on_message(self.key, body)
        except Exception:  # noqa: BLE001
            log.exception("处理智能机器人 %s 的消息失败", self.key)

    # ------------------------------------------------------------------ 请求
    async def _request(self, cmd: str, body: dict | None, ws=None) -> dict:
        ws = ws or self._ws
        if ws is None:
            raise AiBotError(f"智能机器人 {self.key} 未连接")
        req_id = uuid.uuid4().hex
        frame: dict = {"cmd": cmd, "headers": {"req_id": req_id}}
        if body is not None:
            frame["body"] = body
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await ws.send(json.dumps(frame, ensure_ascii=False))
        try:
            resp = await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        finally:
            self._pending.pop(req_id, None)
        if resp.get("errcode", 0) != 0:
            raise AiBotError(f"{cmd} 失败：{resp.get('errcode')} {resp.get('errmsg')}")
        return resp

    async def send_markdown(self, chatid: str, chat_type: int, content: str) -> None:
        """主动推送到会话。chat_type：1 单聊（chatid=userid）/ 2 群聊。"""
        if not self.connected:
            try:
                await asyncio.wait_for(self._ready.wait(), 20)
            except asyncio.TimeoutError as e:
                raise AiBotError(f"智能机器人 {self.key} 未连接") from e
        await self._request(
            "aibot_send_msg",
            {"chatid": chatid, "chat_type": chat_type, "msgtype": "markdown", "markdown": {"content": _truncate(content)}},
        )

    @staticmethod
    async def download(http: httpx.AsyncClient, url: str, aeskey: str | None) -> bytes:
        r = await http.get(url)
        r.raise_for_status()
        return decrypt_media(r.content, aeskey) if aeskey else r.content
