#!/usr/bin/env python3
"""Run a match under run_match.py and sample what it costs.

Resource sizing for this workload cannot be reasoned out from first principles,
because the two things that dominate it pull in opposite directions:

* Skirmish AI updates are **serial**. CEngineOutHandler::Update loops over every
  AI on the sim thread (EngineOutHandler.cpp:85-88, called from Game.cpp:1748),
  so N AI multiply the per-frame AI cost on a single core rather than spreading
  across cores.
* CircuitAI offloads some work to a **process-wide** pool of
  ``clamp(hardware_concurrency() - 1, 2, 8)`` threads (Scheduler.cpp:21,36),
  shared by every AI instance rather than one pool each.

So neither "N AI needs N cores" nor "it is single-threaded" is true. Measure.

Usage:
  scripts/measure_match.py --ai 8 --duration 600
  scripts/measure_match.py --ai 16 --duration 600 --tag 8v8

Writes summary-<tag>.json next to the run directory. Needs psutil.

`--duration` is a wall-clock budget, not a match length: the run is scored on
sustained cost, so it sets deathmode=neverend by default to stop the match
ending early and truncating the sample.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    sys.exit("psutil is required: pip install psutil")

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "run_match.py"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ai", type=int, default=8, help="number of AI (default 8)")
    ap.add_argument("--allyteams", type=int, default=2)
    ap.add_argument("--duration", type=int, default=600,
                    help="wall-clock budget in seconds (default 600)")
    ap.add_argument("--speed", type=float, default=100.0)
    ap.add_argument("--map", default=os.environ.get("BARHOST_MAP", ""))
    ap.add_argument("--out", default="", help="output directory (default ./measure)")
    ap.add_argument("--tag", default="", help="label for the summary file")
    ap.add_argument("--let-it-end", action="store_true",
                    help="do not set deathmode=neverend; let the match finish naturally")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    tag = args.tag or ("ai%d" % args.ai)
    out_root = Path(args.out or "measure").resolve()
    run_dir = out_root / ("run-" + tag)
    run_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({
        "BARHOST_WRITE_DIR": str(run_dir),
        "BARHOST_AI_COUNT": str(args.ai),
        "BARHOST_ALLYTEAMS": str(args.allyteams),
        "BARHOST_SPEED": str(args.speed),
        "BARHOST_TIMEOUT_SECONDS": str(args.duration),
    })
    if args.map:
        env["BARHOST_MAP"] = args.map
    if not args.let_it_end:
        # game_end.lua:246 removes itself entirely on this, so the match cannot
        # finish early and cut the sample short.
        extra = env.get("BARHOST_MODOPTIONS", "")
        env["BARHOST_MODOPTIONS"] = (extra + "," if extra else "") + "deathmode=neverend"

    print("[measure] %d AI, %ds budget, speed %g -> %s"
          % (args.ai, args.duration, args.speed, run_dir), flush=True)

    started = time.time()
    proc = subprocess.Popen([sys.executable, str(RUNNER)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")

    log_lines: list = []

    def pump() -> None:
        for line in proc.stdout:
            log_lines.append(line.rstrip())
            print("  " + line.rstrip(), flush=True)

    threading.Thread(target=pump, daemon=True).start()

    def find_engine():
        for p in psutil.process_iter(["name", "create_time"]):
            try:
                name = (p.info["name"] or "").lower()
                if "spring-headless" in name and p.info["create_time"] >= started - 5:
                    return p
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None

    samples: list = []
    engine = None
    peak_rss = 0
    peak_threads = 0

    while proc.poll() is None:
        if engine is None or not engine.is_running():
            engine = find_engine()
        if engine is not None:
            try:
                with engine.oneshot():
                    ct = engine.cpu_times()
                    rss = engine.memory_info().rss
                    nthreads = engine.num_threads()
                peak_rss = max(peak_rss, rss)
                peak_threads = max(peak_threads, nthreads)
                samples.append({
                    "t": round(time.time() - started, 1),
                    "cpu_s": round(ct.user + ct.system, 2),
                    "rss_mb": round(rss / 1e6, 1),
                    "threads": nthreads,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                engine = None
        time.sleep(1.0)

    proc.wait()

    # Measure over the window the engine was actually alive, so process startup
    # and teardown do not dilute the rate.
    if len(samples) > 1:
        window = samples[-1]["t"] - samples[0]["t"]
        cpu_used = samples[-1]["cpu_s"] - samples[0]["cpu_s"]
    else:
        window, cpu_used = 0.0, 0.0

    # Steady state: the last third, after loading and the early build-up.
    tail = samples[len(samples) * 2 // 3:] if len(samples) > 6 else samples
    if len(tail) > 1:
        tail_cores = (tail[-1]["cpu_s"] - tail[0]["cpu_s"]) / (tail[-1]["t"] - tail[0]["t"])
    else:
        tail_cores = None

    result = {}
    rj = run_dir / "result.json"
    if rj.exists():
        result = json.loads(rj.read_text(encoding="utf-8"))

    summary = {
        "ai_count": args.ai,
        "allyteams": args.allyteams,
        "speed": args.speed,
        "map": result.get("map"),
        "sample_window_s": round(window, 1),
        "cpu_seconds": round(cpu_used, 1),
        "mean_cores": round(cpu_used / window, 2) if window else None,
        "steady_state_cores": round(tail_cores, 2) if tail_cores else None,
        "peak_rss_mb": round(peak_rss / 1e6, 1),
        "peak_threads": peak_threads,
        "runner_exit": proc.returncode,
        "match_result": result,
        "samples": samples,
    }

    (out_root / ("summary-%s.json" % tag)).write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    (out_root / ("log-%s.txt" % tag)).write_text(
        "\n".join(log_lines), encoding="utf-8")

    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("samples", "match_result")}, indent=2), flush=True)
    print("\nSize the Job from steady_state_cores and peak_rss_mb, with headroom:")
    print("memory is the one that kills a pod, and it grows with match length.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
