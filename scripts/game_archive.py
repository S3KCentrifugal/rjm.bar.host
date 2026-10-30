#!/usr/bin/env python3
"""Move a finished game's files from the fast work disk into the game archive.

A headless client runs each game in its own run directory on fast storage
(the SSD: BARHOST_WORK_DIR). Once the engine has exited, this copies what is
worth keeping into the long-term archive (an HDD shared by every app and pod:
BARHOST_ARCHIVE_DIR), checks it, and removes the run directory. It runs as a
separate low-priority process (nice 19, idle I/O class), never inside the
client, so it never competes with a running engine.

  game_archive.py RUN_DIR... --archive ROOT --source NAME [--keep]
  game_archive.py --pending WORK_DIR --archive ROOT --source NAME

--pending archives every run directory under WORK_DIR; the client runs it at
start, when no engine of its own can be running, so a game cut short by a
restart is archived too (outcome "interrupted").

Archive layout (schema rjm-bar-archive/game/1), the same for every source, so
games from every app and pod sit side by side:

  ROOT/
    YYYY/MM/DD/<YYYYMMDD-HHMMSS>Z_<source>_<map>_<id>/   one game (UTC start)
      manifest.json      what, where, when, outcome, summaries, the engine's
                         game-end profiler totals (profile: sim, AI and each
                         AI's team, draw, Lua; engine_profile), file list with
                         sha256 (written last: a folder without one is partial)
      lobby.json         the room as the lobby client saw it at the start: the
                         battle, its script tags (mod options, skill), players,
                         bots, the client's own settings (AI channel, bonus)
      replay/<name>.sdfz the replay as the engine wrote it (its own name)
      metrics/           engine-timeline.csv (from the engine, every few
                         seconds: sim frame, how far behind the server, sim
                         speed, ping, profiler shares, units),
                         engine-events.csv (game start, game over),
                         engine-profile.csv (with BARHOST_ENGINE_PROFILER:
                         every engine timer's time per interval),
                         timeline.csv and threads.csv (from the client, same
                         interval: CPU, engine threads, memory, disk,
                         pressure), perf.csv (per-second cgroup samples)
      logs/              infolog.txt.gz, engine-console.log.gz, client.log.gz
                         (the lobby client's own lines during the game),
                         lobby-events.log.gz (room traffic during the game)
      config/            springsettings.cfg, script.txt (password removed)
      files/             anything else the engine or AIs wrote (AI logs, ...),
                         text gzipped, paths kept
    _index/<source>.jsonl  one line per archived game (one writer per source)
    _chat/<source>/        the lobby client's chat audit log (not written
                           here: lobby_client.py BARHOST_CHAT_DIR)

A folder is published with one rename from ROOT/_incoming/, so readers never
see half a game. <id> is the replay's game id (first 8 hex digits), or the
room number without a replay; the full id is in manifest.json and is the key
rjm.bar.telemetry uses.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_stats  # noqa: E402

SCHEMA = "rjm-bar-archive/game/1"
# Written by the engine in the write dir, not worth keeping: caches it rebuilds.
SKIP_DIRS = {"cache", "temp", "pool", "packages", "rapid", "maps", "games", "engine"}
TEXT = {".txt", ".log", ".csv", ".json", ".cfg", ".lua", ".jsonl", ".tsv", ".ini", ".xml", ".yaml", ".yml"}
# The metrics widget's files (rjm_host_metrics.lua): BAR lets user widgets
# write only under LuaUI/Config/ (luaui/barwidgets.lua, game test-31723);
# before that sandbox they were at the run dir's top.
ENGINE_TIMELINE = "LuaUI/Config/rjm-engine-timeline.csv"
ENGINE_TIMELINE_OLD = "engine-timeline.csv"
# Run-dir files with a fixed place in the archive: path in the run dir -> archive path.
PLACED = {
    "game.json": "lobby.json",
    "timeline.csv": "metrics/timeline.csv",
    "threads.csv": "metrics/threads.csv",
    ENGINE_TIMELINE_OLD: "metrics/engine-timeline.csv",
    "engine-events.csv": "metrics/engine-events.csv",
    ENGINE_TIMELINE: "metrics/engine-timeline.csv",
    "LuaUI/Config/rjm-engine-events.csv": "metrics/engine-events.csv",
    "LuaUI/Config/rjm-engine-profile.csv": "metrics/engine-profile.csv",
    "perf.csv": "metrics/perf.csv",
    "infolog.txt": "logs/infolog.txt",
    "engine-console.log": "logs/engine-console.log",
    "client.log": "logs/client.log",
    "lobby-events.log": "logs/lobby-events.log",
    "springsettings.cfg": "config/springsettings.cfg",
    "script.txt": "config/script.txt",
}
GZIP_PLACED = {"logs/infolog.txt", "logs/engine-console.log", "logs/client.log", "logs/lobby-events.log"}


def log(msg: str) -> None:
    print("[game-archive] " + msg, file=sys.stderr, flush=True)


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9.-]+", "-", text or "").strip("-.")
    return (s or "unknown")[:limit].strip("-.") or "unknown"


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def redact_script(text: str) -> str:
    """The start script carries this client's one-game password."""
    return re.sub(r"(?im)^(\s*MyPasswd\s*=\s*)[^;]*;", r"\1<removed>;", text)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def low_priority() -> None:
    """Lowest CPU priority and the idle I/O class: only spare capacity."""
    try:
        os.nice(19)
    except (OSError, AttributeError):       # AttributeError: not on Windows
        pass
    try:  # ioprio_set(IOPRIO_WHO_PROCESS, 0, IOPRIO_CLASS_IDLE << 13): Linux only
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        nr = {"x86_64": 251, "aarch64": 30}.get(os.uname().machine)
        if nr:
            libc.syscall(nr, 1, 0, 3 << 13)
    except Exception:  # not Linux, or no such syscall: normal I/O priority
        pass


