#!/usr/bin/env bash
# Fast offline tests, no game content and no network:
#   1. unit tests, run inside the image (the code under test is the installed copy)
#   2. static checks of the Kustomize output, run here (needs kubectl + python3-yaml)
#
#   tests/run_unit.sh [image]        default image: rjm-host:dev
set -euo pipefail
export MSYS_NO_PATHCONV=1

IMAGE="${1:-rjm-host:dev}"
HERE="$(cd "$(dirname "$0")" && { pwd -W 2> /dev/null || pwd; })"   # pwd -W: a Windows path for docker.exe under Git Bash
C="rjm-unit-$$"
trap 'docker rm -f "$C" > /dev/null 2>&1 || true' EXIT

echo "[unit] python unit tests in $IMAGE"
docker create --name "$C" --entrypoint python3 -w /tmp/unit "$IMAGE" \
	-m unittest discover -s /tmp/unit -v > /dev/null
docker cp "$HERE/unit" "$C:/tmp/"
docker start -a "$C"

echo "[unit] static manifest checks"
python3 "$HERE/static/check_manifests.py"
echo "[unit] PASS"
