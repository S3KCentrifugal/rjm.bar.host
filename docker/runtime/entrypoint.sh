#!/usr/bin/env bash
# Container entrypoint. Subcommands:
#
#   match           run one AI-vs-AI match to completion (default)
#   fetch-content   populate the data volume with the pinned game + maps
#   list-ais        print the skirmish AIs the engine can load (diagnostic)
#   <anything else> executed verbatim
set -euo pipefail

ENGINE_DIR="${BARHOST_ENGINE_DIR:-/opt/engine}"
DATA_DIR="${BARHOST_DATA_DIR:-/spring-data}"
ENGINE="${BARHOST_ENGINE:-$ENGINE_DIR/spring-headless}"
PRD="$ENGINE_DIR/pr-downloader"

# pr-downloader defaults its rapid master to https://repos.springrts.com/repos.gz
# (confirmed from `pr-downloader --help`). That master does not carry the byar
# repo, so a `byar:test` tag silently fails to resolve against it. BAR's own
# master must be set explicitly.
export PRD_RAPID_REPO_MASTER="${PRD_RAPID_REPO_MASTER:-https://repos-cdn.beyondallreason.dev/repos.gz}"

log() { echo "[entrypoint] $*" >&2; }

cmd_fetch_content() {
	local gametype="${BARHOST_GAMETYPE:-byar:test}"
	local maps="${BARHOST_MAPS:-}"

	mkdir -p "$DATA_DIR"

	if [ ! -x "$PRD" ]; then
		log "FAIL: pr-downloader not found at $PRD"
		exit 1
	fi

	# Idempotent by design: this runs as an init container on every pod start,
	# and rapid skips what the pool already has.
	log "rapid master: $PRD_RAPID_REPO_MASTER"
	log "fetching game: $gametype"
	"$PRD" --filesystem-writepath "$DATA_DIR" --download-game "$gametype"

	if [ -n "$maps" ]; then
		# BARHOST_MAPS is a newline- or semicolon-separated list of map display
		# names. --download-map may be repeated, but one call per map gives a
		# clearer failure when a single name is wrong.
		local old_ifs="$IFS"
		IFS=$';\n'
		for map in $maps; do
			map="$(echo "$map" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
			[ -n "$map" ] || continue
			log "fetching map: $map"
			"$PRD" --filesystem-writepath "$DATA_DIR" --download-map "$map"
		done
		IFS="$old_ifs"
	else
		log "BARHOST_MAPS is empty -- no maps fetched"
	fi

	log "content ready in $DATA_DIR"
	du -sh "$DATA_DIR" 2>/dev/null || true
}

cmd_list_ais() {
	local tmp
	tmp="$(mktemp -d)"
	# Isolation pins content to one dir; SPRING_DATADIR adds the engine's own
	# AI/ tree back (DataDirLocater.cpp:434-441).
	SPRING_DATADIR="$ENGINE_DIR" "$ENGINE" \
		--isolation --isolation-dir "$DATA_DIR" \
		--write-dir "$tmp" \
		--list-skirmish-ais
	rm -rf "$tmp"
}

case "${1:-match}" in
	match)
		exec python3 /usr/local/bin/run_match.py
		;;
	fetch-content)
		cmd_fetch_content
		;;
	list-ais)
		cmd_list_ais
		;;
	*)
		exec "$@"
		;;
esac
