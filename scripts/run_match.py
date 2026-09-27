#!/usr/bin/env python3
"""Run one headless Beyond All Reason AI-vs-AI match to completion.

Generates a GameSetup script, launches ``spring-headless``, drives the match
over the engine's Autohost Protocol, and exits when the game is over.

Everything here is derived from the engine source, not from folklore:

* Script keys           ``rts/Game/GameSetup.cpp`` (LoadPlayers / LoadTeams /
                        LoadAllyTeams / LoadSkirmishAIs), ``rts/Game/
                        ClientSetup.cpp:39-77``, ``rts/Sim/Misc/TeamBase.cpp:55``,
                        ``rts/Sim/Misc/AllyTeam.cpp:23``,
                        ``rts/Game/Players/PlayerBase.cpp:31``
* Autohost events       ``rts/Net/AutohostInterface.cpp:36-130``
* Autohost commands     ``rts/Net/GameServer.cpp:861`` routes a leading ``/``
                        to ``PushAction(..., fromAutoHost=true)``; the actions
                        used here are ``setmaxspeed`` / ``setminspeed`` (:2485),
                        ``forcestart`` (:2505) and ``kill`` (:2600)
* Rapid tag in Gametype ``rts/Game/GameSetup.cpp:668``

The engine has no "quit when the game ends" setting -- there is no such CONFIG
anywhere in the tree -- so terminating the process is this supervisor's job.
Without it a Kubernetes Job hangs forever after the match has finished.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# --- Autohost Protocol event codes (AutohostInterface.cpp:36-130) -----------

SERVER_STARTED = 0
SERVER_QUIT = 1
SERVER_STARTPLAYING = 2
SERVER_GAMEOVER = 3
SERVER_MESSAGE = 4
SERVER_WARNING = 5
PLAYER_JOINED = 10
PLAYER_LEFT = 11
PLAYER_READY = 12

EVENT_NAMES = {
    SERVER_STARTED: "SERVER_STARTED",
    SERVER_QUIT: "SERVER_QUIT",
    SERVER_STARTPLAYING: "SERVER_STARTPLAYING",
    SERVER_GAMEOVER: "SERVER_GAMEOVER",
    SERVER_MESSAGE: "SERVER_MESSAGE",
    SERVER_WARNING: "SERVER_WARNING",
    PLAYER_JOINED: "PLAYER_JOINED",
    PLAYER_LEFT: "PLAYER_LEFT",
    PLAYER_READY: "PLAYER_READY",
}

# BAR's two factions, alternated across teams.
SIDES = ("Armada", "Cortex")

# TeamBase::SetDefaultColor has few distinct colours; supplying our own keeps
# an 8-team replay readable.
TEAM_COLORS = (
    "0.7 0.1 0.1", "0.1 0.3 0.8", "0.1 0.6 0.2", "0.8 0.7 0.1",
    "0.6 0.2 0.7", "0.9 0.5 0.1", "0.2 0.7 0.7", "0.8 0.4 0.6",
    "0.5 0.5 0.5", "0.4 0.2 0.1", "0.3 0.9 0.5", "0.9 0.3 0.5",
    "0.2 0.2 0.6", "0.6 0.8 0.3", "0.9 0.9 0.9", "0.3 0.1 0.3",
)


def log(msg: str) -> None:
    print("[run_match] " + msg, flush=True)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    raw = env(name)
    return int(raw) if raw else default


def env_float(name: str, default: float) -> float:
    raw = env(name)
    return float(raw) if raw else default


def parse_kv(raw: str) -> "dict[str, str]":
    """Parse ``a=1,b=2`` into a dict. An empty string yields an empty dict."""
    out = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise SystemExit("expected key=value, got " + repr(item))
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out



# Modoptions that differ from BAR's own defaults. Everything not listed here is
# left alone, so a match matches what people actually play.
#
#   experimentallegionfaction  modoptions.lua:1855, bool, default false
#   draft_mode                 modoptions.lua:224-257, list, default "random";
#                              "skill" orders start-position picking by skill.
#                              Only has an effect with StartPosType 2, since
#                              game_initial_spawn.lua only consults draftMode on
#                              the choose-in-game path.
#   coop                       modoptions.lua:690, bool, default false; gives
#                              id-sharing teams one commander per player. Inert
#                              in an all-AI match, set for parity with the
#                              lobby-hosted game.
DEFAULT_MODOPTIONS = {
    "experimentallegionfaction": "1",
    "draft_mode": "skill",
    "coop": "1",
}


def default_modoptions(raw: str) -> "dict[str, str]":
    """BAR's defaults, plus Legion, with anything from the environment on top."""
    opts = dict(DEFAULT_MODOPTIONS)
    opts.update(parse_kv(raw))
    # An explicit empty value removes the key, so a run can turn Legion back off
    # with BARHOST_MODOPTIONS=experimentallegionfaction=
    return {k: v for k, v in opts.items() if v != ""}


