# syntax=docker/dockerfile:1.7
#
# rjm-bar-host: a reusable Beyond All Reason headless host image.
#
#   engine ─┐
#   spads  ─┴─▶ runtime
#
# One image, two jobs, chosen by the first argument (see entrypoint.sh):
#
#   match   an unattended AI-vs-AI simulation that runs to game over and exits
#   spads   a SPADS autohost on BAR's lobby server that hosts AI bots itself
#   list-ais / install-ai / fetch-content / <any command>   tooling
#
# The engine is BAR's *official* Linux release, not a source build. A lobby host
# must run the same engine build as the players who join it, and a simulation
# result only means something against what people actually play. The version is
# a build argument, pinned in config/engine.pin; bumping it is one reviewed diff.
#
# Custom Skirmish AIs are *not* baked in. They are installed at container start
# into $BAR_ENGINE_DIR/AI/Skirmish -- where a Windows install keeps them,
# data\engine\recoil_<version>\AI\Skirmish -- from a mounted folder, an archive
# or a GitHub release. See scripts/install_ai.py.

ARG RUNTIME_BASE=debian:stable-slim


# ---------------------------------------------------------------------------
# Stage 1: fetch and verify the official engine release.
# ---------------------------------------------------------------------------
FROM ${RUNTIME_BASE} AS engine

ARG ENGINE_VERSION=2026.07.04
# sha256 of recoil_<version>_amd64-linux.7z. An unverified engine is the one
# thing this image must never ship.
ARG ENGINE_SHA256=9824c2c38124e4b90a9b5f7c4e7200c3ea6503c0bd4bff0e0ff46212ec29dcab

RUN apt-get update \
 && apt-get install --no-install-recommends --yes ca-certificates curl 7zip \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    asset="recoil_${ENGINE_VERSION}_amd64-linux.7z"; \
    curl -fsSL --retry 5 --retry-all-errors -o "/tmp/${asset}" \
      "https://github.com/beyond-all-reason/RecoilEngine/releases/download/${ENGINE_VERSION}/${asset}"; \
    echo "${ENGINE_SHA256}  /tmp/${asset}" | sha256sum -c -; \
    mkdir -p "/bar/engine/recoil_${ENGINE_VERSION}"; \
    7z x -bd -o"/bar/engine/recoil_${ENGINE_VERSION}" "/tmp/${asset}" > /dev/null; \
    rm "/tmp/${asset}"; \
    # The release ships the graphical client too; a host never runs it.
    rm -f "/bar/engine/recoil_${ENGINE_VERSION}/spring"; \
    test -x "/bar/engine/recoil_${ENGINE_VERSION}/spring-headless"; \
    test -f "/bar/engine/recoil_${ENGINE_VERSION}/libunitsync.so"; \
    test -f "/bar/engine/recoil_${ENGINE_VERSION}/AI/Interfaces/C/0.1/libAIInterface.so"


# ---------------------------------------------------------------------------
# Stage 2: SPADS, from pinned commits.
#
# Not spadsInstaller.pl: it fetches packages over plain HTTP from
# planetspads.free.fr and needs the engine at install time. SPADS is plain Perl,
# so a release is just its files, laid flat in one directory exactly as the
# installer lays them (design section 10.1).
# ---------------------------------------------------------------------------
FROM ${RUNTIME_BASE} AS spads

# SPADS 0.13.55 and the Yaribz libraries it loads.
ARG SPADS_REF=4bbb2b86280a7ce4b6ffd21f718191b1d3ced53a
ARG SIMPLEEVENT_REF=767848e
ARG SIMPLELOG_REF=9f403ee
ARG SPRINGLOBBYPROTOCOL_REF=5f01e12
ARG SPRINGLOBBYINTERFACE_REF=9566525
ARG SPRINGAUTOHOSTINTERFACE_REF=da7f731
# BAR's SPADS configuration repository, for its map start boxes only.
ARG SPADS_CONFIG_BAR_REF=ca95fa1