LAG_COLUMNS = ("behind_s", "sim_fps", "ping_s", "units")              # engine-timeline.csv
LOAD_COLUMNS = ("cores", "main_thread_cores", "main_preempt_per_s", "mem_gib", "throttled_pct",
                "cpu_pressure_pct", "io_pressure_pct", "mem_pressure_pct", "write_mb_s")   # timeline.csv


def read_rows(path: Path) -> "list[dict]":
    """A metrics CSV as dicts; a line cut short at the end is left out."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if len(lines) < 2:
        return []
    head = lines[0].split(",")
    return [dict(zip(head, ln.split(","))) for ln in lines[1:] if ln.count(",") == len(head) - 1]


def summarise_rows(rows: "list[dict]", columns) -> dict:
    """Per column: max, p95, average, min, and the last value ("now")."""
    out = {"samples": len(rows)} if rows else {}
    for name in columns:
        v = []
        for r in rows:
            try:
                v.append(float(r.get(name, "")))
            except ValueError:
                pass
        if v:
            now = v[-1]
            v.sort()
            out[name] = {"max": round(v[-1], 3), "p95": round(v[min(len(v) - 1, int(len(v) * 0.95))], 3),
                         "avg": round(sum(v) / len(v), 3), "min": round(v[0], 3), "now": round(now, 3)}
    return out


def summarise_timeline(path: Path, columns) -> dict:
    """max, p95, average, min and last of some columns over the game."""
    return summarise_rows(read_rows(path), columns)


def engine_timeline(run: Path) -> Path:
    """The metrics widget's timeline in a run dir (where an older widget put it, if so)."""
    new = run / ENGINE_TIMELINE
    return new if new.is_file() or not (run / ENGINE_TIMELINE_OLD).is_file() else run / ENGINE_TIMELINE_OLD


# The engine's profiler table, printed at game end by a headless engine
# (RecoilEngine rts/Game/Game.cpp GameEnd -> CTimeProfiler::PrintProfilingInfo,
# rts/System/TimeProfiler.cpp): a "Part|Total Time|..." header, then one
# "<timer> <total>ms <pct>%" line per timer in its last sorted list (it can
# miss timers). The % is of the last 0.5 s only, so it is not kept. "AI" needs
# the profiler on (/debug; BARHOST_ENGINE_PROFILER); "Sim" and "Draw" are kept
# without it.
PROFILE_HEAD = re.compile(rb"\] +Part\|\s*Total Time\|")
PROFILE_ROW = re.compile(r"^\[t=[^\]]*\]\[f=[^\]]*\] *(\S.*?) +(\d+(?:\.\d+)?)ms +[\d.]+%$")
AI_TEAM = re.compile(r"^AI::\{.*\bteam=(\d+)\b")
PROFILE_TAIL_BYTES = 4 << 20


