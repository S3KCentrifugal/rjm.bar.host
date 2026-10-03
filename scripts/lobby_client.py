#!/usr/bin/env python3
"""A headless lobby client that runs AI bots in a room on BAR's own autohosts.

Entrypoint mode `client`. It needs no inbound network access: every connection
is outbound, as for a player.

  1. Log in to BAR's lobby server (Spring lobby protocol, as Chobby does).
  2. Take an empty room on one of BAR's cluster autohosts (`Host[XXn][k]`),
     join it, become boss, and send the room setup commands
     (BARHOST_ROOM_SETUP, e.g. `!bSet experimentallegionfaction 1`).
  3. Add BARHOST_BOT_COUNT bots it owns, then become a spectator.
  4. When enough humans are in, ready and synced, count down and `!cv start`.
  5. When the autohost's game starts, launch spring-headless as a spectator
     client of it. The engine runs the bots, because this client owns them.
  6. After the game, stay in the room and go back to 4. If the room is lost,
     take another; if the connection is lost, log in again.

The protocol details (LOGIN fields, room choice, `!boss`, ADDBOT, the client
start script) follow BYAR-Chobby's liblobby and battle list window, where the
"Host" button does the same thing for a person.

Settings (environment):
  BARHOST_LOBBY_LOGIN, BARHOST_LOBBY_PASSWORD   the account (password falls
        back to SPADS_LOBBY_PASSWORD, login to SPADS_LOBBY_LOGIN)
  BARHOST_LOBBY_HOST, BARHOST_LOBBY_PORT        server4.beyondallreason.info:8200
  BARHOST_LOBBY_CLUSTER   regex on the autohost manager region, e.g. "EU" (any)
  BARHOST_ROOM_NAME       room title, set with !rename (optional)
  BARHOST_ROOM_SETUP      ';'-separated chat commands sent once boss
                          (default "!preset custom;!bSet experimentallegionfaction 1")
  BARHOST_BOT_AI          AI short name (BARb)
  BARHOST_BOT_COUNT       number of bots (8)
  BARHOST_BOT_ALLY        their ally team, 0-based (1); humans play ally 0
  BARHOST_BOT_SIDES       ','-separated sides cycled over the bots (0,1:
                          Armada, Cortex; 3 is Legion)
  BARHOST_BOT_PROFILE     optional JSON sent as !aiProfile for every bot
  BARHOST_MIN_HUMANS      humans needed before an auto-start (1; 0 = never)
  BARHOST_START_DELAY     countdown in seconds once all are ready (20)
  BARHOST_POSTGAME_DELAY  no auto-start for this many seconds after a game (60)
  BARHOST_OUT_DIR         games are written under $BARHOST_OUT_DIR/client/
  BARHOST_STATUS_FILE     JSON status, rewritten on every change
                          (/tmp/lobby-client-status.json)
"""
import asyncio
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from barcommon import (  # noqa: E402
    Engine, battle_status, client_script, engine_settings, make_log, password_hash, unpack_battle_status,
)

VERSION = "rjm-bar-host-client:1"
FOUNDER_RE = re.compile(r"^Host\[([A-Za-z]+)(\d+)\]\[\d+\]$")

# SPADS kicks clients that talk too fast: at most one command per this many s.
COMMAND_SPACING = 1.5


log = make_log("lobby-client")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


