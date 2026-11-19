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
#   SuiteSparse 5.12 (KLU), uv 0.12.18.
#
#   docker build --platform linux/amd64 -t bngsim-linux-amd64:latest -f images/Dockerfile images
#   docker build --platform linux/arm64 -t bngsim-linux-arm64:latest -f images/Dockerfile images
ARG PYTHON_VERSION=3.12.16
FROM python:${PYTHON_VERSION}-bookworm

ARG UV_VERSION=0.12.18

# build-essential: gcc/g++ 12 for the core and for codegen's cc backend.
# libsuitesparse-dev: KLU, so the sparse linear solver is built (BNGSIM_ENABLE_KLU).
# git: bngsim's build reads its own commit; tests that check vendoring call git.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        libsuitesparse-dev \
        ninja-build \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# A tree is copied to /src for each run; its build and venv stay in the container.
# Two compiles at a time: ExprTk's translation unit (src/expression.cpp) alone
# takes several GB, and three at once had cc1plus killed in Docker Desktop's
# default 8 GB VM. container-test.sh takes BUILD_JOBS to change it.
ENV UV_LINK_MODE=copy \
    CMAKE_BUILD_PARALLEL_LEVEL=2 \
    SUNLOGGER_WARNING_FILENAME=/dev/null
WORKDIR /src
CMD ["bash"]
