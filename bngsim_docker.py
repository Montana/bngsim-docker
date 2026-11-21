#!/usr/bin/env python3
"""Build bngsim's Linux test images, check Docker can start them, and run a
bngsim tree's tests in one.

    python bngsim_docker.py build                       # bngsim-linux-amd64:latest
    python bngsim_docker.py build --arch arm64
    python bngsim_docker.py build --kind ubuntu         # bngsim-linux-ubuntu:latest, as CI
    python bngsim_docker.py build --kind bionetgen      # bngsim-linux-bionetgen:latest (x86-64)
    python bngsim_docker.py probe                       # does a container start?
    python bngsim_docker.py doctor                      # probe; restart Docker Desktop if not
    python bngsim_docker.py test --tree ~/work/bngsim   # the whole of python/tests
    python bngsim_docker.py test --tree ~/work/bngsim --tests python/tests/test_x.py --kind ubuntu
    python bngsim_docker.py compare --tree ~/work/branch --main ~/work/main
    python bngsim_docker.py test --tree ~/work/bngsim --merge-main   # its commit merged with main
    python bngsim_docker.py test --tree ~/work/bngsim --pr 1774      # a pull request's head
    python bngsim_docker.py shell --tree ~/work/bngsim
    python bngsim_docker.py bng2 model.bngl             # BNG2.pl 2.9.3 on a model; files to ./bng2-out
    python bngsim_docker.py test --tree . --dry-run     # print the commands, run nothing
    python bngsim_docker.py clean                       # remove the compiler caches
    python bngsim_docker.py clean --images              # and the images

Three images (`--kind`):
- debian (default): Debian 12, the image bngsim-preflights' linux_check runs;
- ubuntu: Ubuntu 26.04 set up as bngsim's CI sets up ubuntu-latest (Python 3.12
  from uv, KLU built from source, the dev extra);
- bionetgen: the Debian image with BioNetGen 2.9.3, so the BNG2.pl tests run.

`test` copies the tree into a scratch directory (without `.venv`, `build`,
`.git`, logs and caches, so the container builds its own), builds it there with
`uv sync`, runs pytest, and sorts the failures: those listed in
`known-failures.txt` fail in this container on main too and are reported as
such; any other failure is the tree's. The exit status is 1 only for those.
An entry may name the images it fails in (`images=debian,bionetgen`), or be
`emulated-only`; a listed test that passes in this image is reported STALE.

`compare` runs the tests on a tree, then runs each test that failed again on a
checkout of main, in the same image: a test that fails on main too is the
image's or main's, and one that fails only on the tree is the tree's. It needs
no list of known failures. The exit status is 1 only for those.

The build compiles through ccache into a Docker volume per image
(`bngsim-ccache-<kind>-<arch>`), so a second run of a tree, or of a branch and
its main, recompiles only what changed. `--no-ccache` builds from scratch.

Each image keeps a copy of the Dockerfiles it was built from; `test` and
`compare` warn where it differs from this repository's, as after a change such
as ccache that the image was not rebuilt for.

`--pr N` tests a pull request's head, fetched from the tree's `origin`
(`--remote` names another), and `--merge-main` tests the tree's commit, or the
pull request's, merged with that remote's main: two branches that each pass
alone can fail together, as when each changes a count a test pins. Either
checks the commit out in a worktree of the tree's repository, in a scratch
directory, so what is tested is the commit and every file it tracks, and not
the tree's uncommitted changes; the worktree is removed afterwards. A merge
that conflicts stops the run and names the files.

`bng2` runs BNG2.pl, in the BioNetGen image, on a model: the model's directory
is mounted read-only, and the files BNG2.pl writes (the .net, the .gdat, ...)
and its log go to `--out` (default `bng2-out/<model>`).

`probe` asks for a real container, not `docker info`: after an update Docker
Desktop on macOS can answer `docker info` and start no container. `doctor`
then quits Docker Desktop by its full name, stops its backend, opens it again,
and probes once more.

`clean` lists what this tool made, with sizes: the cache volumes and the
images. It removes the caches, and the images too with `--images`; with
`--dry-run` it lists them and removes nothing. Volumes and images of other
names are not touched.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform as host_platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGES = ROOT / "images"
SCRIPTS = ROOT / "scripts"
IN_IMAGE_SCRIPTS = ("bngsim-test", "container-test.sh")  # copied into the test images
ANTIMONY_SCRIPT = "build-antimony.sh"  # builds antimony for arm64 (no Linux aarch64 wheel on PyPI)
KNOWN = ROOT / "known-failures.txt"
EXCLUDE = (".venv", "build", ".git", "*.log", "__pycache__", ".pytest_cache", ".mutate-backup", "*.so")
KINDS = ("debian", "ubuntu", "bionetgen", "bngsim", "bngsim-jupyter")
TESTING = ("debian", "ubuntu", "bionetgen")  # the images a tree is tested in; bngsim's runs bngsim
ARCHES = ("amd64", "arm64")
DOCKERFILE = {"debian": "Dockerfile", "ubuntu": "Dockerfile.ubuntu", "bionetgen": "Dockerfile.bionetgen",
              "bngsim": "Dockerfile.bngsim", "bngsim-jupyter": "Dockerfile.bngsim"}
RUNNING = {"bngsim": "bngsim", "bngsim-jupyter": "jupyter"}  # the images that run bngsim: their Dockerfile stage
STAMPS = "/etc/bngsim-docker"  # where each image keeps a copy of the Dockerfiles it was built from


def image_name(arch: str, kind: str = "debian") -> str:
    if kind in RUNNING:
        name = "bngsim-run" if kind == "bngsim" else "bngsim-jupyter"
        return f"{name}:latest" if arch == "amd64" else f"{name}-{arch}:latest"
    if kind == "bionetgen":
        return "bngsim-linux-bionetgen:latest"
    if kind == "ubuntu":
        return "bngsim-linux-ubuntu:latest" if arch == "amd64" else f"bngsim-linux-ubuntu-{arch}:latest"
    return f"bngsim-linux-{arch}:latest"


def ccache_volume(arch: str, kind: str) -> str:
    """One volume per image: the images' compilers differ, and so do their objects."""
    return f"bngsim-ccache-{kind}-{arch}"


