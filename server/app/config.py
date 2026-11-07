from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


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

    @classmethod
    def from_env(cls) -> "Settings":
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
        return [k for k, v in required.items() if not v]
