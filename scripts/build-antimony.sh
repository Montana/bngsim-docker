#!/bin/bash
# Build antimony's Python wheel from source, for Linux aarch64, where PyPI has
# none (antimony publishes no sdist either):
#
#   build-antimony.sh VERSION WHEEL_DIR
#
# As antimony's own CI builds it: libantimony linked to a static libSBML (with
# the C++ namespace and the comp, layout and render packages antimony needs)
# and a static SBMLNetwork, at the commits sys-bio/libroadrunner-deps pins for
# antimony (a839656b, 2026-11-11). Debian's libSBML is built without the
# namespace, so it cannot be used. What the library needs at run time is then
# only libz and the C++ runtime.
#
# Needs: a C++ compiler, CMake, Ninja, git, libexpat1-dev and zlib1g-dev.
set -euo pipefail
VERSION="${1:?antimony version, e.g. 3.3.0}"
WHEELS="${2:?the directory to write the wheel to}"

LIBSBML_COMMIT=96d88cca8329d960eb9f77de33c2b7e7ee6dfba7
SBMLNETWORK_COMMIT=e4818e636c7cf382aed3c5dcf6ee2228a67533dc

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
lib="/usr/lib/$(gcc -print-multiarch)"
jobs="${BUILD_JOBS:-$(nproc)}"

# A commit, fetched alone: its hash names its whole tree.
fetch() {
    git init -q "$3"
    git -C "$3" remote add origin "$1"
    git -C "$3" fetch -q --depth 1 origin "$2"
    git -C "$3" checkout -q FETCH_HEAD
}
fetch https://github.com/sbmlteam/libsbml "$LIBSBML_COMMIT" "$work/libsbml"
fetch https://github.com/sys-bio/SBMLNetwork "$SBMLNETWORK_COMMIT" "$work/sbmlnetwork"
fetch https://github.com/sys-bio/antimony "refs/tags/v$VERSION" "$work/antimony"

common=(-G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON
        -DEXPAT_INCLUDE_DIR=/usr/include -DEXPAT_LIBRARY="$lib/libexpat.a")

cmake -S "$work/libsbml" -B "$work/b-libsbml" "${common[@]}" -DCMAKE_INSTALL_PREFIX="$work/deps" \
    -DLIBSBML_SKIP_SHARED_LIBRARY=ON -DWITH_CPP_NAMESPACE=ON -DWITH_SWIG=OFF \
    -DWITH_EXPAT=ON -DWITH_LIBXML=OFF -DWITH_ZLIB=ON -DWITH_BZIP2=OFF \
    -DLIBZ_INCLUDE_DIR=/usr/include -DLIBZ_LIBRARY="$lib/libz.a" \
    -DENABLE_ARRAYS=ON -DENABLE_COMP=ON -DENABLE_DISTRIB=ON -DENABLE_FBC=ON -DENABLE_GROUPS=ON \
    -DENABLE_LAYOUT=ON -DENABLE_QUAL=ON -DENABLE_RENDER=ON >/dev/null
cmake --build "$work/b-libsbml" -j "$jobs" >/dev/null
cmake --install "$work/b-libsbml" >/dev/null

cmake -S "$work/sbmlnetwork" -B "$work/b-sbmlnetwork" "${common[@]}" -DCMAKE_INSTALL_PREFIX="$work/deps" \
    -DCMAKE_PREFIX_PATH="$work/deps" -DDEPENDENCIES_INSTALL_PREFIX="$work/deps" \
    -DBUILD_SHARED_LIBS=OFF -DBUILD_STATIC_LIBS=ON -DWITH_PYTHON=OFF -DWITH_ZLIB=OFF >/dev/null
cmake --build "$work/b-sbmlnetwork" -j "$jobs" >/dev/null
cmake --install "$work/b-sbmlnetwork" >/dev/null

# libSBML's static library leaves libz to whoever links it.
cmake -S "$work/antimony" -B "$work/b-antimony" "${common[@]}" -DCMAKE_INSTALL_PREFIX="$work/install" \
    -DANT_DEPENDENCIES_INSTALL_PREFIX="$work/deps" -DCMAKE_PREFIX_PATH="$work/deps" \
    -DLIBSBML_LIBRARIES="$lib/libz.a" \
    -DWITH_QTANTIMONY=OFF -DWITH_PYTHON=ON -DWITH_GTEST=OFF >/dev/null
cmake --build "$work/b-antimony" -j "$jobs" >/dev/null
cmake --install "$work/b-antimony" >/dev/null

mkdir -p "$WHEELS"
pip wheel --no-cache-dir --no-deps --wheel-dir "$WHEELS" "$work/install/bindings/python" >/dev/null
ls "$WHEELS"/antimony-"$VERSION"-*.whl
