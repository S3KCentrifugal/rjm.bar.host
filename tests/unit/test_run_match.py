"""run_match.py: GameSetup script generation and configuration checks."""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("RJM_BIN", "/opt/rjm-bar-host/bin"))
import run_match  # noqa: E402


def cfg(**kw):
    engine = Path(tempfile.mkdtemp()) / "spring-headless"
    engine.write_text("")
    base = dict(engine=str(engine), map_name="All That Glitters v2.2.3", ai_count=4, ally_teams=2,
                ai_shortname="SMRTBARb", modoptions=run_match.default_modoptions(""))
    base.update(kw)
    return run_match.Config(**base)


def section(script, name):
    m = re.search(r"\[" + name + r"\]\s*\{(.*?)\n\t\}", script, re.S)
    return m.group(1) if m else ""


class Script(unittest.TestCase):
    def test_host_is_a_spectator_hosting_every_ai(self):
        s = run_match.build_script(cfg())
        self.assertIn("Spectator=1;", section(s, "PLAYER0"))
        for i in range(4):
            ai = section(s, f"AI{i}")
            self.assertIn("Host=0;", ai)
            self.assertIn("ShortName=SMRTBARb;", ai)
            self.assertIn(f"Team={i};", ai)

    def test_interleaved_layout(self):
        s = run_match.build_script(cfg(team_layout="interleaved"))
        allies = [re.search(r"AllyTeam=(\d+);", section(s, f"TEAM{i}")).group(1) for i in range(4)]
        self.assertEqual(allies, ["0", "1", "0", "1"])

    def test_blocked_layout(self):
        s = run_match.build_script(cfg(team_layout="blocked"))
        allies = [re.search(r"AllyTeam=(\d+);", section(s, f"TEAM{i}")).group(1) for i in range(4)]
        self.assertEqual(allies, ["0", "0", "1", "1"])

    def test_handicap_on_every_team(self):
        s = run_match.build_script(cfg(ai_handicap=50))
        self.assertEqual(len(re.findall(r"Handicap=50;", s)), 4)

    def test_default_modoptions_and_removal(self):
        self.assertEqual(run_match.default_modoptions("")["experimentallegionfaction"], "1")
        self.assertNotIn("experimentallegionfaction",
                         run_match.default_modoptions("experimentallegionfaction="))
        self.assertEqual(run_match.default_modoptions("coop=0")["coop"], "0")

    def test_map_start_boxes_emit_no_rects(self):
        self.assertNotIn("StartRect", run_match.build_script(cfg(start_boxes="map")))

    def test_explicit_start_boxes(self):
        s = run_match.build_script(cfg(start_boxes="0,0,0.3,1;0.7,0,1,1"))
        self.assertIn("StartRectRight=0.3;", section(s, "ALLYTEAM0"))

    def test_start_boxes_count_must_match(self):
        with self.assertRaises(SystemExit):
            run_match.start_boxes(cfg(start_boxes="0,0,1,1"))

    def test_autohost_channel_is_loopback(self):
        s = run_match.build_script(cfg())
        self.assertIn("AutohostIP=127.0.0.1;", s)
        self.assertIn("HostIP=127.0.0.1;", s)


class Validation(unittest.TestCase):
    def test_map_required(self):
        with self.assertRaises(SystemExit):
            cfg(map_name="").validate()

    def test_ai_count_must_divide(self):
        with self.assertRaises(SystemExit):
            cfg(ai_count=3, ally_teams=2).validate()

    def test_engine_must_exist(self):
        with self.assertRaises(SystemExit):
            cfg(engine="/nonexistent/spring-headless").validate()

    def test_from_env_uses_the_image_engine_dir(self):
        d = Path(tempfile.mkdtemp())
        (d / "spring-headless").write_text("")
        with mock.patch.dict(os.environ, {"BAR_ENGINE_DIR": str(d), "BARHOST_MAP": "M"}, clear=False):
            c = run_match.Config.from_env()
        self.assertEqual(c.engine, str(d / "spring-headless"))


class EngineConfig(unittest.TestCase):
    def test_demos_recorded_and_workers_from_quota(self):
        out = Path(tempfile.mkdtemp()) / "springsettings.cfg"
        with mock.patch.object(run_match, "detect_cpu_quota", return_value=3.0):
            settings = run_match.write_engine_config(cfg(), out)
        self.assertEqual(settings["ServerRecordDemos"], "1")
        self.assertEqual(settings["WorkerThreadCount"], "3")
        self.assertIn("ServerRecordDemos = 1", out.read_text())


if __name__ == "__main__":
    unittest.main()
