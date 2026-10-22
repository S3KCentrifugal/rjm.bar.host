#!/usr/bin/env python3
"""The container's own CPU and memory use, from its cgroup, for sizing.

The kernel keeps running totals for every cgroup: CPU time used, how often
the CPU quota throttled it, and memory in use (and its peak). Reading those few
small files once a second costs microseconds, so this can run for the life of
the container without affecting what it measures.

    rec = PerfRecorder()           # finds cgroup v2 or v1 under /sys/fs/cgroup
    rec.sample(now)                # call about once a second
    rec.start("game 3", run_dir)   # per-second samples go to run_dir/perf.csv
    summary = rec.stop()           # dict; one row added to the summary CSV

Each summary has the window's duration, CPU cores used (average, p50, p95,
the highest 1-second and 10-second averages), the share of CPU periods that
were throttled and the time lost to it, and peak memory. For sizing: the
request near the p95, the limit above the 10-second peak, and throttling near
zero.
"""

from __future__ import annotations

import csv
import datetime as dt
import math
from pathlib import Path

ROOT = Path("/sys/fs/cgroup")


def _read(path: Path) -> "str | None":
    try:
        return path.read_text()
    except OSError:
        return None


def _kv(text: "str | None") -> dict:
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            out[parts[0]] = int(parts[1])
    return out


class Cgroup:
    """Reads one container's counters, cgroup v2 or v1. ``read()`` returns
    cumulative CPU seconds, CPU periods and throttled periods, throttled
    seconds, and memory bytes, or None when nothing is readable."""

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self.v2 = (self.root / "cgroup.controllers").exists()

    def limit_cores(self) -> "float | None":
        if self.v2:
            text = (_read(self.root / "cpu.max") or "").split()
            if len(text) == 2 and text[0] != "max":
                return int(text[0]) / int(text[1])
            return None
        quota = _read(self.root / "cpu" / "cpu.cfs_quota_us") or _read(self.root / "cpu,cpuacct" / "cpu.cfs_quota_us")
        period = _read(self.root / "cpu" / "cpu.cfs_period_us") or _read(self.root / "cpu,cpuacct" / "cpu.cfs_period_us")
        if quota and period and int(quota) > 0:
            return int(quota) / int(period)
        return None

    def read(self) -> "dict | None":
        if self.v2:
            stat = _kv(_read(self.root / "cpu.stat"))
            if "usage_usec" not in stat:
                return None
            mem = _read(self.root / "memory.current")
            peak = _read(self.root / "memory.peak")
            return {"cpu_s": stat["usage_usec"] / 1e6, "periods": stat.get("nr_periods", 0),
                    "throttled": stat.get("nr_throttled", 0), "throttled_s": stat.get("throttled_usec", 0) / 1e6,
                    "mem": int(mem) if mem and mem.strip().isdigit() else None,
                    "mem_peak": int(peak) if peak and peak.strip().isdigit() else None}
        for cpu_dir in ("cpu,cpuacct", "cpuacct", "cpu"):
            usage = _read(self.root / cpu_dir / "cpuacct.usage")
            if usage and usage.strip().isdigit():
                break
        else:
            return None
        stat = {}
        for cpu_dir in ("cpu,cpuacct", "cpu"):
            stat = _kv(_read(self.root / cpu_dir / "cpu.stat"))
            if stat:
                break
        mem = _read(self.root / "memory" / "memory.usage_in_bytes")
        peak = _read(self.root / "memory" / "memory.max_usage_in_bytes")
        return {"cpu_s": int(usage) / 1e9, "periods": stat.get("nr_periods", 0),
                "throttled": stat.get("nr_throttled", 0), "throttled_s": stat.get("throttled_time", 0) / 1e9,
                "mem": int(mem) if mem and mem.strip().isdigit() else None,
                "mem_peak": int(peak) if peak and peak.strip().isdigit() else None}


