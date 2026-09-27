"""lan_game.py: game spec validation and the host/client start scripts."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.environ.get("RJM_BIN", "/opt/rjm-bar-host/bin"))
import lan_game  # noqa: E402

AI_VS_AI = {
    "map": "All That Glitters v2.2.3",
    "players": [{"name": "BoxA", "password": "a1", "spectator": True},
                {"name": "BoxB", "password": "b1", "spectator": True}],
    "teams": [{"allyTeam": 0, "ai": {"shortName": "SMRTBARb", "host": "BoxA"}, "handicap": 50},
              {"allyTeam": 1, "ai": {"shortName": "BARb", "version": "stable", "host": "BoxB"}}],
}
HUMANS_VS_AI = {
    "map": "All That Glitters v2.2.3",
    "players": [{"name": "Alice", "password": "p1"}, {"name": "Bob", "password": "p2"}],
    "teams": [{"allyTeam": 0, "player": "Alice"}, {"allyTeam": 0, "player": "Bob"},
              {"allyTeam": 1, "ai": {"shortName": "SMRTBARb", "host": "host"}}],
}


def section(script, name):
    m = re.search(r"\[" + name + r"\]\s*\{(.*?)\n\t\}", script, re.S)
    return m.group(1) if m else ""


def with_(spec, **changes):
    s = {k: (list(v) if isinstance(v, list) else v) for k, v in spec.items()}
    s.update(changes)
    return s


class Validation(unittest.TestCase):
    def rejects(self, spec, fragment):
        with self.assertRaises(lan_game.SpecError) as cm:
            lan_game.validate(spec)
        self.assertIn(fragment, str(cm.exception))

    def test_valid_specs(self):
        lan_game.validate(AI_VS_AI)
        lan_game.validate(HUMANS_VS_AI)

    def test_map_required(self):
        self.rejects(with_(AI_VS_AI, map=""), "map is required")

    def test_team_needs_exactly_one_controller(self):
        self.rejects(with_(AI_VS_AI, teams=[{"allyTeam": 0}, AI_VS_AI["teams"][1]]), "exactly one")

    def test_ai_host_must_exist(self):
        self.rejects(with_(AI_VS_AI, teams=[{"allyTeam": 0, "ai": {"shortName": "X", "host": "Nobody"}},
                                           AI_VS_AI["teams"][1]]), "neither")

    def test_spectator_cannot_play(self):
        self.rejects(with_(AI_VS_AI, teams=[{"allyTeam": 0, "player": "BoxA"}, AI_VS_AI["teams"][1]]),
                     "spectator")

    def test_player_needs_a_team(self):
        self.rejects(with_(HUMANS_VS_AI, teams=[HUMANS_VS_AI["teams"][0], HUMANS_VS_AI["teams"][2]]),
                     "plays no team")

    def test_names_and_passwords(self):
        self.rejects(with_(AI_VS_AI, players=[{"name": "bad name", "password": "x"}]), "player name")
        self.rejects(with_(AI_VS_AI, players=[{"name": "A", "password": "has space"}]), "password")
        self.rejects(with_(AI_VS_AI, players=[{"name": "host", "password": "x"}]), "reserved")

    def test_allyteams_numbered_without_gaps(self):
        self.rejects(with_(AI_VS_AI, teams=[AI_VS_AI["teams"][0], dict(AI_VS_AI["teams"][1], allyTeam=2)]),
                     "without gaps")

    def test_observer_team_auto(self):
        self.assertTrue(lan_game.validate(AI_VS_AI)["observerTeam"])        # no humans playing
        self.assertFalse(lan_game.validate(HUMANS_VS_AI)["observerTeam"])   # humans playing
        self.assertTrue(lan_game.validate(with_(HUMANS_VS_AI, observerTeam=True))["observerTeam"])


class HostScript(unittest.TestCase):
    def test_ai_runs_in_the_named_participant(self):
        s = lan_game.host_script(lan_game.validate(AI_VS_AI))
        self.assertIn("Name=LanHost;", section(s, "PLAYER0"))
        self.assertIn("Name=BoxA;", section(s, "PLAYER1"))
        self.assertIn("Host=1;", section(s, "AI0"))        # BoxA runs SMRTBARb
        self.assertIn("Host=2;", section(s, "AI1"))        # BoxB runs BARb
        self.assertIn("ShortName=SMRTBARb;", section(s, "AI0"))
        self.assertIn("Version=stable;", section(s, "AI1"))

    def test_spectators_sit_on_the_observer_team(self):
        s = lan_game.host_script(lan_game.validate(AI_VS_AI))
        self.assertIn("NumTeams=3;", s)
        self.assertIn("NumAllyTeams=3;", s)
        for p in ("PLAYER0", "PLAYER1", "PLAYER2"):
            self.assertIn("Spectator=1;", section(s, p))
            self.assertIn("Team=2;", section(s, p))
        self.assertIn("AllyTeam=2;", section(s, "TEAM2"))

    def test_humans_lead_their_teams(self):
        s = lan_game.host_script(lan_game.validate(HUMANS_VS_AI))
        self.assertIn("NumTeams=3;", s)                     # no observer team: humans play
        self.assertIn("Spectator=0;", section(s, "PLAYER1"))
        self.assertIn("Team=0;", section(s, "PLAYER1"))
        self.assertIn("Team=1;", section(s, "PLAYER2"))
        self.assertIn("TeamLeader=1;", section(s, "TEAM0"))
        self.assertIn("Host=0;", section(s, "AI0"))         # the host runs the AI
        self.assertIn("Password=p1;", section(s, "PLAYER1"))

    def test_listens_everywhere_but_control_stays_local(self):
        s = lan_game.host_script(lan_game.validate(dict(AI_VS_AI, port=8460)))
        self.assertIn("HostIP=;", s)
        self.assertIn("HostPort=8460;", s)
        self.assertIn("AutohostIP=127.0.0.1;", s)
        self.assertIn("IsHost=1;", s)

    def test_handicap_and_modoptions(self):
        s = lan_game.host_script(lan_game.validate(dict(AI_VS_AI, modoptions={"coop": "0"})))
        self.assertIn("Handicap=50;", section(s, "TEAM0"))
        self.assertIn("coop=0;", s)
        self.assertIn("experimentallegionfaction=1;", s)


class ClientScript(unittest.TestCase):
    def test_client_script(self):
        s = lan_game.client_script("lan-host", 8452, "BoxA", "a1")
        for kv in ("HostIP=lan-host;", "HostPort=8452;", "IsHost=0;", "MyPlayerName=BoxA;", "MyPasswd=a1;"):
            self.assertIn(kv, s)


if __name__ == "__main__":
    unittest.main()
