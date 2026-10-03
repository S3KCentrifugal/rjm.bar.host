#!/usr/bin/env python3
"""Direct-connect (LAN) multiplayer games between rjm-bar-host containers.

No lobby server and no internet: the engine's own networking. One container
hosts, the others join it over UDP. Any participant can run Skirmish AIs in its
own process, so two AI builds can play each other from separate containers.

    lan_game.py host      host the game described by the spec, supervise it,
                          write result.json; exits non-zero if it failed
    lan_game.py join      join a hosted game as one participant
    lan_game.py script    print the host's start script (for inspection)

Host spec -- JSON, from $BARHOST_LAN_SPEC or the file $BARHOST_LAN_SPEC_FILE:

    {
      "map": "All That Glitters v2.2.3",       # versioned archive name
      "game": "byar:test",                    # rapid tag or archive name
      "port": 8452,                           # UDP port the host listens on
      "startPosType": 2,                      # 0 fixed, 1 random, 2 choose in game
      "modoptions": {"coop": "1"},            # on top of run_match's defaults
      "speed": 1,                             # game speed; every client must keep up
      "waitForPlayers": 600,                  # seconds for every player to load
      "forceStartDelay": 10,                  # seconds after all loaded, then start
      "maxGameSeconds": 0,                    # stop after this much play; 0 = to game over
      "timeout": 7200,                        # wall-clock budget for everything
      "observerTeam": "auto",                 # see below
      "players": [                            # everyone but the host
        {"name": "Alice", "password": "a1"},                    # plays a team
        {"name": "BoxA", "password": "b1", "spectator": true}   # only runs AIs
      ],
      "teams": [
        {"allyTeam": 0, "player": "Alice", "side": "Armada"},
        {"allyTeam": 1, "ai": {"shortName": "SMRTBARb", "host": "BoxA"}, "handicap": 50},
        {"allyTeam": 1, "ai": {"shortName": "BARb", "version": "stable", "host": "host",
                               "options": {"profile": "hard"}}}
      ]
    }

An AI's "host" is the player whose engine runs it, or "host" for the hosting
container. That engine needs the AI installed (install_ai.py); no other does.

"observerTeam": the host and spectator players sit on an extra, empty team in
an allyteam of their own. Without it BAR's game_end.lua wipes out the allyteam a
spectator sits in when it holds two or more AI teams (design section 11,
risk 1). "auto" (default) turns it on when no human plays; true/false force it.

Join -- environment:
    BARHOST_LAN_HOST       the host's address (container name or IP)
    BARHOST_LAN_PORT       its UDP port (default 8452)
    BARHOST_LAN_NAME       this participant's name in the spec
    BARHOST_LAN_PASSWORD   its password in the spec

Both write into $BARHOST_WRITE_DIR (the entrypoint uses
/bar/out/lan/<run-id>/): infolog.txt, engine-console.log, replays, script.txt,
result.json. $BARHOST_ENGINE_SETTINGS ("Key=Value,...") adds engine settings,
e.g. InitialNetworkTimeout=300 for slow machines.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import barcommon as rm  # noqa: E402  (shared with every mode: Tdf, Autohost, Engine, settings)

NAME_RE = re.compile(r"^[\w\[\]]{1,20}$")
HOST_ALIAS = "host"
DEFAULTS = {
    "game": "byar:test", "port": 8452, "startPosType": 2, "modoptions": {}, "speed": 1,
    "waitForPlayers": 600, "forceStartDelay": 10, "maxGameSeconds": 0, "timeout": 7200,
    "observerTeam": "auto", "hostName": "LanHost", "players": [],
}


def log(msg: str) -> None:
    print("[lan_game] " + msg, flush=True)


class SpecError(ValueError):
    pass


# --- Spec -------------------------------------------------------------------


def load_spec() -> dict:
    raw = os.environ.get("BARHOST_LAN_SPEC", "").strip()
    path = os.environ.get("BARHOST_LAN_SPEC_FILE", "").strip()
    if not raw and path:
        raw = Path(path).read_text(encoding="utf-8")
    if not raw:
        raise SpecError("no game spec: set BARHOST_LAN_SPEC or BARHOST_LAN_SPEC_FILE")
    try:
        spec = json.loads(raw)
    except ValueError as exc:
        raise SpecError(f"game spec is not valid JSON: {exc}") from exc
    return validate(spec)


def validate(spec: dict) -> dict:
    """Fill defaults and reject anything the engine would choke on later."""
    s = dict(DEFAULTS)
    s.update(spec)
    if not s.get("map"):
        raise SpecError("spec.map is required")
    if not NAME_RE.match(s["hostName"]):
        raise SpecError(f"hostName {s['hostName']!r} is not a valid player name")

    players = []
    seen = {s["hostName"].lower(), HOST_ALIAS}
    for p in s["players"]:
        name = p.get("name", "")
        if not NAME_RE.match(name):
            raise SpecError(f"player name {name!r} must be 1-20 letters, digits, _ or []")
        if name.lower() in seen:
            raise SpecError(f"player name {name!r} is used twice (or is reserved)")
        seen.add(name.lower())
        if not re.match(r"^[\w]{1,50}$", str(p.get("password", ""))):
            raise SpecError(f"player {name}: password must be 1-50 letters, digits or _")
        players.append({"name": name, "password": str(p["password"]),
                        "spectator": bool(p.get("spectator", False))})
    s["players"] = players
    by_name = {p["name"]: p for p in players}

    teams = s.get("teams") or []
    if len(teams) < 2:
        raise SpecError("spec.teams needs at least two teams")
    led = set()
    ally_ids = set()
    for i, t in enumerate(teams):
        if ("player" in t) == ("ai" in t):
            raise SpecError(f"team {i}: give exactly one of \"player\" or \"ai\"")
        if not isinstance(t.get("allyTeam"), int) or t["allyTeam"] < 0:
            raise SpecError(f"team {i}: allyTeam must be a non-negative integer")
        ally_ids.add(t["allyTeam"])
        if "player" in t:
            p = by_name.get(t["player"])
            if not p:
                raise SpecError(f"team {i}: player {t['player']!r} is not in spec.players")
            if p["spectator"]:
                raise SpecError(f"team {i}: {p['name']} is a spectator and cannot play a team")
            if p["name"] in led:
                raise SpecError(f"team {i}: {p['name']} already plays another team")
            led.add(p["name"])
        else:
            ai = t["ai"]
            if not NAME_RE.match(ai.get("shortName", "")):
                raise SpecError(f"team {i}: ai.shortName is required")
            host = ai.get("host", HOST_ALIAS)
            if host != HOST_ALIAS and host not in by_name:
                raise SpecError(f"team {i}: ai.host {host!r} is neither \"host\" nor a player")
    if ally_ids != set(range(len(ally_ids))):
        raise SpecError(f"allyTeams must be numbered 0..n-1 without gaps, got {sorted(ally_ids)}")
    for p in players:
        if not p["spectator"] and p["name"] not in led:
            raise SpecError(f"player {p['name']} plays no team; make it a spectator or give it one")
    if len(teams) > len(rm.TEAM_COLORS):
        raise SpecError(f"at most {len(rm.TEAM_COLORS)} teams")

    obs = s["observerTeam"]
    if obs == "auto":
        s["observerTeam"] = not led          # an all-AI game
    elif not isinstance(obs, bool):
        raise SpecError("observerTeam must be true, false or \"auto\"")
    s["modoptions"] = rm.default_modoptions(",".join(f"{k}={v}" for k, v in s["modoptions"].items()))
    for key in ("port", "startPosType", "waitForPlayers", "forceStartDelay", "maxGameSeconds", "timeout"):
        s[key] = int(s[key])
    s["speed"] = float(s["speed"])
    s["teams"] = teams
    return s


# --- Start scripts ----------------------------------------------------------


def host_script(s: dict, autohost_port: int = 53200) -> str:
    """The host's GameSetup script. Player 0 is the host, a spectator."""
    teams = s["teams"]
    names = [s["hostName"]] + [p["name"] for p in s["players"]]
    index = {n: i for i, n in enumerate(names)}
    index[HOST_ALIAS] = 0
    n_ally = max(t["allyTeam"] for t in teams) + 1
    observer_team = len(teams) if s["observerTeam"] else None
    team_of_player = {t["player"]: i for i, t in enumerate(teams) if "player" in t}

    tdf = rm.Tdf()
    tdf.open("GAME")
    tdf.kv("IsHost", 1)
    tdf.kv("HostIP", "")                      # every interface: clients come from outside
    tdf.kv("HostPort", s["port"])
    tdf.kv("MyPlayerName", s["hostName"])
    tdf.kv("AutohostIP", "127.0.0.1")         # the supervisor's control channel stays local
    tdf.kv("AutohostPort", autohost_port)
    tdf.kv("GameType", s["game"])
    tdf.kv("MapName", s["map"])
    tdf.kv("StartPosType", s["startPosType"])
    tdf.kv("NumPlayers", len(names))
    tdf.kv("NumTeams", len(teams) + (1 if observer_team is not None else 0))
    tdf.kv("NumAllyTeams", n_ally + (1 if observer_team is not None else 0))

    def spectator_team() -> int:
        return observer_team if observer_team is not None else 0

    tdf.open("PLAYER0")
    tdf.kv("Name", s["hostName"])
    tdf.kv("Spectator", 1)
    tdf.kv("Team", spectator_team())
    tdf.close()
    for i, p in enumerate(s["players"], start=1):
        tdf.open(f"PLAYER{i}")
        tdf.kv("Name", p["name"])
        tdf.kv("Password", p["password"])
        if p["spectator"]:
            tdf.kv("Spectator", 1)
            tdf.kv("Team", spectator_team())
        else:
            tdf.kv("Spectator", 0)
            tdf.kv("Team", team_of_player[p["name"]])
        tdf.close()

    ai_n = 0
    for i, t in enumerate(teams):
        if "ai" not in t:
            continue
        ai = t["ai"]
        tdf.open(f"AI{ai_n}")
        tdf.kv("Name", ai.get("name") or f"{ai['shortName']}_{i}")
        tdf.kv("ShortName", ai["shortName"])
        if ai.get("version"):
            tdf.kv("Version", ai["version"])
        tdf.kv("Team", i)
        tdf.kv("Host", index[ai.get("host", HOST_ALIAS)])
        tdf.open("OPTIONS")
        for k, v in (ai.get("options") or {}).items():
            tdf.kv(k, v)
        tdf.close()
        tdf.close()
        ai_n += 1

    for i, t in enumerate(teams):
        tdf.open(f"TEAM{i}")
        leader = index[t["player"]] if "player" in t else index[t["ai"].get("host", HOST_ALIAS)]
        tdf.kv("TeamLeader", leader)
        tdf.kv("AllyTeam", t["allyTeam"])
        tdf.kv("RGBColor", t.get("color", rm.TEAM_COLORS[i]))
        tdf.kv("Side", t.get("side", rm.SIDES[i % len(rm.SIDES)]))
        tdf.kv("Handicap", "%g" % float(t.get("handicap", 0)))
        tdf.close()
    if observer_team is not None:
        tdf.open(f"TEAM{observer_team}")
        tdf.kv("TeamLeader", 0)
        tdf.kv("AllyTeam", n_ally)
        tdf.kv("RGBColor", "0.5 0.5 0.5")
        tdf.kv("Side", rm.SIDES[0])
        tdf.close()

    boxes = s.get("startBoxes")
    for a in range(n_ally + (1 if observer_team is not None else 0)):
        tdf.open(f"ALLYTEAM{a}")
        tdf.kv("NumAllies", 0)
        if boxes and a < len(boxes):
            left, top, right, bottom = boxes[a]
            tdf.kv("StartRectLeft", left)
            tdf.kv("StartRectTop", top)
            tdf.kv("StartRectRight", right)
            tdf.kv("StartRectBottom", bottom)
        tdf.close()

    tdf.open("MODOPTIONS")
    for k, v in s["modoptions"].items():
        tdf.kv(k, v)
    tdf.close()
    tdf.close()
    return tdf.render()


