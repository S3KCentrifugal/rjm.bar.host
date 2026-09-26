#!/usr/bin/env bash
# Build the match image, or just extract the engine + AI install tree.
#
# The build itself lives in docker/runtime/Dockerfile as a multi-stage build, so
# CI and a developer's laptop run exactly the same thing. This script only
# arranges the source checkouts and calls buildx.
#
# Stages:
#   engine-build  the engine, AI interface and C++ wrapper -- cached on the
#                 engine source alone, so an AI-only change skips it entirely
#   ai-build      the Skirmish AI, compiled against that exact engine
#   export        the install tree, for extracting to a local directory
#   runtime       the final image (default)
#
# Usage:
#   scripts/build-engine-ai.sh --engine-src DIR --ai-src DIR [options]
#
# Options:
#   --engine-src DIR   Recoil checkout at the pinned commit (required)
#   --ai-src DIR       the Skirmish AI checkout (required)
#   --out DIR          extract the install tree here instead of building an image
#   --tag NAME         image tag to build (default barhost-match:local)
#   --ai-dir-name NAME directory under AI/Skirmish/ (default from config/engine.pin)
#   --push             push the tag after building
#   --no-verify        skip the AI-loads smoke test in the image build

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Defaults come from the pin file so this script and CI agree.
AI_DIR_NAME="BARb"
AI_VERSION="stable"
if [ -f "$REPO_ROOT/config/engine.pin" ]; then
	# shellcheck disable=SC1091
	set -a; . "$REPO_ROOT/config/engine.pin"; set +a
fi

ENGINE_SRC=""
AI_SRC=""
OUT_DIR=""
TAG="barhost-match:local"
PUSH=0
VERIFY_AI=1

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
	case "$1" in
		--engine-src)   ENGINE_SRC="$2"; shift 2 ;;
		--ai-src)       AI_SRC="$2";     shift 2 ;;
		--out)          OUT_DIR="$2";    shift 2 ;;
		--tag)          TAG="$2";        shift 2 ;;
		--ai-dir-name)  AI_DIR_NAME="$2"; shift 2 ;;
		--push)         PUSH=1;          shift ;;
		--no-verify)    VERIFY_AI=0;     shift ;;
		-h|--help)      usage 0 ;;
		*) echo "unknown argument: $1" >&2; usage 1 ;;
	esac
done

[ -n "$ENGINE_SRC" ] || { echo "--engine-src is required" >&2; exit 1; }
[ -n "$AI_SRC" ]     || { echo "--ai-src is required" >&2; exit 1; }

[ -f "$ENGINE_SRC/rts/builds/headless/CMakeLists.txt" ] || {
	echo "--engine-src does not look like a Recoil checkout: $ENGINE_SRC" >&2; exit 1; }
[ -f "$AI_SRC/CMakeLists.txt" ] || {
	echo "--ai-src has no CMakeLists.txt: $AI_SRC" >&2; exit 1; }
[ -f "$AI_SRC/VERSION" ] || {
	echo "--ai-src has no VERSION file; the installed AI path depends on it" >&2; exit 1; }

RESOLVED_VERSION="$(head -n1 "$AI_SRC/VERSION")"
if [ "$RESOLVED_VERSION" != "$AI_VERSION" ]; then
	echo "note: AI VERSION file says '$RESOLVED_VERSION', config/engine.pin says '$AI_VERSION'" >&2
	echo "      using '$RESOLVED_VERSION'; update the pin if that is the intended version" >&2
	AI_VERSION="$RESOLVED_VERSION"
fi

command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || {
	echo "docker buildx is required (the Dockerfile uses named build contexts)" >&2; exit 1; }

args=(
	buildx build
	--file "$REPO_ROOT/docker/runtime/Dockerfile"
	--build-context "engine-src=$ENGINE_SRC"
	--build-context "ai-src=$AI_SRC"
	--build-arg "AI_DIR_NAME=$AI_DIR_NAME"
	--build-arg "AI_VERSION=$AI_VERSION"
	--build-arg "VERIFY_AI=$VERIFY_AI"
)

if [ -n "$OUT_DIR" ]; then
	mkdir -p "$OUT_DIR"
	echo "==> extracting the install tree to $OUT_DIR"
	args+=(--target export --output "type=local,dest=$OUT_DIR")
else
	echo "==> building image $TAG"
	args+=(--target runtime --tag "$TAG")
	if [ "$PUSH" = "1" ]; then args+=(--push); else args+=(--load); fi
fi

args+=("$REPO_ROOT")

echo "    engine: $ENGINE_SRC"
echo "    ai:     $AI_SRC  ($AI_DIR_NAME / $AI_VERSION)"
docker "${args[@]}"

if [ -n "$OUT_DIR" ]; then
	echo
	echo "==> $OUT_DIR"
	echo "    spring-headless"
	echo "    AI/Interfaces/C/0.1/libAIInterface.so"
	echo "    AI/Skirmish/$AI_DIR_NAME/$AI_VERSION/libSkirmishAI.so"
	echo
	echo "Both .so files come from one build, which is what keeps them"
	echo "ABI-compatible (CircuitAI README:25)."
fi
