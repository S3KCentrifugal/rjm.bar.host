"""lobby_client.py: the protocol state machine, driven with lobby lines and a fake clock."""

import os
import sys
import unittest

sys.path.insert(0, os.environ.get("RJM_BIN", "/opt/rjm-bar-host/bin"))
import lobby_client as lc  # noqa: E402

ME = "[SMRT]Clyde"
HOST = "Host[EU1][3]"
ENGINE = "2026.07.04"


def opened(bid, founder=HOST, passworded=0, engine=ENGINE, title="Empty room"):
    return ("BATTLEOPENED %s 0 0 %s 10.1.2.3 8452 16 %d 0 123 Recoil\t%s\tAll That Glitters v2.2.3\t%s\t"
            "Beyond All Reason test-29000-abc" % (bid, founder, passworded, engine, title))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Proc:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class Harness(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.launched = []
        self.proc = Proc()
        s = lc.Settings({"BARHOST_LOBBY_LOGIN": ME, "BARHOST_LOBBY_PASSWORD": "secret",
                         "BAR_ENGINE_VERSION": ENGINE, "BARHOST_LOBBY_CLUSTER": "EU",
                         "BARHOST_STATUS_FILE": "/tmp/test-lobby-status.json"})
        self.c = lc.LobbyClient(s, now=self.clock, launch=self.launch)

    def launch(self, battle, password):
        self.launched.append((battle.ip, battle.port, password))
        return self.proc

    def feed(self, *lines):
        for line in lines:
            self.c.feed(line)

    def take(self):
        out, self.c.outbox[:] = list(self.c.outbox), []
        return out

    def run_for(self, seconds, step=0.5):
        """Advance the clock, ticking; return everything sent."""
        out = []
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += step
            self.c.tick()
            out += self.take()
        return out

    def log_in(self):
        self.feed("TASSERVER 0.38 * 8201 0")
        login = self.take()
        self.feed("ACCEPTED " + ME,
                  "ADDUSER %s XX 0 1" % HOST,
                  "CLIENTSTATUS %s 64" % HOST,
                  opened("12"), "UPDATEBATTLEINFO 12 1 0 123 All That Glitters v2.2.3",
                  "LOGININFOEND")
        return login

    def join(self):
        self.log_in()
        sent = self.run_for(1)
        join = [x for x in sent if x.startswith("JOINBATTLE")]
        self.assertEqual(len(join), 1, sent)
        password = join[0].split(" ")[3]
        self.feed("JOINBATTLE 12 hash", "JOINEDBATTLE 12 %s %s" % (ME, password))
        return password

    def boss_and_setup(self):
        self.join()
        self.run_for(2)
        self.feed("SAIDBATTLEEX %s * Boss mode enabled for %s" % (HOST, ME))
        sent = self.run_for(40)
        self.c.fetched(self.c.battle, True)
        sent += self.run_for(3)
        return sent


class Login(Harness):
    def test_login_line_matches_chobby(self):
        login = self.log_in()
        self.assertEqual(len(login), 1)
        w = login[0].split(" ", 5)
        self.assertEqual(w[:5], ["LOGIN", ME, lc.password_hash("secret"), "0", "*"])
        self.assertTrue(w[5].startswith("LuaLobby "))
        self.assertTrue(w[5].endswith("\tb sp"))

    def test_password_hash_is_base64_md5(self):
        self.assertEqual(lc.password_hash("abc"), "kAFQmDzST7DWlj99KOF/cg==")

    def test_unaccepted_agreement_stops(self):
        self.feed("TASSERVER 0.38 * 8201 0", "AGREEMENT text")
        with self.assertRaises(ConnectionAbortedError):
            self.feed("AGREEMENTEND")

    def test_denied_stops(self):
        self.feed("TASSERVER 0.38 * 8201 0")
        with self.assertRaises(ConnectionAbortedError):
            self.feed("DENIED Bad username/password")


class RoomChoice(Harness):
    def candidates(self, *lines):
        self.feed("TASSERVER 0.38 * 8201 0", "ACCEPTED " + ME, *lines)
        sent = self.run_for(1)
        return [x.split(" ")[1] for x in sent if x.startswith("JOINBATTLE")]

    def test_skips_unsuitable_rooms(self):
        self.assertEqual(self.candidates(
            opened("1", founder="SomePlayer"),                    # not a cluster autohost
            opened("2", passworded=1),                            # passworded
            opened("3", engine="2025.06.12"),                     # other engine
            opened("4", founder="Host[US1][2]"),                  # other region
            opened("5"), "JOINEDBATTLE 5 Alice",                  # occupied
            opened("6"), "UPDATEBATTLEINFO 6 1 1 123 m",          # locked
            opened("7"), "CLIENTSTATUS %s 1" % HOST,              # in game
        ), [])

    def test_takes_an_empty_room(self):
        self.assertEqual(self.candidates(opened("9")), ["9"])


class Setup(Harness):
    def test_boss_then_options_bots_and_spectate(self):
        sent = self.boss_and_setup()
        said = [x[len("SAYBATTLE "):] for x in sent if x.startswith("SAYBATTLE ")]
        self.assertIn("!preset custom", said)
        self.assertIn("!bSet experimentallegionfaction 1", said)
        bots = [x.split(" ") for x in sent if x.startswith("ADDBOT")]
        self.assertEqual(len(bots), 8)
        statuses = [lc.unpack_battle_status(b[2]) for b in bots]
        self.assertEqual({s["ally"] for s in statuses}, {1})
        self.assertEqual(len({s["team"] for s in statuses}), 8)
        self.assertNotIn(0, {s["team"] for s in statuses})
        self.assertEqual([s["side"] for s in statuses], [0, 1] * 4)
        self.assertEqual({b[4] for b in bots}, {"BARb"})
        last = [lc.unpack_battle_status(x.split(" ")[1]) for x in sent if x.startswith("MYBATTLESTATUS")][-1]
        self.assertEqual((last["mode"], last["sync"]), (0, 1))

    def test_commands_are_spaced_for_flood_protection(self):
        self.join()
        self.feed("SAIDBATTLEEX %s * Boss mode enabled for %s" % (HOST, ME))
        sent = []
        times = []
        for _ in range(40):
            self.clock.t += 0.5
            self.c.tick()
            new = self.take()
            sent += new
            times += [self.clock.t] * len(new)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= lc.COMMAND_SPACING for g in gaps), gaps)

    def test_sets_up_without_boss_confirmation(self):
        self.join()
        sent = self.run_for(60)
        self.assertEqual(len([x for x in sent if x.startswith("ADDBOT")]), 8)

    def test_battle_status_round_trip(self):
        for kw in ({"ready": 1, "team": 20, "ally": 17, "mode": 1, "sync": 1, "side": 3},
                   {"team": 0, "ally": 1, "mode": 0, "sync": 2, "side": 1}):
            st = lc.unpack_battle_status(lc.battle_status(**kw))
            for k, v in kw.items():
                self.assertEqual(st[k], v, (k, kw))