class Settings:
    def __init__(self, e=None):
        g = (lambda k, d="": (e or {}).get(k, d)) if e is not None else env
        self.login = g("BARHOST_LOBBY_LOGIN") or g("SPADS_LOBBY_LOGIN")
        self.password = g("BARHOST_LOBBY_PASSWORD") or g("SPADS_LOBBY_PASSWORD")
        self.host = g("BARHOST_LOBBY_HOST", "server4.beyondallreason.info")
        self.port = int(g("BARHOST_LOBBY_PORT", "8200"))
        self.cluster = re.compile(g("BARHOST_LOBBY_CLUSTER", ".*"))
        self.room_name = g("BARHOST_ROOM_NAME")
        self.setup = [c.strip() for c in
                      g("BARHOST_ROOM_SETUP", "!preset custom;!bSet experimentallegionfaction 1").split(";")
                      if c.strip()]
        self.bot_ai = g("BARHOST_BOT_AI", "BARb")
        self.bot_count = int(g("BARHOST_BOT_COUNT", "8"))
        self.bot_ally = int(g("BARHOST_BOT_ALLY", "1"))
        self.bot_sides = [int(s) for s in g("BARHOST_BOT_SIDES", "0,1").split(",") if s.strip()] or [0]
        self.bot_profile = g("BARHOST_BOT_PROFILE")
        self.min_humans = int(g("BARHOST_MIN_HUMANS", "1"))
        self.start_delay = int(g("BARHOST_START_DELAY", "20"))
        self.postgame_delay = int(g("BARHOST_POSTGAME_DELAY", "60"))
        self.engine_version = g("BAR_ENGINE_VERSION")
        self.out_dir = Path(g("BARHOST_OUT_DIR", "/bar/out")) / "client"
        self.status_file = Path(g("BARHOST_STATUS_FILE", "/tmp/lobby-client-status.json"))
        if not self.login or not self.password:
            raise SystemExit("BARHOST_LOBBY_LOGIN and BARHOST_LOBBY_PASSWORD must be set")


class Battle:
    def __init__(self, bid, founder, ip, port, passworded, engine_name, engine_version, map_name, title, game):
        self.id, self.founder, self.ip, self.port = bid, founder, ip, int(port)
        self.passworded = passworded
        self.engine_name, self.engine_version = engine_name, engine_version
        self.map, self.title, self.game = map_name, title, game
        self.users = {founder}
        self.spectators = 1
        self.locked = False
        self.bots = {}  # name -> {"owner", "status"}


