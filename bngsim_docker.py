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
    python bngsim_docker.py shell --tree ~/work/bngsim
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
An entry may name the images it fails in (`@debian @bionetgen`); a listed test
that passes in this image is reported STALE, so the list follows main.

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
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGES = ROOT / "images"
KNOWN = ROOT / "known-failures.txt"
EXCLUDE = (".venv", "build", ".git", "*.log", "__pycache__", ".pytest_cache", ".mutate-backup", "*.so")
KINDS = ("debian", "ubuntu", "bionetgen")
ARCHES = ("amd64", "arm64")
DOCKERFILE = {"debian": "Dockerfile", "ubuntu": "Dockerfile.ubuntu", "bionetgen": "Dockerfile.bionetgen"}
STAMPS = "/etc/bngsim-docker"  # where each image keeps a copy of the Dockerfiles it was built from


def image_name(arch: str, kind: str = "debian") -> str:
    if kind == "bionetgen":
        return "bngsim-linux-bionetgen:latest"
    if kind == "ubuntu":
        return "bngsim-linux-ubuntu:latest" if arch == "amd64" else f"bngsim-linux-ubuntu-{arch}:latest"
    return f"bngsim-linux-{arch}:latest"


def ccache_volume(arch: str, kind: str) -> str:
    """One volume per image: the images' compilers differ, and so do their objects."""
    return f"bngsim-ccache-{kind}-{arch}"


def build_commands(arch: str, kind: str) -> list[list[str]]:
    """The docker builds for an image: BioNetGen's is built on the Debian one."""
    if kind == "bionetgen" and arch != "amd64":
        raise SystemExit("BioNetGen's Linux release is x86-64 only: --kind bionetgen needs --arch amd64")

    def build(k: str, *extra: str) -> list[str]:
        return ["docker", "build", "--platform", f"linux/{arch}", "-t", image_name(arch, k),
                "-f", str(IMAGES / DOCKERFILE[k]), *extra, str(IMAGES)]

    if kind == "bionetgen":
        return [build("debian"), build("bionetgen", "--build-arg", f"BASE={image_name(arch)}")]
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


def parse(output: str) -> Run:
    """The summary line, and the failed (or errored) and passed test ids, of a pytest
    run made with `-rfEp`, which lists each by id."""
    failed = [m.group(1) for m in re.finditer(r"^(?:FAILED|ERROR) (\S+)", output, re.M)]
    passed = [m.group(1) for m in re.finditer(r"^PASSED (\S+)", output, re.M)]
    rows = [ln.strip("= ") for ln in output.splitlines() if re.search(r"\d+ (passed|failed)", ln)]
    return Run(rows[-1] if rows else "", list(dict.fromkeys(failed)), list(dict.fromkeys(passed)))


EMULATED = "emulated"


def read_known(path: Path = KNOWN, kind: str | None = None, emulated: bool | None = None) -> dict[str, str]:
    """`known-failures.txt`: one test id per line, then `#` and why it fails here.
    An id may be followed by the images it fails in, as `@debian @bionetgen`; one
    with none fails in every image. `@emulated` limits an entry to a run whose
    image is emulated (an amd64 image on an Arm host). With `kind` and `emulated`,
    only the entries for that run."""
    known: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        head, _, why = line.partition("#")
        if not head.strip():
            continue
        test, *tags = head.split()
        tags = [t[1:] for t in tags if t.startswith("@")]
        kinds = [t for t in tags if t != EMULATED]
        if kind is not None and kinds and kind not in kinds:
            continue
        if emulated is not None and EMULATED in tags and not emulated:
            continue
        known[test] = why.strip()
    return known


def daemon_arch() -> str | None:
    """The architecture Docker runs natively ("amd64" or "arm64"), or None if it
    cannot say. A container of another architecture is emulated."""
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.Architecture}}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(r.stdout.strip())


def known_kinds(path: Path = KNOWN) -> dict[str, list[str]]:
    """Each entry's `@` tags, as written (no image tag: every image)."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        head = line.partition("#")[0].split()
        if head:
            out[head[0]] = [t[1:] for t in head[1:] if t.startswith("@")]
    return out


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
       capture: bool = True) -> subprocess.CompletedProcess:
    print("+ " + " ".join(shlex.quote(c) for c in cmd), flush=True)
    if dry:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)


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
    """The Dockerfiles an image is built from: BioNetGen's is built on the Debian one."""
    return ["Dockerfile", "Dockerfile.bionetgen"] if kind == "bionetgen" else [DOCKERFILE[kind]]


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
        want = hashlib.sha256((IMAGES / name).read_bytes()).hexdigest()
        if name not in have:
            out.append(f"{image} carries no copy of images/{name}, so it predates it or this check; {rebuild}")
        elif have[name] != want:
            out.append(f"{image} was built from an older images/{name}; {rebuild}")
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
    return parse(log.read_text(encoding="utf-8", errors="replace") if log.is_file() else r.stdout)


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


