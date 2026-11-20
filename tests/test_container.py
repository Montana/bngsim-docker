"""Inside a built image: the architecture, the toolchain's versions, ccache and
KLU (from apt, or for the Ubuntu image none, as CI), and for the BioNetGen
image, BNG2.pl and run_network. Run against one image:

    BNGSIM_DOCKER_IMAGE=bngsim-linux-amd64:latest python -m pytest tests/test_container.py

Skipped when BNGSIM_DOCKER_IMAGE is not set (CI sets it after building).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("BNGSIM_DOCKER_IMAGE", "")
pytestmark = pytest.mark.skipif(not IMAGE, reason="set BNGSIM_DOCKER_IMAGE to a built image")
ARCH = os.environ.get("BNGSIM_DOCKER_ARCH", "arm64" if "arm64" in IMAGE else "amd64")
BASE = (ROOT / "images" / "Dockerfile").read_text(encoding="utf-8")
UBUNTU = "ubuntu" in IMAGE


def run(script: str) -> str:
    r = subprocess.run(["docker", "run", "--rm", "--platform", f"linux/{ARCH}", IMAGE, "bash", "-c", script],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def test_the_architecture_is_the_one_asked_for():
    assert run("uname -m") == {"amd64": "x86_64", "arm64": "aarch64"}[ARCH]


def test_python_and_uv_are_the_pinned_versions():
    uv = re.search(r"^ARG UV_VERSION=(\S+)$", BASE, re.M).group(1)
    assert run("uv --version").split()[1] == uv
    if UBUNTU:
        # CI's Python: 3.12 from uv, installed into the image, so a run downloads none.
        assert run("uv python find 3.12 --no-python-downloads").startswith("/opt/uv-python/")
        assert run("echo $BNGSIM_EXTRA") == "dev"
        return
    python = re.search(r"^ARG PYTHON_VERSION=(\S+)$", BASE, re.M).group(1)
    assert run("python --version") == f"Python {python}"


def test_the_toolchain_is_there():
    out = run("gcc --version | head -1; g++ --version | head -1; cmake --version | head -1; ninja --version; git --version")
    assert "gcc" in out and "g++" in out and "git version" in out
    # 3.25 in the Debian images, 4.2 in the Ubuntu one (as CI's runners); bngsim needs 3.20 (its cmake_minimum_required).
    major, minor = map(int, re.search(r"cmake version (\d+)\.(\d+)", out).groups())
    assert (major, minor) >= (3, 20), out


def test_ccache_is_there():
    """The build compiles through it when a /ccache volume is mounted."""
    assert run("ccache --version | head -1").startswith("ccache version")


@pytest.mark.skipif(UBUNTU, reason="the Ubuntu image has no SuiteSparse, as CI")
def test_klu_s_header_and_library_are_there():
    """bngsim builds its sparse solver (BNGSIM_ENABLE_KLU) against SuiteSparse."""
    run("test -f /usr/include/suitesparse/klu.h && ls /usr/lib/*/libklu.so")


@pytest.mark.skipif(not UBUNTU, reason="the Ubuntu image only")
def test_ubuntu_has_no_system_suitesparse():
    """CI builds KLU's subset from source; a system one would hide a break there."""
    assert run("ls /usr/include/suitesparse/klu.h 2>/dev/null || echo none") == "none"


def test_the_container_test_script_refuses_a_missing_tree():
    r = subprocess.run(["docker", "run", "--rm", "--platform", f"linux/{ARCH}",
                        "-v", f"{ROOT / 'scripts'}:/scripts:ro", IMAGE, "bash", "/scripts/container-test.sh"],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 2 and "no bngsim tree" in r.stderr


@pytest.mark.skipif("bionetgen" not in IMAGE, reason="the BioNetGen image only")
def test_bionetgen_runs():
    assert run("echo $BNGPATH").startswith("/opt/BioNetGen-")
    assert run('perl "$BNGPATH/BNG2.pl" -v 2>&1 | grep -o "BioNetGen version [0-9.]*"').startswith("BioNetGen version")
    run('test -x "$BNGPATH/bin/run_network" && test -x "$BNGPATH/bin/NFsim"')


def test_the_image_carries_the_repository_s_dockerfiles():
    # What `test` and `compare` compare with the repository's, to warn of an image
    # built from an older Dockerfile. CI builds and checks the image from this tree.
    names = ["Dockerfile", "Dockerfile.bionetgen"] if "bionetgen" in IMAGE else (
        ["Dockerfile.ubuntu"] if UBUNTU else ["Dockerfile"])
    for name in names:
        assert run(f"cat /etc/bngsim-docker/{name}") == (ROOT / "images" / name).read_text(encoding="utf-8").strip()


def test_the_image_carries_the_scripts_it_runs():
    """bngsim-test and container-test.sh, on PATH, and the copies the staleness check reads."""
    for name in ("bngsim-test", "container-test.sh"):
        assert run(f"command -v {name}") == f"/usr/local/bin/{name}"
        want = (ROOT / "scripts" / name).read_text(encoding="utf-8").strip()
        assert run(f"cat /usr/local/bin/{name}") == want
        assert run(f"cat /etc/bngsim-docker/{name}") == want


def _bngsim_test(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "run", "--rm", "--platform", f"linux/{ARCH}", IMAGE, "bngsim-test", *args],
                          capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize("args,said", [
    (["--merge-main"], "--merge-main merges --pr or --ref"),
    (["--pr", "12", "--ref", "main"], "--pr or --ref, not both"),
    (["--pr", "abc"], "takes a pull request number"),
    (["--bogus"], "unknown option --bogus"),
])
def test_bngsim_test_refuses_what_it_cannot_do(args, said):
    r = _bngsim_test(*args)
    assert r.returncode == 2 and said in r.stderr, r.stdout + r.stderr


def test_bngsim_test_explains_itself():
    r = _bngsim_test("--help")
    assert r.returncode == 0 and "--pr N" in r.stdout and "--merge-main" in r.stdout


def test_bngsim_test_never_fetches_over_a_mounted_tree(tmp_path):
    """With a tree at /tree, --pr and --ref are refused at once, not fetched in its place."""
    (tmp_path / "python" / "bngsim").mkdir(parents=True)
    for args in (["--pr", "1"], ["--ref", "main"]):
        r = subprocess.run(["docker", "run", "--rm", "--platform", f"linux/{ARCH}", "-v", f"{tmp_path}:/tree:ro",
                            IMAGE, "bngsim-test", *args], capture_output=True, text=True, timeout=60)
        assert r.returncode == 2 and "a tree is mounted at /tree" in r.stderr, r.stdout + r.stderr
        assert "fetching" not in r.stdout
