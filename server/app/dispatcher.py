"""核心调度：收消息 → 合并 → 理解需求 → 建任务 → 下发给家里电脑 → 回传结果 / 审批部署。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

from . import db as S
from .config import MAIN_CHANNEL
from .db import Database
from .files import FileStore
from .textfmt import plain_text
from .triage import Triage
from .wecom.client import WeComAPIError, WeComClient

log = logging.getLogger(__name__)

HELP_TEXT = """直接用大白话描述需求即可，比如「商城后台登录页的按钮改成蓝色」。可以连发多条、发截图。

常用指令：
进度 —— 查看最近的任务
进度 12 —— 查看任务 #12
取消 12 —— 取消任务 #12"""

OWNER_HELP = HELP_TEXT + """

负责人指令：
同意 12 —— 开始执行大改动 / 确认部署
拒绝 12 —— 不做 / 暂不部署
部署 项目id —— 手动部署某个项目
授权 账号 名字 —— 允许这个人在当前应用里提需求（账号见通讯录成员详情，如：授权 TangMuMao 汤姆猫）
用户 —— 查看已授权用户"""

COMMANDS = [
    ("approve", re.compile(r"^(?:同意|批准|确认|ok)\s*#?(\d+)$", re.I)),
    ("reject", re.compile(r"^(?:拒绝|驳回|不同意)\s*#?(\d+)$")),
    ("cancel", re.compile(r"^取消\s*#?(\d+)$")),
    ("status", re.compile(r"^(?:进度|状态|任务)\s*#?(\d+)?$")),
    ("deploy", re.compile(r"^部署\s+(\S+)$")),
    # 「授权 kf:xxx 名字」「授权 TangMuMao 汤姆猫」「授权给TangMuMao」都可以；不写渠道时授权到发指令的那个应用
    ("grant", re.compile(r"^授权(?:给)?[\s:：]*((?:[a-z][a-z0-9]*:)?[A-Za-z0-9][A-Za-z0-9_.@-]*)(?:\s+(\S+))?$")),
    ("users", re.compile(r"^用户$")),
    ("help", re.compile(r"^(?:帮助|help|\?|？)$", re.I)),
]


def parse_command(text: str) -> tuple[str, tuple] | None:
    text = text.strip()
    for name, pattern in COMMANDS:
        m = pattern.match(text)
        if m:
            return name, m.groups()
    return None


@dataclass
class Incoming:
    uid: str
    text: str | None = None
    file_id: str | None = None
    filename: str | None = None
    ts: float = 0.0

    def as_text(self) -> str:
        return self.text if self.text is not None else f"[图片:{self.filename or self.file_id}]"


OWNER_TAG = "#owner"  # 智能机器人渠道：「<渠道>:<userid>#owner」= 在这个人所在的会话里 @负责人


