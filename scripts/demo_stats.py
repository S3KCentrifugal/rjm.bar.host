#!/usr/bin/env python3
"""Print per-team statistics from a Recoil demo (.sdfz).

This is the objective scoreboard for a match: units produced, units killed,
metal produced, damage dealt and taken, per team. Two AI builds playing the same
map with the same settings can be compared on these numbers rather than on who
happened to win.

File layout, from ``rts/System/LoadSave/DemoFile.h:53-72`` and
``rts/System/LoadSave/DemoRecorder.cpp:296-317``:

    DemoFileHeader | script | demo stream | player stats | team stats

and the team-stats chunk is ``numTeams`` little-endian uint32 sample counts,
followed by that many packed ``TeamStatistics`` records per team, in team order
(``rts/Sim/Misc/TeamStatistics.h:13``).

Note that ``numTeams`` in the header is
``teamStartingData.size() - useLuaGaia`` at game over (``GameServer.cpp:319``),
so with Lua Gaia enabled the highest-numbered team may be absent from the table.

**Only games that reached game over carry statistics.** The recorder fills them
in on the game-over path (``GameServer.cpp:313-322``), so a match cut short by
the wall clock, or one played with ``deathmode=neverend``, reports zero samples
for every team. That is not a parsing failure; there is genuinely nothing there.

Usage:
  scripts/demo_stats.py <demo.sdfz> [--json]
"""

from __future__ import annotations

import gzip
import json
import struct
import sys
from pathlib import Path

# DemoFileHeader, little-endian, no padding between these fields.
HEADER = "<16s i i 256s 16s Q i i i i i i i i i i i i"
# TeamStatistics: int frame, 12 floats, 7 ints (TeamStatistics.h:19-37).
TEAM_STAT = "<i 12f 7i"

FIELDS = (
    "frame",
    "metalUsed", "energyUsed",
    "metalProduced", "energyProduced",
    "metalExcess", "energyExcess",
    "metalReceived", "energyReceived",
    "metalSent", "energySent",
    "damageDealt", "damageReceived",
    "unitsProduced", "unitsDied", "unitsReceived", "unitsSent",
    "unitsCaptured", "unitsOutCaptured", "unitsKilled",
)


def read_demo(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".sdfz" else open
    with opener(path, "rb") as fh:
        raw = fh.read()

    if len(raw) < struct.calcsize(HEADER):
        raise SystemExit("%s: too short to be a demo" % path)

    values = struct.unpack_from(HEADER, raw, 0)
    (magic, version, header_size, version_string, game_id, unix_time,
     script_size, stream_size, game_time, wallclock_time, num_players,
     player_stat_size, player_stat_elem, num_teams, team_stat_size,
     team_stat_elem, team_stat_period, winners_size) = values

    if not magic.startswith(b"spring demofile"):
        raise SystemExit("%s: not a Recoil demo (magic=%r)" % (path, magic))

    if team_stat_elem and team_stat_elem != struct.calcsize(TEAM_STAT):
        raise SystemExit(
            "TeamStatistics is %d bytes in this demo but %d here -- the struct "
            "changed upstream and this parser needs updating"
            % (team_stat_elem, struct.calcsize(TEAM_STAT)))

    out = {
        "file": str(path),
        "engine": version_string.split(b"\0", 1)[0].decode("utf-8", "replace"),
        "gameTimeSeconds": game_time,
        "wallclockSeconds": wallclock_time,
        "numPlayers": num_players,
        "numTeams": num_teams,
        "statsPeriodSeconds": team_stat_period,
        "teams": [],
    }

    offset = header_size + script_size + stream_size + player_stat_size
    if team_stat_size <= 0 or num_teams <= 0:
        return out

    counts = struct.unpack_from("<%dI" % num_teams, raw, offset)
    offset += 4 * num_teams

    for team in range(num_teams):
        last = None
        for _ in range(counts[team]):
            last = struct.unpack_from(TEAM_STAT, raw, offset)
            offset += team_stat_elem
        entry = {"team": team, "samples": counts[team]}
        if last is not None:
            entry.update(dict(zip(FIELDS, last)))
        out["teams"].append(entry)

    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    as_json = "--json" in sys.argv
    if not args:
        raise SystemExit(__doc__)

    data = read_demo(Path(args[0]))

    if as_json:
        print(json.dumps(data, indent=2))
        return 0

    print("engine   %s" % data["engine"])
    print("game     %ds of game time in %ds wallclock"
          % (data["gameTimeSeconds"], data["wallclockSeconds"]))
    print("teams    %d   players %d" % (data["numTeams"], data["numPlayers"]))

    if not any(t["samples"] for t in data["teams"]):
        print("\nNo team statistics in this demo.")
        print("The recorder writes them on the game-over path only")
        print("(GameServer.cpp:313-322), so a run that timed out or used")
        print("deathmode=neverend has none to report.")
        return 0

    print("\n%-5s %8s %8s %8s %10s %12s %12s"
          % ("team", "samples", "built", "killed", "lost", "metal", "damage"))
    for t in data["teams"]:
        if not t["samples"]:
            print("%-5d %8d  (no samples)" % (t["team"], 0))
            continue
        print("%-5d %8d %8d %8d %10d %12.0f %12.0f"
              % (t["team"], t["samples"], t["unitsProduced"], t["unitsKilled"],
                 t["unitsDied"], t["metalProduced"], t["damageDealt"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