def engine_profile(infolog: Path) -> dict:
    """The engine's game-end profiler table from an infolog: every timer's
    total (s), and the main ones: sim, the AIs (all, and per team), draw,
    Lua call-ins, the profiler's own cost. {} without a table."""
    try:
        with infolog.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - PROFILE_TAIL_BYTES))
            tail = f.read()
    except OSError:
        return {}
    heads = list(PROFILE_HEAD.finditer(tail))
    if not heads:
        return {}
    timers = {}
    for line in tail[heads[-1].end():].decode("utf-8", "replace").splitlines()[1:]:
        m = PROFILE_ROW.match(line)
        if not m:
            break
        timers[m.group(1)] = round(float(m.group(2)) / 1000, 3)
    if not timers:
        return {}
    out = {"timers": timers}
    for key, name in (("simS", "Sim"), ("aiS", "AI"), ("drawS", "Draw"), ("updateS", "Update"),
                      ("profilerS", "Misc::Profiler::AddTime")):
        if name in timers:
            out[key] = timers[name]
    lua = [v for k, v in timers.items() if k.startswith("Lua::Callins::")]
    if lua:
        out["luaCallinsS"] = round(sum(lua), 3)
    teams = {}
    for k, v in timers.items():
        m = AI_TEAM.match(k)
        if m:
            teams[m.group(1)] = round(teams.get(m.group(1), 0) + v, 3)
    if teams:
        out["aiByTeamS"] = dict(sorted(teams.items(), key=lambda kv: int(kv[0])))
    if out.get("simS") and out.get("aiS") is not None:
        out["aiPctOfSim"] = round(out["aiS"] * 100 / out["simS"], 1)
    return out


def index_path(root: Path, source: str) -> Path:
    return root / "_index" / ("%s.jsonl" % slug(source, 64))


