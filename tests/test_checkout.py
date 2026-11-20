"""--pr and --merge-main (checked against real git repositories: a stand-in for
GitHub with a main and a pull request's ref, and a clone of it as the tree),
and the bng2 command (its docker command line, and a dry run)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bngsim_docker as bd  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "init.defaultBranch=main", *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", *files)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path):
    """(the remote, the tree): the remote's main holds count.py = 29 and a
    pull request 7 that adds pr.txt; the tree is a clone on a branch."""
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-q")
    commit(remote, {"count.py": "COUNT = 28\n", "python/bngsim/__init__.py": ""}, "base")
    git(remote, "checkout", "-q", "-b", "pr7")
    commit(remote, {"pr.txt": "from the pull request\n"}, "pr")
    git(remote, "update-ref", "refs/pull/7/head", "HEAD")
    git(remote, "checkout", "-q", "main")
    tree = tmp_path / "tree"
    git(tmp_path, "clone", "-q", str(remote), str(tree))
    git(tree, "checkout", "-q", "-b", "mine")
    commit(tree, {"mine.txt": "the branch's\n"}, "mine")
    commit(remote, {"count.py": "COUNT = 29\n", "main.txt": "main's\n"}, "main moves")
    return remote, tree


def worktrees(tree: Path) -> int:
    return git(tree, "worktree", "list").count("\n") + 1


def test_without_either_flag_the_tree_is_the_tree(tmp_path):
    with bd.prepared_tree(tmp_path) as t:
        assert t == tmp_path


def test_merge_main_tests_the_commit_merged_with_main(repos, capsys):
    remote, tree = repos
    (tree / "mine.txt").write_text("an uncommitted edit\n")
    with bd.prepared_tree(tree, merge_main=True) as t:
        assert t != tree
        assert (t / "count.py").read_text() == "COUNT = 29\n"  # main's
        assert (t / "main.txt").is_file() and (t / "mine.txt").read_text() == "the branch's\n"
        tracked = git(t, "ls-files").split()
        assert "main.txt" in tracked  # the copy commits main's new files too
        assert git(t, "rev-parse", "HEAD") == git(tree, "rev-parse", "HEAD")
    err = capsys.readouterr().err
    assert "uncommitted changes" in err
    assert not t.exists() and worktrees(tree) == 1
    assert (tree / "mine.txt").read_text() == "an uncommitted edit\n"  # the tree is untouched
    assert git(tree, "rev-parse", "--abbrev-ref", "HEAD") == "mine"


def test_pr_tests_the_pull_request_s_head(repos):
    remote, tree = repos
    with bd.prepared_tree(tree, pr=7) as t:
        assert (t / "pr.txt").is_file()
        assert not (t / "mine.txt").exists() and not (t / "main.txt").exists()
        assert git(t, "rev-parse", "HEAD") == git(remote, "rev-parse", "refs/pull/7/head")
    assert worktrees(tree) == 1


def test_pr_and_merge_main_together(repos):
    _, tree = repos
    with bd.prepared_tree(tree, pr=7, merge_main=True) as t:
        assert (t / "pr.txt").is_file() and (t / "main.txt").is_file()
    assert worktrees(tree) == 1


def test_a_merge_that_conflicts_stops_and_names_the_files(repos):
    _, tree = repos
    commit(tree, {"count.py": "COUNT = 30\n"}, "the branch's count")
    with pytest.raises(SystemExit, match=r"does not merge with origin's main: count\.py"):
        with bd.prepared_tree(tree, merge_main=True):
            pytest.fail("a conflicted merge must not be tested")
    assert worktrees(tree) == 1
    assert (tree / "count.py").read_text() == "COUNT = 30\n"


def test_the_worktree_is_removed_when_the_run_fails(repos):
    _, tree = repos
    with pytest.raises(RuntimeError):
        with bd.prepared_tree(tree, merge_main=True) as t:
            raise RuntimeError("the run failed")
    assert not t.exists() and worktrees(tree) == 1


def test_a_pull_request_that_is_not_there_is_named(repos):
    _, tree = repos
    with pytest.raises(SystemExit, match="cannot fetch pull request 99"):
        with bd.prepared_tree(tree, pr=99):
            pass


def test_the_fetches_leave_the_tree_s_own_refs_alone(repos):
    _, tree = repos
    before = git(tree, "for-each-ref", "refs/heads", "refs/remotes")
    with bd.prepared_tree(tree, pr=7, merge_main=True):
        pass
    assert git(tree, "for-each-ref", "refs/heads", "refs/remotes") == before


def test_not_a_checkout_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="need a git checkout"):
        with bd.prepared_tree(tmp_path, merge_main=True):
            pass


def test_the_copy_of_a_merged_tree_carries_main_s_files_as_tracked(repos, tmp_path):
    _, tree = repos
    with bd.prepared_tree(tree, merge_main=True) as t:
        bd.copy_tree(t, tmp_path / "copy", dry=False)
    listed = (tmp_path / "copy" / bd.TRACKED).read_text().split("\0")
    assert "main.txt" in listed and "mine.txt" in listed
    assert not (tmp_path / "copy" / ".git").exists()


def test_test_dry_run_with_pr_and_merge_main_prints_the_fetches(capsys, tmp_path):
    rc = bd.main(["test", "--tree", str(tmp_path), "--pr", "1774", "--merge-main", "--remote", "upstream",
                  "--out", str(tmp_path / "out"), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fetch --quiet --refmap= upstream +refs/pull/1774/head:refs/bngsim-docker/pr/1774" in out
    assert "fetch --quiet --refmap= upstream +refs/heads/main:refs/bngsim-docker/main" in out
    assert "merge --quiet --no-commit --no-ff refs/bngsim-docker/main" in out
    assert out.index("worktree add") < out.index("+ copy") < out.index("worktree remove")


def test_bng2_mounts_the_model_read_only_and_writes_to_out():
    cmd = bd.bng2_command(Path("/m/dir/model one.bngl"), Path("/o"), "bngsim-linux-bionetgen:latest",
                          ["--log"])
    assert cmd[:6] == ["docker", "run", "--rm", "--platform", "linux/amd64", *cmd[5:6]]
    assert "/m/dir:/model:ro" in cmd and "/o:/out" in cmd
    script = cmd[-1]
    assert "--outdir /out --log '/model/model one.bngl'" in script
    assert "tee '/out/model one.bng2.log'" in script
    assert script.endswith("exit $rc") and "PIPESTATUS[0]" in script


def test_bng2_dry_run(capsys, tmp_path):
    rc = bd.main(["bng2", str(tmp_path / "m.bngl"), "--out", str(tmp_path / "o"), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "bngsim-linux-bionetgen:latest uname -m" in out  # the probe
    assert f"{tmp_path}:/model:ro" in out
    assert not (tmp_path / "o").exists()


def test_bng2_refuses_a_model_that_is_not_there(tmp_path):
    with pytest.raises(SystemExit, match="no model at"):
        bd.main(["bng2", str(tmp_path / "missing.bngl")])