class Messenger:
    """按用户来源（哪个自建应用 / 智能机器人 / 微信客服）选择发送通道。uid 形如 "<渠道>:<id>"。"""

    def __init__(
        self,
        wecom: WeComClient,
        db: Database,
        owner_userid: str,
        apps: dict[str, WeComClient] | None = None,
        bots: dict | None = None,
        bot_notify: dict[str, str] | None = None,
        bot_names: dict[str, str] | None = None,
    ):
        self.wecom = wecom  # 主应用，也负责微信客服
        self.apps: dict[str, WeComClient] = {MAIN_CHANNEL: wecom, **(apps or {})}
        self.bots: dict = bots or {}  # 渠道名 -> AiBotConnection
        self.bot_notify = bot_notify or {}  # 渠道名 -> 负责人私信用的应用渠道
        self.bot_names = bot_names or {}
        self.db = db
        self.owner_userid = owner_userid
        self.owner_uid = f"{MAIN_CHANNEL}:{owner_userid}"

    def owner_on(self, channel: str) -> str:
        """负责人在某个应用 / 机器人里的 uid；微信客服等其他渠道回到主应用。"""
        return f"{channel}:{self.owner_userid}" if channel in self.apps or channel in self.bots else self.owner_uid

    # ---- 智能机器人：记住每个人最近在哪个会话（群 / 单聊）里说话，回复发回那里
    def remember_chat(self, uid: str, chatid: str, chat_type: int) -> None:
        self.db.kv_set(f"botchat:{uid}", json.dumps({"chatid": chatid, "chat_type": chat_type}))

    def bot_chat(self, uid: str) -> dict | None:
        raw = self.db.kv_get(f"botchat:{uid}")
        return json.loads(raw) if raw else None

    async def _send_bot(self, channel: str, ident: str, text: str) -> bool:
        for_owner = ident.endswith(OWNER_TAG)
        base_uid = f"{channel}:{ident[: -len(OWNER_TAG)] if for_owner else ident}"
        target_uid = f"{channel}:{self.owner_userid}" if for_owner else base_uid
        chat = self.bot_chat(base_uid) or (self.bot_chat(target_uid) if for_owner else None)
        ok = False
        try:
            if not chat:
                raise ValueError("这个人还没在群里或单聊中和机器人说过话")
            content = text if chat["chat_type"] == 1 else f"@{self._name(target_uid)} {text}"
            await self.bots[channel].send_markdown(chat["chatid"], chat["chat_type"], content)
            self.db.add_message(target_uid, "out", text)
            ok = True
        except Exception as e:  # noqa: BLE001
            log.exception("机器人 %s 发送给 %s 失败", channel, target_uid)
            if not for_owner and target_uid.partition(":")[2] != self.owner_userid:
                await self.send(self.owner_uid, f"⚠️ 无法给 {self._name(target_uid)} 发消息：{e}\n原文：{text[:300]}")
        if for_owner:
            # 需要负责人处理的消息：再私信一份，保证手机上有推送（群里发送失败时也靠它兜底）
            app = self.bot_notify.get(channel)
            bot = self.bot_names.get(channel, channel)
            if app:
                await self.send(f"{app}:{self.owner_userid}", f"【{bot}】{text}")
            elif not ok:
                await self.send(self.owner_uid, f"【{bot}】{text}")
        return ok

    async def send(self, uid: str, text: str) -> bool:
        text = plain_text(text)  # 企业微信文本消息不渲染 Markdown
        channel, _, ident = uid.partition(":")
        if channel in self.bots:
            return await self._send_bot(channel, ident, text)
        try:
            if channel in self.apps:
                await self.apps[channel].send_app_text(ident, text)
            elif channel == "kf":
                user = self.db.get_user(uid)
                open_kfid = ((user or {}).get("meta") or {}).get("open_kfid")
                if not open_kfid:
                    raise WeComAPIError(-1, "缺少 open_kfid，用户需要先通过客服链接发过消息")
                await self.wecom.kf_send_text(open_kfid, ident, text)
            else:
                raise ValueError(f"未知通道 {uid}")
        except Exception as e:  # noqa: BLE001
            log.exception("发送消息给 %s 失败", uid)
            if uid != self.owner_uid:
                await self.send(self.owner_uid, f"⚠️ 无法给 {self._name(uid)} 发消息：{e}\n原文：{text[:300]}")
            return False
        self.db.add_message(uid, "out", text)
        return True

    async def owner(self, text: str, channel: str = MAIN_CHANNEL) -> bool:
        return await self.send(self.owner_on(channel), text)

    def _name(self, uid: str) -> str:
        u = self.db.get_user(uid)
        return (u or {}).get("name") or uid


