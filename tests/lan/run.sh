#!/usr/bin/env bash
# LAN end-to-end test: three containers, one direct-connect game, no lobby
# server and no internet during the game.
#
#   host   runs the game server (lan_game.py host), no AI
#   box-a  joins and runs SMRTBARb (from its GitHub release) for team 0
#   box-b  joins and runs the engine's stock BARb for team 1
#
#   tests/lan/run.sh [image]          default image: rjm-bar-host:dev
#
# Environment: E2E_DATA_VOLUME (default rjm-bar-data), LAN_PLAY_SECONDS (game
# time to play before the host stops it, default 150), E2E_ARTIFACTS, E2E_KEEP=1.
#
# Passes when both joiners finish loading, the game starts, and it is still
# running when the host stops it at LAN_PLAY_SECONDS -- or ends by a real game
# over after at least that long -- with each AI loaded in its own container.
# Three engines at once need ~12 GB; this is a local test, not a CI one.
set -euo pipefail
export MSYS_NO_PATHCONV=1

IMAGE="${1:-rjm-bar-host:dev}"
HERE="$(cd "$(dirname "$0")" && { pwd -W 2> /dev/null || pwd; })"   # Windows path for docker.exe under Git Bash
DATA="${E2E_DATA_VOLUME:-rjm-bar-data}"
PLAY="${LAN_PLAY_SECONDS:-150}"
ARTIFACTS="${E2E_ARTIFACTS:-$PWD/e2e-artifacts/lan}"
NET="rjm-lan-$$"
HOST="rjm-lan-host-$$"
BOXA="rjm-lan-boxa-$$"
BOXB="rjm-lan-boxb-$$"
FAILED=0

log()  { echo "[lan] $*"; }
fail() { echo "[lan] FAIL: $*" >&2; FAILED=1; }

cleanup() {
	mkdir -p "$ARTIFACTS"
	for c in "$HOST" "$BOXA" "$BOXB"; do
		docker cp "$c:/bar/out/lan/." "$ARTIFACTS/" > /dev/null 2>&1 || true
		docker logs "$c" > "$ARTIFACTS/$c.log" 2>&1 || true
	done
	if [ "${E2E_KEEP:-0}" != 1 ]; then
		docker rm -f "$HOST" "$BOXA" "$BOXB" > /dev/null 2>&1 || true
		docker network rm "$NET" > /dev/null 2>&1 || true
	fi
	log "artifacts in $ARTIFACTS"
}
trap cleanup EXIT

docker volume create "$DATA" > /dev/null
docker run --rm -v "$DATA:/bar/data" -e BARHOST_GAMETYPE=byar:test \
	-e "BARHOST_MAPS=All That Glitters v2.2.3" "$IMAGE" fetch-content 2>&1 | grep -v '^\[Progress\]' | tail -1
docker network create "$NET" > /dev/null

# The example spec, with a play limit so the test ends on its own.
SPEC="$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); s["maxGameSeconds"]=int(sys.argv[2]); print(json.dumps(s))' \
	"$HERE/../../examples/lan/ai-vs-ai.json" "$PLAY")"
COMMON=(--network "$NET" -v "$DATA:/bar/data:ro" -e BARHOST_FETCH_CONTENT=0 -e BARHOST_RUN_ID=lan
	-e "BARHOST_ENGINE_SETTINGS=InitialNetworkTimeout=300,NetworkTimeout=300")

log "host ($HOST)"
docker run -d --name "$HOST" "${COMMON[@]}" -e "BARHOST_LAN_SPEC=$SPEC" "$IMAGE" host > /dev/null
log "box-a: SMRTBARb ($BOXA)"
docker run -d --name "$BOXA" "${COMMON[@]}" -e "BARHOST_LAN_HOST=$HOST" -e BARHOST_LAN_NAME=BoxA \
	-e BARHOST_LAN_PASSWORD=boxa1 -e BARHOST_AI_ARCHIVES=github:S3KCentrifugal/CircuitAI@newest "$IMAGE" join > /dev/null
log "box-b: stock BARb ($BOXB)"
docker run -d --name "$BOXB" "${COMMON[@]}" -e "BARHOST_LAN_HOST=$HOST" -e BARHOST_LAN_NAME=BoxB \
	-e BARHOST_LAN_PASSWORD=boxb1 "$IMAGE" join > /dev/null

docker logs -f "$HOST" 2>&1 | grep --line-buffered '^\[lan_game\]' | sed -u 's/^/  | /' &
TAIL=$!
HOST_RC="$(docker wait "$HOST")"
for c in "$BOXA" "$BOXB"; do timeout 180 docker wait "$c" > /dev/null 2>&1 || docker rm -f "$c" > /dev/null; done
kill "$TAIL" 2> /dev/null || true

mkdir -p "$ARTIFACTS"
docker cp "$HOST:/bar/out/lan/lan/result.json" - 2> /dev/null | tar -xO > "$ARTIFACTS/host-result.json" 2> /dev/null || true
cat "$ARTIFACTS/host-result.json"
[ "$HOST_RC" = 0 ] || fail "host exited $HOST_RC"
cat > "$ARTIFACTS/check.py" <<'EOF'
import json, sys
play = int(sys.argv[2])
try:
    r = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    r = {}
problems = []
if r.get("playersLoaded") != ["BoxA", "BoxB"]:
    problems.append(f"players loaded: {r.get('playersLoaded')}")
if not r.get("started"):
    problems.append("the game never started")
if r.get("exitReason") not in ("time limit", "game over"):
    problems.append(f"exit reason: {r.get('exitReason')}")
if r.get("playedSeconds", 0) < play * 0.9:
    problems.append(f"only {r.get('playedSeconds')}s played -- ended early (collapse?)")
for p in problems:
    print("[lan] FAIL:", p, file=sys.stderr)
sys.exit(1 if problems else 0)
EOF
python3 "$ARTIFACTS/check.py" "$ARTIFACTS/host-result.json" "$PLAY" || FAILED=1

# Each AI ran in its own container: the joiners' logs say so.
for c in "$BOXA" "$BOXB"; do
	docker cp "$c:/bar/out/lan/." "$ARTIFACTS/" > /dev/null 2>&1 || true
done
grep -qs 'local skirmish AI.*Short-Name: "SMRTBARb"' "$ARTIFACTS"/lan-BoxA/infolog.txt || fail "SMRTBARb did not run in box-a"
grep -qs 'local skirmish AI.*Short-Name: "BARb"' "$ARTIFACTS"/lan-BoxB/infolog.txt || fail "BARb did not run in box-b"

if [ "$FAILED" = 0 ]; then log "PASS"; else log "FAILED"; exit 1; fi