class LobbyClient:
    """The protocol state machine. `feed(line)` and `tick(now)` act on the
    world; everything to send is appended to `self.outbox`. No I/O here."""

    def __init__(self, s: Settings, now=time.monotonic, launch=None):
        self.s = s
        self.now = now
        self.launch = launch or (lambda b, pw: None)
        self.outbox = []
        self.reset()

    # -- state -------------------------------------------------------------

    def reset(self):
        self.state = "connecting"   # connecting, login, lobby, joining, setup, waiting, countdown, ingame
        self.users = {}             # name -> CLIENTSTATUS int
        self.battles = {}
        self.battle = None
        self.script_password = ""
        self.statuses = {}          # name -> unpacked battle status, in my battle
        self.my_status = None
        self.boss = False
        self.pending = []           # chat/commands waiting for the flood spacing
        self.last_sent = 0.0
        self.countdown_end = None
        self.join_attempted = set()
        self.next_join = 0.0
        # A running engine and the game count survive a reconnect.
        self.game_proc = getattr(self, "game_proc", None)
        self.games = getattr(self, "games", 0)
        self.last_error = getattr(self, "last_error", "")
        self.need_fetch = None      # a Battle whose content the I/O layer must fetch
        self.synced = False
        self.boss_deadline = None
        self.quiet_until = 0.0      # no auto-start before this (after a game)

    def status(self) -> dict:
        b = self.battle
        humans = self.humans()
        return {
            "state": self.state, "login": self.s.login, "games": self.games,
            "battle": None if not b else {
                "id": b.id, "founder": b.founder, "title": b.title, "map": b.map, "game": b.game,
                "engine": b.engine_version, "boss": self.boss, "synced": self.synced,
                "bots": sorted(n for n, x in b.bots.items() if x["owner"] == self.s.login),
                "humans": [{"name": n, **{k: self.statuses.get(n, {}).get(k) for k in ("ready", "sync", "ally")}}
                           for n in humans],
            },
            "countdown": None if self.countdown_end is None else max(0, round(self.countdown_end - self.now())),
            "lastError": self.last_error,
        }

    def humans(self) -> list:
        b = self.battle
        if not b:
            return []
        return sorted(n for n in b.users
                      if n not in (b.founder, self.s.login)
                      and self.statuses.get(n, {}).get("mode") == 1)

    # -- sending -----------------------------------------------------------

    def send(self, *words):
        self.outbox.append(" ".join(str(w) for w in words))

    def say(self, text):
        """Battle chat and commands go through the flood spacing."""
        self.pending.append(text)

    def flush_pending(self):
        if self.pending and self.now() - self.last_sent >= COMMAND_SPACING:
            item = self.pending.pop(0)
            if isinstance(item, tuple):
                self.send(*item)
            else:
                self.send("SAYBATTLE", item)
            self.last_sent = self.now()

    # -- lobby lines -------------------------------------------------------

    def feed(self, line: str):
        line = re.sub(r"^#\d+ ", "", line.rstrip("\r\n"))
        if not line:
            return
        cmd = line.split(" ", 1)[0]
        handler = getattr(self, "on_" + cmd, None)
        if handler:
            handler(line)

    def on_TASSERVER(self, line):
        self.state = "login"
        sentence = "LuaLobby %s\t%s %s\tb sp" % (
            VERSION, hashlib.md5(self.s.login.encode()).hexdigest()[:16], "0")
        self.send("LOGIN", self.s.login, password_hash(self.s.password), "0", "*", sentence)

    def on_ACCEPTED(self, line):
        log("logged in as %s" % self.s.login)
        self.state = "lobby"

    def on_DENIED(self, line):
        self.last_error = "login denied: " + line[7:]
        raise ConnectionAbortedError(self.last_error)

    def on_AGREEMENTEND(self, line):
        self.last_error = "the account has not accepted the terms of service: log in once with Chobby"
        raise ConnectionAbortedError(self.last_error)

    def on_QUEUED(self, line):
        log("login queued by the server")

    def on_ADDUSER(self, line):
        w = line.split(" ")
        if len(w) > 1:
            self.users.setdefault(w[1], 0)

    def on_REMOVEUSER(self, line):
        self.users.pop(line.split(" ")[1], None)

    def on_CLIENTSTATUS(self, line):
        _, name, st = line.split(" ")[:3]
        st = int(st)
        was = self.users.get(name, 0)
        self.users[name] = st
        b = self.battle
        if b and name == b.founder and (st & 1) != (was & 1):
            if st & 1:
                self.game_started()
            else:
                log("the autohost's game ended")

    def on_BATTLEOPENED(self, line):
        p = line.split(" ", 11)
        if len(p) < 12:
            return
        rest = p[11].split("\t")
        rest += [""] * (5 - len(rest))
        bid, founder, ip, port, passworded = p[1], p[4], p[5], p[6], p[8] == "1"
        self.battles[bid] = Battle(bid, founder, ip, port, passworded, *rest[:5])

    def on_BATTLECLOSED(self, line):
        bid = line.split(" ")[1]
        self.battles.pop(bid, None)
        if self.battle and self.battle.id == bid:
            self.lost_battle("the room was closed")

    def on_UPDATEBATTLEINFO(self, line):
        p = line.split(" ", 5)
        b = self.battles.get(p[1])
        if b and len(p) >= 5:
            b.spectators, b.locked = int(p[2]), p[3] == "1"
            if len(p) == 6:
                b.map = p[5]

    def on_JOINEDBATTLE(self, line):
        p = line.split(" ")
        b = self.battles.get(p[1])
        if b:
            b.users.add(p[2])
            if p[2] == self.s.login and len(p) > 3:
                self.script_password = p[3]

    def on_LEFTBATTLE(self, line):
        p = line.split(" ")
        b = self.battles.get(p[1])
        if b:
            b.users.discard(p[2])
            if b is self.battle:
                self.statuses.pop(p[2], None)
                if p[2] == self.s.login:
                    self.lost_battle("left the room")

    def on_FORCEQUITBATTLE(self, line):
        self.lost_battle("kicked from the room")

    def on_JOINBATTLEFAILED(self, line):
        self.last_error = line
        log(line)
        self.battle = None
        self.state = "lobby"
        self.next_join = self.now() + 5

    def on_JOINBATTLE(self, line):
        bid = line.split(" ")[1]
        self.battle = self.battles.get(bid)
        if not self.battle:
            return
        self.state = "setup"
        self.boss = False
        self.synced = False
        b = self.battle
        log("joined room %s (%s) on %s, %s, engine %s" % (b.id, b.title, b.founder, b.map, b.engine_version))
        # The game and map download in the background; unsynced until then.
        self.need_fetch = b
        # Join as a player first: SPADS grants boss to players (Chobby does the same).
        self.my_status = battle_status(ready=0, team=0, ally=0, mode=1, sync=2)
        self.pending.append(("MYBATTLESTATUS", self.my_status, 0))
        self.say("!boss " + self.s.login)
        # SPADS runs a command at once when nobody else can vote; if its "Boss
        # mode enabled" reply is missed, set the room up anyway.
        self.boss_deadline = self.now() + 20

    def fetched(self, b: Battle, ok: bool):
        if b is not self.battle:
            return
        if not ok:
            self.last_error = "could not download %s / %s" % (b.game, b.map)
            self.send("LEAVEBATTLE")
            self.lost_battle(self.last_error)
            return
        log("content ready: %s, %s" % (b.game, b.map))
        self.synced = True
        st = unpack_battle_status(self.my_status) if self.my_status is not None else {"mode": 1}
        self.my_status = battle_status(mode=st["mode"], sync=1)
        self.pending.append(("MYBATTLESTATUS", self.my_status, 0))

    def on_REQUESTBATTLESTATUS(self, line):
        if self.my_status is None:
            self.my_status = battle_status(mode=1, sync=1)
        self.send("MYBATTLESTATUS", self.my_status, 0)

    def on_CLIENTBATTLESTATUS(self, line):
        p = line.split(" ")
        if self.battle:
            self.statuses[p[1]] = unpack_battle_status(p[2])

    def on_ADDBOT(self, line):
        p = line.split(" ", 6)
        b = self.battles.get(p[1])
        if b:
            b.bots[p[2]] = {"owner": p[3], "status": unpack_battle_status(p[4])}

    def on_REMOVEBOT(self, line):
        p = line.split(" ")
        b = self.battles.get(p[1])
        if b:
            b.bots.pop(p[2], None)

    def on_UPDATEBOT(self, line):
        p = line.split(" ")
        b = self.battles.get(p[1])
        if b and p[2] in b.bots:
            b.bots[p[2]]["status"] = unpack_battle_status(p[3])

    def on_SAIDBATTLEEX(self, line):
        self.on_SAIDBATTLE(line)

    def on_SAIDBATTLE(self, line):
        p = line.split(" ", 2)
        if len(p) < 3 or not self.battle:
            return
        user, msg = p[1], p[2]
        if user == self.battle.founder and re.search(r"Boss mode enabled for %s\b" % re.escape(self.s.login), msg):
            if not self.boss:
                self.boss = True
                self.configure_room()

    # -- the room ----------------------------------------------------------

    def suitable(self, b: Battle) -> bool:
        m = FOUNDER_RE.match(b.founder)
        if not m or not self.s.cluster.search(m.group(1)):
            return False
        if b.passworded or b.locked or (self.users.get(b.founder, 0) & 1):
            return False
        if b.spectators != 1 or len(b.users) != 1:   # only the autohost itself
            return False
        if self.s.engine_version and self.s.engine_version not in b.engine_version:
            return False
        return b.id not in self.join_attempted

    def try_join(self):
        rooms = [b for b in self.battles.values() if self.suitable(b)]
        if not rooms:
            return
        b = random.choice(rooms)
        self.join_attempted.add(b.id)
        self.script_password = str(random.randint(10 ** 8, 10 ** 9 - 1))
        self.state = "joining"
        self.send("JOINBATTLE", b.id, "empty", self.script_password)

    def configure_room(self):
        b = self.battle
        self.boss_deadline = None
        log("boss in room %s; setting it up" % b.id)
        if self.s.room_name:
            self.say("!rename " + self.s.room_name)
        for c in self.s.setup:
            self.say(c)
        used = {s["team"] for s in self.statuses.values() if s["mode"] == 1}
        used |= {x["status"]["team"] for x in b.bots.values()}
        mine = [n for n, x in b.bots.items() if x["owner"] == self.s.login]
        team = 1
        for i in range(len(mine), self.s.bot_count):
            while team in used:
                team += 1
            used.add(team)
            name = "%s%d" % (self.s.bot_ai, i + 1)
            side = self.s.bot_sides[i % len(self.s.bot_sides)]
            st = battle_status(ready=1, team=team, ally=self.s.bot_ally, mode=1, sync=1, side=side)
            self.pending.append(("ADDBOT", name, st, 0, self.s.bot_ai))
            if self.s.bot_profile:
                self.say("!aiProfile %s %s" % (name, self.s.bot_profile))
        # Then step aside: the account spectates; its engine runs the bots.
        self.my_status = battle_status(ready=0, mode=0, sync=1 if self.synced else 2)
        self.pending.append(("MYBATTLESTATUS", self.my_status, 0))
        self.state = "waiting"

    def lost_battle(self, why):
        if self.battle:
            log("lost room %s: %s" % (self.battle.id, why))
        self.battle = None
        self.boss = False
        self.statuses = {}
        self.my_status = None
        self.pending = []
        self.countdown_end = None
        self.need_fetch = None
        self.synced = False
        self.boss_deadline = None
        if self.state != "ingame":
            self.state = "lobby"
        self.next_join = self.now() + 10

    def ready_to_start(self) -> bool:
        if not self.synced:
            return False
        humans = self.humans()
        if self.s.min_humans <= 0 or len(humans) < self.s.min_humans:
            return False
        return all(self.statuses[h].get("ready") and self.statuses[h].get("sync") == 1 for h in humans)

    def game_started(self):
        b = self.battle
        self.countdown_end = None
        self.state = "ingame"
        self.games += 1
        log("game %d starting on %s:%s" % (self.games, b.ip, b.port))
        self.game_proc = self.launch(b, self.script_password)

    # -- time --------------------------------------------------------------

    def tick(self):
        now = self.now()
        if self.state == "ingame":
            if self.game_proc is not None and self.game_proc.poll() is None:
                return
            if self.battle and self.users.get(self.battle.founder, 0) & 1:
                return     # our engine ended but the game goes on: wait it out
            if self.game_proc is not None and hasattr(self.game_proc, "close"):
                self.game_proc.close()
            self.game_proc = None
            self.state = "waiting" if self.battle else "lobby"
            self.quiet_until = now + self.s.postgame_delay
        if self.state == "setup" and self.boss_deadline is not None and now >= self.boss_deadline \
                and not self.pending:
            log("no boss confirmation; setting the room up anyway")
            self.boss = True
            self.configure_room()
        if self.state == "lobby" and now >= self.next_join:
            self.next_join = now + 5
            self.try_join()
        if self.state in ("waiting", "countdown") and self.boss and not self.pending \
                and now >= self.quiet_until:
            if self.ready_to_start():
                if self.countdown_end is None:
                    self.countdown_end = now + self.s.start_delay
                    self.state = "countdown"
                    self.say("Everyone is ready: starting in %d s." % self.s.start_delay)
                elif now >= self.countdown_end:
                    self.countdown_end = None
                    self.state = "waiting"
                    self.say("!cv start")
            elif self.countdown_end is not None:
                self.countdown_end = None
                self.state = "waiting"
                self.say("Countdown cancelled: waiting for everyone to be ready.")
        self.flush_pending()