client_script = rm.client_script
engine_paths = rm.engine_paths


# --- Host -------------------------------------------------------------------


def host(s: dict) -> int:
    write_dir = Path(rm.env("BARHOST_WRITE_DIR", "/bar/out/lan/host"))
    write_dir.mkdir(parents=True, exist_ok=True)
    script = write_dir / "script.txt"
    script.write_text(host_script(s), encoding="utf-8")
    settings = rm.engine_settings(write_dir)
    expected = {p["name"] for p in s["players"]}
    log(f"hosting {s['map']} on UDP {s['port']} for {', '.join(sorted(expected)) or 'nobody'}"
        f" (observer team: {'on' if s['observerTeam'] else 'off'})")

    ah = rm.Autohost(53200, log=log)
    proc = rm.Engine(write_dir, script, settings)
    started_at = time.monotonic()
    loaded, connected, left = set(), set(), {}
    result = {"started": False, "gameOver": False, "winningAllyTeams": [], "demo": "",
              "exitReason": "unknown"}
    all_loaded_at = None
    forced = False
    playing_since = None
    kill_at = None

    try:
        while True:
            now = time.monotonic()
            if proc.poll() is not None:
                if result["exitReason"] == "unknown":
                    result["exitReason"] = "engine exited"
                break
            if kill_at is None and now - started_at > s["timeout"]:
                result["exitReason"] = "timeout"
                kill_at = now
                ah.command("kill")
            if kill_at is not None and now - kill_at > 60:
                break
            if (not result["started"] and all_loaded_at is None
                    and now - started_at > s["waitForPlayers"] and kill_at is None):
                missing = sorted(expected - loaded)
                log("players never finished loading: " + ", ".join(missing))
                result["exitReason"] = "players missing: " + ", ".join(missing)
                kill_at = now
                ah.command("kill")
            if all_loaded_at is not None and not forced and now - all_loaded_at >= s["forceStartDelay"]:
                forced = True
                ah.command("forcestart")
            if (playing_since is not None and s["maxGameSeconds"] and kill_at is None
                    and now - playing_since >= s["maxGameSeconds"]):
                log(f"reached maxGameSeconds={s['maxGameSeconds']}; stopping")
                result["exitReason"] = "time limit"
                kill_at = now
                ah.command("kill")

            ev = ah.poll()
            if ev is None:
                continue
            code, data = ev
            if code == rm.SERVER_STARTED:
                log("<- SERVER_STARTED")
                if s["speed"] != 1:
                    ah.command("setmaxspeed %g" % s["speed"])
                    ah.command("setminspeed %g" % s["speed"])
            elif code == rm.SERVER_MESSAGE:
                text = data[1:].decode("utf-8", "replace").strip()
                m = re.match(r"^Connection attempt from (\S+)$", text)
                if m:
                    connected.add(m.group(1))
                    log(f"{m.group(1)} is connecting")
            elif code == rm.PLAYER_JOINED and len(data) > 2:
                name = data[2:].decode("utf-8", "replace").strip("\x00")
                if name in expected and name not in loaded:
                    loaded.add(name)
                    log(f"{name} finished loading ({len(loaded)}/{len(expected)})")
                    if loaded == expected:
                        all_loaded_at = now
                        log(f"everyone is in; starting in {s['forceStartDelay']}s")
            elif code == rm.PLAYER_LEFT and len(data) > 2:
                left[str(data[1])] = int(data[2])
            elif code == rm.SERVER_STARTPLAYING:
                result["started"] = True
                playing_since = now
                if len(data) > 21:
                    result["demo"] = data[21:].decode("utf-8", "replace")
                log("<- SERVER_STARTPLAYING")
            elif code == rm.SERVER_GAMEOVER:
                result["gameOver"] = True
                result["winningAllyTeams"] = list(data[3:])
                result["exitReason"] = "game over"
                log(f"<- SERVER_GAMEOVER winners={result['winningAllyTeams']}")
                if kill_at is None:
                    kill_at = now
                    ah.command("kill")
            if not expected and all_loaded_at is None:
                all_loaded_at = now          # nobody to wait for
    finally:
        ah.close()
        proc.stop()

    result.update({
        "engineVersion": rm.env("BAR_ENGINE_VERSION"), "map": s["map"], "game": s["game"],
        "players": sorted(expected), "playersLoaded": sorted(loaded),
        "playedSeconds": round(time.monotonic() - playing_since, 1) if playing_since else 0,
        "observerTeam": s["observerTeam"],
        "teams": [{"allyTeam": t["allyTeam"], **({"player": t["player"]} if "player" in t else
                  {"ai": t["ai"]["shortName"], "aiHost": t["ai"].get("host", HOST_ALIAS)})} for t in s["teams"]],
        "wallclockSeconds": round(time.monotonic() - started_at, 1),
    })
    (write_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    log(f"finished: {result['exitReason']}")
    ok = result["started"] and result["exitReason"] in ("game over", "time limit")
    return 0 if ok else 1


# --- Join -------------------------------------------------------------------


def join() -> int:
    host_addr = rm.env("BARHOST_LAN_HOST")
    name = rm.env("BARHOST_LAN_NAME")
    password = rm.env("BARHOST_LAN_PASSWORD")
    port = rm.env_int("BARHOST_LAN_PORT", 8452)
    timeout = rm.env_int("BARHOST_LAN_TIMEOUT", 7200)
    if not (host_addr and name and password):
        raise SystemExit("join needs BARHOST_LAN_HOST, BARHOST_LAN_NAME and BARHOST_LAN_PASSWORD")
    write_dir = Path(rm.env("BARHOST_WRITE_DIR", "/bar/out/lan/" + name))
    write_dir.mkdir(parents=True, exist_ok=True)
    script = write_dir / "script.txt"
    script.write_text(client_script(host_addr, port, name, password), encoding="utf-8")
    settings = rm.engine_settings(write_dir)
    log(f"joining {host_addr}:{port} as {name}")

    started = time.monotonic()
    proc = rm.Engine(write_dir, script, settings)
    reason = "engine exited"
    try:
        while proc.poll() is None:
            if time.monotonic() - started > timeout:
                reason = "timeout"
                break
            time.sleep(1)
    finally:
        proc.stop()
    rc = proc.returncode
    infolog = (write_dir / "infolog.txt")
    text = infolog.read_text(encoding="utf-8", errors="replace") if infolog.exists() else ""
    frames = re.findall(r"\[f=(\d+)\]", text)
    result = {"name": name, "host": f"{host_addr}:{port}", "exitReason": reason, "engineReturncode": rc,
              "lastFrame": int(frames[-1]) if frames else -1,
              "wallclockSeconds": round(time.monotonic() - started, 1)}
    (write_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    log(f"finished: {reason}, engine rc={rc}, last frame {result['lastFrame']}")
    return 0 if reason == "engine exited" and rc == 0 else 1


def main(argv) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    try:
        if mode == "host":
            return host(load_spec())
        if mode == "join":
            return join()
        if mode == "script":
            print(host_script(load_spec()), end="")
            return 0
    except SpecError as exc:
        log(f"FAIL: {exc}")
        return 2
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
