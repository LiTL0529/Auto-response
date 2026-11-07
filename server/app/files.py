"""文件存储：朋友发来的截图、家里电脑打出来的安装包，统一存在服务器磁盘上，用签名链接下载。

如果安装包很大或者想走 CDN，可以把这里换成阿里云 OSS。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path

from .db import Database


def safe_filename(name: str) -> str:
    name = os.path.basename(name or "file")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(". ")
    return name[:120] or "file"


class FileStore:
    def __init__(self, root: str, db: Database, secret: str, public_base_url: str, ttl_hours: int):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db
        self.key = hashlib.sha256(b"file-link:" + secret.encode()).digest()
        self.public_base_url = public_base_url
        self.ttl = ttl_hours * 3600

    def _new_path(self, file_id: str) -> Path:
        d = self.root / file_id[:2]
        d.mkdir(parents=True, exist_ok=True)
        return d / file_id

    def save_bytes(self, data: bytes, filename: str, content_type: str) -> str:
        file_id = secrets.token_hex(12)
        path = self._new_path(file_id)
        path.write_bytes(data)
        self.db.add_file(file_id, str(path), safe_filename(filename), content_type, len(data))
        return file_id

    async def save_upload(self, upload, filename: str, content_type: str) -> str:
        """流式保存上传文件（安装包可能有几百 MB）。"""
        file_id = secrets.token_hex(12)
        path = self._new_path(file_id)
        size = 0
        with open(path, "wb") as f:
            while chunk := await upload.read(1024 * 1024):
                f.write(chunk)
                size += len(chunk)
        self.db.add_file(file_id, str(path), safe_filename(filename), content_type, size)
        return file_id

    def _sig(self, file_id: str, exp: int) -> str:
        return hmac.new(self.key, f"{file_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]

    def signed_url(self, file_id: str) -> str:
        f = self.db.get_file(file_id)
        name = f["filename"] if f else "file"
        exp = int(time.time()) + self.ttl
        return f"{self.public_base_url}/files/{file_id}/{name}?exp={exp}&sig={self._sig(file_id, exp)}"

    def check_sig(self, file_id: str, exp: int, sig: str) -> bool:
        return exp >= time.time() and hmac.compare_digest(self._sig(file_id, exp), sig or "")
