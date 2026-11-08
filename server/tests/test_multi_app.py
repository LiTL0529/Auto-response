"""多个企业微信自建应用：每个应用有自己的回调地址、只处理自己的项目，回复和审批都走同一个应用。"""

import base64
import time
import os

import pytest
from fastapi.testclient import TestClient

from app.config import ExtraApp, Settings
from app.main import create_app
from app.wecom.crypto import WeComCrypto
from tests.test_flow import AES_KEY, AGENT_TOKEN, AUTH, CORP_ID, OWNER, FakeWeCom, app_text, wait_for

DX_AES = base64.b64encode(os.urandom(32)).decode().rstrip("=")


def make_settings(tmp_path, **kw):
    base = dict(
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
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def env(tmp_path):
    main, dx = FakeWeCom(), FakeWeCom()
    with TestClient(create_app(make_settings(tmp_path), wecom=main, extra_wecom={"dx": dx})) as client:
        projects = [
            {"id": "shop", "name": "商城后台", "aliases": [], "description": "", "has_deploy": False},
            {"id": "party", "name": "党校平台", "aliases": ["党校"], "description": "", "has_deploy": True, "url": "https://x/dx/"},
        ]
        assert client.post("/api/agent/hello", json={"projects": projects, "startup": True}, headers=AUTH).status_code == 200
        yield client, main, dx


def push(client, path, crypto, xml):
    body, params = crypto.build_callback(xml)
    r = client.post(path, params=params, content=body)
    assert r.status_code == 200, r.text


def test_extra_app_routes_to_its_project_and_replies_through_it(env):
    client, main, dx = env
    dxc = WeComCrypto("dxtoken", DX_AES, CORP_ID)
    owner_dx = f"app:{OWNER}"  # FakeWeCom 记录的前缀固定是 app:，用不同的 fake 区分应用

    # 没提项目名，也会落到党校应用唯一的项目上
    push(client, "/wecom/callback/dx", dxc, app_text(OWNER, "首页横幅换成红色", "d1"))
    assert wait_for(lambda: any("已加入队列" in m for m in dx.to(owner_dx)))
    assert not main.sent

    job = client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"]
    assert job["project_id"] == "party"
    client.post(
        f"/api/agent/jobs/{job['job_id']}/result",
        json={"ok": True, "changed": True, "summary": "横幅已改红", "branch": "claude/task-1"},
        headers=AUTH,
    )
    assert any("同意 1" in m for m in dx.to(owner_dx))

    # 在党校应用里同意部署，部署结果也回到党校应用
    push(client, "/wecom/callback/dx", dxc, app_text(OWNER, "同意 1", "d2"))
    assert wait_for(lambda: any("开始部署" in m for m in dx.to(owner_dx)))
    dep = client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"]
    assert dep["kind"] == "deploy" and dep["task_ids"] == [1]
    client.post(f"/api/agent/jobs/{dep['job_id']}/result", json={"ok": True}, headers=AUTH)
    assert any("部署成功" in m for m in dx.to(owner_dx))
    assert not main.sent

    # 主应用的「进度」只看主应用的项目
    main_c = WeComCrypto("cbtoken", AES_KEY, CORP_ID)
    push(client, "/wecom/callback", main_c, app_text(OWNER, "进度", "m1"))
    assert wait_for(lambda: any("还没有任务" in m for m in main.to(owner_dx)))


def test_main_app_does_not_take_other_apps_projects(env):
    client, main, dx = env
    main_c = WeComCrypto("cbtoken", AES_KEY, CORP_ID)
    push(client, "/wecom/callback", main_c, app_text(OWNER, "党校首页横幅换成红色", "m1"))
    assert wait_for(lambda: any("已加入队列" in c for _, c in main.sent))
    job = client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"]
    assert job["project_id"] == "shop"


def test_unknown_app_and_url_verification(env):
    client, _, _ = env
    assert client.get("/wecom/callback/nope", params={"msg_signature": "x", "timestamp": "1", "nonce": "n", "echostr": "e"}).status_code == 404
    assert client.get("/wecom/callback/app", params={"msg_signature": "x", "timestamp": "1", "nonce": "n", "echostr": "e"}).status_code == 404
    dxc = WeComCrypto("dxtoken", DX_AES, CORP_ID)
    echo = dxc.encrypt("hi-dx")
    sig = dxc.signature("1700000000", "n", echo)
    r = client.get("/wecom/callback/dx", params={"msg_signature": sig, "timestamp": "1700000000", "nonce": "n", "echostr": echo})
    assert r.text == "hi-dx"


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("WECOM_APPS", "dx, lab")
    monkeypatch.setenv("WECOM_DX_NAME", "智能党校")
    monkeypatch.setenv("WECOM_DX_AGENT_ID", "1000003")
    monkeypatch.setenv("WECOM_DX_PROJECTS", "party")
    s = Settings.from_env()
    assert [a.key for a in s.extra_apps] == ["dx", "lab"]
    assert s.extra_apps[0].projects == ["party"] and s.extra_apps[0].name == "智能党校"
    missing = s.missing()
    assert "WECOM_DX_APP_SECRET" in missing and "WECOM_LAB_AGENT_ID" in missing


def test_grant_without_channel_uses_the_app_it_was_sent_in(env):
    client, main, dx = env
    dxc = WeComCrypto("dxtoken", DX_AES, CORP_ID)
    owner = f"app:{OWNER}"
    # 先在主应用授权过，名字沿用
    main_c = WeComCrypto("cbtoken", AES_KEY, CORP_ID)
    push(client, "/wecom/callback", main_c, app_text(OWNER, "授权 TangMuMao 汤姆猫", "g1"))
    assert wait_for(lambda: any("已授权 汤姆猫" in m for m in main.to(owner)))
    # 在党校应用里用口语写法授权：不建任务、授权到 dx 渠道
    push(client, "/wecom/callback/dx", dxc, app_text(OWNER, "授权给TangMuMao", "g2"))
    assert wait_for(lambda: any("已授权 汤姆猫" in m for m in dx.to(owner)))
    assert any("已开通需求通道" in m for m in dx.to("app:TangMuMao"))
    users = client.app.state.core.db.list_users()
    assert {u["uid"] for u in users} >= {"app:TangMuMao", "dx:TangMuMao"}
    assert client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"] is None


def test_malformed_grant_from_owner_is_not_a_task(env):
    client, main, dx = env
    dxc = WeComCrypto("dxtoken", DX_AES, CORP_ID)
    push(client, "/wecom/callback/dx", dxc, app_text(OWNER, "授权汤姆猫用党校平台", "g3"))
    assert wait_for(lambda: any("授权指令的格式" in m for m in dx.to(f"app:{OWNER}")))
    time.sleep(0.3)
    assert client.get("/api/agent/jobs/next?wait=0", headers=AUTH).json()["job"] is None
