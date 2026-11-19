<img width="1870" height="841" alt="76b6a44b-fce6-43ba-9d2c-8878dd0c587e" src="https://github.com/user-attachments/assets/a793c0f9-419d-45c5-ae11-1db924514c2c" />

# bngsim-docker

Docker images and a small CLI for running [bngsim](https://github.com/lanl/bngsim)'s tests on Linux.

A fix that depends on rounding can pass on Apple Silicon and fail on CI's x86-64 Linux. These images let a bngsim tree be built from source and tested on Linux, on a Mac or anywhere Docker runs, before its pull request is opened. The Debian image is the one [bngsim-preflights](https://github.com/Montana/bngsim-preflights)' `linux_check.py` runs (`bngsim-linux-amd64:latest`), written down so it can be rebuilt the same way.

The images hold only the toolchain. Each run copies a bngsim tree in, without its `.venv`, `build`, `.git`, logs and compiled cores, and builds it there with `uv sync`. So the build is the container's own, and nothing is written back to the tree.

## Contents

- [The images](#the-images)
- [Which image for which question](#which-image-for-which-question)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Settings](#settings)
- [Compose](#compose)
- [The compiler cache](#the-compiler-cache)
- [Failures that are the container's](#failures-that-are-the-containers)
- [Troubleshooting](#troubleshooting)
- [What is in the repository](#what-is-in-the-repository)
- [Tests and CI](#tests-and-ci)
- [Requirements](#requirements)

## The images

| | `bngsim-linux-amd64` / `-arm64` | `bngsim-linux-ubuntu` | `bngsim-linux-bionetgen` |
|---|---|---|---|
| **What it is for** | the everyday Linux check | reproducing bngsim's CI | the tests that need BNG2.pl |
| **Dockerfile** | [`images/Dockerfile`](images/Dockerfile) | [`images/Dockerfile.ubuntu`](images/Dockerfile.ubuntu) | [`images/Dockerfile.bionetgen`](images/Dockerfile.bionetgen) |
| **`--kind`** | `debian` (the default) | `ubuntu` | `bionetgen` |
| **OS** | Debian 12 (bookworm) | Ubuntu 26.04 (resolute), as `ubuntu-latest` | Debian 12 |
| **C library** | glibc 2.36 | glibc 2.43 | glibc 2.36 |
| **Compiler** | gcc 12.2 | gcc 15.2 | gcc 12.2 |
| **CMake / Ninja** | 3.25 / 1.11 | 4.2 / 1.13 | 3.25 / 1.11 |
| **Python** | 3.12.16 (the `python` image) | 3.12.14 (from uv, as CI) | 3.12.16 |
| **uv** | 0.12.18 | 0.12.18 | 0.12.18 |
| **KLU** | SuiteSparse 5.12 from apt | built from source, as CI and an sdist install do | SuiteSparse 5.12 from apt |
| **Extra `uv sync` installs** | `test` | `dev` (Antimony, RoadRunner, JAX, ...), as CI | `test` |
| **ccache** | yes | yes | yes |
| **BioNetGen** | no | no | 2.9.3, sha256-checked, absolute `BNGPATH` |
| **Platforms** | `linux/amd64`, `linux/arm64` | `linux/amd64` | `linux/amd64` (BioNetGen ships x86-64 only) |
| **Pinned by** | exact Python and Debian tags | the Ubuntu image's digest | the base, plus BioNetGen's sha256 |

The versions are read from the built images; `tests/test_container.py` checks the ones that matter inside each.

## Which image for which question

| The question | Run |
|---|---|
| Does my change pass on x86-64 Linux at all? | `test --tree .` |
| A CI leg failed and I can't reproduce it on the Mac | `test --tree . --kind ubuntu` |
| Is a ulp-sensitive solver change the same on Linux? | `test --tree . --kind ubuntu` (CI's libm), then `--kind debian` for a second glibc |
| Is this failure mine or main's? | `compare --tree . --main ~/work/bngsim-main` |
| Do the BNG2.pl / NFsim parity tests pass? | `test --tree . --kind bionetgen` |
| Does it pass on 64-bit Arm Linux? | `test --tree . --arch arm64` (native speed on Apple Silicon) |
| I want to poke at the build by hand | `shell --tree .` |

## Quick start

```bash
git clone https://github.com/Montana/bngsim-docker && cd bngsim-docker

python bngsim_docker.py build                        # bngsim-linux-amd64:latest
python bngsim_docker.py build --kind ubuntu          # bngsim-linux-ubuntu:latest
python bngsim_docker.py build --kind bionetgen       # the amd64 image, then BioNetGen on it
python bngsim_docker.py probe                        # does a container start?

python bngsim_docker.py test --tree ~/work/bngsim    # the whole of python/tests
python bngsim_docker.py test --tree ~/work/bngsim --tests python/tests/test_x.py
python bngsim_docker.py test --tree ~/work/bngsim --kind ubuntu
python bngsim_docker.py compare --tree ~/work/branch --main ~/work/main
python bngsim_docker.py shell --tree ~/work/bngsim   # a shell, the tree copied to /src

python bngsim_docker.py clean --dry-run              # what clean would remove, with sizes
python bngsim_docker.py clean                        # remove the compiler caches
python bngsim_docker.py clean --images               # and the images
```

## Commands

| Command | What it does | Exit status |
|---|---|---|
| `build` | builds the `--kind` / `--arch` image (for `bionetgen`, the Debian image first) | docker build's |
| `probe` | starts a container and checks `uname -m`; not `docker info` | 0 if a container starts |
| `doctor` | `probe`; if nothing starts, restarts Docker Desktop (macOS) and probes again | 0 if a container starts |
| `test` | copies the tree in, builds it, runs pytest, sorts failures against [`known-failures.txt`](known-failures.txt) | 1 if a failure is not listed, or the run did not finish |
| `compare` | runs the tree's tests, then runs each failure again on a main checkout in the same image | 1 if a failure is the branch's only, or a run did not finish |
| `shell` | an interactive shell, the tree copied to `/src` | the shell's |
| `clean` | lists the cache volumes and images this tool makes, with their sizes, and removes the caches (`--images`: the images too). Volumes and images of other names are left alone | 1 if Docker refuses one, such as a volume a running container uses |

Flags shared by every command but `clean` (which takes only `--images` and `--dry-run`):

| Flag | Default | Meaning |
|---|---|---|
| `--kind` | `debian` | `debian`, `ubuntu` or `bionetgen` |
| `--arch` | `amd64` | `amd64` or `arm64` |
| `--dry-run` | off | print each command; run none |

And by `test` and `compare`:

| Flag | Default | Meaning |
|---|---|---|
| `--tree` | `.` | the bngsim tree to test |
| `--main` | (required for `compare`) | a checkout of bngsim's main |
| `--tests` | `python/tests` | test files or ids |
| `--workers` | 3 | pytest-xdist workers |
| `--build-jobs` | 2 | compiles at once (see [Troubleshooting](#troubleshooting)) |
| `--memory` | `10g` | the container's memory limit |
| `--no-ccache` | off | build without the compiler cache |
| `--out` | `docker-check/` | where `build.log`, `pytest.log` and `docker.log` go (`compare`: `branch/` and `main/` under it) |
| `--timeout` | 5400 | seconds for each run |
| `--image` | the `--kind` / `--arch` image | run another tag, such as `:candidate` |
| `--known` | `known-failures.txt` | (`test` only) the standing failures |

### `test` and `compare`, side by side

| | `test` | `compare` |
|---|---|---|
| Knows a failure is not the tree's from | the list in `known-failures.txt` | running it again on main, now |
| Runs | the tree once | the tree, then only its failures on main |
| Needs | nothing else | a checkout of main |
| Goes stale | when main gains or fixes a standing failure | never |
| Best for | a quick check | the final word before a pull request |

## Settings

The CLI and Compose both run [`scripts/container-test.sh`](scripts/container-test.sh), which reads these from the environment:

| Variable | Default | Meaning |
|---|---|---|
| `WORKERS` | 3 | pytest-xdist workers |
| `BUILD_JOBS` | 2 | compiles at once (`CMAKE_BUILD_PARALLEL_LEVEL`) |
| `BNGSIM_EXTRA` | `test` (`dev` in the Ubuntu image) | the extra `uv sync` installs |
| `HOST_UID`, `HOST_GID` | unset (the CLI sets them) | the container runs as root; on the way out it hands `/src` and `/out` back to this user, so the caller can delete them |
| `BNGSIM_TREE` | none | (Compose only) the tree to mount at `/tree` |

## Compose

```bash
docker compose build linux-amd64 && docker compose build linux-bionetgen
docker compose build linux-ubuntu linux-arm64
BNGSIM_TREE=~/work/bngsim docker compose run --rm suite
BNGSIM_TREE=~/work/bngsim docker compose run --rm suite python/tests/test_x.py
BNGSIM_TREE=~/work/bngsim docker compose run --rm suite-ubuntu
BNGSIM_TREE=~/work/bngsim docker compose run --rm suite-bionetgen
BNGSIM_TREE=~/work/bngsim docker compose run --rm shell
```

| Service | Image | Does |
|---|---|---|
| `linux-amd64`, `linux-arm64`, `linux-ubuntu`, `linux-bionetgen` | the image of that name | build only (profile `build`) |
| `suite` | `bngsim-linux-amd64` | the tree's tests |
| `suite-arm64` | `bngsim-linux-arm64` | the same, on arm64 |
| `suite-ubuntu` | `bngsim-linux-ubuntu` | the same, as CI |
| `suite-bionetgen` | `bngsim-linux-bionetgen` | the same, with BNG2.pl |
| `shell` | `bngsim-linux-amd64` | a shell, the tree copied to `/src` |

Logs go to `./out`. Compose does not sort failures; use the CLI's `test` or `compare` for that.

## The compiler cache

Every run builds bngsim from scratch inside the container, since the tree's own build is the host's. A run compiles through ccache into a Docker volume, one per image, so the next run of the same tree, or of a branch and its main, recompiles only what changed.

| | Time to build and run one test file | Compiles from the cache |
|---|---|---|
| First run (cold cache) | 483 s | none |
| Second run, same tree | 90 s | 152 of 153 |
| Ubuntu image, second run | | 150 of 248 (the `dev` extra builds more, and some of it is not cached) |

Measured on an Apple Silicon Mac, `bngsim-linux-amd64` under emulation, bngsim main, one test file. Most of the 90 s left is downloading and installing the Python dependencies. The cache for a full build is about 30 MB compressed.

| Volume | Used by |
|---|---|
| `bngsim-ccache-debian-amd64` | `--kind debian`, `suite` |
| `bngsim-ccache-debian-arm64` | `--arch arm64`, `suite-arm64` |
| `bngsim-ccache-ubuntu-amd64` | `--kind ubuntu`, `suite-ubuntu` |
| `bngsim-ccache-bionetgen-amd64` | `--kind bionetgen`, `suite-bionetgen` |

The CLI and Compose use the same names, so they fill the same cache. The images' compilers differ, so each has its own. `--no-ccache` builds without it. `docker volume rm bngsim-ccache-debian-amd64` empties one, `clean` empties them all, and the cache caps itself at 3 GB.

## Failures that are the container's

A handful of tests fail in these containers on bngsim's main too. [`known-failures.txt`](known-failures.txt) lists them, each with its reason, so a red `test` run says at once whether it is the tree:

| Test | Fails in | Why it fails there |
|---|---|---|
| `test_codegen_compile_reaping.py::test_a_compiler_that_cannot_be_started_raises_as_popen_did` | every image | a missing compiler does not raise `FileNotFoundError` ("DID NOT RAISE"). It fails in the Debian and the Ubuntu image alike and passes on CI's runners, so it is not the distribution; cause not traced |
| `test_codegen_toolchain_verdict.py::test_a_missing_compiler_is_a_verdict_said_once` | every image | the same: with `CC` pointing nowhere, the refusal reads "the C toolchain cannot build a codegen library", not "C compiler not found" |
| `test_run_tests_sh.py::test_the_script_itself_runs_walk_up_dependent_tests_green` | Debian, BioNetGen | the optional extras are not installed (jax, diffrax, roadrunner, vivarium-core); it passes on the Ubuntu image, which installs the `dev` extra |
| `test_vendor_script_git_cwd.py::TestGuardRunsGitInsideAWorkTree::test_git_is_invoked_from_a_directory_git_recognizes` | every image | the tree is copied without `.git`, so `/src` is not a Git work tree |

Measured on bngsim main: the Debian image, 4 failed and 79 passed on these four files and a few others (the full suite, in the image `linux_check.py` ran before this repository: the same 4 failed, 19,932 passed); the Ubuntu image, 4 failed and 88 passed before its RoadRunner fix, then that file 22 passed. A listed test that passes in one image is not a failure there, so one list serves all three.

Add a line when main shows a new one here, and remove it when main fixes it. `compare` needs no list at all.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `docker info` works, but no container ever starts | Docker Desktop on macOS after an update | `doctor`: it quits Docker Desktop by its full name (`quit app "Docker Desktop"`; "Docker" does not do it), stops `com.docker.backend`, opens it again, and probes |
| `Killed signal terminated program cc1plus` | ExprTk's translation unit (`src/expression.cpp`) alone takes several GB; three at once overran Docker Desktop's default 8 GB VM | keep `--build-jobs 2`; raise it only with the VM's memory, and run one bngsim build at a time |
| BNG2.pl tests fail when run from another directory | a relative `BNGPATH` | the BioNetGen image sets an absolute one; keep it absolute if you override it |
| RoadRunner: `libpython3.12.so.1.0: cannot open shared object file` | uv's Python is not on the loader path, and bngsim's fix (`ci/libpython_on_loader_path.sh`) writes to `$GITHUB_ENV`, which only GitHub Actions reads | fixed: the script gives it one and reads `LD_LIBRARY_PATH` back |
| `PermissionError` deleting the scratch copy (Linux host) | the container runs as root, so the `.venv` it built is root's | fixed: the CLI passes `HOST_UID`/`HOST_GID` and the script hands its files back; with Compose, pass them yourself |
| `no bngsim tree at /tree (set BNGSIM_TREE)` | Compose run without `BNGSIM_TREE` | `BNGSIM_TREE=~/work/bngsim docker compose run --rm suite` |
| `no pytest summary (see build.log)` | the build failed before pytest started | read `--out/build.log`; its last 50 lines are printed too |
| The amd64 run is very slow on a Mac | x86-64 is emulated on Apple Silicon | let the cache warm (see above), or use `--arch arm64` when the architecture does not matter |
| The run fails with "exec format error" | the image was built for another platform | build with the same `--arch` you run with |

## What is in the repository

| Path | What it is |
|---|---|
| [`bngsim_docker.py`](bngsim_docker.py) | the CLI (standard library only) |
| [`scripts/container-test.sh`](scripts/container-test.sh) | what runs in the container: copy, build, test; shared by the CLI and Compose |
| [`images/`](images) | the three Dockerfiles |
| [`compose.yaml`](compose.yaml) | build and run services, and the named cache volumes |
| [`known-failures.txt`](known-failures.txt) | the standing failures, each with its reason |
| [`tests/`](tests) | the repository's own tests |
| [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | CI |

## Tests and CI

```bash
python -m pip install pytest
python -m pytest -q tests                                            # no image needed
BNGSIM_DOCKER_IMAGE=bngsim-linux-amd64:latest python -m pytest -q tests/test_container.py
BNGSIM_DOCKER_IMAGE=bngsim-linux-ubuntu:latest python -m pytest -q tests/test_container.py
BNGSIM_DOCKER_IMAGE=bngsim-linux-bionetgen:latest python -m pytest -q tests/test_container.py
```

| File | Checks | Needs |
|---|---|---|
| `tests/test_cli.py` | the commands the CLI runs (pure functions and `--dry-run`), how it reads a pytest run, how `test` sorts failures and how `compare` decides, with the runs stood in for | nothing |
| `tests/test_images.py` | every version pinned exactly; BioNetGen's sha256; `BNGPATH` absolute; the Ubuntu image set up as CI; Compose naming the images and volumes the CLI uses | Docker Compose, for the Compose checks |
| `tests/test_container.py` | inside a built image: the architecture, the pinned Python and uv, the toolchain, ccache, KLU, the script's refusal of a missing tree, and BNG2.pl, run_network and NFsim | a built image |

| CI job | Runs on | Does |
|---|---|---|
| tests and lint | ubuntu-latest | the tests above, ruff, shellcheck, hadolint |
| build the images, check inside them | ubuntu-latest | builds the Debian, Ubuntu and BioNetGen images and runs `test_container.py` in each |
| the arm64 image | ubuntu-24.04-arm | builds the arm64 image and checks inside it |
| bngsim main, built and tested in the image | ubuntu-latest | builds bngsim's main through the CLI and runs a few of its test files; also weekly, as main moves |

## Requirements

Docker (Docker Desktop on macOS), and Python 3.10 or newer for the CLI, which uses the standard library only. On Apple Silicon the amd64 images run under emulation: a cold build of bngsim there takes about 8 minutes, and 1.5 with a warm cache.
