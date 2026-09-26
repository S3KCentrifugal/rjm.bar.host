#!/usr/bin/env bash
# rjm-host container entrypoint.
#
#   match           install AIs, fetch content, run one AI-vs-AI simulation (default)
#   spads           install AIs, fetch content, run a SPADS lobby autohost
#   fetch-content   populate $BARHOST_DATA_DIR with the game and maps
#   install-ai      install custom AIs into $BAR_ENGINE_DIR/AI/Skirmish
#   list-ais        print the Skirmish AIs the engine can load
#   <anything else> executed verbatim (e.g. bash)
#
# Everything a run produces -- infolog, engine console, replay, result, AI logs,
# SPADS logs -- is written under $BARHOST_OUT_DIR, so mounting one volume there
# captures it all.
set -euo pipefail

ENGINE_DIR="${BAR_ENGINE_DIR:?BAR_ENGINE_DIR must be set}"
DATA_DIR="${BARHOST_DATA_DIR:-/bar/data}"
OUT_DIR="${BARHOST_OUT_DIR:-/bar/out}"
ENGINE="$ENGINE_DIR/spring-headless"
PRD="$ENGINE_DIR/pr-downloader"
BIN=/opt/rjm-host/bin

# pr-downloader settings, copied from BAR's own launcher config (the installed
# launcher's resources set exactly these three). The built-in defaults point at
# springrts.com, which does not carry the byar repository or BAR's maps, and
# BAR's CDN rejects the rapid "streamer" endpoint with an HTTP error -- the
# launcher turns it off and fetches pool files individually instead.
export PRD_RAPID_REPO_MASTER="${PRD_RAPID_REPO_MASTER:-https://repos-cdn.beyondallreason.dev/repos.gz}"
export PRD_RAPID_USE_STREAMER="${PRD_RAPID_USE_STREAMER:-false}"
export PRD_HTTP_SEARCH_URL="${PRD_HTTP_SEARCH_URL:-https://files-cdn.beyondallreason.dev/find}"

log() { echo "[entrypoint] $*" >&2; }

truthy() { case "${1,,}" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac; }

# A run id names the output directory, so repeated runs on one volume build a
# history instead of overwriting each other. Kubernetes passes the pod name.
run_id() {
	if [ -n "${BARHOST_RUN_ID:-}" ]; then
		echo "$BARHOST_RUN_ID"
	else
		date -u +%Y%m%dT%H%M%SZ
	fi
}

cmd_install_ai() {
	python3 "$BIN/install_ai.py"
}

cmd_fetch_content() {
	local gametype="${BARHOST_GAMETYPE:-byar:test}"
	local maps="${BARHOST_MAPS:-}"
	# The match map is always fetched, even if BARHOST_MAPS forgets it.
	[ -n "${BARHOST_MAP:-}" ] && maps="${BARHOST_MAP};${maps}"

	mkdir -p "$DATA_DIR"
	log "rapid master: $PRD_RAPID_REPO_MASTER"
	log "fetching game: $gametype"
	"$PRD" --filesystem-writepath "$DATA_DIR" --download-game "$gametype"

	local seen="" map
	local IFS=$';\n'
	for map in $maps; do
		map="$(echo "$map" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
		[ -n "$map" ] || continue
		case ";$seen;" in *";$map;"*) continue ;; esac
		seen="$seen;$map"
		log "fetching map: $map"
		"$PRD" --filesystem-writepath "$DATA_DIR" --download-map "$map"
	done
	log "content ready in $DATA_DIR ($(du -sh "$DATA_DIR" 2>/dev/null | cut -f1))"
}

cmd_list_ais() {
	local tmp
	tmp="$(mktemp -d)"
	SPRING_DATADIR="$ENGINE_DIR" "$ENGINE" \
		--isolation --isolation-dir "$DATA_DIR" \
		--write-dir "$tmp" \
		--list-skirmish-ais
	rm -rf "$tmp"
}

prepare() {
	cmd_install_ai
	if truthy "${BARHOST_FETCH_CONTENT:-1}"; then
		cmd_fetch_content
	fi
}

cmd_match() {
	prepare
	local dir
	dir="$OUT_DIR/matches/$(run_id)"
	mkdir -p "$dir"
	log "writing this run to $dir"
	# The engine's own infolog.txt and demos/ land in the write dir; its
	# console, which holds anything logged before the infolog opens, is kept
	# alongside as engine-console.log by run_match.py.
	BARHOST_WRITE_DIR="$dir" exec python3 "$BIN/run_match.py"
}

cmd_spads() {
	prepare
	exec "/opt/rjm-host/spads/run_spads.sh"
}

case "${1:-match}" in
	match)         cmd_match ;;
	spads)         cmd_spads ;;
	fetch-content) cmd_fetch_content ;;
	install-ai)    cmd_install_ai ;;
	list-ais)      cmd_install_ai && cmd_list_ais ;;
	*)             exec "$@" ;;
esac
