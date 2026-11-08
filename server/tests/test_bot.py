"""智能机器人（群里 @机器人）：需求进对应项目，回复发回群里，需要负责人处理的消息 @负责人 并私信一份。"""

import base64
import os

import pytest
from fastapi.testclient import TestClient

from app.config import ExtraApp, ExtraBot, Settings
from app.main import create_app
from app.wecom.aibot import decrypt_media
from tests.test_flow import AES_KEY, AGENT_TOKEN, AUTH, CORP_ID, OWNER, FakeWeCom, wait_for

DX_AES = base64.b64encode(os.urandom(32)).decode().rstrip("=")
GROUP = "wrGROUP1"


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[str, int, str]] = []
        self.connected = True

    def start(self):
        pass

    async def stop(self):
        pass

    async def send_markdown(self, chatid, chat_type, content):
        self.sent.append((chatid, chat_type, content))

    def in_group(self):
        return [c for chat, t, c in self.sent if chat == GROUP and t == 2]


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        corp_id=CORP_ID,
        app_agent_id="1000002",
        app_secret="secret",
        callback_token="cbtoken",
        encoding_aes_key=AES_KEY,
        owner_userid=OWNER,
        dashscope_api_key="",
        dashscope_base_url="",
        qwen_model="qwen-plus",
        agent_token=AGENT_TOKEN,
        public_base_url="https://bot.example.com",
        data_dir=str(tmp_path),
        merge_window_seconds=0.1,
        file_link_ttl_hours=1,
        agent_offline_alert_minutes=10,
        app_projects=["shop"],
        extra_apps=[ExtraApp("dx", "智能党校", "1000003", "dxsecret", "dxtoken", DX_AES, ["party"])],
        bots=[ExtraBot("dxbot", "党校助手", "BOTID", "SECRET", ["party"], "dx")],
    )
    main, dx, bot = FakeWeCom(), FakeWeCom(), FakeBot()
    app = create_app(settings, wecom=main, extra_wecom={"dx": dx}, extra_bots={"dxbot": bot})
    with TestClient(app) as client:
        projects = [
            {"id": "shop", "name": "商城后台", "aliases": [], "description": "", "has_deploy": False},
            {"id": "party", "name": "党校平台", "aliases": ["党校"], "description": "", "has_deploy": True, "url": "https://x/dx/"},
        ]
        assert client.post("/api/agent/hello", json={"projects": projects, "startup": True}, headers=AUTH).status_code == 200
        yield client, bot, dx


def say(client, user, text, msgid, group=True):
    body = {
        "msgid": msgid,
        "aibotid": "BOTID",
        "from": {"userid": user},
        "msgtype": "text",
        "text": {"content": text},
    }
    if group:
        body.update(chattype="group", chatid=GROUP)
    else:
        body.update(chattype="single")
    core = client.app.state.core
    client.portal.call(core.handle_bot_message, "dxbot", body)


def test_group_flow_friend_task_owner_review(env):
    client, bot, dx = env
    core = client.app.state.core
    core.db.upsert_user("dxbot:tom", "friend", "汤姆猫")

    say(client, "tom", "@党校助手 首页横幅换成红色", "b1")
    assert wait_for(lambda: any("已加入队列" in m and m.startswith("@汤姆猫") for m in bot.in_group()))
    assert any("新需求" in m and m.startswith("@负责人") for m in bot.in_group())
    # 负责人同时收到私信（保证有推送）
    assert any("【党校助手】" in m for m in dx.to(f"app:{OWNER}"))

    job = client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"]
    assert job["project_id"] == "party" and "横幅" in job["requirement"] and "@党校助手" not in job["requirement"]
    client.post(
        f"/api/agent/jobs/{job['job_id']}/result",
        json={"ok": True, "changed": True, "summary": "横幅已改红", "branch": "claude/task-1"},
        headers=AUTH,
    )
    assert any("同意 1" in m and m.startswith("@负责人") for m in bot.in_group())

    # 朋友在群里发「同意」不行
    say(client, "tom", "@党校助手 同意 1", "b2")
    assert wait_for(lambda: any("只有负责人" in m for m in bot.in_group()))
    # 负责人在群里同意，部署结果回到群里
    say(client, OWNER, "@党校助手 同意 1", "b3")
    assert wait_for(lambda: any("开始部署" in m for m in bot.in_group()))
    dep = client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"]
    client.post(f"/api/agent/jobs/{dep['job_id']}/result", json={"ok": True}, headers=AUTH)
    assert any("已上线" in m and m.startswith("@汤姆猫") for m in bot.in_group())
    assert any("部署成功" in m for m in bot.in_group())