def cmd_test(a: argparse.Namespace) -> int:
    w = _where(a)
    warnings = _start(w, a.kind)
    run = run_tree(a.tree, a.out, a.tests, w)
    if w.dry:
        return 0
    native = daemon_arch()
    emulated = native is not None and native != w.arch
    known = read_known(a.known, a.kind, emulated)
    standing, new = sort_failures(run.failed, known)
    lines = [f"image {w.image}" + (f" (emulated: {w.arch} on an {native} Docker)" if emulated else ""),
             *warnings, *filter(None, [cache_line(a.out)]),
             f"summary: {run.summary or 'no pytest summary (see build.log)'}"]
    lines += [f"  standing (fails on main here too): {t}" for t in standing]
    lines += [f"  NEW: {t}" for t in new]
    stale = stale_known(run, known)
    lines += [f"  STALE: {t} is listed in {a.known.name} for this image and passed: delete its line, "
              f"or name the images it fails in with @" for t in stale]
    lines.append("no failure but the standing ones" if run.summary and not new else
                 "failures that are the tree's" if new else "the run did not finish")
    if a.fail_on_stale and stale:
        lines.append(f"{len(stale)} standing failure(s) passed: {a.known.name} is out of date (--fail-on-stale)")
    _report(a.out, lines)
    if a.fail_on_stale and stale:
        return 1
    return 0 if run.summary and not new else 1


def cmd_compare(a: argparse.Namespace) -> int:
    w = _where(a)
    warnings = _start(w, a.kind)
    branch = run_tree(a.tree, a.out / "branch", a.tests, w)
    if w.dry:
        run_tree(a.main, a.out / "main", ["<the branch's failures>"], w)
        return 0
    lines = [f"image {w.image}", *warnings, *filter(None, [cache_line(a.out / "branch")]),
             f"branch: {branch.summary or 'no pytest summary (see branch/build.log)'}"]
    if not branch.summary:
        _report(a.out, [*lines, "the branch's run did not finish"])
        return 1
    if not branch.failed:
        _report(a.out, [*lines, "nothing fails on the branch"])
        return 0
    main = run_tree(a.main, a.out / "main", branch.failed, w)
    lines.append(f"main, the branch's {len(branch.failed)} failure(s) run again: "
                 f"{main.summary or 'no pytest summary (see main/build.log)'}")
    if not main.summary:
        _report(a.out, [*lines, "the run on main did not finish"])
        return 1
    both, only = compare_failures(branch.failed, main.failed)
    lines += [f"  fails on main too: {t}" for t in both]
    lines += [f"  BRANCH ONLY: {t}" for t in only]
    lines.append("failures that are the branch's" if only else "every failure fails on main too")
    _report(a.out, lines)
    return 1 if only else 0



def made_here() -> tuple[set[str], set[str]]:
    """(the cache volumes, the image repositories) this tool names: nothing else
    is ever removed by `clean`."""
    pairs = [(k, a) for k in KINDS for a in ARCHES if not (k == "bionetgen" and a != "amd64")]
    return ({ccache_volume(a, k) for k, a in pairs},
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
                        help="debian (default), ubuntu (as bngsim's CI) or bionetgen")
    common.add_argument("--dry-run", action="store_true", help="print the commands; run nothing")

    sub.add_parser("build", parents=[common], help="build an image")
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
        if name == "shell":
            continue
        if name == "compare":
            p.add_argument("--main", type=Path, required=True, help="a checkout of bngsim's main")
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
    p = sub.add_parser("clean", help="remove the compiler caches (and, with --images, the images)")
    p.add_argument("--images", action="store_true", help="remove the images too")
    p.add_argument("--dry-run", action="store_true", help="list them; remove nothing")
    a = ap.parse_args(argv)

    if a.cmd == "build":
        for cmd in build_commands(a.arch, a.kind):
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
    # shell
    image = a.image or image_name(a.arch, a.kind)
    with tempfile.TemporaryDirectory(prefix="bngsim-docker-", ignore_cleanup_errors=True) as scratch:
        copy = Path(scratch) / "src"
        copy_tree(a.tree.resolve(), copy, dry=a.dry_run)
        cmd = ["docker", "run", "--rm", "-it", "--platform", f"linux/{a.arch}", "--memory", a.memory,
               "-v", f"{copy}:/src", image, "bash"]
        return sh(cmd, dry=a.dry_run, capture=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
