#!/bin/bash
# Run inside a bngsim-linux image: build a bngsim tree and run its tests.
#
#   container-test.sh [pytest targets...]      (default: python/tests)
#
# The tree is read from /tree (mounted read-only) and copied to /src, without
# its .venv, build, .git, logs and compiled cores, so the build is the
# container's own and nothing is written back to the host. If /src already
# holds a tree (the CLI copies one there), it is used as it is. The logs go to
# /out: build.log (uv sync) and pytest.log; the summary and the failures are
# printed. WORKERS (default 3) sets pytest-xdist's workers; BUILD_JOBS (default
# the image's 2) the compiles run at once.
set -euo pipefail
WORKERS="${WORKERS:-3}"
export CMAKE_BUILD_PARALLEL_LEVEL="${BUILD_JOBS:-${CMAKE_BUILD_PARALLEL_LEVEL:-2}}"
if [ ! -d /src/python/bngsim ]; then
    [ -d /tree/python/bngsim ] || { echo "no bngsim tree at /tree (set BNGSIM_TREE) or /src" >&2; exit 2; }
    mkdir -p /src
    tar -C /tree --exclude=./.venv --exclude=./build --exclude=./.git --exclude='*.log' \
        --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.so' -cf - . | tar -C /src -xf -
fi
mkdir -p /out
cd /src
[ "$#" -gt 0 ] || set -- python/tests
uv sync --frozen --extra test > /out/build.log 2>&1 || { tail -50 /out/build.log; exit 1; }
.venv/bin/python -c "import bngsim, platform; print(bngsim.__version__, platform.machine(), platform.libc_ver())"
.venv/bin/python -m pytest -q -p no:cacheprovider -n "$WORKERS" -rf "$@" > /out/pytest.log 2>&1 || true
grep -E '^FAILED|^ERROR|[0-9]+ (passed|failed)' /out/pytest.log | tail -40
