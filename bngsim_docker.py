#!/usr/bin/env python3
"""Build bngsim's Linux test images, check Docker can start them, and run a
bngsim tree's tests in one.

    python bngsim_docker.py build                      # bngsim-linux-amd64:latest
    python bngsim_docker.py build --arch arm64
    python bngsim_docker.py build --bionetgen          # bngsim-linux-bionetgen:latest (x86-64)
    python bngsim_docker.py probe                      # does a container start?
    python bngsim_docker.py doctor                     # probe; restart Docker Desktop if not
    python bngsim_docker.py test --tree ~/work/bngsim  # the whole of python/tests
    python bngsim_docker.py test --tree ~/work/bngsim --tests python/tests/test_x.py
    python bngsim_docker.py shell --tree ~/work/bngsim
    python bngsim_docker.py test --tree . --dry-run    # print the commands, run nothing

`test` copies the tree into a scratch directory (without `.venv`, `build`,
`.git`, logs and caches, so the container builds its own), builds it there with
`uv sync`, runs pytest, and sorts the failures: those listed in
`known-failures.txt` fail in this container on main too and are reported as
such; any other failure is the tree's. The exit status is 1 only for those.

`probe` asks for a real container, not `docker info`: after an update Docker
Desktop on macOS can answer `docker info` and start no container. `doctor`
then quits Docker Desktop by its full name, stops its backend, opens it again,
and probes once more.
"""

from __future__ import annotations

import argparse
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


def image_name(arch: str, bionetgen: bool = False) -> str:
    return "bngsim-linux-bionetgen:latest" if bionetgen else f"bngsim-linux-{arch}:latest"


def build_commands(arch: str, bionetgen: bool) -> list[list[str]]:
    """The docker builds for an image: the base, then BioNetGen on top of it."""
    if bionetgen and arch != "amd64":
        raise SystemExit("BioNetGen's Linux release is x86-64 only: --bionetgen needs --arch amd64")
    base = ["docker", "build", "--platform", f"linux/{arch}", "-t", image_name(arch),
            "-f", str(IMAGES / "Dockerfile"), str(IMAGES)]
    if not bionetgen:
        return [base]
    return [base, ["docker", "build", "--platform", "linux/amd64", "-t", image_name(arch, True),
                   "-f", str(IMAGES / "Dockerfile.bionetgen"), str(IMAGES)]]


def container_script(tests: list[str], workers: int, build_jobs: int = 2) -> str:
    """What the container runs: scripts/container-test.sh, mounted at /scripts,
    the one script compose.yaml runs too."""
    targets = " ".join(shlex.quote(t) for t in tests)
    return f"WORKERS={workers} BUILD_JOBS={build_jobs} bash /scripts/container-test.sh {targets}".rstrip()


@dataclass
class Run:
    summary: str
    failed: list[str] = field(default_factory=list)


def parse(output: str) -> Run:
    """The summary line and the failed (or errored) test ids of a pytest run."""
    failed = [m.group(1) for m in re.finditer(r"^(?:FAILED|ERROR) (\S+)", output, re.M)]
    rows = [ln.strip("= ") for ln in output.splitlines() if re.search(r"\d+ (passed|failed)", ln)]
    return Run(rows[-1] if rows else "", list(dict.fromkeys(failed)))


def read_known(path: Path = KNOWN) -> dict[str, str]:
    """`known-failures.txt`: one test id per line, then `#` and why it fails here."""
    known: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        test, _, why = line.partition("#")
        if test.strip():
            known[test.strip()] = why.strip()
    return known


def sort_failures(failed: list[str], known: dict[str, str]) -> tuple[list[str], list[str]]:
    """(failures this container always has, failures that are the tree's). A known
    entry matches a failed id that is it, or that is one of its parametrized cases."""
    def is_known(test: str) -> bool:
        return any(test == k or test.startswith(k + "[") for k in known)
    standing = [t for t in failed if is_known(t)]
    return standing, [t for t in failed if not is_known(t)]


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


def copy_tree(src: Path, dest: Path, *, dry: bool) -> None:
    print(f"+ copy {src} -> {dest} (without {', '.join(EXCLUDE)})", flush=True)
    if not dry:
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*EXCLUDE), symlinks=True)