def copy_into(src: Path, dest: Path, gz: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if gz:
        with src.open("rb") as fi, gzip.open(str(dest) + ".gz", "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
    else:
        shutil.copyfile(src, dest)


def archive_run(run: Path, root: Path, source: str, keep: bool = False) -> "Path | None":
    """Archive one run directory; return the published game folder."""
    lobby = read_json(run / "game.json")
    ended = read_json(run / "ended.json")
    replays = sorted((run / "demos").glob("*.sdfz")) if (run / "demos").is_dir() else []
    demo = {}
    if replays:
        try:
            demo = demo_stats.read_demo(replays[-1])
        except (SystemExit, OSError, ValueError, EOFError) as e:  # a replay cut short
            demo = {"error": str(e)}
    started = lobby.get("startedUtc") or ""
    try:
        t0 = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        t0 = dt.datetime.fromtimestamp(run.stat().st_mtime, dt.timezone.utc)
    t0 = t0.astimezone(dt.timezone.utc)
    game_id = demo.get("gameId") or ""
    short = game_id[:8] if game_id and game_id.strip("0") else "room%s" % slug(str(lobby.get("battle", "x")), 12)
    folder = "%sZ_%s_%s_%s" % (t0.strftime("%Y%m%d-%H%M%S"), slug(source, 32), slug(lobby.get("map", ""), 32), short)
    final = root / t0.strftime("%Y") / t0.strftime("%m") / t0.strftime("%d") / folder
    if final.exists():
        log("%s already archived as %s" % (run, final))
        if not keep:
            shutil.rmtree(run, ignore_errors=True)
        return final
    incoming = root / "_incoming" / ("%s.%d" % (folder, os.getpid()))
    if incoming.exists():
        shutil.rmtree(incoming)
    incoming.mkdir(parents=True)

    for name, rel in PLACED.items():
        src = run / name
        if not src.is_file():
            continue
        if name == "script.txt":
            dest = incoming / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(redact_script(src.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        else:
            copy_into(src, incoming / rel, rel in GZIP_PLACED)
    for r in replays:
        copy_into(r, incoming / "replay" / r.name, False)
    for src in sorted(run.rglob("*")):
        rel = src.relative_to(run)
        if rel.as_posix() in PLACED or rel.parts[0] in ("ended.json", "demos") or rel.parts[0] in SKIP_DIRS \
                or not src.is_file() or src.is_symlink():
            continue
        copy_into(src, incoming / "files" / rel, src.suffix.lower() in TEXT)

    if ended.get("interrupted") or not ended:
        outcome = "interrupted"
    elif demo.get("winningAllyTeams"):
        outcome = "winner"
    elif demo.get("teams") and any(t.get("samples") for t in demo["teams"]):
        outcome = "no_winner"
    else:
        outcome = "unfinished"
    files = []
    for f in sorted(p for p in incoming.rglob("*") if p.is_file()):
        files.append({"path": f.relative_to(incoming).as_posix(), "bytes": f.stat().st_size, "sha256": sha256(f)})
    manifest = {
        "schema": SCHEMA,
        "folder": "%s/%s" % (final.relative_to(root).parent.as_posix(), folder),
        "source": source,
        "gameId": game_id or None,
        "startedUtc": t0.isoformat().replace("+00:00", "Z"),
        "endedUtc": ended.get("endedUtc"),
        "outcome": outcome,
        "winningAllyTeams": demo.get("winningAllyTeams", []),
        "gameSeconds": demo.get("gameTimeSeconds"),
        "wallclockSeconds": demo.get("wallclockSeconds"),
        "map": lobby.get("map"),
        "game": lobby.get("game"),
        "engine": demo.get("engine") or lobby.get("engine"),
        "room": {k: lobby.get(k) for k in ("battle", "founder", "title")},
        "run": run.name,
        "players": [p.get("name") for p in lobby.get("players", [])],
        "humans": [p.get("name") for p in lobby.get("players", [])
                   if p.get("player") and not p.get("self") and not p.get("bot")],
        "bots": len(lobby.get("bots", [])),
        "ai": lobby.get("ai"),
        "host": {"pod": lobby.get("pod") or socket.gethostname(), "client": lobby.get("client"),
                 "engineExit": ended.get("engineExit"), "resigned": ended.get("resigned")},
        "lag": summarise_timeline(engine_timeline(run), LAG_COLUMNS),
        "load": summarise_timeline(run / "timeline.csv", LOAD_COLUMNS),
        "perf": ended.get("perf"),
        "profile": engine_profile(run / "infolog.txt"),
        "teams": demo.get("teams", []),
        "replay": ("replay/" + replays[-1].name) if replays else None,
        "replayError": demo.get("error"),
        "files": files,
    }
    # The manifest last, then publish the folder in one rename.
    (incoming / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    final.parent.mkdir(parents=True, exist_ok=True)
    incoming.rename(final)
    index = index_path(root, source)
    index.parent.mkdir(parents=True, exist_ok=True)
    line = {k: manifest[k] for k in ("folder", "run", "gameId", "startedUtc", "endedUtc", "outcome", "map",
                                     "source", "gameSeconds")}
    perf = manifest["perf"] or {}
    line.update({"players": len(manifest["players"]), "humans": len(manifest["humans"]),
                 "lagMaxS": (manifest["lag"].get("behind_s") or {}).get("max"),
                 "lagP95S": (manifest["lag"].get("behind_s") or {}).get("p95"),
                 "coresP95": perf.get("cores_p95"), "memPeakGiB": perf.get("mem_peak_gib"),
                 "simS": manifest["profile"].get("simS"), "aiPctOfSim": manifest["profile"].get("aiPctOfSim"),
                 "bytes": sum(f["bytes"] for f in files)})
    with index.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    log("archived %s -> %s (%s, %d files, %.1f MB)" % (
        run.name, final, outcome, len(files), line["bytes"] / 1e6))
    if not keep:
        shutil.rmtree(run, ignore_errors=True)
    return final


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="*", type=Path)
    ap.add_argument("--pending", type=Path, help="archive every run directory under this work dir")
    ap.add_argument("--archive", type=Path, required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--keep", action="store_true", help="leave the run directories in place")
    a = ap.parse_args(argv)
    low_priority()
    runs = list(a.runs)
    if a.pending and a.pending.is_dir():
        runs += sorted(p for p in a.pending.iterdir() if p.is_dir() and not p.name.startswith("."))
    failed = 0
    for run in runs:
        try:
            archive_run(run, a.archive, a.source, a.keep)
        except Exception as e:  # keep the run dir; the next --pending retries it
            failed += 1
            log("could not archive %s: %s" % (run, e))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
