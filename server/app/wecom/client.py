"""企业微信服务端 API：access_token、应用消息、微信客服收发、素材下载。"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

BASE = "https://qyapi.weixin.qq.com/cgi-bin"
TOKEN_EXPIRED_CODES = {40014, 42001, 40001}
TEXT_LIMIT_BYTES = 2000  # 企业微信文本上限 2048 字节，留一点余量


class WeComAPIError(Exception):
    def __init__(self, errcode: int, errmsg: str):
        super().__init__(f"企业微信 API 错误 {errcode}: {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


def split_utf8(text: str, limit: int = TEXT_LIMIT_BYTES) -> list[str]:
    """按 UTF-8 字节数切分长文本（企业微信限制的是字节数）。"""
    chunks, cur, size = [], [], 0
    for ch in text:
        n = len(ch.encode())
        if size + n > limit:
            chunks.append("".join(cur))
            cur, size = [], 0
        cur.append(ch)
        size += n
    if cur:
        chunks.append("".join(cur))
    return chunks or [""]


def truncate_utf8(text: str, limit: int = TEXT_LIMIT_BYTES) -> str:
    chunks = split_utf8(text, limit - 12)
    return chunks[0] if len(chunks) == 1 else chunks[0] + "…(已截断)"


class WeComClient:
    def __init__(self, corp_id: str, secret: str, agent_id: str, http: httpx.AsyncClient):
        self.corp_id = corp_id
        self.secret = secret
        self.agent_id = agent_id
        self.http = http
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = asyncio.Lock()

    async def access_token(self, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and time.time() < self._token_exp:
                return self._token
            r = await self.http.get(f"{BASE}/gettoken", params={"corpid": self.corp_id, "corpsecret": self.secret})
            data = r.json()
            if data.get("errcode", 0) != 0:
                raise WeComAPIError(data["errcode"], data.get("errmsg", ""))
            self._token = data["access_token"]
            self._token_exp = time.time() + data.get("expires_in", 7200) - 300
            return self._token

    async def _post(self, path: str, payload: dict) -> dict:
        for attempt in range(2):
            token = await self.access_token(force=attempt > 0)
            r = await self.http.post(f"{BASE}{path}", params={"access_token": token}, json=payload)
            data = r.json()
            code = data.get("errcode", 0)
            if code in TOKEN_EXPIRED_CODES and attempt == 0:
                continue
            if code != 0:
                raise WeComAPIError(code, data.get("errmsg", ""))
            return data
        raise WeComAPIError(-1, "unreachable")

    # ---- 自建应用消息 ----
    async def send_app_text(self, userid: str, content: str) -> None:
        for chunk in split_utf8(content):
            await self._post(
                "/message/send",
                {"touser": userid, "msgtype": "text", "agentid": int(self.agent_id), "text": {"content": chunk}},
            )

    # ---- 微信客服 ----
    async def kf_sync_msg(self, open_kfid: str, token: str | None, cursor: str | None) -> dict:
        payload: dict = {"open_kfid": open_kfid, "limit": 1000}
        if token:
            payload["token"] = token
        if cursor:
            payload["cursor"] = cursor
        return await self._post("/kf/sync_msg", payload)

    async def kf_send_text(self, open_kfid: str, external_userid: str, content: str) -> None:
        # 微信客服：用户发消息后 48 小时内最多回复 5 条，所以这里截断而不是拆分
        await self._post(
            "/kf/send_msg",
            {
                "touser": external_userid,
                "open_kfid": open_kfid,
                "msgtype": "text",
                "text": {"content": truncate_utf8(content)},
            },
        )

    # ---- 素材 ----
    async def download_media(self, media_id: str) -> tuple[bytes, str, str]:
        """返回 (内容, 文件名, content_type)。"""
        token = await self.access_token()
        r = await self.http.get(f"{BASE}/media/get", params={"access_token": token, "media_id": media_id})
        ctype = r.headers.get("content-type", "application/octet-stream")
        if "json" in ctype or "text/plain" in ctype:
            data = r.json()
            raise WeComAPIError(data.get("errcode", -1), data.get("errmsg", "下载素材失败"))
        filename = "image.jpg"
        m = re.search(r'filename="?([^";]+)"?', r.headers.get("content-disposition", ""))
        if m:
            filename = m.group(1)
        return r.content, filename, ctype.split(";")[0]

    async def download_url(self, url: str) -> tuple[bytes, str]:
        r = await self.http.get(url)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/jpeg").split(";")[0]
