# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the headless BAR match runner.
#
#   engine-build -> ai-build -> runtime
#                            \-> export   (extract the install tree locally)
#
# The staging is not cosmetic. It exists so a change to the AI -- the thing that
# changes several times a day -- recompiles only the AI, while the engine's
# object files stay in a cached layer. The engine is by far the larger half.
#
# Why engine and AI are built in one tree at all: s3k-CircuitAI/CMakeLists.txt:12
# pulls ${CMAKE_SOURCE_DIR}/rts/ sources, so the AI is a subdirectory of an
# engine checkout rather than a standalone project; and CircuitAI's README:25
# records that a libSkirmishAI.so built against a different engine than the
# libAIInterface.so beside it yields an AI that is dead at match start. Building
# both from one configuration makes that pairing structural.
#
# Sources arrive as named build contexts, so neither repository has to be copied
# into the main context:
#
#   docker buildx build \
#     --build-context engine-src=./engine-src \
#     --build-context ai-src=./ai-src \
#     -f docker/runtime/Dockerfile .

# Pinned by digest from RecoilEngine docker-build-v2/images_versions.sh.
# Ubuntu 18.04 + gcc-13, mostly static deps, deliberately old glibc
# (images/all-linux/Dockerfile:1-3). Bump deliberately: moving the engine
# silently invalidates the AI interface pairing.
ARG BUILD_IMAGE=ghcr.io/beyond-all-reason/recoil-build-amd64-linux@sha256:31b3667ba0d37620b4ad9b45d1840c8d3e95317f2b233044d2e17697ce8fe61d
ARG RUNTIME_BASE=debian:stable-slim


# ---------------------------------------------------------------------------
# Stage 1: the engine, with the AI interface and C++ wrapper, but no Skirmish AI.
# Cached on the engine source alone, so it survives every AI-only change.
# ---------------------------------------------------------------------------
FROM ${BUILD_IMAGE} AS engine-build

WORKDIR /build
COPY --from=engine-src . /build/src

