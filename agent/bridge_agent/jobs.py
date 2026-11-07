"""执行服务器下发的任务：code（让 Claude 改代码）和 deploy（执行部署命令）。"""

from __future__ import annotations

import glob
import logging
import os
import time
import traceback
from pathlib import Path

from . import gitops, shell
from .api import ServerAPI
from .claude_runner import ClaudeRunner
from .config import (
    DEFAULT_ALLOWED_TOOLS, DEFAULT_DISALLOWED_TOOLS, QUERY_ALLOWED_TOOLS, QUERY_DISALLOWED_TOOLS, Config, Project,
)
from .gitops import GitError
from .prompt import build_prompt, build_query_prompt

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, cfg: Config, api: ServerAPI, claude: ClaudeRunner):
        self.cfg = cfg
        self.api = api
        self.claude = claude

    def run(self, job: dict) -> dict:
        project = self.cfg.project(job["project_id"])
        if not project:
            return {"ok": False, "error": f"本机没有配置项目 {job['project_id']}"}
        start = time.time()
        try:
            if job["kind"] == "deploy":
                result = self.run_deploy(job, project)
            elif job["kind"] == "query":
                result = self.run_query(job, project)
            else:
                result = self.run_code(job, project)
        except Exception as e:  # noqa: BLE001
            log.exception("任务执行异常")
            result = {"ok": False, "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"}
            self._back_to_base(project)
        result["duration_s"] = int(time.time() - start)
        return result

    # ------------------------------------------------------------------ query
    def run_query(self, job: dict, p: Project) -> dict:
        """只读查询线上数据：Claude 只能读代码和执行 p.query，不能改文件，不建分支、不提交。"""
        if not p.query:
            return {"ok": False, "error": f"项目 {p.id} 没有配置 query，不支持查询线上数据"}

        def on_progress(msg: str) -> bool:
            return bool(self.api.progress(job["job_id"], msg).get("cancel"))

        allowed = QUERY_ALLOWED_TOOLS + [f"Bash({p.query} *)"]
        disallowed = DEFAULT_DISALLOWED_TOOLS + QUERY_DISALLOWED_TOOLS
        timeout = min(p.timeout_minutes or self.cfg.timeout_minutes, 20) * 60
        cr = self.claude.run(
            build_query_prompt(job, p), p.path, allowed, disallowed, timeout, None, on_progress, permission_mode="default"
        )
        base = {"session_id": cr.session_id, "cost_usd": cr.cost_usd}
        if cr.canceled:
            return {**base, "ok": False, "canceled": True}
        if not cr.ok:
            return {**base, "ok": False, "error": cr.error}
        return {**base, "ok": True, "summary": (cr.text or "").strip()}

    # ------------------------------------------------------------------ code
    def run_code(self, job: dict, p: Project) -> dict:
        job_id, task_id = job["job_id"], job["task_id"]
        notes: list[str] = []
        path, base = p.path, p.base_branch

        gitops.ensure_repo(path)
        if gitops.is_dirty(path):
            if p.on_dirty == "fail":
                return {"ok": False, "error": "项目目录有未提交的改动，已按配置拒绝执行（on_dirty: fail）"}
            gitops.stash(path, gitops.stash_message(task_id))
            notes.append("开始前项目里有未提交的改动，已自动 git stash 暂存（git stash list 查看）。")

        branch = gitops.unique_branch(path, f"claude/task-{task_id}")
        gitops.start_branch(path, base, branch)

        attachments = self._fetch_attachments(job, p)
        prompt = build_prompt(job, p, attachments, self.cfg.extra_prompt)
        related = job.get("related_task") or {}
        resume = related.get("session_id") or None

        def on_progress(msg: str) -> bool:
            return bool(self.api.progress(job_id, msg).get("cancel"))

        timeout = (p.timeout_minutes or self.cfg.timeout_minutes) * 60
        allowed = DEFAULT_ALLOWED_TOOLS + p.allowed_tools
        cr = self.claude.run(prompt, path, allowed, DEFAULT_DISALLOWED_TOOLS, timeout, resume, on_progress)
        if resume and not cr.ok and not cr.canceled and not cr.timed_out and cr.num_turns == 0:
            log.info("恢复上次会话失败，改为新会话：%s", cr.error)
            cr = self.claude.run(prompt, path, allowed, DEFAULT_DISALLOWED_TOOLS, timeout, None, on_progress)

        base_result = {"branch": branch, "session_id": cr.session_id, "cost_usd": cr.cost_usd}

        if cr.canceled:
            gitops.discard_branch(path, base, branch)
            return {**base_result, "ok": False, "canceled": True, "branch": ""}

        commit_msg = f"#{task_id} {job['title']}\n\n需求方：{job.get('requester', '')}\n{job['requirement'][:800]}"
        if not cr.ok:
            kept = gitops.commit_all(path, f"[未完成] {commit_msg}")
            gitops.checkout(path, base)
            where = f"\n半成品已保留在分支 {branch}" if kept else ""
            return {**base_result, "ok": False, "error": f"{cr.error}{where}"}

        changed = gitops.commit_all(path, commit_msg)
        summary = cr.text
        if not changed:
            gitops.checkout(path, base)
            shell.git(path, "branch", "-D", branch)
            return {**base_result, "ok": True, "changed": False, "branch": "", "summary": summary, "notes": notes}

        result = {**base_result, "ok": True, "changed": True, "diff_stat": gitops.diff_stat(path, base, branch)}

        if p.test:
            self.api.progress(job_id, "运行测试")
            t = shell.run(p.test, cwd=path, timeout=p.command_timeout_minutes * 60)
            if not t.ok:
                gitops.checkout(path, base)
                return {**result, "ok": False, "error": f"测试未通过，代码保留在分支 {branch}：\n{t.tail(2000)}"}
            result["test_output_tail"] = t.tail(500)

        if p.auto_merge:
            gitops.merge(path, base, branch, f"Merge {branch}: #{task_id} {job['title']}")
        else:
            notes.append(f"代码在分支 {branch}，需要手动合并。")

        if p.package and (job.get("wants_package") or p.package_on_change):
            self.api.progress(job_id, "打包安装包")
            file_id, err = self._package(p)
            if file_id:
                result["package_file_id"] = file_id
            else:
                result["package_error"] = err
                summary += "\n（安装包生成失败，负责人会处理。）"

        if not p.auto_merge:
            gitops.checkout(path, base)
        result["summary"] = summary.strip()
        result["notes"] = notes
        return result

    def _fetch_attachments(self, job: dict, p: Project) -> list[str]:
        items = job.get("attachments") or []
        if not items:
            return []
        gitops.exclude_attachments(p.path)
        rel_dir = Path(gitops.ATTACH_DIR) / f"task-{job['task_id']}"
        paths = []
        for i, a in enumerate(items, 1):
            name = os.path.basename(a.get("filename") or "image.jpg")
            rel = rel_dir / f"{i}-{name}"
            try:
                self.api.download(a["file_id"], Path(p.path) / rel)
                paths.append(rel.as_posix())
            except Exception as e:  # noqa: BLE001
                log.warning("下载附件失败：%s", e)
        return paths

    def _package(self, p: Project) -> tuple[str | None, str]:
        started = time.time()
        res = shell.run(p.package, cwd=p.path, timeout=p.command_timeout_minutes * 60)
        if not res.ok:
            return None, res.tail(1500)
        if not p.package_artifact:
            return None, "未配置 package_artifact，无法找到安装包文件"
        pattern = p.package_artifact if os.path.isabs(p.package_artifact) else os.path.join(p.path, p.package_artifact)
        files = [f for f in glob.glob(pattern) if os.path.isfile(f) and os.path.getmtime(f) >= started - 5]
        if not files:
            return None, f"打包命令成功，但没找到本次生成的文件：{p.package_artifact}"
        newest = max(files, key=os.path.getmtime)
        return self.api.upload(newest), ""

    # ------------------------------------------------------------------ deploy
    def run_deploy(self, job: dict, p: Project) -> dict:
        if not p.deploy:
            return {"ok": False, "error": "没有配置部署命令"}
        gitops.ensure_repo(p.path)
        if gitops.current_branch(p.path) != p.base_branch:
            if gitops.is_dirty(p.path):
                gitops.stash(p.path, f"claude-bridge: 部署前自动暂存 {time.strftime('%Y-%m-%d %H:%M')}")
            gitops.checkout(p.path, p.base_branch)
        self.api.progress(job["job_id"], "部署中")
        res = shell.run(p.deploy, cwd=p.path, timeout=p.command_timeout_minutes * 60)
        if res.ok:
            return {"ok": True, "output_tail": res.tail(1000)}
        return {"ok": False, "error": res.tail(2500)}

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _back_to_base(p: Project) -> None:
        try:
            if gitops.current_branch(p.path) != p.base_branch:
                gitops.commit_all(p.path, "[异常中断] claude-bridge 自动保存")
                gitops.checkout(p.path, p.base_branch)
        except (GitError, Exception):  # noqa: BLE001
            log.exception("回到主分支失败")
