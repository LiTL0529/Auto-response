import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from bridge_agent import claude_runner
from bridge_agent.claude_runner import ClaudeRunner
from bridge_agent.config import Config, Project
from bridge_agent.jobs import JobRunner

FAKE_CLAUDE = textwrap.dedent(
    f"""\
    #!{sys.executable}
    import json, os, sys, time
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_CLAUDE_MODE", "edit")
    args = sys.argv[1:]
    open(os.path.join(os.environ["FAKE_LOG_DIR"], "args.json"), "w", encoding="utf-8").write(json.dumps({{"args": args, "prompt": prompt}}))
    def emit(d): print(json.dumps(d, ensure_ascii=False), flush=True)
    emit({{"type": "system", "subtype": "init", "session_id": "sess-1"}})
    if mode == "slow":
        emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Read", "input": {{"file_path": "a.txt"}}}}]}}}})
        time.sleep(30)
    if mode == "edit":
        emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Edit", "input": {{"file_path": "app.txt"}}}}]}}}})
        with open("app.txt", "a", encoding="utf-8") as f:
            f.write("蓝色按钮\\n")
    if mode == "fail":
        emit({{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "出错了", "session_id": "sess-1"}})
        sys.exit(1)
    emit({{"type": "result", "subtype": "success", "is_error": False, "result": "按钮已改为蓝色", "session_id": "sess-1", "total_cost_usd": 0.1, "num_turns": 3}})
    """
)


