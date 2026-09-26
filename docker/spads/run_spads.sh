#!/usr/bin/env bash
# Start SPADS as a BAR lobby autohost that runs Skirmish AI bots in its own
# spring-headless process.
#
#   run_spads.sh           validate, then exec SPADS
#   run_spads.sh --check   validate the configuration and exit (CI smoke test)
#
# Configuration is the files in /opt/rjm-host/spads/etc, parametrised by
# environment variables that SPADS itself substitutes (%ENV{..}% and
# %(ENV{..}|default)%, SpadsConf.pm). Credentials come from the environment
# only -- never from the image.
#
# Required:  SPADS_LOBBY_LOGIN  SPADS_LOBBY_PASSWORD (or SPADS_LOBBY_PASSWORD_FILE)
#            SPADS_OWNER_LOBBY_LOGIN
# Optional:  see k8s/base/spads.env for every setting and its default.
#
# Output (all under $BARHOST_OUT_DIR/spads, so one volume captures it):
#   log/spads.log, log/chat/, log/spring-headless.log   SPADS and engine console
#   var/demos/*.sdfz                                     every game's replay
#   games/<end>-<gameId>/{infolog.txt,startscript.txt}   archived per game
set -euo pipefail

SPADS_HOME=/opt/spads
ETC=/opt/rjm-host/spads/etc
OUT="${BARHOST_OUT_DIR:-/bar/out}/spads"

log() { echo "[run_spads] $*" >&2; }
die() { log "FAIL: $*"; exit 1; }

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

if [ -z "${SPADS_LOBBY_PASSWORD:-}" ] && [ -n "${SPADS_LOBBY_PASSWORD_FILE:-}" ] && [ -f "$SPADS_LOBBY_PASSWORD_FILE" ]; then
	SPADS_LOBBY_PASSWORD="$(head -n1 "$SPADS_LOBBY_PASSWORD_FILE")"
	export SPADS_LOBBY_PASSWORD
fi
[ -n "${SPADS_LOBBY_LOGIN:-}" ]    || die "SPADS_LOBBY_LOGIN is not set"
[ -n "${SPADS_LOBBY_PASSWORD:-}" ] || die "SPADS_LOBBY_PASSWORD is not set"
# users.conf grants level 140 (bot owner) to this login. An empty value would
# match *every* user and hand them full control of the host.
[ -n "${SPADS_OWNER_LOBBY_LOGIN:-}" ] || die "SPADS_OWNER_LOBBY_LOGIN is not set (it would make every user the owner)"
name_re='^[][A-Za-z0-9_]+$'   # lobby names: letters, digits, _ and [ ]
[[ "$SPADS_OWNER_LOBBY_LOGIN" =~ $name_re ]] || die "SPADS_OWNER_LOBBY_LOGIN is not a valid lobby name"

[ -x "${BAR_ENGINE_DIR:?}/spring-headless" ] || die "no spring-headless in $BAR_ENGINE_DIR"
[ -f "$BAR_ENGINE_DIR/libunitsync.so" ]      || die "no libunitsync.so in $BAR_ENGINE_DIR"

# SPADS does not create its own writable directories (SpadsConf.pm:1602-1627).
mkdir -p "$OUT/var/plugins" "$OUT/var/spring" "$OUT/log" "$OUT/games"

if [ "$CHECK" = 1 ]; then
	# Load the configuration through SPADS's own parser, exactly as spads.pl
	# does, without connecting anywhere.
	cd "$SPADS_HOME"
	perl -I"$SPADS_HOME" -MSpadsConf -MSimpleLog -e '
		my $log = SimpleLog->new(logFiles => [""], logLevels => [2], useANSICodes => [0], useTimestamps => [0], prefix => "[SpadsConf] ");
		my $conf = SpadsConf->new($ARGV[0], $log, {});
		die "SPADS configuration did not load\n" unless $conf;
		my %c = %{$conf->{conf}};
		printf "%s = %s\n", $_, $c{$_} for qw(lobbyHost lobbyPort springServerType springServer unitsyncDir springDataDir varDir logDir autoHostPort);
		printf "%s = %s\n", $_, $conf->{hSettings}{$_} for qw(modName port);
		print "configuration OK\n";
	' "$ETC/spads.conf"
	exit 0
fi

# A pid file left by a container that was killed would stop SPADS starting
# (spads.pl:16024-16047). This container is the only instance.
rm -f "$OUT/var/spads.pid"

log "lobby ${SPADS_LOBBY_HOST:-server4.beyondallreason.info}:${SPADS_LOBBY_PORT:-8200} as $SPADS_LOBBY_LOGIN, owner $SPADS_OWNER_LOBBY_LOGIN"
log "game UDP port ${SPADS_GAME_PORT:-8452}; output under $OUT"
cd "$SPADS_HOME"
# shellcheck disable=SC2086 # SPADS_ARGS is a word list, e.g. --tls-cert-trust
exec perl "$SPADS_HOME/spads.pl" "$ETC/spads.conf" ${SPADS_ARGS:-}
