"""Shared by every mode of the image: settings, start scripts, the engine, the
engine's Autohost channel, and the Spring lobby protocol's encodings.

  run_match.py     match       AI-vs-AI simulation (engine as host + autohost)
  lan_game.py      host, join  direct-connect games between containers
  lobby_client.py  client      headless lobby client on BAR's own autohosts
  tests/e2e        spads       the fake lobby server that tests SPADS

Engine facts are cited by Recoil file:line where they are not obvious.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path


# --- Logging and settings ----------------------------------------------------


def make_log(tag: str):
    def log(msg: str) -> None:
        print("[%s] %s" % (tag, msg), flush=True)
    return log


log = make_log("barhost")


def env(name: str, default: str = "") -> str:
    """An environment setting; unset and empty both mean the default."""
    return os.environ.get(name, "").strip() or default


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


# --- Start scripts -------------------------------------------------------------

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


# BAR's two original factions, alternated across teams in a start script.
SIDES = ("Armada", "Cortex")

# TeamBase::SetDefaultColor has few distinct colours; supplying our own keeps
# an 8-team replay readable.
TEAM_COLORS = (
    "0.7 0.1 0.1", "0.1 0.3 0.8", "0.1 0.6 0.2", "0.8 0.7 0.1",
    "0.6 0.2 0.7", "0.9 0.5 0.1", "0.2 0.7 0.7", "0.8 0.4 0.6",
    "0.5 0.5 0.5", "0.4 0.2 0.1", "0.3 0.9 0.5", "0.9 0.3 0.5",
    "0.2 0.2 0.6", "0.6 0.8 0.3", "0.9 0.9 0.9", "0.3 0.1 0.3",
)


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


def client_script(host: str, port: int, name: str, password: str, source_port: "int | None" = None) -> str:
    """A joining client's start script: the server sends the rest
    (ClientSetup.cpp:39-77)."""
    tdf = Tdf()
    tdf.open("GAME")
    tdf.kv("HostIP", host)
    tdf.kv("HostPort", port)
    if source_port is not None:
        tdf.kv("SourcePort", source_port)
    tdf.kv("IsHost", 0)
    tdf.kv("MyPlayerName", name)
    tdf.kv("MyPasswd", password)
    tdf.close()
    return tdf.render()


# --- The engine ----------------------------------------------------------------


def engine_paths():
    """($BAR_ENGINE_DIR, its spring-headless)."""
    engine_dir = env("BAR_ENGINE_DIR")
    if not engine_dir:
        raise SystemExit("BAR_ENGINE_DIR is not set")
    return engine_dir, str(Path(engine_dir) / "spring-headless")


def detect_cpu_quota() -> "float | None":
    """Cores this process may actually use, from the cgroup, or None.

    The engine sizes its ThreadPool from ``CPUID::GetNumLogicalCores``
    (Threading.cpp:348-352, ThreadPool.cpp:115-120) and CircuitAI sizes its
    worker pool from ``hardware_concurrency()`` (Scheduler.cpp:36). Both read
    the *host's* CPU topology. On a 24-core node with a 6-core limit the process
    starts ~24 workers and spends its quota being throttled.
    """
    try:  # cgroup v2
        raw = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if raw and raw[0] != "max":
            return int(raw[0]) / int(raw[1])
    except (OSError, ValueError, IndexError):
        pass
    try:  # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text().strip())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def write_engine_config(cfg, path: Path, extra: "dict[str, str] | None" = None) -> "dict[str, str]":
    """Write the engine config file passed via --config. ``cfg`` needs
    ``record_demos`` and ``worker_threads``.

    ``ServerRecordDemos``  defaults to false outside the dedicated build
        (GameServer.cpp:75), so a headless server records no replay unless asked.
    ``WorkerThreadCount``  sized to the cgroup quota rather than the host's core
        count -- see detect_cpu_quota.
    """
    settings = {"ServerRecordDemos": "1" if cfg.record_demos else "0"}
    workers = cfg.worker_threads
    if workers <= 0:
        quota = detect_cpu_quota()
        if quota is not None:
            # At least 2; the pool includes the main thread.
            workers = max(2, int(quota))
    if workers > 0:
        settings["WorkerThreadCount"] = str(workers)
    settings.update(extra or {})
    lines = ["%s = %s" % kv for kv in sorted(settings.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return settings


def engine_settings(write_dir: Path, record_demos: bool = True) -> Path:
    """``springsettings.cfg`` for a run: demos recorded, worker threads sized to
    the cgroup quota, plus anything in $BARHOST_ENGINE_SETTINGS (``K=V,...``)."""
    path = write_dir / "springsettings.cfg"

    class _Cfg:
        worker_threads = env_int("BARHOST_WORKER_THREADS", 0)
    _Cfg.record_demos = record_demos
    write_engine_config(_Cfg, path, parse_kv(env("BARHOST_ENGINE_SETTINGS")))
    return path


def tee(stream, sink) -> None:
    """Copy the engine console to our stdout and to a file, line by line."""
    for line in iter(stream.readline, b""):
        sink.write(line)
        sink.flush()
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()


class Engine:
    """A running spring-headless. Its console goes to ``engine-console.log`` in
    the write dir, and to our stdout when ``echo`` is set.

    Isolation mode (DataDirLocater.cpp:411-417) pins content discovery to the
    data dir, which would hide the engine's own AI/ and base/ trees; LEVEL 3 of
    LocateDataDirs (:434-441) still honours SPRING_DATADIR, so the engine
    directory is added back that way.
    """

    def __init__(self, write_dir: Path, script: Path, settings: "Path | None" = None, echo: bool = True,
                 data_dir: "str | None" = None, engine: "str | None" = None, engine_dir: "str | None" = None):
        if engine is None:
            engine_dir, engine = engine_paths()
        self.write_dir = write_dir
        self.cmd = [engine, "--isolation", "--isolation-dir", data_dir or env("BARHOST_DATA_DIR", "/bar/data"),
                    "--write-dir", str(write_dir)]
        if settings is not None:
            self.cmd += ["--config", str(settings)]
        self.cmd.append(str(script))
        self.console = (write_dir / "engine-console.log").open("wb")
        child_env = dict(os.environ)
        if engine_dir:
            child_env["SPRING_DATADIR"] = engine_dir
        if echo:
            self.proc = subprocess.Popen(self.cmd, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.pump = threading.Thread(target=tee, args=(self.proc.stdout, self.console), daemon=True)
            self.pump.start()
        else:
            self.proc = subprocess.Popen(self.cmd, env=child_env, stdout=self.console, stderr=subprocess.STDOUT)
            self.pump = None

    def poll(self):
        return self.proc.poll()

    @property
    def returncode(self):
        return self.proc.returncode

    def terminate(self) -> None:
        """Ask the engine to quit, without waiting (SIGTERM)."""
        if self.proc.poll() is None:
            self.proc.terminate()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()

    def stop(self, grace: float = 30) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.close()

    def close(self) -> None:
        if self.pump is not None:
            self.pump.join(timeout=10)
        self.console.close()


# --- The engine's Autohost channel (AutohostInterface.cpp:36-130) ---------------

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


class Autohost:
    """The autohost end of the engine's loopback control channel."""

    def __init__(self, port: int, log=log) -> None:
        self.log = log
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(1.0)
        self.engine_addr = None

    def command(self, cmd: str) -> None:
        """Send a server action. GameServer.cpp:861 strips the leading '/'."""
        if self.engine_addr is None:
            self.log("cannot send /%s yet: engine address unknown" % cmd)
            return
        self.sock.sendto(("/" + cmd).encode("utf-8"), self.engine_addr)
        self.log("-> /" + cmd)

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


