import base64
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.wecom.crypto import WeComCrypto, WeComCryptoError

AES_KEY = base64.b64encode(os.urandom(32)).decode().rstrip("=")
CORP_ID = "ww_test_corp"
OWNER = "zhangsan"
AGENT_TOKEN = "agent-secret"
AUTH = {"Authorization": f"Bearer {AGENT_TOKEN}"}


class FakeWeCom:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_app_text(self, userid, content):
        self.sent.append((f"app:{userid}", content))

    async def kf_send_text(self, open_kfid, external_userid, content):
        self.sent.append((f"kf:{external_userid}", content))

    async def kf_sync_msg(self, open_kfid, token, cursor):
        return {
            "msg_list": [
                {
                    "msgid": "kf1",
                    "origin": 3,
                    "external_userid": "wm_friend",
                    "send_time": int(time.time()),
                    "msgtype": "text",
                    "text": {"content": "官网首页的标题改成「欢迎光临」"},
                }
            ],
            "next_cursor": "c1",
            "has_more": False,
        }

    async def download_media(self, media_id):
        return b"\x89PNG fake", "shot.png", "image/png"

    def to(self, uid):
        return [c for u, c in self.sent if u == uid]


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        corp_id=CORP_ID,
        app_agent_id="1000002",
        app_secret="secret",
        callback_token="cbtoken",
        encoding_aes_key=AES_KEY,
        owner_userid=OWNER,
        dashscope_api_key="",  # 不调用百炼，走兜底规则
        dashscope_base_url="",
        qwen_model="qwen-plus",
        agent_token=AGENT_TOKEN,
        public_base_url="https://bot.example.com",
        data_dir=str(tmp_path),
        merge_window_seconds=0.1,
        file_link_ttl_hours=1,
        agent_offline_alert_minutes=10,
    )
    fake = FakeWeCom()
    crypto = WeComCrypto("cbtoken", AES_KEY, CORP_ID)
    with TestClient(create_app(settings, wecom=fake)) as client:
        yield client, fake, crypto


def push(client, crypto, xml):
    body, params = crypto.build_callback(xml)
    r = client.post("/wecom/callback", params=params, content=body)
    assert r.status_code == 200, r.text


def app_text(user, content, msgid):
    return (
        f"<xml><ToUserName>{CORP_ID}</ToUserName><FromUserName>{user}</FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime><MsgType>text</MsgType>"
        f"<Content><![CDATA[{content}]]></Content><MsgId>{msgid}</MsgId><AgentID>1000002</AgentID></xml>"
    )


def hello(client, deploy=True):
    projects = [
        {"id": "shop", "name": "商城后台", "aliases": ["后台"], "description": "", "has_deploy": deploy, "url": "https://shop.example.com"},
        {"id": "site", "name": "官网", "aliases": [], "description": "", "has_deploy": False},
    ]
    r = client.post("/api/agent/hello", json={"projects": projects, "startup": True}, headers=AUTH)
    assert r.status_code == 200


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_crypto_roundtrip_and_tamper():
    c = WeComCrypto("tok", AES_KEY, CORP_ID)
    body, params = c.build_callback("<xml><a>你好</a></xml>")
    assert c.decrypt_callback(params["msg_signature"], params["timestamp"], params["nonce"], body) == "<xml><a>你好</a></xml>"
    with pytest.raises(WeComCryptoError):
        c.decrypt_callback("bad", params["timestamp"], params["nonce"], body)
    other = WeComCrypto("tok", AES_KEY, "another_corp")
    with pytest.raises(WeComCryptoError):
        other.decrypt(c.encrypt("x"))


def test_url_verification(env):
    client, _, crypto = env
    echo = crypto.encrypt("echo-123")
    ts, nonce = "1700000000", "n"
    sig = crypto.signature(ts, nonce, echo)
    r = client.get("/wecom/callback", params={"msg_signature": sig, "timestamp": ts, "nonce": nonce, "echostr": echo})
    assert r.text == "echo-123"


def test_agent_auth_required(env):
    client, _, _ = env
    assert client.get("/api/agent/jobs/next?wait=0").status_code == 401


