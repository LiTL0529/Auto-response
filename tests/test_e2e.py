"""端到端：企业微信加密回调 → 服务器 → Agent（假 Claude）→ 审批 → 部署。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "agent"), str(ROOT / "server" / "tests"), str(ROOT / "agent" / "tests")]

from bridge_agent.api import ServerAPI  # noqa: E402
from test_flow import AUTH, OWNER, app_text, env, push, wait_for  # noqa: E402,F401
from test_jobs import make_runner, repo  # noqa: E402,F401


def test_wechat_to_claude_to_deploy(env, repo):
    client, fake, crypto = env
    r, _, _ = repo
    api = ServerAPI("http://unused", "x")
    client.headers.update(AUTH)
    api.http = client
    runner = make_runner(repo, api, deploy=f'"{sys.executable}" -c "print(\'ok\')"', url="https://shop.example.com")
    runner.cfg.projects[0].aliases = ["商城"]
    api.hello([p.public_info() for p in runner.cfg.projects], startup=True)
    owner = f"app:{OWNER}"

    push(client, crypto, app_text(OWNER, "商城的登录按钮改成蓝色", "e1"))
    assert wait_for(lambda: any("已加入队列" in m for m in fake.to(owner)))

    job = api.next_job(wait=0)
    result = runner.run(job)
    assert result["ok"], result
    api.result(job["job_id"], result)
    assert "蓝色按钮" in (r / "app.txt").read_text(encoding="utf-8")
    assert any("按钮已改为蓝色" in m for m in fake.to(owner))
    assert any("同意 1" in m for m in fake.to(owner))

    push(client, crypto, app_text(OWNER, "同意 1", "e2"))
    assert wait_for(lambda: any("开始部署" in m for m in fake.to(owner)))
    dep = api.next_job(wait=0)
    api.result(dep["job_id"], runner.run(dep))
    assert any("部署成功" in m and "shop.example.com" in m for m in fake.to(owner))