def test_unknown_member_asks_owner_in_group_then_grant(env):
    client, bot, dx = env
    say(client, "jerry", "@党校助手 帮我加个功能", "u1")
    assert wait_for(lambda: any("未授权" in m and m.startswith("@负责人") for m in bot.in_group()))
    say(client, OWNER, "@党校助手 授权 jerry 杰瑞", "u2")
    assert wait_for(lambda: any("已授权 杰瑞" in m for m in bot.in_group()))
    assert any(m.startswith("@杰瑞") and "已开通" in m for m in bot.in_group())


def test_duplicate_and_bare_mention(env):
    client, bot, _ = env
    say(client, OWNER, "@党校助手", "d1")
    say(client, OWNER, "@党校助手", "d1")  # 重复推送去重
    assert wait_for(lambda: any("负责人指令" in m for m in bot.in_group()))
    assert sum("负责人指令" in m for m in bot.in_group()) == 1


def test_single_chat_replies_privately(env):
    client, bot, _ = env
    say(client, OWNER, "帮助", "s1", group=False)
    assert wait_for(lambda: any(chat == OWNER and t == 1 for chat, t, _ in bot.sent))


def test_decrypt_media_roundtrip():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = os.urandom(32)
    aeskey = base64.b64encode(key).decode().rstrip("=")
    plain = b"\x89PNG fake image bytes"
    pad = 32 - len(plain) % 32
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    data = enc.update(plain + bytes([pad]) * pad) + enc.finalize()
    assert decrypt_media(data, aeskey) == plain


def test_connection_protocol_against_local_server():
    """真实的 AiBotConnection 对一个本地假服务器：订阅、收消息、主动发送（成功 / 报错）、被踢下线。"""
    import asyncio
    import json

    import websockets

    from app.wecom.aibot import AiBotConnection, AiBotError

    async def scenario():
        frames, received = [], []
        kick = asyncio.Event()

        async def server(ws):
            async for raw in ws:
                f = json.loads(raw)
                frames.append(f)
                rid = f["headers"]["req_id"]
                if f["cmd"] == "aibot_subscribe":
                    ok = f["body"] == {"bot_id": "BOTID", "secret": "SECRET"}
                    await ws.send(json.dumps({"headers": {"req_id": rid}, "errcode": 0 if ok else 40001, "errmsg": "ok"}))
                    await ws.send(json.dumps({"cmd": "aibot_msg_callback", "headers": {"req_id": "cb1"},
                                              "body": {"msgid": "m1", "msgtype": "text", "text": {"content": "@党校助手 hi"}}}))
                elif f["cmd"] == "aibot_send_msg":
                    bad = f["body"]["chatid"] == "BAD"
                    await ws.send(json.dumps({"headers": {"req_id": rid}, "errcode": 93000 if bad else 0, "errmsg": "nope" if bad else "ok"}))
                    if f["body"]["chatid"] == "KICK":
                        await ws.send(json.dumps({"cmd": "aibot_event_callback", "headers": {"req_id": "e1"},
                                                  "body": {"msgtype": "event", "event": {"eventtype": "disconnected_event"}}}))
                        kick.set()

        async def on_message(key, body):
            received.append((key, body["text"]["content"]))

        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            conn = AiBotConnection("dxbot", "BOTID", "SECRET", on_message, url=f"ws://127.0.0.1:{port}")
            conn.start()
            for _ in range(100):
                if conn.connected and received:
                    break
                await asyncio.sleep(0.05)
            assert conn.connected and received == [("dxbot", "@党校助手 hi")]
            await conn.send_markdown("wrGROUP", 2, "**完成**")
            sent = [f for f in frames if f["cmd"] == "aibot_send_msg"][0]["body"]
            assert sent == {"chatid": "wrGROUP", "chat_type": 2, "msgtype": "markdown", "markdown": {"content": "**完成**"}}
            with pytest.raises(AiBotError, match="93000"):
                await conn.send_markdown("BAD", 2, "x")
            await conn.send_markdown("KICK", 2, "x")
            await asyncio.wait_for(kick.wait(), 3)
            for _ in range(60):
                if not conn.connected:
                    break
                await asyncio.sleep(0.05)
            assert not conn.connected  # 被踢后断开，并等 60 秒才重连
            await conn.stop()

    asyncio.run(scenario())
