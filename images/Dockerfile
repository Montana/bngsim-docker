# The image bngsim's Linux checks run in (bngsim-linux-amd64 / -arm64).
#
# A fix that depends on rounding can pass on Apple Silicon and fail on CI's
# x86-64 Linux, so the suite is run here too. The image holds only the toolchain:
# each run copies a bngsim tree in and builds it from source with `uv sync`, so
# the build is the container's own, not the host's.
#
# Everything is pinned to the versions the checks were first run with, so a
# rebuild gives the same compiler and the same libm:
#   Debian 12 (bookworm), Python 3.12.16, gcc 12.2, CMake 3.25, Ninja 1.11,
#   SuiteSparse 5.12 (KLU), ccache, uv 0.12.18, and on arm64 antimony 3.2.1
#   (the version bngsim's uv.lock pins), built from source: PyPI has no Linux
#   aarch64 wheel for it.
#
#   docker build --platform linux/amd64 -t bngsim-linux-amd64:latest -f images/Dockerfile images
#   docker build --platform linux/arm64 -t bngsim-linux-arm64:latest -f images/Dockerfile images
ARG PYTHON_VERSION=3.12.16
FROM python:${PYTHON_VERSION}-bookworm

ARG UV_VERSION=0.12.18
ARG ANTIMONY_VERSION=3.2.1
ARG TARGETARCH

# build-essential: gcc/g++ 12 for the core and for codegen's cc backend.
# libsuitesparse-dev: KLU, so the sparse linear solver is built (BNGSIM_ENABLE_KLU).
# git: bngsim's build reads its own commit; tests that check vendoring call git.
# ccache: a run's compiles are kept in a volume, so the next run of a tree
# rebuilds only what changed (scripts/container-test.sh).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        ccache \
        cmake \
        git \
        libexpat1-dev \
        libsuitesparse-dev \
        ninja-build \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# antimony, on arm64: its wheel, built from source (scripts/build-antimony.sh,
# about a minute), for container-test.sh to install after uv sync, so the
# tests that load Antimony models run there too. libexpat1-dev and zlib1g-dev
# are what it links.
# hadolint ignore=DL3022
RUN --mount=type=bind,from=scripts,source=build-antimony.sh,target=/usr/local/bin/build-antimony.sh \
    if [ "$TARGETARCH" = arm64 ]; then bash /usr/local/bin/build-antimony.sh "$ANTIMONY_VERSION" /opt/antimony; fi

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# A tree is copied to /src for each run; its build and venv stay in the container.
# Two compiles at a time: ExprTk's translation unit (src/expression.cpp) alone
# takes several GB, and three at once had cc1plus killed in Docker Desktop's
# default 8 GB VM. container-test.sh takes BUILD_JOBS to change it.
ENV UV_LINK_MODE=copy \
    CMAKE_BUILD_PARALLEL_LEVEL=2 \
    SUNLOGGER_WARNING_FILENAME=/dev/null
# Where the image comes from, and what it is for: Docker Hub and other tools
# read these (the OCI image annotations) to link the image to its source.
LABEL org.opencontainers.image.source="https://github.com/Montana/bngsim-docker" \
      org.opencontainers.image.description="Linux test images for bngsim, the BioNetGen simulation engine from Los Alamos National Laboratory"

# A copy of this file, which the CLI compares with the repository's to say when
# the image is older than its Dockerfile. Last, so no other layer depends on
# it.
# bngsim-test: build bngsim and run its tests in one command (a pull request,
# a branch, main, or a tree mounted at /tree), and container-test.sh, which it
# runs. They come from the repository's scripts/ (a named build context:
# --build-context scripts=scripts), and copies go beside the Dockerfile's for
# the CLI's check that the image is current.
# hadolint ignore=DL3022
COPY --from=scripts --chmod=0755 bngsim-test container-test.sh /usr/local/bin/
# hadolint ignore=DL3022
COPY --from=scripts bngsim-test container-test.sh build-antimony.sh /etc/bngsim-docker/
COPY Dockerfile /etc/bngsim-docker/Dockerfile
WORKDIR /src
CMD ["bash"]