class Dispatcher:
    def __init__(
        self,
        db: Database,
        messenger: Messenger,
        triage: Triage,
        files: FileStore,
        merge_window: float = 15,
        offline_alert_minutes: int = 10,
        channel_projects: dict[str, list[str]] | None = None,
    ):
        self.db = db
        self.msg = messenger
        self.triage = triage
        self.files = files
        self.merge_window = merge_window
        self.offline_alert_seconds = offline_alert_minutes * 60
        self.buffers: dict[str, list[Incoming]] = {}
        self.timers: dict[str, asyncio.Task] = {}
        self.job_event = asyncio.Event()
        # 每个应用能处理的项目 id；没列出或为空 = 全部项目
        self.channel_projects = {k: v for k, v in (channel_projects or {}).items() if v}

    # ------------------------------------------------------------------ 工具
    @property
    def owner_uid(self) -> str:
        return self.msg.owner_uid

    def projects(self) -> list[dict]:
        return json.loads(self.db.kv_get("projects", "[]"))

    def projects_for(self, uid: str) -> list[dict]:
        """这个用户所在的应用能处理的项目。"""
        allowed = self.channel_projects.get(uid.partition(":")[0])
        projects = self.projects()
        return [p for p in projects if p["id"] in allowed] if allowed else projects

    def owner_for(self, uid: str) -> str:
        """和 uid 同一个应用里的负责人（通知发到对应的应用里）。
        智能机器人：发到这个人所在的会话（通常是群）并 @负责人。"""
        channel = uid.partition(":")[0]
        if channel in self.msg.bots:
            return f"{uid}{OWNER_TAG}"
        return self.msg.owner_on(channel)

    def project(self, project_id: str) -> dict | None:
        return next((p for p in self.projects() if p["id"] == project_id), None)

    def project_name(self, project_id: str) -> str:
        p = self.project(project_id)
        return (p or {}).get("name") or project_id

    def user_name(self, uid: str) -> str:
        u = self.db.get_user(uid)
        return (u or {}).get("name") or uid

    def agent_online(self) -> bool:
        last = float(self.db.kv_get("agent_last_seen", "0"))
        return time.time() - last < 120

    def is_owner(self, uid: str) -> bool:
        channel, _, ident = uid.partition(":")
        return (channel in self.msg.apps or channel in self.msg.bots) and ident == self.msg.owner_userid

    def format_task(self, t: dict, detail: bool = False) -> str:
        line = f"#{t['id']}【{t['title']}】{S.STATUS_LABELS.get(t['status'], t['status'])}"
        if t["status"] == S.RUNNING and t["progress"]:
            line += f"（{t['progress']}）"
        if detail:
            line += f"\n项目：{self.project_name(t['project_id'])}\n需求：{t['requirement'][:500]}"
            if t["summary"]:
                line += f"\n结果：{t['summary'][:800]}"
            if t["error"]:
                line += f"\n错误：{t['error'][:300]}"
            if t["package_file_id"]:
                line += f"\n安装包：{self.files.signed_url(t['package_file_id'])}"
        return line

    def enqueue_code_job(self, task: dict, kind: str = "code") -> dict:
        job = self.db.create_job(kind, task["project_id"], task_id=task["id"])
        self.db.update_task(task["id"], status=S.QUEUED)
        self.job_event.set()
        return job

    # ------------------------------------------------------------------ 收消息
    async def handle_incoming(self, item: Incoming) -> None:
        item.ts = item.ts or time.time()
        user = self.db.get_user(item.uid)
        if not user:
            await self._unknown_user(item)
            return
        self.db.add_message(item.uid, "in", item.as_text())

        if item.text is not None:
            cmd = parse_command(item.text)
            if cmd:
                await self.run_command(item.uid, *cmd)
                return
            if self.is_owner(item.uid) and item.text.strip().startswith("授权"):
                # 负责人的授权指令写法不对时提示格式，不要当成开发需求
                await self.msg.send(
                    item.uid,
                    "授权指令的格式：授权 账号 名字\n例如：授权 TangMuMao 汤姆猫\n"
                    "账号在企业微信后台「通讯录 → 点成员」里看；授权只对当前这个应用生效。",
                )
                return

        # 连续消息先缓存，停顿一会儿后一起理解（微信里一句话常被拆成好几条发）
        self.buffers.setdefault(item.uid, []).append(item)
        old = self.timers.pop(item.uid, None)
        if old:
            old.cancel()
        self.timers[item.uid] = asyncio.create_task(self._flush_later(item.uid))

    async def _flush_later(self, uid: str) -> None:
        try:
            await asyncio.sleep(self.merge_window)
        except asyncio.CancelledError:
            return
        self.timers.pop(uid, None)
        try:
            await self.flush(uid)
        except Exception:  # noqa: BLE001
            log.exception("处理 %s 的消息失败", uid)
            await self.msg.send(uid, "抱歉，处理你的消息时出错了，已通知负责人。")
            if not self.is_owner(uid):
                await self.msg.send(self.owner_for(uid), f"⚠️ 处理 {self.user_name(uid)} 的消息时出错，请查看服务器日志。")

    async def _unknown_user(self, item: Incoming) -> None:
        key = f"unknown_notified:{item.uid}"
        last = float(self.db.kv_get(key, "0"))
        if time.time() - last < 3600:
            return
        self.db.kv_set(key, str(time.time()))
        await self.msg.send(
            self.owner_for(item.uid),
            f"有未授权用户发来消息：\n{item.as_text()[:200]}\n\n如果是自己人，回复：\n授权 {item.uid} 名字"
        )

    async def flush(self, uid: str) -> None:
        items = self.buffers.pop(uid, [])
        if not items:
            return
        projects = self.projects_for(uid)
        if not projects:
            await self.msg.send(uid, "家里的电脑还没连上服务器（没有上报这个应用对应的项目），需求先记下了，稍后会处理。")
            await self.msg.send(self.owner_for(uid), "⚠️ 有人提需求，但家里电脑的 Agent 还没上报这个应用对应的项目，请检查 Agent 是否在线、config.yaml 里的项目 id。")
            self.buffers[uid] = items + self.buffers.get(uid, [])
            return

        first_ts = items[0].ts
        history = [m for m in self.db.recent_messages(uid, 30) if m["created_at"] < first_ts][-20:]
        recent = self.db.list_tasks(uid=uid, limit=5)
        texts = [i.as_text() for i in items]
        attachments = [{"file_id": i.file_id, "filename": i.filename} for i in items if i.file_id]

        r = await self.triage.analyze(texts, projects, history=history, recent_tasks=recent)
        log.info("triage %s -> %s", uid, r)

        if r.intent == "status":
            await self.reply_status(uid, r.related_task_id)
        elif r.intent == "cancel":
            target = r.related_task_id or next(
                (t["id"] for t in self.db.list_tasks(uid=uid, limit=5, statuses=S.ACTIVE_STATUSES)), None
            )
            if target:
                await self.cancel_task(uid, target)
            else:
                await self.msg.send(uid, "你目前没有进行中的任务。")
        elif r.intent == "query":
            await self.create_query_task(uid, r, texts)
        elif r.intent in ("new_task", "followup") and r.project_id or (r.intent == "followup" and r.related_task_id):
            await self.create_task_from_triage(uid, r, texts, attachments)
        else:
            reply = r.reply or "收到～如果是新需求，请说一下是哪个项目、具体要改什么。发「帮助」查看用法。"
            await self.msg.send(uid, reply)

    async def create_query_task(self, uid: str, r, texts: list[str]) -> None:
        """只读查询线上数据：不改代码、不建分支、不部署，结果直接回复。只对负责人开放（涉及用户数据）。"""
        if not self.is_owner(uid):
            await self.msg.send(uid, "查询线上数据只对负责人开放。如果需要，请直接联系负责人。")
            return
        projects = self.projects_for(uid)
        project_id = r.project_id or (projects[0]["id"] if len(projects) == 1 else None)
        project = next((p for p in projects if p["id"] == project_id), None)
        if not project:
            await self.msg.send(uid, "请问要查哪个项目的数据？")
            return
        if not project.get("has_query"):
            await self.msg.send(uid, f"「{project.get('name') or project_id}」还没有配置线上数据查询，暂时只能处理改软件的需求。")
            return
        task = self.db.create_task(
            uid=uid,
            project_id=project_id,
            title=r.title or "查询数据",
            requirement=r.requirement or "\n".join(texts),
            acceptance=[],
            raw_messages=texts,
            attachments=[],
            risk="small",
            wants_package=0,
            related_task_id=None,
            status=S.QUEUED,
        )
        job = self.enqueue_code_job(task, kind="query")
        ahead = self.db.queue_position(job["id"])
        tip = f"前面还有 {ahead} 个任务" if ahead else "马上开始"
        offline = "" if self.agent_online() else "\n（家里电脑暂时离线，上线后会自动处理）"
        await self.msg.send(
            uid,
            f"收到！#{task['id']}【{task['title']}】只读查询线上数据，不会改动系统，{tip}。\n要查的是：{task['requirement'][:300]}{offline}",
        )

    async def create_task_from_triage(self, uid: str, r, texts: list[str], attachments: list[dict]) -> None:
        related = self.db.get_task(r.related_task_id) if r.related_task_id else None
        if related and related["uid"] != uid and not self.is_owner(uid):
            related = None

        # 补充到还没开始的任务里
        if r.intent == "followup" and related and related["status"] in (S.QUEUED, S.AWAITING_CONFIRM):
            self.db.update_task(
                related["id"],
                requirement=related["requirement"] + "\n\n【补充】" + (r.requirement or "\n".join(texts)),
                acceptance=(related["acceptance"] or []) + r.acceptance,
                raw_messages=(related["raw_messages"] or []) + texts,
                attachments=(related["attachments"] or []) + attachments,
            )
            await self.msg.send(uid, f"好的，已补充到任务 #{related['id']}【{related['title']}】。")
            return

        project_id = r.project_id or (related or {}).get("project_id")
        if project_id and project_id not in {p["id"] for p in self.projects_for(uid)}:
            project_id = None
        if not project_id:
            await self.msg.send(uid, "请问是哪个项目的需求？")
            return
        task = self.db.create_task(
            uid=uid,
            project_id=project_id,
            title=r.title or (texts[0][:20] if texts else "新需求"),
            requirement=r.requirement or "\n".join(texts),
            acceptance=r.acceptance,
            raw_messages=texts,
            attachments=attachments,
            risk=r.risk,
            wants_package=int(r.wants_package),
            related_task_id=related["id"] if related else None,
            status=S.AWAITING_CONFIRM if (r.risk == "large" and not self.is_owner(uid)) else S.QUEUED,
        )
        pname = self.project_name(project_id)
        criteria = "\n".join(f"- {a}" for a in task["acceptance"]) or "（无）"

        if task["status"] == S.AWAITING_CONFIRM:
            await self.msg.send(uid, f"收到，任务 #{task['id']}【{task['title']}】改动较大，需要负责人确认后开始，确认后会通知你。")
            await self.msg.send(
                self.owner_for(uid),
                f"🟡 {self.user_name(uid)} 提了一个较大的需求，需要你确认：\n"
                f"#{task['id']}【{task['title']}】项目：{pname}\n{task['requirement'][:800]}\n验收：\n{criteria}\n\n"
                f"回复「同意 {task['id']}」开始，「拒绝 {task['id']}」不做。"
            )
            return

        job = self.enqueue_code_job(task)
        ahead = self.db.queue_position(job["id"])
        tip = f"前面还有 {ahead} 个任务" if ahead else "马上开始"
        offline = "" if self.agent_online() else "\n（家里电脑暂时离线，上线后会自动处理）"
        await self.msg.send(
            uid,
            f"收到！任务 #{task['id']}【{task['title']}】（{pname}）已加入队列，{tip}。\n我的理解：{task['requirement'][:300]}{offline}",
        )
        if not self.is_owner(uid):
            await self.msg.send(self.owner_for(uid), f"🔵 {self.user_name(uid)} 新需求 #{task['id']}【{task['title']}】（{pname}），已自动开始。")

    # ------------------------------------------------------------------ 指令
    async def run_command(self, uid: str, name: str, args: tuple) -> None:
        owner = self.is_owner(uid)
        if name == "help":
            await self.msg.send(uid, OWNER_HELP if owner else HELP_TEXT)
        elif name == "status":
            await self.reply_status(uid, int(args[0]) if args[0] else None)
        elif name == "cancel":
            await self.cancel_task(uid, int(args[0]))
        elif not owner:
            await self.msg.send(uid, "这个指令只有负责人可以用。")
        elif name == "approve":
            await self.approve(int(args[0]), uid)
        elif name == "reject":
            await self.reject(int(args[0]), uid)
        elif name == "deploy":
            await self.start_deploy(args[0], [], uid)
        elif name == "grant":
            target, uname = args[0], args[1] or ""
            if ":" not in target:
                channel = uid.partition(":")[0]
                target = f"{channel}:{target}"
            if not uname:
                # 在别的应用里授权过的同一个人，沿用那边的名字
                ident = target.partition(":")[2]
                uname = next((u["name"] for u in self.db.list_users() if u["uid"].partition(":")[2] == ident and u["name"]), "")
            pending_kfid = self.db.kv_get(f"pending_kfid:{target}")
            self.db.upsert_user(target, "friend", uname, {"open_kfid": pending_kfid} if pending_kfid else None)
            await self.msg.send(uid, f"已授权 {uname or target}，ta 现在可以提需求了。")
            await self.msg.send(target, "你好，已开通需求通道～直接描述需求就行，发「帮助」查看用法。")
        elif name == "users":
            lines = [f"{u['name'] or '-'}  {u['uid']}  ({u['role']})" for u in self.db.list_users()]
            await self.msg.send(uid, "\n".join(lines) or "暂无")

    async def reply_status(self, uid: str, task_id: int | None) -> None:
        if task_id:
            t = self.db.get_task(task_id)
            if not t or (t["uid"] != uid and not self.is_owner(uid)):
                await self.msg.send(uid, f"没有找到任务 #{task_id}。")
                return
            await self.msg.send(uid, self.format_task(t, detail=True))
            return
        if self.is_owner(uid):
            mine = {p["id"] for p in self.projects_for(uid)} if uid.partition(":")[0] in self.channel_projects else None
            tasks = [t for t in self.db.list_tasks(limit=50) if mine is None or t["project_id"] in mine][:8]
        else:
            tasks = self.db.list_tasks(uid=uid, limit=8)
        if not tasks:
            await self.msg.send(uid, "还没有任务。")
            return
        text = "\n".join(self.format_task(t) for t in tasks)
        if self.is_owner(uid):
            text += "\n\n家里电脑：" + ("在线 ✅" if self.agent_online() else "离线 ❌")
        await self.msg.send(uid, text)

    async def cancel_task(self, uid: str, task_id: int) -> None:
        t = self.db.get_task(task_id)
        if not t or (t["uid"] != uid and not self.is_owner(uid)):
            await self.msg.send(uid, f"没有找到任务 #{task_id}。")
            return
        if t["status"] in (S.QUEUED, S.AWAITING_CONFIRM):
            self.db.cancel_queued_jobs_for_task(task_id)
            self.db.update_task(task_id, status=S.CANCELED)
            await self.msg.send(uid, f"已取消任务 #{task_id}。")
        elif t["status"] == S.RUNNING:
            self.db.update_task(task_id, cancel_requested=1)
            await self.msg.send(uid, f"正在停止任务 #{task_id}…")
        else:
            await self.msg.send(uid, f"任务 #{task_id} 当前状态是「{S.STATUS_LABELS.get(t['status'])}」，无法取消。")

    async def approve(self, task_id: int, reply_to: str) -> None:
        t = self.db.get_task(task_id)
        if not t:
            await self.msg.send(reply_to, f"没有找到任务 #{task_id}。")
            return
        if t["status"] == S.AWAITING_CONFIRM:
            self.enqueue_code_job(t)
            await self.msg.send(reply_to, f"好的，#{task_id} 已开始排队处理。")
            if not self.is_owner(t["uid"]):
                await self.msg.send(t["uid"], f"任务 #{task_id}【{t['title']}】负责人已确认，开始处理。")
        elif t["status"] == S.AWAITING_DEPLOY:
            pending = self.db.tasks_by_project_status(t["project_id"], S.AWAITING_DEPLOY)
            await self.start_deploy(t["project_id"], [p["id"] for p in pending], reply_to)
        else:
            await self.msg.send(reply_to, f"#{task_id} 当前状态是「{S.STATUS_LABELS.get(t['status'])}」，不需要确认。")

    async def reject(self, task_id: int, reply_to: str) -> None:
        t = self.db.get_task(task_id)
        if not t:
            await self.msg.send(reply_to, f"没有找到任务 #{task_id}。")
            return
        if t["status"] == S.AWAITING_CONFIRM:
            self.db.update_task(task_id, status=S.REJECTED)
            await self.msg.send(reply_to, f"已拒绝 #{task_id}。")
            if not self.is_owner(t["uid"]):
                await self.msg.send(t["uid"], f"任务 #{task_id}【{t['title']}】负责人暂时不做，有疑问可以直接沟通。")
        elif t["status"] == S.AWAITING_DEPLOY:
            self.db.update_task(task_id, status=S.DONE)
            await self.msg.send(reply_to, f"#{task_id} 暂不部署（代码已合并在本地主分支，下次部署时会一起带上）。")
            if not self.is_owner(t["uid"]):
                await self.msg.send(t["uid"], f"任务 #{task_id}【{t['title']}】已完成开发，上线时间负责人另行安排。")
        else:
            await self.msg.send(reply_to, f"#{task_id} 当前状态是「{S.STATUS_LABELS.get(t['status'])}」，无法拒绝。")

    async def start_deploy(self, project_id: str, task_ids: list[int], reply_to: str) -> None:
        p = self.project(project_id)
        if not p:
            await self.msg.send(reply_to, f"没有项目「{project_id}」。可用：{', '.join(x['id'] for x in self.projects())}")
            return
        if not p.get("has_deploy"):
            await self.msg.send(reply_to, f"项目「{p.get('name') or project_id}」没有配置部署命令。")
            return
        for tid in task_ids:
            self.db.update_task(tid, status=S.DEPLOYING)
        self.db.create_job("deploy", project_id, payload={"task_ids": task_ids, "notify": reply_to})
        self.job_event.set()
        ids = "、".join(f"#{i}" for i in task_ids) or "无关联任务"
        await self.msg.send(reply_to, f"🚀 开始部署 {p.get('name') or project_id}（包含 {ids}）")

    # ------------------------------------------------------------------ Agent 接口
    async def agent_hello(self, projects: list[dict], startup: bool) -> None:
        self.db.kv_set("projects", json.dumps(projects, ensure_ascii=False))
        self.touch_agent()
        if not startup:
            return
        # Agent 重启了：之前标记为 running 的任务实际已中断
        for job in self.db.jobs_with_status("running"):
            await self.job_result(job["id"], {"ok": False, "error": "家里电脑的 Agent 重启，任务被中断，请重新提交或回复负责人重试。"})

    def touch_agent(self) -> None:
        self.db.kv_set("agent_last_seen", str(time.time()))
        self.db.kv_set("agent_offline_alerted", "0")

    async def next_job(self, wait: float) -> dict | None:
        deadline = time.monotonic() + wait
        while True:
            self.touch_agent()
            job = self.db.claim_next_job()
            if job:
                return self.build_job_payload(job)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.job_event.clear()
            try:
                await asyncio.wait_for(self.job_event.wait(), timeout=min(remaining, 10))
            except asyncio.TimeoutError:
                pass

    def build_job_payload(self, job: dict) -> dict:
        payload = {"job_id": job["id"], "kind": job["kind"], "project_id": job["project_id"]}
        if job["kind"] == "deploy":
            payload["task_ids"] = (job["payload"] or {}).get("task_ids", [])
            return payload
        t = self.db.get_task(job["task_id"])
        self.db.update_task(t["id"], status=S.RUNNING, progress="开始处理")
        related = self.db.get_task(t["related_task_id"]) if t["related_task_id"] else None
        payload.update(
            task_id=t["id"],
            title=t["title"],
            requirement=t["requirement"],
            acceptance=t["acceptance"] or [],
            raw_messages=t["raw_messages"] or [],
            attachments=t["attachments"] or [],
            requester=self.user_name(t["uid"]),
            wants_package=bool(t["wants_package"]),
            related_task=(
                {
                    "id": related["id"],
                    "title": related["title"],
                    "requirement": related["requirement"],
                    "summary": related["summary"],
                    "session_id": related["session_id"],
                }
                if related and related["project_id"] == t["project_id"]
                else None
            ),
        )
        return payload

    def job_progress(self, job_id: int, message: str) -> dict:
        self.touch_agent()
        job = self.db.get_job(job_id)
        if not job or not job["task_id"]:
            return {"cancel": False}
        t = self.db.update_task(job["task_id"], progress=message[:100])
        return {"cancel": bool(t and t["cancel_requested"])}

    async def job_result(self, job_id: int, result: dict) -> None:
        job = self.db.get_job(job_id)
        if not job or job["status"] not in ("running", "queued"):
            return
        ok = bool(result.get("ok"))
        self.db.finish_job(job_id, "done" if ok else "failed", result)
        if job["kind"] == "deploy":
            await self._deploy_result(job, result)
        elif job["kind"] == "query":
            await self._query_result(job, result)
        else:
            await self._code_result(job, result)

    async def _query_result(self, job: dict, result: dict) -> None:
        t = self.db.get_task(job["task_id"])
        uid, tid, title = t["uid"], t["id"], t["title"]
        fields = {"session_id": result.get("session_id", ""), "summary": (result.get("summary") or "")[:4000], "progress": ""}
        if result.get("canceled"):
            self.db.update_task(tid, status=S.CANCELED, **fields)
            await self.msg.send(uid, f"查询 #{tid} 已停止。")
            return
        if not result.get("ok"):
            self.db.update_task(tid, status=S.FAILED, error=(result.get("error") or "")[:4000], **fields)
            await self.msg.send(uid, f"🔴 查询 #{tid}【{title}】没有完成：\n{(result.get('error') or '')[:1200]}")
            return
        self.db.update_task(tid, status=S.DONE, **fields)
        await self.msg.send(uid, f"🔍 #{tid}【{title}】\n{fields['summary'] or '没有查到结果。'}")

    async def _code_result(self, job: dict, result: dict) -> None:
        t = self.db.get_task(job["task_id"])
        uid, tid, title = t["uid"], t["id"], t["title"]
        from_friend = not self.is_owner(uid)
        owner_uid = self.owner_for(uid)
        fields = {
            "branch": result.get("branch", ""),
            "session_id": result.get("session_id", ""),
            "summary": (result.get("summary") or "")[:4000],
            "package_file_id": result.get("package_file_id") or "",
            "progress": "",
        }

        if result.get("canceled"):
            self.db.update_task(tid, status=S.CANCELED, **fields)
            await self.msg.send(uid, f"任务 #{tid} 已停止。")
            return

        if not result.get("ok"):
            self.db.update_task(tid, status=S.FAILED, error=(result.get("error") or "")[:4000], **fields)
            if from_friend:
                await self.msg.send(uid, f"任务 #{tid}【{title}】这次没能自动完成，已通知负责人处理，请稍等。")
            await self.msg.send(owner_uid, f"🔴 任务 #{tid}【{title}】失败：\n{(result.get('error') or '')[:1200]}")
            return

        p = self.project(t["project_id"]) or {}
        changed = result.get("changed", True)
        needs_deploy = changed and p.get("has_deploy")
        t = self.db.update_task(tid, status=S.AWAITING_DEPLOY if needs_deploy else S.DONE, **fields)

        summary = fields["summary"] or "已完成。"
        pkg = f"\n\n安装包下载：{self.files.signed_url(t['package_file_id'])}" if t["package_file_id"] else ""
        if needs_deploy:
            user_text = f"✅ 任务 #{tid}【{title}】开发完成：\n{summary}{pkg}\n\n上线需要负责人确认，确认后会通知你。"
        else:
            user_text = f"✅ 任务 #{tid}【{title}】完成：\n{summary}{pkg}"
        await self.msg.send(uid, user_text)

        stats = result.get("diff_stat") or ""
        tests = result.get("test_output_tail")
        owner_text = f"🟢 #{tid}【{title}】完成（{p.get('name') or t['project_id']}，分支 {fields['branch'] or '-'}）"
        if stats:
            owner_text += f"\n{stats[-600:]}"
        if tests:
            owner_text += "\n测试：通过"
        for note in result.get("notes") or []:
            owner_text += f"\n注意：{note}"
        if result.get("package_error"):
            owner_text += f"\n⚠️ 打包失败：{result['package_error'][-500:]}"
        if result.get("duration_s"):
            owner_text += f"\n耗时 {max(1, int(result['duration_s']) // 60)} 分钟"
        if needs_deploy:
            owner_text += f"\n\n回复「同意 {tid}」部署上线，「拒绝 {tid}」暂不部署。"
        if from_friend or needs_deploy or result.get("notes") or result.get("package_error"):
            await self.msg.send(owner_uid, owner_text)

    async def _deploy_result(self, job: dict, result: dict) -> None:
        task_ids = (job["payload"] or {}).get("task_ids", [])
        first = self.db.get_task(task_ids[0]) if task_ids else None
        notify = (job["payload"] or {}).get("notify") or (self.owner_for(first["uid"]) if first else self.owner_uid)
        p = self.project(job["project_id"]) or {}
        pname = p.get("name") or job["project_id"]
        if result.get("ok"):
            url = p.get("url") or ""
            for tid in task_ids:
                t = self.db.update_task(tid, status=S.DEPLOYED)
                if t and not self.is_owner(t["uid"]):
                    await self.msg.send(t["uid"], f"🎉 任务 #{tid}【{t['title']}】已上线！{('访问：' + url) if url else ''}")
            await self.msg.send(notify, f"✅ {pname} 部署成功。{url}")
        else:
            for tid in task_ids:
                self.db.update_task(tid, status=S.AWAITING_DEPLOY)
            retry = f"修复后回复「同意 {task_ids[0]}」重试。" if task_ids else f"修复后回复「部署 {job['project_id']}」重试。"
            await self.msg.send(notify, f"🔴 {pname} 部署失败：\n{(result.get('error') or '')[-1500:]}\n\n{retry}")

    # ------------------------------------------------------------------ 后台巡检
    async def monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.check_agent_offline()
            except Exception:  # noqa: BLE001
                log.exception("巡检失败")

    async def check_agent_offline(self) -> None:
        last = float(self.db.kv_get("agent_last_seen", "0"))
        if not last or time.time() - last < self.offline_alert_seconds:
            return
        if self.db.kv_get("agent_offline_alerted") == "1":
            return
        if not self.db.jobs_with_status("queued"):
            return
        self.db.kv_set("agent_offline_alerted", "1")
        minutes = int((time.time() - last) / 60)
        await self.msg.owner(f"⚠️ 家里电脑已经 {minutes} 分钟没连服务器了，有任务在排队。请检查电脑是否休眠/断网。")
