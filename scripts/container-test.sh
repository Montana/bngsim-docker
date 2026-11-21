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
# The copy has no .git. Where the tree came from a checkout, make it one, with
# exactly the files that checkout tracks committed, before the build: bngsim's
# build stamps the commit it is built from, and tests ask git what is tracked
# and where the work tree is. (The CLI writes the list; from /tree, under
# Compose, it is read here.) git refuses a tree another user owns, as /src is
# on a Linux host, unless told it is safe. A tree from no checkout is left as it
# is, and the tests that need git skip, as they do outside a checkout.
if [ ! -e .git ] && command -v git >/dev/null; then
    git config --global --add safe.directory '*'
    if [ ! -f .bngsim-docker-tracked ] && [ -d /tree/.git ]; then
        git -C /tree ls-files -z > .bngsim-docker-tracked
        git -C /tree rev-parse HEAD > .bngsim-docker-head 2>/dev/null || true
    fi
    if [ -f .bngsim-docker-tracked ]; then
        mv .bngsim-docker-tracked /tmp/tracked
        head=$(cat .bngsim-docker-head 2>/dev/null || echo unknown)
        rm -f .bngsim-docker-head
        git init -q .
        # Only the listed files the copy holds (the copy leaves out logs and builds).
        tr '\0' '\n' < /tmp/tracked | while IFS= read -r f; do [ -f "$f" ] && printf '%s\0' "$f"; done \
            | git add --pathspec-from-file=- --pathspec-file-nul
        git -c user.name=bngsim-docker -c user.email=bngsim-docker@localhost \
            commit -q --no-verify -m "bngsim-docker copy of $head"
    fi
fi
[ "$#" -gt 0 ] || set -- python/tests
# antimony ships no Linux aarch64 wheel, so on arm64 uv sync leaves it out, and
# the wheel the image built from source (/opt/antimony) is installed after it.
# An image without one (built before it did) runs without antimony: the tests
# that need it skip or fail there (161 of bngsim's test files mention it).
SKIP=()
if [ "$(uname -m)" = aarch64 ]; then
    SKIP=(--no-install-package antimony)
fi
uv sync --frozen --extra "$EXTRA" "${SKIP[@]}" >> /out/build.log 2>&1 || { tail -50 /out/build.log; exit 1; }
if [ "$(uname -m)" = aarch64 ]; then
    wheels=(/opt/antimony/antimony-*.whl)
    wheel=""
    if [ -f "${wheels[0]}" ]; then wheel="${wheels[0]}"; fi
    locked=$(sed -n '/^name = "antimony"$/{n;s/^version = "\(.*\)"$/\1/p;}' uv.lock 2>/dev/null | head -1)
    if [ -n "$wheel" ]; then
        built=$(basename "$wheel" | cut -d- -f2)
        uv pip install --python .venv/bin/python --no-deps "$wheel" >> /out/build.log 2>&1 \
            || { tail -50 /out/build.log; exit 1; }
        note="arm64: antimony $built, built from source in the image (PyPI has no Linux aarch64 wheel)"
        if [ -n "$locked" ] && [ "$locked" != "$built" ]; then note="$note; uv.lock pins $locked"; fi
        echo "$note" | tee -a /out/build.log
    else
        echo "arm64: installing without antimony (no Linux aarch64 wheel, and this image predates its source build; rebuild it); the tests that need it skip or fail here" \
            | tee -a /out/build.log
    fi
fi
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
.venv/bin/python -c "import bngsim, platform; print(bngsim.__version__, platform.machine(), platform.libc_ver())"
# -rfEp: the failed, errored and passed ids (junit.xml: each failure's message), so the CLI can say which standing
# failure passed.
.venv/bin/python -m pytest -q -p no:cacheprovider -n "$WORKERS" -rfEp --junitxml=/out/junit.xml "$@" > /out/pytest.log 2>&1 || true
# Under xdist a path that names no file collects nothing, and says only this.
# (Before the summary: with nothing to show, its grep ends the script.)
if grep -q 'no tests ran' /out/pytest.log; then
    echo "no tests ran: check the test paths ($*) name files in the tree" >&2
fi
grep -E '^FAILED|^ERROR|[0-9]+ (passed|failed)' /out/pytest.log | tail -40