def run_command(tree: Path, out: Path, image: str, arch: str, script: str,
                memory: str) -> list[str]:
    return ["docker", "run", "--rm", "--platform", f"linux/{arch}", "--memory", memory,
            "-v", f"{tree}:/src", "-v", f"{out}:/out", "-v", f"{ROOT / 'scripts'}:/scripts:ro",
            image, "bash", "-c", script]


def cmd_test(a: argparse.Namespace) -> int:
    image = a.image or image_name(a.arch, a.bionetgen)
    tree = a.tree.resolve()
    if not (tree / "python" / "bngsim").is_dir() and not a.dry_run:
        raise SystemExit(f"{tree} is not a bngsim tree (no python/bngsim)")
    if not probe(image, a.arch, dry=a.dry_run):
        raise SystemExit(f"no container of {image} starts: run `python bngsim_docker.py doctor`")
    out = a.out.resolve()
    if not a.dry_run:
        out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bngsim-docker-") as scratch:
        copy = Path(scratch) / "src"
        copy_tree(tree, copy, dry=a.dry_run)
        r = sh(run_command(copy, out, image, a.arch, container_script(a.tests, a.workers, a.build_jobs),
                           a.memory),
               dry=a.dry_run, timeout=a.timeout)
    if a.dry_run:
        return 0
    log = out / "pytest.log"
    run = parse(log.read_text(encoding="utf-8", errors="replace") if log.is_file() else r.stdout)
    standing, new = sort_failures(run.failed, read_known(a.known))
    report = [f"image {image}", f"summary: {run.summary or 'no pytest summary (see build.log)'}"]
    report += [f"  standing (fails on main here too): {t}" for t in standing]
    report += [f"  NEW: {t}" for t in new]
    verdict = "no failure but the standing ones" if run.summary and not new else \
        "failures that are the tree's" if new else "the run did not finish"
    report.append(verdict)
    (out / "docker.log").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    return 0 if run.summary and not new else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--arch", choices=["amd64", "arm64"], default="amd64")
    common.add_argument("--dry-run", action="store_true", help="print the commands; run nothing")

    b = sub.add_parser("build", parents=[common], help="build an image")
    b.add_argument("--bionetgen", action="store_true", help="also build the BioNetGen image (x86-64)")
    for name, helptext in (("probe", "does a container start?"),
                           ("doctor", "probe; restart Docker Desktop if no container starts")):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--image", help="default: bngsim-linux-<arch>:latest")
    for name, helptext in (("test", "run a tree's tests in a container"),
                           ("shell", "a shell in a container, the tree copied to /src")):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--tree", type=Path, default=Path("."), help="a bngsim tree (default: here)")
        p.add_argument("--image", help="default: bngsim-linux-<arch>:latest")
        p.add_argument("--bionetgen", action="store_true", help="use bngsim-linux-bionetgen:latest")
        p.add_argument("--memory", default="10g", help="the container's memory limit (default 10g)")
        if name == "test":
            p.add_argument("--tests", nargs="*", default=[], help="test files or ids (default: python/tests)")
            p.add_argument("--workers", type=int, default=3, help="pytest-xdist workers")
            p.add_argument("--build-jobs", type=int, default=2,
                           help="compiles at once (default 2: ExprTk alone takes several GB)")
            p.add_argument("--out", type=Path, default=Path("docker-check"), help="where the logs go")
            p.add_argument("--known", type=Path, default=KNOWN, help="the standing failures")
            p.add_argument("--timeout", type=float, default=5400, help="seconds for the run")
    a = ap.parse_args(argv)

    if a.cmd == "build":
        for cmd in build_commands(a.arch, a.bionetgen):
            if sh(cmd, dry=a.dry_run, capture=False).returncode != 0:
                return 1
        return 0
    if a.cmd in ("probe", "doctor"):
        image = a.image or image_name(a.arch)
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
    # shell
    image = a.image or image_name(a.arch, a.bionetgen)
    with tempfile.TemporaryDirectory(prefix="bngsim-docker-") as scratch:
        copy = Path(scratch) / "src"
        copy_tree(a.tree.resolve(), copy, dry=a.dry_run)
        cmd = ["docker", "run", "--rm", "-it", "--platform", f"linux/{a.arch}", "--memory", a.memory,
               "-v", f"{copy}:/src", image, "bash"]
        return sh(cmd, dry=a.dry_run, capture=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
