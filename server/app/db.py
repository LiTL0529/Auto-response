"""SQLite 存储。规模很小（几个人、几个项目），一个文件 + 一把锁足够。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uid TEXT PRIMARY KEY,          -- "app:<userid>" 或 "kf:<external_userid>"
    role TEXT NOT NULL,            -- owner / friend
    name TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    direction TEXT NOT NULL,       -- in / out
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_uid ON messages(uid, id);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    requirement TEXT NOT NULL,
    acceptance TEXT NOT NULL DEFAULT '[]',
    raw_messages TEXT NOT NULL DEFAULT '[]',
    attachments TEXT NOT NULL DEFAULT '[]',
    risk TEXT NOT NULL DEFAULT 'small',
    wants_package INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    related_task_id INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    progress TEXT NOT NULL DEFAULT '',
    package_file_id TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- code / deploy
    task_id INTEGER,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued / running / done / failed / canceled
    payload TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (msgid TEXT PRIMARY KEY, created_at REAL NOT NULL);
"""

JSON_FIELDS = {"acceptance", "raw_messages", "attachments", "meta", "payload", "result"}

# 任务状态
AWAITING_CONFIRM = "awaiting_confirm"  # 大改动，等你确认后才开始
QUEUED = "queued"
RUNNING = "running"
AWAITING_DEPLOY = "awaiting_deploy"  # 已完成并合并，等你确认部署
DEPLOYING = "deploying"
DEPLOYED = "deployed"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"
REJECTED = "rejected"

ACTIVE_STATUSES = (AWAITING_CONFIRM, QUEUED, RUNNING, AWAITING_DEPLOY, DEPLOYING)

STATUS_LABELS = {
    AWAITING_CONFIRM: "等待确认",
    QUEUED: "排队中",
    RUNNING: "处理中",
    AWAITING_DEPLOY: "已完成，等待确认部署",
    DEPLOYING: "部署中",
    DEPLOYED: "已上线",
    DONE: "已完成",
    FAILED: "失败",
    CANCELED: "已取消",
    REJECTED: "已拒绝",
}


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS & d.keys():
        d[k] = json.loads(d[k]) if d[k] else None
    return d


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        with self.lock:
            return _row(self.conn.execute(sql, args).fetchone())

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [_row(r) for r in self.conn.execute(sql, args).fetchall()]

    # ---- kv ----
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---- 去重 ----
    def mark_seen(self, msgid: str) -> bool:
        """第一次见到返回 True。企业微信超时会重推，需要去重。"""
        try:
            self._exec("INSERT INTO seen(msgid, created_at) VALUES(?, ?)", (msgid, time.time()))
            return True
        except sqlite3.IntegrityError:
            return False

    # ---- 用户 ----
    def get_user(self, uid: str) -> dict | None:
        return self._one("SELECT * FROM users WHERE uid=?", (uid,))

    def upsert_user(self, uid: str, role: str, name: str = "", meta: dict | None = None) -> None:
        old = self.get_user(uid)
        merged_meta = {**((old or {}).get("meta") or {}), **(meta or {})}
        self._exec(
            "INSERT INTO users(uid, role, name, meta, created_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET role=excluded.role, name=excluded.name, meta=excluded.meta",
            (uid, role, name or (old or {}).get("name", ""), json.dumps(merged_meta, ensure_ascii=False), time.time()),
        )

    def update_user_meta(self, uid: str, meta: dict) -> None:
        user = self.get_user(uid)
        if user:
            merged = {**(user["meta"] or {}), **meta}
            self._exec("UPDATE users SET meta=? WHERE uid=?", (json.dumps(merged, ensure_ascii=False), uid))

    def list_users(self) -> list[dict]:
        return self._all("SELECT * FROM users ORDER BY created_at")

    # ---- 消息记录 ----
    def add_message(self, uid: str, direction: str, content: str) -> None:
        self._exec(
            "INSERT INTO messages(uid, direction, content, created_at) VALUES(?, ?, ?, ?)",
            (uid, direction, content, time.time()),
        )

    def recent_messages(self, uid: str, limit: int = 20) -> list[dict]:
        rows = self._all("SELECT * FROM messages WHERE uid=? ORDER BY id DESC LIMIT ?", (uid, limit))
        return list(reversed(rows))

    # ---- 任务 ----
    def create_task(self, **fields: Any) -> dict:
        now = time.time()
        fields.setdefault("status", QUEUED)
        fields["created_at"] = fields["updated_at"] = now
        for k in JSON_FIELDS & fields.keys():
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self._exec(f"INSERT INTO tasks({cols}) VALUES({marks})", tuple(fields.values()))
        return self.get_task(cur.lastrowid)

    def get_task(self, task_id: int) -> dict | None:
        return self._one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def update_task(self, task_id: int, **fields: Any) -> dict | None:
        fields["updated_at"] = time.time()
        for k in JSON_FIELDS & fields.keys():
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
        return self.get_task(task_id)

    def list_tasks(self, uid: str | None = None, limit: int = 10, statuses: tuple[str, ...] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if uid:
            sql += " AND uid=?"
            args.append(uid)
        if statuses:
            sql += f" AND status IN ({','.join('?' for _ in statuses)})"
            args.extend(statuses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self._all(sql, tuple(args))

    def tasks_by_project_status(self, project_id: str, status: str) -> list[dict]:
        return self._all("SELECT * FROM tasks WHERE project_id=? AND status=? ORDER BY id", (project_id, status))

    # ---- 任务队列（发给家里电脑的 job）----
    def create_job(self, kind: str, project_id: str, task_id: int | None = None, payload: dict | None = None) -> dict:
        cur = self._exec(
            "INSERT INTO jobs(kind, task_id, project_id, status, payload, created_at) VALUES(?, ?, ?, 'queued', ?, ?)",
            (kind, task_id, project_id, json.dumps(payload or {}, ensure_ascii=False), time.time()),
        )
        return self.get_job(cur.lastrowid)

    def get_job(self, job_id: int) -> dict | None:
        return self._one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def claim_next_job(self) -> dict | None:
        with self.lock:
            job = self._one("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
            if not job:
                return None
            self._exec("UPDATE jobs SET status='running', started_at=? WHERE id=?", (time.time(), job["id"]))
            return self.get_job(job["id"])

    def finish_job(self, job_id: int, status: str, result: dict) -> None:
        self._exec(
            "UPDATE jobs SET status=?, result=?, finished_at=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False), time.time(), job_id),
        )

    def cancel_queued_jobs_for_task(self, task_id: int) -> int:
        cur = self._exec("UPDATE jobs SET status='canceled' WHERE task_id=? AND status='queued'", (task_id,))
        return cur.rowcount

    def jobs_with_status(self, status: str) -> list[dict]:
        return self._all("SELECT * FROM jobs WHERE status=? ORDER BY id", (status,))

    def queue_position(self, job_id: int) -> int:
        r = self._one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running') AND id<?", (job_id,))
        return r["n"] if r else 0

    # ---- 文件 ----
    def add_file(self, file_id: str, path: str, filename: str, content_type: str, size: int) -> None:
        self._exec(
            "INSERT INTO files(id, path, filename, content_type, size, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (file_id, path, filename, content_type, size, time.time()),
        )

    def get_file(self, file_id: str) -> dict | None:
        return self._one("SELECT * FROM files WHERE id=?", (file_id,))