def build_commands(arch: str, kind: str, fresh: bool = False,
                   bngsim_version: str | None = None, attest: bool = False) -> list[list[str]]:
    """The docker builds for an image: BioNetGen's is built on the Debian one.
    `fresh` rebuilds every layer from a newly pulled base (Debian's or Ubuntu's
    security updates); BioNetGen's base is the local Debian image, built just
    before, so it is not pulled. `bngsim_version` picks the release the bngsim
    image installs (default: the Dockerfile's). `attest` records an SBOM and the
    build's provenance with the image (the publish job; Docker needs its
    containerd image store for that)."""
    if bngsim_version and kind not in RUNNING:
        raise SystemExit("--bngsim-version is for --kind bngsim or bngsim-jupyter: the other images build a tree, "
                         "not a release")
    if kind == "bionetgen" and arch != "amd64":
        raise SystemExit("BioNetGen's Linux release is x86-64 only: --kind bionetgen needs --arch amd64")

    def build(k: str, *extra: str) -> list[str]:
        new = (["--no-cache"] if k == "bionetgen" else ["--pull", "--no-cache"]) if fresh else []
        if attest:
            new += ["--sbom=true", "--provenance=mode=max"]
        # The test images carry the repository's scripts (bngsim-test, container-test.sh),
        # and the Debian and bngsim images build antimony with one on arm64.
        scripts = ["--build-context", f"scripts={SCRIPTS}"] if k != "bionetgen" else []
        return ["docker", "build", "--platform", f"linux/{arch}", "-t", image_name(arch, k),
                "-f", str(IMAGES / DOCKERFILE[k]), *scripts, *new, *extra, str(IMAGES)]

    if kind == "bionetgen":
        return [build("debian"), build("bionetgen", "--build-arg", f"BASE={image_name(arch)}")]
    if kind in RUNNING:
        version = ["--build-arg", f"BNGSIM_VERSION={bngsim_version}"] if bngsim_version else []
        return [build(kind, "--target", RUNNING[kind], *version)]
    return [build(kind)]


def container_script(tests: list[str], workers: int, build_jobs: int = 2) -> str:
    """What the container runs: scripts/container-test.sh, mounted at /scripts,
    the one script compose.yaml runs too."""
    targets = " ".join(shlex.quote(t) for t in tests)
    return f"WORKERS={workers} BUILD_JOBS={build_jobs} bash /scripts/container-test.sh {targets}".rstrip()


@dataclass
class Run:
    summary: str
    failed: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    messages: dict[str, str] = field(default_factory=dict)  # failed id -> its error, from junit.xml


def test_id(rest: str) -> str:
    """The test id at the start of a summary line's remainder. A parametrized id
    can hold spaces (`test_x[S, V]`): it runs to the bracket that closes its
    first `[`, not to the first space; an id without one ends at the first space
    (a ` - message` may follow)."""
    head = rest.split(" ", 1)[0]
    if "[" not in head:
        return head
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return rest[:i + 1]
    return rest.split(" - ", 1)[0].rstrip()


def parse(output: str) -> Run:
    """The summary line, and the failed (or errored) and passed test ids, of a pytest
    run made with `-rfEp`, which lists each by id."""
    failed = [test_id(m.group(1)) for m in re.finditer(r"^(?:FAILED|ERROR) (.+)$", output, re.M)]
    passed = [test_id(m.group(1)) for m in re.finditer(r"^PASSED (.+)$", output, re.M)]
    rows = [ln.strip("= ") for ln in output.splitlines() if re.search(r"\d+ (passed|failed)", ln)]
    return Run(rows[-1] if rows else "", list(dict.fromkeys(failed)), list(dict.fromkeys(passed)))


def junit_key(test_id: str) -> tuple[str, str]:
    """A pytest id as its JUnit XML (classname, name):
    python/tests/test_a.py::TestB::test_c[x] -> (python.tests.test_a.TestB, test_c[x])."""
    path, *rest = test_id.split("::")
    module = path[:-3] if path.endswith(".py") else path
    return ".".join([module.replace("/", "."), *rest[:-1]]), rest[-1] if rest else ""


