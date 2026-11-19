"""bngsim_docker.py: the commands it would run and how it reads a run, with no
Docker needed (pure functions and --dry-run)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bngsim_docker as bd  # noqa: E402


def test_image_names_are_the_ones_linux_check_expects():
    # bngsim-preflights' linux_check.py runs bngsim-linux-amd64:latest.
    assert bd.image_name("amd64") == "bngsim-linux-amd64:latest"
    assert bd.image_name("arm64") == "bngsim-linux-arm64:latest"
    assert bd.image_name("amd64", "bionetgen") == "bngsim-linux-bionetgen:latest"
    assert bd.image_name("amd64", "ubuntu") == "bngsim-linux-ubuntu:latest"
    assert bd.image_name("arm64", "ubuntu") == "bngsim-linux-ubuntu-arm64:latest"


def test_build_is_the_base_then_bionetgen_on_it():
    (base,) = bd.build_commands("arm64", "debian")
    assert base[:4] == ["docker", "build", "--platform", "linux/arm64"]
    assert "bngsim-linux-arm64:latest" in base and base[-1].endswith("images")
    first, second = bd.build_commands("amd64", "bionetgen")
    assert "bngsim-linux-amd64:latest" in first
    assert "bngsim-linux-bionetgen:latest" in second
    assert second[second.index("-f") + 1].endswith("Dockerfile.bionetgen")
    assert second[second.index("--build-arg") + 1] == "BASE=bngsim-linux-amd64:latest"


def test_the_ubuntu_image_is_its_own_build():
    (ubuntu,) = bd.build_commands("amd64", "ubuntu")
    assert ubuntu[ubuntu.index("-f") + 1].endswith("Dockerfile.ubuntu")
    assert "bngsim-linux-ubuntu:latest" in ubuntu


def test_bionetgen_on_arm64_is_refused():
    """BioNetGen's Linux release is x86-64 only."""
    with pytest.raises(SystemExit, match="x86-64 only"):
        bd.build_commands("arm64", "bionetgen")


def test_the_container_runs_the_shared_script():
    """One script for the CLI and compose.yaml, so the two cannot drift."""
    assert bd.container_script([], 3) == "WORKERS=3 BUILD_JOBS=2 bash /scripts/container-test.sh"
    script = bd.container_script(["python/tests/test_a.py", "python/tests/test_b.py::t[x y]"], 5, 1)
    assert script == ("WORKERS=5 BUILD_JOBS=1 bash /scripts/container-test.sh "
                      "python/tests/test_a.py 'python/tests/test_b.py::t[x y]'")


def test_the_run_mounts_the_copy_the_logs_the_scripts_and_the_cache():
    cmd = bd.run_command(Path("/tmp/copy"), Path("/tmp/out"), "img:latest", "amd64", "S", "10g",
                         "bngsim-ccache-debian-amd64")
    assert cmd[:7] == ["docker", "run", "--rm", "--platform", "linux/amd64", "--memory", "10g"]
    mounts = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]
    assert mounts == ["/tmp/copy:/src", "/tmp/out:/out", f"{ROOT / 'scripts'}:/scripts:ro",
                      "bngsim-ccache-debian-amd64:/ccache"]
    # The caller's ids, for the container to hand its files back to (on a Linux
    # host, root's .venv in the copy could not be deleted: CI's first run).
    env = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-e"]
    assert env == [f"HOST_UID={os.getuid()}", f"HOST_GID={os.getgid()}"]
    assert cmd[-4:] == ["img:latest", "bash", "-c", "S"]
    no_cache = bd.run_command(Path("/tmp/copy"), Path("/tmp/out"), "img:latest", "amd64", "S", "10g")
    assert not any(m.endswith(":/ccache") for m in no_cache)


def test_each_image_has_its_own_cache():
    """The images' compilers differ, so their objects must not mix."""
    names = {bd.ccache_volume(arch, kind) for arch in ("amd64", "arm64") for kind in bd.KINDS}
    assert len(names) == 6


PYTEST_OUTPUT = """\
FAILED python/tests/test_a.py::test_one - AssertionError
FAILED python/tests/test_a.py::test_two[x] - ValueError
ERROR python/tests/test_b.py::test_three - fixture
FAILED python/tests/test_a.py::test_one - AssertionError
= 3 failed, 10 passed, 2 skipped in 4.20s =
"""


def test_a_run_s_summary_and_failures_are_read():
    run = bd.parse(PYTEST_OUTPUT)
    assert run.summary == "3 failed, 10 passed, 2 skipped in 4.20s"
    assert run.failed == ["python/tests/test_a.py::test_one", "python/tests/test_a.py::test_two[x]",
                          "python/tests/test_b.py::test_three"]


def test_a_run_with_no_summary_reads_as_unfinished():
    assert bd.parse("collecting ...\nKilled\n").summary == ""


