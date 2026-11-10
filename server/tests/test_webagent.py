import base64
import os
import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_flow import FakeWeCom

AES_KEY = base64.b64encode(os.urandom(32)).decode().rstrip("=")
OWNER = "zhangsan"
AUTH = {"Authorization": "Bearer agent-secret"}
W = {"X-Requested-With": "webagent"}
FOLDER = "D:/projects/shop"


@pytest.fixture
def web(tmp_path):
    settings = Settings(
        corp_id="ww_test", app_agent_id="1000002", app_secret="secret", callback_token="cbtoken",
        encoding_aes_key=AES_KEY, owner_userid=OWNER, dashscope_api_key="", dashscope_base_url="",
        qwen_model="qwen-plus", agent_token="agent-secret", public_base_url="https://bot.example.com",
        data_dir=str(tmp_path), merge_window_seconds=0.1, file_link_ttl_hours=1, agent_offline_alert_minutes=10,
    )
    fake = FakeWeCom()
    with TestClient(create_app(settings, wecom=fake), base_url="https://testserver") as client:
        client.post("/api/agent/web/hello", json={"folders": [{"path": FOLDER, "name": "商城"}], "startup": True}, headers=AUTH)
        yield client, fake


def login(client, fake):
    r = client.post("/agent/api/login/request")
    assert r.status_code == 200
    d = r.json()
    msg = fake.to(f"app:{OWNER}")[-1]
    assert d["code"] in msg
    token = re.search(r"/agent/approve\?t=(\S+)", msg).group(1)
    assert client.get("/agent/api/login/status", params={"id": d["id"]}).json()["status"] == "pending"
    page = client.get("/agent/approve", params={"t": token})
    assert d["code"] in page.text and "确认登录" in page.text
    assert client.post("/agent/api/login/approve", json={"t": token}).status_code == 200
    assert client.get("/agent/api/login/status", params={"id": d["id"]}).json()["status"] == "ok"
    return token


def test_login_via_wecom_and_access_control(web):
    client, fake = web
    assert client.get("/agent/api/me").status_code == 401
    assert client.get("/agent/").status_code == 200  # 页面本身可以打开（显示登录界面）
    token = login(client, fake)
    me = client.get("/agent/api/me").json()
    assert me["agent_online"] and me["folders"][0]["path"] == FOLDER
    # 确认链接只能用一次
    assert client.post("/agent/api/login/approve", json={"t": token}).status_code == 400
    # 写操作必须带自定义头（防 CSRF）
    assert client.post("/agent/api/chats", json={"folder": FOLDER}).status_code == 403
    client.post("/agent/api/logout")
    assert client.get("/agent/api/me").status_code == 401


