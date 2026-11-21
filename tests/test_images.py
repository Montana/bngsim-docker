"""The image definitions, read as text: every version pinned exactly, BioNetGen
checked by its sha256, and compose.yaml naming the images the CLI runs."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "images" / "Dockerfile").read_text(encoding="utf-8")
BNG = (ROOT / "images" / "Dockerfile.bionetgen").read_text(encoding="utf-8")
UBUNTU = (ROOT / "images" / "Dockerfile.ubuntu").read_text(encoding="utf-8")
RUN_IMAGE = (ROOT / "images" / "Dockerfile.bngsim").read_text(encoding="utf-8")


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


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.ubuntu", "Dockerfile.bionetgen", "Dockerfile.bngsim"])
def test_each_image_keeps_a_copy_of_its_dockerfile(name):
    text = (ROOT / "images" / name).read_text(encoding="utf-8")
    assert f"COPY {name} /etc/bngsim-docker/{name}" in text


def test_the_script_commits_the_tracked_files_before_the_build():
    # bngsim's build stamps the commit it is built from, and tests ask git what
    # is tracked: the copy is made a checkout, with the host's tracked files
    # committed, before uv sync. (After the build, with nothing tracked, 9 tests
    # failed that skip outside a checkout.)
    script = (ROOT / "scripts" / "container-test.sh").read_text(encoding="utf-8")
    commit, sync = script.index("commit -q --no-verify"), script.index("uv sync --frozen")
    assert script.index("git init -q .") < script.index("git add --pathspec-from-file") < commit < sync
    assert "safe.directory '*'" in script and "-rfEp" in script
    assert "if [ -f .bngsim-docker-tracked ]; then" in script  # no checkout: no git init


def test_on_arm64_uv_sync_leaves_antimony_out_and_the_image_s_wheel_goes_in():
    # antimony has no Linux aarch64 wheel: without the skip, uv sync fails there.
    script = (ROOT / "scripts" / "container-test.sh").read_text(encoding="utf-8")
    assert '[ "$(uname -m)" = aarch64 ]' in script and "--no-install-package antimony" in script
    # The log is started once, then appended to, so the arm64 note stays in it.
    assert script.index(": > /out/build.log") < script.index('"${SKIP[@]}" >> /out/build.log')
    # After the sync (which would remove it again), the wheel the image built.
    install = script.index('uv pip install --python .venv/bin/python --no-deps "$wheel"')
    assert script.index('"${SKIP[@]}" >> /out/build.log') < install < script.index("-m pytest")
    assert "wheels=(/opt/antimony/antimony-*.whl)" in script


def _arg_in(text, name):
    m = re.search(rf"^ARG {name}=(\S+)$", text, re.M)
    assert m, name
    return m.group(1)


def test_the_debian_image_builds_antimony_on_arm64_at_the_version_bngsim_locks():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg_in(BASE, "ANTIMONY_VERSION"))
    assert 'if [ "$TARGETARCH" = arm64 ]; then bash /usr/local/bin/build-antimony.sh "$ANTIMONY_VERSION" /opt/antimony; fi' in BASE
    # Before the scripts are copied in, so a change to bngsim-test does not rebuild antimony.
    assert BASE.index("build-antimony.sh \"$ANTIMONY_VERSION\"") < BASE.index("COPY --from=scripts --chmod=0755")
    for package in ("libexpat1-dev", "zlib1g-dev"):
        assert re.search(rf"^\s+{package} \\$", BASE, re.M), package


def test_build_antimony_pins_its_dependencies_by_commit():
    script = (ROOT / "scripts" / "build-antimony.sh").read_text(encoding="utf-8")
    assert re.search(r"^LIBSBML_COMMIT=[0-9a-f]{40}$", script, re.M)
    assert re.search(r"^SBMLNETWORK_COMMIT=[0-9a-f]{40}$", script, re.M)
    assert '"refs/tags/v$VERSION"' in script and "set -euo pipefail" in script
    # Debian's libSBML has no C++ namespace, which SBMLNetwork and antimony need.
    assert "-DWITH_CPP_NAMESPACE=ON" in script and "-DENABLE_COMP=ON" in script


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.ubuntu", "Dockerfile.bionetgen", "Dockerfile.bngsim"])
def test_each_image_names_its_source(name):
    """Docker Hub links an image to the repository its OCI source label names."""
    text = (ROOT / "images" / name).read_text(encoding="utf-8")
    assert 'org.opencontainers.image.source="https://github.com/Montana/bngsim-docker"' in text
    assert "org.opencontainers.image.description=" in text


def test_the_bngsim_image_pins_bngsim_python_and_bionetgen_as_the_others_do():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg(RUN_IMAGE, "BNGSIM_VERSION"))
    assert _arg(RUN_IMAGE, "PYTHON_VERSION") == _arg(BASE, "PYTHON_VERSION")
    # The same BioNetGen, checked against the same sha256, as the BioNetGen test image.
    assert _arg(RUN_IMAGE, "BNG_VERSION") == _arg(BNG, "BNG_VERSION")
    assert _arg(RUN_IMAGE, "BNG_SHA256") == _arg(BNG, "BNG_SHA256")
    assert "hashlib.sha256(data).hexdigest() == '${BNG_SHA256}'" in RUN_IMAGE
    m = re.search(r"^ENV BNGPATH=(\S+)$", RUN_IMAGE, re.M)
    assert m and m.group(1).startswith("/opt/")


def test_the_bngsim_image_installs_the_release_it_names_and_checks_it():
    assert "bngsim.__version__ == '${BNGSIM_VERSION}'" in RUN_IMAGE
    # Antimony on both: PyPI's on amd64, built from source on arm64, the same version.
    assert '"bngsim[pandas,xarray,hdf5,antimony]==${BNGSIM_VERSION}" "antimony==${ANTIMONY_VERSION}"' in RUN_IMAGE
    assert "--find-links /wheels --wheel-dir /wheels" in RUN_IMAGE
    assert 'if [ "$TARGETARCH" = arm64 ]; then bash /usr/local/bin/build-antimony.sh "$ANTIMONY_VERSION" /wheels; fi' in RUN_IMAGE
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg_in(RUN_IMAGE, "ANTIMONY_VERSION"))


@pytest.mark.parametrize("package", ["perl", "libexpat1", "gcc"])
def test_the_bngsim_image_has_what_running_bngsim_needs(package):
    """perl for BNG2.pl, libexpat1 for libsbml (the slim base leaves it out), gcc for compiled RHS."""
    final = RUN_IMAGE.split("# ── the image", 1)[1]
    assert package in final


def test_any_user_can_run_the_bngsim_image():
    """A home every user can write to, for bngsim's caches; and the examples."""
    assert "ENV HOME=/home/bngsim" in RUN_IMAGE
    assert "chmod 1777 /home/bngsim /home/bngsim/.cache /work" in RUN_IMAGE
    assert "COPY examples /examples" in RUN_IMAGE
    for name in ("run.py", "receptor.bngl", "decay.xml"):
        assert (ROOT / "images" / "examples" / name).is_file()


