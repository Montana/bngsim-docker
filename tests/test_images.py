"""The image definitions, read as text: every version pinned exactly, BioNetGen
checked by its sha256, and compose.yaml naming the images the CLI runs."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "images" / "Dockerfile").read_text(encoding="utf-8")
BNG = (ROOT / "images" / "Dockerfile.bionetgen").read_text(encoding="utf-8")
UBUNTU = (ROOT / "images" / "Dockerfile.ubuntu").read_text(encoding="utf-8")


def _arg(text: str, name: str) -> str:
    m = re.search(rf"^ARG {name}=(\S+)$", text, re.M)
    assert m, f"no ARG {name}= with a default"
    return m.group(1)


def test_the_base_is_pinned_to_an_exact_python_and_debian():
    """A floating tag would change the compiler and libm under a rerun."""
    assert re.fullmatch(r"3\.\d+\.\d+", _arg(BASE, "PYTHON_VERSION"))
    assert "FROM python:${PYTHON_VERSION}-bookworm" in BASE


def test_uv_is_pinned_exactly():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg(BASE, "UV_VERSION"))
    assert 'pip install --no-cache-dir "uv==${UV_VERSION}"' in BASE


@pytest.mark.parametrize("package", ["build-essential", "cmake", "ninja-build", "git", "libsuitesparse-dev"])
def test_the_base_has_what_bngsim_s_build_needs(package):
    assert re.search(rf"^\s+{re.escape(package)} \\$", BASE, re.M), package


def test_bionetgen_is_pinned_and_checked():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg(BNG, "BNG_VERSION"))
    assert re.fullmatch(r"[0-9a-f]{64}", _arg(BNG, "BNG_SHA256"))
    assert 'sha256sum -c -' in BNG and "${BNG_SHA256}" in BNG
    assert "BioNetGen-${BNG_VERSION}-linux.tar.gz" in BNG


def test_bngpath_is_absolute():
    """A relative BNGPATH fails the tests that run BNG2.pl from another directory."""
    m = re.search(r"^ENV BNGPATH=(\S+)$", BNG, re.M)
    assert m and m.group(1).startswith("/opt/")


def test_bionetgen_is_x86_64():
    assert "FROM --platform=linux/amd64" in BNG


def _compose(*args: str) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    r = subprocess.run(["docker", "compose", *args, "config", "--format", "json"], cwd=ROOT,
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                                                            "HOME": str(Path.home())})
    if r.returncode != 0 and "is not a docker command" in r.stderr:
        pytest.skip("docker compose not installed")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_compose_names_the_images_the_cli_runs():
    services = _compose("--profile", "build")["services"]
    assert services["linux-amd64"]["image"] == "bngsim-linux-amd64:latest"
    assert services["linux-arm64"]["image"] == "bngsim-linux-arm64:latest"
    assert services["linux-bionetgen"]["image"] == "bngsim-linux-bionetgen:latest"
    assert services["suite"]["image"] == "bngsim-linux-amd64:latest"
    assert services["suite-bionetgen"]["platform"] == "linux/amd64"
    assert services["linux-ubuntu"]["image"] == "bngsim-linux-ubuntu:latest"
    assert services["suite-ubuntu"]["image"] == "bngsim-linux-ubuntu:latest"


def test_compose_builds_without_a_tree_and_runs_the_shared_script():
    """Building needs no BNGSIM_TREE; a run mounts it read-only and runs
    scripts/container-test.sh, the script the CLI runs."""
    suite = _compose("--profile", "build")["services"]["suite"]
    assert suite["entrypoint"] == ["bash", "/scripts/container-test.sh"]
    mounts = {v["target"]: v for v in suite["volumes"]}
    assert mounts["/tree"]["read_only"] is True
    assert mounts["/scripts"]["read_only"] is True
    assert "set-BNGSIM_TREE" in mounts["/tree"]["source"]  # the default names what to set
    assert mounts["/ccache"]["type"] == "volume"


# ── the Ubuntu image, as bngsim's CI sets up ubuntu-latest ──────────────────


def test_ubuntu_is_pinned_by_digest():
    """A tag such as 26.04 moves with each point release; a digest does not."""
    assert re.fullmatch(r"26\.04@sha256:[0-9a-f]{64}", _arg(UBUNTU, "UBUNTU"))
    assert "FROM ubuntu:${UBUNTU}" in UBUNTU


def test_ubuntu_takes_uv_and_python_as_ci_does():
    assert _arg(UBUNTU, "UV_VERSION") == _arg(BASE, "UV_VERSION")
    assert "FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv" in UBUNTU
    assert "UV_PYTHON=3.12" in UBUNTU and "RUN uv python install 3.12" in UBUNTU
    assert "BNGSIM_EXTRA=dev" in UBUNTU


def test_ubuntu_has_no_suitesparse_package():
    """CI builds KLU's subset from source; a system SuiteSparse would hide a break there."""
    assert "suitesparse" not in UBUNTU.split("FROM ubuntu", 1)[1].split("ENV UV_PYTHON")[0].lower()


@pytest.mark.parametrize("text", [BASE, UBUNTU], ids=["debian", "ubuntu"])
def test_both_images_compile_through_ccache(text):
    assert re.search(r"^\s+ccache \\$", text, re.M)


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.ubuntu", "Dockerfile.bionetgen"])
def test_each_image_keeps_a_copy_of_its_dockerfile(name):
    text = (ROOT / "images" / name).read_text(encoding="utf-8")
    assert f"COPY {name} /etc/bngsim-docker/{name}" in text


def test_the_script_makes_the_copy_a_git_work_tree_after_the_build():
    script = (ROOT / "scripts" / "container-test.sh").read_text(encoding="utf-8")
    sync, init, pytest_at = script.index("uv sync"), script.index("git init -q ."), script.index("-m pytest")
    assert sync < init < pytest_at
    assert "safe.directory /src" in script and "-rfEp" in script