# --- The Spring lobby protocol ------------------------------------------------


def password_hash(password: str) -> str:
    """The lobby protocol's password form: base64(md5(password))."""
    return base64.b64encode(hashlib.md5(password.encode("utf-8")).digest()).decode("ascii")


def battle_status(ready=0, team=0, ally=0, mode=0, bonus=0, sync=0, side=0) -> int:
    """MYBATTLESTATUS / CLIENTBATTLESTATUS bits, as Chobby's liblobby packs
    them: team and ally have high bits above 15. Signed 32-bit."""
    v = ((ready & 1) << 1) | ((team & 15) << 2) | ((ally & 15) << 6) | ((mode & 1) << 10) \
        | ((bonus & 127) << 11) | (((team >> 4) & 15) << 18) | ((sync & 3) << 22) \
        | ((side & 15) << 24) | (((ally >> 4) & 15) << 28)
    return v - (1 << 32) if v >= 1 << 31 else v


def unpack_battle_status(v) -> dict:
    v = int(v)
    if v < 0:
        v += 1 << 32
    return {"ready": (v >> 1) & 1,
            "team": ((v >> 2) & 15) | (((v >> 18) & 15) << 4),
            "ally": ((v >> 6) & 15) | (((v >> 28) & 15) << 4),
            "mode": (v >> 10) & 1, "bonus": (v >> 11) & 127,
            "sync": (v >> 22) & 3, "side": (v >> 24) & 15}
