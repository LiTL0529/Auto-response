from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Claude 可以直接使用的工具。没列出来的命令在无人值守模式下会被自动拒绝，Claude 会换别的方式。
DEFAULT_ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "LS", "TodoWrite", "NotebookEdit",
    "Bash(npm *)", "Bash(npx *)", "Bash(pnpm *)", "Bash(yarn *)", "Bash(node *)",
    "Bash(python *)", "Bash(python3 *)", "Bash(py *)", "Bash(pip *)", "Bash(pip3 *)", "Bash(pytest *)", "Bash(uv *)",
    "Bash(mvn *)", "Bash(gradle *)", "Bash(./gradlew *)", "Bash(dotnet *)", "Bash(go *)", "Bash(cargo *)",
    "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)", "Bash(git branch*)",
    "Bash(ls *)", "Bash(cat *)", "Bash(head *)", "Bash(tail *)", "Bash(find *)", "Bash(grep *)", "Bash(mkdir *)",
]
# 明确禁止：版本管理和部署由 Agent 负责，不让 Claude 自己动
DEFAULT_DISALLOWED_TOOLS = [
    "Bash(git commit*)", "Bash(git push*)", "Bash(git checkout*)", "Bash(git switch*)", "Bash(git reset*)",
    "Bash(git rebase*)", "Bash(git merge*)", "Bash(git stash*)", "Bash(git clean*)",
    "Bash(rm -rf*)", "Bash(del /s*)", "Bash(rmdir /s*)", "Bash(format *)", "Bash(shutdown*)",
    "Bash(npm publish*)",
]


@dataclass
class Project:
    id: str
    path: str
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    base_branch: str = "main"
    test: str = ""  # 测试/构建校验命令，失败则不合并
    deploy: str = ""  # 部署命令，需要负责人在微信里确认后才执行
    package: str = ""  # 打包命令，自动执行
    package_artifact: str = ""  # 安装包路径（glob），如 dist/*.exe
    package_on_change: bool = False  # 每次改完都打包（否则仅在需求方要求时打包）
    url: str = ""  # 线上地址，部署成功后告诉需求方
    notes: str = ""  # 额外的项目约定，会附加到提示词
    on_dirty: str = "stash"  # 项目目录有未提交改动时：stash（自动暂存）/ fail（拒绝执行）
    auto_merge: bool = True  # 测试通过后自动合并到 base_branch
    allowed_tools: list[str] = field(default_factory=list)  # 追加允许的工具
    timeout_minutes: int = 0
    command_timeout_minutes: int = 30

    def public_info(self) -> dict:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "aliases": self.aliases,
            "description": self.description,
            "has_deploy": bool(self.deploy),
            "has_package": bool(self.package),
            "url": self.url,
        }


@dataclass
class Config:
    server_url: str
    agent_token: str
    projects: list[Project]
    claude_path: str = "claude"
    claude_model: str = ""  # 留空使用 Claude Code 默认模型
    timeout_minutes: int = 60
    max_turns: int = 100
    use_api_key: bool = False  # False：强制使用订阅账号登录（会去掉 ANTHROPIC_API_KEY 环境变量）
    log_dir: str = "logs"
    extra_prompt: str = ""

    def project(self, project_id: str) -> Project | None:
        return next((p for p in self.projects if p.id == project_id), None)


def load_config(path: str | os.PathLike) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    projects = []
    for raw in data.pop("projects", []) or []:
        unknown = set(raw) - set(Project.__dataclass_fields__)
        if unknown:
            raise ValueError(f"项目 {raw.get('id')} 有未知配置项：{', '.join(sorted(unknown))}")
        projects.append(Project(**raw))
    if not projects:
        raise ValueError("config.yaml 里至少需要配置一个项目（projects）")
    ids = [p.id for p in projects]
    if len(ids) != len(set(ids)):
        raise ValueError("项目 id 重复")
    for p in projects:
        if not Path(p.path).is_dir():
            raise ValueError(f"项目 {p.id} 的目录不存在：{p.path}")
    unknown = set(data) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"未知配置项：{', '.join(sorted(unknown))}")
    cfg = Config(projects=projects, **data)
    cfg.server_url = cfg.server_url.rstrip("/")
    return cfg
