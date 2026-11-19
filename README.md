<img width="1870" height="841" alt="76b6a44b-fce6-43ba-9d2c-8878dd0c587e" src="https://github.com/user-attachments/assets/a793c0f9-419d-45c5-ae11-1db924514c2c" />

# bngsim-docker

Docker images and a small CLI for running [bngsim](https://github.com/lanl/bngsim)'s tests on Linux.

A fix that depends on rounding can pass on Apple Silicon and fail on CI's x86-64 Linux. These images let a bngsim tree be built from source and tested on Linux, on a Mac or anywhere Docker runs, before its pull request is opened. They are the images [bngsim-preflights](https://github.com/Montana/bngsim-preflights)' `linux_check.py` runs (`bngsim-linux-amd64:latest`), written down so they can be rebuilt the same way.

| image | what it is | platforms |
|---|---|---|
| `bngsim-linux-amd64:latest` | the toolchain: Debian 12, Python 3.12.16, gcc 12.2, CMake 3.25, Ninja, SuiteSparse 5.12 (KLU), uv 0.12.18 | `linux/amd64` |
| `bngsim-linux-arm64:latest` | the same, for arm64 | `linux/arm64` |
| `bngsim-linux-bionetgen:latest` | the amd64 image plus BioNetGen 2.9.3 (BNG2.pl, run_network, NFsim), sha256-checked, with `BNGPATH` set, so the tests that need BNG2.pl run instead of skipping | `linux/amd64` (BioNetGen ships x86-64 binaries) |

The images hold only the toolchain. Each run copies a bngsim tree in, without its `.venv`, `build`, `.git`, logs and compiled cores, and builds it there with `uv sync`. So the build is the container's own, and nothing is written back to the tree.

## Quick start

```bash
git clone https://github.com/Montana/bngsim-docker && cd bngsim-docker

python bngsim_docker.py build                        # bngsim-linux-amd64:latest
python bngsim_docker.py build --bionetgen            # and bngsim-linux-bionetgen:latest on it
python bngsim_docker.py probe                        # does a container start?

python bngsim_docker.py test --tree ~/work/bngsim    # the whole of python/tests
python bngsim_docker.py test --tree ~/work/bngsim --tests python/tests/test_x.py
python bngsim_docker.py test --tree ~/work/bngsim --bionetgen
python bngsim_docker.py shell --tree ~/work/bngsim   # a shell, the tree copied to /src
```

`test` writes `build.log`, `pytest.log` and `docker.log` under `--out` (default `docker-check/`). It sorts the failures:
- one listed in `known-failures.txt` fails in this container on main too, and is reported as standing;
- any other is the tree's.

The exit status is 1 only if the tree has a failure of its own, or the run did not finish. `--dry-run` prints every command and runs none.

Or with Compose:

```bash
docker compose build linux-amd64 && docker compose build linux-bionetgen
BNGSIM_TREE=~/work/bngsim docker compose run --rm suite
BNGSIM_TREE=~/work/bngsim docker compose run --rm suite python/tests/test_x.py
BNGSIM_TREE=~/work/bngsim docker compose run --rm suite-bionetgen
BNGSIM_TREE=~/work/bngsim docker compose run --rm shell
```

The CLI and Compose run the same script, [`scripts/container-test.sh`](scripts/container-test.sh). `WORKERS` sets pytest-xdist's workers (default 3), and `BUILD_JOBS` the compiles run at once (default 2).

## Things that went wrong, and what the tools do about them

- **Docker Desktop that answers but starts nothing.** After an update, Docker Desktop on macOS can answer `docker info` with no container ever starting. `probe` asks for a real container (`uname -m`) instead. `doctor` then quits Docker Desktop by its full name (`osascript -e 'quit app "Docker Desktop"'`), stops `com.docker.backend`, opens it again, and probes once more. Quitting it as "Docker" does not do it.
- **The compiler killed for memory.** ExprTk's translation unit (`src/expression.cpp`) alone takes several GB to compile. With three compiles at once, `cc1plus` was killed (`Killed signal terminated program cc1plus`) in Docker Desktop's default 8 GB VM. The images compile two at a time. Raise `BUILD_JOBS` only with the VM's memory, and run one bngsim build at a time.
- **A relative `BNGPATH`** fails the tests that run BNG2.pl from another directory, so the BioNetGen image sets an absolute one.
- **Failures that are the container's, not the change's.** A handful of tests fail in this container on bngsim's main too. They're listed, each with its reason, in [`known-failures.txt`](known-failures.txt), so a red run says at once whether it is the tree. Add a line when main shows a new one here, and remove it when main fixes it.

## Tests

```bash
python -m pip install pytest
python -m pytest -q tests                                            # no image needed
BNGSIM_DOCKER_IMAGE=bngsim-linux-amd64:latest python -m pytest -q tests/test_container.py
BNGSIM_DOCKER_IMAGE=bngsim-linux-bionetgen:latest python -m pytest -q tests/test_container.py
```

- `tests/test_cli.py`: the commands the CLI runs (pure functions and `--dry-run`), how it reads a pytest run, and how it sorts the failures.
- `tests/test_images.py`: every version pinned exactly; BioNetGen checked by its sha256; `BNGPATH` absolute; Compose naming the images the CLI runs.
- `tests/test_container.py`: inside a built image: the architecture, the pinned Python and uv, the toolchain, KLU's header and library, the script's refusal of a missing tree, and BNG2.pl, run_network and NFsim.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs these, plus four more checks:
- shellcheck and hadolint;
- builds the amd64 and BioNetGen images and checks inside them;
- builds the arm64 image on an arm64 runner;
- builds bngsim's main in the image and runs two of its test files through the CLI. That last check runs weekly too, as bngsim's main moves.

## Requirements

Docker (Docker Desktop on macOS), and Python 3.10 or newer for the CLI, which uses the standard library only. On Apple Silicon the amd64 images run under emulation: building bngsim there takes about 15 minutes.
