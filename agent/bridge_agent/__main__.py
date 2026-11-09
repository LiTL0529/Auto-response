"""家里电脑上的 Agent 入口。

    python -m bridge_agent --config config.yaml          # 正常运行
    python -m bridge_agent --config config.yaml --check  # 检查配置、Claude、git、服务器连通性
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import time
from pathlib import Path

from . import gitops, shell
from .api import ServerAPI
from .claude_runner import ClaudeRunner, resolve_claude
from .config import Config, load_config
from .jobs import JobRunner

log = logging.getLogger("bridge_agent")
HELLO_INTERVAL = 600


def setup_logging(cfg: Config, base: Path) -> None:
    log_dir = Path(cfg.log_dir) if Path(cfg.log_dir).is_absolute() else base / cfg.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(log_dir / "agent.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:  # pythonw 下没有控制台
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        handlers.append(console)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def check(cfg: Config, api: ServerAPI) -> int:
    ok = True

    def item(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"[{'OK' if passed else 'XX'}] {name}{('：' + detail) if detail else ''}")

    try:
        exe = resolve_claude(cfg.claude_path)
        v = shell.run([exe, "--version"], cwd=".", timeout=30, shell=False)
        item("Claude Code", v.ok, v.output.strip() or exe)
    except FileNotFoundError as e:
        item("Claude Code", False, str(e))
    g = shell.run(["git", "--version"], cwd=".", timeout=30, shell=False)
    item("git", g.ok, g.output.strip())
    for p in cfg.projects:
        try:
            gitops.ensure_repo(p.path)
            exists = gitops.branch_exists(p.path, p.base_branch)
            dirty = gitops.is_dirty(p.path)
            item(
                f"项目 {p.id}",
                exists,
                f"{p.path}，主分支 {p.base_branch}{'' if exists else ' 不存在'}"
                f"{'，有未提交改动' if dirty else ''}"
                f"{'，可部署' if p.deploy else ''}{'，可打包' if p.package else ''}",
            )
        except Exception as e:  # noqa: BLE001
            item(f"项目 {p.id}", False, str(e))
    try:
        api.hello([p.public_info() for p in cfg.projects], startup=False)
        item("服务器", True, cfg.server_url)
    except Exception as e:  # noqa: BLE001
        item("服务器", False, f"{cfg.server_url}：{e}")
    print("\n全部通过 ✅" if ok else "\n有检查项未通过 ❌")
    return 0 if ok else 1


def start_webchat(cfg: Config, data_dir: Path) -> None:
    """网页 Agent（独立线程，自己的连接）。没配 web_folders 或没装 SDK 时不启用。"""
    if not cfg.web_folders:
        return
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        log.warning("配置了 web_folders，但没有安装 claude-agent-sdk，网页 Agent 未启用")
        return
    from .webchat import WebChat

    WebChat(cfg, ServerAPI(cfg.server_url, cfg.agent_token), data_dir).start()


def serve(cfg: Config, api: ServerAPI, data_dir: Path | None = None) -> None:
    if data_dir:
        start_webchat(cfg, data_dir)
    claude = ClaudeRunner(cfg.claude_path, cfg.claude_model, cfg.use_api_key, cfg.max_turns, cfg.claude_effort)
    runner = JobRunner(cfg, api, claude)
    projects = [p.public_info() for p in cfg.projects]

    delay = 2
    while True:
        try:
            api.hello(projects, startup=True)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("连接服务器失败，%ds 后重试：%s", delay, e)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    log.info("已连接 %s，项目：%s", cfg.server_url, ", ".join(p.id for p in cfg.projects))

    last_hello = time.time()
    delay = 2
    while True:
        try:
            if time.time() - last_hello > HELLO_INTERVAL:
                api.hello(projects, startup=False)
                last_hello = time.time()
            job = api.next_job(wait=25)
            delay = 2
        except Exception as e:  # noqa: BLE001
            log.warning("拉取任务失败，%ds 后重试：%s", delay, e)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if not job:
            continue
        log.info("收到任务 job=%s kind=%s project=%s", job["job_id"], job["kind"], job["project_id"])
        result = runner.run(job)
        log.info("任务完成 job=%s ok=%s", job["job_id"], result.get("ok"))
        api.result(job["job_id"], result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge_agent")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--check", action="store_true", help="检查环境后退出")
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    cfg = load_config(config_path)
    setup_logging(cfg, config_path.parent)
    api = ServerAPI(cfg.server_url, cfg.agent_token)
    if args.check:
        return check(cfg, api)
    try:
        serve(cfg, api, config_path.parent / "web-data")
    except KeyboardInterrupt:
        log.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