def test_standing_failures_are_told_from_the_tree_s(tmp_path):
    known = tmp_path / "known.txt"
    known.write_text("# a comment line\npython/tests/test_a.py::test_two  # the container's\n\n")
    standing, new = bd.sort_failures(
        ["python/tests/test_a.py::test_two[x]", "python/tests/test_a.py::test_two_more",
         "python/tests/test_a.py::test_two"],
        bd.read_known(known),
    )
    # A parametrized case of a known test is known; a test whose name only
    # starts the same way is not.
    assert standing == ["python/tests/test_a.py::test_two[x]", "python/tests/test_a.py::test_two"]
    assert new == ["python/tests/test_a.py::test_two_more"]


def test_every_known_failure_says_why():
    for line in bd.KNOWN.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            test, sep, why = line.partition("#")
            assert test.strip().startswith("python/tests/test_") and "::" in test, line
            assert sep and why.strip(), f"no reason given: {line}"


def test_the_copy_leaves_out_the_host_s_build(tmp_path):
    src = tmp_path / "tree"
    for rel in ("python/bngsim/__init__.py", ".venv/bin/python", "build/x.o", ".git/HEAD",
                "python/bngsim/_bngsim_core.so", "run.log", "python/tests/__pycache__/t.pyc"):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text("x")
    bd.copy_tree(src, tmp_path / "copy", dry=False)
    kept = sorted(str(p.relative_to(tmp_path / "copy")) for p in (tmp_path / "copy").rglob("*") if p.is_file())
    assert kept == ["python/bngsim/__init__.py"]


def test_test_dry_run_prints_the_probe_the_copy_and_the_run(capsys, tmp_path):
    rc = bd.main(["test", "--tree", str(tmp_path), "--tests", "python/tests/test_a.py",
                  "--out", str(tmp_path / "out"), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.startswith("+ ")]
    assert lines[0].startswith("+ docker run --rm --platform linux/amd64 bngsim-linux-amd64:latest uname -m")
    assert lines[1].startswith(f"+ copy {tmp_path}")
    assert "container-test.sh python/tests/test_a.py" in lines[2]
    assert not (tmp_path / "out").exists()  # a dry run writes nothing


def test_test_refuses_a_tree_that_is_not_bngsim(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, "probe", lambda *a, **k: True)
    with pytest.raises(SystemExit, match="not a bngsim tree"):
        bd.main(["test", "--tree", str(tmp_path)])


def test_doctor_is_macos_only(monkeypatch):
    monkeypatch.setattr(bd.host_platform, "system", lambda: "Linux")
    with pytest.raises(SystemExit, match="macOS"):
        bd.restart_docker_desktop(dry=True)


def test_a_dry_run_mounts_the_cache_unless_told_not_to(capsys, tmp_path):
    bd.main(["test", "--tree", str(tmp_path), "--kind", "ubuntu", "--dry-run"])
    run = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("+ docker run") and "/src" in ln][0]
    assert "bngsim-ccache-ubuntu-amd64:/ccache" in run and "bngsim-linux-ubuntu:latest" in run
    bd.main(["test", "--tree", str(tmp_path), "--no-ccache", "--dry-run"])
    run = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("+ docker run") and "/src" in ln][0]
    assert "/ccache" not in run


# ── compare ─────────────────────────────────────────────────────────────────


def test_failures_main_has_too_are_told_from_the_branch_s():
    both, only = bd.compare_failures(["t::a", "t::b[x]", "t::c"], ["t::b[x]"])
    assert both == ["t::b[x]"] and only == ["t::a", "t::c"]


def _compare(monkeypatch, tmp_path, branch: bd.Run, main: bd.Run | None):
    """compare, with the two runs stood in for and what main was asked to run recorded."""
    asked = []

    def run_tree(tree, out, tests, w):
        out.mkdir(parents=True, exist_ok=True)
        if out.name == "branch":
            return branch
        asked.append(tests)
        return main

    monkeypatch.setattr(bd, "probe", lambda *a, **k: True)
    monkeypatch.setattr(bd, "run_tree", run_tree)
    rc = bd.main(["compare", "--tree", str(tmp_path), "--main", str(tmp_path), "--out", str(tmp_path / "o")])
    return rc, asked, (tmp_path / "o" / "docker.log").read_text()


def test_compare_reruns_only_the_branch_s_failures_on_main(monkeypatch, tmp_path):
    rc, asked, log = _compare(monkeypatch, tmp_path, bd.Run("2 failed, 9 passed", ["t::a", "t::b"]),
                              bd.Run("1 failed, 1 passed", ["t::b"]))
    assert asked == [["t::a", "t::b"]]
    assert "BRANCH ONLY: t::a" in log and "fails on main too: t::b" in log
    assert rc == 1


def test_compare_passes_when_every_failure_fails_on_main_too(monkeypatch, tmp_path):
    rc, _, log = _compare(monkeypatch, tmp_path, bd.Run("1 failed, 9 passed", ["t::a"]),
                          bd.Run("1 failed", ["t::a"]))
    assert rc == 0 and "every failure fails on main too" in log


