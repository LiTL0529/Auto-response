"""与云端服务器通信。全部由家里电脑主动发起（HTTPS 长轮询），不需要公网 IP 或端口映射。"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class ServerAPI:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(75, connect=15),
            transport=transport,
        )

    def _post(self, path: str, **kw) -> dict:
        r = self.http.post(path, **kw)
        r.raise_for_status()
        return r.json()

    def hello(self, projects: list[dict], startup: bool) -> None:
        self._post("/api/agent/hello", json={"projects": projects, "startup": startup})

    def next_job(self, wait: int = 25) -> dict | None:
        r = self.http.get("/api/agent/jobs/next", params={"wait": wait})
        r.raise_for_status()
        return r.json().get("job")

    def progress(self, job_id: int, message: str) -> dict:
        try:
            return self._post(f"/api/agent/jobs/{job_id}/progress", json={"message": message})
        except Exception as e:  # noqa: BLE001  进度上报失败不影响任务
            log.warning("上报进度失败：%s", e)
            return {}

    def result(self, job_id: int, result: dict, attempts: int = 40) -> bool:
        """结果必须送达：网络抖动时重试约 20 分钟。"""
        delay = 2.0
        for i in range(attempts):
            try:
                self._post(f"/api/agent/jobs/{job_id}/result", json=result)
                return True
            except Exception as e:  # noqa: BLE001
                log.warning("上报结果失败（第 %d 次）：%s", i + 1, e)
                time.sleep(delay)
                delay = min(delay * 2, 60)
        log.error("任务 %s 的结果最终没有送达：%s", job_id, result)
        return False

    # 上传时 2 分钟没有任何进展就判定卡住（之前是 30 分钟，网络一卡整个 Agent 就被占住）
    UPLOAD_TIMEOUT = httpx.Timeout(connect=15, read=120, write=120, pool=15)
    UPLOAD_ATTEMPTS = 3

    def upload(self, path: str | Path, attempts: int | None = None) -> str:
        path = Path(path)
        attempts = attempts or self.UPLOAD_ATTEMPTS
        for n in range(1, attempts + 1):
            try:
                with open(path, "rb") as f:
                    data = self._post(
                        "/api/agent/files",
                        files={"file": (path.name, f, "application/octet-stream")},
                        data={"filename": path.name},
                        timeout=self.UPLOAD_TIMEOUT,
                    )
                return data["file_id"]
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if n == attempts:
                    raise
                log.warning("上传 %s 失败（第 %d 次）：%s，%d 秒后重试", path.name, n, e, 5 * n)
                time.sleep(5 * n)
        raise RuntimeError("unreachable")

    # ------------------------------------------------ 网页 Agent
    def web_hello(self, folders: list[dict], startup: bool, defaults: dict | None = None) -> None:
        self._post("/api/agent/web/hello", json={"folders": folders, "startup": startup, "defaults": defaults or {}})

    def web_reply(self, reply_id: str, data: dict) -> None:
        self._post("/api/agent/web/reply", json={"reply_id": reply_id, "data": data})

    def web_next(self, wait: int = 25) -> list[dict]:
        r = self.http.get("/api/agent/web/next", params={"wait": wait})
        r.raise_for_status()
        return r.json().get("commands") or []

    def web_events(self, chat_id: str, events: list[dict], attempts: int = 8) -> None:
        """事件要按顺序送达：失败就重试（最多约 2 分钟），不跳过。"""
        delay = 1.0
        for n in range(1, attempts + 1):
            try:
                self._post("/api/agent/web/events", json={"chat_id": chat_id, "events": events})
                return
            except Exception as e:  # noqa: BLE001
                if n == attempts:
                    log.error("网页对话 %s 的事件没有送达：%s", chat_id, e)
                    return
                time.sleep(delay)
                delay = min(delay * 2, 30)

    def download(self, file_id: str, dest: Path, cancel=None, progress=None) -> Path:
        """cancel：threading.Event，置位后中止下载；progress(已下载字节, 总字节)：下载过程中定期回调。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.http.stream("GET", f"/api/agent/files/{file_id}") as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("下载已取消")
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        return dest
