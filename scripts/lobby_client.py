#!/usr/bin/env python3
"""A headless lobby client that runs AI bots in rooms on BAR's own autohosts.

Entrypoint mode `client`. It needs no inbound network access: every connection
is outbound, as for a player. Two ways to use it (BARHOST_CLIENT_MODE):

follow (a person hosts, the client brings the bots)
  1. Log in and wait. When a user in BARHOST_FOLLOW is in a room, join it as a
     spectator (a passworded room needs `!join <password>` by private message).
     BAR's server does not tell this client who is in other rooms (see
     NOT_SEEN), so the user can name the room instead: `!room <room> [password]`,
     where <room> is its number, its host (autohost or hosting player), a
     player seen in it, or its title (whole or a unique part).
  2. Add BARHOST_BOT_COUNT bots it owns: BAR's server takes ADDBOT only from
     a player, so the client is a player for the adds, then spectates again
     (the bots stay). The followed user can change the count by
     private message: `!bots <n> [AI]`; also `!leave`, `!join [password]`,
     `!room`, `!status`. The followed user stays boss and starts the game.
  3. When the game starts, run spring-headless as a spectator client; it runs
     the bots. When the followed user leaves the room, remove the bots and leave.

room (the client hosts by itself)
  1. Log in to BAR's lobby server (Spring lobby protocol, as Chobby does).
  2. Public: take an empty room on one of BAR's cluster autohosts
     (`Host[XXn][k]`). Private: PM a region manager (`Host[XXn]`)
     `!privatehost`, as Chobby's "passworded private battle" does, and join
     the room it opens with the password it answers. Join as a player,
     become boss, and send the room setup commands (BARHOST_ROOM_SETUP, e.g.
     `!bSet experimentallegionfaction 1`). A BARHOST_FOLLOW user switches
     with `!public` / `!private` by PM; the choice survives restarts
     (BARHOST_STATE_FILE).
  3. Once boss, spectate: a headless host never plays. Settings and maps
     are a boss's; only adding bots (BAR's server takes ADDBOT only from
     players) takes a player seat, for the adds alone. A start called while
     it holds that seat (`!start`, `!cv start`, `!forcestart`, a vote for
     one) makes it spectate at once.
  4. While boss, give boss to BARHOST_FOLLOW users as they join (`!boss`),
     and end at once (`!ev`) any vote to unboss everyone, this client, or a
     BARHOST_FOLLOW user. A BARHOST_FOLLOW user switches the bots' AI with
     `!barb` / `!smrt` (kept across restarts; done as a player, after the
     game if one is on), and `!stop` asks the players to vote `!cv stop`
     (a spectator may not stop a game). Each joiner gets BARHOST_WELCOME
     privately.
     A player's "bonus" gets one line on voting the AIs' bonus (all, or one),
     once a minute; a passed bonus vote is applied to every AI
     (`!setallaibonus`) and kept for the room's later bot adds.
     When a player rings another (`!ring <name>`), say
     how to vote an away player to spectator, at most once a minute; the room
     decides.
  5. When enough humans are in, ready and synced, count down and `!cv start`.
  6. When the autohost's game starts, launch spring-headless as a spectator
     client of it. The engine runs the bots, because this client owns them.
  7. After the game, stay in the room and go back to 5. If the room is lost,
     take another; if the connection is lost, log in again.

The protocol details (LOGIN fields, room choice, `!boss`, ADDBOT, the client
start script) follow BYAR-Chobby's liblobby and battle list window, where the
"Host" button does the same thing for a person.

Settings (environment):
  BARHOST_CLIENT_MODE     room (default) or follow
  BARHOST_FOLLOW          follow mode: ','-separated users to follow and obey
  BARHOST_LOBBY_LOGIN, BARHOST_LOBBY_PASSWORD   the account (password falls
        back to SPADS_LOBBY_PASSWORD, login to SPADS_LOBBY_LOGIN)
  BARHOST_LOBBY_HOST, BARHOST_LOBBY_PORT        server4.beyondallreason.info:8200
  BARHOST_LOBBY_CLUSTER   regex on the autohost manager region, e.g. "EU" (any)
  BARHOST_ROOM_PRIVACY    room mode: public (default) or private, first start only
  BARHOST_STATE_FILE      where !public / !private is kept
                          (/bar/data/lobby-client-state.json)
  BARHOST_MAP_ROTATION    room mode: ';'-separated maps, one per game, in order
                          (its place is kept in BARHOST_STATE_FILE)
  BARHOST_ROOM_NAME       room title, set with !rename (optional)
  BARHOST_START_BOXES_FILE  room mode: JSON of start boxes per map, applied
                          after every map change (optional; see start_box_for)
  BARHOST_START_BOX_KEYS  ','-separated variants to use, first found wins
                          (team2_ai,override)
  BARHOST_WELCOME         room mode: said privately in the room to each joiner
  BARHOST_AI_UPDATE_EVERY seconds between checks for a new release of a
                          BARHOST_AI_ARCHIVES moving tag, only between games,
                          and after every game (900; 0 = never)
  BARHOST_ROOM_SETUP      ';'-separated chat commands sent once boss
                          (default "!preset custom;!bSet experimentallegionfaction 1")
  BARHOST_BOT_AI          AI short name (BARb)
  BARHOST_BOT_COUNT       number of bots (8); in follow mode, added on joining
  BARHOST_BOT_ALLY        their ally team, 0-based (1); humans play ally 0
  BARHOST_BOT_SIDES       ','-separated sides cycled over the bots (2, Random;
                          0 Armada, 1 Cortex, 3 Legion)
  BARHOST_BOT_AI_VERSION  the AI version in the bots' names, as Chobby shows
                          them with simple AI names off (stable: BARbstable(1))
  BARHOST_BOT_PROFILE     optional JSON sent as !aiProfile for every bot
  BARHOST_BOT_SETUP       ';'-separated chat commands sent each time after the
                          client adds bots, e.g. "!setallaibonus 50"
  BARHOST_FRIEND_JOINERS  room mode: send a friend request to each player who
                          joins the room, once per account (0; 1 = on)
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
import cgroup_perf  # noqa: E402
from barcommon import (  # noqa: E402
    Engine, battle_status, client_script, engine_settings, make_log, password_hash, unpack_battle_status,
)

VERSION = "rjm-bar-host-client:1"
FOUNDER_RE = re.compile(r"^Host\[([A-Za-z]+)(\d+)\]\[\d+\]$")
# A region's autohost manager, which opens private rooms on `!privatehost`
# (BYAR-Chobby battle_list_window.lua, HostBattle).
MANAGER_RE = re.compile(r"^Host\[([A-Za-z]+)\d+\]$")
PRIVATE_WAIT = 30           # seconds to wait for the private room to appear
VACANCY_WAIT = 3            # seconds after joining before judging a room free
MAP_WAIT = 20               # seconds for the autohost to switch to a !map before skipping it
VOTE_RE = re.compile(r'called a vote for command "([^"]*)"')
# A game about to start (a player's command, or the autohost's vote): if it
# holds a player seat (only ever to add bots) it spectates at once, so its
# engine is never a lagging player (the server slows the game for players,
# GameServer.cpp:905).
START_RE = re.compile(r"^!(?:cv\s+)?(?:force)?start\b", re.IGNORECASE)
# A player ringing another (Chobby's "Ring" sends `!ring <name>`): answer with
# how the room can vote an away player to spectator (Chobby's "Force
# Spectator" is `!spec <name>`); the room decides. At most once a minute.
RING_RE = re.compile(r"^!ring(?=\s|$)\s*(\S+)?", re.IGNORECASE)
RING_HINT = "If %s is away: vote them to spectator with !cv spec %s, then !vote y."
RING_HINT_EVERY = 60
# A player mentioning "bonus": how to vote the AIs' bonus (all, or one AI by
# SPADS `force <name> bonus <n>`, docker/spads/plugins/Autopilot.pm), at most
# once a minute. A passed bonus vote is applied to every AI with
# `!setallaibonus`, and used for this room's later bot adds.
BONUS_RE = re.compile(r"\bbonus\b", re.IGNORECASE)
BONUS_HINT = ("Bonus: vote for all AI with !cv setallaibonus <0-100>, or one with "
              "!cv force %<AI name> bonus <0-100>, then !vote y.")
BONUS_HINT_EVERY = 60
BONUS_PASSED_RE = re.compile(r'Vote for command "(?:setallaibonus|force \S+ bonus) (\d{1,3})" passed', re.IGNORECASE)
SETALL_RE = re.compile(r"^!setallaibonus\s+\d+\s*$", re.IGNORECASE)
# An authorised user's `!stop` by PM: a spectator (host or this client) may
# not stop a game (SPADS [stop] is for players only), so ask the players to
# vote, and leave it to them. At most once a minute.
STOP_HINT = "%s asks to end this game: a player can type !cv stop, then the others !vote y."
STOP_HINT_EVERY = 60
# !stop while this engine is in the game: it leaves, which resigns the bots.
RESIGN_SAY = "%s ended this game: resigning the AI bots."
RESIGN_KILL_AFTER = 20
# Authorised users switch the bots' AI by PM; the choice is kept.
AI_COMMANDS = {"!barb": "BARb", "!smrt": "SMRTBARb"}

# SPADS kicks clients that talk too fast: at most one command per this many s.
COMMAND_SPACING = 1.5
# Seconds after joining before "everyone I follow left" applies: the room's
# member list arrives just after JOINBATTLE.
JOIN_SETTLE = 10
# follow mode: seconds after joining before adding the bots, so the members'
# teams are known and the bots take free ones.
BOTS_DELAY = 3

# BAR's server tells a client who joins other rooms only if it is a lobby it
# lists (Teiserver spring_in.ex @optimisation_level: Chobby, skylobby,
# modlobby); every other client hears only about its own room
# (spring_tcp_server.ex handle_info :joined_lobby, protocol_optimisation :full).
# So following by itself can fail, and the user names the room instead.
NOT_SEEN = ("I cannot see you in a room (BAR's server does not tell me who is in other rooms). "
            "PM me !room <room number or host name> [password].")


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
        self.mode = g("BARHOST_CLIENT_MODE", "room")
        if self.mode not in ("room", "follow"):
            raise SystemExit("BARHOST_CLIENT_MODE must be room or follow")
        self.follow = {u.strip() for u in g("BARHOST_FOLLOW").replace(";", ",").split(",") if u.strip()}
        if self.mode == "follow" and not self.follow:
            raise SystemExit("follow mode needs BARHOST_FOLLOW (the users to follow)")
        self.room_name = g("BARHOST_ROOM_NAME")
        # Room mode: said privately in the room to each player who joins.
        self.welcome = g("BARHOST_WELCOME")
        self.setup = [c.strip() for c in
                      g("BARHOST_ROOM_SETUP", "!preset custom;!bSet experimentallegionfaction 1").split(";")
                      if c.strip()]
        self.bot_ai = g("BARHOST_BOT_AI", "BARb")
        self.bot_count = int(g("BARHOST_BOT_COUNT", "8"))
        self.bot_ally = int(g("BARHOST_BOT_ALLY", "1"))
        # Default 2, Random, as Chobby gives the AIs it adds (BYAR-Chobby
        # ai_list_window.lua AddAi side = 2; sidedata.lua: 0 Armada, 1 Cortex,
        # 2 Random, 3 Legion).
        self.bot_sides = [int(s) for s in g("BARHOST_BOT_SIDES", "2").split(",") if s.strip()] or [2]
        # Bots are named as Chobby names them with its simple AI names off:
        # "<shortName> <version> (<n>)" without spaces, e.g. BARbstable(1).
        self.bot_ai_version = g("BARHOST_BOT_AI_VERSION", "stable")
        self.bot_profile = g("BARHOST_BOT_PROFILE")
        self.bot_setup = [c.strip() for c in g("BARHOST_BOT_SETUP").split(";") if c.strip()]
        self.privacy = g("BARHOST_ROOM_PRIVACY", "public")
        if self.privacy not in ("public", "private"):
            raise SystemExit("BARHOST_ROOM_PRIVACY must be public or private")
        self.state_file = Path(g("BARHOST_STATE_FILE",
                                 os.path.join(g("BARHOST_DATA_DIR", "/bar/data"), "lobby-client-state.json")))
        # Room mode: maps to cycle through, one per game (';'-separated names as
        # the autohost's !map takes them; a part of a name is enough if unique).
        self.map_rotation = [m.strip() for m in g("BARHOST_MAP_ROTATION").split(";") if m.strip()]
        # Room mode: start boxes per map, sent as the mapmetadata_startbox_override
        # mod option after every map change, whoever changed it.
        self.start_box_keys = [k.strip() for k in g("BARHOST_START_BOX_KEYS", "team2_ai,override").split(",")
                               if k.strip()]
        self.start_boxes = load_start_boxes(g("BARHOST_START_BOXES_FILE"), self.start_box_keys)
        # Seconds between checks for a new release of a BARHOST_AI_ARCHIVES
        # source on a moving tag (@latest / @newest), only between games; also
        # checked after every game. 0 = never.
        self.ai_update_every = int(g("BARHOST_AI_UPDATE_EVERY", "900"))
        # Room mode: a friend request to each player who joins the room, once
        # per account (Teiserver c.user.add_friend <account id>).
        self.friend_joiners = g("BARHOST_FRIEND_JOINERS", "0") == "1"
        self.min_humans = int(g("BARHOST_MIN_HUMANS", "1"))
        self.start_delay = int(g("BARHOST_START_DELAY", "20"))
        self.postgame_delay = int(g("BARHOST_POSTGAME_DELAY", "60"))
        self.engine_version = g("BAR_ENGINE_VERSION")
        self.out_dir = Path(g("BARHOST_OUT_DIR", "/bar/out")) / "client"
        self.status_file = Path(g("BARHOST_STATUS_FILE", "/tmp/lobby-client-status.json"))
        if not self.login or not self.password:
            raise SystemExit("BARHOST_LOBBY_LOGIN and BARHOST_LOBBY_PASSWORD must be set")


def _map_key(name: str) -> str:
    return " ".join(name.replace("_", " ").lower().split())


def load_start_boxes(path: str, keys: list) -> dict:
    """{"<map>": {"<variant>": "<base64url(zlib(json))>", ...}, ...} -> {normalised
    map name: value} for the first of `keys` each map has. The value is the
    game's mapmetadata_startbox_override mod option (modoptions.lua;
    startbox_utilities.lua decodes it), as BAR's start position tool copies it:
    "!bSet mapmetadata_startbox_override <value>" (cmd_startpos_tool.lua)."""
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log("start boxes: cannot read %s (%s); none applied" % (path, e))
        return {}
    out = {}
    for name, variants in data.items():
        if not isinstance(variants, dict):
            continue
        for k in keys:
            v = variants.get(k)
            if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]+", v):
                out[_map_key(name)] = v
                break
    log("start boxes: %d maps from %s (%s)" % (len(out), path, ",".join(keys)))
    return out


def start_box_for(boxes: dict, map_name: str):
    """The start boxes for a room's map ("All That Glitters v2.2.3" matches
    "All That Glitters"; the longest match wins), or None."""
    m = _map_key(map_name)
    best = None
    for k in boxes:
        if (m == k or m.startswith(k + " ")) and (best is None or len(k) > len(best)):
            best = k
    return boxes[best] if best else None


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

    def __init__(self, s: Settings, now=time.monotonic, launch=None, perf=None):
        self.s = s
        self.now = now
        self.launch = launch or (lambda b, pw: None)
        # cgroup_perf.PerfRecorder: the container's CPU and memory per game.
        self.perf = perf
        self.outbox = []
        # Room mode: public or private, as last chosen by !public / !private;
        # kept on the data volume so a restart keeps it.
        state = self.load_state()
        self.privacy = state.get("privacy", s.privacy)
        # Room mode: the next map of BARHOST_MAP_ROTATION; also kept.
        self.map_index = state.get("map_index", 0)
        # The bots' AI, as last chosen by !barb / !smrt (or !bots n AI); kept.
        self.bot_ai = state.get("bot_ai", s.bot_ai)
        # Accounts already sent a friend request; kept so each gets one.
        self.friended = set(str(i) for i in state.get("friended", []))
        self.account_ids = {}       # user name -> account id, from ADDUSER
        self.ai_switch_due = False  # a switch asked during a game, done after it
        # New AI releases: checked between games by the I/O layer (install_ai.update).
        self.next_ai_check = self.now() + s.ai_update_every
        self.need_ai_update = False
        self.ai_updating = False
        self.reset()

    def load_state(self) -> dict:
        try:
            st = json.loads(self.s.state_file.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(st, dict):
            return {}
        out = {}
        if st.get("privacy") in ("public", "private"):
            out["privacy"] = st["privacy"]
        if isinstance(st.get("map_index"), int) and st["map_index"] >= 0:
            out["map_index"] = st["map_index"]
        if isinstance(st.get("bot_ai"), str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", st["bot_ai"]):
            out["bot_ai"] = st["bot_ai"]
        if isinstance(st.get("friended"), list):
            out["friended"] = [str(i) for i in st["friended"] if str(i).isdigit()]
        return out

    def save_state(self):
        try:
            self.s.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.s.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"privacy": self.privacy, "map_index": self.map_index,
                                       "bot_ai": self.bot_ai, "friended": sorted(self.friended)}))
            tmp.replace(self.s.state_file)
        except OSError as e:
            log("could not save %s: %s" % (self.s.state_file, e))

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
        # follow mode; survives a reconnect
        self.want_bots = getattr(self, "want_bots", self.s.bot_count)
        self.bot_ai = getattr(self, "bot_ai", self.s.bot_ai)
        self.room_password = getattr(self, "room_password", "")
        self.paused = getattr(self, "paused", False)
        self.told = set()           # (battle id, reason) already explained by PM
        # A room the followed user named with !room: BAR's server does not tell
        # this client who is in other rooms, so it cannot always find them.
        self.target_room = ""
        self.target_owner = ""
        self.joined_at = None
        self.bots_due = None        # follow mode: when to add the bots after joining
        self.private_request = None  # room mode: {"manager", "until", "password"} while one opens
        self.map_wanted = None      # room mode: a !map sent, until the room shows it
        self.start_boxes_map = None  # room mode: the map whose start boxes were last sent
        self.map_deadline = None
        self.map_skips = 0          # maps skipped in a row (not accepted or not downloadable)
        self.ring_hint_after = 0.0  # room mode: no ring hint before this time
        self.stop_hint_after = 0.0  # no !stop request posted before this
        self.resign_kill_at = getattr(self, "resign_kill_at", None)  # !stop: kill the engine if still running then
        self.bonus_hint_after = 0.0  # no bonus hint before this
        self.voted_bonus = None     # the AI bonus this room voted in, if any
        self.vacant = None          # room mode: the joined room was found free
        self.vacancy_check_at = None
        self.room_password_now = ""  # the private room's password, once in it

    def status(self) -> dict:
        b = self.battle
        humans = self.humans()
        return {
            "state": self.state, "mode": self.s.mode, "login": self.s.login, "games": self.games,
            "follow": sorted(self.s.follow), "wantBots": self.want_bots, "botAI": self.bot_ai,
            "paused": self.paused, "privacy": self.privacy if self.s.mode == "room" else None,
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
        if cmd == "s.user.add_friend":
            self.on_friend_reply(line)
            return
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
        if self.game_running():
            # Reconnected while its engine still plays a game: no new room
            # until that game ends (tick() moves on from "ingame" then).
            self.state = "ingame"
            log("my engine is still in a game: no new room until it ends")

    def game_running(self) -> bool:
        """This client's engine is in a game. Never leave a room or take a
        new one then."""
        return self.game_proc is not None and self.game_proc.poll() is None

    def on_DENIED(self, line):
        self.last_error = "login denied: " + line[7:]
        raise ConnectionAbortedError(self.last_error)

    def on_AGREEMENTEND(self, line):
        self.last_error = "the account has not accepted the terms of service: log in once with Chobby"
        raise ConnectionAbortedError(self.last_error)

    def on_QUEUED(self, line):
        log("login queued by the server")

    def on_ADDUSER(self, line):
        # ADDUSER userName country accountID [lobbyID] (Chobby interface.lua _OnAddUser)
        w = line.split(" ")
        if len(w) > 1:
            self.users.setdefault(w[1], 0)
            if len(w) > 3 and w[3].isdigit():
                self.account_ids[w[1]] = w[3]

    def friend(self, user):
        """Room mode: one friend request per account, ever (kept in the state
        file). Teiserver takes it by account id (spring_user_in.ex add_friend)
        and answers s.user.add_friend <id> success / failure <reason>."""
        aid = self.account_ids.get(user)
        if not self.s.friend_joiners or self.s.mode != "room" or not aid or aid in self.friended:
            return
        self.friended.add(aid)
        self.save_state()
        log("friend request to %s (%s)" % (user, aid))
        self.pending.append(("c.user.add_friend", aid))

    def on_friend_reply(self, line):
        w = line.replace("	", " ").split(" ", 2)
        if len(w) >= 3 and not w[2].startswith("success"):
            log("friend request %s: %s" % (w[1], w[2]))

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
            if len(p) == 6 and p[5] != b.map:
                b.map = p[5]
                if b is self.battle:
                    self.map_changed(b)

    def map_changed(self, b: Battle):
        """The room's map changed: unsynced until it is downloaded, so no game
        starts (and the engine never launches) without it."""
        log("the room's map is now %s" % b.map)
        if self.map_wanted and self.map_wanted.lower() in b.map.lower():
            self.map_wanted = self.map_deadline = None
            self.map_skips = 0
        self.synced = False
        if self.my_status is not None:
            st = unpack_battle_status(self.my_status)
            self.my_status = battle_status(ready=0, team=st["team"], ally=st["ally"], mode=st["mode"], sync=2)
            self.pending.append(("MYBATTLESTATUS", self.my_status, 0))
        self.need_fetch = b
        self.apply_start_boxes()

    def apply_start_boxes(self):
        """Room mode, as boss: set the start boxes for the room's map, once per
        map, after any map change (the rotation's or a boss's). A map with no
        entry gets "0", Chobby's Default Boxes (gui_battle_room_window.lua), so
        the previous map's boxes never carry over."""
        b = self.battle
        if (self.s.mode != "room" or not self.s.start_boxes or not b or not self.boss
                or self.game_running() or self.start_boxes_map == b.map):
            return
        self.start_boxes_map = b.map
        value = start_box_for(self.s.start_boxes, b.map)
        log("start boxes for %s: %s" % (b.map, "set" if value else "none known, map defaults"))
        self.say("!bSet mapmetadata_startbox_override " + (value or "0"))

    def on_JOINEDBATTLE(self, line):
        p = line.split(" ")
        b = self.battles.get(p[1])
        if b:
            b.users.add(p[2])
            if p[2] == self.s.login and len(p) > 3:
                self.script_password = p[3]
            if b is self.battle:
                self.boss_authorised([p[2]])
                # Seen only by the joiner, in the room's chat; any member may
                # send it (Teiserver SAYBATTLEPRIVATEEX, lobby.ex allow?).
                if self.s.mode == "room" and self.s.welcome and p[2] not in (b.founder, self.s.login):
                    self.pending.append(("SAYBATTLEPRIVATEEX", p[2], self.s.welcome))
                if p[2] not in (b.founder, self.s.login):
                    self.friend(p[2])

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
        if self.target_room:        # a named room: say why, and do not retry it
            self.tell(self.target_owner, "I could not join room %s: %s" % (self.target_room, line[17:]))
            self.target_room = ""
        self.next_join = self.now() + 5

    def on_JOINBATTLE(self, line):
        bid = line.split(" ")[1]
        self.battle = self.battles.get(bid)
        if not self.battle:
            return
        self.state = "setup"
        self.boss = False
        self.synced = False
        self.joined_at = self.now()
        b = self.battle
        log("joined room %s (%s) on %s, %s, engine %s" % (b.id, b.title, b.founder, b.map, b.engine_version))
        # The game and map download in the background; unsynced until then.
        self.need_fetch = b
        if self.s.mode == "follow":
            # A guest: spectate, bring the bots, leave hosting to the person.
            # The bots wait until the members' statuses (their teams) are in.
            self.my_status = battle_status(ready=0, mode=0, sync=2)
            self.pending.append(("MYBATTLESTATUS", self.my_status, 0))
            self.state = "waiting"
            self.bots_due = self.now() + BOTS_DELAY
            return
        # Join as a player first: SPADS grants boss to players (Chobby does the same).
        self.my_status = battle_status(ready=0, team=0, ally=0, mode=1, sync=2)
        self.pending.append(("MYBATTLESTATUS", self.my_status, 0))
        # BAR's server does not say who is in other rooms (NOT_SEEN), so a room
        # can only be known to be free from inside it: its members arrive as
        # CLIENTBATTLESTATUS just after JOINBATTLE. Check before claiming it.
        self.vacant = None
        self.vacancy_check_at = self.now() + VACANCY_WAIT

    def fetched(self, b: Battle, ok: bool):
        if b is not self.battle:
            return
        if not ok:
            self.last_error = "could not download %s / %s" % (b.game, b.map)
            if self.s.mode == "room" and self.boss and self.s.map_rotation and self.state == "waiting":
                log(self.last_error + "; skipping that map")     # our own map choice: take the next
                self.skip_map()
                return
            self.send("LEAVEBATTLE")
            self.lost_battle(self.last_error)
            return
        log("content ready: %s, %s" % (b.game, b.map))
        self.synced = True
        st = unpack_battle_status(self.my_status) if self.my_status is not None \
            else {"mode": 1, "team": 0, "ally": 0}
        self.my_status = battle_status(team=st["team"], ally=st["ally"], mode=st["mode"], sync=1)
        self.pending.append(("MYBATTLESTATUS", self.my_status, 0))

    # -- map rotation (room mode) ------------------------------------------

    def rotate_map(self):
        """Ask the autohost for the rotation's current map."""
        if not self.battle:
            return
        if not self.s.map_rotation:
            self.apply_start_boxes()        # for the room's own map
            return
        name = self.s.map_rotation[self.map_index % len(self.s.map_rotation)]
        if name.lower() in self.battle.map.lower():
            self.map_wanted = None
            self.apply_start_boxes()
            return
        # The wait counts from when the command goes out, after the queue.
        self.map_wanted = name
        self.map_deadline = self.now() + MAP_WAIT + COMMAND_SPACING * (len(self.pending) + 1)
        log("next map: %s" % name)
        self.say("!map " + name)

    def advance_map(self):
        self.map_index = (self.map_index + 1) % len(self.s.map_rotation)
        self.save_state()

    def skip_map(self):
        """The current rotation map cannot be used: try the next, at most one
        full round in a row."""
        self.map_wanted = self.map_deadline = None
        self.map_skips += 1
        if self.map_skips >= len(self.s.map_rotation):
            self.last_error = "no map of BARHOST_MAP_ROTATION could be used; keeping %s" % self.battle.map
            log(self.last_error)
            return
        self.advance_map()
        self.rotate_map()

    def on_REQUESTBATTLESTATUS(self, line):
        if self.my_status is None:
            self.my_status = battle_status(mode=1, sync=1)
        self.send("MYBATTLESTATUS", self.my_status, 0)

    def on_CLIENTBATTLESTATUS(self, line):
        p = line.split(" ")
        if self.battle:
            self.statuses[p[1]] = unpack_battle_status(p[2])
            # On joining, Teiserver lists who is already in the room only as
            # CLIENTBATTLESTATUS, not JOINEDBATTLE (spring_out.ex do_join_battle).
            self.battle.users.add(p[1])

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
                # Room mode sets up only a room it has found free.
                if self.s.mode != "room" or self.vacant:
                    self.configure_room()
            return
        vote_cmd = VOTE_RE.search(msg) if user == self.battle.founder else None
        starting = (user not in (self.battle.founder, self.s.login) and START_RE.match(msg.strip())) or \
            (vote_cmd and re.match(r"(?:force)?start\b", vote_cmd.group(1), re.IGNORECASE))
        if starting and self.s.mode == "room":
            if self.playing():
                self.set_seat(False, "a game start was called", first=True)
        passed = BONUS_PASSED_RE.search(msg) if user == self.battle.founder else None
        if passed and self.s.mode == "room" and self.boss and 0 <= int(passed.group(1)) <= 100:
            self.voted_bonus = int(passed.group(1))
            log("a bonus vote passed: every AI to %d" % self.voted_bonus)
            self.say("!setallaibonus %d" % self.voted_bonus)
            return
        if self.s.mode == "room" and user not in (self.battle.founder, self.s.login) \
                and BONUS_RE.search(msg) and self.now() >= self.bonus_hint_after:
            self.bonus_hint_after = self.now() + BONUS_HINT_EVERY
            self.say(BONUS_HINT)
        ring = RING_RE.match(msg.strip())
        if ring and self.s.mode == "room" and user not in (self.battle.founder, self.s.login) \
                and self.now() >= self.ring_hint_after:
            self.ring_hint_after = self.now() + RING_HINT_EVERY
            name = ring.group(1) or "a player"
            self.say(RING_HINT % (name, ring.group(1) or "<name>"))
        # The autohost announces a vote as `* <user> called a vote for command
        # "<command>" [!vote y, ...]` (BYAR-Chobby gui_battle_room_window.lua,
        # ParseForVotingSaidBattle). End one that would unboss this client or
        # an authorised user at once.
        m = VOTE_RE.search(msg)
        if user == self.battle.founder and self.boss and m and self.protected_from(m.group(1)):
            log("ending the vote \"%s\"" % m.group(1))
            self.pending.insert(0, "!ev")       # ahead of anything queued

    def protected_from(self, command: str) -> bool:
        """`unboss` alone (everyone), or of this client or an authorised user."""
        w = command.split()
        if not w or w[0].lower() != "unboss":
            return False
        guarded = {self.s.login.lower()} | {u.lower() for u in self.s.follow}
        return len(w) == 1 or w[1].lower() in guarded

    def boss_authorised(self, names):
        """Room mode, once boss: give boss to the authorised users in `names`."""
        if self.s.mode != "room" or not self.boss:
            return
        for n in sorted(set(names) & self.s.follow):
            log("bossing %s" % n)
            self.say("!boss " + n)

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
        if self.game_running():
            return
        if self.privacy == "private":
            self.try_private()
            return
        rooms = [b for b in self.battles.values() if self.suitable(b)]
        if not rooms:
            return
        b = random.choice(rooms)
        self.join_attempted.add(b.id)
        self.script_password = str(random.randint(10 ** 8, 10 ** 9 - 1))
        self.state = "joining"
        self.send("JOINBATTLE", b.id, "empty", self.script_password)

    def try_private(self):
        """As Chobby's "passworded private battle": PM a region's manager
        `!privatehost`; it answers with the password and opens a room titled
        after this account; join that as a player."""
        now = self.now()
        req = self.private_request
        if req is None:
            managers = sorted(u for u in self.users
                              if MANAGER_RE.match(u) and self.s.cluster.search(MANAGER_RE.match(u).group(1)))
            if not managers:
                self.last_error = "no autohost manager online to open a private room"
                self.next_join = now + 30
                return
            mgr = random.choice(managers)
            self.private_request = {"manager": mgr, "until": now + PRIVATE_WAIT, "password": None}
            log("asking %s for a private room" % mgr)
            self.tell(mgr, "!privatehost")
            return
        if now >= req["until"]:
            self.last_error = "%s opened no private room" % req["manager"]
            log(self.last_error)
            self.private_request = None
            self.next_join = now + 60
            return
        if not req["password"]:
            return
        for b in self.battles.values():
            if b.title.startswith(self.s.login) and b.founder.startswith(req["manager"] + "[") \
                    and (not self.s.engine_version or self.s.engine_version in b.engine_version):
                self.room_password_now = req["password"]
                self.private_request = None
                self.script_password = str(random.randint(10 ** 8, 10 ** 9 - 1))
                self.state = "joining"
                self.send("JOINBATTLE", b.id, self.room_password_now, self.script_password)
                return

    def configure_room(self):
        b = self.battle
        self.boss_deadline = None
        log("boss in room %s; setting it up" % b.id)
        # A headless host never plays: it took a player seat only to be made
        # boss (as Chobby, which forces a player seat "so he can boss self",
        # battle_list_window.lua HostBattle). Settings and maps are a boss's,
        # not a player's; only adding bots needs the seat, briefly (sync_bots).
        self.set_seat(False, "boss; a headless host spectates")
        if self.s.room_name:
            self.say("!rename " + self.s.room_name)
        for c in self.s.setup:
            self.say(c)
        self.boss_authorised(b.users)
        self.sync_bots()
        if b.passworded:
            for u in sorted(self.s.follow):
                self.tell(u, "Private room %s is up on %s; password %s." % (b.id, b.founder, self.room_password_now))
        self.state = "waiting"
        self.rotate_map()

    def switch_bots(self):
        """Replace this client's bots with self.bot_ai. sync_bots takes a
        player seat for the adds only (only players may add bots) and returns
        to spectating."""
        self.ai_switch_due = False
        b = self.battle
        log("switching the bots to %s" % self.bot_ai)
        for name in self.my_bots():
            self.pending.append(("REMOVEBOT", name))
            b.bots.pop(name, None)
        if not self.playing():
            self.sync_bots()
            return
        self.set_seat(False, "a headless host spectates")
        self.sync_bots()

    def playing(self) -> bool:
        return self.my_status is not None and unpack_battle_status(self.my_status)["mode"] == 1

    def set_seat(self, play: bool, why: str, first: bool = False):
        """Player (on a free team, the humans' ally team) or spectator; `first`
        sends it ahead of anything queued."""
        sync = unpack_battle_status(self.my_status)["sync"] if self.my_status is not None else 2
        team = 0
        if play:
            used = {st["team"] for n, st in self.statuses.items() if st.get("mode") == 1 and n != self.s.login}
            used |= {x["status"]["team"] for x in self.battle.bots.values()} if self.battle else set()
            while team in used:
                team += 1
        self.my_status = battle_status(ready=0, team=team, ally=0, mode=1 if play else 0, sync=sync)
        self.pending.insert(0, ("MYBATTLESTATUS", self.my_status, 0)) if first \
            else self.pending.append(("MYBATTLESTATUS", self.my_status, 0))
        log("%s: %s" % ("playing" if play else "spectating", why))

    def bot_name(self, n: int) -> str:
        """Chobby's real AI name (simple names off): BARbstable(1)."""
        return ("%s %s (%d)" % (self.bot_ai, self.s.bot_ai_version, n)).replace(" ", "")

    def my_bots(self) -> "list[str]":
        b = self.battle
        return sorted((n for n, x in b.bots.items() if x["owner"] == self.s.login),
                      key=lambda n: (len(n), n)) if b else []

    def sync_bots(self):
        """Bring the bots this client owns in the room to self.want_bots: add
        on free teams (after the humans' and other bots'), or remove the last."""
        b = self.battle
        if not b:
            return
        mine = self.my_bots()
        for name in mine[self.want_bots:][::-1]:
            self.pending.append(("REMOVEBOT", name))
        if len(mine) >= self.want_bots:
            return
        used = {st["team"] for n, st in self.statuses.items() if st.get("mode") == 1 and n != self.s.login}
        used |= {x["status"]["team"] for x in b.bots.values()}
        names = set(b.bots)
        team, i = 1, 0      # team 0 stays free for the first human
        adds = []
        for k in range(len(mine), self.want_bots):
            while team in used:
                team += 1
            used.add(team)
            i += 1
            while self.bot_name(i) in names:
                i += 1
            name = self.bot_name(i)
            names.add(name)
            side = self.s.bot_sides[k % len(self.s.bot_sides)]
            st = battle_status(ready=1, team=team, ally=self.s.bot_ally, mode=1, sync=1, side=side)
            adds.append(("ADDBOT", name, st, 0, self.bot_ai))
        # Teiserver lets only players add bots (lobby.ex allow?, player_command
        # :add_bot); a bot stays when its owner then spectates, and goes only
        # when the owner leaves the room. So a spectator turns player for the
        # adds and back.
        spectator = self.my_status is not None and unpack_battle_status(self.my_status)["mode"] == 0
        if spectator:
            sync = unpack_battle_status(self.my_status)["sync"]
            while team in used:
                team += 1
            self.pending.append(("MYBATTLESTATUS", battle_status(ready=0, team=team, ally=self.s.bot_ally,
                                                                 mode=1, sync=sync), 0))
        self.pending += adds
        if spectator:
            self.pending.append(("MYBATTLESTATUS", self.my_status, 0))
        if self.s.bot_profile:
            for a in adds:
                self.say("!aiProfile %s %s" % (a[1], self.s.bot_profile))
        if adds:
            # e.g. !setallaibonus 50: it applies to the AIs in the room, so it
            # follows every add (setup, an AI switch, more bots). A bonus the
            # room voted in replaces the configured one in this room.
            for c in self.s.bot_setup:
                if self.voted_bonus is not None and SETALL_RE.match(c):
                    c = "!setallaibonus %d" % self.voted_bonus
                self.say(c)

    def leave_battle(self, why):
        for name in self.my_bots():
            self.pending.append(("REMOVEBOT", name))
        self.pending.append(("LEAVEBATTLE",))
        b = self.battle
        log("leaving room %s: %s" % (b.id, why))
        # Keep the queue (bot removal, LEAVEBATTLE) but forget the room.
        pending = self.pending
        self.lost_battle(why)
        self.pending = pending

    # -- follow mode ---------------------------------------------------------

    def tell(self, user, text):
        self.pending.append(("SAYPRIVATE", user, text))

    def on_SAIDPRIVATE(self, line):
        p = line.split(" ", 2)
        if len(p) < 3:
            return
        user, msg = p[1], p[2].strip()
        req = self.private_request
        if req and user == req["manager"] and "Starting a new private instance in" in msg and "password=" in msg:
            # Chobby reads the 4 characters after "password=".
            i = msg.index("password=") + 9
            req["password"] = msg[i:i + 4]
            req["until"] = self.now() + PRIVATE_WAIT
            log("%s is opening a private room" % user)
            return
        if user not in self.s.follow:
            return
        w = msg.split()
        cmd = w[0].lower() if w else ""
        if cmd == "!bots" and len(w) >= 2 and w[1].isdigit() and 0 <= int(w[1]) <= 16:
            self.want_bots = int(w[1])
            live = self.battle is not None and self.state != "ingame"
            if len(w) >= 3 and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", w[2]) and w[2] != self.bot_ai:
                self.bot_ai = w[2]
                self.save_state()
                if live:            # replace the bots with the new AI
                    for name in self.my_bots():
                        self.pending.append(("REMOVEBOT", name))
                        self.battle.bots.pop(name, None)
            if live:
                self.sync_bots()
            when = "" if live else (" after this game" if self.battle else " when I join your room")
            self.tell(user, "OK: %d %s%s." % (self.want_bots, self.bot_ai, when))
        elif cmd == "!perf":
            last = self.perf.last_summary if self.perf is not None else None
            self.tell(user, ("Last " + cgroup_perf.describe(last)) if last else "No game measured yet.")
        elif cmd == "!stop":
            b = self.battle
            if not b or not (self.state == "ingame" or self.users.get(b.founder, 0) & 1):
                self.tell(user, "No game is running in my room.")
            elif self.game_proc is not None and self.game_proc.poll() is None and hasattr(self.game_proc, "terminate"):
                self.resign(user)
            elif self.now() < self.stop_hint_after:
                self.tell(user, "I asked the room less than a minute ago.")
            else:
                self.stop_hint_after = self.now() + STOP_HINT_EVERY
                self.say(STOP_HINT % user)
                self.tell(user, "Asked the players to vote !cv stop.")
        elif cmd in AI_COMMANDS:
            ai = AI_COMMANDS[cmd]
            if ai == self.bot_ai and not self.ai_switch_due:
                self.tell(user, "The bots already run %s." % ai)
                return
            self.bot_ai = ai
            self.save_state()
            if self.battle and self.state == "waiting" and not self.game_running():
                self.switch_bots()
                self.tell(user, "OK: switching the bots to %s." % ai)
            else:
                self.ai_switch_due = bool(self.battle)
                self.tell(user, "OK: the bots run %s %s." % (
                    ai, "after this game" if self.battle else "from the next room"))
        elif cmd in ("!public", "!private") and self.s.mode == "room":
            self.privacy = cmd[1:]
            self.save_state()
            b = self.battle
            if b is None or b.passworded == (self.privacy == "private"):
                self.tell(user, "OK: I host a %s room%s." % (self.privacy, " (as now)" if b else ""))
            elif self.state in ("ingame", "countdown") or self.game_running():
                self.tell(user, "OK: a %s room after this game." % self.privacy)
            else:
                self.tell(user, "OK: moving to a %s room." % self.privacy)
            # tick() leaves a room of the other kind once no game is running.
        elif cmd == "!join":
            self.room_password = w[1] if len(w) >= 2 else ""
            self.paused = False
            self.told = set()
            self.next_join = self.now()
            if self.battle:
                self.tell(user, "I am already in a room.")
            elif self.followed_battle() is None:
                self.tell(user, NOT_SEEN)
            else:
                self.tell(user, "Joining your room.")
        elif cmd == "!room":
            # !room <room> [password]: <room> may have spaces (a title), so try
            # all of it first, then all but the last word with that as password.
            args = msg[len(w[0]):].strip()
            b, why, password = self.find_room(args), "", ""
            if isinstance(b, str):
                b, why = None, b
            if b is None and " " in args:
                ref, password = args.rsplit(" ", 1)
                b = self.find_room(ref)
                if isinstance(b, str):
                    b, why = None, b
            if b is None:
                self.tell(user, (why or "I see no such room among %d." % len(self.battles))
                          + " PM me !room <room number, host, player or title> [password].")
                return
            if self.battle is b:
                self.tell(user, "I am already in room %s." % b.id)
                return
            if self.state == "ingame":
                self.tell(user, "I am in a game; PM me again when it ends.")
                return
            if self.battle:
                self.leave_battle("moving to room %s for %s" % (b.id, user))
            self.target_room, self.target_owner = b.id, user
            self.room_password = password
            self.paused = False
            self.told = set()
            self.next_join = self.now()
            self.tell(user, "Joining room %s (%s on %s)." % (b.id, b.title, b.founder))
        elif cmd == "!leave":
            self.paused = True
            if self.battle and self.state != "ingame":
                self.leave_battle("asked to leave by %s" % user)
            self.tell(user, "Left. PM me !join [password] or !room <room> to come back.")
        elif cmd == "!status":
            b = self.battle
            if self.s.mode == "room":
                where = "%s room%s" % (self.privacy, (" %s (%s)%s, %s" % (
                    b.id, b.title, (", password " + self.room_password_now) if b.passworded else "",
                    "playing" if self.playing() else "spectating")) if b else ", none yet")
            else:
                where = ("room %s (%s)" % (b.id, b.title)) if b else \
                    "no room (%d rooms seen; I cannot see you in one: PM me !room <room>)" % len(self.battles)
            self.tell(user, "%s; %s; bots %d/%d %s; games %d%s" % (
                self.state, where, len(self.my_bots()), self.want_bots, self.bot_ai, self.games,
                ("; " + self.last_error) if self.last_error else ""))
        elif self.s.mode == "room":
            self.tell(user, "Commands: !public, !private, !barb, !smrt, !bots <0-16> [AI], !stop, !perf, !status")
        else:
            self.tell(user, "Commands: !bots <0-16> [AI], !barb, !smrt, !room <room number, host, player or "
                            "title> [password], !join [password], !leave, !status")

    def find_room(self, ref):
        """The room `ref` names, in this order: its number; its host (the
        autohost, or a player hosting it); a player seen in it (BAR's server
        shows this client only hosts, see NOT_SEEN); its title, whole or a
        unique part. Any case. A Battle, None, or a string saying why not."""
        if not ref:
            return None
        if ref in self.battles:
            return self.battles[ref]
        r = ref.lower()
        rooms = list(self.battles.values())
        for b in rooms:
            if b.founder.lower() == r:
                return b
        for b in rooms:
            if r in {u.lower() for u in b.users}:
                return b
        for b in rooms:
            if b.title.lower() == r:
                return b
        part = [b for b in rooms if len(r) >= 3 and r in b.title.lower()]
        if len(part) == 1:
            return part[0]
        if len(part) > 1:
            return "%d room titles contain \"%s\"." % (len(part), ref)
        if any(u.lower() == r for u in self.users):
            return "%s is online, but BAR's server does not tell me which room they are in." % ref
        return None

    def followed_battle(self):
        for b in self.battles.values():
            if self.s.follow & b.users and not (self.users.get(b.founder, 0) & 1):
                return b
        b = self.battles.get(self.target_room)
        if b is not None and not (self.users.get(b.founder, 0) & 1):
            return b
        return None

    def try_follow(self):
        b = self.followed_battle()
        if b is None or self.paused or self.game_running():
            return
        owner = (sorted(self.s.follow & b.users) or [self.target_owner or sorted(self.s.follow)[0]])[0]

        def explain(reason, text):
            if (b.id, reason) not in self.told:
                self.told.add((b.id, reason))
                self.tell(owner, text)

        if self.s.engine_version and self.s.engine_version not in b.engine_version:
            explain("engine", "I cannot join: your room runs engine %s and I have %s."
                    % (b.engine_version, self.s.engine_version))
            return
        if b.passworded and not self.room_password:
            explain("password", "Your room has a password: PM me !join <password>.")
            return
        self.script_password = str(random.randint(10 ** 8, 10 ** 9 - 1))
        self.state = "joining"
        self.send("JOINBATTLE", b.id, self.room_password or "empty", self.script_password)

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
        self.target_room = ""       # !room names one room once; it is not rejoined
        self.start_boxes_map = None  # boxes are sent again in the next room
        self.voted_bonus = None     # a voted bonus belongs to that room
        self.bots_due = None
        if self.state != "ingame":
            self.state = "lobby"
        self.next_join = self.now() + 10

    def ai_updated(self, changes, errors):
        """The I/O layer's result of install_ai.update()."""
        self.ai_updating = False
        for c in changes:
            log("AI release updated: " + c)
            for u in sorted(self.s.follow):
                self.tell(u, "AI release updated (%s); the next game uses it." % c.rsplit(": ", 1)[-1])
        for e in errors:
            log("AI update check failed: " + e)

    def ready_to_start(self) -> bool:
        if not self.synced or self.map_wanted or self.ai_updating:   # no start mid change
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
        self.game_launched_at = self.now()
        self.engine_exit_logged = False
        self.resign_kill_at = None
        if self.perf is not None:
            self.perf.start("game %d %s" % (self.games, b.map), getattr(self.game_proc, "write_dir", None),
                            now=self.now())

    def resign(self, user):
        """Resign the bots: this engine hosts them, so it leaves the game. BAR
        then counts the bots' teams as uncontrolled and wipes out their ally
        team (game_end.lua UpdateAllyTeamIsDead: playerQuitIsDead and not
        isControlled; CheckPlayer marks an AI host's AIs uncontrolled when it is
        inactive), so the game ends; the client never rejoins mid-game."""
        if self.resign_kill_at is not None:
            self.tell(user, "Already resigning the bots.")
            return
        log("resigning the bots at %s's request: leaving the game" % user)
        self.game_proc.terminate()
        self.resign_kill_at = self.now() + RESIGN_KILL_AFTER
        self.say(RESIGN_SAY % user)
        self.tell(user, "Resigning the bots: I am leaving the game, so BAR ends their team.")

    def log_engine_exit(self):
        """Once per game: the engine's exit, in the pod log (its console is
        only on the volume). An odd exit (non-zero, or while the game goes
        on) brings the console's last lines along."""
        if self.game_proc is None or getattr(self, "engine_exit_logged", True):
            return
        self.engine_exit_logged = True
        code = self.game_proc.poll()
        ran = self.now() - getattr(self, "game_launched_at", self.now())
        still = bool(self.battle and self.users.get(self.battle.founder, 0) & 1)
        log("engine exited with code %s after %d s%s" % (code, ran, "; the game goes on" if still else ""))
        if self.perf is not None:
            summary = self.perf.stop()
            if summary:
                log("perf " + cgroup_perf.describe(summary))
        if code == 0 and not still:
            return
        console = getattr(self.game_proc, "write_dir", None)
        try:
            lines = (console / "engine-console.log").read_text(errors="replace").splitlines()[-15:]
        except (OSError, TypeError):
            return
        for line in lines:
            log("  engine: " + line)

    # -- time --------------------------------------------------------------

    def tick(self):
        now = self.now()
        if self.state == "ingame":
            # Replies to private messages still go out during a game.
            if self.game_proc is not None and self.game_proc.poll() is None:
                if self.resign_kill_at is not None and now >= self.resign_kill_at and hasattr(self.game_proc, "kill"):
                    log("the engine did not quit; killing it")
                    self.game_proc.kill()
                    self.resign_kill_at = now + RESIGN_KILL_AFTER
                self.flush_pending()
                return
            self.log_engine_exit()
            if self.battle and self.users.get(self.battle.founder, 0) & 1:
                self.flush_pending()
                return     # our engine ended but the game goes on: wait it out
            if self.game_proc is not None and hasattr(self.game_proc, "close"):
                self.game_proc.close()
            self.game_proc = None
            self.state = "waiting" if self.battle else "lobby"
            self.quiet_until = now + self.s.postgame_delay
            if self.s.mode == "follow" and self.battle:
                self.sync_bots()        # a !bots sent during the game
            self.next_ai_check = now    # look for a new AI release before the next game
            if self.s.mode == "room" and self.battle and self.boss and self.s.map_rotation:
                self.map_skips = 0      # one map per game
                self.advance_map()
                self.rotate_map()
        if self.map_wanted and self.map_deadline is not None and now >= self.map_deadline \
                and self.battle and self.state in ("waiting", "countdown"):
            log("the autohost did not switch to %s; skipping it" % self.map_wanted)
            self.skip_map()
        if self.s.mode == "room" and self.state == "setup" and self.battle \
                and self.vacancy_check_at is not None and now >= self.vacancy_check_at:
            self.vacancy_check_at = None
            b = self.battle
            others = sorted(n for n in b.users | set(self.statuses) if n not in (b.founder, self.s.login))
            bots = sorted(n for n, x in b.bots.items() if x["owner"] != self.s.login)
            if others or bots:
                self.leave_battle("in use (%s)" % ", ".join(others + bots))
            else:
                self.vacant = True
                self.say("!boss " + self.s.login)
                self.boss_deadline = now + 20
                if self.boss:               # granted before the check
                    self.configure_room()
        if self.state == "setup" and self.boss_deadline is not None and now >= self.boss_deadline \
                and not self.pending and not self.boss:
            # Never set up a room this client was not made boss of.
            self.leave_battle("the autohost did not make me boss")
        if self.state == "lobby" and now >= self.next_join:
            self.next_join = now + 5
            if self.s.mode == "follow":
                self.try_follow()
            else:
                self.try_join()
        if self.s.mode == "follow" and self.state == "waiting" and self.battle \
                and self.bots_due is not None and now >= self.bots_due:
            self.bots_due = None
            self.sync_bots()
            owner = ", ".join(sorted(self.s.follow & self.battle.users)) or "the owner"
            self.say("%s here: running %d %s for %s on a server. PM me !bots <n> to change that."
                     % (self.s.login, self.want_bots, self.bot_ai, owner))
        # The room's member list arrives just after JOINBATTLE: give it time.
        if self.s.mode == "follow" and self.state == "waiting" and self.battle \
                and not (self.s.follow & self.battle.users) \
                and now - (self.joined_at or 0) >= JOIN_SETTLE:
            self.leave_battle("everyone I follow left")
        # A new AI release is only ever installed between games.
        if self.s.ai_update_every > 0 and not self.ai_updating and not self.game_running() \
                and self.state in ("lobby", "waiting") and now >= self.next_ai_check:
            self.next_ai_check = now + self.s.ai_update_every
            self.ai_updating = self.need_ai_update = True
        if self.ai_switch_due and self.battle and self.state == "waiting" and not self.game_running() \
                and (self.boss or self.s.mode == "follow"):
            self.switch_bots()
        # Room mode: the room kind follows !public / !private; rehosting is
        # only ever done from a room with no game in progress.
        if self.s.mode == "room" and self.battle and self.state == "waiting" and self.boss \
                and not self.game_running() and not (self.users.get(self.battle.founder, 0) & 1):
            if self.battle.passworded != (self.privacy == "private"):
                self.leave_battle("switching to a %s room" % self.privacy)
            elif self.playing() and not self.pending:
                # Never left in a player seat: it is only for adding bots.
                self.set_seat(False, "a headless host spectates")
        if self.state in ("waiting", "countdown") and self.boss and not self.pending \
                and now >= self.quiet_until:
            if self.ready_to_start():
                if self.countdown_end is None:
                    self.countdown_end = now + self.s.start_delay
                    self.state = "countdown"
                    if self.playing():      # no idle commander in the game
                        self.set_seat(False, "the game is about to start")
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

async def update_ais(c: LobbyClient):
    """Install new releases of moving-tag AI sources, off the lobby loop."""
    try:
        import install_ai
        changes, errors = await asyncio.get_running_loop().run_in_executor(None, install_ai.update)
    except Exception as e:  # a failed check keeps what is installed
        changes, errors = [], [str(e)]
    c.ai_updated(changes, errors)


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
    last_ping = last_perf = time.monotonic()
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
            if c.need_ai_update:
                c.need_ai_update = False
                asyncio.ensure_future(update_ais(c))
            if c.perf is not None and time.monotonic() - last_perf >= 1:
                last_perf = time.monotonic()
                c.perf.sample(last_perf)
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
    perf = cgroup_perf.PerfRecorder(summary_csv=s.out_dir / "perf-summary.csv")
    if perf.cg.read() is None:
        log("perf: no cgroup counters readable under %s; games are not measured" % cgroup_perf.ROOT)
    else:
        log("perf: cgroup %s, CPU limit %s cores; each game is summarised here and in %s" % (
            "v2" if perf.cg.v2 else "v1", perf.cg.limit_cores() or "no", perf.summary_csv))
    c = LobbyClient(s, launch=launch_engine(s), perf=perf)
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
