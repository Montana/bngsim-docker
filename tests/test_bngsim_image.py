"""Inside the bngsim image (the one that runs bngsim, not the test images): every
model format it promises loads and simulates, checked against the exact
solution of A -> 0 (tests/bngsim_image/smoke.py). Run against a built image:

    BNGSIM_RUN_IMAGE=bngsim-run:latest python -m pytest tests/test_bngsim_image.py

Skipped when BNGSIM_RUN_IMAGE is not set (CI sets it after building).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("BNGSIM_RUN_IMAGE", "")
ARCH = os.environ.get("BNGSIM_DOCKER_ARCH", "arm64" if "arm64" in IMAGE else "amd64")
pytestmark = pytest.mark.skipif(not IMAGE, reason="set BNGSIM_RUN_IMAGE to a built bngsim image")


def run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "run", "--rm", "--platform", f"linux/{ARCH}",
                           "-v", f"{ROOT / 'tests' / 'bngsim_image'}:/check:ro", IMAGE, *cmd],
                          capture_output=True, text=True, timeout=600)


def test_every_format_loads_and_matches_the_exact_solution():
    r = run("python", "/check/smoke.py")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all checks passed" in r.stdout
    if ARCH == "amd64":
        assert "Antimony: matches" in r.stdout


def test_bngpath_names_bionetgen_and_perl_runs_it():
    r = run("bash", "-c", 'perl "$BNGPATH/BNG2.pl" -v')
    assert r.returncode == 0 and "BioNetGen version 2.9.3" in r.stdout, r.stdout + r.stderr


def test_python_is_what_the_image_runs():
    r = run("python", "-c", "import bngsim, sys; print(bngsim.__version__); sys.exit(0)")
    assert r.returncode == 0 and r.stdout.strip(), r.stderr


def test_bngsim_s_core_finds_every_library_it_links():
    """The arm64 core is built here and links the system's KLU and LAPACK: each
    must be installed in the image, not only in the stage that built it."""
    r = run("bash", "-c", 'ldd "$(python -c "import bngsim._bngsim_core as c; print(c.__file__)")"')
    assert r.returncode == 0, r.stderr
    assert "not found" not in r.stdout, r.stdout


def test_a_user_other_than_root_compiles_a_model():
    """docker run --user: bngsim's caches live under $HOME, which must be
    writable by any user, or codegen fails with "Permission denied: '/.cache'"."""
    r = subprocess.run(["docker", "run", "--rm", "--platform", f"linux/{ARCH}", "--user", "1234:1234", IMAGE,
                        "python", "-c",
                        "import bngsim; s = bngsim.Simulator(bngsim.Model.load('/examples/receptor.bngl'),"
                        " method='ode', codegen=True); s.run(t_span=(0, 1), n_points=2); print(s.codegen_backend)"],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] in ("cc", "mir"), r.stdout


def test_the_examples_run_and_agree_with_their_exact_answers():
    r = subprocess.run(["docker", "run", "--rm", "--platform", f"linux/{ARCH}", "--user", "1234:1234", IMAGE,
                        "python", "/examples/run.py"], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    exact, simulated = re.search(r"equilibrium, exact: ([\d.]+)\s+simulated: ([\d.]+)", r.stdout).groups()
    assert exact == simulated == "425.8", r.stdout
    for t, a, want in re.findall(r"t = (\d)\s+A =\s+([\d.]+)\s+exact\s+([\d.]+)", r.stdout):
        assert a == want, (t, a, want)
