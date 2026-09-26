"""Autopilot's lobby guard: which votes it refuses and which it lets through.

Runs the real plugin under a stub SPADS API (autopilot_harness.pl)."""

import os
import subprocess
import unittest
from pathlib import Path

HARNESS = Path(__file__).with_name("autopilot_harness.pl")


def decisions(commands, **env):
    e = dict(os.environ)
    e.update(env)
    r = subprocess.run(["perl", "-I/opt/spads", str(HARNESS)], input="\n".join(commands) + "\n",
                       env=e, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return dict(line.split("\t", 1) for line in r.stdout.splitlines())


class DefaultPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = decisions([
            "boss Troll", "boss", "kick Owner", "kickBan owner", "spec Owner", "force Owner spec",
            "ban Owner", "ring AutoHost", "hSet password pwned", "rehost", "quit", "preset other",
            "set autoAddBotNb 0", "set autoBalance off",
            "kick Troll", "set map Comet", "map Comet", "stop", "bSet coop 0", "start", "forceStart",
        ])

    def refused(self, cmd, reason_part):
        self.assertIn(reason_part, self.d[cmd], cmd)

    def allowed(self, cmd):
        self.assertEqual(self.d[cmd], "allowed", cmd)

    def test_boss_votes_are_refused_in_every_form(self):
        self.refused("boss Troll", "!boss votes are not allowed")
        self.refused("boss", "!boss votes are not allowed")      # unboss

    def test_the_owner_cannot_be_voted_out(self):
        for cmd in ("kick Owner", "kickBan owner", "spec Owner", "force Owner spec", "ban Owner"):
            self.refused(cmd, "is protected")

    def test_the_autohost_is_protected(self):
        self.refused("ring AutoHost", "is protected")

    def test_room_settings_cannot_be_voted(self):
        for cmd in ("hSet password pwned", "rehost", "quit", "preset other"):
            self.refused(cmd, "votes are not allowed")

    def test_only_the_map_setting_is_votable(self):
        self.refused("set autoAddBotNb 0", "not allowed")
        self.refused("set autoBalance off", "not allowed")
        self.allowed("set map Comet")
        self.allowed("map Comet")        # the alias

    def test_ordinary_votes_still_work(self):
        for cmd in ("kick Troll", "stop", "bSet coop 0", "start", "forceStart"):
            self.allowed(cmd)


class Configuration(unittest.TestCase):
    def test_protected_users_list(self):
        d = decisions(["kick Friend", "kick Troll"], AUTOPILOT_PROTECTED_USERS="Friend,Other")
        self.assertIn("is protected", d["kick Friend"])
        self.assertEqual(d["kick Troll"], "allowed")

    def test_trusted_level_protects_moderators_when_lowered(self):
        self.assertEqual(decisions(["kick Mod"])["kick Mod"], "allowed")
        self.assertIn("is protected", decisions(["kick Mod"], AUTOPILOT_TRUSTED_LEVEL="100")["kick Mod"])

    def test_deny_list_and_votable_settings_are_configurable(self):
        d = decisions(["stop", "boss Troll", "set autoAddBotNb 4"],
                      AUTOPILOT_DENY_VOTES="stop", AUTOPILOT_VOTABLE_SETTINGS="map,autoAddBotNb")
        self.assertIn("not allowed", d["stop"])
        self.assertEqual(d["boss Troll"], "allowed")   # commands.conf still blocks it, as a second layer
        self.assertEqual(d["set autoAddBotNb 4"], "allowed")


class CommandGate(unittest.TestCase):
    """preSpadsCommand: the layer a vote cannot skip, including SPADS's
    immediate execution when nobody else is eligible to vote."""

    @classmethod
    def setUpClass(cls):
        cls.d = decisions([
            "exec Troll force Owner spec", "exec Troll callVote spec Owner", "exec Troll callVote kick Owner",
            "exec Troll callVote set autoAddBotNb 0", "exec Troll callVote boss Troll", "exec Troll kick Owner",
            "exec Troll callVote map Comet", "exec Troll callVote stop", "exec Troll callVote kick Spammer",
            "exec Owner boss Friend", "exec Owner set autoAddBotNb 4", "exec AutoHost boss",
            "exec Mod callVote kick Owner",
        ])

    def test_takeovers_are_refused_however_they_arrive(self):
        for cmd in ("exec Troll force Owner spec", "exec Troll callVote spec Owner", "exec Troll callVote kick Owner",
                    "exec Troll callVote set autoAddBotNb 0", "exec Troll callVote boss Troll", "exec Troll kick Owner",
                    "exec Mod callVote kick Owner"):
            self.assertEqual(self.d[cmd], "refused", cmd)

    def test_ordinary_commands_pass(self):
        for cmd in ("exec Troll callVote map Comet", "exec Troll callVote stop", "exec Troll callVote kick Spammer"):
            self.assertEqual(self.d[cmd], "allowed", cmd)

    def test_owner_and_autohost_are_not_gated(self):
        for cmd in ("exec Owner boss Friend", "exec Owner set autoAddBotNb 4", "exec AutoHost boss"):
            self.assertEqual(self.d[cmd], "allowed", cmd)


class CommandsConf(unittest.TestCase):
    """The first layer: !boss is owner-only and cannot be voted at all."""

    def test_boss_is_owner_only_and_unvotable(self):
        text = Path("/opt/rjm-bar-host/spads/etc/commands.conf").read_text()
        section = text.split("[boss]", 1)[1].split("\n[", 1)[0]
        rules = [l for l in section.splitlines() if l and not l.startswith("#")]
        self.assertEqual(rules, ["::|130:"])


if __name__ == "__main__":
    unittest.main()