# --- Configuration ----------------------------------------------------------


@dataclass
class Config:
    engine: str = ""
    data_dir: str = "/bar/data"
    write_dir: str = "/bar/out"
    engine_dir: str = ""

    gametype: str = "byar:test"
    map_name: str = ""

    ai_shortname: str = "BARb"
    ai_version: str = "stable"
    ai_count: int = 8
    ally_teams: int = 2

    startpostype: int = 2
    start_boxes: str = "map"
    team_layout: str = "interleaved"
    ai_handicap: float = 50.0
    modoptions: dict = field(default_factory=dict)
    ai_options: dict = field(default_factory=dict)

    speed: float = 20.0
    timeout: int = 3600
    autohost_port: int = 53200
    host_port: int = 53210
    host_name: str = "Autohost"
    record_demos: bool = True
    worker_threads: int = 0  # 0 = derive from the cgroup CPU quota
    observer_team: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        # BAR_ENGINE_DIR is set by the image; the engine binary lives in it.
        engine_dir = env("BAR_ENGINE_DIR", env("BARHOST_ENGINE_DIR"))
        cfg = cls(
            engine=env("BARHOST_ENGINE", str(Path(engine_dir) / "spring-headless") if engine_dir else ""),
            data_dir=env("BARHOST_DATA_DIR", cls.data_dir),
            write_dir=env("BARHOST_WRITE_DIR", cls.write_dir),
            engine_dir=engine_dir,
            gametype=env("BARHOST_GAMETYPE", cls.gametype),
            map_name=env("BARHOST_MAP", cls.map_name),
            ai_shortname=env("BARHOST_AI_SHORTNAME", cls.ai_shortname),
            ai_version=env("BARHOST_AI_VERSION", cls.ai_version),
            ai_count=env_int("BARHOST_AI_COUNT", cls.ai_count),
            ally_teams=env_int("BARHOST_ALLYTEAMS", cls.ally_teams),
            startpostype=env_int("BARHOST_STARTPOSTYPE", cls.startpostype),
            start_boxes=env("BARHOST_START_BOXES", cls.start_boxes),
            team_layout=env("BARHOST_TEAM_LAYOUT", cls.team_layout),
            ai_handicap=env_float("BARHOST_AI_HANDICAP", cls.ai_handicap),
            modoptions=default_modoptions(env("BARHOST_MODOPTIONS")),
            ai_options=parse_kv(env("BARHOST_AI_OPTIONS")),
            speed=env_float("BARHOST_SPEED", cls.speed),
            timeout=env_int("BARHOST_TIMEOUT_SECONDS", cls.timeout),
            autohost_port=env_int("BARHOST_AUTOHOST_PORT", cls.autohost_port),
            host_port=env_int("BARHOST_HOST_PORT", cls.host_port),
            host_name=env("BARHOST_HOST_NAME", cls.host_name),
            record_demos=env("BARHOST_RECORD_DEMOS", "1") not in ("0", "false", "no"),
            worker_threads=env_int("BARHOST_WORKER_THREADS", cls.worker_threads),
            observer_team=env("BARHOST_OBSERVER_TEAM", "1") not in ("0", "false", "no"),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.map_name:
            raise SystemExit("BARHOST_MAP is required (the map's display name)")
        if self.ai_count < 2:
            raise SystemExit("BARHOST_AI_COUNT must be at least 2")
        if self.ally_teams < 2:
            raise SystemExit("BARHOST_ALLYTEAMS must be at least 2")
        if self.ai_count % self.ally_teams:
            raise SystemExit(
                "BARHOST_AI_COUNT (%d) must divide evenly into BARHOST_ALLYTEAMS (%d)"
                % (self.ai_count, self.ally_teams)
            )
        if self.ai_count > len(TEAM_COLORS):
            raise SystemExit(
                "BARHOST_AI_COUNT above %d is not supported" % len(TEAM_COLORS)
            )
        if not self.engine or not Path(self.engine).is_file():
            raise SystemExit("engine not found at %r (set BAR_ENGINE_DIR)" % self.engine)


# --- GameSetup script generation -------------------------------------------


class Tdf:
    """Minimal TDF writer matching what ``TdfParser`` accepts."""

    def __init__(self) -> None:
        self._lines = []
        self._depth = 0

    def _emit(self, text: str) -> None:
        self._lines.append(("\t" * self._depth) + text)

    def kv(self, key: str, value) -> None:
        self._emit("%s=%s;" % (key, value))

    def open(self, section: str) -> None:
        self._emit("[" + section + "]")
        self._emit("{")
        self._depth += 1

    def close(self) -> None:
        self._depth -= 1
        self._emit("}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"





def detect_cpu_quota() -> "float | None":
    """Cores this process may actually use, from the cgroup, or None.

    This matters more than it looks. The engine sizes its ThreadPool from
    ``CPUID::GetNumLogicalCores`` (Threading.cpp:348-352, ThreadPool.cpp:115-120)
    and CircuitAI sizes its worker pool from ``hardware_concurrency()``
    (Scheduler.cpp:36). Both read the *host's* CPU topology, which in a
    container is not the same thing as the CPU the container is allowed. On a
    24-core node with a 6-core limit the process happily starts ~24 workers and
    then spends its quota being throttled and context-switching.

    Reading the quota lets the engine's pool be sized to the container instead.
    """
    # cgroup v2
    try:
        raw = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if raw and raw[0] != "max":
            return int(raw[0]) / int(raw[1])
    except (OSError, ValueError, IndexError):
        pass
    # cgroup v1
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text().strip())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def write_engine_config(cfg: Config, path: Path) -> "dict[str, str]":
    """Write the engine config file passed via --config.

    Two settings earn their place:

    ``ServerRecordDemos``  CONFIG(bool, ServerRecordDemos) defaults to *false*
        and only the dedicated build defaults it true (GameServer.cpp:75). The
        headless server therefore records nothing unless asked, and
        SendStartPlaying reports an empty demo name (:2316-2318). Without this
        there is no replay to look at, which is most of the point of a run.

    ``WorkerThreadCount``  sized to the cgroup quota rather than the host's core
        count -- see detect_cpu_quota.
    """
    settings = {"ServerRecordDemos": "1" if cfg.record_demos else "0"}

    workers = cfg.worker_threads
    if workers <= 0:
        quota = detect_cpu_quota()
        if quota is not None:
            # Leave the count at least 2; the pool includes the main thread.
            workers = max(2, int(quota))
    if workers > 0:
        settings["WorkerThreadCount"] = str(workers)

    lines = ["%s = %s" % kv for kv in sorted(settings.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return settings


def ally_team_of(team_index: int, cfg: Config) -> int:
    """Which allyteam a team belongs to.

    Default is "interleaved", i.e. team i joins allyteam i % allyTeams, because
    that is how BAR maps lay their start positions out. All That Glitters, for
    example, alternates them by index -- team 0 north, team 1 south, team 2
    north, and so on. With StartPosType 0 the engine hands team i the map's
    start position i, so blocking teams into contiguous allyteams (0-3 versus
    4-7) scatters each side across both halves of the map and seats every team
    next to an enemy. Interleaving puts one side on each half, which is what the
    map author intended.

    "blocked" keeps the contiguous assignment for maps that group their start
    positions by side instead.
    """
    if cfg.team_layout == "blocked":
        return team_index // (cfg.ai_count // cfg.ally_teams)
    if cfg.team_layout != "interleaved":
        raise SystemExit(
            "BARHOST_TEAM_LAYOUT must be 'interleaved' or 'blocked', got "
            + repr(cfg.team_layout))
    return team_index % cfg.ally_teams


def start_boxes(cfg: Config) -> "list[tuple[float, float, float, float]] | None":
    """Start rectangle per allyteam in 0..1, or None to leave it to the game.

    Default is "map": emit no StartRect at all and let BAR choose, which is what
    standard multiplayer does. BAR's own startbox gadget takes over
    (`game_startbox_config.lua`), sourcing boxes from the map metadata the lobby
    passes in `mapmetadata_startboxes_set`, and falling back to sensible
    north/south or east/west halves chosen from the map's aspect ratio when
    there is no lobby to supply it (`startbox_utilities.lua:233` buildFallback).

    Two things make overriding this a bad idea by default:

    * With `StartPosType` other than choose-in-game, BAR widens every box to the
      whole map anyway (`game_startbox_config.lua:30-40`), so a rect in the
      script is simply discarded -- the engine clamps incoming positions into
      the rect before Lua sees them, and any rect at all would drag valid
      positions onto its edges.
    * With choose-in-game, BAR's per-map polygons are richer than an axis-aligned
      rectangle and are what players actually get.

    "auto" splits the X axis into one band per allyteam, and an explicit value is
    semicolon-separated boxes of comma-separated floats, e.g.
    "0,0,0.3,1;0.7,0,1,1". Both exist for deliberate experiments, not for normal
    runs.
    """
    if cfg.start_boxes == "map":
        return None

    if cfg.start_boxes and cfg.start_boxes != "auto":
        boxes = []
        for chunk in cfg.start_boxes.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = [float(x) for x in chunk.split(",")]
            if len(parts) != 4:
                raise SystemExit(
                    "BARHOST_START_BOXES entries need 4 numbers "
                    "(left,top,right,bottom), got " + repr(chunk))
            boxes.append(tuple(parts))
        if len(boxes) != cfg.ally_teams:
            raise SystemExit(
                "BARHOST_START_BOXES has %d boxes but BARHOST_ALLYTEAMS is %d"
                % (len(boxes), cfg.ally_teams))
        return boxes

    # Even bands along X, each taking 70% of its slice so the sides start apart.
    boxes = []
    width = 1.0 / cfg.ally_teams
    margin = width * 0.15
    for i in range(cfg.ally_teams):
        left = i * width + margin
        right = (i + 1) * width - margin
        boxes.append((round(left, 4), 0.0, round(right, 4), 1.0))
    return boxes


def build_script(cfg: Config) -> str:
    """Render the GameSetup script for an all-AI match.

    One non-playing spectator occupies PLAYER0. It is not decoration: every
    ``[AIn]`` section must name a ``Host`` player -- GameSetup.cpp:47-48 throws
    ``missing AI.Host in GameSetup script`` otherwise -- because a Skirmish AI
    always executes inside the process of the player hosting it.
    """
    tdf = Tdf()

    tdf.open("GAME")

    # ClientSetup (ClientSetup.cpp:49-56).
    tdf.kv("IsHost", 1)
    tdf.kv("HostIP", "127.0.0.1")
    tdf.kv("HostPort", cfg.host_port)
    tdf.kv("MyPlayerName", cfg.host_name)

    # Autohost control channel, loopback only.
    tdf.kv("AutohostIP", "127.0.0.1")
    tdf.kv("AutohostPort", cfg.autohost_port)

    # Content. GameType accepts a rapid tag directly (GameSetup.cpp:668).
    tdf.kv("GameType", cfg.gametype)
    tdf.kv("MapName", cfg.map_name)

    # BAR's game_initial_spawn.lua:710-728 branches on this:
    #   0 automatic spawning at the map's default start positions, fixed order
    #   1 the same, random order
    #   2 choose in game; BAR places every team that did not choose -- which is
    #     every AI team -- via GuessStartSpot inside its start box
    # 2 is the default because it is what multiplayer uses; 0 gives identical
    # placement across runs for benchmarking. See design section 4.5.
    tdf.kv("StartPosType", cfg.startpostype)
    tdf.kv("NumPlayers", 1)
    observer = 1 if cfg.observer_team else 0
    tdf.kv("NumTeams", cfg.ai_count + observer)
    tdf.kv("NumAllyTeams", cfg.ally_teams + observer)

    # The host: a spectator, so it fields no units and cannot win.
    #
    # Its team is an extra "observer" team in an allyteam of its own
    # (BARHOST_OBSERVER_TEAM, default on). Without it, an all-AI team game
    # collapses: BAR's game_end.lua wipes out the allyteam the spectator sits in
    # within seconds whenever that allyteam holds two or more AI teams (design
    # section 11, risk 1). With it, measured 2026-09-27: a 4v4 of SMRTBARb ran
    # 18 minutes to a real elimination, and a 1v1 20 minutes. The cost is an idle
    # observer commander on the map, a third side the AIs eventually destroy.
    tdf.open("PLAYER0")
    tdf.kv("Name", cfg.host_name)
    tdf.kv("Spectator", 1)
    tdf.kv("Team", cfg.ai_count if observer else 0)
    tdf.close()

    for i in range(cfg.ai_count):
        tdf.open("AI%d" % i)
        tdf.kv("Name", "%s_%d" % (cfg.ai_shortname, i))
        tdf.kv("ShortName", cfg.ai_shortname)
        tdf.kv("Version", cfg.ai_version)
        tdf.kv("Team", i)
        tdf.kv("Host", 0)
        tdf.open("OPTIONS")
        for key, value in cfg.ai_options.items():
            tdf.kv(key, value)
        tdf.close()
        tdf.close()

    for i in range(cfg.ai_count):
        tdf.open("TEAM%d" % i)
        tdf.kv("TeamLeader", 0)
        tdf.kv("AllyTeam", ally_team_of(i, cfg))
        tdf.kv("RGBColor", TEAM_COLORS[i])
        tdf.kv("Side", SIDES[i % len(SIDES)])
        tdf.kv("Handicap", "%g" % cfg.ai_handicap)
        tdf.close()

    if observer:
        tdf.open("TEAM%d" % cfg.ai_count)
        tdf.kv("TeamLeader", 0)
        tdf.kv("AllyTeam", cfg.ally_teams)
        tdf.kv("RGBColor", "0.5 0.5 0.5")
        tdf.kv("Side", SIDES[0])
        tdf.close()

    boxes = start_boxes(cfg)
    for i in range(cfg.ally_teams):
        tdf.open("ALLYTEAM%d" % i)
        tdf.kv("NumAllies", 0)
        if boxes is not None:
            left, top, right, bottom = boxes[i]
            tdf.kv("StartRectLeft", left)
            tdf.kv("StartRectTop", top)
            tdf.kv("StartRectRight", right)
            tdf.kv("StartRectBottom", bottom)
        tdf.close()
    if observer:
        tdf.open("ALLYTEAM%d" % cfg.ally_teams)
        tdf.kv("NumAllies", 0)
        tdf.close()

    tdf.open("MODOPTIONS")
    for key, value in cfg.modoptions.items():
        tdf.kv(key, value)
    tdf.close()

    tdf.close()
    return tdf.render()


# --- Autohost supervision ---------------------------------------------------


@dataclass
class MatchResult:
    engine_config: dict = field(default_factory=dict)
    started: bool = False
    game_over: bool = False
    winning_ally_teams: list = field(default_factory=list)
    demo_name: str = ""
    exit_reason: str = "unknown"
    engine_returncode: object = None
    wallclock_seconds: float = 0.0
    warnings: list = field(default_factory=list)


class Autohost:
    """The autohost end of the engine's loopback control channel."""

    def __init__(self, port: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(1.0)
        self.engine_addr = None

    def command(self, cmd: str) -> None:
        """Send a server action. GameServer.cpp:861 strips the leading '/'."""
        if self.engine_addr is None:
            log("cannot send /%s yet: engine address unknown" % cmd)
            return
        self.sock.sendto(("/" + cmd).encode("utf-8"), self.engine_addr)
        log("-> /" + cmd)

    def poll(self):
        try:
            data, addr = self.sock.recvfrom(4096)
        except socket.timeout:
            return None
        # The engine's source port is where it listens for our commands.
        self.engine_addr = addr
        if not data:
            return None
        return data[0], data

    def close(self) -> None:
        self.sock.close()


def handle_event(event: int, data: bytes, result: MatchResult, cfg: Config,
                 host: Autohost) -> None:
    name = EVENT_NAMES.get(event, "UNKNOWN(%d)" % event)

    if event == SERVER_STARTED:
        log("<- " + name)
        # Order matters: setminspeed clamps to maxUserSpeed
        # (GameServer.cpp:2500), so raise the ceiling first.
        host.command("setmaxspeed %g" % cfg.speed)
        host.command("setminspeed %g" % cfg.speed)
        host.command("forcestart")

    elif event == SERVER_STARTPLAYING:
        # [0]=event [1:5]=uint32 msgsize [5:21]=gameID [21:]=demo name
        result.started = True
        if len(data) > 21:
            result.demo_name = data[21:].decode("utf-8", "replace")
        log("<- %s demo=%s" % (name, result.demo_name or "(recording off)"))

    elif event == SERVER_GAMEOVER:
        # [0]=event [1]=msgsize [2]=playerNum [3:]=winning ally teams
        result.game_over = True
        result.winning_ally_teams = list(data[3:])
        log("<- %s winners=%s" % (name, result.winning_ally_teams))
        host.command("kill")

    elif event == SERVER_QUIT:
        log("<- " + name)

    elif event == SERVER_WARNING:
        text = data[1:].decode("utf-8", "replace").rstrip()
        result.warnings.append(text)
        log("<- %s: %s" % (name, text))

    elif event == PLAYER_LEFT:
        log("<- %s player=%s" % (name, data[1] if len(data) > 1 else "?"))


def tee(stream, sink) -> None:
    """Copy the engine console to our stdout and to a file, line by line."""
    for line in iter(stream.readline, b""):
        sink.write(line)
        sink.flush()
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()


def installed_ai_version(cfg: Config) -> str:
    """The build stamp an AI release carries, if any (e.g. SMRTBARb_VERSION.txt)."""
    base = Path(cfg.engine_dir) / "AI" / "Skirmish" / cfg.ai_shortname / cfg.ai_version
    for stamp in sorted(base.glob("*VERSION*.txt")):
        try:
            return stamp.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
    return ""


def run(cfg: Config) -> MatchResult:
    write_dir = Path(cfg.write_dir)
    write_dir.mkdir(parents=True, exist_ok=True)

    script_path = write_dir / "script.txt"
    script_path.write_text(build_script(cfg), encoding="utf-8")
    log("wrote " + str(script_path))

    engine_config = write_engine_config(cfg, write_dir / "springsettings.cfg")
    result_config = engine_config
    log("engine config: " + ", ".join("%s=%s" % kv for kv in sorted(engine_config.items())))

    host = Autohost(cfg.autohost_port)
    log("autohost listening on 127.0.0.1:%d" % cfg.autohost_port)

    cmd = [
        cfg.engine,
        "--isolation",
        "--isolation-dir", cfg.data_dir,
        "--write-dir", cfg.write_dir,
        "--config", str(write_dir / "springsettings.cfg"),
        str(script_path),
    ]

    # Isolation mode (DataDirLocater.cpp:411-417) pins content discovery to one
    # directory, which would hide the engine's own AI/ and base/ trees. LEVEL 3
    # of LocateDataDirs (:434-441) still honours SPRING_DATADIR even in
    # isolation mode, so the engine directory is added back here. AddDirs splits
    # on colons (:178), so several paths may be joined if needed.
    child_env = dict(os.environ)
    if cfg.engine_dir:
        child_env["SPRING_DATADIR"] = cfg.engine_dir

    log("launching: " + " ".join(cmd))
    log("SPRING_DATADIR=" + child_env.get("SPRING_DATADIR", "(unset)"))

    result = MatchResult()
    result.engine_config = engine_config
    started_at = time.monotonic()

    # The engine console goes to the pod log *and* to engine-console.log in the
    # run directory, so it survives the pod. It holds what is printed before
    # infolog.txt opens, which is exactly what explains a failed start.
    console = (write_dir / "engine-console.log").open("wb")
    proc = subprocess.Popen(cmd, env=child_env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    pump = threading.Thread(target=tee, args=(proc.stdout, console), daemon=True)
    pump.start()
    deadline = started_at + cfg.timeout
    kill_sent_at = None

    try:
        while True:
            if proc.poll() is not None:
                result.engine_returncode = proc.returncode
                if result.exit_reason == "unknown":
                    result.exit_reason = "engine exited"
                break

            now = time.monotonic()

            if now > deadline:
                if kill_sent_at is None:
                    log("timeout after %ds -- asking the server to stop" % cfg.timeout)
                    result.exit_reason = "timeout"
                    host.command("kill")
                    kill_sent_at = now
                elif now - kill_sent_at > 30:
                    log("engine did not stop after /kill -- SIGTERM")
                    proc.terminate()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        log("engine ignored SIGTERM -- SIGKILL")
                        proc.kill()
                        proc.wait()
                    result.engine_returncode = proc.returncode
                    break

            if result.game_over and kill_sent_at is None:
                kill_sent_at = now
                result.exit_reason = "game over"
                # Give the engine room to finalise the demo before forcing it.
                deadline = min(deadline, now + 120)

            event = host.poll()
            if event is not None:
                handle_event(event[0], event[1], result, cfg, host)
    finally:
        host.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        pump.join(timeout=10)
        console.close()

    result.wallclock_seconds = round(time.monotonic() - started_at, 1)
    return result


def main() -> int:
    cfg = Config.from_env()

    log("gametype=%s map=%r" % (cfg.gametype, cfg.map_name))
    log("%dx %s/%s across %d allyteams at speed %g"
        % (cfg.ai_count, cfg.ai_shortname, cfg.ai_version, cfg.ally_teams, cfg.speed))

    result = run(cfg)

    payload = {
        "engineVersion": env("BAR_ENGINE_VERSION"),
        "runDir": cfg.write_dir,
        "gametype": cfg.gametype,
        "map": cfg.map_name,
        "ai": {
            "shortName": cfg.ai_shortname,
            "version": cfg.ai_version,
            "count": cfg.ai_count,
            "build": installed_ai_version(cfg),
            "options": cfg.ai_options,
        },
        "allyTeams": cfg.ally_teams,
        "teamLayout": cfg.team_layout,
        "startBoxes": start_boxes(cfg),
        "modoptions": cfg.modoptions,
        "speed": cfg.speed,
        "engineConfig": result.engine_config,
        "started": result.started,
        "gameOver": result.game_over,
        "winningAllyTeams": result.winning_ally_teams,
        "demo": result.demo_name,
        "exitReason": result.exit_reason,
        "engineReturncode": result.engine_returncode,
        "wallclockSeconds": result.wallclock_seconds,
        "warnings": result.warnings[:50],
    }
    out = Path(cfg.write_dir) / "result.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log("wrote " + str(out))

    log("finished: %s in %ss" % (result.exit_reason, result.wallclock_seconds))

    # A match that never started, or that the wall clock cut off, is a failed
    # run: the Job should go Failed so it is visible without digging.
    if not result.started:
        log("FAIL: the match never reached SERVER_STARTPLAYING")
        return 1
    if not result.game_over:
        log("FAIL: the match did not reach game over")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