def test_compare_does_not_run_main_when_nothing_fails(monkeypatch, tmp_path):
    rc, asked, log = _compare(monkeypatch, tmp_path, bd.Run("9 passed"), None)
    assert rc == 0 and asked == [] and "nothing fails on the branch" in log


@pytest.mark.parametrize("which", ["branch", "main"])
def test_compare_fails_when_a_run_does_not_finish(monkeypatch, tmp_path, which):
    branch = bd.Run("" if which == "branch" else "1 failed", [] if which == "branch" else ["t::a"])
    rc, _, log = _compare(monkeypatch, tmp_path, branch, bd.Run(""))
    assert rc == 1 and "did not finish" in log


def test_the_container_hands_its_files_back():
    script = (ROOT / "scripts" / "container-test.sh").read_text(encoding="utf-8")
    assert "trap 'chown -R \"$HOST_UID:$HOST_GID\" /src /out" in script


def test_the_cache_s_count_is_read_from_the_build_log(tmp_path):
    assert bd.cache_line(tmp_path) is None
    (tmp_path / "build.log").write_text("Building\nccache: 0 of 212 compiles from the cache\nBuilt\n")
    assert bd.cache_line(tmp_path) == "ccache: 0 of 212 compiles from the cache"


# ── clean ───────────────────────────────────────────────────────────────────

DF = ('[{"Name": "bngsim-ccache-debian-amd64", "Size": "38MB"}, {"Name": "bngsim-ccache-mine", "Size": "1GB"},'
      ' {"Name": "bngsim-ccache-ubuntu-amd64", "Size": "33MB"}, {"Name": "pgdata", "Size": "2GB"}]')
IMAGES_OUT = ("bngsim-linux-amd64|latest|1.16GB\nbngsim-linux-bionetgen|candidate|1.17GB\n"
              "bngsim-linux-amd64|<none>|1.1GB\nbngsim-linux-other|latest|5MB\npostgres|16|400MB\n")


def _clean(monkeypatch, capsys, *args, df=DF, images=IMAGES_OUT, rm_rc=0):
    """clean, with docker's listings stood in for and every rm recorded."""
    removed = []
    monkeypatch.setattr(bd, "docker_out", lambda cmd: df if "df" in cmd else images)

    def sh(cmd, *, dry, **k):
        print("+ " + " ".join(cmd))
        if not dry:
            removed.append(cmd)
        return bd.subprocess.CompletedProcess(cmd, rm_rc, "", "in use" if rm_rc else "")

    monkeypatch.setattr(bd, "sh", sh)
    rc = bd.main(["clean", *args])
    return rc, removed, capsys.readouterr().out


def test_every_name_clean_may_remove_is_one_the_tool_makes():
    volumes, repos = bd.made_here()
    assert volumes == {"bngsim-ccache-debian-amd64", "bngsim-ccache-debian-arm64", "bngsim-ccache-ubuntu-amd64",
                       "bngsim-ccache-ubuntu-arm64", "bngsim-ccache-bionetgen-amd64"}
    assert repos == {"bngsim-linux-amd64", "bngsim-linux-arm64", "bngsim-linux-ubuntu",
                     "bngsim-linux-ubuntu-arm64", "bngsim-linux-bionetgen"}
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert all(v in volumes for v in re.findall(r"name: (bngsim-ccache-\S+)", compose))


def test_clean_removes_the_caches_and_keeps_the_images(monkeypatch, capsys):
    rc, removed, out = _clean(monkeypatch, capsys)
    assert rc == 0
    assert removed == [["docker", "volume", "rm", "bngsim-ccache-debian-amd64", "bngsim-ccache-ubuntu-amd64"]]
    assert "bngsim-ccache-mine" not in out and "pgdata" not in out and "postgres" not in out
    assert "bngsim-linux-other" not in out and "<none>" not in out
    assert "bngsim-linux-amd64:latest  1.16GB  (kept; --images removes it)" in out


def test_clean_images_removes_bionetgen_before_the_image_it_is_built_on(monkeypatch, capsys):
    rc, removed, _ = _clean(monkeypatch, capsys, "--images")
    assert rc == 0
    assert removed[1] == ["docker", "image", "rm", "bngsim-linux-bionetgen:candidate", "bngsim-linux-amd64:latest"]


def test_clean_dry_run_lists_and_removes_nothing(monkeypatch, capsys):
    rc, removed, out = _clean(monkeypatch, capsys, "--images", "--dry-run")
    assert rc == 0 and removed == []
    assert "+ docker volume rm" in out and "+ docker image rm" in out


def test_clean_with_nothing_there_says_so(monkeypatch, capsys):
    rc, removed, out = _clean(monkeypatch, capsys, df="null", images="postgres|16|400MB\n")
    assert rc == 0 and removed == [] and "nothing of bngsim-docker's to remove" in out


def test_clean_fails_when_docker_refuses(monkeypatch, capsys):
    rc, _, _ = _clean(monkeypatch, capsys, rm_rc=1)
    assert rc == 1