def test_login_rate_limited(web):
    client, _ = web
    codes = [client.post("/agent/api/login/request").status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


def test_chat_turn_permission_and_rewind(web):
    client, fake = web
    login(client, fake)
    assert client.post("/agent/api/chats", json={"folder": "C:/Windows"}, headers=W).status_code == 400
    chat = client.post("/agent/api/chats", json={"folder": FOLDER, "mode": "acceptEdits"}, headers=W).json()
    cid = chat["id"]

    up = client.post("/agent/api/upload", files={"file": ("shot.png", b"\x89PNG", "image/png")}, headers=W).json()
    client.post(f"/agent/api/chats/{cid}/messages", json={"text": "把按钮改成蓝色", "files": [up["file_id"]]}, headers=W)
    cmd = client.get("/api/agent/web/next", params={"wait": 0}, headers=AUTH).json()["commands"][0]
    assert cmd["op"] == "turn" and cmd["turn"] == 1 and cmd["folder"] == FOLDER and cmd["resume"] == ""
    assert cmd["files"][0]["name"] == "shot.png"
    # 上一条没跑完不能再发
    assert client.post(f"/agent/api/chats/{cid}/messages", json={"text": "x"}, headers=W).status_code == 409

    def events(*evs):
        r = client.post("/api/agent/web/events", json={"chat_id": cid, "events": list(evs)}, headers=AUTH)
        assert r.status_code == 200

    events({"type": "turn_started", "turn": 1, "session_before": "", "snapshot": "snap1"},
           {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "npm test"}},
           {"type": "permission", "request_id": "p1", "tool": "Bash", "input": {"command": "npm test"}})
    assert client.get(f"/agent/api/chats/{cid}").json()["chat"]["status"] == "waiting"
    assert any("需要你批准" in m for m in fake.to(f"app:{OWNER}"))  # 页面没人看时提醒到企业微信

    client.post(f"/agent/api/chats/{cid}/permission", json={"request_id": "p1", "allow": True, "always": True}, headers=W)
    cmd = client.get("/api/agent/web/next", params={"wait": 0}, headers=AUTH).json()["commands"][0]
    assert cmd == {"op": "permission", "chat_id": cid, "request_id": "p1", "allow": True, "always": True}

    events({"type": "tool_result", "tool_use_id": "t1", "content": "ok"}, {"type": "text", "text": "改好了"},
           {"type": "session", "session_id": "s1"}, {"type": "result", "ok": True}, {"type": "status", "status": "idle"})
    d = client.get(f"/agent/api/chats/{cid}").json()
    assert d["chat"]["session_id"] == "s1" and d["chat"]["status"] == "idle" and d["rewindable"] == [1]
    assert d["chat"]["title"] == "把按钮改成蓝色"
    assert [e["type"] for e in d["events"]][:2] == ["user", "tool_use"]

    # 第二轮从上一轮的会话继续
    client.post(f"/agent/api/chats/{cid}/messages", json={"text": "再加粗"}, headers=W)
    cmd = client.get("/api/agent/web/next", params={"wait": 0}, headers=AUTH).json()["commands"][0]
    assert cmd["resume"] == "s1" and cmd["turn"] == 2
    events({"type": "turn_started", "turn": 2, "session_before": "s1", "snapshot": "snap2"},
           {"type": "session", "session_id": "s2"}, {"type": "status", "status": "idle"})

    # 回退到第 2 条之前：文件恢复到 snap2，对话回到 s1
    client.post(f"/agent/api/chats/{cid}/rewind", json={"turn": 2}, headers=W)
    cmd = client.get("/api/agent/web/next", params={"wait": 0}, headers=AUTH).json()["commands"][0]
    assert cmd["op"] == "rewind" and cmd["snapshot"] == "snap2"
    events({"type": "rewound", "turn": 2, "ok": True, "changed": ["a.txt"]})
    d = client.get(f"/agent/api/chats/{cid}").json()
    assert d["chat"]["session_id"] == "s1" and d["chat"]["turns"] == 1 and d["rewindable"] == [1]
    assert all(e["hidden"] for e in d["events"] if e["turn"] == 2)

    client.post(f"/agent/api/chats/{cid}/messages", json={"text": "换成红色"}, headers=W)
    cmd = client.get("/api/agent/web/next", params={"wait": 0}, headers=AUTH).json()["commands"][0]
    assert cmd["resume"] == "s1" and cmd["turn"] == 2


def test_agent_restart_interrupts_running_chat(web):
    client, fake = web
    login(client, fake)
    cid = client.post("/agent/api/chats", json={"folder": FOLDER}, headers=W).json()["id"]
    client.post(f"/agent/api/chats/{cid}/messages", json={"text": "hi"}, headers=W)
    client.post("/api/agent/web/hello", json={"folders": [{"path": FOLDER}], "startup": False}, headers=AUTH)
    assert client.get(f"/agent/api/chats/{cid}").json()["chat"]["status"] == "running"
    client.post("/api/agent/web/hello", json={"folders": [{"path": FOLDER}], "startup": True}, headers=AUTH)
    d = client.get(f"/agent/api/chats/{cid}").json()
    assert d["chat"]["status"] == "idle" and d["events"][-1]["type"] == "error"


def test_agent_endpoints_need_token(web):
    client, _ = web
    assert client.get("/api/agent/web/next", params={"wait": 0}).status_code in (401, 403)
    assert client.post("/api/agent/web/events", json={"chat_id": "x", "events": []}).status_code in (401, 403)


def _next(client):
    return client.get("/api/agent/web/next", params={"wait": 0}, headers=AUTH).json()["commands"]


def _events(client, cid, *evs):
    assert client.post("/api/agent/web/events", json={"chat_id": cid, "events": list(evs)}, headers=AUTH).status_code == 200


