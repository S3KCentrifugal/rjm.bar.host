"""engine_metrics.py: the engine's CPU, threads, disk and the container's
pressure, from fake /proc and cgroup files."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("RJM_BIN", "/opt/rjm-bar-host/bin"))
import cgroup_perf  # noqa: E402
import engine_metrics  # noqa: E402

PID = 4242


def stat_line(comm, ticks):
    # pid (comm) state ppid ... utime stime: fields 14 and 15 overall
    return "%d (%s) R 1 1 1 0 -1 0 0 0 0 0 %d 0 0 0 20 0 1 0" % (PID, comm, ticks)


class Fake:
    def __init__(self, root: Path):
        self.root = root
        self.cg = root / "cg"
        self.cg.mkdir()
        (self.cg / "cgroup.controllers").write_text("cpu memory io\n")
        self.proc = root / "proc"
        self.task = self.proc / str(PID) / "task"
        self.task.mkdir(parents=True)
        self.set(0, {PID: ("spring-headless", 0), 4243: ("ThreadPool", 0), 4244: ("ThreadPool", 0)}, 0, 0, 0)

    def set(self, cpu_s, threads, read, write, preempt, psi_io=0.0):
        (self.cg / "cpu.stat").write_text("usage_usec %d\nnr_periods 100\nnr_throttled 0\nthrottled_usec 0\n"
                                          % (cpu_s * 1e6))
        (self.cg / "memory.current").write_text(str(3 * 2 ** 30))
        (self.cg / "cpu.pressure").write_text("some avg10=1.50 avg60=0.00 avg300=0.00 total=1\n")
        (self.cg / "io.pressure").write_text("some avg10=%.2f avg60=0 avg300=0 total=1\n"
                                             "full avg10=0.25 avg60=0 avg300=0 total=1\n" % psi_io)
        total = sum(t for _, t in threads.values())
        (self.proc / str(PID) / "stat").write_text(stat_line("spring-headless", total))
        (self.proc / str(PID) / "statm").write_text("1000 262144 0 0 0 0 0\n")
        (self.proc / str(PID) / "io").write_text("rchar: 1\nread_bytes: %d\nwrite_bytes: %d\n" % (read, write))
        for tid, (name, ticks) in threads.items():
            d = self.task / str(tid)
            d.mkdir(exist_ok=True)
            (d / "stat").write_text(stat_line(name, ticks))
        (self.task / str(PID) / "status").write_text("Name:\tspring-headless\nnonvoluntary_ctxt_switches:\t%d\n"
                                                     % preempt)


class HostSampler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fake = Fake(root)
        self.run = root / "run"
        self.run.mkdir()
        self.s = engine_metrics.HostSampler(every=5, cgroup=cgroup_perf.Cgroup(self.fake.cg), proc=self.fake.proc)
        self.s.tick = 100

    def tearDown(self):
        self.s.stop()
        self.tmp.cleanup()

    def rows(self, name="timeline.csv"):
        lines = (self.run / name).read_text().splitlines()
        head = lines[0].split(",")
        return [dict(zip(head, ln.split(","))) for ln in lines[1:]]

    def test_one_line_per_interval_with_the_main_thread_apart(self):
        self.s.start(self.run, PID, now=100.0)
        self.fake.set(10, {PID: ("spring-headless", 500), 4243: ("ThreadPool", 200), 4244: ("ThreadPool", 100)},
                      0, 50 * 10 ** 6, 25, psi_io=12.5)
        self.s.sample(103.0)                     # under the interval: nothing
        self.assertEqual(self.rows(), [])
        self.s.sample(105.0)
        row = self.rows()[0]
        self.assertEqual(float(row["cores"]), 2.0)               # 10 s of CPU in 5 s
        self.assertEqual(float(row["main_thread_cores"]), 1.0)   # 500 ticks = 5 s in 5 s
        self.assertEqual(float(row["engine_cores"]), 1.6)
        self.assertEqual(float(row["main_preempt_per_s"]), 5.0)
        self.assertEqual(float(row["write_mb_s"]), 10.0)
        self.assertEqual((row["io_pressure_pct"], row["io_full_pct"], row["cpu_pressure_pct"]),
                         ("12.50", "0.25", "1.50"))
        self.assertEqual(row["engine_threads"], "3")
        self.assertEqual(float(row["engine_rss_gib"]), 1.0)       # 262144 pages of 4 KiB
        self.assertEqual(row["top_threads"], "spring-headless=1.00;ThreadPool=0.60")
        threads = self.rows("threads.csv")
        self.assertEqual({(t["name"], t["threads"]) for t in threads}, {("spring-headless", "1"), ("ThreadPool", "2")})

    def test_a_gone_engine_writes_blanks_not_errors(self):
        self.s.start(self.run, PID, now=0.0)
        import shutil
        shutil.rmtree(self.fake.proc / str(PID))
        self.s.sample(5.0)
        row = self.rows()[0]
        self.assertEqual((row["engine_cores"], row["main_thread_cores"]), ("", ""))

    def test_off(self):
        s = engine_metrics.HostSampler(every=0, cgroup=cgroup_perf.Cgroup(self.fake.cg), proc=self.fake.proc)
        s.start(self.run, PID, now=0.0)
        s.sample(10.0)
        self.assertFalse((self.run / "timeline.csv").exists())

    def test_names_with_spaces_and_parentheses(self):
        self.assertEqual(engine_metrics.stat_times("1 (a (b) c) S 0 0 0 0 0 0 0 0 0 0 7 3 0"), ("a (b) c", 10))


if __name__ == "__main__":
    unittest.main()