def test_the_jupyter_image_pins_its_versions_and_keeps_the_token():
    jupyter = RUN_IMAGE.split("FROM bngsim AS jupyter", 1)[1]
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg(jupyter, "JUPYTERLAB_VERSION"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg(jupyter, "MATPLOTLIB_VERSION"))
    # Anyone who reached the port could run code without it.
    assert "token=''" not in jupyter and "IdentityProvider.token" not in jupyter and "--NotebookApp.token" not in jupyter
    assert (ROOT / "images" / "examples" / "tour.ipynb").is_file()


def test_the_binder_image_is_the_jupyter_one_set_up_as_binder_needs():
    """mybinder.org runs the server itself, as uid 1000, through jupyterhub-singleuser."""
    binder = (ROOT / "binder" / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM montanamendy/bngsim:jupyter" in binder
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg(binder, "JUPYTERHUB_VERSION"))
    assert _arg(binder, "NB_UID") == "1000" and "USER ${NB_USER}" in binder
    assert 'cp -r /examples "/home/${NB_USER}/examples"' in binder


def test_the_readme_s_binder_link_opens_the_tour_on_the_binder_branch():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://mybinder.org/v2/gh/Montana/bngsim-docker/binder?urlpath=lab/tree/examples/tour.ipynb" in readme


def test_the_binder_branch_is_moved_only_by_the_jupyter_image_s_digest():
    """The publish job pins the jupyter image by digest on the binder branch, and
    leaves the branch where it is when that digest has not changed."""
    publish = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    job = publish.split("\n  binder:\n", 1)[1]
    assert 'pinned="FROM montanamendy/bngsim:jupyter@${digest}"' in job
    assert "already serves" in job and "git push -q --force origin binder" in job
    assert "contents: write" in job


def test_a_run_that_collects_nothing_says_so():
    # Under xdist, a path naming no file gives no error, only "no tests ran".
    script = (ROOT / "scripts" / "container-test.sh").read_text(encoding="utf-8")
    assert "if grep -q 'no tests ran' /out/pytest.log; then" in script
    assert script.index("-m pytest") < script.index("no tests ran: check the test paths")
    # Before the summary's grep, which finds nothing then and, under pipefail, ends the script.
    assert script.index("no tests ran: check the test paths") < script.index("grep -E '^FAILED|^ERROR")


# ── the lessons, the image's user, attestations, scans, version tags ──────────

LESSONS = ROOT / "images" / "examples" / "lessons"
PUBLISH = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")


