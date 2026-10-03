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
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Shared with the other modes; re-exported here for callers of run_match.
from barcommon import (  # noqa: F401
    EVENT_NAMES, PLAYER_JOINED, PLAYER_LEFT, PLAYER_READY, SERVER_GAMEOVER, SERVER_MESSAGE,
    SERVER_QUIT, SERVER_STARTED, SERVER_STARTPLAYING, SERVER_WARNING, SIDES, TEAM_COLORS,
    DEFAULT_MODOPTIONS, Autohost, Engine, Tdf, default_modoptions, detect_cpu_quota, env, env_float,
    env_int, make_log, parse_kv, tee, write_engine_config,
)

log = make_log("run_match")


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
    log("engine config: " + ", ".join("%s=%s" % kv for kv in sorted(engine_config.items())))

    host = Autohost(cfg.autohost_port, log=log)
    log("autohost listening on 127.0.0.1:%d" % cfg.autohost_port)

    result = MatchResult()
    result.engine_config = engine_config
    started_at = time.monotonic()

    # The engine console goes to the pod log *and* to engine-console.log in the
    # run directory, so it survives the pod. It holds what is printed before
    # infolog.txt opens, which is exactly what explains a failed start.
    proc = Engine(write_dir, script_path, write_dir / "springsettings.cfg", echo=True,
                  data_dir=cfg.data_dir, engine=cfg.engine, engine_dir=cfg.engine_dir)
    log("launched: " + " ".join(proc.cmd))
    log("SPRING_DATADIR=" + (cfg.engine_dir or "(unset)"))
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
                    log("engine did not stop after /kill -- stopping it")
                    proc.stop(grace=30)
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
        proc.stop(grace=0)

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
