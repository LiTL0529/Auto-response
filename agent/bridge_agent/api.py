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

    def upload(self, path: str | Path) -> str:
        path = Path(path)
        with open(path, "rb") as f:
            data = self._post(
                "/api/agent/files",
                files={"file": (path.name, f, "application/octet-stream")},
                data={"filename": path.name},
                timeout=httpx.Timeout(1800, connect=15),
            )
        return data["file_id"]

    def download(self, file_id: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.http.stream("GET", f"/api/agent/files/{file_id}") as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return dest
