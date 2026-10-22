"""cgroup_perf.py: the container's CPU, throttling and memory from fake cgroup files."""

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("RJM_BIN", "/opt/rjm-bar-host/bin"))
import cgroup_perf as cp  # noqa: E402


class FakeV2:
    """A cgroup v2 directory whose counters the test advances."""

    def __init__(self, root: Path, quota="600000 100000"):
        self.root = root
        (root / "cgroup.controllers").write_text("cpu memory\n")
        (root / "cpu.max").write_text(quota + "\n")
        self.cpu_s = self.periods = self.throttled = 0
        self.throttled_s = 0.0
        self.mem = 1 << 30
        self.write()

    def write(self):
        (self.root / "cpu.stat").write_text(
            "usage_usec %d\nuser_usec 0\nsystem_usec 0\nnr_periods %d\nnr_throttled %d\nthrottled_usec %d\n"
            % (self.cpu_s * 1e6, self.periods, self.throttled, self.throttled_s * 1e6))
        (self.root / "memory.current").write_text("%d\n" % self.mem)

    def run(self, cores, seconds=1, throttled=0, mem=None):
        self.cpu_s += cores * seconds
        self.periods += 10 * seconds
        self.throttled += throttled
        self.throttled_s += throttled * 0.01
        if mem is not None:
            self.mem = mem
        self.write()


class V2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "cg").mkdir()
        self.cg = FakeV2(self.dir / "cg")
        self.summary = self.dir / "out" / "perf-summary.csv"
        self.rec = cp.PerfRecorder(cp.Cgroup(self.dir / "cg"), summary_csv=self.summary)
        self.t = 100.0

    def tick(self, cores, throttled=0, mem=None):
        self.cg.run(cores, throttled=throttled, mem=mem)
        self.t += 1
        self.rec.sample(self.t)

    def test_a_game_window_is_summarised(self):
        self.rec.sample(self.t)
        run = self.dir / "run"
        run.mkdir()
        self.rec.start("game 1", run, now=self.t)
        for c in [2.0] * 20 + [5.0] * 10 + [1.0] * 10:
            self.tick(c)
        self.tick(3.0, throttled=4, mem=7 << 30)
        s = self.rec.stop()
        self.assertEqual(s["seconds"], 41)
        self.assertAlmostEqual(s["cores_avg"], (40 + 50 + 10 + 3) / 41, places=2)
        self.assertEqual(s["cores_peak_1s"], 5.0)
        self.assertEqual(s["cores_peak_10s"], 5.0)
        self.assertEqual(s["cores_p95"], 5.0)
        self.assertEqual(s["limit_cores"], 6.0)
        self.assertAlmostEqual(s["throttled_pct"], 100 * 4 / 410, places=2)
        self.assertEqual(s["mem_peak_gib"], 7.0)
        with (run / "perf.csv").open() as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["t_s", "cores", "mem_bytes", "throttled_periods"])
        self.assertEqual(len(rows), 42)
        with self.summary.open() as f:
            summary = list(csv.DictReader(f))
        self.assertEqual([r["label"] for r in summary], ["game 1"])
        self.assertIn("throttled 0.98%", cp.describe(s))

    def test_samples_outside_a_window_are_not_counted(self):
        self.rec.sample(self.t)
        self.tick(8.0)
        self.rec.start("game 2", now=self.t)
        self.tick(1.0)
        self.assertEqual(self.rec.stop()["cores_peak_1s"], 1.0)

    def test_summaries_append(self):
        for label in ("a", "b"):
            self.rec.sample(self.t)
            self.rec.start(label, now=self.t)
            self.tick(1.0)
            self.rec.stop()
        with self.summary.open() as f:
            self.assertEqual([r["label"] for r in csv.DictReader(f)], ["a", "b"])

    def test_no_quota_is_no_limit(self):
        (self.dir / "cg" / "cpu.max").write_text("max 100000\n")
        self.assertIsNone(cp.Cgroup(self.dir / "cg").limit_cores())


class V1(unittest.TestCase):
    def test_v1_counters_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cpu,cpuacct").mkdir()
            (root / "memory").mkdir()
            (root / "cpu,cpuacct" / "cpuacct.usage").write_text("2500000000\n")
            (root / "cpu,cpuacct" / "cpu.stat").write_text("nr_periods 30\nnr_throttled 3\nthrottled_time 50000000\n")
            (root / "cpu,cpuacct" / "cpu.cfs_quota_us").write_text("1600000\n")
            (root / "cpu,cpuacct" / "cpu.cfs_period_us").write_text("100000\n")
            (root / "memory" / "memory.usage_in_bytes").write_text("%d\n" % (3 << 30))
            cg = cp.Cgroup(root)
            self.assertFalse(cg.v2)
            c = cg.read()
            self.assertEqual((c["cpu_s"], c["periods"], c["throttled"], c["throttled_s"], c["mem"]),
                             (2.5, 30, 3, 0.05, 3 << 30))
            self.assertEqual(cg.limit_cores(), 16.0)

    def test_nothing_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = cp.PerfRecorder(cp.Cgroup(Path(tmp)))
            rec.sample(1.0)
            rec.start("x", now=1.0)
            rec.sample(2.0)
            self.assertEqual(rec.stop()["seconds"], 0)


if __name__ == "__main__":
    unittest.main()
