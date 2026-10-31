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
  3. Once boss, spectate: a headless host never plays a game. Settings and
     maps are a boss's; adding bots (BAR's server takes ADDBOT only from
     players) takes a player seat, for the adds alone. With
     BARHOST_BOOST_BELOW set, it also sits as a player while few humans
     play, so the battle list shows the room busier, and spectates again
     from BARHOST_BOOST_UNTIL humans or a full room. Everyone ready, or a
     start called (`!start`, `!cv start`, `!forcestart`, a vote for one),
     makes it spectate at once, so its engine never joins as a player.
  4. While boss, give boss to BARHOST_FOLLOW users as they join (`!boss`),
     and end at once (`!ev`) a vote that changes who is boss: any `boss` or
     `unboss` vote by an untrusted player (a boss won by vote could unboss
     the others and take the room), and from anyone a vote to unboss this
     client, everyone or a trusted user ("Shame on you, <caller>!" when the
     vote was to unboss this client or everyone). Trusted users are the
     BARHOST_FOLLOW and BARHOST_AUTO_BOSS users. An untrusted player's vote
     to change the start boxes it set (box commands, presets, start
     settings: is_start_box_vote) is ended too. A boss's direct command
     (`!boss`, `!addbox`) is not a vote and is left alone. BARHOST_AUTO_BOSS users are bossed as
     they join too, but get no commands. A BARHOST_FOLLOW
     user picks the bots' AI with `!ai <channel>` (BARHOST_AI_CHANNELS:
     the game's own BARb, or a release such as SMRTBARb test or prod, kept
     on the data volume and updated between games). The choice is kept
     across restarts and applied between games: the engine is pointed at
     the release, and the bots are replaced (as a player) only if the AI's
     name changes. `!stop` resigns the bots during a game (its
     engine leaves the game). `!say <text>` says it in the room (a
     `!command` is then the boss's). `!profiler` and `!widgets` switch the
     engine profiler and BAR's own widgets for the next games. Each joiner gets BARHOST_WELCOME privately.
     A player's "bonus" gets one line on voting the AIs' bonus (all, or one),
     once a minute; a passed bonus vote is applied to every AI
     (`!setallaibonus`) and kept for the room's later bot adds.
     When a player rings another (`!ring <name>`), say
     how to vote an away player to spectator, at most once a minute; the room
     decides.
  5. When enough humans are in, ready and synced (with BARHOST_MATCHED_TEAMS,
     at least as many as the bots), say how to vote the start (`!cv start`).
     It never starts a game itself: the players vote it. With
     BARHOST_RING_EVERY, `!ring` the unready once the teams are matched.
     With BARHOST_PROMOTE_EVERY set, while a public room has fewer humans
     than bots (no bots: a free seat) and no start is near, PM the autohost
     `!promote` (it advertises the room in #main): once the room is set up,
     as soon as a game is over, and each time its cooldown allows.
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
  BARHOST_AUTO_BOSS       room mode: ','-separated users made boss when they
                          join and guarded from unboss votes, as
                          BARHOST_FOLLOW users are; they get no commands
                          (optional)
  BARHOST_LOBBY_LOGIN, BARHOST_LOBBY_PASSWORD   the account (password falls
        back to SPADS_LOBBY_PASSWORD, login to SPADS_LOBBY_LOGIN)
  BARHOST_LOBBY_HOST, BARHOST_LOBBY_PORT        server4.beyondallreason.info:8200
  BARHOST_LOBBY_CLUSTER   regex on the autohost manager region, e.g. "EU" (any)
  BARHOST_ROOM_PRIVACY    room mode: public (default) or private, first start only
  BARHOST_STATE_FILE      where !public / !private is kept
                          (/bar/data/lobby-client-state.json)
  BARHOST_MAP_ROTATION    room mode: ';'-separated maps, one per game, in order
                          (its place is kept in BARHOST_STATE_FILE)
  BARHOST_ROOM_NAME       room title, set with !rename (optional); {bonus} in it
                          is the AIs' bonus (e.g. "30", "30-50" if they differ),
                          renamed whenever it changes
  BARHOST_START_BOXES_FILE  room mode: JSON of start boxes per map, applied
                          after every map change (optional; see start_box_for)
  BARHOST_START_BOX_KEYS  ','-separated variants to use, first found wins
                          (team2_ai,override)
  BARHOST_WELCOME         room mode: said privately in the room to each joiner
  BARHOST_AI_CHANNELS     the AIs a BARHOST_FOLLOW user picks with `!ai <name>`,
                          ';'-separated `name=AI` (the engine's own AI) or
                          `name=AI:github:OWNER/REPO@TAG[#GLOB]` (a release,
                          kept on the data volume; see install_ai.py), e.g.
                          "barb=BARb;smrt-prod=SMRTBARb:github:o/r@latest"
                          (optional; without it, BARHOST_BOT_AI)
  BARHOST_AI_CHANNEL      the channel until one is picked (the first)
  BARHOST_AI_RELEASES_DIR where channel releases are kept
                          ($BARHOST_DATA_DIR/ai-releases)
  BARHOST_AI_UPDATE_EVERY seconds between checks for a new release of a
                          channel, or of a BARHOST_AI_ARCHIVES moving tag,
                          only between games, and after every game (900;
                          0 = never; channels are still fetched at start)
  BARHOST_ROOM_SETUP      ';'-separated chat commands sent once boss
                          (default "!preset custom;!bSet experimentallegionfaction 1")
  BARHOST_BOT_AI          AI short name (BARb), without BARHOST_AI_CHANNELS
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
  BARHOST_FRIEND_PENDING_DAYS  withdraw a friend request not accepted after
                          this many days (0 = never, default)
  BARHOST_FRIEND_OFFLINE_DAYS  remove a friend not seen online for this many
                          days, except BARHOST_FOLLOW and BARHOST_AUTO_BOSS
                          (0 = never, default). The server does not say when
                          someone was last online: the client counts from
                          when it last saw them online.
  BARHOST_MIN_HUMANS      room mode: ready humans needed before the client says
                          how to vote the game started (1; 0 = never). It
                          never starts a game itself: the players vote it.
  BARHOST_POSTGAME_DELAY  no start hint for this many seconds after a game (60)
  BARHOST_BOOST_BELOW     room mode: take a player seat while fewer humans than
                          this play, so the room lists more players (0 = never)
  BARHOST_BOOST_UNTIL     ... and spectate again once this many humans play, or
                          the room is full (7). In between, the seat is kept.
  BARHOST_PROMOTE_EVERY   room mode: `!promote` a public room with fewer humans
                          than bots, once set up, after each game and this many
                          seconds apart (the autohost's promoteDelay; its
                          "Please wait" answer sets the next try). 0 = never
                          (default)
  BARHOST_OUT_DIR         the client's files (perf-summary.csv) go under
                          $BARHOST_OUT_DIR/client/
  BARHOST_WORK_DIR        each game's engine write dir, one per game
                          ($BARHOST_OUT_DIR/client/games); put it on fast
                          storage: the engine writes its logs there in-game
  BARHOST_ARCHIVE_DIR     after each game, its files go into this archive
                          (game_archive.py: replay, metrics, logs, lobby data)
                          and leave the work dir; unset = they stay
  BARHOST_ARCHIVE_SOURCE  this client's name in the archive (the login)
  BARHOST_CHAT_DIR        chat audit log: private/<day>.log (every private
                          message, both ways), rooms/Y/M/D/<joined>_room<id>.log
                          (each room session's chat), channels/<day>.log
                          ($BARHOST_ARCHIVE_DIR/_chat/<source>; none without)
  BARHOST_METRICS_EVERY   seconds between metrics lines in a game: how far the
                          engine is behind the server (a widget,
                          rjm_host_metrics.lua) and its CPU, threads, memory,
                          disk and pressure (engine_metrics.py) (5; 0 = off)
  BARHOST_FROZEN_GAME_MINUTES  minutes a game may stand still (its frame
                          unchanged while the engine runs: paused, say) once
                          every human player has resigned or left it, before
                          the bots are resigned and the client moves to a new
                          room; with players still in it, 6 times that (0 =
                          never)
  BARHOST_ENGINE_PROFILER 1 = engine profiler on in games, for the AIs' time
                          share (0); a BARHOST_FOLLOW user's `!profiler on|off`
                          overrides it from the next game, kept in
                          BARHOST_STATE_FILE until `!profiler default`
  BARHOST_BAR_WIDGETS     0 = BAR's own widgets off in games: only the metrics
                          widget, BAR's Autoquit (it quits the engine after
                          game over) and BAR's hidden helpers load (0); 1 = on; a
                          BARHOST_FOLLOW user's `!widgets on|off` overrides it
                          from the next game, kept until `!widgets default`
  BARHOST_WIDGET_NAMES_FILE  BAR's widget names, learnt after each game from
                          its LuaUI/Config/BYAR.lua (first: the newest archived
                          game's) (bar-widget-names.json next to BARHOST_STATE_FILE)
  BARHOST_STATUS_FILE     JSON status, rewritten on every change
                          (/tmp/lobby-client-status.json)
"""
import asyncio
import base64
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
import engine_metrics  # noqa: E402
import game_archive  # noqa: E402
import install_ai  # noqa: E402
from barcommon import (  # noqa: E402
    Engine, battle_status, client_script, engine_settings, make_log, password_hash, unpack_battle_status,
)

VERSION = "rjm-bar-host-client:1"
# !games lists at most this many; !perf <n> reaches as far back.
GAMES_KEPT = 20
# Lobby traffic about the room, kept per game in lobby-events.log while a game
# runs (those with a room id only for this client's room).
ROOM_EVENTS = {"SAIDBATTLE", "SAIDBATTLEEX", "JOINEDBATTLE", "LEFTBATTLE", "CLIENTBATTLESTATUS",
               "UPDATEBATTLEINFO", "SETSCRIPTTAGS", "REMOVESCRIPTTAGS", "ADDBOT", "REMOVEBOT", "UPDATEBOT",
               "ADDSTARTRECT", "REMOVESTARTRECT", "CLIENTSTATUS", "BATTLECLOSED"}
ROOM_EVENTS_BY_ID = {"JOINEDBATTLE", "LEFTBATTLE", "UPDATEBATTLEINFO", "ADDBOT", "REMOVEBOT", "UPDATEBOT",
                     "BATTLECLOSED", "CLIENTSTATUS"}
# Lobby chat kept in the chat audit log (BARHOST_CHAT_DIR).
CHAT_EVENTS = {"SAIDPRIVATE", "SAIDPRIVATEEX", "SAIDBATTLE", "SAIDBATTLEEX", "JOINEDBATTLE", "LEFTBATTLE",
               "SAID", "SAIDEX"}
FOUNDER_RE = re.compile(r"^Host\[([A-Za-z]+)(\d+)\]\[\d+\]$")
# A region's autohost manager, which opens private rooms on `!privatehost`
# (BYAR-Chobby battle_list_window.lua, HostBattle).
MANAGER_RE = re.compile(r"^Host\[([A-Za-z]+)\d+\]$")
PRIVATE_WAIT = 30           # seconds to wait for the private room to appear
VACANCY_WAIT = 3            # seconds after joining before judging a room free
MAP_WAIT = 20               # seconds for the autohost to switch to a !map before skipping it
# Room mode: the title follows the AIs' bonus once their updates have settled
# (a !setallaibonus is one UPDATEBOT per AI).
RENAME_SETTLE = 5
# Room mode: with no human in the player team this long, the room goes back to
# its default settings (anything voted in since is undone), once per idle spell,
# only if someone changed something (a command in the room's chat, a vote
# passed) since it was set up or last reset, and at most once per
# IDLE_RESET_EVERY (seconds since the last reset). Players joining and leaving
# alone change nothing.
IDLE_RESET_AFTER = 60
IDLE_RESET_EVERY = 900
# Commands that change none of what a reset restores.
IDLE_RESET_IGNORE = {"ring", "status", "help", "list", "stats", "whois", "cv", "callvote", "vote", "y", "n",
                     "ev", "endvote", "promote", "map", "nextmap", "boss", "joinas", "spec", "specafk",
                     "balance", "rename", "say", "start", "stop", "resign", "notify", "aibonus"}
VOTE_PASSED_RE = re.compile(r'Vote for command "([^"]*)" passed', re.IGNORECASE)
IDLE_RESET_SAY = "Nobody is playing: back to this room's default settings."
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
# BARHOST_BONUS_VOTE: the client's own AI bonus vote. While anyone is boss,
# SPADS counts every other player as level 0 (spads.pl:8549), and BAR's
# setAllAiBonus needs level 10 to vote (spads_config_bar BarManagerCmd.conf),
# so the players cannot vote it there. Plain words, not `!` commands: SPADS
# answers any unknown `!word` with "Invalid command" (spads.pl:3232), and
# `!y` / `!n` are its own vote aliases (spads.pl:296, :305).
AIBONUS_RE = re.compile(r"^!?aibonus\s+(\d{1,3})\s*$", re.IGNORECASE)
AIBONUS_VOTE_RE = re.compile(r"^!?(y|yes|n|no)\s*$", re.IGNORECASE)
AIBONUS_HINT = "AI bonus (now %s): call a vote with aibonus <0-100>, then the players say y or n."
AIBONUS_VOTE_TIME = 60
# BARHOST_BALANCE: at most one !balance per this many seconds (a seat that
# changes hands would otherwise balance the room on every change).
BALANCE_GAP = 30
BONUS_PASSED_RE = re.compile(r'Vote for command "(?:setallaibonus|force \S+ bonus) (\d{1,3})" passed', re.IGNORECASE)
SETALL_RE = re.compile(r"^!setallaibonus\s+\d+\s*$", re.IGNORECASE)
# An authorised user's `!stop` by PM: a spectator (host or this client) may
# not stop a game (SPADS [stop] is for players only), so ask the players to
# vote, and leave it to them. At most once a minute.
STOP_HINT = "%s asks to end this game: a player can type !cv stop, then the others !vote y."
STOP_HINT_EVERY = 60
# Everyone ready: the players vote the start (the client never starts a game).
START_HINT = "Everyone is ready. To start the game, call a vote with !cv start, then the others !vote y."
START_HINT_EVERY = 60
# After a start is called or everyone is ready, a start may be under way: no
# boost seat and no move to another room for this long.
START_HOLD = 120
# Said after ending (!ev) a vote that would unboss this client (every boss
# or unboss vote is ended; this line only for those against the client).
SHAME_SAY = "Shame on you, %s!"
CALLER_RE = re.compile(r"(\S+) called a vote for command")
# Votes that would change the start boxes the client sets after every map
# change. SPADS box commands (spads.pl :229-300: addbox, clearbox,
# loadboxes, split); presets, which reset the battle settings the boxes are
# in (preset, bpreset); and the start settings (bar-Beyond-All-Reason
# modoptions.lua:2276-2296 mapmetadata_startpos, mapmetadata_startboxes_set,
# mapmetadata_startbox_override; SPADS startpostype), whether voted through
# bSet or as their own shortcut (spads.pl:3073).
START_BOX_COMMANDS = {"addbox", "clearbox", "loadboxes", "split", "preset", "bpreset"}
START_BOX_SETTING_RE = re.compile(r"^(?:mapmetadata_start\w*|startpostype)$", re.IGNORECASE)


def is_start_box_vote(words) -> bool:
    """A voted command (split into words) that would change the start boxes."""
    cmd = words[0].lower()
    if cmd in START_BOX_COMMANDS or START_BOX_SETTING_RE.match(cmd):
        return True
    return cmd == "bset" and len(words) > 1 and bool(START_BOX_SETTING_RE.match(words[1]))

# The autohost's answers to !promote (SPADS spads.pl hPromote, :10963): the
# cooldown left (secToTime, :1096: "4 minutes and 10 seconds"), done, or
# refused (closed, disabled, locked, full, enough players).
PROMOTE_WAIT_RE = re.compile(r"Please wait (.+?) before promoting battle")
PROMOTE_UNIT_RE = re.compile(r"(\d+) (year|day|hour|minute|second)s?")
PROMOTE_UNITS = {"year": 31536000, "day": 86400, "hour": 3600, "minute": 60, "second": 1}
PROMOTE_DONE = "Promoting battle in"
PROMOTE_REFUSED_RE = re.compile(r"Unable to promote battle|Promote command is disabled")
# A command this client may not call (spads.pl:3280-3287).
PROMOTE_DENIED_RE = re.compile(r'not allowed to call command "promote"', re.IGNORECASE)
PROMOTE_RETRY = 120         # after a refusal, or no answer
PROMOTE_MARGIN = 5          # past the cooldown the autohost names
# !stop while this engine is in the game: it leaves, which resigns the bots.
RESIGN_SAY = "%s ended this game: resigning the AI bots."
RESIGN_KILL_AFTER = 20
# Seconds after the autohost's game ends before the client stops its engine if
# it is still running. BAR's Autoquit widget quits a headless engine 12 s after
# game over (luaui/Widgets/cmd_autoquit.lua); without it the engine stays in
# the ended game for good, holding its memory and CPU.
ENGINE_QUIT_AFTER = 60
# !rehost: end the game (as !stop), leave the room, take a new one.
REHOST_GAME_SAY = "%s ended this game: resigning the AI bots and moving to a new room."
REHOST_SAY = "%s asked me to rehost: moving to a new room."
# A game standing still (paused with nobody left to unpause, say): checked
# this often, from the metrics widget's timeline.
FROZEN_CHECK_EVERY = 60
FROZEN = "a frozen game"    # rehost_due for it: nobody to tell
# ... given up after BARHOST_FROZEN_GAME_MINUTES only once every human who
# started has resigned or left (the engine's console), after this many times
# that regardless (an abandoned pause).
FROZEN_ANYWAY = 6
PLAYER_GONE_RE = re.compile(rb"\] Player (\S+) (?:resigned from|left) the game")
FROZEN_SAY = "This game has stood still for %d minutes: resigning the AI bots and moving to a new room."

# SPADS kicks clients that talk too fast: at most one command per this many s.
COMMAND_SPACING = 1.5
# Friend list upkeep (Teiserver spring_user_in.ex: c.user.list_relationships,
# rescind_friend_request, remove_friend, whois; relationship_lib.ex
# check_relationship_limit counts friends + outgoing + incoming requests).
FRIEND_SWEEP_FIRST = 60         # seconds after logging in (the user list is in)
FRIEND_SWEEP_EVERY = 6 * 3600
FRIEND_FULL = "You have reached the maximum number of friends"
FRIEND_FULL_PAUSE = 6 * 3600    # no requests for this long after FRIEND_FULL
FRIEND_BATCH = 50               # account ids per command line
DAY = 86400
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


_log = make_log("lobby-client")
# While a game runs, the client's lines also go to client.log in its run dir
# (and from there into the archive), timestamped (UTC).
GAME_LOG = {"file": None}


def log(msg: str) -> None:
    _log(msg)
    f = GAME_LOG["file"]
    if f is not None:
        try:
            f.write("%s %s\n" % (dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), msg))
        except (OSError, ValueError):
            GAME_LOG["file"] = None


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
        # Room mode: trusted players made boss on joining; unlike BARHOST_FOLLOW
        # they cannot command the client.
        self.auto_boss = {u.strip() for u in g("BARHOST_AUTO_BOSS").replace(";", ",").split(",") if u.strip()}
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
        # Named AI choices (!ai <name>), each the engine's own AI or a release
        # kept on the data volume (install_ai.py "Channels").
        try:
            self.ai_channels = install_ai.parse_channels(g("BARHOST_AI_CHANNELS"))
        except ValueError as e:
            raise SystemExit("BARHOST_AI_CHANNELS: %s" % e)
        self.ai_channel = g("BARHOST_AI_CHANNEL").lower() or next(iter(self.ai_channels), "")
        if self.ai_channels and self.ai_channel not in self.ai_channels:
            raise SystemExit("BARHOST_AI_CHANNEL %r is not in BARHOST_AI_CHANNELS" % self.ai_channel)
        self.ai_releases_dir = Path(g("BARHOST_AI_RELEASES_DIR",
                                      os.path.join(g("BARHOST_DATA_DIR", "/bar/data"), "ai-releases")))
        # Room mode: a friend request to each player who joins the room, once
        # per account (Teiserver c.user.add_friend <account id>).
        self.friend_joiners = g("BARHOST_FRIEND_JOINERS", "0") == "1"
        # The friend list's upkeep (Teiserver counts friends and pending
        # requests both ways against one limit, 300 by default): unanswered
        # requests withdrawn after this many days, friends unseen this long
        # removed. 0 = never.
        self.friend_pending_days = float(g("BARHOST_FRIEND_PENDING_DAYS", "0"))
        self.friend_offline_days = float(g("BARHOST_FRIEND_OFFLINE_DAYS", "0"))
        self.min_humans = int(g("BARHOST_MIN_HUMANS", "1"))
        # Room mode: no start hint until at least as many humans play as bots.
        self.matched_teams = g("BARHOST_MATCHED_TEAMS", "0") == "1"
        # Room mode: `!ring` (the autohost rings every unready player) once the
        # teams are matched and someone is not ready, every this many seconds.
        self.ring_every = int(g("BARHOST_RING_EVERY", "0"))
        self.ring_first = int(g("BARHOST_RING_FIRST", "15"))   # the first ring, this long after
        # Room mode: the players vote the AI bonus through this client
        # (`aibonus <n>`), at most once per this many seconds per caller.
        self.bonus_vote = g("BARHOST_BONUS_VOTE", "0") == "1"
        self.bonus_vote_cooldown = int(g("BARHOST_BONUS_VOTE_COOLDOWN", "300"))
        # Room mode: `!balance` once the room is set up (the bots' team ids)
        # and each time the player team fills (as many humans as bots).
        self.balance = g("BARHOST_BALANCE", "0") == "1"
        self.postgame_delay = int(g("BARHOST_POSTGAME_DELAY", "60"))
        # Room mode: a player seat while few humans play (the battle list
        # counts players), a spectator again from boost_until or a full room.
        self.boost_below = int(g("BARHOST_BOOST_BELOW", "0"))
        self.boost_until = int(g("BARHOST_BOOST_UNTIL", "7"))
        # Room mode: !promote while the player team is not full (0 = never).
        self.promote_every = int(g("BARHOST_PROMOTE_EVERY", "0"))
        self.engine_version = g("BAR_ENGINE_VERSION")
        self.out_dir = Path(g("BARHOST_OUT_DIR", "/bar/out")) / "client"
        # Each game runs in its own directory here (fast storage: everything
        # the engine writes during a game lands here) ...
        self.work_dir = Path(g("BARHOST_WORK_DIR") or str(self.out_dir / "games"))
        # ... and, once the engine exits, is moved into the game archive (long-
        # term storage shared by every app; game_archive.py), if one is set.
        self.archive_dir = Path(g("BARHOST_ARCHIVE_DIR")) if g("BARHOST_ARCHIVE_DIR") else None
        self.archive_source = g("BARHOST_ARCHIVE_SOURCE") or re.sub(r"[^A-Za-z0-9.-]+", "-", self.login).strip("-")
        # Chat audit log: room chat per room session, private messages per
        # day (both directions, every sender), lobby channels per day. With the
        # archive by default (ROOT/_chat/<source>); none without either.
        chat = g("BARHOST_CHAT_DIR")
        self.chat_dir = Path(chat) if chat else (
            self.archive_dir / "_chat" / self.archive_source if self.archive_dir is not None else None)
        # Seconds between lines of the game's metrics (engine-timeline.csv from
        # the engine, timeline.csv from this client); 0 = none.
        self.metrics_every = float(g("BARHOST_METRICS_EVERY", "5"))
        # A game whose frame has not moved for this long (the metrics widget's
        # timeline, so it needs BARHOST_METRICS_EVERY) is given up; 0 = never.
        self.frozen_game_after = float(g("BARHOST_FROZEN_GAME_MINUTES", "0")) * 60
        # 1 = turn the engine's profiler on in games, for the "AI" time share
        # (/debug 1 0; costs a timer around every AI call).
        self.engine_profiler = g("BARHOST_ENGINE_PROFILER", "0") == "1"
        # 0 (default) = BAR's own widgets off in games: only the metrics
        # widget (and BAR's hidden helpers, which always load); 1 = all on. A headless engine still
        # runs every widget's Lua (unit events, Update, the draw call-ins with
        # stubbed GL) on the sim thread.
        self.bar_widgets = g("BARHOST_BAR_WIDGETS", "0") == "1"
        # BAR's widget names, learnt from each game's LuaUI/Config/BYAR.lua.
        self.widget_names_file = Path(g("BARHOST_WIDGET_NAMES_FILE",
                                        str(self.state_file.parent / "bar-widget-names.json")))
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
        self.max_players = 0  # the room's player cap (BATTLEOPENED), 0 = unknown
        self.bots = {}  # name -> {"owner", "status"}
        self.script_tags = {}  # SETSCRIPTTAGS of the room joined: mod options, skills


class LobbyClient:
    """The protocol state machine. `feed(line)` and `tick(now)` act on the
    world; everything to send is appended to `self.outbox`. No I/O here."""

    def __init__(self, s: Settings, now=time.monotonic, launch=None, perf=None, activate=None,
                 metrics=None, archive=None, wall=time.time):
        self.s = s
        self.now = now
        self.wall = wall            # seconds since the epoch: friend ages kept across restarts
        self.launch = launch or (lambda b, pw: None)
        # engine_metrics.HostSampler: the engine's CPU, threads, disk, pressure.
        self.metrics = metrics
        # Called with a finished game's run dir (game_archive.py, off the loop).
        self.archive = archive or (lambda run_dir: None)
        self.game_events = None     # lobby-events.log of the game running
        self.room_log = None        # (battle, path): this room session's chat log
        # !games / !perf: the game running (its run dir, start, map) and those
        # ended since the client started, until the archive's index has them.
        self.current_game = None
        self.finished_games = []
        self.rehost_due = None      # !rehost: the user, until the room is left
        # Points the engine at a channel's release (install_ai.activate_channel);
        # returns its tag. The default, for tests, only reports the known tag.
        self.activate = activate or (lambda channel: self.ai_releases.get(channel, ""))
        # cgroup_perf.PerfRecorder: the container's CPU and memory per game.
        self.perf = perf
        self.outbox = []
        # Room mode: public or private, as last chosen by !public / !private;
        # kept on the data volume so a restart keeps it.
        state = self.load_state()
        self.privacy = state.get("privacy", s.privacy)
        # Room mode: the next map of BARHOST_MAP_ROTATION; also kept.
        self.map_index = state.get("map_index", 0)
        # The bots' AI: with channels, the channel last chosen by !ai (kept)
        # decides it; without, BARHOST_BOT_AI or the last !bots n AI (kept).
        self.ai_channel = state.get("ai_channel") if state.get("ai_channel") in s.ai_channels else s.ai_channel
        self.bot_ai = s.ai_channels[self.ai_channel][0] if s.ai_channels else state.get("bot_ai", s.bot_ai)
        self.ai_releases = {}       # channel -> release tag downloaded (from the I/O layer)
        # Channel -> the exact release `!ai <channel> <tag>` holds it to (kept);
        # without one a channel follows its moving tag (latest / newest).
        self.ai_pins = {k: v for k, v in state.get("ai_pins", {}).items()
                        if k in s.ai_channels and s.ai_channels[k][1]}
        self.pin_asked = {}         # channel -> who pinned it, until the download is done
        # The engine profiler: BARHOST_ENGINE_PROFILER until a user's
        # `!profiler on|off` (kept; `!profiler default` forgets it). Each game's
        # launch reads s.engine_profiler, so a change applies from the next game.
        self.profiler_default = s.engine_profiler
        self.profiler_choice = state.get("engine_profiler")
        if self.profiler_choice is not None:
            s.engine_profiler = self.profiler_choice
        # BAR's widgets: BARHOST_BAR_WIDGETS until `!widgets on|off` (kept).
        self.widgets_default = s.bar_widgets
        self.widgets_choice = state.get("bar_widgets")
        if self.widgets_choice is not None:
            s.bar_widgets = self.widgets_choice
        self.ai_active = None       # (channel, tag) the engine was last pointed at
        # Point the engine at the channel between games; at start, once the
        # releases are fetched.
        self.ai_activate_due = False
        # Accounts already sent a friend request; kept so each gets one.
        self.friended = set(str(i) for i in state.get("friended", []))
        # Friend list upkeep, kept: when each pending request was sent (wall
        # time; 0 = before this was recorded), when each friend was last seen
        # online, and their names.
        self.friend_sent = dict(state.get("friend_sent", {}))
        self.friend_seen = dict(state.get("friend_seen", {}))
        self.friend_names = dict(state.get("friend_names", {}))
        self.friends_reconciled = bool(state.get("friends_reconciled"))
        self.friends = set()        # account ids, from the last list
        self.friend_pause_until = 0.0
        self.next_friend_sweep = None
        self.whois_due = set()      # friends to remove once whois says who they are
        self.account_ids = {}       # user name -> account id, from ADDUSER
        self.ai_switch_due = False  # a switch asked during a game, done after it
        # New AI releases: checked between games by the I/O layer
        # (install_ai.update_channels, or install_ai.update without channels).
        # Channels are fetched at start too: the chosen one must be in place.
        self.next_ai_check = self.now() + s.ai_update_every
        self.need_ai_update = self.ai_updating = bool(s.ai_channels)
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
        if isinstance(st.get("ai_channel"), str):
            out["ai_channel"] = st["ai_channel"]
        if isinstance(st.get("ai_pins"), dict):
            out["ai_pins"] = {k: v for k, v in st["ai_pins"].items()
                              if isinstance(k, str) and isinstance(v, str) and install_ai.TAG_RE.match(v)}
        if isinstance(st.get("friended"), list):
            out["friended"] = [str(i) for i in st["friended"] if str(i).isdigit()]
        for key in ("friend_sent", "friend_seen"):
            if isinstance(st.get(key), dict):
                out[key] = {str(k): float(v) for k, v in st[key].items()
                            if str(k).isdigit() and isinstance(v, (int, float))}
        if isinstance(st.get("friend_names"), dict):
            out["friend_names"] = {str(k): v for k, v in st["friend_names"].items()
                                   if str(k).isdigit() and isinstance(v, str)}
        if isinstance(st.get("friends_reconciled"), bool):
            out["friends_reconciled"] = st["friends_reconciled"]
        for key in ("engine_profiler", "bar_widgets"):
            if isinstance(st.get(key), bool):
                out[key] = st[key]
        return out

    def save_state(self):
        try:
            self.s.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.s.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"privacy": self.privacy, "map_index": self.map_index,
                                       "bot_ai": self.bot_ai, "ai_channel": self.ai_channel,
                                       "ai_pins": self.ai_pins,
                                       "engine_profiler": self.profiler_choice,
                                       "bar_widgets": self.widgets_choice,
                                       "friended": sorted(self.friended),
                                       "friend_sent": self.friend_sent, "friend_seen": self.friend_seen,
                                       "friend_names": self.friend_names,
                                       "friends_reconciled": self.friends_reconciled}))
            tmp.replace(self.s.state_file)
        except OSError as e:
            log("could not save %s: %s" % (self.s.state_file, e))

    # -- state -------------------------------------------------------------

    def reset(self):
        self.state = "connecting"   # connecting, login, lobby, joining, setup, waiting, ingame
        self.users = {}             # name -> CLIENTSTATUS int
        self.battles = {}
        self.battle = None
        self.script_password = ""
        self.statuses = {}          # name -> unpacked battle status, in my battle
        self.my_status = None
        self.boss = False
        self.pending = []           # chat/commands waiting for the flood spacing
        self.last_sent = 0.0
        self.join_attempted = set()
        self.next_join = 0.0
        # A running engine and the game count survive a reconnect.
        self.game_proc = getattr(self, "game_proc", None)
        self.games = getattr(self, "games", 0)
        self.last_error = getattr(self, "last_error", "")
        self.need_fetch = None      # a Battle whose content the I/O layer must fetch
        self.synced = False
        self.boss_deadline = None
        self.quiet_until = 0.0      # no start hint before this (after a game)
        self.start_hint_given = False  # said for this time everyone is ready
        self.start_hint_after = 0.0
        self.start_hold_until = 0.0  # room mode: a start may be under way until then
        self.promote_after = 0.0    # room mode: no !promote before this (its cooldown)
        self.promote_known = 0.0    # the end of a cooldown the autohost named
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
        self.ring_after = None      # room mode: when to !ring the unready players
        self.balanced_full = False  # room mode: !balance said for this time the team is full
        self.balance_after = 0.0    # room mode: no !balance before this (BALANCE_GAP)
        self.stop_hint_after = 0.0  # no !stop request posted before this
        self.resign_kill_at = getattr(self, "resign_kill_at", None)  # !stop: kill the engine if still running then
        # The autohost's game ended: stop the engine if it has not quit by then.
        self.engine_quit_at = getattr(self, "engine_quit_at", None)
        self.bonus_hint_after = 0.0  # no bonus hint before this
        self.voted_bonus = None     # the AI bonus this room voted in, if any
        self.room_bonus = None      # the bonus every AI here was last seen with
        self.title_sent = None      # room mode: the title last set with !rename
        self.rename_after = 0.0     # room mode: no rename before this (RENAME_SETTLE)
        self.idle_since = None      # room mode: since when no human plays
        self.defaults_due = None    # room mode: what a human changed since the room was set up or reset
        self.defaults_at = None     # room mode: when restore_defaults last ran
        self.aibonus_vote = None    # room mode: the AI bonus vote running, if any
        self.aibonus_called = {}    # caller (lower case) -> when they last called one
        self.aibonus_remind = False  # remind how to vote the bonus once the next map is in
        self.vacant = None          # room mode: the joined room was found free
        self.vacancy_check_at = None
        self.room_password_now = ""  # the private room's password, once in it

    def status(self) -> dict:
        b = self.battle
        humans = self.humans()
        return {
            "state": self.state, "mode": self.s.mode, "login": self.s.login, "games": self.games,
            "follow": sorted(self.s.follow), "autoBoss": sorted(self.s.auto_boss), "wantBots": self.want_bots, "botAI": self.bot_ai,
            "aiChannel": self.ai_channel or None, "aiRelease": self.ai_active[1] if self.ai_active else None,
            "aiPins": dict(self.ai_pins),
            "paused": self.paused, "privacy": self.privacy if self.s.mode == "room" else None,
            "battle": None if not b else {
                "id": b.id, "founder": b.founder, "title": b.title, "map": b.map, "game": b.game,
                "engine": b.engine_version, "boss": self.boss, "synced": self.synced,
                "bots": sorted(n for n, x in b.bots.items() if x["owner"] == self.s.login),
                "humans": [{"name": n, **{k: self.statuses.get(n, {}).get(k) for k in ("ready", "sync", "ally")}}
                           for n in humans],
            },
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
        line = " ".join(str(w) for w in words)
        self.outbox.append(line)
        w = line.split(" ", 2)
        if w[0] == "SAYPRIVATE" and len(w) == 3:
            self.private_chat("->", w[1], "", w[2])
        elif w[0] == "SAYBATTLE" and len(w) >= 2:
            self.room_chat("%s: %s" % (self.s.login, line.split(" ", 1)[1]))
        elif w[0] == "SAYBATTLEPRIVATEEX" and len(w) == 3:
            self.room_chat("%s -> %s (only they see it): %s" % (self.s.login, w[1], w[2]))

    # -- chat audit log ----------------------------------------------------

    def chat_line(self, path, text):
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("%s %s\n" % (stamp, re.sub(r"[\r\n]+", " ", text)))
        except OSError as e:
            _log("could not write the chat log %s: %s" % (path, e))

    def chat_day(self, folder):
        return self.s.chat_dir / folder / ("%s.log" % dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"))

    def pm_sender(self, user):
        """Who a private message is from, as on_SAIDPRIVATE treats it: only
        authorised users get answers; nobody else ever gets a reply."""
        if user in self.s.follow:
            return "authorised"
        if self.battle and user == self.battle.founder:
            return "autohost"
        req = self.private_request
        if req and user == req["manager"]:
            return "autohost manager"
        return "not authorised, ignored"

    def private_chat(self, way, user, tag, text):
        if self.s.chat_dir is not None:
            self.chat_line(self.chat_day("private"), "%s %s%s: %s" % (way, user, " [%s]" % tag if tag else "", text))

    def room_chat(self, text):
        """A line in this room session's chat log (one file per room joined)."""
        if self.s.chat_dir is None or self.battle is None:
            return
        if self.room_log is None or self.room_log[0] is not self.battle:
            b = self.battle
            now = dt.datetime.now(dt.timezone.utc)
            path = self.s.chat_dir / "rooms" / now.strftime("%Y/%m/%d") / ("%s_room%s.log" % (
                now.strftime("%Y%m%d-%H%M%SZ"), re.sub(r"[^A-Za-z0-9]+", "-", b.id)))
            self.room_log = (b, path)
            self.chat_line(path, "* room %s (%s) on %s, %s" % (b.id, b.title, b.founder, b.map))
        self.chat_line(self.room_log[1], text)

    def end_room_chat(self, why):
        if self.room_log is not None and self.room_log[0] is self.battle:
            self.room_chat("* left the room: %s" % why)
        self.room_log = None

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
        if self.game_events is not None and self.battle and cmd in ROOM_EVENTS:
            w = line.split(" ", 2)
            if cmd not in ROOM_EVENTS_BY_ID or (len(w) > 1 and w[1] == self.battle.id) \
                    or (cmd == "CLIENTSTATUS" and len(w) > 1 and w[1] in self.battle.users):
                try:
                    self.game_events.write("%s %s\n" % (
                        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), line))
                except (OSError, ValueError):
                    self.game_events = None
        if cmd in CHAT_EVENTS:
            self.log_chat(cmd, line)
        if cmd.startswith("s.user."):
            handler = getattr(self, "on_s_user_" + cmd[7:], None)
            if handler:
                handler(line)
            return
        handler = getattr(self, "on_" + cmd, None)
        if handler:
            handler(line)

    def log_chat(self, cmd, line):
        w = line.split(" ", 2)
        if cmd in ("SAIDPRIVATE", "SAIDPRIVATEEX") and len(w) == 3:
            self.private_chat("<-", w[1], self.pm_sender(w[1]), ("/me " if cmd.endswith("EX") else "") + w[2])
        elif cmd in ("SAIDBATTLE", "SAIDBATTLEEX") and len(w) == 3 and w[1] != self.s.login:
            self.room_chat("%s: %s%s" % (w[1], "/me " if cmd.endswith("EX") else "", w[2]))
        elif cmd in ("JOINEDBATTLE", "LEFTBATTLE") and len(w) >= 3 and self.battle and w[1] == self.battle.id:
            who = w[2].split(" ")[0]
            if who != self.s.login:
                self.room_chat("* %s %s" % (who, "joined" if cmd == "JOINEDBATTLE" else "left"))
        elif cmd in ("SAID", "SAIDEX") and self.s.chat_dir is not None:
            p = line.split(" ", 3)
            if len(p) == 4:
                self.chat_line(self.chat_day("channels"), "#%s %s: %s%s" % (
                    p[1], p[2], "/me " if cmd == "SAIDEX" else "", p[3]))

    def on_SETSCRIPTTAGS(self, line):
        # SETSCRIPTTAGS key=value[\tkey=value...] for the room joined: mod options
        # (game/modoptions/...), player skill (game/players/<name>/skill), ...
        if self.battle is None:
            return
        for pair in line.split(" ", 1)[1].split("\t") if " " in line else []:
            k, sep, v = pair.partition("=")
            if sep and k:
                self.battle.script_tags[k.lower()] = v

    def on_REMOVESCRIPTTAGS(self, line):
        if self.battle is not None:
            for k in line.split(" ")[1:]:
                self.battle.script_tags.pop(k.lower(), None)

    def on_TASSERVER(self, line):
        self.state = "login"
        sentence = "LuaLobby %s\t%s %s\tb sp" % (
            VERSION, hashlib.md5(self.s.login.encode()).hexdigest()[:16], "0")
        self.send("LOGIN", self.s.login, password_hash(self.s.password), "0", "*", sentence)

    def on_ACCEPTED(self, line):
        log("logged in as %s" % self.s.login)
        self.state = "lobby"
        if self.s.friend_pending_days > 0 or self.s.friend_offline_days > 0:
            self.next_friend_sweep = self.now() + FRIEND_SWEEP_FIRST
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
                if w[3] in self.friends:            # a friend logged in: seen now
                    self.friend_seen[w[3]] = self.wall()
                    self.friend_names[w[3]] = w[1]

    def friend(self, user):
        """Room mode: one friend request per account, ever (kept in the state
        file). Teiserver takes it by account id (spring_user_in.ex add_friend)
        and answers s.user.add_friend <id> success / failure <reason>."""
        aid = self.account_ids.get(user)
        if not self.s.friend_joiners or self.s.mode != "room" or not aid or aid in self.friended \
                or self.now() < self.friend_pause_until:
            return
        self.friended.add(aid)
        self.save_state()
        log("friend request to %s (%s)" % (user, aid))
        self.send("c.user.add_friend", aid)

    def on_s_user_add_friend(self, line):
        w = line.split(" ", 1)[1].split("\t") if " " in line else []
        if len(w) < 2:
            return
        aid, result = w[0], w[1]
        if result == "success":
            log("friend request %s: sent" % aid)
            self.friend_sent[aid] = self.wall()
        elif len(w) > 2 and w[2] == FRIEND_FULL:
            # Not sent: ask this player again later, and nobody for a while.
            self.friended.discard(aid)
            if self.now() >= self.friend_pause_until:
                log("friend request %s: %s; no requests for %d h" % (aid, w[2], FRIEND_FULL_PAUSE // 3600))
            self.friend_pause_until = self.now() + FRIEND_FULL_PAUSE
        else:
            log("friend request %s: %s" % (aid, " ".join(w[1:])))
        self.save_state()

    # -- friend list upkeep -------------------------------------------------

    def friend_sweep(self):
        self.next_friend_sweep = self.now() + FRIEND_SWEEP_EVERY
        self.send("c.user.list_relationships")

    def send_ids(self, cmd, ids):
        ids = sorted(ids, key=int)
        for i in range(0, len(ids), FRIEND_BATCH):
            self.send(cmd, "\t".join(ids[i:i + FRIEND_BATCH]))

    def friend_exempt(self, name):
        return name.lower() in {u.lower() for u in self.s.follow | self.s.auto_boss}

    def on_s_user_list_relationships(self, line):
        try:
            raw = line.split(" ", 1)[1].strip()
            data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
            ids = {k: {str(i) for i in (data.get(k) or [])} for k in
                   ("friends", "outgoing_friend_requests", "incoming_friend_requests")}
        except (IndexError, ValueError, TypeError, AttributeError) as e:
            log("friend list: could not read the server's answer: %s" % e)
            return
        friends = ids["friends"]
        outgoing = ids["outgoing_friend_requests"]
        incoming = ids["incoming_friend_requests"]
        wall = self.wall()
        self.friends = friends
        if not self.friends_reconciled:
            # Once: accounts marked asked whose request is gone (refused while
            # the list was full, or declined) may be asked again.
            gone = self.friended - friends - outgoing - incoming
            self.friended -= gone
            self.friends_reconciled = True
            if gone:
                log("friend list: %d earlier requests are not pending; they may be asked again" % len(gone))
        online = {aid: n for n, aid in self.account_ids.items() if n in self.users}
        for f in friends:
            if f in online:
                self.friend_names[f] = online[f]
            if f in online or f not in self.friend_seen:
                self.friend_seen[f] = wall       # online now, or tracked from now
        for d, keep in ((self.friend_seen, friends), (self.friend_names, friends), (self.friend_sent, outgoing)):
            for k in [k for k in d if k not in keep]:
                del d[k]
        withdraw, remove, ask = [], [], []
        if self.s.friend_pending_days > 0:
            for o in outgoing:
                self.friend_sent.setdefault(o, 0.0)   # sent before this was recorded: old
                if wall - self.friend_sent[o] >= self.s.friend_pending_days * DAY:
                    withdraw.append(o)
        if self.s.friend_offline_days > 0:
            for f in friends:
                if wall - self.friend_seen[f] < self.s.friend_offline_days * DAY:
                    continue
                name = self.friend_names.get(f)
                if name is None:
                    ask.append(f)               # whois first: never drop an exempt user unseen
                elif not self.friend_exempt(name):
                    remove.append(f)
        log("friend list: %d friends, %d requests sent and %d received pending; withdrawing %d "
            "(over %g days), removing %d (not seen online for %g days), checking %d" % (
                len(friends), len(outgoing), len(incoming), len(withdraw), self.s.friend_pending_days,
                len(remove), self.s.friend_offline_days, len(ask)))
        if withdraw:
            self.send_ids("c.user.rescind_friend_request", withdraw)
            for o in withdraw:
                self.friend_sent.pop(o, None)
        if remove:
            self.send_ids("c.user.remove_friend", remove)
        for f in ask:
            self.whois_due.add(f)
            self.send("c.user.whois", f)
        if withdraw or remove:
            self.friend_pause_until = 0.0       # room on the list again
        self.save_state()

    def on_s_user_whois(self, line):
        w = line.split(" ")
        if len(w) < 3 or w[1] not in self.whois_due:
            return
        aid = w[1]
        self.whois_due.discard(aid)
        try:
            raw = w[2].strip()
            name = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4))).get("name")
        except (ValueError, TypeError, AttributeError):
            name = None
        if not isinstance(name, str):
            log("friend list: no name for %s; kept" % aid)
            return
        self.friend_names[aid] = name
        if not self.friend_exempt(name):
            self.send("c.user.remove_friend", aid)
        self.save_state()

    def on_s_user_rescind_friend_request(self, line):
        w = line.split(" ", 1)[1].split("\t") if " " in line else []
        if len(w) >= 2 and w[1] != "success":
            log("withdrawing friend request %s: %s" % (w[0], " ".join(w[1:])))

    def on_s_user_remove_friend(self, line):
        w = line.split(" ", 1)[1].split("\t") if " " in line else []
        if len(w) < 2:
            return
        if w[1] == "success":
            log("removed friend %s (%s)" % (w[0], self.friend_names.get(w[0], "?")))
            for d in (self.friend_seen, self.friend_names):
                d.pop(w[0], None)
            self.friends.discard(w[0])
            self.save_state()
        else:
            log("removing friend %s: %s" % (w[0], " ".join(w[1:])))

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
                if self.game_running():
                    self.engine_quit_at = self.now() + ENGINE_QUIT_AFTER

    def on_BATTLEOPENED(self, line):
        p = line.split(" ", 11)
        if len(p) < 12:
            return
        rest = p[11].split("\t")
        rest += [""] * (5 - len(rest))
        bid, founder, ip, port, passworded = p[1], p[4], p[5], p[6], p[8] == "1"
        self.battles[bid] = b = Battle(bid, founder, ip, port, passworded, *rest[:5])
        b.max_players = int(p[7]) if p[7].isdigit() else 0

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
                    self.pending.append(("SAYBATTLEPRIVATEEX", p[2], self.welcome_text()))
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
        self.room_log = None
        self.room_chat("* joined")
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
            b.bots[p[2]] = {"owner": p[3], "status": unpack_battle_status(p[4]),
                            "ai": p[6] if len(p) > 6 else None}
            if b is self.battle:
                self.rename_after = self.now() + RENAME_SETTLE

    def on_REMOVEBOT(self, line):
        p = line.split(" ")
        b = self.battles.get(p[1])
        if b:
            b.bots.pop(p[2], None)
            if b is self.battle:
                self.rename_after = self.now() + RENAME_SETTLE

    def on_UPDATEBOT(self, line):
        p = line.split(" ")
        b = self.battles.get(p[1])
        if b and p[2] in b.bots:
            b.bots[p[2]]["status"] = unpack_battle_status(p[3])
            if b is self.battle:
                self.rename_after = self.now() + RENAME_SETTLE
                # A bonus set by anyone (a boss's !setallaibonus or force, a
                # vote): once every AI has it, later adds get it too.
                alike = self.bots_bonus()
                if alike is not None and alike[0] == alike[1]:
                    self.room_bonus = alike[0]

    def on_SAIDBATTLEEX(self, line):
        self.on_SAIDBATTLE(line)

    def on_SAIDBATTLE(self, line):
        p = line.split(" ", 2)
        if len(p) < 3 or not self.battle:
            return
        user, msg = p[1], p[2]
        self.note_change(user, msg)
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
            self.start_hold_until = self.now() + START_HOLD   # no boost seat or move while it starts
            if self.playing():
                self.set_seat(False, "a game start was called", first=True)
        if user == self.battle.founder and PROMOTE_DONE in msg:
            self.promote_answer(msg)        # someone else's !promote: the cooldown restarts
        passed = BONUS_PASSED_RE.search(msg) if user == self.battle.founder else None
        if passed and self.s.mode == "room" and self.boss and 0 <= int(passed.group(1)) <= 100:
            self.voted_bonus = self.room_bonus = int(passed.group(1))
            log("a bonus vote passed: every AI to %d" % self.voted_bonus)
            self.say("!setallaibonus %d" % self.voted_bonus)
            return
        if self.s.mode == "room" and self.s.bonus_vote and user not in (self.battle.founder, self.s.login) \
                and self.aibonus_chat(user, msg.strip()):
            return
        if self.s.mode == "room" and user not in (self.battle.founder, self.s.login) \
                and BONUS_RE.search(msg) and self.now() >= self.bonus_hint_after:
            self.bonus_hint_after = self.now() + BONUS_HINT_EVERY
            self.say(self.bonus_hint())
        ring = RING_RE.match(msg.strip())
        if ring and self.s.mode == "room" and user not in (self.battle.founder, self.s.login) \
                and self.now() >= self.ring_hint_after:
            self.ring_hint_after = self.now() + RING_HINT_EVERY
            name = ring.group(1) or "a player"
            self.say(RING_HINT % (name, ring.group(1) or "<name>"))
        # The autohost announces a vote as `* <user> called a vote for command
        # "<command>" [!vote y, ...]` (BYAR-Chobby gui_battle_room_window.lua,
        # ParseForVotingSaidBattle). End at once a vote that would take over
        # the room, unboss a trusted boss, or change the start boxes.
        m = VOTE_RE.search(msg)
        caller = CALLER_RE.search(msg)
        if user == self.battle.founder and self.boss and m \
                and self.protected_from(m.group(1), caller.group(1) if caller else ""):
            log("ending the vote \"%s\"" % m.group(1))
            self.pending.insert(0, "!ev")       # ahead of anything queued
            w = m.group(1).split()
            if caller and w[0].lower() == "unboss" and (len(w) == 1 or w[1].lower() == self.s.login.lower()):
                self.pending.insert(1, SHAME_SAY % caller.group(1))   # it would unboss this client

    def bonus_hint(self) -> str:
        if not self.s.bonus_vote:
            return BONUS_HINT
        alike = self.bots_bonus()
        if alike is not None:
            now = "%d%%" % alike[0] if alike[0] == alike[1] else "%d-%d%%" % alike
        else:
            now = self.current_bonus()
            now = "%d%%" % now if now is not None else "as set"
        return AIBONUS_HINT % now

    def bots_bonus(self):
        """The lowest and highest bonus of the AIs in the room, as the server
        last said; None without AIs."""
        b = self.battle
        vals = [x["status"].get("bonus", 0) for x in b.bots.values()] if b else []
        return (min(vals), max(vals)) if vals else None

    def room_title(self) -> str:
        """BARHOST_ROOM_NAME with {bonus} as the AIs' bonus now: one number,
        a range if they differ, the room's bonus without AIs."""
        name = self.s.room_name
        if "{bonus}" not in name:
            return name
        alike = self.bots_bonus()
        if alike is not None:
            text = "%d" % alike[0] if alike[0] == alike[1] else "%d-%d" % alike
        else:
            now = self.current_bonus()
            text = "%d" % now if now is not None else ""
        return " ".join(name.replace("{bonus}", text).split())

    def note_change(self, user, msg):
        """Room mode: remember a command or passed vote that may have changed
        what restore_defaults puts back."""
        if user == self.battle.founder:
            v = VOTE_PASSED_RE.search(msg)
            what = v.group(1) if v else None
        elif user != self.s.login and msg.startswith("!"):
            what = msg[1:]
        else:
            return
        word = what.split(" ", 1)[0].lower() if what else ""
        if word and word not in IDLE_RESET_IGNORE:
            self.defaults_due = "%s by %s" % (what[:80], user)

    def restore_defaults(self):
        """Room mode, as boss, nobody playing: undo whatever was voted in
        (settings, the AI bonus, start boxes, the bot count) by applying the
        room's setup again. The map and the owners' choices (AI, privacy)
        stay."""
        log("nobody plays: back to the room's default settings (changed: %s)" % self.defaults_due)
        self.defaults_due = None
        self.defaults_at = self.now()
        self.idle_since = None
        self.say(IDLE_RESET_SAY)
        self.voted_bonus = self.room_bonus = None
        self.aibonus_vote = None
        for c in self.s.setup:
            self.say(c)
        for c in self.s.bot_setup:
            self.say(c)
        self.want_bots = self.s.bot_count
        self.sync_bots()
        self.start_boxes_map = None
        self.apply_start_boxes()
        if self.s.balance:
            self.say("!balance")
            self.balance_after = self.now() + BALANCE_GAP
        self.rename_after = self.now() + RENAME_SETTLE

    def current_bonus(self):
        """The AI bonus for this room's bot adds: the one every AI was last
        seen with, else a voted one, else the configured one."""
        if self.room_bonus is not None:
            return self.room_bonus
        if self.voted_bonus is not None:
            return self.voted_bonus
        for c in self.s.bot_setup:
            if SETALL_RE.match(c):
                return int(c.split()[1])
        return None

    def aibonus_chat(self, user: str, msg: str) -> bool:
        """`aibonus <n>` (a call) or `y` / `n` (a vote) from a player; True if
        it was one of those."""
        call = AIBONUS_RE.match(msg)
        vote = AIBONUS_VOTE_RE.match(msg) if self.aibonus_vote else None
        if not call and not vote:
            return False
        if user not in self.humans():
            if call:
                self.say("%s, only players can call an AI bonus vote." % user)
            return True
        now = self.now()
        if vote:
            v = self.aibonus_vote
            yes = vote.group(1).lower().startswith("y")
            (v["yes"] if yes else v["no"]).add(user)
            (v["no"] if yes else v["yes"]).discard(user)
            self.aibonus_count()
            return True
        value = int(call.group(1))
        if not self.boss or self.state != "waiting" or self.game_running():
            self.say("%s, the AI bonus can be changed between games." % user)
        elif value > 100:
            self.say("%s, the AI bonus is 0 to 100." % user)
        elif self.aibonus_vote:
            self.say("%s, an AI bonus vote is already running (%d%%): say y or n."
                     % (user, self.aibonus_vote["value"]))
        elif now < self.aibonus_called.get(user.lower(), -1e9) + self.s.bonus_vote_cooldown:
            left = int(self.aibonus_called[user.lower()] + self.s.bonus_vote_cooldown - now) + 1
            self.say("%s, you can call another AI bonus vote in %d:%02d." % (user, left // 60, left % 60))
        else:
            self.aibonus_called[user.lower()] = now
            self.aibonus_vote = {"value": value, "caller": user, "yes": {user}, "no": set(),
                                 "until": now + AIBONUS_VOTE_TIME}
            log("%s called an AI bonus vote: %d" % (user, value))
            self.say("%s called a vote: every AI to %d%% bonus. Players, say y or n (%d s)."
                     % (user, value, AIBONUS_VOTE_TIME))
            self.aibonus_count()
        return True

    def aibonus_count(self):
        """Decide the AI bonus vote: a majority of the players yes passes it,
        enough no that yes cannot reach one fails it; at the deadline, more
        yes than no passes it. A game starting drops it."""
        v = self.aibonus_vote
        if v is None:
            return
        if self.state != "waiting" or not self.battle:
            self.aibonus_vote = None
            return
        players = set(self.humans())
        yes, no = len(v["yes"] & players), len(v["no"] & players)
        n = len(players)
        if yes * 2 > n or (self.now() >= v["until"] and yes > no):
            self.aibonus_vote = None
            self.voted_bonus = self.room_bonus = v["value"]
            self.defaults_due = "AI bonus vote %d" % v["value"]
            log("the AI bonus vote passed (%d yes, %d no): every AI to %d" % (yes, no, v["value"]))
            self.say("The AI bonus vote passed (%d yes, %d no): every AI to %d%%." % (yes, no, v["value"]))
            self.say("!setallaibonus %d" % v["value"])
        elif no * 2 >= n or self.now() >= v["until"]:
            self.aibonus_vote = None
            log("the AI bonus vote failed (%d yes, %d no)" % (yes, no))
            self.say("The AI bonus vote failed (%d yes, %d no)." % (yes, no))

    def trusted(self, name: str) -> bool:
        """An authorised or auto-boss user (any case)."""
        return name.lower() in {u.lower() for u in self.s.follow | self.s.auto_boss}

    def protected_from(self, command: str, caller: str = "") -> bool:
        """A vote to end: from anyone else, any `boss` or `unboss` vote (a
        player who won boss by vote could unboss everyone else and take the
        room); from a trusted user, one to unboss this client, everyone or a
        trusted user. Also, from anyone but a trusted user, a vote that would
        change the start boxes this client set (is_start_box_vote). A boss's
        direct command (`!boss <name>`, `!addbox ...`) is not a vote and is
        left alone."""
        w = command.split()
        if not w:
            return False
        if is_start_box_vote(w):
            return not self.trusted(caller)     # the boxes this client set
        if w[0].lower() not in ("boss", "unboss"):
            return False
        if not self.trusted(caller):
            return True
        return w[0].lower() == "unboss" and (len(w) == 1 or w[1].lower() == self.s.login.lower()
                                             or self.trusted(w[1]))

    def boss_authorised(self, names):
        """Room mode, once boss: give boss to the authorised and auto-boss
        users in `names`."""
        if self.s.mode != "room" or not self.boss:
            return
        for n in sorted(set(names) & (self.s.follow | self.s.auto_boss)):
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
        # A headless host never plays a game: it took a player seat to be made
        # boss (as Chobby, which forces a player seat "so he can boss self",
        # battle_list_window.lua HostBattle). Settings and maps are a boss's,
        # not a player's; only adding bots needs the seat, briefly (sync_bots).
        self.set_seat(False, "boss; a headless host spectates")
        if self.s.room_name:
            self.title_sent = self.room_title()
            self.say("!rename " + self.title_sent)
        for c in self.s.setup:
            self.say(c)
        self.defaults_due = None    # set up just now: nothing to undo yet
        self.idle_since = None
        self.boss_authorised(b.users)
        self.sync_bots()
        if self.s.balance:
            self.say("!balance")        # after the bots: their team ids
            self.balance_after = self.now() + BALANCE_GAP
        self.balanced_full = False
        if b.passworded:
            for u in sorted(self.s.follow):
                self.tell(u, "Private room %s is up on %s; password %s." % (b.id, b.founder, self.room_password_now))
        self.state = "waiting"
        self.promote_known = 0.0    # a new room: its autohost's cooldown is unknown
        self.promote_soon("the room is set up")
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
        if self.playing():                  # its own seat (boosting) is taken too
            used.add(unpack_battle_status(self.my_status)["team"])
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
                if SETALL_RE.match(c):
                    c = "!setallaibonus %d" % self.current_bonus()
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
        if self.battle and user == self.battle.founder:
            if not self.promote_answer(msg):
                log("%s says: %s" % (user, msg[:160]))    # the autohost answers PM commands by PM
            return
        if user not in self.s.follow:
            return
        w = msg.split()
        cmd = w[0].lower() if w else ""
        if cmd == "!bots" and len(w) >= 2 and w[1].isdigit() and 0 <= int(w[1]) <= 16 \
                and len(w) >= 3 and self.s.ai_channels:
            if w[2].lower() not in self.s.ai_channels:   # check first: change nothing
                self.tell(user, "No AI named %s. %s" % (w[2], self.ai_summary()))
                return
            self.want_bots = int(w[1])
            if self.battle is not None and self.state != "ingame":
                self.sync_bots()
            self.choose_ai(user, w[2].lower(), "%d bots, " % self.want_bots)
        elif cmd == "!bots" and len(w) >= 2 and w[1].isdigit() and 0 <= int(w[1]) <= 16:
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
            games = self.recent_games(GAMES_KEPT)
            if len(w) >= 2 and not w[1].isdigit():
                self.tell(user, "PM me !perf for the game now (or the last one), or !perf <number> from !games.")
                return
            if len(w) >= 2:
                n = int(w[1])
                pick = [g for g in games if g["number"] == n]
            else:
                pick = games[:1]
            if not pick:
                self.tell(user, "No game %s. PM me !games for the list." % (w[1] if len(w) >= 2 else "measured yet"))
                return
            for line in self.perf_report(pick[0]):
                self.tell(user, line)
        elif cmd == "!games":
            count = min(int(w[1]), GAMES_KEPT) if len(w) >= 2 and w[1].isdigit() and int(w[1]) > 0 else 5
            games = self.recent_games(count)
            if not games:
                self.tell(user, "No games yet.")
                return
            for g in games:
                self.tell(user, self.game_line(g))
            self.tell(user, "Details: !perf <number>.")
        elif cmd == "!help":
            for line in self.help_lines():
                self.tell(user, line)
        elif cmd == "!rehost" and self.s.mode == "room":
            if self.rehost_due is not None:
                self.tell(user, "Already rehosting.")
            elif self.game_running() and hasattr(self.game_proc, "terminate"):
                self.rehost_due = user
                if self.resign_kill_at is None:
                    log("rehosting at %s's request: resigning the bots first" % user)
                    self.game_proc.terminate()
                    self.resign_kill_at = self.now() + RESIGN_KILL_AFTER
                    self.say(REHOST_GAME_SAY % user)
                self.tell(user, "Ending the game (resigning the bots), then moving to a new room.")
            elif self.battle is None:
                self.tell(user, "I am not in a room; I am already looking for one.")
            else:
                self.rehost_due = user
                self.say(REHOST_SAY % user)
                self.finish_rehost()
                self.tell(user, "Left room; taking a new one.")
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
        elif cmd == "!profiler":
            arg = w[1].lower() if len(w) >= 2 else ""
            if arg not in ("", "on", "off", "default"):
                self.tell(user, "PM me !profiler on, !profiler off or !profiler default.")
                return
            if arg:
                self.profiler_choice = None if arg == "default" else arg == "on"
                self.s.engine_profiler = self.profiler_default if arg == "default" else self.profiler_choice
                self.save_state()
                log("engine profiler %s at %s's request" % ("on" if self.s.engine_profiler else "off", user))
            why = "the default" if self.profiler_choice is None else "chosen by PM, kept across restarts"
            when = (" from the next game" if arg and self.game_running() else "")
            self.tell(user, "Engine profiler: %s%s (%s)." % ("on" if self.s.engine_profiler else "off", when, why))
        elif cmd == "!widgets":
            arg = w[1].lower() if len(w) >= 2 else ""
            if arg not in ("", "on", "off", "default"):
                self.tell(user, "PM me !widgets on, !widgets off or !widgets default.")
                return
            if arg:
                self.widgets_choice = None if arg == "default" else arg == "on"
                self.s.bar_widgets = self.widgets_default if arg == "default" else self.widgets_choice
                self.save_state()
                log("BAR's widgets %s at %s's request" % ("on" if self.s.bar_widgets else "off", user))
            why = "the default" if self.widgets_choice is None else "chosen by PM, kept across restarts"
            when = (" from the next game" if arg and self.game_running() else "")
            what = "on" if self.s.bar_widgets else "off (only my metrics widget)"
            self.tell(user, "BAR's widgets: %s%s (%s)." % (what, when, why))
        elif cmd == "!say":
            text = msg[len(w[0]):].strip() if w else ""
            if not text:
                self.tell(user, "PM me !say <text>: I say it in my room.")
            elif self.battle is None:
                self.tell(user, "I am not in a room.")
            else:
                log("saying for %s: %s" % (user, text[:160]))
                self.say(text)
                self.tell(user, "Said in room %s." % self.battle.id)
        elif cmd == "!ai" and self.s.ai_channels:
            if len(w) < 2:
                self.tell(user, self.ai_summary())
            else:
                self.choose_ai(user, w[1].lower(), version=w[2] if len(w) >= 3 else "")
        elif cmd in ("!public", "!private") and self.s.mode == "room":
            self.privacy = cmd[1:]
            self.save_state()
            b = self.battle
            if b is None or b.passworded == (self.privacy == "private"):
                self.tell(user, "OK: I host a %s room%s." % (self.privacy, " (as now)" if b else ""))
            elif self.state == "ingame" or self.game_running():
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
                self.state, where, len(self.my_bots()), self.want_bots,
                self.ai_label(self.ai_channel) if self.s.ai_channels else self.bot_ai, self.games,
                ("; " + self.last_error) if self.last_error else ""))
        else:
            self.tell(user, "Unknown command%s. PM me !help for the list." % ((" " + w[0]) if w else ""))

    def help_lines(self) -> "list[str]":
        """!help: every command, a few PM lines."""
        if self.s.ai_channels:
            ai = ("!ai [name] [version]: list the AIs, or pick one for the next games; a version (a release "
                  "tag, such as v1.1150-test) holds it to that release, !ai <name> follows the newest again. "
                  "!bots <0-16> [AI name]: how many bots, and optionally which AI.")
        else:
            ai = "!bots <0-16> [AI]: how many bots, and optionally which AI."
        lines = ["Commands, by PM: !status: my room, bots, AI and games played. !games [count]: recent games, "
                 "newest first (5; up to %d)." % GAMES_KEPT,
                 "!perf [number]: how the game now (or the last one, or game <number> from !games) ran: how far "
                 "behind the server I was, sim speed, CPU, memory, pressure.",
                 ai,
                 "!profiler [on|off|default]: the engine profiler (the AIs' share of the sim time); !widgets "
                 "[on|off|default]: BAR's own widgets in my engine (off: only my metrics). Both from the next "
                 "game, kept across restarts. !say <text>: I say it in my room (a !command too, as boss)."]
        if self.s.mode == "room":
            lines.append("!public / !private: the kind of room I host (I PM you a private room's password). "
                         "!stop: in a game, resign the bots (ends it). !rehost: end any game, leave this room "
                         "and host a new one. !help: this list.")
        else:
            lines.append("!room <room number, host, player or title> [password], !join [password], !leave: "
                         "which room I follow you into. !stop: in a game, resign the bots. !help: this list.")
        return lines

    # -- games and their performance ---------------------------------------

    def recent_games(self, limit: int) -> "list[dict]":
        """Newest first, numbered: the game running (0), games ended and not
        yet in the archive, and the archive's own (its index)."""
        out = []
        if self.current_game is not None:
            out.append(dict(self.current_game, kind="live", number=0))
        archived = []
        if self.s.archive_dir is not None:
            try:
                lines = game_archive.index_path(self.s.archive_dir, self.s.archive_source).read_text(
                    encoding="utf-8").splitlines()[-500:]
            except OSError:
                lines = []
            for ln in lines:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("folder"):
                    archived.append(dict(e, kind="archived"))
        names = {a.get("run") for a in archived}
        ended = [dict(g, kind="ended") for g in self.finished_games
                 if g["run"] is None or g["run"].name not in names]
        rest = sorted(ended + archived, key=lambda g: g.get("startedUtc") or "", reverse=True)
        for i, g in enumerate(rest, 1):
            g["number"] = i
        return (out + rest)[:limit]

    def game_line(self, g: dict) -> str:
        """One line of !games."""
        when = (g.get("startedUtc") or "")[5:16].replace("T", " ") + "Z"
        what = (g.get("map") or "?")
        if g["kind"] == "live":
            lag = game_archive.summarise_timeline(game_archive.engine_timeline(g["run"]), ("behind_s",)).get(
                "behind_s") if g["run"] else None
            mins = (self.now() - g["since"]) / 60
            return "0. NOW %s %s, %d min in%s" % (when, what, mins, (
                "; behind %.1f s now, max %.1f s" % (lag["now"], lag["max"])) if lag else "")
        if g["kind"] == "ended":
            perf = g.get("perf") or {}
            return "%d. %s %s, %d min%s%s" % (
                g["number"], when, what, (perf.get("seconds") or 0) / 60,
                ", being archived" if g["run"] is not None and self.s.archive_dir is not None else "", (
                "; cores p95 %.1f" % perf["cores_p95"]) if perf.get("cores_p95") is not None else "")
        secs = g.get("gameSeconds")
        if not secs:
            try:
                secs = (dt.datetime.fromisoformat(g["endedUtc"].replace("Z", "+00:00"))
                        - dt.datetime.fromisoformat(g["startedUtc"].replace("Z", "+00:00"))).total_seconds()
            except (KeyError, TypeError, ValueError, AttributeError):
                secs = 0
        bits = []
        if g.get("lagMaxS") is not None:
            bits.append("lag max %.1f s" % g["lagMaxS"])
        if g.get("coresP95") is not None:
            bits.append("cores p95 %.1f" % g["coresP95"])
        return "%d. %s %s, %d min, %s, %s humans%s" % (
            g["number"], when, what, secs / 60, g.get("outcome", "?"), g.get("humans", "?"),
            ("; " + "; ".join(bits)) if bits else "")

    def perf_report(self, g: dict) -> "list[str]":
        """!perf: a game's lag, sim speed, CPU, memory and pressure."""
        if g["kind"] == "archived":
            try:
                m = json.loads((self.s.archive_dir / g["folder"] / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return ["Game %d (%s): cannot read its manifest: %s" % (g["number"], g.get("folder"), e)]
            lag, load, perf = m.get("lag") or {}, m.get("load") or {}, m.get("perf")
            ai = m.get("ai") or {}
            head = "Game %d: %s, %s, %d min, %s%s, %d humans, %d bots%s." % (
                g["number"], (m.get("startedUtc") or "")[:16].replace("T", " ") + "Z", m.get("map"),
                (m.get("gameSeconds") or (perf or {}).get("seconds") or 0) / 60, m.get("outcome"),
                (" (ally %s)" % ",".join(str(a) for a in m["winningAllyTeams"])) if m.get("winningAllyTeams") else "",
                len(m.get("humans") or []), m.get("bots") or 0,
                (", %s %s" % (ai.get("name"), ai.get("release") or "")).rstrip() if ai.get("name") else "")
            tail = profile_lines(m.get("profile") or {}) + ["Files: %s" % g["folder"]]
        else:
            run = g["run"]
            lag = game_archive.summarise_timeline(game_archive.engine_timeline(run), game_archive.LAG_COLUMNS) if run else {}
            load = game_archive.summarise_timeline(run / "timeline.csv", game_archive.LOAD_COLUMNS) if run else {}
            perf = g.get("perf")
            live = g["kind"] == "live"
            head = "Game %d%s: %s, started %s, %d min%s." % (
                g["number"], " (now)" if live else "", g.get("map"),
                (g.get("startedUtc") or "")[11:16] + "Z",
                ((self.now() - g["since"]) if live else ((perf or {}).get("seconds") or 0)) / 60,
                " in" if live else (", being archived" if run is not None and self.s.archive_dir else ""))
            tail = []
        return [head] + perf_lines(lag, load, perf, now=g["kind"] == "live") + tail

    # -- the bots' AI ------------------------------------------------------

    def ai_label(self, channel) -> str:
        """smrt-test (SMRTBARb v1.1151-test), barb (BARb, the game's own)."""
        ai, source = self.s.ai_channels[channel]
        if not source:
            return "%s (%s, the game's own)" % (channel, ai)
        pin = self.ai_pins.get(channel)
        got = self.ai_releases.get(channel)
        if pin and got != pin:
            return "%s (%s %s, pinned to %s, not downloaded yet)" % (channel, ai, got or "none", pin)
        return "%s (%s %s%s)" % (channel, ai, got or "not downloaded yet", ", pinned" if pin else "")

    def channel_sources(self) -> dict:
        """BARHOST_AI_CHANNELS with each pinned channel's tag in its source."""
        return {n: (ai, install_ai.pin_source(src, self.ai_pins[n]) if src and n in self.ai_pins else src)
                for n, (ai, src) in self.s.ai_channels.items()}

    def ai_summary(self) -> str:
        return "AI now: %s. Choose with !ai <name> [version]: %s." % (
            self.ai_label(self.ai_channel), ", ".join(self.ai_label(c) for c in self.s.ai_channels))

    def choose_ai(self, user, channel, prefix="", version=""):
        """!ai <channel> [version]: kept across restarts; the engine and the
        bots follow between games. A version pins a release channel to that
        release tag; none (or latest / newest) has it follow its newest."""
        if channel not in self.s.ai_channels:
            self.tell(user, "No AI named %s. %s" % (channel, self.ai_summary()))
            return
        source = self.s.ai_channels[channel][1]
        if version.lower() in ("latest", "newest"):
            version = ""
        if version and not source:
            self.tell(user, "%s is the game's own AI: it has no versions." % channel)
            return
        if version and not install_ai.TAG_RE.match(version):
            self.tell(user, "%s is not a release tag (such as v1.1150-test)." % version)
            return
        if source and self.ai_pins.get(channel, "") != version:
            if version:
                self.ai_pins[channel] = version
                self.pin_asked[channel] = user
            else:
                self.ai_pins.pop(channel, None)
                self.pin_asked.pop(channel, None)
            self.ai_channel = channel
            self.save_state()
            self.ai_activate_due = True
            # Fetched between games: the game-over check, now, or right
            # after the check under way (the download never touches the
            # engine; the switch is at activation).
            if self.game_running() or self.state == "ingame":
                when = "after this game"
            elif not self.ai_updating:
                self.need_ai_update = self.ai_updating = True
                when = "once it is downloaded"
            else:
                self.next_ai_check = self.now()
                when = "once the AI check under way ends"
            what = ("%s %s, pinned" % (channel, version)) if version else \
                "%s, following its newest release" % channel
            self.tell(user, "OK: %s%s, %s." % (prefix, what, when))
            log("%s %s %s" % (user, "pinned" if version else "unpinned", what))
            return
        if channel == self.ai_channel and not self.ai_activate_due:
            self.tell(user, "OK: %sthe bots already run %s." % (prefix, self.ai_label(channel)))
            return
        self.ai_channel = channel
        self.save_state()
        self.ai_activate_due = True
        source = self.s.ai_channels[channel][1]
        if self.game_running() or self.state == "ingame":
            when = "after this game"
        elif source and not self.ai_releases.get(channel):
            when = "once it is downloaded"
            if not self.ai_updating:
                self.need_ai_update = self.ai_updating = True
        elif self.ai_updating:
            when = "once the AI check under way ends"
        else:
            self.apply_ai()
            when = "from the next game"
        self.tell(user, "OK: %s%s %s." % (prefix, self.ai_label(channel), when))

    def apply_ai(self):
        """Between games: point the engine at the chosen channel's release,
        and have the bots replaced (ai_switch_due) if its AI is another one.
        Said in the room when the AI for the next game changes."""
        self.ai_activate_due = False
        channel = self.ai_channel
        ai = self.s.ai_channels[channel][0]
        try:
            tag = self.activate(channel)
        except Exception as e:  # the engine keeps what it had
            self.last_error = "AI %s: %s" % (channel, e)
            log("could not switch the engine to %s: %s" % (channel, e))
            return
        if ai != self.bot_ai:
            self.bot_ai = ai
            self.save_state()
            self.ai_switch_due = bool(self.battle)
        if (channel, tag) != self.ai_active:
            first = self.ai_active is None
            self.ai_active = (channel, tag)
            log("the AI for the next game: %s" % self.ai_label(channel))
            if self.battle and not first:
                self.say("Reloading the AI for the next game: %s." % (
                    ("%s version %s" % (ai, tag)) if tag else "%s (the game's own)" % ai))

    def welcome_text(self) -> str:
        """BARHOST_WELCOME, plus the AI release in use if it is one."""
        if self.ai_active and self.ai_active[1]:
            return "%s Current AI: %s version %s." % (self.s.welcome, self.bot_ai, self.ai_active[1])
        return self.s.welcome

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
        self.end_room_chat(why)
        self.battle = None
        self.boss = False
        self.statuses = {}
        self.my_status = None
        self.pending = []
        self.need_fetch = None
        self.synced = False
        self.boss_deadline = None
        self.target_room = ""       # !room names one room once; it is not rejoined
        self.start_boxes_map = None  # boxes are sent again in the next room
        self.voted_bonus = None     # a voted bonus belongs to that room
        self.room_bonus = None      # so does the bonus its AIs were seen with
        self.title_sent = None
        self.idle_since = None
        self.defaults_due = None
        self.defaults_at = None
        self.aibonus_vote = None
        self.aibonus_remind = False
        self.bots_due = None
        if self.state != "ingame":
            self.state = "lobby"
        self.next_join = self.now() + 10

    def ai_updated(self, changes, errors, releases=None):
        """The I/O layer's result of install_ai.update_channels() (with
        channels: `releases` is {channel: tag}) or install_ai.update()."""
        self.ai_updating = False
        if not self.s.ai_channels:
            for c in changes:
                log("AI release updated: " + c)
                for u in sorted(self.s.follow):
                    self.tell(u, "AI release updated (%s); the next game uses it." % c.rsplit(": ", 1)[-1])
        else:
            self.ai_releases.update({k: v for k, v in (releases or {}).items() if v})
            for c in changes:
                log("AI release downloaded: " + c)
                channel = c.split(":", 1)[0]
                use = "the next game uses it" if channel == self.ai_channel else "PM me !ai %s to use it" % channel
                what = "Pinned AI release in place" if channel in self.ai_pins else "New AI release"
                for u in sorted(self.s.follow):
                    self.tell(u, "%s: %s; %s." % (what, c, use))
            self.ai_activate_due = True     # the engine follows between games (tick)
        for e in errors:
            log("AI update check failed: " + e)
            channel = e.split(":", 1)[0]
            if channel in self.pin_asked and self.ai_pins.get(channel):
                # Only right after the pin: a later failure is a network
                # blip, and the release already downloaded stays in use.
                tag = self.ai_pins.pop(channel)
                who = self.pin_asked.pop(channel)
                self.save_state()
                self.next_ai_check = self.now()
                self.tell(who, "Could not get %s %s (%s); it follows its newest release again."
                          % (channel, tag, e.split(":", 1)[-1].strip()[:150]))
        for channel, tag in (releases or {}).items():
            if self.pin_asked.get(channel) and self.ai_pins.get(channel) == tag:
                self.pin_asked.pop(channel)

    def ready_to_start(self) -> bool:
        if not self.synced or self.map_wanted or self.ai_updating:   # no start mid change
            return False
        humans = self.humans()
        if self.s.min_humans <= 0 or len(humans) < self.s.min_humans:
            return False
        if self.s.matched_teams and not self.teams_matched():
            return False                    # the human team is not yet as big as the AI team
        return all(self.statuses[h].get("ready") and self.statuses[h].get("sync") == 1 for h in humans)

    def teams_matched(self) -> bool:
        """At least one human plays and at least as many as the bots."""
        humans = self.humans()
        return bool(humans) and len(humans) >= len(self.battle.bots)

    def ring_due(self) -> bool:
        """Room mode: the teams are matched but someone is not ready, for
        ring_first seconds since then, or ring_every since the last ring."""
        now = self.now()
        if self.s.ring_every <= 0 or not self.synced or self.map_wanted or self.ai_updating \
                or not self.teams_matched() \
                or all(self.statuses[h].get("ready") for h in self.humans()):
            self.ring_after = None
            return False
        if self.ring_after is None:
            self.ring_after = now + self.s.ring_first
        return now >= self.ring_after

    def game_started(self):
        b = self.battle
        if self.game_running():
            # The last game's engine never quit: stop it, record that game,
            # then launch. Two engines would share the pod's CPU and memory.
            log("my engine from the last game is still running; stopping it first")
            self.stop_engine(wait=10)
            self.log_engine_exit()
        self.state = "ingame"
        self.games += 1
        log("game %d starting on %s:%s" % (self.games, b.ip, b.port))
        self.room_chat("* game starting on %s" % b.map)
        self.game_proc = self.launch(b, self.script_password)
        self.game_launched_at = self.now()
        self.engine_exit_logged = False
        self.resign_kill_at = None
        self.engine_quit_at = None
        self.frozen = None          # (frame, timeline row, first seen) of the newest frame seen
        self.game_humans = set(self.humans())   # who must resign or leave before a frozen game is given up
        self.next_frozen_check = self.now() + FROZEN_CHECK_EVERY
        run_dir = getattr(self.game_proc, "write_dir", None)
        if self.perf is not None:
            self.perf.start("game %d %s" % (self.games, b.map), run_dir, now=self.now())
        if run_dir is not None:
            self.record_game_start(Path(run_dir))
        else:
            self.current_game = {"run": None, "map": b.map, "since": self.now(),
                                 "startedUtc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}

    def lobby_snapshot(self) -> dict:
        """The room as the game starts, for the archive (lobby.json): what the
        replay does not hold, or holds only as the engine saw it."""
        b = self.battle
        players = []
        for n in sorted(b.users):
            st = self.statuses.get(n, {})
            status = self.users.get(n, 0)
            players.append({"name": n, "accountId": self.account_ids.get(n), "player": st.get("mode") == 1,
                            "team": st.get("team"), "ally": st.get("ally"), "side": st.get("side"),
                            "ready": st.get("ready"), "sync": st.get("sync"), "bonus": st.get("bonus"),
                            "rank": (status >> 2) & 7, "bot": bool(status & 64), "founder": n == b.founder,
                            "self": n == self.s.login})
        bots = [{"name": n, "owner": x["owner"], "ai": x.get("ai"), "mine": x["owner"] == self.s.login,
                 **{k: (x.get("status") or {}).get(k) for k in ("team", "ally", "side", "bonus")}}
                for n, x in sorted(b.bots.items())]
        return {
            "schema": "rjm-bar-archive/lobby/1",
            "startedUtc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "client": VERSION, "login": self.s.login, "mode": self.s.mode, "pod": os.environ.get("HOSTNAME"),
            "cpus": usable_cpus(),
            "game_number": self.games, "battle": b.id, "founder": b.founder, "title": b.title, "map": b.map,
            "game": b.game, "engine": b.engine_version, "passworded": b.passworded, "maxPlayers": b.max_players,
            "privacy": self.privacy if self.s.mode == "room" else None,
            "scriptTags": dict(sorted(b.script_tags.items())),
            "players": players, "bots": bots,
            "ai": {"channel": self.ai_channel or None, "name": self.bot_ai,
                   "release": self.ai_active[1] if self.ai_active else None},
            "setup": {"room": self.s.setup, "bots": self.s.bot_setup, "votedBonus": self.voted_bonus,
                      "startBoxesMap": self.start_boxes_map, "wantBots": self.want_bots},
        }

    def record_game_start(self, run_dir: Path):
        """game.json (lobby.json in the archive), and this game's log files."""
        snap = self.lobby_snapshot()
        self.current_game = {"run": run_dir, "startedUtc": snap["startedUtc"], "map": snap["map"],
                             "since": self.now()}
        try:
            (run_dir / "game.json").write_text(json.dumps(snap, indent=2), encoding="utf-8")
            GAME_LOG["file"] = (run_dir / "client.log").open("a", encoding="utf-8", buffering=1)
            self.game_events = (run_dir / "lobby-events.log").open("a", encoding="utf-8", buffering=1)
        except OSError as e:
            log("could not write the game's lobby data in %s: %s" % (run_dir, e))
        pid = getattr(getattr(self.game_proc, "proc", None), "pid", None)
        if self.metrics is not None and pid:
            self.metrics.start(run_dir, pid, self.now())

    def record_game_end(self, code, summary):
        """ended.json, close this game's files, and hand the run dir over to
        the archive (once per game)."""
        run_dir = getattr(self.game_proc, "write_dir", None)
        self.room_chat("* game over (my engine exited)")
        if self.metrics is not None:
            self.metrics.stop()
        for f in (GAME_LOG["file"], self.game_events):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        GAME_LOG["file"] = self.game_events = None
        if self.current_game is not None:
            self.finished_games = (self.finished_games + [dict(self.current_game, perf=summary)])[-GAMES_KEPT:]
            self.current_game = None
        if run_dir is None:
            return
        try:
            (Path(run_dir) / "ended.json").write_text(json.dumps({
                "endedUtc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "engineExit": code, "resigned": self.resign_kill_at is not None,
                "ranSeconds": round(self.now() - getattr(self, "game_launched_at", self.now()), 1),
                "perf": summary,
            }, indent=2), encoding="utf-8")
        except (OSError, TypeError) as e:
            log("could not write ended.json in %s: %s" % (run_dir, e))
        learn_widget_names(self.s, Path(run_dir))
        self.archive(Path(run_dir))

    def finish_rehost(self):
        """!rehost, once no engine of ours is in a game: forget the game, leave
        the room (it is not rejoined: join_attempted) and look for another
        at once, with the next map of the rotation if a game was played."""
        user, self.rehost_due = self.rehost_due, None
        played = self.game_proc is not None
        if self.game_proc is not None and hasattr(self.game_proc, "close"):
            self.game_proc.close()
        self.game_proc = None
        self.resign_kill_at = None
        if self.battle is not None:
            self.state = "waiting"
            self.leave_battle("rehosting for %s" % user)
        self.state = "lobby"
        self.next_join = self.now() + 3
        if played and self.s.map_rotation:
            self.map_skips = 0
            self.advance_map()
        if played and user != FROZEN:
            self.tell(user, "Game over for my bots; left the room, taking a new one.")

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

    def stop_engine(self, wait=0):
        """SIGTERM the engine (it leaves the game), SIGKILL if it is still
        there after `wait` seconds (0: leave that to tick())."""
        p = self.game_proc
        if p is None or p.poll() is not None or not hasattr(p, "terminate"):
            return
        p.terminate()
        proc = getattr(p, "proc", None)
        if wait and proc is not None:
            try:
                proc.wait(wait)
            except Exception:       # subprocess.TimeoutExpired
                p.kill()
                try:
                    proc.wait(5)
                except Exception:
                    pass

    def game_frame_now(self):
        """(game frame, row stamp) of the metrics widget's newest timeline
        row in the running game, or None."""
        run = getattr(self.game_proc, "write_dir", None)
        if run is None:
            return None
        try:
            with game_archive.engine_timeline(Path(run)).open("rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 4096))
                rows = f.read().decode(errors="replace").splitlines()
        except (OSError, TypeError):
            return None
        for row in reversed(rows):
            x = row.split(",")
            if len(x) >= 3 and x[2].isdigit():
                return int(x[2]), x[1]
        return None

    def game_frozen(self, now):
        """True once the game's frame has stood still for frozen_game_after
        while the widget kept writing rows (the engine runs, the game does
        not: paused with every player resigned never reaches game over) and
        no human who started is still in it, or for FROZEN_ANYWAY times that.
        A paused game with players in it is theirs to unpause; a lagging
        engine's frame still moves; a silent widget or a game not started yet
        (frame 0) is no evidence."""
        if self.s.frozen_game_after <= 0 or now < getattr(self, "next_frozen_check", 0):
            return False
        self.next_frozen_check = now + FROZEN_CHECK_EVERY
        got = self.game_frame_now()
        if got is None or got[0] == 0:
            return False
        frame, row = got
        seen = getattr(self, "frozen", None)
        if seen is None or seen[0] != frame:
            self.frozen = (frame, row, now)
            return False
        if row == seen[1]:
            return False
        still = now - seen[2]
        return still >= FROZEN_ANYWAY * self.s.frozen_game_after or             (still >= self.s.frozen_game_after and not self.humans_in_game())

    def humans_in_game(self) -> set:
        """The humans who started this game and, by the engine's console, have
        neither resigned nor left it."""
        left = set(getattr(self, "game_humans", ()))
        run = getattr(self.game_proc, "write_dir", None)
        if not left or run is None:
            return left
        try:
            console = (Path(run) / "engine-console.log").read_bytes()
        except (OSError, TypeError):
            return left
        return left - {m.group(1).decode(errors="replace") for m in PLAYER_GONE_RE.finditer(console)}

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
        summary = None
        if self.perf is not None:
            summary = self.perf.stop()
            if summary:
                log("perf " + cgroup_perf.describe(summary))
        if code != 0 or still:
            console = getattr(self.game_proc, "write_dir", None)
            try:
                lines = (console / "engine-console.log").read_text(errors="replace").splitlines()[-15:]
            except (OSError, TypeError):
                lines = []
            for line in lines:
                log("  engine: " + line)
        self.record_game_end(code, summary)

    # -- time --------------------------------------------------------------

    def tick(self):
        now = self.now()
        if self.next_friend_sweep is not None and now >= self.next_friend_sweep \
                and self.state not in ("connecting", "login"):
            self.friend_sweep()
        if self.state == "ingame":
            # Replies to private messages still go out during a game.
            if self.game_proc is not None and self.game_proc.poll() is None:
                if self.resign_kill_at is not None and now >= self.resign_kill_at and hasattr(self.game_proc, "kill"):
                    log("the engine did not quit; killing it")
                    self.game_proc.kill()
                    self.resign_kill_at = now + RESIGN_KILL_AFTER
                elif self.engine_quit_at is not None and now >= self.engine_quit_at and self.resign_kill_at is None                         and not (self.battle and self.users.get(self.battle.founder, 0) & 1):
                    log("the game ended %d s ago and my engine is still running; stopping it" % ENGINE_QUIT_AFTER)
                    self.engine_quit_at = None
                    self.stop_engine()
                    self.resign_kill_at = now + RESIGN_KILL_AFTER   # killed then if still running
                elif self.resign_kill_at is None and self.rehost_due is None and self.game_frozen(now):
                    mins = (now - self.frozen[2]) / 60
                    log("the game has stood still at frame %d for %d+ min (players still in it: %s); "
                        "resigning the bots and rehosting"
                        % (self.frozen[0], mins, ", ".join(sorted(self.humans_in_game())) or "none"))
                    self.rehost_due = FROZEN
                    self.say(FROZEN_SAY % mins)
                    self.stop_engine()
                    self.resign_kill_at = now + RESIGN_KILL_AFTER
                self.flush_pending()
                return
            self.log_engine_exit()
            if self.rehost_due is not None:
                self.finish_rehost()    # !rehost: the engine has left; leave the room too
                self.flush_pending()
                return
            if self.battle and self.users.get(self.battle.founder, 0) & 1:
                self.flush_pending()
                return     # our engine ended but the game goes on: wait it out
            if self.game_proc is not None and hasattr(self.game_proc, "close"):
                self.game_proc.close()
            self.game_proc = None
            self.state = "waiting" if self.battle else "lobby"
            self.quiet_until = now + self.s.postgame_delay
            if self.battle:
                self.promote_soon("the game is over")
                self.aibonus_remind = self.s.mode == "room" and self.s.bonus_vote
            if self.s.mode == "follow" and self.battle:
                self.sync_bots()        # a !bots sent during the game
            self.next_ai_check = now    # look for a new AI release before the next game
            if self.s.mode == "room" and self.battle and self.boss and self.s.map_rotation:
                self.map_skips = 0      # one map per game
                self.advance_map()
                self.rotate_map()
        if self.map_wanted and self.map_deadline is not None and now >= self.map_deadline \
                and self.battle and self.state == "waiting":
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
        # The engine is pointed at the chosen channel's release only between
        # games (it loads the AI when a game starts).
        if self.ai_activate_due and not self.ai_updating and not self.game_running() and self.state != "ingame":
            self.apply_ai()
        if self.ai_switch_due and self.battle and self.state == "waiting" and not self.game_running() \
                and (self.boss or self.s.mode == "follow"):
            self.switch_bots()
        # Room mode: the room kind follows !public / !private; rehosting is
        # only ever done from a room with no game in progress.
        if self.s.mode == "room" and self.battle and self.state == "waiting" and self.boss \
                and not self.game_running() and not (self.users.get(self.battle.founder, 0) & 1):
            if self.battle.passworded != (self.privacy == "private") and now >= self.start_hold_until:
                self.leave_battle("switching to a %s room" % self.privacy)
            elif self.battle.passworded == (self.privacy == "private") and not self.pending:
                # The seat outside a bot add: a spectator, unless boosting.
                want = self.boost_seat()
                if want is True and not self.playing():
                    self.set_seat(True, "boosting the room (%d playing)" % len(self.humans()))
                elif want is False and self.playing():
                    self.set_seat(False, "a headless host spectates")
        # A full team is balanced, and its unready players rung, also in the
        # post-game delay.
        if self.s.mode == "room" and self.state == "waiting" and self.boss:
            full = self.synced and bool(self.humans()) and not self.players_wanted()
            if not full:
                self.balanced_full = False
            elif self.s.balance and not self.balanced_full and now >= self.balance_after:
                self.balanced_full = True
                self.balance_after = now + BALANCE_GAP
                log("balancing: the player team is full (%d playing, %d bots)"
                    % (len(self.humans()), len(self.battle.bots)))
                self.say("!balance")
            if self.ring_due():             # the teams are matched: ring whoever is not ready
                self.ring_after = now + self.s.ring_every
                log("ringing the unready players (%d playing, %d bots)" % (len(self.humans()), len(self.battle.bots)))
                self.say("!ring")
        # The players start the game by vote; the client only says how.
        if self.s.mode == "room" and self.state == "waiting" and self.boss and now >= self.quiet_until:
            if not self.ready_to_start():
                self.start_hint_given = False
            elif not self.start_hint_given and now >= self.start_hint_after:
                self.start_hint_given = True
                self.start_hint_after = now + START_HINT_EVERY
                self.start_hold_until = now + START_HOLD
                if self.playing():          # never an idle commander in the game
                    self.set_seat(False, "everyone is ready", first=True)
                self.say(START_HINT)
        if self.s.mode == "room" and self.state == "waiting" and self.boss and self.battle                 and not self.game_running() and not (self.users.get(self.battle.founder, 0) & 1):
            # The title follows the AIs' bonus, whoever changed it.
            if self.s.room_name and now >= self.rename_after and not self.pending:
                title = self.room_title()
                if title != self.title_sent:
                    self.title_sent = title
                    log("renaming the room: %s" % title)
                    self.say("!rename " + title)
            # An idle spell (no human in the player team): back to the defaults.
            if self.humans():
                self.idle_since = None
            elif self.defaults_due:
                if self.idle_since is None:
                    self.idle_since = now
                elif now - self.idle_since >= IDLE_RESET_AFTER and (
                        self.defaults_at is None or now - self.defaults_at >= IDLE_RESET_EVERY):
                    self.restore_defaults()
        if self.aibonus_vote is not None:
            self.aibonus_count()        # the deadline, or a game started
        if self.aibonus_remind and self.state == "waiting" and self.boss and self.synced \
                and not self.map_wanted and not self.game_running():
            self.aibonus_remind = False     # after a game, once the next map is in
            self.bonus_hint_after = now + BONUS_HINT_EVERY
            self.say(self.bonus_hint())
        if self.promote_due():
            self.promote_after = now + PROMOTE_RETRY    # until the autohost answers
            log("promoting the room (%d playing)" % len(self.humans()))
            self.pending.append(("SAYPRIVATE", self.battle.founder, "!promote"))
        self.flush_pending()

    def promote_due(self) -> bool:
        """Room mode: time to !promote a public room whose player team is not
        full, with no game on or start near. By PM, so the room sees no
        command and no cooldown answers."""
        b = self.battle
        if self.s.mode != "room" or self.s.promote_every <= 0 or not b or self.state != "waiting":
            return False
        now = self.now()
        if now < self.promote_after or now < self.start_hold_until or b.passworded \
                or self.game_running() or self.users.get(b.founder, 0) & 1:
            return False
        return self.players_wanted() and not self.ready_to_start()

    def players_wanted(self) -> bool:
        """The player team wants more: fewer humans than bots (the teams not
        matched), or with no bots, a free player seat."""
        b = self.battle
        if self.team_full():
            return False
        return len(self.humans()) < len(b.bots) if b.bots else True

    def promote_soon(self, why: str):
        """Promote at the next chance (set up, a game over), but never inside
        a cooldown the autohost named."""
        if self.s.mode == "room" and self.s.promote_every > 0:
            self.promote_after = max(self.now(), self.promote_known)
            log("promote due: %s" % why)

    def promote_answer(self, msg) -> bool:
        """The autohost's answer to a !promote: set when to try next."""
        now = self.now()
        wait = PROMOTE_WAIT_RE.search(msg)
        if wait:
            secs = sum(int(n) * PROMOTE_UNITS[u] for n, u in PROMOTE_UNIT_RE.findall(wait.group(1)))
            self.promote_after = self.promote_known = now + secs + PROMOTE_MARGIN
        elif PROMOTE_DONE in msg:
            self.promote_after = self.promote_known = now + max(self.s.promote_every, 0) + PROMOTE_MARGIN
        elif PROMOTE_REFUSED_RE.search(msg):
            self.promote_after = now + PROMOTE_RETRY
        elif PROMOTE_DENIED_RE.search(msg):
            self.promote_after = now + max(self.s.promote_every, PROMOTE_RETRY)
        else:
            return False
        log("promote: %s says %s" % (self.battle.founder if self.battle else "the autohost", msg[:160]))
        return True

    def team_full(self) -> bool:
        """The room's player seats are all taken: humans, this client if it
        plays, and the bots (the room's player limit counts them)."""
        b = self.battle
        seated = len(self.humans()) + len(b.bots) + (1 if self.playing() else 0)
        return bool(b.max_players) and seated >= b.max_players

    def boost_seat(self):
        """Room mode: True to take a player seat (fewer than boost_below humans
        play: the battle list counts players), False to spectate (boost off,
        boost_until humans or more, the room full with this client in it,
        everyone ready, or a start just called), None to keep the seat."""
        b = self.battle
        humans = self.humans()
        if self.s.boost_below <= 0 or self.now() < self.start_hold_until:
            return False
        if humans and all(self.statuses[h].get("ready") for h in humans):
            return False                    # about to start: never in the game
        seated = len(humans) + len(b.bots) + 1     # with this client in a seat
        if len(humans) >= self.s.boost_until or (b.max_players and seated >= b.max_players):
            return False
        if len(humans) < self.s.boost_below:
            return True
        return None


# -- I/O --------------------------------------------------------------------

async def update_ais(c: LobbyClient):
    """Download new releases of the AI channels (or install those of
    moving-tag BARHOST_AI_ARCHIVES sources), off the lobby loop."""
    s, releases = c.s, None
    try:
        if s.ai_channels:
            changes, errors, releases = await asyncio.get_running_loop().run_in_executor(
                None, install_ai.update_channels, c.channel_sources(), s.ai_releases_dir, engine_skirmish_dir())
        else:
            changes, errors = await asyncio.get_running_loop().run_in_executor(None, install_ai.update)
    except Exception as e:  # a failed check keeps what is installed
        changes, errors = [], [str(e)]
    c.ai_updated(changes, errors, releases)


def engine_skirmish_dir() -> "Path | None":
    engine = os.environ.get("BAR_ENGINE_DIR", "").strip()
    return Path(engine) / "AI" / "Skirmish" if engine else None


def activate_channel(s: Settings):
    """The client's `activate`: point the engine at a channel's release."""
    def activate(channel):
        dest = engine_skirmish_dir()
        if dest is None:
            raise ValueError("BAR_ENGINE_DIR is not set")
        dest.mkdir(parents=True, exist_ok=True)
        return install_ai.activate_channel(s.ai_releases_dir, channel, s.ai_channels, dest)
    return activate


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


def usable_cpus() -> str:
    """The CPUs this process may run on, as ranges ("2-9,42-49"): with
    dedicated cores (a Guaranteed pod under the static CPU manager policy)
    exactly its own; otherwise every CPU of the node."""
    try:
        cpus = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return ""
    out, start = [], None
    for i, c in enumerate(cpus):
        if start is None:
            start = c
        if i + 1 == len(cpus) or cpus[i + 1] != c + 1:
            out.append(str(start) if start == c else "%d-%d" % (start, c))
            start = None
    return ",".join(out)


def _stat(d: dict, fmt: str, now: bool, low: bool = False) -> str:
    """"now 2.3, max 11.4, p95 6.2, avg 1.3" (or min, for a speed)."""
    parts = (["now " + fmt % d["now"]] if now else []) + (
        ["min " + fmt % d["min"]] if low else ["max " + fmt % d["max"], "p95 " + fmt % d["p95"]])
    return ", ".join(parts + ["avg " + fmt % d["avg"]])


def profile_lines(p: dict) -> "list[str]":
    """!perf: the engine's game-end profiler totals (game_archive.engine_profile)."""
    if not p.get("simS"):
        return []
    line = "Engine time over the game: sim %.0f s" % p["simS"]
    if p.get("aiS") is not None:
        line += ", of it the AIs %.0f s (%.0f%%)" % (p["aiS"], p.get("aiPctOfSim") or 0)
    for key, what in (("drawS", "draw"), ("luaCallinsS", "Lua call-ins"), ("profilerS", "the profiler itself")):
        if p.get(key) is not None:
            line += ", %s %.0f s" % (what, p[key])
    out = [line + "."]
    teams = p.get("aiByTeamS") or {}
    if teams:
        out.append("AI time by team: " + ", ".join("%s %.0f s" % kv for kv in teams.items()) + ".")
    return out


def perf_lines(lag: dict, load: dict, perf: "dict | None", now: bool = False) -> "list[str]":
    """!perf lines from a game's metrics summaries (game_archive.summarise_rows)
    and its cgroup summary (cgroup_perf)."""
    out = []
    b = lag.get("behind_s")
    if b:
        line = "Behind the server: %s s" % _stat(b, "%.1f", now)
        if lag.get("sim_fps"):
            line += "; sim speed %s frames/s (30 = real time)" % _stat(lag["sim_fps"], "%.0f", now, low=True)
        if lag.get("ping_s"):
            line += "; ping max %.1f s" % lag["ping_s"]["max"]
        out.append(line + ".")
    else:
        out.append("No lag data for this game (the engine's metrics widget wrote none).")
    c = load.get("cores")
    if c:
        line = "CPU: %s cores" % _stat(c, "%.1f", now)
        if load.get("main_thread_cores"):
            line += "; main (sim) thread %s (1.0 = a whole core)" % _stat(load["main_thread_cores"], "%.2f", now)
        if load.get("main_preempt_per_s"):
            line += "; pushed off its CPU p95 %.0f/s" % load["main_preempt_per_s"]["p95"]
        if load.get("throttled_pct"):
            line += "; throttled max %.1f%%" % load["throttled_pct"]["max"]
        out.append(line + ".")
    if perf:
        out.append("Whole game: " + cgroup_perf.describe(perf) + ".")
    bits = []
    if load.get("mem_gib"):
        bits.append("memory max %.1f GiB" % load["mem_gib"]["max"])
    pressure = ["%s %.1f%%" % (name, load[k]["max"]) for name, k in
                (("CPU", "cpu_pressure_pct"), ("disk", "io_pressure_pct"), ("memory", "mem_pressure_pct"))
                if load.get(k)]
    if pressure:
        bits.append("pressure max " + ", ".join(pressure))
    if load.get("write_mb_s"):
        bits.append("disk writes max %.1f MB/s" % load["write_mb_s"]["max"])
    if lag.get("units"):
        bits.append("units max %d" % lag["units"]["max"])
    if bits:
        out.append(("; ".join(bits) + ".")[0].upper() + ("; ".join(bits) + ".")[1:])
    return out


WIDGET = Path(__file__).resolve().parent / "rjm_host_metrics.lua"
WIDGET_NAME = "rjm host metrics"    # its GetInfo name
# BAR widgets kept on with BAR's widgets off: Autoquit quits a headless engine
# 12 s after game over (cmd_autoquit.lua); without it the engine never exits.
BAR_WIDGETS_KEPT = ("Autoquit",)
# BAR's widget config in the write dir (luaui/barwidgets.lua CONFIG_FILENAME:
# LuaUI/Config/<Game.gameShortName>.lua). Its `order` maps each widget's name
# to its load order; 0 = off (barwidgets.lua LoadWidget "Enabling"); a name
# missing loads if the widget is on by default. Hidden widgets and the widget
# selector load whatever it says. BAR rewrites it at the end of each game with
# every widget it found, which is where the names are learnt.
BAR_WIDGET_CONFIG = Path("LuaUI") / "Config" / "BYAR.lua"
ORDER_BLOCK = re.compile(r"\n\torder = \{\n(.*?)\n\t\},?\n", re.S)
# table.save writes a name that is a Lua identifier bare (AdvPlayersList = 43),
# any other quoted (["Widget Selector"] = 24).
ORDER_ENTRY = re.compile(r'^\t\t(?:\[("(?:[^"\\\n]|\\.)*")\]|([A-Za-z_][A-Za-z0-9_]*)) = -?\d+,?$', re.M)


def widget_names_in(text: str) -> "list[str]":
    """The widget names of a BYAR.lua, each as a Lua string literal."""
    m = ORDER_BLOCK.search(text)
    return [quoted or '"%s"' % bare for quoted, bare in ORDER_ENTRY.findall(m.group(1))] if m else []


def known_widget_names(s: Settings) -> "list[str]":
    """BAR's widget names: learnt from the last games, else the newest
    archived game's config."""
    try:
        names = json.loads(s.widget_names_file.read_text(encoding="utf-8"))
        if isinstance(names, list) and names:
            return [n for n in names if isinstance(n, str) and ORDER_ENTRY.fullmatch("\t\t[%s] = 0," % n)]
    except (OSError, ValueError):
        pass
    if s.archive_dir is None:
        return []
    import gzip
    try:
        found = sorted(s.archive_dir.glob("*/*/*/*/files/LuaUI/Config/BYAR.lua.gz"))
    except OSError:
        return []
    for f in reversed(found[-5:]):
        try:
            names = widget_names_in(gzip.decompress(f.read_bytes()).decode("utf-8", "replace"))
        except (OSError, EOFError, ValueError):
            continue
        if names:
            return names
    return []


def learn_widget_names(s: Settings, run_dir: Path) -> None:
    """After a game: add the names BAR saved in its config to the known ones."""
    try:
        names = widget_names_in((run_dir / BAR_WIDGET_CONFIG).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return
    if not names:
        return
    known = known_widget_names(s)
    merged = sorted(set(known) | set(names))
    if merged == sorted(known):
        return
    try:
        s.widget_names_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = s.widget_names_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, indent=0), encoding="utf-8")
        tmp.replace(s.widget_names_file)
    except OSError as e:
        log("could not save %s: %s" % (s.widget_names_file, e))


def bar_widgets_off_config(names: "list[str]") -> str:
    """A BYAR.lua that turns every named widget off but the metrics widget
    and BAR_WIDGETS_KEPT."""
    keep = ['"%s"' % n for n in (WIDGET_NAME,) + BAR_WIDGETS_KEPT]
    lines = ["-- written by the lobby client (BARHOST_BAR_WIDGETS=0 / !widgets off):",
             "-- every BAR widget off (order 0) but the host's metrics widget and Autoquit",
             "return {", "\tallowUserWidgets = true,", "\torder = {"]
    lines += ["\t\t[%s] = 0," % n for n in sorted(set(names) - set(keep))]
    lines += ["\t\t[%s] = %d," % (n, i + 1) for i, n in enumerate(keep)] + ["\t},", "}", ""]
    return "\n".join(lines)


def prepare_write_dir(s: Settings, d: Path) -> None:
    """Before the engine starts in a fresh write dir: the metrics widget, BAR's
    own widgets off if asked, and the engine's cache shared between games (it
    rebuilds it otherwise)."""
    if s.metrics_every > 0 and WIDGET.is_file():
        widgets = d / "LuaUI" / "Widgets"
        widgets.mkdir(parents=True, exist_ok=True)
        text = WIDGET.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^local EVERY = [0-9.]+", "local EVERY = %g" % s.metrics_every, text, count=1)
        text = re.sub(r"(?m)^local RJM_PROFILER = (true|false)",
                      "local RJM_PROFILER = %s" % ("true" if s.engine_profiler else "false"), text, count=1)
        (widgets / WIDGET.name).write_text(text, encoding="utf-8")
    if not s.bar_widgets:
        names = known_widget_names(s)
        if names:
            (d / BAR_WIDGET_CONFIG).parent.mkdir(parents=True, exist_ok=True)
            (d / BAR_WIDGET_CONFIG).write_text(bar_widgets_off_config(names), encoding="utf-8")
            log("engine: %d of BAR's widgets off" % len(names))
        else:
            log("engine: BAR's widget names are not known yet; they load this game")
    shared = s.work_dir / ".engine-cache"
    try:
        shared.mkdir(parents=True, exist_ok=True)
        (d / "cache").symlink_to(shared, target_is_directory=True)
    except OSError:
        pass


def archive_runs(s: Settings, *runs: Path, pending: bool = False):
    """Hand finished run dirs to game_archive.py: a separate process, lowest
    CPU and I/O priority, never waited for."""
    if s.archive_dir is None:
        return
    import subprocess
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "game_archive.py"),
           "--archive", str(s.archive_dir), "--source", s.archive_source]
    cmd += ["--pending", str(s.work_dir)] if pending else [str(r) for r in runs]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        log("could not start the archiver: %s" % e)


def launch_engine(s: Settings):
    def launch(b: Battle, script_password: str):
        run = "%s-%s" % (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S"), b.id)
        d = s.work_dir / run
        d.mkdir(parents=True, exist_ok=True)
        script = d / "script.txt"
        script.write_text(client_script(b.ip, b.port, s.login, script_password, source_port=0), encoding="utf-8")
        prepare_write_dir(s, d)
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
                if c.metrics is not None:
                    c.metrics.sample(last_perf)
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
    cpus = usable_cpus()
    if cpus:
        n = sum((int(b) - int(a) + 1) if "-" in r else 1 for r in cpus.split(",")
                for a, _, b in [r.partition("-")])
        log("cpus: %d usable (%s)%s" % (n, cpus, "" if n < (os.cpu_count() or 0)
                                         else "; shared with the node, not dedicated"))
    s.work_dir.mkdir(parents=True, exist_ok=True)
    if s.archive_dir is not None:
        log("archive: each game moves from %s to %s as %s" % (s.work_dir, s.archive_dir, s.archive_source))
        archive_runs(s, pending=True)       # games a restart cut short; no engine runs yet
    metrics = engine_metrics.HostSampler(every=s.metrics_every, cgroup=perf.cg) if s.metrics_every > 0 else None
    c = LobbyClient(s, launch=launch_engine(s), perf=perf, activate=activate_channel(s), metrics=metrics,
                    archive=lambda run_dir: archive_runs(s, run_dir))
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