def junit_messages(path: Path) -> dict[tuple[str, str], str]:
    """Each failed or errored test's message, from pytest's --junitxml file
    ({} if there is none, as from an older script)."""
    import xml.etree.ElementTree as ET  # noqa: N817 (the standard library's own spelling)

    if not path.is_file():
        return {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {}
    out = {}
    for case in root.iter("testcase"):
        for tag in ("failure", "error"):
            el = case.find(tag)
            if el is not None:
                out[(case.get("classname", ""), case.get("name", ""))] = el.get("message", "") or (el.text or "")
                break
    return out


# arm64 images built before antimony was built from source in them install
# bngsim without it (PyPI has no Linux aarch64 wheel): a test whose only failure
# is that antimony is missing is the image's, there.
NO_ANTIMONY = re.compile(r"The 'antimony' package is required|No module named 'antimony'")


def needs_antimony(failed: list[str], messages: dict[str, str]) -> tuple[list[str], list[str]]:
    """(failures that are antimony missing, the rest)."""
    missing = [t for t in failed if NO_ANTIMONY.search(messages.get(t, ""))]
    return missing, [t for t in failed if t not in missing]


EMULATED = "emulated-only"  # the tag that limits an entry to an emulated run
IMAGES_TAG = "images="  # the tag that names the images an entry fails in


def entry_tags(tokens: list[str]) -> tuple[list[str], bool]:
    """(the images an entry fails in, whether only where emulated) from the
    words after its id: `images=debian,bionetgen` and `emulated-only`. Neither
    is written `@name`, which GitHub reads as a mention of that account."""
    kinds: list[str] = []
    emulated = False
    for t in tokens:
        if t == EMULATED:
            emulated = True
        elif t.startswith(IMAGES_TAG):
            kinds += [k for k in t[len(IMAGES_TAG):].split(",") if k]
        else:
            raise ValueError(f"known-failures: {t!r} is no tag (use {IMAGES_TAG}<kind,...> or {EMULATED})")
    return kinds, emulated


def read_known(path: Path = KNOWN, kind: str | None = None, emulated: bool | None = None) -> dict[str, str]:
    """`known-failures.txt`: one test id per line, then `#` and why it fails here.
    An id may be followed by `images=debian,bionetgen`, the images it fails in;
    one with none fails in every image. `emulated-only` limits an entry to a run
    whose image is emulated (an amd64 image on an Arm host). With `kind` and
    `emulated`, only the entries for that run."""
    known: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        head, _, why = line.partition("#")
        if not head.strip():
            continue
        test, *tokens = head.split()
        kinds, only_emulated = entry_tags(tokens)
        if kind is not None and kinds and kind not in kinds:
            continue
        if emulated is not None and only_emulated and not emulated:
            continue
        known[test] = why.strip()
    return known


def known_tags(path: Path = KNOWN) -> dict[str, tuple[list[str], bool]]:
    """Each entry's (images, emulated-only), as written (no images: every image)."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        head = line.partition("#")[0].split()
        if head:
            out[head[0]] = entry_tags(head[1:])
    return out


def daemon_arch() -> str | None:
    """The architecture Docker runs natively ("amd64" or "arm64"), or None if it
    cannot say. A container of another architecture is emulated."""
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.Architecture}}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(r.stdout.strip())


def _matches(test: str, ids: list[str] | dict[str, str]) -> bool:
    """A failed id is one of `ids`, or one of its parametrized cases."""
    return any(test == k or test.startswith(k + "[") for k in ids)


def sort_failures(failed: list[str], known: dict[str, str]) -> tuple[list[str], list[str]]:
    """(failures this container always has, failures that are the tree's)."""
    return [t for t in failed if _matches(t, known)], [t for t in failed if not _matches(t, known)]


def stale_known(run: Run, known: dict[str, str]) -> list[str]:
    """Standing failures this run passed (some case of, failing none): bngsim fixed
    them, or they fail only in other images. A test the run skipped or did not
    select is not listed."""
    return [k for k in known if any(_matches(t, [k]) for t in run.passed)
            and not any(_matches(t, [k]) for t in run.failed)]


def compare_failures(branch: list[str], on_main: list[str]) -> tuple[list[str], list[str]]:
    """(failures main has too, failures only the branch has): `on_main` is what
    failed when the branch's failures were run again on main."""
    return [t for t in branch if t in on_main], [t for t in branch if t not in on_main]


def sh(cmd: list[str], *, dry: bool, timeout: float | None = None,
       capture: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    print("+ " + " ".join(shlex.quote(c) for c in cmd), flush=True)
    if dry:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout,
                          env=None if env is None else {**os.environ, **env})


def probe(image: str, arch: str, *, dry: bool, wait: float = 90.0) -> bool:
    """Whether a container of `image` starts and reports the architecture asked for."""
    want = {"amd64": "x86_64", "arm64": "aarch64"}[arch]
    try:
        r = sh(["docker", "run", "--rm", "--platform", f"linux/{arch}", image, "uname", "-m"],
               dry=dry, timeout=wait)
    except subprocess.TimeoutExpired:
        return False
    return dry or (r.returncode == 0 and r.stdout.strip() == want)


def restart_docker_desktop(*, dry: bool) -> None:
    """Quit Docker Desktop by its full name, stop its backend, open it again."""
    if host_platform.system() != "Darwin":
        raise SystemExit("doctor restarts Docker Desktop on macOS; restart the daemon by hand here")
    for cmd in (["osascript", "-e", 'quit app "Docker Desktop"'],
                ["pkill", "-f", "com.docker.backend"], ["pkill", "-f", "Docker Desktop"],
                ["open", "-a", "Docker Desktop"]):
        sh(cmd, dry=dry)
        if not dry:
            time.sleep(5)
    for _ in range(36):
        if dry or sh(["docker", "info"], dry=dry).returncode == 0:
            return
        time.sleep(5)


def stamp_names(kind: str) -> list[str]:
    """The files an image is built from, whose copies it keeps: its Dockerfile
    (BioNetGen's is built on the Debian one), the scripts the test images carry,
    and the script that builds antimony on arm64 (the Debian and bngsim images)."""
    if kind == "bionetgen":
        return ["Dockerfile", "Dockerfile.bionetgen", *IN_IMAGE_SCRIPTS, ANTIMONY_SCRIPT]
    if kind == "ubuntu":
        return [DOCKERFILE[kind], *IN_IMAGE_SCRIPTS]
    if kind in TESTING:
        return [DOCKERFILE[kind], *IN_IMAGE_SCRIPTS, ANTIMONY_SCRIPT]
    return [DOCKERFILE[kind], ANTIMONY_SCRIPT]


def stamp_source(name: str) -> Path:
    return SCRIPTS / name if name in (*IN_IMAGE_SCRIPTS, ANTIMONY_SCRIPT) else IMAGES / name


def stale_image(image: str, arch: str, kind: str, *, dry: bool) -> list[str]:
    """Warnings where `image` was built from other Dockerfiles than the repository's
    (each image keeps a copy of its own in STAMPS), so a change to an image, such as
    ccache, does not reach the runs: the run goes on as before, without it."""
    names = stamp_names(kind)
    if dry:
        return []
    r = sh(["docker", "run", "--rm", "--platform", f"linux/{arch}", image, "sh", "-c",
            f"cd {STAMPS} 2>/dev/null && sha256sum {' '.join(names)} 2>/dev/null; true"], dry=False)
    have = {}
    for line in r.stdout.splitlines():
        digest, _, name = line.partition("  ")
        have[name.strip()] = digest.strip()
    rebuild = f"rebuild it: python bngsim_docker.py build --kind {kind} --arch {arch}"
    out = []
    for name in names:
        want = hashlib.sha256(stamp_source(name).read_bytes()).hexdigest()
        where = stamp_source(name).relative_to(ROOT)
        if name not in have:
            out.append(f"{image} carries no copy of {where}, so it predates it or this check; {rebuild}")
        elif have[name] != want:
            out.append(f"{image} was built from an older {where}; {rebuild}")
    return out


TRACKED = ".bngsim-docker-tracked"  # in the copy: the files the host's checkout tracks
HEAD = ".bngsim-docker-head"  # in the copy: the host's commit


def tracked_in_copy(src: Path, dest: Path) -> list[str] | None:
    """The files *src*'s git checkout tracks that the copy holds, or None where
    *src* is not a checkout. The copy has no .git; the container makes it one
    with these files committed, so a test that asks git what is tracked, and the
    build's commit stamp, read a checkout as they do on the host."""
    try:
        r = subprocess.run(["git", "-C", str(src), "ls-files", "-z"], capture_output=True, text=True)
    except OSError:  # no git on the host
        return None
    if r.returncode != 0:
        return None
    return [f for f in r.stdout.split("\0") if f and (dest / f).is_file()]


def copy_tree(src: Path, dest: Path, *, dry: bool) -> None:
    print(f"+ copy {src} -> {dest} (without {', '.join(EXCLUDE)})", flush=True)
    if dry:
        return
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*EXCLUDE), symlinks=True)
    tracked = tracked_in_copy(src, dest)
    if tracked is not None:
        (dest / TRACKED).write_text("\0".join(tracked), encoding="utf-8")
        head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True)
        (dest / HEAD).write_text(head.stdout.strip() or "unknown", encoding="utf-8")