# -- I/O --------------------------------------------------------------------

async def fetch_content(c: LobbyClient, b: Battle):
    """Download the room's game and map (pr-downloader skips what is there),
    without blocking the connection."""
    prd = os.environ.get("PRD", "pr-downloader")
    data = os.environ.get("BARHOST_DATA_DIR", "/bar/data")
    ok = True
    for kind, name in (("--download-game", b.game), ("--download-map", b.map)):
        if not name:
            continue
        log("fetching %s" % name)
        try:
            proc = await asyncio.create_subprocess_exec(prd, "--filesystem-writepath", data, kind, name)
            code = await proc.wait()
        except OSError as e:
            code = str(e)
        if code != 0:
            log("pr-downloader failed for %s: %s" % (name, code))
            ok = False
            break
    c.fetched(b, ok)


def launch_engine(s: Settings):
    def launch(b: Battle, script_password: str):
        run = "%s-%s" % (dt.datetime.now().strftime("%Y%m%d-%H%M%S"), b.id)
        d = s.out_dir / "games" / run
        d.mkdir(parents=True, exist_ok=True)
        script = d / "script.txt"
        script.write_text(client_script(b.ip, b.port, s.login, script_password, source_port=0), encoding="utf-8")
        (d / "game.json").write_text(json.dumps(
            {"battle": b.id, "founder": b.founder, "title": b.title, "map": b.map, "game": b.game,
             "engine": b.engine_version, "started": dt.datetime.now().isoformat()}, indent=2))
        log("engine: writing to %s" % d)
        # The console stays in the run directory: the pod log is the client's.
        return Engine(d, script, engine_settings(d), echo=False)
    return launch