def _pct(values: list, q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


SUMMARY_FIELDS = ["ended", "label", "seconds", "cores_avg", "cores_p50", "cores_p95", "cores_peak_1s",
                  "cores_peak_10s", "limit_cores", "throttled_pct", "throttled_s", "mem_peak_gib"]


class PerfRecorder:
    """Samples a Cgroup about once a second; summarises windows (games)."""

    def __init__(self, cgroup: "Cgroup | None" = None, summary_csv: "Path | None" = None):
        self.cg = cgroup or Cgroup()
        self.summary_csv = summary_csv
        self.last = None            # (now, counters) of the previous sample
        self.window = None          # the open window, if any
        self.last_summary = None

    def sample(self, now: float) -> None:
        c = self.cg.read()
        if c is None:
            return
        prev, self.last = self.last, (now, c)
        if prev is None or now <= prev[0]:
            return
        dt_s = now - prev[0]
        cores = max(0.0, (c["cpu_s"] - prev[1]["cpu_s"]) / dt_s)
        w = self.window
        if w is None:
            return
        w["cores"].append(cores)
        w["periods"] += max(0, c["periods"] - prev[1]["periods"])
        w["throttled"] += max(0, c["throttled"] - prev[1]["throttled"])
        w["throttled_s"] += max(0.0, c["throttled_s"] - prev[1]["throttled_s"])
        if c["mem"] is not None:
            w["mem_peak"] = max(w["mem_peak"], c["mem"])
        if w["out"] is not None:
            w["out"].write("%.1f,%.3f,%s,%d\n" % (now - w["start"], cores,
                                                  c["mem"] if c["mem"] is not None else "",
                                                  c["throttled"] - prev[1]["throttled"]))
            w["unflushed"] += 1
            if w["unflushed"] >= 30:
                w["out"].flush()
                w["unflushed"] = 0

    def start(self, label: str, run_dir: "Path | None" = None, now: "float | None" = None) -> None:
        self.stop()
        out = None
        if run_dir is not None:
            try:
                out = (Path(run_dir) / "perf.csv").open("w")
                out.write("t_s,cores,mem_bytes,throttled_periods\n")
            except OSError:
                out = None
        self.window = {"label": label, "start": now if now is not None else (self.last[0] if self.last else 0.0),
                       "cores": [], "periods": 0, "throttled": 0, "throttled_s": 0.0, "mem_peak": 0,
                       "out": out, "unflushed": 0}

    def stop(self) -> "dict | None":
        w, self.window = self.window, None
        if w is None:
            return None
        if w["out"] is not None:
            w["out"].close()
        cores = w["cores"]
        peak10 = max((sum(cores[i:i + 10]) / len(cores[i:i + 10]) for i in range(max(1, len(cores) - 9))),
                     default=0.0) if cores else 0.0
        s = {"ended": dt.datetime.now().isoformat(timespec="seconds"), "label": w["label"],
             "seconds": len(cores), "cores_avg": round(sum(cores) / len(cores), 2) if cores else 0.0,
             "cores_p50": round(_pct(cores, 0.50), 2), "cores_p95": round(_pct(cores, 0.95), 2),
             "cores_peak_1s": round(max(cores, default=0.0), 2), "cores_peak_10s": round(peak10, 2),
             "limit_cores": self.cg.limit_cores(),
             "throttled_pct": round(100.0 * w["throttled"] / w["periods"], 2) if w["periods"] else 0.0,
             "throttled_s": round(w["throttled_s"], 1),
             "mem_peak_gib": round(w["mem_peak"] / 2 ** 30, 2)}
        self.last_summary = s
        if self.summary_csv is not None:
            try:
                new = not Path(self.summary_csv).exists()
                Path(self.summary_csv).parent.mkdir(parents=True, exist_ok=True)
                with open(self.summary_csv, "a", newline="") as f:
                    wr = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
                    if new:
                        wr.writeheader()
                    wr.writerow(s)
            except OSError:
                pass
        return s


def describe(s: dict) -> str:
    """One line for a log or a PM."""
    limit = ("/%g" % s["limit_cores"]) if s.get("limit_cores") else ""
    return ("%s: %d s; cores avg %.2f, p95 %.2f, peak %.2f (10 s) %.2f (1 s)%s; throttled %.2f%% "
            "(%.1f s); memory peak %.2f GiB" % (s["label"], s["seconds"], s["cores_avg"], s["cores_p95"],
                                                s["cores_peak_10s"], s["cores_peak_1s"], limit,
                                                s["throttled_pct"], s["throttled_s"], s["mem_peak_gib"]))


if __name__ == "__main__":
    # A quick look: one second of the container's own use.
    import sys
    import time
    rec = PerfRecorder()
    if rec.cg.read() is None:
        print("no cgroup counters readable under %s" % ROOT)
        sys.exit(1)
    rec.sample(time.monotonic())
    rec.start("now")
    time.sleep(1)
    rec.sample(time.monotonic())
    print(describe(rec.stop()))