def owner_env() -> list[str]:
    """The caller's user and group, for the container to hand its files back to:
    it runs as root, and on a Linux host what root wrote into the copy (the
    tree's .venv) could not be deleted by the caller afterwards."""
    if not hasattr(os, "getuid"):
        return []
    return ["-e", f"HOST_UID={os.getuid()}", "-e", f"HOST_GID={os.getgid()}"]


def run_command(tree: Path, out: Path, image: str, arch: str, script: str, memory: str,
                ccache: str | None = None) -> list[str]:
    cmd = ["docker", "run", "--rm", "--platform", f"linux/{arch}", "--memory", memory, *owner_env(),
           "-v", f"{tree}:/src", "-v", f"{out}:/out", "-v", f"{ROOT / 'scripts'}:/scripts:ro"]
    if ccache:
        cmd += ["-v", f"{ccache}:/ccache"]
    return [*cmd, image, "bash", "-c", script]


@dataclass
class Where:
    """What a run is on: the image, its platform, and how the container is given."""
    image: str
    arch: str
    memory: str
    ccache: str | None
    workers: int
    build_jobs: int
    timeout: float
    dry: bool


PRIVATE_REFS = "refs/bngsim-docker"
BNGSIM_URL = "https://github.com/lanl/bngsim"  # where --pr and --merge-main fetch to, apart from the tree's own refs