def test_full_flow_owner_task_then_deploy(env):
    client, fake, crypto = env
    hello(client)
    owner = f"app:{OWNER}"

    # 连发两条消息，应合并为一个任务
    push(client, crypto, app_text(OWNER, "后台登录按钮改成蓝色", "m1"))
    push(client, crypto, app_text(OWNER, "字体也加粗", "m2"))
    push(client, crypto, app_text(OWNER, "字体也加粗", "m2"))  # 重推去重
    assert wait_for(lambda: any("已加入队列" in m for m in fake.to(owner)))

    job = client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"]
    assert job["kind"] == "code" and job["project_id"] == "shop"
    assert "蓝色" in job["requirement"] and "加粗" in job["requirement"]
    assert client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"] is None

    r = client.post(f"/api/agent/jobs/{job['job_id']}/progress", json={"message": "编辑 Login.vue"}, headers=AUTH)
    assert r.json() == {"cancel": False}

    client.post(
        f"/api/agent/jobs/{job['job_id']}/result",
        json={"ok": True, "changed": True, "summary": "按钮已改为蓝色加粗", "branch": "claude/task-1", "diff_stat": "1 file changed"},
        headers=AUTH,
    )
    assert any("同意 1" in m for m in fake.to(owner))

    push(client, crypto, app_text(OWNER, "同意 1", "m3"))
    assert wait_for(lambda: any("开始部署" in m for m in fake.to(owner)))
    dep = client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"]
    assert dep["kind"] == "deploy" and dep["task_ids"] == [1]
    client.post(f"/api/agent/jobs/{dep['job_id']}/result", json={"ok": True}, headers=AUTH)
    assert any("部署成功" in m for m in fake.to(owner))

    push(client, crypto, app_text(OWNER, "进度 1", "m4"))
    assert wait_for(lambda: any("已上线" in m for m in fake.to(owner)))


def test_kf_friend_unknown_then_granted(env):
    client, fake, crypto = env
    hello(client)
    owner = f"app:{OWNER}"
    friend = "kf:wm_friend"
    kf_event = (
        f"<xml><ToUserName>{CORP_ID}</ToUserName><CreateTime>1</CreateTime><MsgType>event</MsgType>"
        "<Event>kf_msg_or_event</Event><Token>tk</Token><OpenKfId>wk_1</OpenKfId></xml>"
    )
    push(client, crypto, kf_event)
    assert wait_for(lambda: any("授权 kf:wm_friend" in m for m in fake.to(owner)))
    assert not fake.to(friend)

    push(client, crypto, app_text(OWNER, "授权 kf:wm_friend 小李", "g1"))
    assert wait_for(lambda: any("已开通" in m for m in fake.to(friend)))

    # 授权后再来一条（新的 msgid）
    async def sync2(open_kfid, token, cursor):
        return {
            "msg_list": [{"msgid": "kf2", "origin": 3, "external_userid": "wm_friend", "send_time": int(time.time()),
                          "msgtype": "text", "text": {"content": "官网首页的标题改成「欢迎光临」"}}],
            "has_more": False,
        }

    fake.kf_sync_msg = sync2
    push(client, crypto, kf_event)
    assert wait_for(lambda: any("已加入队列" in m for m in fake.to(friend)))
    assert any("小李 新需求" in m for m in fake.to(owner))

    job = client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"]
    assert job["project_id"] == "site" and job["requester"] == "小李"
    client.post(f"/api/agent/jobs/{job['job_id']}/result", json={"ok": True, "summary": "标题已修改"}, headers=AUTH)
    # 官网没配部署 → 直接完成
    assert any("完成" in m and "标题已修改" in m for m in fake.to(friend))


def test_cancel_running_and_failure(env):
    client, fake, crypto = env
    hello(client)
    owner = f"app:{OWNER}"
    push(client, crypto, app_text(OWNER, "后台加一个导出按钮", "x1"))
    assert wait_for(lambda: any("已加入队列" in m for m in fake.to(owner)))
    job = client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"]
    push(client, crypto, app_text(OWNER, "取消 1", "x2"))
    assert wait_for(lambda: any("正在停止" in m for m in fake.to(owner)))
    r = client.post(f"/api/agent/jobs/{job['job_id']}/progress", json={"message": "..."}, headers=AUTH)
    assert r.json()["cancel"] is True
    client.post(f"/api/agent/jobs/{job['job_id']}/result", json={"ok": False, "canceled": True}, headers=AUTH)
    assert any("已停止" in m for m in fake.to(owner))


def test_agent_restart_fails_running_jobs(env):
    client, fake, crypto = env
    hello(client)
    owner = f"app:{OWNER}"
    push(client, crypto, app_text(OWNER, "后台改个文案", "r1"))
    assert wait_for(lambda: any("已加入队列" in m for m in fake.to(owner)))
    client.get("/api/agent/jobs/next?wait=0", headers=AUTH)
    hello(client)  # startup=True
    assert any("失败" in m and "中断" in m for m in fake.to(owner))


def test_file_upload_and_signed_download(env):
    client, _, _ = env
    r = client.post("/api/agent/files", files={"file": ("setup.exe", b"MZ...", "application/octet-stream")}, headers=AUTH)
    file_id = r.json()["file_id"]
    # 无签名不可下载
    assert client.get(f"/files/{file_id}/setup.exe?exp=9999999999&sig=bad").status_code == 403
    url = client.app.state.core.files.signed_url(file_id)
    path = url.replace("https://bot.example.com", "")
    d = client.get(path)
    assert d.status_code == 200 and d.content == b"MZ..."
