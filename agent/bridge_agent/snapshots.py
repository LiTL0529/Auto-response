"""目录快照（网页 Agent 的「回退」用）。

每个工作目录对应 Agent 数据目录里的一个影子 git 仓库（--git-dir 指向它，--work-tree 指向项目目录），
所以不会碰项目自己的 .git，非 git 目录也能用。遵守项目里的 .gitignore，另外忽略常见的依赖/构建目录。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from . import shell

DEFAULT_EXCLUDES = """\
node_modules/
.venv/
venv/
__pycache__/
*.pyc
.next/
.nuxt/
dist/
build/
target/
.gradle/
.idea/
.vs/
"""


class SnapshotError(RuntimeError):
    pass


class Snapshots:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _git_dir(self, folder: str) -> Path:
        key = hashlib.sha1(os.path.normcase(os.path.abspath(folder)).encode("utf-8")).hexdigest()[:16]
        return self.root / f"{key}.git"

    def _git(self, folder: str, *args: str, timeout: float = 600) -> shell.CmdResult:
        gd = self._git_dir(folder)
        return shell.run(
            ["git", f"--git-dir={gd}", f"--work-tree={folder}", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.quotepath=false",
             "-c", "user.name=web-agent", "-c", "user.email=web-agent@localhost", *args],
            cwd=folder, timeout=timeout,
        )

    def _ensure(self, folder: str) -> None:
        gd = self._git_dir(folder)
        if (gd / "HEAD").exists():
            return
        gd.parent.mkdir(parents=True, exist_ok=True)
        r = shell.run(["git", "init", "-q", "--bare", str(gd)], cwd=str(gd.parent), timeout=60)
        if not r.ok:
            raise SnapshotError(f"初始化快照仓库失败：{r.tail(500)}")
        (gd / "info").mkdir(exist_ok=True)
        (gd / "info" / "exclude").write_text(DEFAULT_EXCLUDES, encoding="utf-8")
        (gd / "folder.txt").write_text(folder, encoding="utf-8")

    def _check(self, r: shell.CmdResult, what: str) -> str:
        if not r.ok:
            raise SnapshotError(f"{what}失败：{r.tail(800)}")
        return r.output.strip()

    NESTED_MARK = "# nested repo"

    def nested_repos(self, folder: str) -> list[str]:
        """快照时跳过的子仓库（目录里另一个独立的 git 仓库，内容没法用影子仓库记录）。"""
        f = self._git_dir(folder) / "info" / "exclude"
        if not f.exists():
            return []
        lines = f.read_text(encoding="utf-8").splitlines()
        return [lines[i + 1].strip("/") for i, line in enumerate(lines[:-1]) if line == self.NESTED_MARK]

    def _exclude_nested(self, folder: str, paths: list[str]) -> None:
        known = set(self.nested_repos(folder))
        new = [p.strip("/") for p in paths if p.strip("/") and p.strip("/") not in known]
        if new:
            with open(self._git_dir(folder) / "info" / "exclude", "a", encoding="utf-8") as f:
                for p in new:
                    f.write(f"{self.NESTED_MARK}\n/{p}/\n")  # 注释单独一行（exclude 不支持行尾注释）

    def _add_all(self, folder: str) -> None:
        for _ in range(50):
            r = self._git(folder, "add", "-A", ".")
            if r.ok:
                break
            bad = re.findall(r"'([^']+)' does not have a commit checked out", r.output)
            if not bad:
                self._check(r, "记录文件")
            self._exclude_nested(folder, bad)
        else:
            raise SnapshotError("记录文件失败：子仓库太多")
        # 已有提交的子仓库会被记成 gitlink（只有一个提交号），同样跳过
        staged = self._check(self._git(folder, "ls-files", "-s"), "读取文件列表")
        links = [line.split("\t", 1)[1] for line in staged.splitlines() if line.startswith("160000 ") and "\t" in line]
        if links:
            self._check(self._git(folder, "update-index", "--force-remove", "--", *links), "跳过子仓库")
            self._exclude_nested(folder, links)

    def take(self, folder: str, message: str = "snapshot") -> str:
        """把目录当前状态存成一个提交，返回提交号。"""
        self._ensure(folder)
        self._add_all(folder)
        self._check(self._git(folder, "commit", "-q", "--allow-empty", "--no-verify", "-m", message), "保存快照")
        return self._check(self._git(folder, "rev-parse", "HEAD"), "读取快照")

    def diff(self, folder: str, a: str, b: str) -> list[tuple[str, str]]:
        """两个快照之间变了哪些文件：[(A/M/D, 相对路径)]。"""
        out = self._check(self._git(folder, "diff", "--name-status", "--no-renames", a, b), "比较文件")
        changes = []
        for line in out.splitlines():
            status, _, path = line.partition("\t")
            if path:
                changes.append((status[:1], path))
        return changes

    def restore(self, folder: str, snapshot: str) -> tuple[list[str], str]:
        """把目录恢复成快照时的样子（包括删除之后新建的文件）。返回（变动的文件，恢复前的安全快照）。"""
        self._ensure(folder)
        self._check(self._git(folder, "cat-file", "-e", f"{snapshot}^{{commit}}"), "查找快照")
        backup = self.take(folder, f"before restore to {snapshot[:10]}")
        diff = self._check(self._git(folder, "diff", "--name-status", "--no-renames", snapshot, backup), "比较文件")
        changed: list[str] = []
        for line in diff.splitlines():
            status, _, path = line.partition("\t")
            if not path:
                continue
            changed.append(path)
            if status.startswith("A"):  # 快照之后新建的文件：删掉
                full = Path(folder) / path
                try:
                    full.unlink()
                except FileNotFoundError:
                    pass
        if any(not line.startswith("A") for line in diff.splitlines() if line):
            self._check(self._git(folder, "checkout", snapshot, "--", "."), "恢复文件")
        self._remove_empty_dirs(folder, [p for p in changed])
        return changed, backup

    @staticmethod
    def _remove_empty_dirs(folder: str, paths: list[str]) -> None:
        base = Path(folder).resolve()
        for p in paths:
            d = (base / p).parent
            while d != base and base in d.parents:
                try:
                    d.rmdir()
                except OSError:
                    break
                d = d.parent