def write_status(c: LobbyClient):
    try:
        tmp = c.s.status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(c.status(), indent=2))
        tmp.replace(c.s.status_file)
    except OSError:
        pass


async def session(c: LobbyClient):
    s = c.s
    log("connecting to %s:%d" % (s.host, s.port))
    reader, writer = await asyncio.wait_for(asyncio.open_connection(s.host, s.port), 30)
    c.reset()
    last_ping = time.monotonic()
    try:
        while True:
            try:
                raw = await asyncio.wait_for(reader.readline(), 0.5)
                if raw == b"":
                    raise ConnectionResetError("the server closed the connection")
                c.feed(raw.decode("utf-8", errors="replace"))
            except asyncio.TimeoutError:
                pass
            c.tick()
            if c.need_fetch is not None:
                asyncio.ensure_future(fetch_content(c, c.need_fetch))
                c.need_fetch = None
            if time.monotonic() - last_ping > 30:
                c.send("PING")
                last_ping = time.monotonic()
            for line in c.outbox:
                if not line.startswith("LOGIN "):
                    log("> " + line)
                writer.write((line + "\n").encode("utf-8"))
            c.outbox.clear()
            await writer.drain()
            write_status(c)
    finally:
        writer.close()


def main() -> int:
    s = Settings()
    s.out_dir.mkdir(parents=True, exist_ok=True)
    c = LobbyClient(s, launch=launch_engine(s))
    delay = 10
    while True:
        try:
            asyncio.run(session(c))
        except ConnectionAbortedError as e:
            log("giving up for now: %s" % e)
            delay = 600
        except (OSError, asyncio.TimeoutError) as e:
            c.last_error = str(e)
            log("disconnected: %s" % e)
        c.state = "connecting"
        write_status(c)
        if c.game_proc is not None and c.game_proc.poll() is None:
            log("the game is still running; reconnecting around it")
        time.sleep(delay)
        delay = min(delay * 2, 300) if delay < 600 else 10


if __name__ == "__main__":
    sys.exit(main())