def _git(tree: Path, *args: str, dry: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return sh(["git", "-C", str(tree), *args], dry=dry, env=env)


# git asks who merges even when nothing is committed (--no-commit), and fails
# where no name is set or can be guessed, as on CI's runners and in containers.
# The merge is never committed, so this name is recorded nowhere.
MERGE_IDENTITY = {f"GIT_{who}_{what}": value for who in ("AUTHOR", "COMMITTER")
                  for what, value in (("NAME", "bngsim-docker"), ("EMAIL", "bngsim-docker@localhost"))}


def fetch_main(tree: Path, remote: str, *, dry: bool) -> str:
    """Fetch `remote`'s main to a ref of this tool's own (the tree's branches and
    origin/* stay where they were: --refmap= stops git updating them as well)."""
    main = f"{PRIVATE_REFS}/main"
    r = _git(tree, "fetch", "--quiet", "--refmap=", remote, f"+refs/heads/main:{main}", dry=dry)
    if r.returncode != 0:
        raise SystemExit(f"cannot fetch main from {remote}: {r.stderr.strip()}")
    return main


def _need_checkout(tree: Path, what: str, *, dry: bool) -> None:
    if not dry and subprocess.run(["git", "-C", str(tree), "rev-parse", "--git-dir"],
                                  capture_output=True).returncode != 0:
        raise SystemExit(f"{what}: {tree} is no git checkout")


def short(tree: Path, ref: str) -> str:
    return _git(tree, "rev-parse", "--short", ref, dry=False).stdout.strip()


@contextlib.contextmanager
def worktree_at(tree: Path, ref: str, *, dry: bool) -> Iterator[Path]:
    """A worktree of `tree`'s repository at `ref`, in a scratch directory, removed
    on the way out whatever happens. Only that worktree is ever removed."""
    with tempfile.TemporaryDirectory(prefix="bngsim-docker-wt-", ignore_cleanup_errors=True) as scratch:
        wt = Path(scratch) / "tree"
        r = _git(tree, "worktree", "add", "--quiet", "--detach", str(wt), ref, dry=dry)
        if r.returncode != 0:
            raise SystemExit(f"cannot check {ref} out: {r.stderr.strip()}")
        try:
            yield wt
        finally:
            assert wt.parent == Path(scratch)
            _git(tree, "worktree", "remove", "--force", str(wt), dry=dry)
            _git(tree, "worktree", "prune", dry=dry)


def carry_uncommitted(tree: Path, wt: Path, *, dry: bool) -> list[str]:
    """Bring the tree's uncommitted work into the worktree: its changes to tracked
    files (a three-way apply, so main's changes merged in beside them are kept)
    and its untracked files that are not ignored (a new test not yet added).
    They are then staged in the worktree's own index, so the copy counts them as
    tracked; the tree's index is not touched. Returns the untracked files."""
    print(f"+ git -C {tree} diff --binary HEAD | git -C {wt} apply --3way", flush=True)
    if dry:
        return []
    diff = subprocess.run(["git", "-C", str(tree), "diff", "--binary", "HEAD"], capture_output=True)
    if diff.stdout:
        r = subprocess.run(["git", "-C", str(wt), "apply", "--3way", "--whitespace=nowarn", "-"],
                           input=diff.stdout, capture_output=True)
        if r.returncode != 0:
            raise SystemExit("the tree's uncommitted changes do not apply to the commit tested: "
                             + r.stderr.decode(errors="replace").strip())
    others = _git(tree, "ls-files", "--others", "--exclude-standard", "-z", dry=False).stdout
    untracked = [f for f in others.split("\0") if f]
    for f in untracked:
        (wt / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tree / f, wt / f, follow_symlinks=False)
    _git(wt, "add", "--all", dry=False)
    return untracked


def is_bngsim_checkout(tree: Path) -> bool:
    """A git checkout of bngsim (not of another repository, such as this one)."""
    if not (tree / "python" / "bngsim").is_dir():
        return False
    return subprocess.run(["git", "-C", str(tree), "rev-parse", "--git-dir"], capture_output=True).returncode == 0


@contextlib.contextmanager
def checkout_for_pr(a: argparse.Namespace, *, dry: bool) -> Iterator[None]:
    """With --pr and no bngsim checkout at --tree, a clone of --repo-url to fetch
    the pull request (and main) from: partial (file contents come only with the
    commits checked out), in a scratch directory, removed afterwards. It becomes
    --tree, and its origin --remote, for the run."""
    if a.pr is None or dry or is_bngsim_checkout(a.tree.resolve()):
        yield
        return
    with tempfile.TemporaryDirectory(prefix="bngsim-docker-clone-", ignore_cleanup_errors=True) as scratch:
        clone = Path(scratch) / "bngsim"
        print(f"{a.tree.resolve()} is no bngsim checkout: cloning {a.repo_url} to fetch pull request {a.pr} from",
              flush=True)
        r = sh(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", a.repo_url, str(clone)], dry=False)
        if r.returncode != 0:
            raise SystemExit(f"cannot clone {a.repo_url}: {r.stderr.strip()}")
        a.tree, a.remote = clone, "origin"
        yield


@contextlib.contextmanager
def prepared_tree(tree: Path, *, pr: int | None = None, merge_main: bool = False, uncommitted: bool = False,
                  remote: str = "origin", dry: bool = False) -> Iterator[Path]:
    """The tree to copy into the container: `tree` itself, or, with `pr` or
    `merge_main`, a worktree of its repository at the pull request's head (or
    the tree's HEAD), merged with `remote`'s main if asked (left uncommitted,
    so the name it is merged under, MERGE_IDENTITY, is recorded nowhere), with
    the tree's uncommitted work carried in if `uncommitted`."""
    if pr is None and not merge_main:
        yield tree  # the tree as it stands, uncommitted work and all
        return
    if pr is not None and uncommitted:
        raise SystemExit("--uncommitted carries the tree's own edits onto its own commit: not with --pr")
    tree = tree.resolve()
    _need_checkout(tree, "--pr and --merge-main need a git checkout", dry=dry)
    ref = "HEAD"
    if pr is not None:
        ref = f"{PRIVATE_REFS}/pr/{pr}"
        r = _git(tree, "fetch", "--quiet", "--refmap=", remote, f"+refs/pull/{pr}/head:{ref}", dry=dry)
        if r.returncode != 0:
            raise SystemExit(f"cannot fetch pull request {pr} from {remote}: {r.stderr.strip()}")
    elif not dry and not uncommitted:
        dirty = _git(tree, "status", "--porcelain", dry=False).stdout
        if dirty.strip():
            print(f"WARNING: {tree} has uncommitted changes or untracked files: --merge-main tests "
                  "its commit, without them (--uncommitted carries them in)", file=sys.stderr, flush=True)
    main = fetch_main(tree, remote, dry=dry) if merge_main else ""
    with worktree_at(tree, ref, dry=dry) as wt:
        if merge_main:
            r = _git(wt, "merge", "--quiet", "--no-commit", "--no-ff", main, dry=dry, env=MERGE_IDENTITY)
            if r.returncode != 0:
                conflicts = _git(wt, "diff", "--name-only", "--diff-filter=U", dry=dry).stdout.split()
                _git(wt, "merge", "--abort", dry=dry)
                raise SystemExit(f"{ref} does not merge with {remote}'s main: "
                                 f"{', '.join(conflicts) or r.stdout.strip() or r.stderr.strip()}")
        carried = carry_uncommitted(tree, wt, dry=dry) if uncommitted else []
        if not dry:
            what = f"pull request {pr} at " if pr is not None else ""
            with_main = f" merged with {remote}'s main {short(tree, main)}" if merge_main else ""
            extra = (f", with the tree's uncommitted work ({len(carried)} untracked file(s))"
                     if uncommitted else "")
            print(f"testing {what}{short(wt, 'HEAD')}{with_main}{extra}", flush=True)
        yield wt


@contextlib.contextmanager
def main_tree(a: argparse.Namespace, *, dry: bool) -> Iterator[tuple[Path, str]]:
    """(the main checkout `compare` reruns the branch's failures on, a warning or
    ""). With --main, that checkout; without it, `remote`'s main, fetched (once:
    with --merge-main it is the main the branch was merged with), in a worktree.
    A --main other than the main the branch was merged with is warned of: a
    failure that main's newer commits cause would read as the branch's."""
    if a.main is not None:
        warn = ""
        if a.merge_main and not dry:
            have, merged = short(a.main, "HEAD"), short(a.tree.resolve(), f"{PRIVATE_REFS}/main")
            if have != merged:
                warn = (f"WARNING: --main is at {have}, the branch was merged with main {merged}: a failure only "
                        "main's other commits cause reads as BRANCH ONLY; leave --main out to rerun on "
                        f"{merged}")
        yield a.main, warn
        return
    tree = a.tree.resolve()
    _need_checkout(tree, "compare without --main fetches main, so needs a git checkout", dry=dry)
    ref = f"{PRIVATE_REFS}/main" if a.merge_main else fetch_main(tree, a.remote, dry=dry)
    with worktree_at(tree, ref, dry=dry) as wt:
        if not dry:
            print(f"main: {a.remote}'s main at {short(wt, 'HEAD')}", flush=True)
        yield wt, ""


def bng2_root(model: Path, root: Path | None) -> Path:
    """What `bng2` mounts: `root`, or the git checkout the model is in, or its
    directory. BNG2.pl reads a relative path (readFile, a .net) from the
    directory it runs in, the model's, so a model that reads ../x needs x mounted."""
    if root is not None:
        root = root.resolve()
        if not model.is_relative_to(root):
            raise SystemExit(f"--root {root} does not hold the model {model}")
        return root
    try:
        r = subprocess.run(["git", "-C", str(model.parent), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True)
    except OSError:
        return model.parent
    top = Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None
    return top if top is not None and model.is_relative_to(top) else model.parent


def bng2_command(model: Path, root: Path, out: Path, image: str, args: list[str]) -> list[str]:
    """BNG2.pl on `model`, run from its own directory: `root` (which holds it)
    mounted read-only at /model, its output and its log written to `out`, handed
    back to the caller's user."""
    rel = model.relative_to(root)
    here = shlex.quote(str(Path("/model") / rel.parent))
    log = shlex.quote(f"/out/{model.stem}.bng2.log")
    script = (f'cd {here} && perl "$BNGPATH/BNG2.pl" --outdir /out {" ".join(shlex.quote(x) for x in args)} '
              f"{shlex.quote(model.name)} 2>&1 | tee {log}; rc=${{PIPESTATUS[0]}}; "
              'if [ -n "${HOST_UID:-}" ]; then chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" /out; fi; '
              "exit $rc")
    script = re.sub(" +", " ", script)
    return ["docker", "run", "--rm", "--platform", "linux/amd64", *owner_env(),
            "-v", f"{root}:/model:ro", "-v", f"{out}:/out", image, "bash", "-c", script]


def cmd_bng2(a: argparse.Namespace) -> int:
    model = a.model.resolve()
    if not a.dry_run and not model.is_file():
        raise SystemExit(f"no model at {model}")
    root = bng2_root(model, a.root)
    out = (a.out or Path("bng2-out") / model.stem).resolve()
    image = a.image or image_name("amd64", "bionetgen")
    if not probe(image, "amd64", dry=a.dry_run):
        raise SystemExit(f"no container of {image} starts: build it (build --kind bionetgen), or run doctor")
    if not a.dry_run:
        out.mkdir(parents=True, exist_ok=True)
    print(f"mounting {root} read-only; BNG2.pl runs in the model's directory", flush=True)
    r = sh(bng2_command(model, root, out, image, shlex.split(a.args)), dry=a.dry_run, capture=False,
           timeout=a.timeout)
    if not a.dry_run:
        files = sorted(p.name for p in out.iterdir() if p.is_file())
        print(f"BNG2.pl exited {r.returncode}; in {out}: {', '.join(files) or 'nothing'}")
    return r.returncode


def run_tree(tree: Path, out: Path, tests: list[str], w: Where) -> Run:
    """Copy `tree` in, build it, run `tests`; the logs go to `out`."""
    tree = tree.resolve()
    if not (tree / "python" / "bngsim").is_dir() and not w.dry:
        raise SystemExit(f"{tree} is not a bngsim tree (no python/bngsim)")
    if not w.dry:
        out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bngsim-docker-", ignore_cleanup_errors=True) as scratch:
        copy = Path(scratch) / "src"
        copy_tree(tree, copy, dry=w.dry)
        script = container_script(tests, w.workers, w.build_jobs)
        r = sh(run_command(copy, out.resolve(), w.image, w.arch, script, w.memory, w.ccache),
               dry=w.dry, timeout=w.timeout)
    if w.dry:
        return Run("dry run")
    log = out / "pytest.log"
    run = parse(log.read_text(encoding="utf-8", errors="replace") if log.is_file() else r.stdout)
    by_key = junit_messages(out / "junit.xml")
    run.messages = {t: by_key[junit_key(t)] for t in run.failed if junit_key(t) in by_key}
    return run


def _where(a: argparse.Namespace) -> Where:
    image = a.image or image_name(a.arch, a.kind)
    return Where(image, a.arch, a.memory, None if a.no_ccache else ccache_volume(a.arch, a.kind),
                 a.workers, a.build_jobs, a.timeout, a.dry_run)


def cache_line(out: Path) -> str | None:
    """The script's `ccache: H of N compiles from the cache` line, from build.log."""
    log = out / "build.log"
    if not log.is_file():
        return None
    rows = [ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.startswith("ccache: ")]
    return rows[-1] if rows else None


def _report(out: Path, lines: list[str]) -> None:
    print("\n".join(lines))
    if out.is_dir():
        (out / "docker.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _start(w: Where, kind: str) -> list[str]:
    """Probe, and warn where the image is older than its Dockerfiles."""
    if not probe(w.image, w.arch, dry=w.dry):
        raise SystemExit(f"no container of {w.image} starts: run `python bngsim_docker.py doctor`")
    warnings = [f"WARNING: {m}" for m in stale_image(w.image, w.arch, kind, dry=w.dry)]
    for m in warnings:
        print(m, file=sys.stderr, flush=True)
    return warnings


def _a_testing_image(kind: str) -> None:
    if kind not in TESTING:
        raise SystemExit(f"the {kind} image runs bngsim; it does not build a tree: "
                         f"test in one of {', '.join(TESTING)}")


def cmd_test(a: argparse.Namespace) -> int:
    _a_testing_image(a.kind)
    w = _where(a)
    warnings = _start(w, a.kind)
    with (checkout_for_pr(a, dry=w.dry),
          prepared_tree(a.tree, pr=a.pr, merge_main=a.merge_main, uncommitted=a.uncommitted,
                        remote=a.remote, dry=w.dry) as tree):
        run = run_tree(tree, a.out, a.tests, w)
    if w.dry:
        return 0
    native = daemon_arch()
    emulated = native is not None and native != w.arch
    known = read_known(a.known, a.kind, emulated)
    standing, new = sort_failures(run.failed, known)
    no_antimony, new = needs_antimony(new, run.messages) if w.arch == "arm64" else ([], new)
    lines = [f"image {w.image}" + (f" (emulated: {w.arch} on an {native} Docker)" if emulated else ""),
             *warnings, *filter(None, [cache_line(a.out)]),
             f"summary: {run.summary or 'no pytest summary (see build.log)'}"]
    lines += [f"  standing (fails on main here too): {t}" for t in standing]
    if no_antimony:
        lines.append(f"  {len(no_antimony)} failure(s) only because the arm64 image has no antimony (no Linux "
                     "aarch64 wheel); the amd64 images run them")
        lines += [f"  needs antimony: {t}" for t in no_antimony]
    lines += [f"  NEW: {t}" for t in new]
    stale = stale_known(run, known)
    lines += [f"  STALE: {t} is listed in {a.known.name} for this image and passed: delete its line, "
              f"or narrow it with images=... or emulated-only" for t in stale]
    lines.append("no failure but the standing ones" if run.summary and not new else
                 "failures that are the tree's" if new else "the run did not finish")
    if a.fail_on_stale and stale:
        lines.append(f"{len(stale)} standing failure(s) passed: {a.known.name} is out of date (--fail-on-stale)")
    _report(a.out, lines)
    if a.fail_on_stale and stale:
        return 1
    return 0 if run.summary and not new else 1


def cmd_compare(a: argparse.Namespace) -> int:
    _a_testing_image(a.kind)
    with checkout_for_pr(a, dry=a.dry_run):
        return _compare(a)


def _compare(a: argparse.Namespace) -> int:
    w = _where(a)
    warnings = _start(w, a.kind)
    with prepared_tree(a.tree, pr=a.pr, merge_main=a.merge_main, uncommitted=a.uncommitted,
                       remote=a.remote, dry=w.dry) as tree:
        branch = run_tree(tree, a.out / "branch", a.tests, w)
    if w.dry:
        with main_tree(a, dry=True) as (m, _):
            run_tree(m, a.out / "main", ["<the branch's failures>"], w)
        return 0
    lines = [f"image {w.image}", *warnings, *filter(None, [cache_line(a.out / "branch")]),
             f"branch: {branch.summary or 'no pytest summary (see branch/build.log)'}"]
    if not branch.summary:
        _report(a.out, [*lines, "the branch's run did not finish"])
        return 1
    # On arm64 a failure that is only antimony missing is the image's: it is not
    # rerun on main, where it would fail alike.
    no_antimony, rerun = needs_antimony(branch.failed, branch.messages) if w.arch == "arm64" else ([], branch.failed)
    if no_antimony:
        lines.append(f"  {len(no_antimony)} failure(s) only because the arm64 image has no antimony (no Linux "
                     "aarch64 wheel), not rerun on main; the amd64 images run them")
        lines += [f"  needs antimony: {t}" for t in no_antimony]
    if not rerun:
        _report(a.out, [*lines, "nothing fails on the branch" if not no_antimony else
                        "no failure but antimony missing"])
        return 0
    with main_tree(a, dry=False) as (m, warn):
        if warn:
            print(warn, file=sys.stderr, flush=True)
            lines.insert(1, warn)
        main = run_tree(m, a.out / "main", rerun, w)
    lines.append(f"main, the branch's {len(rerun)} failure(s) run again: "
                 f"{main.summary or 'no pytest summary (see main/build.log)'}")
    if not main.summary:
        _report(a.out, [*lines, "the run on main did not finish"])
        return 1
    both, only = compare_failures(rerun, main.failed)
    lines += [f"  fails on main too: {t}" for t in both]
    lines += [f"  BRANCH ONLY: {t}" for t in only]
    lines.append("failures that are the branch's" if only else "every failure fails on main too")
    _report(a.out, lines)
    return 1 if only else 0



def made_here() -> tuple[set[str], set[str]]:
    """(the cache volumes, the image repositories) this tool names: nothing else
    is ever removed by `clean`."""
    pairs = [(k, a) for k in KINDS for a in ARCHES if not (k == "bionetgen" and a != "amd64")]
    # Only the images a tree is built in have a compiler cache.
    return ({ccache_volume(a, k) for k, a in pairs if k in TESTING},
            {image_name(a, k).rsplit(":", 1)[0] for k, a in pairs})


def docker_out(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)}: {r.stderr.strip()}")
    return r.stdout


def find_made_here() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """([(volume, size)], [(image:tag, size)]) of what exists now. The BioNetGen
    image comes first: it is built on the Debian one."""
    volumes, repos = made_here()
    df = json.loads(docker_out(["docker", "system", "df", "-v", "--format", "{{json .Volumes}}"]) or "[]")
    vols = sorted((v["Name"], v.get("Size", "?")) for v in df or [] if v.get("Name") in volumes)
    imgs = []
    for row in docker_out(["docker", "images", "--format", "{{.Repository}}|{{.Tag}}|{{.Size}}"]).splitlines():
        repo, tag, size = (row.split("|") + ["", ""])[:3]
        if repo in repos and tag != "<none>":
            imgs.append((f"{repo}:{tag}", size))
    imgs.sort(key=lambda t: ("bionetgen" not in t[0], t[0]))
    return vols, imgs


def cmd_clean(a: argparse.Namespace) -> int:
    vols, imgs = find_made_here()
    if not vols and not imgs:
        print("nothing of bngsim-docker's to remove")
        return 0
    for name, size in vols:
        print(f"volume  {name}  {size}")
    for name, size in imgs:
        print(f"image   {name}  {size}" + ("" if a.images else "  (kept; --images removes it)"))
    rc = 0
    for kind, names in (("volume", [n for n, _ in vols]), ("image", [n for n, _ in imgs] if a.images else [])):
        if names:
            r = sh(["docker", kind, "rm", *names], dry=a.dry_run)
            if r.returncode != 0:
                print(r.stderr.strip(), file=sys.stderr)
                rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--arch", choices=ARCHES, default="amd64")
    common.add_argument("--kind", choices=KINDS, default="debian",
                        help="debian (default), ubuntu (as bngsim's CI), bionetgen; or bngsim (bngsim installed, "
                             "to run it) and bngsim-jupyter (the same, with JupyterLab)")
    common.add_argument("--dry-run", action="store_true", help="print the commands; run nothing")

    p = sub.add_parser("build", parents=[common], help="build an image")
    p.add_argument("--fresh", action="store_true",
                   help="pull the base image again and build every layer anew (picks up security updates)")
    p.add_argument("--bngsim-version",
                   help="with --kind bngsim or bngsim-jupyter: the bngsim release to install (default: the Dockerfile's)")
    p.add_argument("--attest", action="store_true",
                   help="record an SBOM and the build's provenance with the image (needs Docker's containerd "
                        "image store; the publish job turns it on)")
    for name, helptext in (("probe", "does a container start?"),
                           ("doctor", "probe; restart Docker Desktop if no container starts")):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--image", help="default: the --kind and --arch image")
    for name, helptext in (("test", "run a tree's tests in a container"),
                           ("compare", "run a tree's tests, and its failures again on main"),
                           ("shell", "a shell in a container, the tree copied to /src")):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--tree", type=Path, default=Path("."), help="a bngsim tree (default: here)")
        p.add_argument("--image", help="default: the --kind and --arch image")
        p.add_argument("--memory", default="10g", help="the container's memory limit (default 10g)")
        p.add_argument("--pr", type=int, help="test this pull request's head, fetched from --remote")
        p.add_argument("--merge-main", action="store_true",
                       help="test the commit merged with --remote's main (two branches can fail only together)")
        p.add_argument("--uncommitted", action="store_true",
                       help="with --merge-main: carry the tree's uncommitted edits and untracked files in")
        p.add_argument("--remote", default="origin", help="where --pr and --merge-main fetch from (default origin)")
        p.add_argument("--repo-url", default=BNGSIM_URL,
                       help="with --pr and no bngsim checkout at --tree: the repository to clone (default lanl/bngsim)")
        if name == "shell":
            continue
        if name == "compare":
            p.add_argument("--main", type=Path,
                           help="a checkout of bngsim's main (default: --remote's main, fetched now)")
        p.add_argument("--tests", nargs="*", default=[], help="test files or ids (default: python/tests)")
        p.add_argument("--workers", type=int, default=3, help="pytest-xdist workers")
        p.add_argument("--build-jobs", type=int, default=2,
                       help="compiles at once (default 2: ExprTk alone takes several GB)")
        p.add_argument("--no-ccache", action="store_true", help="build without the ccache volume")
        p.add_argument("--out", type=Path, default=Path("docker-check"), help="where the logs go")
        p.add_argument("--timeout", type=float, default=5400, help="seconds for each run")
        if name == "test":
            p.add_argument("--known", type=Path, default=KNOWN, help="the standing failures")
            p.add_argument("--fail-on-stale", action="store_true",
                           help="exit 1 when a standing failure passed, so the list is kept true")
    p = sub.add_parser("bng2", help="run BNG2.pl (BioNetGen 2.9.3) on a model, in the BioNetGen image")
    p.add_argument("model", type=Path, help="a .bngl file")
    p.add_argument("--out", type=Path, help="where its files go (default bng2-out/<model>)")
    p.add_argument("--root", type=Path,
                   help="the directory to mount, holding the model and what it reads "
                        "(default: its git checkout, else its directory)")
    p.add_argument("--args", default="", help="more arguments for BNG2.pl, as one string")
    p.add_argument("--image", help="default: bngsim-linux-bionetgen:latest")
    p.add_argument("--timeout", type=float, default=1800, help="seconds")
    p.add_argument("--dry-run", action="store_true", help="print the commands; run nothing")
    p = sub.add_parser("clean", help="remove the compiler caches (and, with --images, the images)")
    p.add_argument("--images", action="store_true", help="remove the images too")
    p.add_argument("--dry-run", action="store_true", help="list them; remove nothing")
    a = ap.parse_args(argv)

    if a.cmd == "build":
        for cmd in build_commands(a.arch, a.kind, a.fresh, a.bngsim_version, a.attest):
            if sh(cmd, dry=a.dry_run, capture=False).returncode != 0:
                return 1
        return 0
    if a.cmd in ("probe", "doctor"):
        image = a.image or image_name(a.arch, a.kind)
        if probe(image, a.arch, dry=a.dry_run):
            print(f"{image}: a container starts")
            return 0
        if a.cmd == "probe":
            print(f"{image}: no container starts", file=sys.stderr)
            return 1
        restart_docker_desktop(dry=a.dry_run)
        ok = probe(image, a.arch, dry=a.dry_run)
        print(f"{image}: {'a container starts' if ok else 'still no container starts'}")
        return 0 if ok else 1
    if a.cmd == "test":
        return cmd_test(a)
    if a.cmd == "compare":
        return cmd_compare(a)
    if a.cmd == "clean":
        return cmd_clean(a)
    if a.cmd == "bng2":
        return cmd_bng2(a)
    # shell
    image = a.image or image_name(a.arch, a.kind)
    with (checkout_for_pr(a, dry=a.dry_run),
          prepared_tree(a.tree, pr=a.pr, merge_main=a.merge_main, uncommitted=a.uncommitted,
                        remote=a.remote, dry=a.dry_run) as tree,
          tempfile.TemporaryDirectory(prefix="bngsim-docker-", ignore_cleanup_errors=True) as scratch):
        copy = Path(scratch) / "src"
        copy_tree(tree.resolve(), copy, dry=a.dry_run)
        cmd = ["docker", "run", "--rm", "-it", "--platform", f"linux/{a.arch}", "--memory", a.memory,
               "-v", f"{copy}:/src", image, "bash"]
        return sh(cmd, dry=a.dry_run, capture=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