# Drop the vendored Skirmish AI submodules (BARb, CircuitAI, CppTestAI). AIs are
# discovered by globbing */CMakeLists.txt -- rts/build/cmake/Util.cmake:169,
# despite the name get_list_of_submodules -- so removing the directories leaves
# this stage with no Skirmish AI. The trailing slash keeps AI/Skirmish/CMakeLists.txt.
#
# AI_EXCLUDE_REGEX is deliberately NOT used for this: it is also matched against
# the C interface and the C++ wrapper (AI/Interfaces/C/CMakeLists.txt:26,81 and
# AI/Wrappers/Cpp/CMakeLists.txt:22), both of which CircuitAI links against, so
# a broad pattern would quietly remove its own dependencies.
#
# BUILD_spring-legacy / -dedicated default ON (rts/builds/CMakeLists.txt:76,
# 105-107). Legacy is the full graphical engine and the largest target here;
# dedicated compiles zero ExternalAI sources and cannot host a Skirmish AI at
# all. Neither is wanted.
RUN --mount=type=cache,target=/build/cache,sharing=locked \
    set -eux; \
    rm -rf /build/src/AI/Skirmish/*/; \
    cmake --fresh -S /build/src -B /build/out \
      -DCMAKE_INSTALL_PREFIX:PATH=/build/out/install \
      -DAI_TYPES=NATIVE \
      -DBUILD_spring-legacy=OFF \
      -DBUILD_spring-dedicated=OFF \
      -DUSERDOCS_PLAIN=ON \
      -DCMAKE_BUILD_TYPE=RELWITHDEBINFO \
      -DCMAKE_CXX_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG" \
      -DCMAKE_C_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG" \
      -G Ninja; \
    cmake --build /build/out


# ---------------------------------------------------------------------------
# Stage 2: add the Skirmish AI and build it against that exact engine.
#
# Reconfigures in place rather than from scratch: /build/out already holds the
# engine's object files from the cached layer above, so Ninja compiles only the
# newly-discovered AI target. `cmake --fresh` here would throw that away.
# ---------------------------------------------------------------------------
FROM engine-build AS ai-build

# Directory name under AI/Skirmish/. Becomes the CMake target name and the
# installed path, and must match `shortName` in the AI's data/AIInfo.lua.
ARG AI_DIR_NAME=BARb

COPY --from=ai-src . /build/src/AI/Skirmish/${AI_DIR_NAME}/

RUN --mount=type=cache,target=/build/cache,sharing=locked \
    set -eux; \
    AI_VERSION="$(head -n1 /build/src/AI/Skirmish/${AI_DIR_NAME}/VERSION)"; \
    echo "building ${AI_DIR_NAME} version ${AI_VERSION}"; \
    cmake -S /build/src -B /build/out \
      -DCMAKE_INSTALL_PREFIX:PATH=/build/out/install \
      -DAI_TYPES=NATIVE \
      -DBUILD_spring-legacy=OFF \
      -DBUILD_spring-dedicated=OFF \
      -DUSERDOCS_PLAIN=ON \
      -DCMAKE_BUILD_TYPE=RELWITHDEBINFO \
      -DCMAKE_CXX_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG" \
      -DCMAKE_C_FLAGS_RELWITHDEBINFO="-O3 -g -DNDEBUG" \
      -G Ninja; \
    cmake --build /build/out; \
    cmake --install /build/out; \
    rm -rf /build/out/install/lib /build/out/install/include /build/out/install/share; \
    test -x /build/out/install/spring-headless; \
    test -f "/build/out/install/AI/Interfaces/C/0.1/libAIInterface.so"; \
    test -f "/build/out/install/AI/Skirmish/${AI_DIR_NAME}/${AI_VERSION}/libSkirmishAI.so"; \
    echo "${AI_VERSION}" > /build/out/install/.ai-version


# ---------------------------------------------------------------------------
# Stage 3: extraction target for local use.
#   docker buildx build --target export --output type=local,dest=./engine ...
# ---------------------------------------------------------------------------
FROM scratch AS export
COPY --from=ai-build /build/out/install/ /


# ---------------------------------------------------------------------------
# Stage 4: the runtime image.
#
# glibc: the build stage is Ubuntu 18.04 on purpose and links most deps
# statically, so Debian stable's newer glibc runs those binaries. The reverse
# would not hold.
# ---------------------------------------------------------------------------
FROM ${RUNTIME_BASE} AS runtime

ARG ENGINE_DIR=/opt/engine
ARG AI_DIR_NAME=BARb
ARG AI_VERSION=stable
# Set to 0 to skip the AI-loads smoke test below (not recommended).
ARG VERIFY_AI=1

ENV DEBIAN_FRONTEND=noninteractive

# Runtime libraries only, no toolchain. p7zip is not needed -- the engine links
# its own 7zip. curl/ca-certificates are for pr-downloader's rapid fetches.
RUN apt-get update \
 && apt-get install --no-install-recommends --yes \
      ca-certificates \
      libcurl4 \
      libstdc++6 \
      libfreetype6 \
      libfontconfig1 \
      zlib1g \
      python3 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ai-build /build/out/install/ ${ENGINE_DIR}/

COPY docker/runtime/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scripts/run_match.py /usr/local/bin/run_match.py
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/run_match.py \
 && chmod +x ${ENGINE_DIR}/spring-headless \
 && if [ -f ${ENGINE_DIR}/pr-downloader ]; then chmod +x ${ENGINE_DIR}/pr-downloader; fi

# Fail the build, not a match, if the AI cannot be loaded.
#
# --list-skirmish-ais (SpringApp.cpp:138) makes the engine enumerate what it can
# actually load, so a missing runtime library or a mismatched interface surfaces
# here in seconds instead of five minutes into a game.
#
# AILibraryManager::OutputSkirmishAIInfo (AILibraryManager.cpp:450-466) prints
#   # [Name]  [Version]  [Interface-name]  [Interface-version]
#   # BARb    stable     C                 0.1
# (format confirmed against a real engine release), so this asserts the exact AI
# and version are present rather than merely that something was listed. It also
# rejects the duplicate-AI warning the same function emits (:474), which is what
# two copies of one AI would look like.
RUN if [ "${VERIFY_AI}" = "1" ]; then \
      set -eux; \
      ldd ${ENGINE_DIR}/spring-headless; \
      mkdir -p /tmp/verify; \
      HOME=/tmp/verify ${ENGINE_DIR}/spring-headless \
        --isolation --isolation-dir ${ENGINE_DIR} \
        --write-dir /tmp/verify \
        --list-skirmish-ais > /tmp/verify/ais.txt 2>&1 || \
        { echo "--- engine output ---"; cat /tmp/verify/ais.txt; \
          echo "FAIL: the engine could not enumerate skirmish AIs"; exit 1; }; \
      cat /tmp/verify/ais.txt; \
      if grep -q "Duplicate Skirmish AI Info" /tmp/verify/ais.txt; then \
        echo "FAIL: duplicate Skirmish AI -- two copies of one AI are installed"; exit 1; \
      fi; \
      grep -Eq "^#[[:space:]]+${AI_DIR_NAME}[[:space:]]+${AI_VERSION}[[:space:]]" /tmp/verify/ais.txt || \
        { echo "FAIL: ${AI_DIR_NAME} ${AI_VERSION} was not listed -- the AI did not load"; exit 1; }; \
      rm -rf /tmp/verify; \
    fi

# Non-root. Nothing here needs privilege: both ports used (53200, 53210) are
# unprivileged and bound to loopback.
RUN useradd --system --create-home --uid 10001 barhost \
 && mkdir -p /spring-data /out \
 && chown -R barhost:barhost /spring-data /out
USER barhost

ENV BARHOST_ENGINE=${ENGINE_DIR}/spring-headless \
    BARHOST_ENGINE_DIR=${ENGINE_DIR} \
    BARHOST_DATA_DIR=/spring-data \
    BARHOST_WRITE_DIR=/out \
    BARHOST_AI_SHORTNAME=${AI_DIR_NAME} \
    BARHOST_AI_VERSION=${AI_VERSION} \
    HOME=/home/barhost

VOLUME ["/spring-data", "/out"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["match"]
