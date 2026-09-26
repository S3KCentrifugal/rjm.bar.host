#!/usr/bin/env bash
# SPADS readiness: true when *this* SPADS process holds an established TCP
# connection to the lobby port.
#
# The pod runs with hostNetwork, so /proc/net/tcp lists every socket on the
# node -- another environment's SPADS included. Matching only the socket inodes
# this container's SPADS process owns (/proc/<pid>/fd -> socket:[inode]) keeps
# one environment's lobby connection from making the other look ready.
set -uo pipefail

port_hex="$(printf '%04X' "${SPADS_LOBBY_PORT:-8200}")"

inodes=""
for p in /proc/[0-9]*; do
	cmd="$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)" || continue
	case "$cmd" in *spads.pl*) ;; *) continue ;; esac
	for fd in "$p"/fd/*; do
		link="$(readlink "$fd" 2>/dev/null)" || continue
		case "$link" in socket:\[*\]) inodes="$inodes ${link//[!0-9]/}" ;; esac
	done
done
[ -n "$inodes" ] || exit 1

# /proc/net/tcp columns: sl local rem st ... inode (10th). State 01 = ESTABLISHED.
awk -v port=":$port_hex" -v inodes="$inodes" '
	BEGIN { n = split(inodes, list, " "); for (i = 1; i <= n; i++) want[list[i]] = 1 }
	$4 == "01" && substr($3, length($3) - 4) == port && ($10 in want) { found = 1 }
	END { exit found ? 0 : 1 }
' /proc/net/tcp /proc/net/tcp6 2>/dev/null