RUN apt-get update \
 && apt-get install --no-install-recommends --yes ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    mkdir -p /src /opt/spads/etc-stock; \
    cd /src; \
    for pair in \
        "SPADS=${SPADS_REF}" \
        "SimpleEvent=${SIMPLEEVENT_REF}" \
        "SimpleLog=${SIMPLELOG_REF}" \
        "SpringLobbyProtocol=${SPRINGLOBBYPROTOCOL_REF}" \
        "SpringLobbyInterface=${SPRINGLOBBYINTERFACE_REF}" \
        "SpringAutoHostInterface=${SPRINGAUTOHOSTINTERFACE_REF}"; do \
      repo="${pair%%=*}"; ref="${pair#*=}"; \
      git clone -q "https://github.com/Yaribz/${repo}" "${repo}"; \
      git -C "${repo}" checkout -q "${ref}"; \
    done; \
    cp SPADS/src/spads.pl SPADS/src/SpadsConf.pm SPADS/src/SpadsUpdater.pm \
       SPADS/src/SpadsPluginApi.pm SPADS/src/PerlUnitSync.pm \
       SPADS/src/getDefaultModOptions.pl SPADS/src/sequentialSpadsUnitsyncProcess.pl \
       SPADS/var/help.dat SPADS/var/helpSettings.dat SPADS/var/springLobbyCertificates.dat \
       SimpleEvent/SimpleEvent.pm SimpleLog/SimpleLog.pm \
       SpringLobbyProtocol/SpringLobbyProtocol.pm \
       SpringLobbyInterface/SpringLobbyInterface.pm \
       SpringAutoHostInterface/SpringAutoHostInterface.pm \
       /opt/spads/; \
    cp SPADS/etc/*.conf /opt/spads/etc-stock/; \
    # Start boxes per map and team count, curated by BAR for its maps. SPADS
    # refuses to start a choose-in-game room without boxes, and SPADS's own
    # mapBoxes.conf knows no BAR map.
    git clone -q https://github.com/beyond-all-reason/spads_config_bar spads_config_bar; \
    git -C spads_config_bar checkout -q "${SPADS_CONFIG_BAR_REF}"; \
    cp spads_config_bar/etc/mapBoxes.conf /opt/spads/etc-stock/mapBoxes.conf


# ---------------------------------------------------------------------------
# Stage 3: the runtime image.
# ---------------------------------------------------------------------------
FROM ${RUNTIME_BASE} AS runtime

ARG ENGINE_VERSION=2026.07.04

ENV DEBIAN_FRONTEND=noninteractive

# The engine binaries are static apart from libc (checked with ldd), so the
# only runtime packages are for the tooling:
#   python3                                run_match.py, install_ai.py, demo_stats.py
#   perl, FFI::Platypus, IO::Socket::SSL   SPADS (unitsync via FFI; lobby TLS)
#   DBD::SQLite                            SPADS preferences storage
#   tini                                   PID 1: reaps children, forwards signals
RUN apt-get update \
 && apt-get install --no-install-recommends --yes \
      ca-certificates tini \
      python3 \
      perl libffi-platypus-perl libio-socket-ssl-perl libdbd-sqlite3-perl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=engine /bar/engine/ /bar/engine/
COPY --from=spads /opt/spads/ /opt/spads/

COPY docker/runtime/entrypoint.sh /opt/rjm-bar-host/bin/entrypoint.sh
COPY scripts/run_match.py scripts/lan_game.py scripts/lobby_client.py scripts/install_ai.py scripts/demo_stats.py /opt/rjm-bar-host/bin/
COPY docker/spads/ /opt/rjm-bar-host/spads/

# SPADS refuses to start without all nine configuration files. The ones this
# image does not customise are SPADS's own, from the pinned release.
RUN set -eux; \
    for f in commands.conf levels.conf banLists.conf mapBoxes.conf mapLists.conf; do \
      [ -f "/opt/rjm-bar-host/spads/etc/$f" ] || cp "/opt/spads/etc-stock/$f" /opt/rjm-bar-host/spads/etc/; \
    done; \
    chmod +x /opt/rjm-bar-host/bin/* /opt/rjm-bar-host/spads/*.sh /opt/rjm-bar-host/spads/spadsctl; \
    ln -s /opt/rjm-bar-host/spads/spadsctl /opt/rjm-bar-host/bin/spadsctl; \
    perl -I/opt/spads -c /opt/rjm-bar-host/spads/plugins/Autopilot.pm

# Fail the build, not a match, if the engine cannot load its stock AI.
#
# --list-skirmish-ais makes the engine enumerate what it can actually load. Its
# rows read "# <shortName> <version> <interface> <interfaceVersion>"
# (AILibraryManager.cpp OutputSkirmishAIInfo), so this asserts that BARb stable
# loaded, not merely that something was printed.
RUN set -eux; \
    mkdir -p /tmp/verify; \
    HOME=/tmp/verify "/bar/engine/recoil_${ENGINE_VERSION}/spring-headless" \
      --isolation --isolation-dir "/bar/engine/recoil_${ENGINE_VERSION}" \
      --write-dir /tmp/verify --list-skirmish-ais > /tmp/verify/ais.txt 2>&1 \
      || { cat /tmp/verify/ais.txt; exit 1; }; \
    cat /tmp/verify/ais.txt; \
    grep -Eq '^#[[:space:]]+BARb[[:space:]]+stable[[:space:]]' /tmp/verify/ais.txt; \
    rm -rf /tmp/verify

# Non-root. uid/gid 10001 owns every writable path, including the engine's AI
# tree so custom AIs can be installed into it at start. No privileged ports.
RUN useradd --system --create-home --uid 10001 --user-group barhost \
 && mkdir -p /bar/data /bar/out /bar/custom-ai \
 && chown -R barhost:barhost /bar

USER barhost

ENV BAR_ENGINE_VERSION=${ENGINE_VERSION} \
    BAR_ENGINE_DIR=/bar/engine/recoil_${ENGINE_VERSION} \
    BARHOST_DATA_DIR=/bar/data \
    BARHOST_OUT_DIR=/bar/out \
    BARHOST_CUSTOM_AI_DIR=/bar/custom-ai \
    HOME=/home/barhost \
    PATH=/opt/rjm-bar-host/bin:$PATH

LABEL org.opencontainers.image.title="rjm-bar-host" \
      org.opencontainers.image.description="Beyond All Reason headless host: AI simulations and SPADS lobby hosting with custom Skirmish AIs" \
      org.opencontainers.image.source="https://github.com/S3KCentrifugal/rjm.bar.host" \
      io.github.s3kcentrifugal.recoil-version="${ENGINE_VERSION}"

VOLUME ["/bar/data", "/bar/out"]

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/rjm-bar-host/bin/entrypoint.sh"]
CMD ["match"]
