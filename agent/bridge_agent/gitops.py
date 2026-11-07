"""项目目录的 git 操作：每个需求一个分支，做完提交、测试通过后合并回主分支。"""

from __future__ import annotations

import time
from pathlib import Path

from .shell import git

AGENT_NAME = "Claude Bridge"
AGENT_EMAIL = "claude-bridge@localhost"
ATTACH_DIR = ".claude-attachments"


class GitError(Exception):
    pass


def _check(res, what: str):
    if not res.ok:
        raise GitError(f"{what} 失败：{res.tail(1500)}")
    return res.output.strip()


def ensure_repo(path: str) -> None:
    res = git(path, "rev-parse", "--is-inside-work-tree")
    if not res.ok or res.output.strip() != "true":
        raise GitError(f"{path} 不是 git 仓库，请先 git init 并提交一次")


def current_branch(path: str) -> str:
    return _check(git(path, "rev-parse", "--abbrev-ref", "HEAD"), "读取当前分支")


def branch_exists(path: str, name: str) -> bool:
    return git(path, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").ok


def is_dirty(path: str) -> bool:
    return bool(_check(git(path, "status", "--porcelain"), "git status"))


def stash(path: str, message: str) -> None:
    _check(git(path, "stash", "push", "-u", "-m", message), "git stash")


def checkout(path: str, branch: str) -> None:
    _check(git(path, "checkout", branch), f"切换到 {branch}")


def unique_branch(path: str, base_name: str) -> str:
    name, n = base_name, 2
    while branch_exists(path, name):
        name = f"{base_name}-{n}"
        n += 1
    return name


def start_branch(path: str, base: str, branch: str) -> None:
    if not branch_exists(path, base):
        raise GitError(f"主分支 {base} 不存在（检查 base_branch 配置）")
    checkout(path, base)
    _check(git(path, "checkout", "-b", branch), f"创建分支 {branch}")


def _identity_args(path: str) -> list[str]:
    if git(path, "config", "user.email").output.strip():
        return []
    return ["-c", f"user.name={AGENT_NAME}", "-c", f"user.email={AGENT_EMAIL}"]


def commit_all(path: str, message: str) -> bool:
    """提交所有改动，没有改动返回 False。"""
    _check(git(path, "add", "-A"), "git add")
    if git(path, "diff", "--cached", "--quiet").ok:
        return False
    _check(git(path, *_identity_args(path), "commit", "-q", "-m", message), "git commit")
    return True


def diff_stat(path: str, base: str, branch: str) -> str:
    return git(path, "diff", "--stat", f"{base}...{branch}").output.strip()


def merge(path: str, base: str, branch: str, message: str) -> None:
    checkout(path, base)
    res = git(path, *_identity_args(path), "merge", "--no-ff", "-m", message, branch)
    if not res.ok:
        git(path, "merge", "--abort")
        raise GitError(f"合并 {branch} 到 {base} 失败（可能有冲突），分支已保留：{res.tail(800)}")


def discard_branch(path: str, base: str, branch: str) -> None:
    """放弃分支上未提交的改动并删除分支（用于取消任务）。"""
    git(path, "reset", "--hard")
    git(path, "clean", "-fd", "-e", ATTACH_DIR)
    git(path, "checkout", base)
    git(path, "branch", "-D", branch)


def exclude_attachments(path: str) -> None:
    """把附件目录加入 .git/info/exclude，避免被提交。"""
    rel = git(path, "rev-parse", "--git-path", "info/exclude").output.strip()
    if not rel:
        return
    f = Path(rel) if Path(rel).is_absolute() else Path(path) / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    content = f.read_text(encoding="utf-8") if f.exists() else ""
    line = f"/{ATTACH_DIR}/"
    if line not in content.splitlines():
        f.write_text(content.rstrip("\n") + ("\n" if content else "") + line + "\n", encoding="utf-8")


def stash_message(task_id) -> str:
    return f"claude-bridge: 任务 #{task_id} 开始前自动暂存 {time.strftime('%Y-%m-%d %H:%M')}"
