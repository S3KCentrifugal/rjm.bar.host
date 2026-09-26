#!/usr/bin/env bash
# Smoke-test an rjm-bar-host image before it is published.
#
#   scripts/smoke_test.sh <image>
#
# Proves, in about a minute and without downloading the game:
#   1. the engine lists its stock AI (BARb stable)
#   2. a custom AI from a GitHub release installs and the engine can load it
#   3. a mounted custom-AI folder is linked into AI/Skirmish and loadable
#   4. a Windows-only AI folder is refused with a clear error, not skipped
#   5. SPADS is installed and its configuration renders
set -euo pipefail

IMAGE="${1:?usage: smoke_test.sh <image>}"
AI_RELEASE="${SMOKE_AI_RELEASE:-github:S3KCentrifugal/CircuitAI@newest}"
AI_NAME="${SMOKE_AI_NAME:-SMRTBARb}"

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
pass() { echo "ok - $*"; }

# Named volumes rather than bind mounts: they behave the same on a Linux runner
# and under Docker Desktop, where host temp paths do not map cleanly.
vol="rjm-bar-host-smoke-$$"
trap 'docker volume rm -f "$vol-mounted" "$vol-windows" > /dev/null 2>&1 || true' EXIT

# 1. stock AI
out="$(docker run --rm "$IMAGE" list-ais 2>&1)" || { echo "$out"; fail "list-ais failed"; }
grep -Eq '^#[[:space:]]+BARb[[:space:]]+stable[[:space:]]' <<< "$out" || { echo "$out"; fail "BARb stable not listed"; }
pass "engine lists BARb stable"

# 2. custom AI from a release
out="$(docker run --rm -e GITHUB_TOKEN="${GITHUB_TOKEN:-}" -e BARHOST_AI_ARCHIVES="$AI_RELEASE" "$IMAGE" list-ais 2>&1)" \
	|| { echo "$out"; fail "installing $AI_RELEASE failed"; }
grep -Eq "^#[[:space:]]+${AI_NAME}[[:space:]]+stable[[:space:]]" <<< "$out" || { echo "$out"; fail "$AI_NAME not listed after install"; }
pass "$AI_RELEASE installs and $AI_NAME loads"

# 3. mounted folder: reuse the stock NullAI's files under a new name
docker run --rm --user 0 -v "$vol-mounted:/export" "$IMAGE" sh -c '
	cp -r "$BAR_ENGINE_DIR/AI/Skirmish/NullAI" /export/SmokeAI &&
	sed -i "s/NullAI/SmokeAI/" /export/SmokeAI/*/AIInfo.lua'
out="$(docker run --rm -v "$vol-mounted:/bar/custom-ai:ro" "$IMAGE" list-ais 2>&1)" \
	|| { echo "$out"; fail "mounted custom AI failed to install"; }
grep -Eq '^#[[:space:]]+SmokeAI[[:space:]]' <<< "$out" || { echo "$out"; fail "mounted SmokeAI not listed"; }
pass "mounted AI folder is linked and loadable"

# 4. a Windows build must be refused
docker run --rm --user 0 -v "$vol-windows:/export" "$IMAGE" sh -c '
	mkdir -p /export/WinOnly/stable &&
	echo "return {}" > /export/WinOnly/stable/AIInfo.lua &&
	: > /export/WinOnly/stable/SkirmishAI.dll'
if out="$(docker run --rm -v "$vol-windows:/bar/custom-ai:ro" "$IMAGE" install-ai 2>&1)"; then
	echo "$out"; fail "a Windows-only AI was accepted"
fi
grep -q "is a Windows build" <<< "$out" || { echo "$out"; fail "Windows-only AI refused without the explanation"; }
pass "Windows-only AI refused with an explanation"

# 5. SPADS
out="$(docker run --rm -e SPADS_LOBBY_LOGIN=smoke -e SPADS_LOBBY_PASSWORD=smoke -e SPADS_OWNER_LOBBY_LOGIN=smoke \
	--entrypoint /opt/rjm-bar-host/spads/run_spads.sh "$IMAGE" --check 2>&1)" \
	|| { echo "$out"; fail "SPADS configuration check failed"; }
pass "SPADS installed and configuration renders"

echo "smoke test passed: $IMAGE"
