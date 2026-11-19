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
            assert test.split()[0].startswith("python/tests/test_") and "::" in test, line
            assert sep and why.strip(), f"no reason given: {line}"


def test_every_image_a_known_failure_names_is_one():
    for test, kinds in bd.known_kinds().items():
        assert set(kinds) <= {*bd.KINDS, bd.EMULATED}, (test, kinds)


KNOWN_TEXT = """t.py::everywhere  # fails in every image
t.py::somewhere @debian @bionetgen  # only where the extras are missing
"""


def test_a_known_failure_counts_only_in_the_images_it_names(tmp_path):
    path = tmp_path / "known.txt"
    path.write_text(KNOWN_TEXT)
    assert set(bd.read_known(path)) == {"t.py::everywhere", "t.py::somewhere"}
    assert set(bd.read_known(path, "debian")) == {"t.py::everywhere", "t.py::somewhere"}
    assert set(bd.read_known(path, "ubuntu")) == {"t.py::everywhere"}
    assert bd.read_known(path, "ubuntu")["t.py::everywhere"] == "fails in every image"
    # Where it is not named, its failure is the tree's.
    standing, new = bd.sort_failures(["t.py::somewhere"], bd.read_known(path, "ubuntu"))
    assert standing == [] and new == ["t.py::somewhere"]


def test_the_passed_ids_are_read():
    out = "PASSED t.py::a\nPASSED t.py::b[1]\nFAILED t.py::c - boom\n=== 1 failed, 2 passed in 3s ===\n"
    run = bd.parse(out)
    assert run.passed == ["t.py::a", "t.py::b[1]"] and run.failed == ["t.py::c"]
    assert run.summary == "1 failed, 2 passed in 3s"


def test_a_standing_failure_that_passed_is_stale():
    known = {"t.py::a": "", "t.py::b": "", "t.py::c": "", "t.py::d": ""}
    run = bd.Run("x", failed=["t.py::b[2]"], passed=["t.py::a", "t.py::b[1]", "t.py::c[1]", "t.py::cc"])
    # a passed; b passed one case but failed another; c passed its case; d did not run.
    assert bd.stale_known(run, known) == ["t.py::a", "t.py::c"]


def test_test_reports_a_stale_entry_and_still_passes(monkeypatch, tmp_path):
    (tmp_path / "k.txt").write_text(KNOWN_TEXT)
    monkeypatch.setattr(bd, "probe", lambda *a, **k: True)
    monkeypatch.setattr(bd, "stale_image", lambda *a, **k: [])
    monkeypatch.setattr(bd, "run_tree", lambda *a, **k: bd.Run("5 passed", passed=["t.py::everywhere"]))
    out = tmp_path / "o"
    out.mkdir()
    rc = bd.main(["test", "--tree", str(tmp_path), "--out", str(out), "--known", str(tmp_path / "k.txt")])
    log = (out / "docker.log").read_text()
    assert rc == 0 and "STALE: t.py::everywhere" in log and "somewhere" not in log


# ── the image older than its Dockerfiles ────────────────────────────────────


def _digest(name):
    import hashlib
    return hashlib.sha256((bd.IMAGES / name).read_bytes()).hexdigest()


def _stale(monkeypatch, kind, stdout):
    seen = []

    def sh(cmd, *, dry, **k):
        seen.append(cmd)
        return bd.subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(bd, "sh", sh)
    return bd.stale_image("img", "amd64", kind, dry=False), seen


def test_an_image_built_from_the_repository_s_dockerfile_is_current(monkeypatch):
    warnings, seen = _stale(monkeypatch, "debian", f"{_digest('Dockerfile')}  Dockerfile\n")
    assert warnings == []
    assert seen[0][:6] == ["docker", "run", "--rm", "--platform", "linux/amd64", "img"]


def test_an_image_built_from_an_older_dockerfile_is_named(monkeypatch):
    warnings, _ = _stale(monkeypatch, "ubuntu", "0" * 64 + "  Dockerfile.ubuntu\n")
    assert len(warnings) == 1 and "older images/Dockerfile.ubuntu" in warnings[0]
    assert "build --kind ubuntu --arch amd64" in warnings[0]


