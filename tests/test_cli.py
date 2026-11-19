"""bngsim_docker.py: the commands it would run and how it reads a run, with no
Docker needed (pure functions and --dry-run)."""

from __future__ import annotations

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
    assert bd.image_name("amd64", bionetgen=True) == "bngsim-linux-bionetgen:latest"


def test_build_is_the_base_then_bionetgen_on_it():
    (base,) = bd.build_commands("arm64", bionetgen=False)
    assert base[:4] == ["docker", "build", "--platform", "linux/arm64"]
    assert "bngsim-linux-arm64:latest" in base and base[-1].endswith("images")
    first, second = bd.build_commands("amd64", bionetgen=True)
    assert "bngsim-linux-amd64:latest" in first
    assert "bngsim-linux-bionetgen:latest" in second
    assert second[second.index("-f") + 1].endswith("Dockerfile.bionetgen")


def test_bionetgen_on_arm64_is_refused():
    """BioNetGen's Linux release is x86-64 only."""
    with pytest.raises(SystemExit, match="x86-64 only"):
        bd.build_commands("arm64", bionetgen=True)


def test_the_container_runs_the_shared_script():
    """One script for the CLI and compose.yaml, so the two cannot drift."""
    assert bd.container_script([], 3) == "WORKERS=3 BUILD_JOBS=2 bash /scripts/container-test.sh"
    script = bd.container_script(["python/tests/test_a.py", "python/tests/test_b.py::t[x y]"], 5, 1)
    assert script == ("WORKERS=5 BUILD_JOBS=1 bash /scripts/container-test.sh "
                      "python/tests/test_a.py 'python/tests/test_b.py::t[x y]'")


def test_the_run_mounts_the_copy_the_logs_and_the_scripts():
    cmd = bd.run_command(Path("/tmp/copy"), Path("/tmp/out"), "img:latest", "amd64", "S", "10g")
    assert cmd[:7] == ["docker", "run", "--rm", "--platform", "linux/amd64", "--memory", "10g"]
    mounts = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]
    assert mounts == ["/tmp/copy:/src", "/tmp/out:/out", f"{ROOT / 'scripts'}:/scripts:ro"]
    assert cmd[-4:] == ["img:latest", "bash", "-c", "S"]


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
