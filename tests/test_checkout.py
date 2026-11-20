"""--pr and --merge-main (checked against real git repositories: a stand-in for
GitHub with a main and a pull request's ref, and a clone of it as the tree),
and the bng2 command (its docker command line, and a dry run)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bngsim_docker as bd  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


IDENTITY = {f"GIT_{who}_{what}": value for who in ("AUTHOR", "COMMITTER")
            for what, value in (("NAME", "t"), ("EMAIL", "t@example.invalid"))}


def git(cwd: Path, *args: str) -> str:
    """git for the fixtures, with its own identity (the tool's runs have none)."""
    r = subprocess.run(["git", "-C", str(cwd), "-c", "init.defaultBranch=main", *args],
                       capture_output=True, text=True, env={**os.environ, **IDENTITY})
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


def test_merge_main_needs_no_identity(repos, monkeypatch):
    """CI's runners have no git name set and none to guess: git refuses a merge,
    even one never committed, unless it is given one."""
    _, tree = repos
    for k in IDENTITY:
        monkeypatch.setenv(k, "")
    with bd.prepared_tree(tree, merge_main=True) as t:
        assert (t / "main.txt").is_file()


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
    with pytest.raises(SystemExit, match="need a git checkout: .* is no git checkout"):
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


def test_bng2_mounts_the_root_read_only_and_runs_in_the_model_s_directory():
    cmd = bd.bng2_command(Path("/m/dir/sub/model one.bngl"), Path("/m/dir"), Path("/o"),
                          "bngsim-linux-bionetgen:latest", ["--log"])
    assert cmd[:5] == ["docker", "run", "--rm", "--platform", "linux/amd64"]
    assert "/m/dir:/model:ro" in cmd and "/o:/out" in cmd
    script = cmd[-1]
    # BNG2.pl reads a relative path from where it runs: the model's own directory.
    assert script.startswith("cd /model/sub && ")
    assert "--outdir /out --log 'model one.bngl'" in script
    assert "tee '/out/model one.bng2.log'" in script
    assert script.endswith("exit $rc") and "PIPESTATUS[0]" in script


def test_bng2_mounts_the_model_s_checkout_or_else_its_directory(repos, tmp_path):
    _, tree = repos
    (tree / "models" / "sub").mkdir(parents=True)
    model = tree / "models" / "sub" / "m.bngl"
    model.write_text("")
    assert bd.bng2_root(model, None) == tree.resolve()
    assert bd.bng2_root(model, tree / "models") == (tree / "models").resolve()
    with pytest.raises(SystemExit, match="does not hold the model"):
        bd.bng2_root(model, tmp_path / "remote")
    loose = tmp_path / "loose" / "m.bngl"
    loose.parent.mkdir()
    loose.write_text("")
    assert bd.bng2_root(loose.resolve(), None) == loose.parent.resolve()


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


# ── --uncommitted ───────────────────────────────────────────────────────────


def test_uncommitted_carries_edits_new_files_and_deletions_onto_the_merge(repos):
    _, tree = repos
    (tree / "mine.txt").write_text("edited, not committed\n")
    (tree / "python" / "tests").mkdir(parents=True)
    (tree / "python" / "tests" / "test_new.py").write_text("def test_x(): pass\n")  # never added
    (tree / "count.py").unlink()  # main changed it too: a deletion of a file main edited
    git(tree, "rm", "-q", "--cached", "count.py")
    (tree / "ignored.log").write_text("x")
    (tree / ".gitignore").write_text("*.log\n")
    before = git(tree, "status", "--porcelain")
    with pytest.raises(SystemExit, match="do not apply"):
        with bd.prepared_tree(tree, merge_main=True, uncommitted=True):
            pass
    git(tree, "reset", "-q", "HEAD", "count.py")
    git(tree, "checkout", "-q", "count.py")
    before = git(tree, "status", "--porcelain")
    with bd.prepared_tree(tree, merge_main=True, uncommitted=True) as t:
        assert (t / "mine.txt").read_text() == "edited, not committed\n"
        assert (t / "python" / "tests" / "test_new.py").is_file()
        assert (t / "main.txt").is_file() and (t / "count.py").read_text() == "COUNT = 29\n"
        assert not (t / "ignored.log").exists()
        tracked = git(t, "ls-files").split()
        assert "python/tests/test_new.py" in tracked and ".gitignore" in tracked
    assert git(tree, "status", "--porcelain") == before  # the tree's index is not touched
    assert worktrees(tree) == 1


def test_uncommitted_deletion_is_carried(repos):
    _, tree = repos
    (tree / "mine.txt").unlink()
    with bd.prepared_tree(tree, merge_main=True, uncommitted=True) as t:
        assert not (t / "mine.txt").exists()
        assert "mine.txt" not in git(t, "ls-files").split()


def test_uncommitted_is_not_for_a_pull_request(repos):
    _, tree = repos
    with pytest.raises(SystemExit, match="not with --pr"):
        with bd.prepared_tree(tree, pr=7, uncommitted=True):
            pass


def test_the_warning_names_untracked_files_and_the_flag(repos, capsys):
    _, tree = repos
    (tree / "new_test.py").write_text("")
    with bd.prepared_tree(tree, merge_main=True):
        pass
    assert "--uncommitted carries them in" in capsys.readouterr().err


# ── compare's main ──────────────────────────────────────────────────────────


def _args(tree, main=None, merge_main=True):
    import argparse
    return argparse.Namespace(tree=tree, main=main, merge_main=merge_main, remote="origin")


def test_compare_reruns_on_the_very_main_the_branch_was_merged_with(repos):
    remote, tree = repos
    with bd.prepared_tree(tree, merge_main=True):
        pass
    merged = git(tree, "rev-parse", "refs/bngsim-docker/main")
    commit(remote, {"later.txt": "main moved during the run\n"}, "later")
    with bd.main_tree(_args(tree), dry=False) as (m, warn):
        assert git(m, "rev-parse", "HEAD") == merged and not (m / "later.txt").exists()
        assert warn == ""
    assert worktrees(tree) == 1


def test_compare_without_merge_main_fetches_main_now(repos):
    remote, tree = repos
    with bd.main_tree(_args(tree, merge_main=False), dry=False) as (m, _):
        assert git(m, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")


def test_a_main_checkout_behind_the_merged_main_is_warned_of(repos, tmp_path):
    remote, tree = repos
    old = tmp_path / "old-main"
    git(tmp_path, "clone", "-q", str(remote), str(old))
    commit(remote, {"later.txt": "x\n"}, "later")
    with bd.prepared_tree(tree, merge_main=True):
        pass
    with bd.main_tree(_args(tree, main=old), dry=False) as (m, warn):
        assert m == old and "reads as BRANCH ONLY" in warn
    git(old, "pull", "-q")
    with bd.main_tree(_args(tree, main=old), dry=False) as (_, warn):
        assert warn == ""


# ── failures that are only antimony missing (the arm64 images) ──────────────

JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite>
<testcase classname="python.tests.test_a" name="test_x[1]"><failure message="ImportError: The 'antimony' package is required to load Antimony models.">tb</failure></testcase>
<testcase classname="python.tests.test_a.TestB" name="test_y"><error message="ModuleNotFoundError: No module named 'antimony'">tb</error></testcase>
<testcase classname="python.tests.test_a" name="test_z"><failure message="AssertionError: assert 30 == 29">tb</failure></testcase>
<testcase classname="python.tests.test_a" name="test_ok"/>
</testsuite></testsuites>"""
IDS = ["python/tests/test_a.py::test_x[1]", "python/tests/test_a.py::TestB::test_y", "python/tests/test_a.py::test_z"]


def test_a_pytest_id_is_its_junit_key():
    assert bd.junit_key("python/tests/test_a.py::TestB::test_c[x]") == ("python.tests.test_a.TestB", "test_c[x]")
    assert bd.junit_key("python/tests/test_a.py::test_c") == ("python.tests.test_a", "test_c")


def test_only_antimony_missing_is_sorted_out(tmp_path):
    (tmp_path / "junit.xml").write_text(JUNIT)
    by_key = bd.junit_messages(tmp_path / "junit.xml")
    messages = {t: by_key[bd.junit_key(t)] for t in IDS}
    missing, rest = bd.needs_antimony(IDS, messages)
    assert missing == IDS[:2] and rest == IDS[2:]
    assert bd.junit_messages(tmp_path / "none.xml") == {}


def _test_on(monkeypatch, tmp_path, arch):
    (tmp_path / "junit.xml").write_text(JUNIT)
    run = bd.Run("3 failed, 1 passed", failed=IDS[:2],
                 messages={t: bd.junit_messages(tmp_path / "junit.xml")[bd.junit_key(t)] for t in IDS[:2]})
    monkeypatch.setattr(bd, "probe", lambda *a, **k: True)
    monkeypatch.setattr(bd, "stale_image", lambda *a, **k: [])
    monkeypatch.setattr(bd, "daemon_arch", lambda: arch)
    monkeypatch.setattr(bd, "run_tree", lambda *a, **k: run)
    (tmp_path / "k.txt").write_text("")
    out = tmp_path / "o"
    out.mkdir()
    rc = bd.main(["test", "--tree", str(tmp_path), "--arch", arch, "--out", str(out), "--known", str(tmp_path / "k.txt")])
    return rc, (out / "docker.log").read_text()


def test_on_arm64_antimony_missing_is_the_image_s(monkeypatch, tmp_path):
    rc, log = _test_on(monkeypatch, tmp_path, "arm64")
    assert rc == 0 and "2 failure(s) only because the arm64 image has no antimony" in log and "NEW" not in log


def test_on_amd64_antimony_missing_is_the_tree_s(monkeypatch, tmp_path):
    rc, log = _test_on(monkeypatch, tmp_path, "amd64")
    assert rc == 1 and log.count("NEW: ") == 2


def test_shell_passes_uncommitted_on(monkeypatch, tmp_path):
    seen = {}

    @bd.contextlib.contextmanager
    def prepared(tree, **k):
        seen.update(k)
        yield tree

    monkeypatch.setattr(bd, "prepared_tree", prepared)
    bd.main(["shell", "--tree", str(tmp_path), "--merge-main", "--uncommitted", "--dry-run"])
    assert seen["uncommitted"] and seen["merge_main"]
