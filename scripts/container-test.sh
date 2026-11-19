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
# printed.
#
# Settings, from the environment:
#   WORKERS       pytest-xdist's workers (default 3)
#   BUILD_JOBS    compiles at once (default the image's, 2)
#   BNGSIM_EXTRA  the extra `uv sync` installs (default test; the Ubuntu image
#                 sets dev, as bngsim's CI does)
#   HOST_UID, HOST_GID  the user and group to hand /src and /out back to
# With a volume at /ccache, the build compiles through ccache, as CI's does,
# and a later run of the same tree recompiles only what changed.
set -euo pipefail
# The container runs as root, so what it writes (the tree's .venv and build, the
# logs) is root's. On a Linux host the caller then cannot delete it: hand it back
# to the caller's user on the way out (HOST_UID, HOST_GID; the CLI sets both).
if [ -n "${HOST_UID:-}" ]; then
    HOST_GID="${HOST_GID:-$HOST_UID}"
    trap 'chown -R "$HOST_UID:$HOST_GID" /src /out 2>/dev/null || true' EXIT
fi
WORKERS="${WORKERS:-3}"
export CMAKE_BUILD_PARALLEL_LEVEL="${BUILD_JOBS:-${CMAKE_BUILD_PARALLEL_LEVEL:-2}}"
EXTRA="${BNGSIM_EXTRA:-test}"
if [ ! -d /src/python/bngsim ]; then
    [ -d /tree/python/bngsim ] || { echo "no bngsim tree at /tree (set BNGSIM_TREE) or /src" >&2; exit 2; }
    mkdir -p /src
    tar -C /tree --exclude=./.venv --exclude=./build --exclude=./.git --exclude='*.log' \
        --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.so' -cf - . | tar -C /src -xf -
fi
if command -v ccache >/dev/null && [ -d /ccache ] && [ -w /ccache ]; then
    # The tree is always at /src, so its objects hash alike from run to run.
    export CCACHE_DIR=/ccache CCACHE_BASEDIR=/src CCACHE_NOHASHDIR=true CCACHE_MAXSIZE=3G
    export CMAKE_C_COMPILER_LAUNCHER=ccache CMAKE_CXX_COMPILER_LAUNCHER=ccache
    ccache --zero-stats >/dev/null
fi
mkdir -p /out
: > /out/build.log
cd /src
[ "$#" -gt 0 ] || set -- python/tests
# antimony ships no Linux aarch64 wheel, so on arm64 bngsim installs without it.
# The tests that need it skip or fail there (161 of bngsim's test files mention
# it); the amd64 images run the whole suite.
SKIP=()
if [ "$(uname -m)" = aarch64 ]; then
    SKIP=(--no-install-package antimony)
    echo "arm64: installing without antimony (no Linux aarch64 wheel); the tests that need it skip or fail here" \
        | tee -a /out/build.log
fi
uv sync --frozen --extra "$EXTRA" "${SKIP[@]}" >> /out/build.log 2>&1 || { tail -50 /out/build.log; exit 1; }
if [ -n "${CCACHE_DIR:-}" ]; then
    ccache --print-stats | awk -F'\t' '$1 == "direct_cache_hit" || $1 == "preprocessed_cache_hit" { h += $2 }
        $1 == "cache_miss" { m = $2 } END { printf "ccache: %d of %d compiles from the cache\n", h, h + m }' \
        | tee -a /out/build.log
fi
# RoadRunner's wheel links libpython by name; where the loader cannot find it
# (Ubuntu 26.04 has no system 3.12), bngsim's own CI script makes it findable.
# The script writes its fix to $GITHUB_ENV, as on GitHub Actions; give it one, and
# read back what it wrote (LD_LIBRARY_PATH).
if [ -f ci/libpython_on_loader_path.sh ]; then
    GITHUB_ENV=$(mktemp)
    GITHUB_ENV="$GITHUB_ENV" bash ci/libpython_on_loader_path.sh .venv/bin/python >> /out/build.log 2>&1 || true
    while IFS='=' read -r name value; do
        [ "$name" = LD_LIBRARY_PATH ] && export LD_LIBRARY_PATH="$value"
    done < "$GITHUB_ENV"
    rm -f "$GITHUB_ENV"
fi
# The copy has no .git, and a test asks git about the tree it runs in: make it a
# work tree, as a checkout is. After the build, which reads no git; git refuses a
# tree another user owns, as /src is on a Linux host, unless told it is safe.
if [ ! -e .git ] && command -v git >/dev/null; then
    git config --global --add safe.directory /src
    git init -q .
fi
.venv/bin/python -c "import bngsim, platform; print(bngsim.__version__, platform.machine(), platform.libc_ver())"
# -rfEp: the failed, errored and passed ids, so the CLI can say which standing
# failure passed.
.venv/bin/python -m pytest -q -p no:cacheprovider -n "$WORKERS" -rfEp "$@" > /out/pytest.log 2>&1 || true
grep -E '^FAILED|^ERROR|[0-9]+ (passed|failed)' /out/pytest.log | tail -40
