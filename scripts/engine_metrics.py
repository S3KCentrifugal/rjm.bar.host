#!/usr/bin/env python3
"""What the engine process and its container do during a game, every few
seconds, for lining up with the engine's own lag line (rjm_host_metrics.lua).

Everything comes from small kernel files (/proc/<pid>, the cgroup), read a
few times a minute from the lobby client's loop: no tracing, nothing inside
the engine.

    s = HostSampler(every=5)
    s.start(run_dir, engine_pid, now)   # timeline.csv, threads.csv in run_dir
    s.sample(now)                       # call about once a second
    s.stop()

timeline.csv, one line per interval:
  unix_s, t_s              wall clock, and seconds since the game started
  cores                    the container's CPU use (cores)
  throttled_pct            share of CPU periods the quota throttled
  mem_gib                  the container's memory
  cpu_pressure_pct,        PSI "some" avg10 for the container: share of time
  io_pressure_pct,         a task waited for CPU / disk / memory (cgroup v2;
  io_full_pct,             io "full": every task waited for disk)
  mem_pressure_pct
  engine_cores             the engine process's CPU use
  main_thread_cores        its main thread, which runs the simulation: near
                           1.0 means the sim is CPU-bound on one core
  main_preempt_per_s       the main thread's involuntary context switches per
                           second (it wanted to run and was pushed off a CPU)
  engine_threads           its thread count
  engine_rss_gib           its resident memory
  read_mb_s, write_mb_s    its disk reads and writes
  top_threads              the busiest thread names, "name=cores;..."
threads.csv, one line per thread name per interval: unix_s, name, threads, cores.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import cgroup_perf

TIMELINE_HEAD = ("unix_s,t_s,cores,throttled_pct,mem_gib,cpu_pressure_pct,io_pressure_pct,io_full_pct,"
                 "mem_pressure_pct,engine_cores,main_thread_cores,main_preempt_per_s,engine_threads,"
                 "engine_rss_gib,read_mb_s,write_mb_s,top_threads\n")


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def pressure(text: str) -> dict:
    """PSI file -> {"some": avg10, "full": avg10} (percent)."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] in ("some", "full"):
            for p in parts[1:]:
                if p.startswith("avg10="):
                    try:
                        out[parts[0]] = float(p[6:])
                    except ValueError:
                        pass
    return out


def stat_times(text: str) -> "tuple[str, int] | None":
    """/proc/.../stat -> (comm, utime + stime in ticks). comm may hold spaces
    and parentheses, so split at the last ')'."""
    if ")" not in text:
        return None
    head, _, rest = text.rpartition(")")
    comm = head.partition("(")[2]
    fields = rest.split()
    try:
        return comm, int(fields[11]) + int(fields[12])   # utime, stime (fields 14, 15)
    except (IndexError, ValueError):
        return None


