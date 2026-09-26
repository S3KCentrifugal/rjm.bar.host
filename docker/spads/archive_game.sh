#!/usr/bin/env bash
# SPADS endGameCommand: keep each lobby game's logs.
#
#   archive_game.sh <gameId> <startTimestamp> <endTimestamp> <result>
#
# SPADS writes infolog.txt and startscript.txt into its instance directory and
# overwrites both on the next game, so without this only the last game's logs
# would survive. Replays are already one file per game in var/demos/; the
# newest one is linked into the archive for convenience.
#
# Only SPADS-generated tokens are passed in (hex id, epoch seconds, a result
# word); anything user-controlled, such as the map name, stays out of the shell.
set -uo pipefail

OUT="${BARHOST_OUT_DIR:-/bar/out}/spads"
VAR="$OUT/var"

game_id="${1:-unknown}"
start="${2:-0}"
end="${3:-$(date +%s)}"
result="${4:-unknown}"

# Accept only the shapes SPADS produces, so a surprise cannot become a path.
[[ "$game_id" =~ ^[0-9a-fA-F]*$ ]] || game_id=unknown
[[ "$start" =~ ^[0-9]+$ ]] || start=0
[[ "$end" =~ ^[0-9]+$ ]] || end="$(date +%s)"
[[ "$result" =~ ^[A-Za-z]+$ ]] || result=unknown

dir="$OUT/games/$(date -u -d "@$end" +%Y%m%dT%H%M%SZ)-${game_id:-unknown}"
mkdir -p "$dir"

for f in infolog.txt startscript.txt; do
	[ -f "$VAR/$f" ] && cp "$VAR/$f" "$dir/$f"
done

demo="$(ls -t "$VAR"/demos*/*.sdfz 2>/dev/null | head -n1 || true)"
[ -n "$demo" ] && ln -sf "$demo" "$dir/replay.sdfz"

cat > "$dir/game.json" <<EOF
{"gameId": "$game_id", "startTimestamp": $start, "endTimestamp": $end, "result": "$result", "replay": "${demo#"$OUT"/}"}
EOF
echo "[archive_game] archived game $game_id ($result) to $dir"
