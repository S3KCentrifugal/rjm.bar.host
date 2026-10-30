"""game_archive.py and demo_stats.py: a finished run dir into the archive."""

import gzip
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("RJM_BIN", "/opt/rjm-bar-host/bin"))
import demo_stats  # noqa: E402
import game_archive  # noqa: E402

GAME_ID = bytes(range(1, 17))


def make_demo(path: Path, winners=(1,), teams=2, samples=3) -> Path:
    """A minimal v5 demo: header | script | stream | winners | player stats |
    team stats (DemoRecorder.cpp:55-57)."""
    script = b"[game]{mapname=Test Map;}"
    stream = b"\0" * 40
    win = bytes(winners)
    pstats = b"\1" * 20                              # one player
    tstat = struct.calcsize(demo_stats.TEAM_STAT)
    counts = struct.pack("<%dI" % teams, *([samples] * teams))
    records = b""
    for t in range(teams):
        for i in range(samples):
            records += struct.pack(demo_stats.TEAM_STAT, 450 * (i + 1), *([float(t + 1)] * 12),
                                   *([t * 10 + i] * 7))
    hsize = struct.calcsize(demo_stats.HEADER)
    header = struct.pack(demo_stats.HEADER, b"spring demofile\0", 5, hsize, b"2026.07.04", GAME_ID,
                         1793000000, len(script), len(stream), 1234, 1300, 1, len(pstats), 20, teams,
                         len(counts) + len(records), tstat, 15, len(win))
    with gzip.open(path, "wb") as f:
        f.write(header + script + stream + win + pstats + counts + records)
    return path


class DemoStats(unittest.TestCase):
    def test_reads_past_the_winners(self):
        with tempfile.TemporaryDirectory() as d:
            demo = demo_stats.read_demo(make_demo(Path(d) / "a.sdfz", winners=(1, 2)))
        self.assertEqual(demo["winningAllyTeams"], [1, 2])
        self.assertEqual(demo["gameId"], GAME_ID.hex())
        self.assertEqual([t["samples"] for t in demo["teams"]], [3, 3])
        self.assertEqual(demo["teams"][1]["frame"], 1350)            # the last sample, read in step
        self.assertEqual(demo["teams"][1]["unitsKilled"], 12)

    def test_no_winners(self):
        with tempfile.TemporaryDirectory() as d:
            demo = demo_stats.read_demo(make_demo(Path(d) / "a.sdfz", winners=()))
        self.assertEqual(demo["winningAllyTeams"], [])
        self.assertEqual(demo["teams"][0]["unitsKilled"], 2)


