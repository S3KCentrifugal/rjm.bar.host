#!/usr/bin/env python3
"""Offline end-to-end test of the SPADS lobby host.

Plays BAR's lobby server (Teiserver) for one real client -- SPADS, running in
the `spads` container exactly as it runs in the cluster -- and scripts the
people in the room:

  Owner     SPADS's owner account; sends !commands by private message
  Human1    a player: joins, readies up, and plays -- its game client is a real
            spring-headless connecting to SPADS's game server over UDP
  Spammer   floods the room chat, to prove SPADS's flood protection kicks
  Troll     tries to take the room over: !boss, and votes to boss itself, kick
            the owner, change the room password and strip the bots
  Friend    an ordinary user the owner makes boss, which the guard reverts

Wire format follows SpringLobbyProtocol.pm (the library SPADS itself uses):
space-separated words, then tab-separated "sentences" from a per-command
position. Only the subset of the protocol SPADS exercises is implemented.

Scenario, each step an assertion with a deadline:
   1  SPADS logs in with the configured account and password
   2  it opens the room: title, engine 2026.07.04, BAR game, map, 16 slots
   3  humans-vs-bots mode fills the bot team: N bots of the configured AI
   4  Autopilot raises every bot's bonus to the configured value
   5  the owner's !status is answered privately
   6  Spammer is kicked for flooding
   6a Troll's !boss and boss vote are refused by SPADS (commands.conf), its
      votes against the owner, the password and the bots by the Autopilot
      guard, and it is kicked after three refused votes
   6b boss mode given to a user below the trusted level is switched off
   7  Human1 joins and readies up; Autopilot counts down and starts
   8  balance puts every bot on the bot team, Human1 on the other
   9  SPADS launches its engine and goes in-game; Human1's engine connects
  10  Autopilot force-starts the stalled pre-game; the game keeps running
      (the autohost stays in-game) for the whole window
  11  the bot team survives SURVIVE_SECONDS of game time: no team defeated in
      Human1's log, which is complete once its engine has exited
  12  the owner's !stop ends the game; the room stays open, bots still in it
  13  a second game starts and stops the same way (the room persists)

Writes transcript.log (every protocol line) and result.json to OUT_DIR, and
exits non-zero on the first failed step. With --serve it keeps the lobby up
afterwards until SERVE_UNTIL_FILE exists, so the wrapper can exercise the
control socket against a live room.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

LOGIN = os.environ.get("E2E_LOGIN", "AutoHost")
PASSWORD = os.environ.get("E2E_PASSWORD", "e2e-secret")
OWNER = os.environ.get("E2E_OWNER", "Owner")
AI = os.environ.get("E2E_AI", "SMRTBARb")
BOTS = int(os.environ.get("E2E_BOTS", "8"))
BONUS = int(os.environ.get("E2E_BONUS", "50"))
TITLE = os.environ.get("E2E_TITLE", "rjm-bar-host e2e room")
MAP = os.environ.get("E2E_MAP", "All That Glitters v2.2.3")
ENGINE_VERSION = os.environ.get("BAR_ENGINE_VERSION", "2026.07.04")
SURVIVE_SECONDS = int(os.environ.get("E2E_SURVIVE_SECONDS", "120"))
# Loading plus the pre-game force-start (AUTOPILOT_PREGAME_FORCE in run.sh) before play.
PREGAME_ALLOWANCE = int(os.environ.get("E2E_PREGAME_ALLOWANCE", "75"))
# 0 = lobby profile: no Human1 game client, so one engine instead of two. For
# CI runners too small for two BAR engines; the survival check is skipped.
HUMAN_ENGINE = os.environ.get("E2E_HUMAN_ENGINE", "1") != "0"
OUT_DIR = Path(os.environ.get("E2E_OUT", "/bar/out/e2e"))
SERVE_UNTIL_FILE = Path(os.environ.get("E2E_SERVE_UNTIL", "/bar/out/e2e/stop"))

HUMAN = "Human1"
HUMAN_SCRIPT_PASSWORD = "hpw123"
SPAMMER = "Spammer"
TROLL = "Troll"
FRIEND = "Friend"

# SpringLobbyProtocol.pm: position of the first tab-separated sentence.
SERVER_SENTENCE_POS = {
    "ADDUSER": 4, "BATTLEOPENED": 11, "UPDATEBATTLEINFO": 5, "SAIDBATTLE": 2,
    "SAIDBATTLEEX": 2, "SAIDPRIVATE": 2, "SAIDPRIVATEEX": 2, "SAYPRIVATE": 2,
    "SAYPRIVATEEX": 2, "ADDBOT": 6, "SERVERMSG": 1, "MOTD": 1, "SETSCRIPTTAGS": 1,
}
CLIENT_SENTENCE_POS = {
    "LOGIN": 5, "OPENBATTLE": 9, "UPDATEBATTLEINFO": 4, "SAYBATTLE": 1, "SAYBATTLEEX": 1,
    "SAYPRIVATE": 2, "SAYPRIVATEEX": 2, "SAYBATTLEPRIVATE": 2, "SAYBATTLEPRIVATEEX": 2,
    "ADDBOT": 4, "SETSCRIPTTAGS": 1, "JOINBATTLEDENY": 2, "SAY": 2, "SAYEX": 2,
    "c.battle.update_lobby_title": 1,
}


# The lobby protocol's encodings are shared with the image's lobby client.
sys.path.insert(0, os.environ.get("RJM_BIN", "/opt/rjm-bar-host/bin"))
from barcommon import Engine, battle_status, client_script, password_hash, unpack_battle_status  # noqa: E402


def marshall(words: list) -> bytes:
    words = [str(w) for w in words]
    pos = SERVER_SENTENCE_POS.get(words[0])
    if pos and len(words) > pos:
        line = " ".join(words[:pos]) + " " + "\t".join(words[pos:])
    else:
        line = " ".join(words)
    return (line + "\n").encode("utf-8")


def unmarshall(line: str) -> list:
    line = re.sub(r"^#\d+ ", "", line)
    cmd = line.split(" ", 1)[0]
    pos = CLIENT_SENTENCE_POS.get(cmd)
    if pos:
        words = line.split(" ", pos)
        if len(words) == pos + 1:
            return words[:pos] + words[pos].split("\t")
        return words
    return line.split(" ")


class StepFailed(Exception):
    pass


class FakeLobby:
    def __init__(self):
        self.writer: asyncio.StreamWriter | None = None
        self.peer_ip = ""
        self.compat = set()
        self.transcript = open(OUT_DIR / "transcript.log", "w", encoding="utf-8")
        self.users = {LOGIN: {"status": 64, "id": 1000}}   # the autohost carries the bot flag
        self.next_account = 2000
        self.battle = None          # dict once SPADS opens its room
        self.bots = {}              # name -> {owner, status, color, ai}
        self.members = {}           # name -> {status, color}
        self.founder_ingame = False
        self.private_to = []        # (to, msg) from SPADS
        self.battle_chat = []       # messages SPADS said in the room
        self.kicked = []
        self.left = []
        self.closed = False
        self.logged_in = asyncio.Event()
        self.changed = asyncio.Event()
        self.login_ok = None

    # --- plumbing ---------------------------------------------------------
    def log(self, direction: str, text: str):
        self.transcript.write(f"{time.strftime('%H:%M:%S')} {direction} {text.rstrip()}\n")
        self.transcript.flush()

    def send(self, *words):
        data = marshall(list(words))
        self.log("S>", data.decode())
        if self.writer:
            self.writer.write(data)

    def touch(self):
        self.changed.set()

    async def wait_for(self, what: str, predicate, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            if predicate():
                print(f"  ok   {what}", flush=True)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StepFailed(f"timed out after {timeout:.0f}s: {what}")
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=min(remaining, 2))
            except asyncio.TimeoutError:
                pass

    # --- server side ------------------------------------------------------
    async def handle(self, reader, writer):
        self.writer = writer
        self.peer_ip = writer.get_extra_info("peername")[0]
        self.log("--", f"connection from {self.peer_ip}")
        self.send("TASSERVER", "0.38-33-ga5f3b28", "*", "8201", "0")
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                self.log("C>", line)
                try:
                    self.dispatch(unmarshall(line))
                except Exception as exc:  # a protocol surprise is a test failure, but keep the log
                    self.log("!!", f"handler error for {line!r}: {exc!r}")
                self.touch()
                await writer.drain()
        finally:
            self.log("--", "connection closed")
            self.writer = None
            self.touch()

    def dispatch(self, w: list):
        cmd = w[0]
        h = getattr(self, "c_" + cmd.replace(".", "_"), None)
        if h:
            h(w)
        else:
            self.log("..", f"ignored {cmd}")

    def c_PING(self, w):
        self.send("PONG")

    def c_LOGIN(self, w):
        user, passwd = w[1], w[2]
        expected = password_hash(PASSWORD)
        if user != LOGIN or passwd != expected:
            self.login_ok = False
            self.send("DENIED", "Invalid username or password")
            self.touch()
            return
        if len(w) > 7:
            self.compat = set(w[7].split())
        self.login_ok = True
        self.send("ACCEPTED", user)
        self.send("MOTD", "rjm-bar-host offline e2e lobby")
        for name, u in self.users.items():
            self.send("ADDUSER", name, "XX", u["id"], "e2e")
        for name, u in self.users.items():
            self.send("CLIENTSTATUS", name, u["status"])
        self.send("LOGININFOEND")
        self.logged_in.set()

    def c_MYSTATUS(self, w):
        status = int(w[1]) | 64          # keep the bot flag, as the real server would
        self.users[LOGIN]["status"] = status
        was = self.founder_ingame
        self.founder_ingame = bool(status & 1)
        if was != self.founder_ingame:
            self.log("==", f"autohost in-game: {self.founder_ingame}")
        self.send("CLIENTSTATUS", LOGIN, status)

    def c_OPENBATTLE(self, w):
        # OPENBATTLE type natType password port maxPlayers gameHash rank mapHash
        #            {engineName} {engineVersion} {map} {title} {gameName}
        (_, btype, nat, password, port, maxp, _ghash, rank, mhash,
         engine, engine_ver, map_, title, game) = w[:14]
        self.battle = {"id": 1, "type": btype, "nat": nat, "password": password, "port": int(port),
                       "maxPlayers": int(maxp), "rank": rank, "mapHash": mhash, "engine": engine,
                       "engineVersion": engine_ver, "map": map_, "title": title, "game": game}
        passworded = 0 if password == "*" else 1
        self.send("BATTLEOPENED", 1, btype, nat, LOGIN, self.peer_ip, port, maxp, passworded,
                  rank, mhash, engine, engine_ver, map_, title, game)
        self.send("OPENBATTLE", 1)
        self.send("REQUESTBATTLESTATUS")

    def c_MYBATTLESTATUS(self, w):
        self.send("CLIENTBATTLESTATUS", LOGIN, w[1], w[2])

    def c_UPDATEBATTLEINFO(self, w):
        _, spec, locked, mhash, map_ = w[:5]
        if self.battle:
            self.battle["map"] = map_
        self.send("UPDATEBATTLEINFO", 1, spec, locked, mhash, map_)

    def c_ADDBOT(self, w):
        _, name, status, color, ai = w[:5]
        self.bots[name] = {"owner": LOGIN, "status": int(status), "color": color, "ai": ai}
        self.send("ADDBOT", 1, name, LOGIN, status, color, ai)

    def c_UPDATEBOT(self, w):
        _, name, status, color = w[:4]
        if name in self.bots:
            self.bots[name].update(status=int(status), color=color)
        self.send("UPDATEBOT", 1, name, status, color)

    def c_REMOVEBOT(self, w):
        self.bots.pop(w[1], None)
        self.send("REMOVEBOT", 1, w[1])

    def c_SAYBATTLE(self, w):
        self.battle_chat.append(w[1] if len(w) > 1 else "")
        self.send("SAIDBATTLE", LOGIN, w[1] if len(w) > 1 else "")

    def c_SAYBATTLEEX(self, w):
        self.battle_chat.append(w[1] if len(w) > 1 else "")
        self.send("SAIDBATTLEEX", LOGIN, w[1] if len(w) > 1 else "")

    def c_SAYPRIVATE(self, w):
        self.private_to.append((w[1], w[2] if len(w) > 2 else ""))
        self.send("SAYPRIVATE", w[1], w[2] if len(w) > 2 else "")

    c_SAYPRIVATEEX = c_SAYPRIVATE

    def c_SAYBATTLEPRIVATE(self, w):
        self.private_to.append((w[1], w[2] if len(w) > 2 else ""))

    c_SAYBATTLEPRIVATEEX = c_SAYBATTLEPRIVATE

    def c_SETSCRIPTTAGS(self, w):
        self.send("SETSCRIPTTAGS", *w[1:])

    def c_JOINBATTLEACCEPT(self, w):
        self._joined(w[1])

    def c_JOINBATTLEDENY(self, w):
        self.log("==", f"join denied for {w[1]}: {w[2:] }")

    def c_FORCETEAMNO(self, w):
        self._force(w[1], "team", int(w[2]))

    def c_FORCEALLYNO(self, w):
        self._force(w[1], "ally", int(w[2]))

    def c_FORCESPECTATORMODE(self, w):
        self._force(w[1], "mode", 0)

    def c_HANDICAP(self, w):
        self._force(w[1], "bonus", int(w[2]))

    def c_FORCETEAMCOLOR(self, w):
        if w[1] in self.members:
            self.members[w[1]]["color"] = w[2]
            self.send("CLIENTBATTLESTATUS", w[1], self.members[w[1]]["status"], w[2])

    def _force(self, name, field, value):
        m = self.members.get(name)
        if not m:
            return
        st = unpack_battle_status(m["status"])
        st[field] = value
        m["status"] = battle_status(**st)
        self.send("CLIENTBATTLESTATUS", name, m["status"], m["color"])

    def c_KICKFROMBATTLE(self, w):
        name = w[1]
        self.kicked.append(name)
        if self.members.pop(name, None) is not None:
            self.send("LEFTBATTLE", 1, name)

    def c_LEAVEBATTLE(self, w):
        self.closed = True
        self.send("BATTLECLOSED", 1)

    def c_GETUSERINFO(self, w):
        self.send("SERVERMSG", "e2e lobby: no user info")

    # --- virtual users ------------------------------------------------------
    def user_online(self, name, status=0):
        self.next_account += 1
        self.users[name] = {"status": status, "id": self.next_account}
        self.send("ADDUSER", name, "XX", self.next_account, "e2e")
        self.send("CLIENTSTATUS", name, status)

    def user_join(self, name, script_password):
        self.members[name] = {"status": 0, "color": "0", "scriptPassword": script_password}
        if "b" in self.compat:
            self.send("JOINBATTLEREQUEST", name, "10.0.0.99")
        else:
            self._joined(name)

    def _joined(self, name):
        m = self.members.get(name)
        if m is None:
            return
        self.send("JOINEDBATTLE", 1, name, m["scriptPassword"])
        m["joined"] = True

    def user_status(self, name, **fields):
        m = self.members[name]
        m["status"] = battle_status(**fields)
        m["color"] = str(0x3355FF)
        self.send("CLIENTBATTLESTATUS", name, m["status"], m["color"])

    def user_say_battle(self, name, msg):
        self.send("SAIDBATTLE", name, msg)

    def user_say_private(self, name, msg):
        self.send("SAIDPRIVATE", name, msg)

    # --- views ----------------------------------------------------------------
    def bot_statuses(self):
        return {n: unpack_battle_status(b["status"]) for n, b in self.bots.items()}


class HumanEngine:
    """Human1's game client: a real spring-headless joining SPADS's game."""

    def __init__(self, host: str, port: int, run: int):
        self.dir = OUT_DIR / f"human-game-{run}"
        self.dir.mkdir(parents=True, exist_ok=True)
        script = self.dir / "script.txt"
        script.write_text(client_script(host, port, HUMAN, HUMAN_SCRIPT_PASSWORD), encoding="utf-8")
        self.engine = Engine(self.dir, script, self.config(), echo=False)

    @staticmethod
    def config():
        """The same long network timeouts run.sh gives the host (slow CI runners)."""
        cfg = Path("/tmp/e2e-springsettings.cfg")
        return cfg if cfg.exists() else None

    def infolog(self) -> str:
        p = self.dir / "infolog.txt"
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

    def frame(self) -> int:
        frames = re.findall(r"\[f=(\d+)\]", self.infolog())
        return int(frames[-1]) if frames else -1

    def stop(self):
        self.engine.stop(grace=20)


async def scenario(lobby: FakeLobby) -> dict:
    result = {"steps": []}

    def step(name):
        result["steps"].append(name)
        print(f"step {len(result['steps']):2d}: {name}", flush=True)

    step("SPADS logs in with the configured account")
    await lobby.wait_for("LOGIN accepted", lambda: lobby.login_ok is not None, 240)
    if not lobby.login_ok:
        raise StepFailed("SPADS sent the wrong login or password")

    step("SPADS opens the room")
    await lobby.wait_for("OPENBATTLE received", lambda: lobby.battle is not None, 180)
    b = lobby.battle
    checks = {
        "title": (b["title"], TITLE), "map": (b["map"], MAP),
        "maxPlayers": (b["maxPlayers"], 16),
    }
    for key, (got, want) in checks.items():
        if got != want:
            raise StepFailed(f"room {key} is {got!r}, expected {want!r}")
    if ENGINE_VERSION not in b["engineVersion"]:
        raise StepFailed(f"room engine version {b['engineVersion']!r} does not name {ENGINE_VERSION}")
    if not b["game"].startswith("Beyond All Reason"):
        raise StepFailed(f"room game is {b['game']!r}")
    print(f"  ok   room: {b['title']!r}, {b['engine']} {b['engineVersion']}, {b['game']}, {b['map']}", flush=True)
    result["room"] = b

    step(f"humans-vs-bots mode adds {BOTS} x {AI}")
    await lobby.wait_for(f"{BOTS} bots of {AI}",
                         lambda: len([x for x in lobby.bots.values() if x["ai"].split("|")[0] == AI]) >= BOTS, 120)

    step(f"Autopilot sets every bot's bonus to {BONUS}%")
    await lobby.wait_for("bonus on every bot",
                         lambda: all(s["bonus"] == BONUS for s in lobby.bot_statuses().values()), 60)

    step("the owner's !status is answered privately")
    lobby.user_online(OWNER)
    await asyncio.sleep(1)
    lobby.user_say_private(OWNER, "!status")
    await lobby.wait_for("private answer to the owner", lambda: any(t == OWNER for t, _ in lobby.private_to), 30)

    step("flood protection kicks a spammer")
    lobby.user_online(SPAMMER)
    lobby.user_join(SPAMMER, "spam")
    await lobby.wait_for("spammer in the room", lambda: lobby.members.get(SPAMMER, {}).get("joined"), 20)
    lobby.user_status(SPAMMER, mode=0, sync=1)
    for i in range(12):
        lobby.user_say_battle(SPAMMER, f"spam {i}")
    await lobby.wait_for("spammer kicked", lambda: SPAMMER in lobby.kicked, 30)

    step("a troll cannot take the room over")
    # The owner sits in the room as a spectator, so there is someone to target.
    lobby.user_join(OWNER, "own")
    await lobby.wait_for("owner in the room", lambda: lobby.members.get(OWNER, {}).get("joined"), 20)
    lobby.user_status(OWNER, mode=0, sync=1)
    lobby.user_online(TROLL)
    lobby.user_join(TROLL, "troll")
    await lobby.wait_for("troll in the room", lambda: lobby.members.get(TROLL, {}).get("joined"), 20)
    lobby.user_status(TROLL, mode=1, ready=0, sync=1)   # a player, so it may call votes; never ready

    async def troll_says(msg, expects, what):
        seen = len(lobby.battle_chat)
        lobby.user_say_battle(TROLL, msg)
        await lobby.wait_for(what, lambda: any(e in m for m in lobby.battle_chat[seen:] for e in expects), 20)
        await asyncio.sleep(2.5)     # stay under SPADS's command flood limit

    await troll_says("!boss Troll", ["not allowed"], "direct !boss refused by SPADS (commands.conf)")
    await troll_says("!cv boss Troll", ["not allowed"], "!boss vote refused by SPADS (commands.conf)")
    await troll_says(f"!cv kick {OWNER}", ["refused"], "vote to kick the owner refused (guard, strike 1)")
    # SPADS itself rejects this one: the hosting preset allows only the configured password.
    await troll_says("!cv hSet password pwned", ["refused", "not allowed"], "vote to change the room password refused")
    await troll_says(f"!cv spec {OWNER}", ["refused"], "vote to spec the owner refused (guard via alias, strike 2)")
    lobby.user_say_battle(TROLL, "!cv set autoAddBotNb 0")   # strike 3
    await lobby.wait_for("troll kicked after 3 refused takeover votes", lambda: TROLL in lobby.kicked, 30)
    if len(lobby.bots) < BOTS:
        raise StepFailed("the bots were removed")

    step("a boss below the trusted level is removed")
    lobby.user_online(FRIEND)
    lobby.user_join(FRIEND, "friend")
    await lobby.wait_for("friend in the room", lambda: lobby.members.get(FRIEND, {}).get("joined"), 20)
    lobby.user_status(FRIEND, mode=0, sync=1)
    seen = len(lobby.battle_chat)
    lobby.user_say_private(OWNER, f"!boss {FRIEND}")
    await lobby.wait_for("guard disabled boss mode",
                         lambda: any("boss mode is reserved" in m for m in lobby.battle_chat[seen:]), 30)

    engines = []
    for run in (1, 2):
        step(f"game {run}: {HUMAN} readies up and Autopilot starts the game")
        if run == 1:
            lobby.user_online(HUMAN)
            lobby.user_join(HUMAN, HUMAN_SCRIPT_PASSWORD)
            await lobby.wait_for(f"{HUMAN} in the room", lambda: lobby.members.get(HUMAN, {}).get("joined"), 20)
        lobby.user_status(HUMAN, ready=1, ally=0, team=0, mode=1, sync=1, side=0)
        await lobby.wait_for("autohost in-game", lambda: lobby.founder_ingame, 120)

        if run == 1:
            step("balance: every bot on the bot team, the human on the other")
            teams = {s["ally"] for s in lobby.bot_statuses().values()}
            human_team = unpack_battle_status(lobby.members[HUMAN]["status"])["ally"]
            if len(teams) != 1 or human_team in teams:
                raise StepFailed(f"bots on teams {sorted(teams)}, human on team {human_team}")
            print(f"  ok   bots on team {teams.pop()}, {HUMAN} on team {human_team}", flush=True)

        if not HUMAN_ENGINE:
            # Lobby profile (CI): no second engine, so no play -- the host sits
            # in pre-game (Autopilot never forces while a player is loading).
            step(f"game {run}: SPADS's engine is up (lobby profile: no {HUMAN} game client)")
            await asyncio.sleep(20)
            if not lobby.founder_ingame:
                raise StepFailed("the host engine exited before the owner stopped it")
            print("  ok   host engine still running after 20s", flush=True)
            step(f"game {run}: the owner's !stop ends it and the room stays open")
            lobby.user_say_private(OWNER, "!stop")
            await lobby.wait_for("autohost back in the lobby", lambda: not lobby.founder_ingame, 120)
            await asyncio.sleep(5)
            if lobby.closed:
                raise StepFailed("the room was closed after the game")
            if len(lobby.bots) < BOTS:
                raise StepFailed(f"only {len(lobby.bots)} bots left in the room after the game")
            print(f"  ok   room open, {len(lobby.bots)} bots still in it", flush=True)
            continue

        step(f"game {run}: {HUMAN}'s engine connects and the game runs")
        engine = HumanEngine(lobby.peer_ip, lobby.battle["port"], run)
        engines.append(engine)
        # A client's infolog is buffered and nearly silent during play, so the
        # live signal is the autohost's in-game status: if a team collapse (or
        # anything else) ends the game, SPADS stops the engine and leaves
        # in-game within seconds. The window covers loading, the pre-game
        # force-start, and the game time that must survive.
        window = PREGAME_ALLOWANCE + (SURVIVE_SECONDS if run == 1 else 20)
        started = time.monotonic()
        while time.monotonic() - started < window:
            if not lobby.founder_ingame:
                raise StepFailed(f"the game ended after {time.monotonic() - started:.0f}s, before {window}s "
                                 f"-- a team collapse? (see {engine.dir})")
            if engine.engine.poll() is not None:
                raise StepFailed(f"{HUMAN}'s engine exited early (see {engine.dir}/engine-console.log)")
            await asyncio.sleep(2)
        print(f"  ok   game still running after {window}s", flush=True)

        step(f"game {run}: the owner's !stop ends it and the room stays open")
        lobby.user_say_private(OWNER, "!stop")
        await lobby.wait_for("autohost back in the lobby", lambda: not lobby.founder_ingame, 120)
        # The server is gone; the client quits by itself and flushes its log.
        await lobby.wait_for(f"{HUMAN}'s engine exited", lambda: engine.engine.poll() is not None, 90)
        engine.stop()

        frame = engine.frame()
        if run == 1:
            step(f"the bot team survived {SURVIVE_SECONDS}s of game time")
            log = engine.infolog()
            defeated = re.findall(r"\[f=(\d+)\][^\n]*(?:is defeated|has gone to a better place|has bowed out)", log)
            result["collapse"] = {"lastFrame": frame, "defeatedLines": len(defeated),
                                  "firstDefeatFrame": int(defeated[0]) if defeated else None}
            if defeated:
                raise StepFailed(f"a team was defeated at frame {defeated[0]} -- the team-collapse risk is real "
                                 f"for lobby games (see {engine.dir}/infolog.txt)")
            if frame < SURVIVE_SECONDS * 30 * 0.8:
                raise StepFailed(f"{HUMAN}'s game only reached frame {frame}, short of {SURVIVE_SECONDS}s")
            print(f"  ok   reached frame {frame} with no team defeated", flush=True)
        await asyncio.sleep(5)
        if lobby.closed:
            raise StepFailed("the room was closed after the game")
        if len(lobby.bots) < BOTS:
            raise StepFailed(f"only {len(lobby.bots)} bots left in the room after the game")
        print(f"  ok   room open, {len(lobby.bots)} bots still in it", flush=True)

    result["ok"] = True
    return result


async def main(serve: bool) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SERVE_UNTIL_FILE.unlink(missing_ok=True)
    lobby = FakeLobby()
    server = await asyncio.start_server(lobby.handle, "0.0.0.0", 8200)
    print("fake lobby listening on :8200", flush=True)
    rc = 0
    try:
        result = await scenario(lobby)
    except StepFailed as exc:
        result = {"ok": False, "error": str(exc)}
        print(f"FAIL: {exc}", flush=True)
        rc = 1
    result["bots"] = lobby.bot_statuses()
    result["battleChat"] = lobby.battle_chat[-40:]
    (OUT_DIR / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    print("PASS" if rc == 0 else "FAILED", "- transcript and result in", OUT_DIR, flush=True)
    (OUT_DIR / "scenario-done").write_text(str(rc), encoding="utf-8")

    if serve:
        print("serving the room until", SERVE_UNTIL_FILE, "exists", flush=True)
        while not SERVE_UNTIL_FILE.exists():
            await asyncio.sleep(1)
        (OUT_DIR / "final-room.json").write_text(json.dumps(
            {"map": (lobby.battle or {}).get("map"), "bots": lobby.bot_statuses(),
             "battleChat": lobby.battle_chat[-20:]}, indent=2) + "\n", encoding="utf-8")
    server.close()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--serve" in sys.argv)))