def test_model_settings_queue_and_question(web):
    client, fake = web
    login(client, fake)
    client.post("/api/agent/web/hello", json={"folders": [{"path": FOLDER}], "defaults": {"model": "claude-opus-5-5", "effort": "high"}},
                headers=AUTH)
    me = client.get("/agent/api/me").json()
    assert me["defaults"] == {"model": "claude-opus-5-5", "effort": "high"} and "max" in me["efforts"]
    assert client.post("/agent/api/chats", json={"folder": FOLDER, "model": "gpt-4"}, headers=W).status_code == 400
    cid = client.post("/agent/api/chats", json={"folder": FOLDER, "model": "claude-sonnet-5-5", "effort": "low"}, headers=W).json()["id"]

    client.post(f"/agent/api/chats/{cid}/messages", json={"text": "第一条"}, headers=W)
    cmd = _next(client)[0]
    assert cmd["model"] == "claude-sonnet-5-5" and cmd["effort"] == "low"
    # 中途换 effort：下一条起生效
    assert client.post(f"/agent/api/chats/{cid}/settings", json={"model": "", "effort": "max"}, headers=W).status_code == 200

    # 还在跑时发的消息排队，不发给电脑
    r = client.post(f"/agent/api/chats/{cid}/messages", json={"text": "第二条", "queue": True}, headers=W)
    assert r.json() == {"queued": True} and _next(client) == []
    r = client.post(f"/agent/api/chats/{cid}/messages", json={"text": "第三条", "queue": True}, headers=W)
    q = client.get(f"/agent/api/chats/{cid}").json()["queue"]
    assert [x["text"] for x in q] == ["第二条", "第三条"]
    client.post(f"/agent/api/chats/{cid}/unqueue", json={"id": q[1]["id"]}, headers=W)

    # Claude 提问 → 网页回答
    _events(client, cid, {"type": "turn_started", "turn": 1, "snapshot": "s"},
            {"type": "question", "request_id": "q1", "questions": [{"question": "用哪种颜色？", "options": [{"label": "蓝"}]}]})
    assert client.get(f"/agent/api/chats/{cid}").json()["chat"]["status"] == "waiting"
    assert any("在问你" in m for m in fake.to(f"app:{OWNER}"))
    client.post(f"/agent/api/chats/{cid}/answer", json={"request_id": "q1", "answers": {"用哪种颜色？": "蓝"}}, headers=W)
    assert _next(client)[0] == {"op": "answer", "chat_id": cid, "request_id": "q1", "answers": {"用哪种颜色？": "蓝"}}

    # 一轮结束：记下用量，自动发出排队的第二条（用新的 effort）
    usage = {"input_tokens": 1200, "output_tokens": 80, "context_pct": 12.5}
    _events(client, cid, {"type": "result", "ok": True, "usage": usage}, {"type": "status", "status": "idle"})
    cmd = _next(client)[0]
    assert cmd["op"] == "turn" and cmd["text"] == "第二条" and cmd["turn"] == 2 and cmd["effort"] == "max" and cmd["model"] == ""
    d = client.get(f"/agent/api/chats/{cid}").json()
    assert d["queue"] == [] and d["chat"]["status"] == "running"
    import json as _json
    assert _json.loads(d["chat"]["usage"])["context_pct"] == 12.5

    # 打断并发送：排队 + 停止当前这一轮
    client.post(f"/agent/api/chats/{cid}/messages", json={"text": "改主意了", "interrupt": True}, headers=W)
    assert _next(client) == [{"op": "stop", "chat_id": cid}]
    _events(client, cid, {"type": "status", "status": "idle"})
    assert _next(client)[0]["text"] == "改主意了"


def test_browse_and_open_files(web):
    import threading

    client, fake = web
    login(client, fake)
    cid = client.post("/agent/api/chats", json={"folder": FOLDER}, headers=W).json()["id"]
    out = {}

    def browse():
        out["ls"] = client.get(f"/agent/api/chats/{cid}/ls", params={"path": "src"})

    t = threading.Thread(target=browse)
    t.start()
    cmd = None
    for _ in range(100):
        cmds = _next(client)
        if cmds:
            cmd = cmds[0]
            break
    assert cmd and cmd["op"] == "ls" and cmd["path"] == "src" and cmd["folder"] == FOLDER
    entries = [{"name": "a.py", "dir": False, "size": 3, "mtime": 0}]
    client.post("/api/agent/web/reply", json={"reply_id": cmd["reply_id"], "data": {"path": "src", "entries": entries}}, headers=AUTH)
    t.join(10)
    assert out["ls"].json()["entries"] == entries

    # 电脑上传的文件：HTML 也只按纯文本显示，并且禁止脚本
    fid = client.post("/api/agent/files", files={"file": ("x.html", b"<script>alert(1)</script>", "application/octet-stream")},
                      headers=AUTH).json()["file_id"]
    r = client.get(f"/agent/api/files/{fid}")
    assert r.headers["content-type"].startswith("text/plain") and "sandbox" in r.headers["content-security-policy"]
    assert "attachment" in client.get(f"/agent/api/files/{fid}", params={"dl": 1}).headers["content-disposition"]
