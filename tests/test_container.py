"""Inside a built image: the architecture, the toolchain's versions and KLU, and
for the BioNetGen image, BNG2.pl and run_network. Run against one image:

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


def run(script: str) -> str:
    r = subprocess.run(["docker", "run", "--rm", "--platform", f"linux/{ARCH}", IMAGE, "bash", "-c", script],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def test_the_architecture_is_the_one_asked_for():
    assert run("uname -m") == {"amd64": "x86_64", "arm64": "aarch64"}[ARCH]


def test_python_and_uv_are_the_pinned_versions():
    python = re.search(r"^ARG PYTHON_VERSION=(\S+)$", BASE, re.M).group(1)
    uv = re.search(r"^ARG UV_VERSION=(\S+)$", BASE, re.M).group(1)
    assert run("python --version") == f"Python {python}"
    assert run("uv --version").split()[1] == uv


def test_the_toolchain_is_there():
    out = run("gcc --version | head -1; g++ --version | head -1; cmake --version | head -1; ninja --version; git --version")
    assert "gcc" in out and "g++" in out and "cmake version 3." in out and "git version" in out


def test_klu_s_header_and_library_are_there():
    """bngsim builds its sparse solver (BNGSIM_ENABLE_KLU) against SuiteSparse."""
    run("test -f /usr/include/suitesparse/klu.h && ls /usr/lib/*/libklu.so")


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