class FakeAPI:
    def __init__(self, cancel_after=None):
        self.progress_msgs = []
        self.uploads = []
        self.cancel_after = cancel_after

    def progress(self, job_id, message):
        self.progress_msgs.append(message)
        return {"cancel": self.cancel_after is not None and len(self.progress_msgs) >= self.cancel_after}

    def upload(self, path):
        self.uploads.append(Path(path).name)
        return "file123"

    def download(self, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png")
        return dest


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "app.txt").write_text("hello\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    if sys.platform == "win32":
        fake = tmp_path / "claude.cmd"
        fake.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        fake = script
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-be-removed")
    return r, fake, tmp_path


def make_runner(repo, api, **proj):
    r, fake, _ = repo
    p = Project(id="shop", path=str(r), name="商城", **proj)
    cfg = Config(server_url="http://x", agent_token="t", projects=[p], claude_path=str(fake))
    return JobRunner(cfg, api, ClaudeRunner(str(fake)))


def job(**kw):
    base = {
        "job_id": 1, "kind": "code", "project_id": "shop", "task_id": 7, "title": "按钮改蓝",
        "requirement": "登录按钮改蓝色", "acceptance": ["按钮是蓝色"], "raw_messages": ["按钮改蓝"],
        "attachments": [], "requester": "小李", "wants_package": False, "related_task": None,
    }
    base.update(kw)
    return base


def test_edit_commit_test_merge(repo):
    r, _, tmp = repo
    api = FakeAPI()
    res = make_runner(repo, api, test=f'"{sys.executable}" -c "print(1)"').run(job(attachments=[{"file_id": "f1", "filename": "shot.png"}]))
    assert res["ok"] and res["changed"], res
    assert res["summary"] == "按钮已改为蓝色" and res["session_id"] == "sess-1"
    assert git(r, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert "蓝色按钮" in (r / "app.txt").read_text(encoding="utf-8")
    assert "Merge claude/task-7" in git(r, "log", "-1", "--format=%s")
    assert "app.txt" in res["diff_stat"]
    # 附件下载了但没被提交
    assert (r / ".claude-attachments/task-7/1-shot.png").exists()
    assert ".claude-attachments" not in git(r, "show", "--stat", "HEAD^2")
    call = json.loads((tmp / "args.json").read_text(encoding="utf-8"))
    assert ".claude-attachments/task-7/1-shot.png" in call["prompt"]
    assert "--allowedTools" in call["args"] and "acceptEdits" in call["args"]


def test_api_key_stripped_for_subscription():
    env = ClaudeRunner("claude", use_api_key=False).env()
    assert "ANTHROPIC_API_KEY" not in env


def test_failing_tests_keep_branch_unmerged(repo):
    r, _, _ = repo
    res = make_runner(repo, FakeAPI(), test=f'"{sys.executable}" -c "import sys; print(\'FAILED x\'); sys.exit(1)"').run(job())
    assert not res["ok"] and "测试未通过" in res["error"] and "FAILED x" in res["error"]
    assert git(r, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert "蓝色按钮" not in (r / "app.txt").read_text(encoding="utf-8")
    assert "claude/task-7" in git(r, "branch")


def test_no_change(repo, monkeypatch):
    r, _, _ = repo
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noop")
    res = make_runner(repo, FakeAPI()).run(job())
    assert res["ok"] and res["changed"] is False
    assert "claude/task-7" not in git(r, "branch")


def test_claude_failure(repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    res = make_runner(repo, FakeAPI()).run(job())
    assert not res["ok"] and "出错了" in res["error"]


def test_cancel(repo, monkeypatch):
    r, _, _ = repo
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    monkeypatch.setattr(claude_runner, "PROGRESS_INTERVAL", 0.2)
    res = make_runner(repo, FakeAPI(cancel_after=1)).run(job())
    assert res.get("canceled") and res["duration_s"] < 20
    assert git(r, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert "claude/task-7" not in git(r, "branch")


def test_dirty_repo_stashed_and_package_uploaded(repo):
    r, _, _ = repo
    (r / "wip.txt").write_text("my work", encoding="utf-8")
    pkg_cmd = f'"{sys.executable}" -c "import os; os.makedirs(\'dist\', exist_ok=True); open(\'dist/app-setup.exe\',\'w\').write(\'x\')"'
    git(r, "init")  # no-op, keeps repo
    (r / ".gitignore").write_text("dist/\n", encoding="utf-8")
    git(r, "add", ".gitignore")
    git(r, "commit", "-q", "-m", "ignore dist")
    api = FakeAPI()
    res = make_runner(repo, api, package=pkg_cmd, package_artifact="dist/*.exe").run(job(wants_package=True))
    assert res["ok"], res
    assert "git stash" in res["notes"][0] and "stash" not in res["summary"]
    assert "my work" not in (r / "app.txt").read_text(encoding="utf-8")
    assert "wip.txt" in git(r, "stash", "show", "--include-untracked", "--name-only")
    assert res["package_file_id"] == "file123" and api.uploads == ["app-setup.exe"]


def test_dirty_repo_fail_policy(repo):
    r, _, _ = repo
    (r / "app.txt").write_text("changed", encoding="utf-8")
    res = make_runner(repo, FakeAPI(), on_dirty="fail").run(job())
    assert not res["ok"] and "未提交" in res["error"]
    assert (r / "app.txt").read_text(encoding="utf-8") == "changed"


def test_deploy(repo):
    ok = make_runner(repo, FakeAPI(), deploy=f'"{sys.executable}" -c "print(\'deployed\')"').run({"job_id": 2, "kind": "deploy", "project_id": "shop", "task_ids": [7]})
    assert ok["ok"] and "deployed" in ok["output_tail"]
    bad = make_runner(repo, FakeAPI(), deploy=f'"{sys.executable}" -c "import sys; sys.exit(3)"').run({"job_id": 3, "kind": "deploy", "project_id": "shop"})
    assert not bad["ok"]


def test_resume_used_for_followup(repo):
    _, _, tmp = repo
    make_runner(repo, FakeAPI()).run(job(related_task={"id": 6, "title": "t", "requirement": "r", "summary": "s", "session_id": "old-sess"}))
    call = json.loads((tmp / "args.json").read_text(encoding="utf-8"))
    assert call["args"][call["args"].index("--resume") + 1] == "old-sess"
    assert "关联的上一个任务 #6" in call["prompt"]


def test_config_loading(tmp_path):
    from bridge_agent.config import load_config

    (tmp_path / "p").mkdir()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"server_url: https://bot.example.com/\nagent_token: x\nprojects:\n  - id: a\n    path: {tmp_path / 'p'}\n    aliases: [后台]\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.server_url == "https://bot.example.com" and cfg.projects[0].aliases == ["后台"]
    assert cfg.projects[0].public_info()["has_deploy"] is False
    cfg_file.write_text(f"server_url: x\nagent_token: x\nprojects:\n  - id: a\n    path: {tmp_path / 'p'}\n    typo: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="typo"):
        load_config(cfg_file)