def test_an_image_with_no_copy_is_named(monkeypatch):
    warnings, _ = _stale(monkeypatch, "debian", "")
    assert len(warnings) == 1 and "carries no copy of images/Dockerfile" in warnings[0]


def test_bionetgen_is_checked_against_its_base_too(monkeypatch):
    current = f"{_digest('Dockerfile')}  Dockerfile\n{_digest('Dockerfile.bionetgen')}  Dockerfile.bionetgen\n"
    assert _stale(monkeypatch, "bionetgen", current)[0] == []
    old_base = f"{'0' * 64}  Dockerfile\n{_digest('Dockerfile.bionetgen')}  Dockerfile.bionetgen\n"
    warnings, _ = _stale(monkeypatch, "bionetgen", old_base)
    assert len(warnings) == 1 and "older images/Dockerfile;" in warnings[0]


def test_a_dry_run_checks_no_image(monkeypatch):
    monkeypatch.setattr(bd, "sh", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert bd.stale_image("img", "amd64", "debian", dry=True) == []


def test_a_stale_image_is_warned_of_in_the_report(monkeypatch, tmp_path):
    monkeypatch.setattr(bd, "probe", lambda *a, **k: True)
    monkeypatch.setattr(bd, "stale_image", lambda *a, **k: ["img was built from an older images/Dockerfile"])
    monkeypatch.setattr(bd, "run_tree", lambda *a, **k: bd.Run("5 passed"))
    out = tmp_path / "o"
    out.mkdir()
    assert bd.main(["test", "--tree", str(tmp_path), "--out", str(out)]) == 0
    assert "WARNING: img was built from an older images/Dockerfile" in (out / "docker.log").read_text()


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


def test_test_counts_a_failure_as_the_tree_s_in_an_image_its_entry_does_not_name(monkeypatch, tmp_path):
    (tmp_path / "k.txt").write_text(KNOWN_TEXT)
    monkeypatch.setattr(bd, "probe", lambda *a, **k: True)
    monkeypatch.setattr(bd, "stale_image", lambda *a, **k: [])
    monkeypatch.setattr(bd, "run_tree", lambda *a, **k: bd.Run("1 failed", failed=["t.py::somewhere"]))
    out = tmp_path / "o"
    out.mkdir()
    args = ["test", "--tree", str(tmp_path), "--out", str(out), "--known", str(tmp_path / "k.txt")]
    assert bd.main([*args, "--kind", "debian"]) == 0
    assert "standing (fails on main here too): t.py::somewhere" in (out / "docker.log").read_text()
    assert bd.main([*args, "--kind", "ubuntu"]) == 1
    assert "NEW: t.py::somewhere" in (out / "docker.log").read_text()



# ── emulation ───────────────────────────────────────────────────────────────

EMULATED_TEXT = """t.py::spawn @emulated  # only under emulation
t.py::extras @debian  # only in Debian
t.py::everywhere  # always
"""


def test_an_emulated_entry_counts_only_on_an_emulated_run(tmp_path):
    path = tmp_path / "k.txt"
    path.write_text(EMULATED_TEXT)
    assert set(bd.read_known(path, "debian", True)) == {"t.py::spawn", "t.py::extras", "t.py::everywhere"}
    assert set(bd.read_known(path, "debian", False)) == {"t.py::extras", "t.py::everywhere"}
    assert set(bd.read_known(path, "ubuntu", True)) == {"t.py::spawn", "t.py::everywhere"}
    # Unknown (no Docker to ask): every entry, as before the tag.
    assert "t.py::spawn" in bd.read_known(path, "debian", None)


@pytest.mark.parametrize("reported, arch", [("x86_64", "amd64"), ("aarch64", "arm64"), ("", None), ("riscv64", None)])
def test_docker_s_architecture_is_read(monkeypatch, reported, arch):
    monkeypatch.setattr(bd.subprocess, "run",
                        lambda *a, **k: bd.subprocess.CompletedProcess(a[0], 0, reported + "\n", ""))
    assert bd.daemon_arch() == arch


def test_without_docker_the_architecture_is_unknown(monkeypatch):
    def run(*a, **k):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(bd.subprocess, "run", run)
    assert bd.daemon_arch() is None


def _test_on(monkeypatch, tmp_path, native):
    (tmp_path / "k.txt").write_text(EMULATED_TEXT)
    monkeypatch.setattr(bd, "probe", lambda *a, **k: True)
    monkeypatch.setattr(bd, "stale_image", lambda *a, **k: [])
    monkeypatch.setattr(bd, "daemon_arch", lambda: native)
    monkeypatch.setattr(bd, "run_tree", lambda *a, **k: bd.Run("1 failed", failed=["t.py::spawn"]))
    out = tmp_path / "o"
    out.mkdir(exist_ok=True)
    rc = bd.main(["test", "--tree", str(tmp_path), "--out", str(out), "--known", str(tmp_path / "k.txt")])
    return rc, (out / "docker.log").read_text()


def test_an_emulated_failure_is_standing_on_an_arm_host(monkeypatch, tmp_path):
    rc, log = _test_on(monkeypatch, tmp_path, "arm64")
    assert rc == 0 and "standing (fails on main here too): t.py::spawn" in log
    assert "(emulated: amd64 on an arm64 Docker)" in log


def test_the_same_failure_on_a_native_run_is_the_tree_s(monkeypatch, tmp_path):
    rc, log = _test_on(monkeypatch, tmp_path, "amd64")
    assert rc == 1 and "NEW: t.py::spawn" in log and "emulated" not in log



def test_fail_on_stale_turns_a_passed_standing_failure_into_a_failure(monkeypatch, tmp_path):
    (tmp_path / "k.txt").write_text(KNOWN_TEXT)
    monkeypatch.setattr(bd, "probe", lambda *a, **k: True)
    monkeypatch.setattr(bd, "stale_image", lambda *a, **k: [])
    monkeypatch.setattr(bd, "daemon_arch", lambda: "amd64")
    monkeypatch.setattr(bd, "run_tree", lambda *a, **k: bd.Run("5 passed", passed=["t.py::everywhere"]))
    out = tmp_path / "o"
    out.mkdir()
    args = ["test", "--tree", str(tmp_path), "--out", str(out), "--known", str(tmp_path / "k.txt")]
    assert bd.main(args) == 0  # reported, not failed, by default
    assert bd.main([*args, "--fail-on-stale"]) == 1
    assert "out of date (--fail-on-stale)" in (out / "docker.log").read_text()


def test_fail_on_stale_passes_a_list_that_is_true(monkeypatch, tmp_path):
    (tmp_path / "k.txt").write_text(KNOWN_TEXT)
    monkeypatch.setattr(bd, "probe", lambda *a, **k: True)
    monkeypatch.setattr(bd, "stale_image", lambda *a, **k: [])
    monkeypatch.setattr(bd, "daemon_arch", lambda: "amd64")
    monkeypatch.setattr(bd, "run_tree",
                        lambda *a, **k: bd.Run("1 failed, 4 passed", failed=["t.py::everywhere"], passed=["t.py::other"]))
    out = tmp_path / "o"
    out.mkdir()
    rc = bd.main(["test", "--tree", str(tmp_path), "--out", str(out), "--known", str(tmp_path / "k.txt"),
                  "--fail-on-stale"])
    assert rc == 0



def test_the_copy_carries_the_files_its_checkout_tracks(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    (src / "python").mkdir(parents=True)
    (src / "python" / "a.py").write_text("a\n")
    (src / "untracked.py").write_text("u\n")
    (src / "run.log").write_text("log\n")
    bd.subprocess.run(["git", "init", "-q", str(src)], check=True)
    bd.subprocess.run(["git", "-C", str(src), "add", "python/a.py", "run.log"], check=True)
    bd.subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                      check=True)
    bd.copy_tree(src, dest, dry=False)
    # run.log is tracked but not copied (EXCLUDE), so it is not listed.
    assert (dest / bd.TRACKED).read_text().split("\0") == ["python/a.py"]
    assert len((dest / bd.HEAD).read_text()) == 40


def test_a_tree_from_no_checkout_carries_no_list(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    (src / "a.py").write_text("a\n")
    bd.copy_tree(src, dest, dry=False)
    assert not (dest / bd.TRACKED).exists() and not (dest / bd.HEAD).exists()
