#!/usr/bin/env bash
# Offline end-to-end test of the lobby host: real SPADS + real engine + 8 AI,
# against a fake lobby server. No BAR servers, no cluster.
#
#   tests/e2e/run.sh [image]          default image: rjm-host:dev
#
# Environment:
#   E2E_DATA_VOLUME   docker volume with BAR content (default rjm-bar-data);
#                     filled on first use (~3.5 GB), reused after
#   E2E_AI            AI shortName for the bots (default SMRTBARb)
#   E2E_AI_ARCHIVES   where it comes from (default github:S3KCentrifugal/CircuitAI@newest;
#                     set empty with E2E_AI=BARb to use the engine's stock AI, fully offline)
#   E2E_SURVIVE_SECONDS  game time the bot team must survive (default 120)
#   E2E_ARTIFACTS     where logs are copied (default ./e2e-artifacts)
#   E2E_KEEP=1        leave the containers running afterwards, for inspection
#
# Exits 0 only if the scenario in lobby_e2e.py and the control-socket checks
# below all pass.
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite container paths

IMAGE="${1:-rjm-host:dev}"
HERE="$(cd "$(dirname "$0")" && { pwd -W 2> /dev/null || pwd; })"   # pwd -W: a Windows path for docker.exe under Git Bash
DATA="${E2E_DATA_VOLUME:-rjm-bar-data}"
AI="${E2E_AI:-SMRTBARb}"
AI_ARCHIVES="${E2E_AI_ARCHIVES-github:S3KCentrifugal/CircuitAI@newest}"
ARTIFACTS="${E2E_ARTIFACTS:-$PWD/e2e-artifacts}"
NET="rjm-e2e-$$"
SPADS="rjm-e2e-spads-$$"
DRIVER="rjm-e2e-driver-$$"
MAP1="All That Glitters v2.2.3"
MAP2="Comet Catcher Remake 1.8"

log()  { echo "[e2e] $*"; }
fail() { echo "[e2e] FAIL: $*" >&2; FAILED=1; }
FAILED=0

cleanup() {
	mkdir -p "$ARTIFACTS"
	docker cp "$DRIVER:/bar/out/e2e/." "$ARTIFACTS/driver" > /dev/null 2>&1 || true
	docker cp "$SPADS:/bar/out/spads/." "$ARTIFACTS/spads" > /dev/null 2>&1 || true
	docker logs "$SPADS" > "$ARTIFACTS/spads-container.log" 2>&1 || true
	docker logs "$DRIVER" > "$ARTIFACTS/driver-container.log" 2>&1 || true
	if [ "${E2E_KEEP:-0}" != 1 ]; then
		docker rm -f "$SPADS" "$DRIVER" > /dev/null 2>&1 || true
		docker network rm "$NET" > /dev/null 2>&1 || true
	else
		log "kept: containers $SPADS $DRIVER on network $NET"
	fi
	log "artifacts in $ARTIFACTS"
}
trap cleanup EXIT

log "content volume $DATA"
docker volume create "$DATA" > /dev/null
docker run --rm -v "$DATA:/bar/data" -e BARHOST_GAMETYPE=byar:test \
	-e "BARHOST_MAPS=$MAP1;$MAP2" "$IMAGE" fetch-content 2>&1 | grep -v '^\[Progress\]' | tail -3

docker network create "$NET" > /dev/null

log "starting SPADS ($SPADS)"
docker run -d --name "$SPADS" --network "$NET" \
	-v "$DATA:/bar/data:ro" \
	-e SPADS_LOBBY_HOST="$DRIVER" -e SPADS_LOBBY_TLS=off \
	-e SPADS_LOBBY_LOGIN=AutoHost -e SPADS_LOBBY_PASSWORD=e2e-secret \
	-e SPADS_OWNER_LOBBY_LOGIN=Owner -e SPADS_BATTLE_PASSWORD='*' \
	-e SPADS_BATTLE_NAME="rjm-host e2e room" -e "SPADS_MAP=$MAP1" \
	-e SPADS_GAME=rapid://byar:test \
	-e "SPADS_ALLOWED_AIS=$AI" \
	-e "SPADS_LOCAL_BOTS=${AI}1 0 $AI;${AI}2 1 $AI;${AI}3 0 $AI;${AI}4 1 $AI;${AI}5 0 $AI;${AI}6 1 $AI;${AI}7 0 $AI;${AI}8 1 $AI" \
	-e SPADS_AUTO_ADD_BOTS=8 \
	-e "BARHOST_AI_ARCHIVES=$AI_ARCHIVES" -e BARHOST_FETCH_CONTENT=0 \
	-e AUTOPILOT_BOT_BONUS=50 -e AUTOPILOT_START_DELAY=5 -e AUTOPILOT_PREGAME_FORCE=30 \
	"$IMAGE" spads > /dev/null