def test_each_lesson_is_a_notebook_with_its_model_and_no_outputs():
    notebooks = sorted(LESSONS.glob("*.ipynb"))
    assert [n.name for n in notebooks] == ["1-equilibrium.ipynb", "2-ode-and-stochastic.ipynb",
                                           "3-binding-and-dose-response.ipynb", "4-enzyme-kinetics.ipynb"]
    for n in notebooks:
        nb = json.loads(n.read_text(encoding="utf-8"))
        code = "".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")
        for model in re.findall(r'Model\.load\("([^"]+)"\)', code):
            assert (LESSONS / model).is_file(), (n.name, model)  # relative: works in /examples and Binder's copy
        assert all(not c.get("outputs") for c in nb["cells"] if c["cell_type"] == "code"), n.name


def test_the_run_images_run_as_an_ordinary_user():
    assert "useradd --uid 1000 --no-create-home --home-dir /home/bngsim" in RUN_IMAGE
    bngsim_stage, jupyter_stage = RUN_IMAGE.split("FROM bngsim AS jupyter")
    assert 'USER bngsim\nCMD ["python"]' in bngsim_stage
    # The jupyter stage installs as root and hands back before its CMD.
    assert jupyter_stage.index("USER root") < jupyter_stage.index("pip install") < jupyter_stage.rindex("USER bngsim")
    assert jupyter_stage.rindex("USER bngsim") < jupyter_stage.index("CMD [")


def test_binder_installs_as_root_and_serves_as_its_own_user():
    binder = (ROOT / "binder" / "Dockerfile").read_text(encoding="utf-8")
    assert binder.index("USER root") < binder.index("pip install") < binder.index("useradd")
    # bngsim holds uid 1000 in the image; Binder's user must have it.
    assert binder.index("userdel bngsim") < binder.index('useradd --create-home --uid "${NB_UID}"')
    assert binder.rstrip().splitlines()[-1] == "USER ${NB_USER}"


def _job(name):
    """One job of publish.yml, from its key to the next job's."""
    start = re.search(rf"^  {re.escape(name)}:\n", PUBLISH, re.M).end()
    end = re.search(r"^  [a-z-]+:\n", PUBLISH[start:], re.M)
    return PUBLISH[start:start + end.start()] if end else PUBLISH[start:]


def test_every_published_build_carries_its_sbom_and_provenance():
    # Attestations need the containerd image store under the docker driver.
    assert PUBLISH.count("turn on Docker's containerd image store") == 2
    assert PUBLISH.count("--attest") == 3  # the test images' loop, the bngsim and the jupyter builds
    assert "'containerd-snapshotter'] = True" in PUBLISH
    # And every tag is checked for both, per architecture, after it is made.
    assert PUBLISH.count('python3 -c "$ATTEST"') == 2
    assert "{{json .SBOM}}" in PUBLISH and "{{json .Provenance}}" in PUBLISH
    # Each read from its own file: imagetools ends its output with no newline, so two piped
    # through one stream ran together (the first publish run's check failed on it).
    assert PUBLISH.count("--format '{{json .SBOM}}' > sbom.json") == 2
    assert "sys.stdin.read().split" not in PUBLISH


def test_every_image_is_scanned_before_it_is_pushed_and_the_scan_never_blocks():
    for job in ("build", "bngsim-build"):
        body = _job(job)
        assert body.index("scan-image.sh") < body.index("name: push"), job
    assert PUBLISH.count("continue-on-error: true") == 2
    script = (ROOT / "scripts" / "scan-image.sh").read_text(encoding="utf-8")
    assert re.search(r"^TRIVY=aquasec/trivy:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$", script, re.M)
    assert "--severity HIGH,CRITICAL --ignore-unfixed" in script


def test_a_new_release_is_published_within_a_day_and_an_old_one_moves_no_shared_tag():
    assert '- cron: "17 6 * * *"' in PUBLISH and "github.event.schedule != '17 6 * * *'" in PUBLISH
    assert "if: needs.release.outputs.build == 'true'" in PUBLISH
    assert 'if [ "$NEWEST_IN_SERIES" = true ]; then' in PUBLISH and 'if [ "$NEWEST" = true ]; then' in PUBLISH
    assert "if: needs.release.outputs.newest == 'true'" in PUBLISH  # the binder branch
    tag = PUBLISH[PUBLISH.index("# The release's own tags always"):]
    assert tag.index('if [ "$NEWEST" = true ]') < tag.index("montanamendy/bngsim:latest")


def test_the_release_is_picked_as_the_workflow_says(tmp_path):
    """The release job's script, run on PyPI's answer as it is today."""
    yaml = pytest.importorskip("yaml")
    pick = yaml.safe_load(PUBLISH)["jobs"]["release"]["steps"][0]["env"]["PICK"]
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, "-c", pick], capture_output=True, text=True, timeout=120,
                       env={**os.environ, "ASKED": "9.9.9", "DAILY_RUN": "false", "GITHUB_OUTPUT": str(out)})
    if "urlopen error" in r.stderr or "timed out" in r.stderr:
        pytest.skip("no network")
    assert r.returncode != 0 and "bngsim 9.9.9 is not on PyPI" in r.stderr