class HostSampler:
    def __init__(self, every: float = 5, cgroup: "cgroup_perf.Cgroup | None" = None, proc: Path = Path("/proc")):
        self.every = every
        self.cg = cgroup or cgroup_perf.Cgroup()
        self.proc = Path(proc)
        self.tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        self.pid = None
        self.out = self.threads_out = None

    def start(self, run_dir: Path, pid: int, now: float) -> None:
        self.stop()
        if self.every <= 0:
            return
        try:
            self.out = (Path(run_dir) / "timeline.csv").open("w", buffering=1)
            self.threads_out = (Path(run_dir) / "threads.csv").open("w", buffering=1)
        except OSError:
            self.out = self.threads_out = None
            return
        self.out.write(TIMELINE_HEAD)
        self.threads_out.write("unix_s,name,threads,cores\n")
        self.pid, self.t0, self.last_at = pid, now, None
        self.prev = self._read_all()
        self.last_at = now

    def stop(self) -> None:
        for f in (self.out, self.threads_out):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        self.out = self.threads_out = None
        self.pid = None

    def _read_all(self) -> dict:
        d = {"cg": self.cg.read(), "threads": {}, "proc": None, "io": {}, "preempt": None}
        base = self.proc / str(self.pid)
        st = stat_times(_read(base / "stat"))
        d["proc"] = st[1] if st else None
        for task in (base / "task").glob("*") if (base / "task").is_dir() else []:
            t = stat_times(_read(task / "stat"))
            if t:
                d["threads"][task.name] = t
        for line in _read(base / "io").splitlines():
            k, _, v = line.partition(":")
            if k in ("read_bytes", "write_bytes") and v.strip().isdigit():
                d["io"][k] = int(v)
        for line in _read(base / "task" / str(self.pid) / "status").splitlines():
            if line.startswith("nonvoluntary_ctxt_switches:"):
                d["preempt"] = int(line.split()[1])
        statm = _read(base / "statm").split()
        d["rss"] = int(statm[1]) * self.page if len(statm) > 1 and statm[1].isdigit() else None
        return d

    def sample(self, now: float) -> None:
        if self.out is None or self.last_at is None or now - self.last_at < self.every:
            return
        cur, prev, dt = self._read_all(), self.prev, now - self.last_at
        self.prev, self.last_at = cur, now
        cg, pcg = cur["cg"], prev["cg"]
        cores = throttled = mem = None
        if cg and pcg:
            cores = (cg["cpu_s"] - pcg["cpu_s"]) / dt
            periods = cg["periods"] - pcg["periods"]
            throttled = 100.0 * (cg["throttled"] - pcg["throttled"]) / periods if periods > 0 else 0.0
            mem = cg["mem"] / 2 ** 30 if cg.get("mem") is not None else None
        root = self.cg.root
        cpu_p = pressure(_read(root / "cpu.pressure"))
        io_p = pressure(_read(root / "io.pressure"))
        mem_p = pressure(_read(root / "memory.pressure"))
        engine = None
        if cur["proc"] is not None and prev["proc"] is not None:
            engine = (cur["proc"] - prev["proc"]) / self.tick / dt
        by_tid = {}
        for tid, (name, ticks) in cur["threads"].items():
            before = prev["threads"].get(tid)
            by_tid[tid] = (name, (ticks - before[1]) / self.tick / dt if before else 0.0)
        main = by_tid.get(str(self.pid), (None, None))[1]
        preempt = None
        if cur["preempt"] is not None and prev["preempt"] is not None:
            preempt = (cur["preempt"] - prev["preempt"]) / dt
        names = {}
        for name, c in by_tid.values():
            n, total = names.get(name, (0, 0.0))
            names[name] = (n + 1, total + c)
        top = sorted(names.items(), key=lambda kv: -kv[1][1])[:4]
        io = {k: (cur["io"].get(k, 0) - prev["io"].get(k, 0)) / dt / 1e6 for k in ("read_bytes", "write_bytes")}

        def f(v, fmt="%.3f"):
            return "" if v is None else fmt % v
        unix = int(time.time())
        self.out.write(",".join([
            str(unix), "%.1f" % (now - self.t0), f(cores), f(throttled, "%.1f"), f(mem),
            f(cpu_p.get("some"), "%.2f"), f(io_p.get("some"), "%.2f"), f(io_p.get("full"), "%.2f"),
            f(mem_p.get("some"), "%.2f"), f(engine), f(main), f(preempt, "%.1f"), str(len(by_tid)),
            f(cur["rss"] / 2 ** 30 if cur.get("rss") else None), f(io["read_bytes"], "%.2f"),
            f(io["write_bytes"], "%.2f"), ";".join("%s=%.2f" % (n.replace(",", " ").replace(";", " "), c)
                                                   for n, (_, c) in top),
        ]) + "\n")
        for name, (n, c) in sorted(names.items()):
            if c >= 0.01:
                self.threads_out.write("%d,%s,%d,%.3f\n" % (unix, name.replace(",", " "), n, c))
