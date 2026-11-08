from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

MAIN_CHANNEL = "app"  # 主应用（WECOM_AGENT_ID 那个）的渠道名，用户 uid 形如 "app:<userid>"
RESERVED_CHANNELS = {MAIN_CHANNEL, "kf"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


@dataclass
class ExtraApp:
    """额外的企业微信自建应用。每个应用有自己的回调地址 /wecom/callback/<key>，只处理自己的项目。"""

    key: str  # 渠道名（小写字母/数字），用户 uid 形如 "<key>:<userid>"
    name: str
    agent_id: str
    secret: str
    callback_token: str
    encoding_aes_key: str
    projects: list[str] = field(default_factory=list)  # 该应用能处理的项目 id；空 = 全部

    def missing(self) -> list[str]:
        p = f"WECOM_{self.key.upper()}_"
        required = {
            p + "AGENT_ID": self.agent_id,
            p + "APP_SECRET": self.secret,
            p + "CALLBACK_TOKEN": self.callback_token,
            p + "ENCODING_AES_KEY": self.encoding_aes_key,
        }
        return [k for k, v in required.items() if not v]


@dataclass
class ExtraBot:
    """企业微信「智能机器人」（长连接模式）：拉进群里，被 @ 时干活，结果发回群里。"""

    key: str  # 渠道名，用户 uid 形如 "<key>:<userid>"
    name: str
    bot_id: str
    secret: str
    projects: list[str] = field(default_factory=list)  # 只处理这些项目；空 = 全部
    notify_app: str = ""  # 需要负责人处理的消息，同时私信到这个应用（如 dx），保证手机有推送

    def missing(self) -> list[str]:
        p = f"WECOM_{self.key.upper()}_"
        return [k for k, v in {p + "BOT_ID": self.bot_id, p + "SECRET": self.secret}.items() if not v]


@dataclass
class Settings:
    # 企业微信
    corp_id: str
    app_agent_id: str
    app_secret: str
    callback_token: str
    encoding_aes_key: str
    owner_userid: str  # 你自己在企业微信里的 userid，审批和通知都发给他

    # 阿里云百炼
    dashscope_api_key: str
    dashscope_base_url: str
    qwen_model: str

    # 家里电脑上的 Agent
    agent_token: str

    # 其他
    public_base_url: str  # 例如 https://bot.example.com，用于生成下载链接
    data_dir: str
    merge_window_seconds: float  # 连续消息合并等待时间
    file_link_ttl_hours: int
    agent_offline_alert_minutes: int

    # 主应用只处理这些项目（空 = 全部）
    app_projects: list[str] = field(default_factory=list)
    # 额外的自建应用（WECOM_APPS=dx,lab，每个再配 WECOM_<KEY>_* 一组变量）
    extra_apps: list[ExtraApp] = field(default_factory=list)
    # 智能机器人（WECOM_BOTS=dxbot，每个再配 WECOM_<KEY>_BOT_ID / _SECRET / _PROJECTS / _NAME / _NOTIFY_APP）
    bots: list[ExtraBot] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        extra = []
        for key in _list(_env("WECOM_APPS").lower()):
            p = f"WECOM_{key.upper()}_"
            extra.append(
                ExtraApp(
                    key=key,
                    name=_env(p + "NAME", key),
                    agent_id=_env(p + "AGENT_ID"),
                    secret=_env(p + "APP_SECRET"),
                    callback_token=_env(p + "CALLBACK_TOKEN"),
                    encoding_aes_key=_env(p + "ENCODING_AES_KEY"),
                    projects=_list(_env(p + "PROJECTS")),
                )
            )
        bots = []
        for key in _list(_env("WECOM_BOTS").lower()):
            p = f"WECOM_{key.upper()}_"
            bots.append(
                ExtraBot(
                    key=key,
                    name=_env(p + "NAME", key),
                    bot_id=_env(p + "BOT_ID"),
                    secret=_env(p + "SECRET"),
                    projects=_list(_env(p + "PROJECTS")),
                    notify_app=_env(p + "NOTIFY_APP").lower(),
                )
            )
        return cls(
            corp_id=_env("WECOM_CORP_ID"),
            app_agent_id=_env("WECOM_AGENT_ID"),
            app_secret=_env("WECOM_APP_SECRET"),
            callback_token=_env("WECOM_CALLBACK_TOKEN"),
            encoding_aes_key=_env("WECOM_ENCODING_AES_KEY"),
            owner_userid=_env("OWNER_USERID"),
            dashscope_api_key=_env("DASHSCOPE_API_KEY"),
            dashscope_base_url=_env("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            qwen_model=_env("QWEN_MODEL", "qwen-plus"),
            agent_token=_env("AGENT_TOKEN"),
            public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
            data_dir=_env("DATA_DIR", "./data"),
            merge_window_seconds=float(_env("MERGE_WINDOW_SECONDS", "15")),
            file_link_ttl_hours=int(_env("FILE_LINK_TTL_HOURS", "168")),
            agent_offline_alert_minutes=int(_env("AGENT_OFFLINE_ALERT_MINUTES", "10")),
            app_projects=_list(_env("WECOM_PROJECTS")),
            extra_apps=extra,
            bots=bots,
        )

    def missing(self) -> list[str]:
        required = {
            "WECOM_CORP_ID": self.corp_id,
            "WECOM_AGENT_ID": self.app_agent_id,
            "WECOM_APP_SECRET": self.app_secret,
            "WECOM_CALLBACK_TOKEN": self.callback_token,
            "WECOM_ENCODING_AES_KEY": self.encoding_aes_key,
            "OWNER_USERID": self.owner_userid,
            "AGENT_TOKEN": self.agent_token,
            "PUBLIC_BASE_URL": self.public_base_url,
        }
        out = [k for k, v in required.items() if not v]
        for a in self.extra_apps:
            if not re.fullmatch(r"[a-z][a-z0-9]{0,15}", a.key) or a.key in RESERVED_CHANNELS:
                out.append(f"WECOM_APPS 里的「{a.key}」（只能用小写字母和数字，且不能是 app / kf）")
            out += a.missing()
        app_keys = {MAIN_CHANNEL} | {a.key for a in self.extra_apps}
        for b in self.bots:
            if not re.fullmatch(r"[a-z][a-z0-9]{0,15}", b.key) or b.key in RESERVED_CHANNELS or b.key in app_keys:
                out.append(f"WECOM_BOTS 里的「{b.key}」（只能用小写字母和数字，且不能和应用渠道重名）")
            if b.notify_app and b.notify_app not in app_keys:
                out.append(f"WECOM_{b.key.upper()}_NOTIFY_APP（「{b.notify_app}」不是已配置的应用）")
            out += b.missing()
        return out