class Starting(Harness):
    def human(self, name="Alice", ready=1, sync=1):
        self.feed("JOINEDBATTLE 12 " + name,
                  "CLIENTBATTLESTATUS %s %d 0" % (name, lc.battle_status(ready=ready, mode=1, sync=sync)))

    def test_countdown_then_start_then_launch(self):
        password = None
        self.boss_and_setup()
        password = self.c.script_password
        self.human()
        sent = self.run_for(5)
        self.assertTrue(any("starting in" in x for x in sent), sent)
        sent = self.run_for(20)
        self.assertIn("SAYBATTLE !cv start", sent)
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.assertEqual(self.launched, [("10.1.2.3", 8452, password)])
        self.assertEqual(self.c.state, "ingame")
        # The game ends: engine exits, founder leaves game; back to waiting, in the room.
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(1)
        self.assertEqual(self.c.state, "waiting")
        self.assertIsNotNone(self.c.battle)

    def test_unready_human_cancels_countdown(self):
        self.boss_and_setup()
        self.human()
        self.run_for(5)
        self.human(ready=0)
        sent = self.run_for(30)
        self.assertNotIn("SAYBATTLE !cv start", sent)
        self.assertIsNone(self.c.countdown_end)

    def test_no_start_with_nobody_or_unsynced(self):
        self.boss_and_setup()
        self.assertNotIn("SAYBATTLE !cv start", self.run_for(60))
        self.human(sync=2)
        self.assertNotIn("SAYBATTLE !cv start", self.run_for(60))

    def test_no_start_before_content_is_ready(self):
        self.join()
        self.feed("SAIDBATTLEEX %s * Boss mode enabled for %s" % (HOST, ME))
        self.run_for(40)
        self.human()
        self.assertNotIn("SAYBATTLE !cv start", self.run_for(60))


class Recovery(Harness):
    def test_kicked_takes_another_room(self):
        self.boss_and_setup()
        self.feed("FORCEQUITBATTLE", opened("13"), "UPDATEBATTLEINFO 13 1 0 123 m")
        self.assertIsNone(self.c.battle)
        sent = self.run_for(15)
        self.assertIn("13", [x.split(" ")[1] for x in sent if x.startswith("JOINBATTLE")])

    def test_failed_download_leaves(self):
        self.join()
        self.c.fetched(self.c.battle, False)
        self.assertIn("LEAVEBATTLE", self.take())
        self.assertIsNone(self.c.battle)


if __name__ == "__main__":
    unittest.main()
