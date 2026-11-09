import subprocess

from bridge_agent.snapshots import Snapshots


def test_restore_reverts_edits_deletes_and_new_files(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.txt").write_text("v1", encoding="utf-8")
    (proj / "删除我.txt").write_text("keep", encoding="utf-8")
    (proj / ".gitignore").write_text("secret.env\n", encoding="utf-8")
    (proj / "secret.env").write_text("KEY=1", encoding="utf-8")
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "big.js").write_text("x", encoding="utf-8")
    snaps = Snapshots(tmp_path / "shadow")

    s1 = snaps.take(str(proj), "turn 1")
    # Claude 改了文件、删了文件、新建了带目录的文件
    (proj / "src" / "a.txt").write_text("v2", encoding="utf-8")
    (proj / "删除我.txt").unlink()
    (proj / "new" / "deep").mkdir(parents=True)
    (proj / "new" / "deep" / "b.txt").write_text("new", encoding="utf-8")
    (proj / "secret.env").write_text("KEY=2", encoding="utf-8")  # 被忽略的文件不受影响

    changed, backup = snaps.restore(str(proj), s1)
    assert (proj / "src" / "a.txt").read_text(encoding="utf-8") == "v1"
    assert (proj / "删除我.txt").read_text(encoding="utf-8") == "keep"
    assert not (proj / "new").exists()
    assert (proj / "secret.env").read_text(encoding="utf-8") == "KEY=2"
    assert (proj / "node_modules" / "big.js").exists()
    assert set(changed) == {"src/a.txt", "删除我.txt", "new/deep/b.txt"}

    # 回退本身也能撤销：恢复到回退前的安全快照
    snaps.restore(str(proj), backup)
    assert (proj / "src" / "a.txt").read_text(encoding="utf-8") == "v2"
    assert (proj / "new" / "deep" / "b.txt").exists()


def test_nested_repos_are_skipped_not_fatal(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("1", encoding="utf-8")
    snaps = Snapshots(tmp_path / "shadow")
    s1 = snaps.take(str(proj))
    # Claude 新建了一个空仓库和一个有提交的仓库
    subprocess.run(["git", "init", "-q", "empty"], cwd=proj, check=True)
    (proj / "full").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj / "full", check=True)
    (proj / "full" / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=proj / "full", check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"], cwd=proj / "full", check=True)
    (proj / "a.txt").write_text("2", encoding="utf-8")

    changed, _ = snaps.restore(str(proj), s1)
    assert (proj / "a.txt").read_text(encoding="utf-8") == "1"
    assert changed == ["a.txt"]
    assert set(snaps.nested_repos(str(proj))) == {"empty", "full"}
    assert (proj / "full" / "f.txt").exists()  # 子仓库原样保留，不会被删
    snaps.take(str(proj))  # 之后的快照也正常


def test_project_git_untouched(tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    (proj / "a.txt").write_text("1", encoding="utf-8")
    snaps = Snapshots(tmp_path / "shadow")
    s = snaps.take(str(proj))
    (proj / "a.txt").write_text("2", encoding="utf-8")
    snaps.restore(str(proj), s)
    log = subprocess.run(["git", "log", "--oneline"], cwd=proj, capture_output=True, text=True)
    assert log.stdout == ""  # 项目自己的仓库没有任何提交
    status = subprocess.run(["git", "status", "--porcelain"], cwd=proj, capture_output=True, text=True).stdout
    assert "a.txt" in status  # 只是普通的未跟踪文件