class Archive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.work, self.root = base / "work", base / "archive"
        self.run = self.work / "20261023-180000-3298"
        (self.run / "demos").mkdir(parents=True)
        (self.run / "AI" / "Skirmish" / "SMRTBARb" / "stable").mkdir(parents=True)
        (self.run / "cache").mkdir()
        make_demo(self.run / "demos" / "20261023_180010_Test Map_2026.07.04.sdfz")
        (self.run / "game.json").write_text(json.dumps({
            "startedUtc": "2026-10-23T18:00:00.000000Z", "battle": "3298", "founder": "Host[US4][001]",
            "title": "Human vs SMRTBARb 50+", "map": "All That Smolders v1.2", "game": "BAR test-1",
            "players": [{"name": "Alice"}, {"name": "[SMRT]Clyde"}], "bots": [{"name": "b1"}, {"name": "b2"}],
            "ai": {"channel": "smrt-prod", "name": "SMRTBARb", "release": "v1.1153-prod"}}))
        (self.run / "ended.json").write_text(json.dumps({"endedUtc": "2026-10-23T18:40:00Z", "engineExit": 0}))
        (self.run / "infolog.txt").write_text("[t=00:00:01.000000][f=0000001] hello\n" * 100)
        (self.run / "engine-console.log").write_text("console\n")
        (self.run / "script.txt").write_text("[GAME]\n{\n\tHostIP=1.2.3.4;\n\tMyPasswd=s3cret;\n}\n")
        (self.run / "timeline.csv").write_text("unix_s,cores,behind_s\n1,2.0,0.5\n2,3.0,11.0\n")
        (self.run / "engine-timeline.csv").write_text("unix_s,behind_s\n1,0.2\n2,11.4\n")
        (self.run / "AI" / "Skirmish" / "SMRTBARb" / "stable" / "circuit.log").write_text("ai log\n")
        (self.run / "cache" / "big.bin").write_bytes(b"x" * 1000)

    def tearDown(self):
        self.tmp.cleanup()

    def archive(self, **kw):
        return game_archive.archive_run(self.run, self.root, "bar-lobbies.clyde", **kw)

    def test_layout_and_manifest(self):
        folder = self.archive()
        self.assertEqual(folder, self.root / "2026" / "10" / "23" /
                         "20261023-180000Z_bar-lobbies.clyde_All-That-Smolders-v1.2_01020304")
        names = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
        self.assertEqual(names, [
            "config/script.txt", "files/AI/Skirmish/SMRTBARb/stable/circuit.log.gz",
            "lobby.json", "logs/engine-console.log.gz", "logs/infolog.txt.gz", "manifest.json",
            "metrics/engine-timeline.csv", "metrics/timeline.csv",
            "replay/20261023_180010_Test Map_2026.07.04.sdfz"])
        m = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(m["schema"], "rjm-bar-archive/game/1")
        self.assertEqual((m["gameId"], m["outcome"], m["winningAllyTeams"]), (GAME_ID.hex(), "winner", [1]))
        self.assertEqual(m["players"], ["Alice", "[SMRT]Clyde"])
        self.assertEqual(m["ai"]["release"], "v1.1153-prod")
        self.assertEqual(m["lag"]["behind_s"]["max"], 11.4)
        self.assertEqual(m["load"]["cores"]["max"], 3.0)
        self.assertEqual(len(m["files"]), 8)
        self.assertFalse(self.run.exists())                         # moved off the work disk

    def test_the_widget_files_under_luaui_config_are_placed(self):
        """BAR's sandbox keeps user widgets' writes under LuaUI/Config/; the
        AIs' own files there stay under files/."""
        (self.run / "engine-timeline.csv").unlink()
        cfg = self.run / "LuaUI" / "Config"
        (cfg / "BARb").mkdir(parents=True)
        (cfg / "rjm-engine-timeline.csv").write_text("unix_s,behind_s\n1,0.5\n2,42.0\n")
        (cfg / "rjm-engine-events.csv").write_text("unix_s,game_frame,event,detail\n1,0,game_start,\n")
        (cfg / "rjm-engine-profile.csv").write_text("unix_s,game_frame,frames,wall_ms,timer,ms\n1,150,150,5000,AI,2500\n")
        (cfg / "BARb" / "map.txt").write_text("map\n")
        folder = self.archive()
        names = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
        for name in ("metrics/engine-timeline.csv", "metrics/engine-events.csv", "metrics/engine-profile.csv",
                     "files/LuaUI/Config/BARb/map.txt.gz"):
            self.assertIn(name, names)
        self.assertFalse([n for n in names if "rjm-engine" in n])
        m = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(m["lag"]["behind_s"]["max"], 42.0)

    def test_password_is_removed(self):
        folder = self.archive()
        script = (folder / "config" / "script.txt").read_text()
        self.assertNotIn("s3cret", script)
        self.assertIn("MyPasswd=<removed>;", script)

    def test_logs_are_gzipped_and_whole(self):
        folder = self.archive()
        with gzip.open(folder / "logs" / "infolog.txt.gz", "rt") as f:
            self.assertEqual(len(f.read().splitlines()), 100)

    def test_index_line(self):
        self.archive()
        lines = (self.root / "_index" / "bar-lobbies.clyde.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        line = json.loads(lines[0])
        self.assertEqual((line["outcome"], line["players"], line["lagMaxS"]), ("winner", 2, 11.4))

    # The engine's game-end table (TimeProfiler.cpp PrintProfilingInfo), as in a
    # Clyde infolog of 2026-10-27, with lines after it.
    PROFILE = (
        "[t=00:33:06.914966][f=0056718] <Player> Allies: gg\n"
        "[t=00:33:08.348307][f=0056759]                                Part|        Total Time|Time of the last 0.5s\n"
        "[t=00:33:08.348331][f=0056759]                                 ???             0.00ms  0.00%\n"
        "[t=00:33:08.348343][f=0056759]                                  AI        133270.55ms  0.00%\n"
        "[t=00:33:08.348353][f=0056759] AI::{id=10 team=2 name=SMRTBARb version=stable}         27425.76ms  0.00%\n"
        "[t=00:33:08.348361][f=0056759] AI::{id=11 team=3 name=SMRTBARb version=stable}         30754.98ms  0.00%\n"
        "[t=00:33:08.348520][f=0056759]                                Draw        282197.62ms 19.10%\n"
        "[t=00:33:08.348600][f=0056759]                Lua::Callins::Synced         37687.96ms  1.60%\n"
        "[t=00:33:08.348610][f=0056759]              Lua::Callins::Unsynced         76342.91ms  3.40%\n"
        "[t=00:33:08.348700][f=0056759]             Misc::Profiler::AddTime        175103.75ms  8.40%\n"
        "[t=00:33:08.348800][f=0056759]                                 Sim        250632.62ms 12.40%\n"
        "[t=00:33:08.348850][f=0056759]                       Sim::Features          1353.77ms  0.00%\n"
        "[t=00:33:08.349000][f=0056759] [EOH::DestroySkirmishAI(id=0)]\n"
        "[t=00:33:08.349100][f=0056759] Skirmish AI <SMRTBARb-stable>: x 1.00ms 2.00%\n")

    def test_the_engines_game_end_profile_is_in_the_manifest_and_index(self):
        with (self.run / "infolog.txt").open("a") as f:
            f.write(self.PROFILE)
        m = json.loads((self.archive() / "manifest.json").read_text())
        p = m["profile"]
        self.assertEqual(len(p["timers"]), 10)                      # stops at the first other line
        self.assertEqual((p["simS"], p["aiS"], p["drawS"], p["profilerS"]), (250.633, 133.271, 282.198, 175.104))
        self.assertEqual(p["luaCallinsS"], 114.031)
        self.assertEqual(p["aiByTeamS"], {"2": 27.426, "3": 30.755})
        self.assertEqual(p["aiPctOfSim"], 53.2)
        line = json.loads((self.root / "_index" / "bar-lobbies.clyde.jsonl").read_text().splitlines()[0])
        self.assertEqual((line["simS"], line["aiPctOfSim"]), (250.633, 53.2))

    def test_no_profile_table_no_profile(self):
        m = json.loads((self.archive() / "manifest.json").read_text())
        self.assertEqual(m["profile"], {})

    def test_no_partial_folders_left(self):
        self.archive()
        self.assertEqual(list((self.root / "_incoming").iterdir()), [])

    def test_a_game_cut_short_is_interrupted(self):
        (self.run / "ended.json").unlink()
        m = json.loads((self.archive() / "manifest.json").read_text())
        self.assertEqual(m["outcome"], "interrupted")

    def test_without_a_replay_the_room_names_it(self):
        for p in (self.run / "demos").iterdir():
            p.unlink()
        folder = self.archive()
        self.assertTrue(folder.name.endswith("_room3298"))
        self.assertIsNone(json.loads((folder / "manifest.json").read_text())["replay"])

    def test_keep_and_archive_twice(self):
        first = self.archive(keep=True)
        self.assertTrue(self.run.exists())
        self.assertEqual(self.archive(), first)                     # already there: not copied again
        self.assertFalse(self.run.exists())
        self.assertEqual(len((self.root / "_index" / "bar-lobbies.clyde.jsonl").read_text().splitlines()), 1)

    def test_pending_archives_every_run_and_skips_dot_dirs(self):
        (self.work / ".engine-cache").mkdir()
        self.assertEqual(game_archive.main(["--pending", str(self.work), "--archive", str(self.root),
                                            "--source", "bar-lobbies.clyde"]), 0)
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), [".engine-cache"])


if __name__ == "__main__":
    unittest.main()