log "starting the fake lobby and scripted players ($DRIVER)"
docker create --name "$DRIVER" --network "$NET" \
	-v "$DATA:/bar/data:ro" \
	-e "E2E_AI=$AI" -e E2E_BOTS=8 -e E2E_BONUS=50 -e "E2E_MAP=$MAP1" \
	-e "E2E_SURVIVE_SECONDS=${E2E_SURVIVE_SECONDS:-120}" \
	--entrypoint python3 "$IMAGE" /tmp/lobby_e2e.py --serve > /dev/null
docker cp "$HERE/lobby_e2e.py" "$DRIVER:/tmp/lobby_e2e.py"
docker start "$DRIVER" > /dev/null

docker logs -f "$DRIVER" 2>&1 | sed -u 's/^/  | /' &
TAIL=$!

# Scenario first; the room stays up afterwards for the socket checks.
for _ in $(seq 1800); do
	docker exec "$DRIVER" test -f /bar/out/e2e/scenario-done 2> /dev/null && break
	if [ "$(docker inspect -f '{{.State.Running}}' "$DRIVER")" != true ]; then break; fi
	sleep 1
done
SCENARIO_RC="$(docker exec "$DRIVER" cat /bar/out/e2e/scenario-done 2> /dev/null || echo missing)"
[ "$SCENARIO_RC" = 0 ] || fail "lobby scenario (rc=$SCENARIO_RC)"

if [ "$FAILED" = 0 ]; then
	log "control socket: status"
	STATUS="$(docker exec "$SPADS" spadsctl status)" || fail "spadsctl status"
	grep -q '"battleOpen": true' <<< "$STATUS" || fail "status does not show the room open"
	[ "$(grep -c '"ai": "'"$AI" <<< "$STATUS")" -ge 8 ] || fail "status does not list 8 $AI bots"

	log "control socket: change the map"
	docker exec "$SPADS" spadsctl spads map "$MAP2" > /dev/null || fail "spadsctl spads map"

	log "control socket: an unknown verb is refused"
	if docker exec "$SPADS" spadsctl frobnicate > /dev/null 2>&1; then fail "unknown verb accepted"; fi

	log "control socket: the socket is private to the container user"
	PERM="$(docker exec "$SPADS" stat -c '%a %U' /tmp/autopilot.sock)"
	# Owner-only: no group or other bits (the plugin creates it under umask 077).
	case "$PERM" in
		?00\ barhost) ;;
		*) fail "socket permissions are '$PERM', expected owner-only for barhost" ;;
	esac

	log "every game's logs were archived"
	N="$(docker exec "$SPADS" sh -c 'ls /bar/out/spads/games/*/infolog.txt 2>/dev/null | wc -l')"
	[ "$N" -ge 2 ] || fail "expected 2 archived games, found $N"
	docker exec "$SPADS" sh -c 'ls /bar/out/spads/var/demos*/*.sdfz' > /dev/null 2>&1 || fail "no lobby-game replay recorded"

	sleep 3
fi

docker exec "$DRIVER" touch /bar/out/e2e/stop 2> /dev/null || true
for _ in $(seq 30); do
	[ "$(docker inspect -f '{{.State.Running}}' "$DRIVER")" = true ] || break
	sleep 1
done
kill "$TAIL" 2> /dev/null || true

if [ "$FAILED" = 0 ]; then
	FINAL_MAP="$(docker cp "$DRIVER:/bar/out/e2e/final-room.json" - 2> /dev/null | tar -xO 2> /dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["map"])' 2> /dev/null || true)"
	[ "$FINAL_MAP" = "$MAP2" ] || fail "the room's map is '$FINAL_MAP' after spadsctl, expected '$MAP2'"
fi

if [ "$FAILED" = 0 ]; then
	log "PASS"
else
	log "FAILED"
	exit 1
fi
