"""lobby_client.py: the protocol state machine, driven with lobby lines and a fake clock."""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("RJM_BIN", "/opt/rjm-bar-host/bin"))
import lobby_client as lc  # noqa: E402

ME = "[SMRT]Clyde"
OWNER = "[SMRT]Centrifugal"
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

    def terminate(self):
        self.terminated = getattr(self, "terminated", 0) + 1

    def kill(self):
        self.killed = getattr(self, "killed", 0) + 1


class Harness(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.launched = []
        self.proc = Proc()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = self.client()

    def client(self, **extra):
        """A client on room-mode defaults plus `extra` settings, its state
        file in this test's own directory."""
        e = {"BARHOST_LOBBY_LOGIN": ME, "BARHOST_LOBBY_PASSWORD": "secret",
             "BAR_ENGINE_VERSION": ENGINE, "BARHOST_LOBBY_CLUSTER": "EU",
             "BARHOST_STATUS_FILE": os.path.join(self.tmp.name, "status.json"),
             "BARHOST_STATE_FILE": os.path.join(self.tmp.name, "state.json")}
        e.update(extra)
        return lc.LobbyClient(lc.Settings(e), now=self.clock, launch=self.launch)

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
    def test_boss_then_options_and_bots_then_spectating(self):
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
        self.assertEqual([s["side"] for s in statuses], [2] * 8)            # Random, as Chobby
        self.assertEqual([b[1] for b in bots], ["BARbstable(%d)" % n for n in range(1, 9)])
        self.assertEqual({b[4] for b in bots}, {"BARb"})
        # A headless host: a spectator once set up.
        last = [lc.unpack_battle_status(x.split(" ")[1]) for x in sent if x.startswith("MYBATTLESTATUS")][-1]
        self.assertEqual((last["mode"], last["sync"]), (0, 1))

    def test_bot_setup_follows_every_add(self):
        self.c = self.client(BARHOST_BOT_SETUP="!setallaibonus 50", BARHOST_FOLLOW=OWNER)
        sent = self.boss_and_setup()
        adds = [i for i, x in enumerate(sent) if x.startswith("ADDBOT")]
        self.assertIn("SAYBATTLE !setallaibonus 50", sent)
        self.assertGreater(sent.index("SAYBATTLE !setallaibonus 50"), adds[-1])
        for x in sent:                                      # the server echoes the bots
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))
        self.feed("SAIDPRIVATE %s !bots 10" % OWNER)
        self.assertIn("SAYBATTLE !setallaibonus 50", self.run_for(30))   # again after more bots
        self.feed("SAIDPRIVATE %s !bots 4" % OWNER)
        self.assertNotIn("SAYBATTLE !setallaibonus 50", self.run_for(30))  # removing only

    def test_sides_cycle_when_set(self):
        self.c = self.client(BARHOST_BOT_SIDES="0,1")
        bots = [x.split(" ") for x in self.boss_and_setup() if x.startswith("ADDBOT")]
        self.assertEqual([lc.unpack_battle_status(b[2])["side"] for b in bots], [0, 1] * 4)

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

    def test_leaves_without_boss_confirmation(self):
        self.join()
        sent = self.run_for(60)
        self.assertIn("SAYBATTLE !boss " + ME, sent)
        self.assertIn("LEAVEBATTLE", sent)
        self.assertFalse([x for x in sent if x.startswith("ADDBOT") or x.startswith("SAYBATTLE !preset")])


class Vacancy(Harness):
    """BAR's server hides other rooms' members, so a room is judged free only
    from inside it; an occupied room is left untouched and not retried."""

    def test_an_occupied_room_is_left_without_a_word(self):
        self.join()
        self.feed("CLIENTBATTLESTATUS Alice %d 0" % lc.battle_status(mode=1, sync=1))
        sent = self.run_for(10)
        self.assertIn("LEAVEBATTLE", sent)
        self.assertFalse([x for x in sent if x.startswith("SAYBATTLE")])     # no !boss, no setup
        self.assertFalse([x for x in self.run_for(30) if x.startswith("JOINBATTLE 12 ")])

    def test_a_room_with_someone_elses_bots_is_left(self):
        self.join()
        self.feed("ADDBOT 12 Scav1 Alice %d 0 ScavengersAI" % lc.battle_status(team=1, ally=1, mode=1, sync=1))
        self.assertIn("LEAVEBATTLE", self.run_for(10))

    def test_boss_granted_before_the_check_still_waits_for_it(self):
        self.join()
        self.feed("SAIDBATTLEEX %s * Boss mode enabled for %s" % (HOST, ME),
                  "CLIENTBATTLESTATUS Alice %d 0" % lc.battle_status(mode=1, sync=1))
        sent = self.run_for(10)
        self.assertIn("LEAVEBATTLE", sent)
        self.assertFalse([x for x in sent if x.startswith("SAYBATTLE !rename") or x.startswith("SAYBATTLE !preset")
                          or x.startswith("ADDBOT")])

    def test_a_free_room_is_claimed(self):
        sent = self.boss_and_setup()
        self.assertIn("SAYBATTLE !boss " + ME, sent)
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

    def echo_bots(self, sent):
        for x in sent:                                      # the server echoes the bots
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))

    def hints(self, sent):
        return [x for x in sent if x == "SAYBATTLE " + lc.START_HINT]

    def test_ready_gets_the_vote_hint_never_a_start(self):
        self.boss_and_setup()
        password = self.c.script_password
        self.human()
        sent = self.run_for(5)
        self.assertEqual(len(self.hints(sent)), 1, sent)
        self.assertIn("!cv start", lc.START_HINT)
        sent += self.run_for(120)
        self.assertEqual(len(self.hints(sent)), 1)                          # once while ready
        self.assertFalse([x for x in sent if x.startswith("SAYBATTLE !cv start") or x.startswith("SAYBATTLE !start")
                          or "starting in" in x])
        self.feed("SAIDBATTLE Alice !cv start", "CLIENTSTATUS %s 65" % HOST)   # the players start it
        self.assertEqual(self.launched, [("10.1.2.3", 8452, password)])
        self.assertEqual(self.c.state, "ingame")
        # The game ends: engine exits, founder leaves game; back to waiting, in the room.
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(1)
        self.assertEqual(self.c.state, "waiting")
        self.assertIsNotNone(self.c.battle)

    def test_ready_again_gets_the_hint_again_at_most_once_a_minute(self):
        self.boss_and_setup()
        self.human()
        self.run_for(5)
        self.human(ready=0)
        self.run_for(5)
        self.human()
        self.assertEqual(self.hints(self.run_for(5)), [])                   # within the minute
        self.assertEqual(len(self.hints(self.run_for(60))), 1)

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

    def test_matched_teams_waits_for_as_many_humans_as_bots(self):
        self.c = self.client(BARHOST_MATCHED_TEAMS="1")
        self.echo_bots(self.boss_and_setup())
        self.assertEqual(len(self.c.battle.bots), 8)
        for k in range(1, 8):                                # 7 ready humans against 8 bots
            self.human("P%d" % k)
        self.assertEqual(self.hints(self.run_for(10)), [])
        self.human("P8")                                     # 8 against 8
        self.assertEqual(len(self.hints(self.run_for(5))), 1)

    def test_matched_teams_counts_the_bots_in_the_room(self):
        self.c = self.client(BARHOST_MATCHED_TEAMS="1", BARHOST_BOT_COUNT="2")
        self.echo_bots(self.boss_and_setup())
        self.assertEqual(len(self.c.battle.bots), 2)
        self.human("P1")
        self.assertEqual(self.hints(self.run_for(10)), [])
        self.human("P2")
        self.assertEqual(len(self.hints(self.run_for(5))), 1)

    def rings(self, sent):
        return [x for x in sent if x == "SAYBATTLE !ring"]

    def test_matched_teams_ring_the_unready(self):
        self.c = self.client(BARHOST_MATCHED_TEAMS="1", BARHOST_BOT_COUNT="2", BARHOST_RING_EVERY="60")
        self.echo_bots(self.boss_and_setup())
        self.human("P1", ready=0)
        self.assertEqual(self.rings(self.run_for(120)), [])          # 1 against 2: no ring
        self.human("P2")
        self.assertEqual(self.rings(self.run_for(12)), [])           # not before 15 s
        self.assertEqual(len(self.rings(self.run_for(6))), 1)
        self.assertEqual(self.rings(self.run_for(55)), [])           # then a minute apart
        self.assertEqual(len(self.rings(self.run_for(10))), 1)
        self.human("P1")                                             # everyone ready: no more
        sent = self.run_for(120)
        self.assertEqual(self.rings(sent), [])
        self.assertEqual(len(self.hints(sent)), 1)
        self.feed("SAIDBATTLE %s !ring" % ME)                        # its own ring gets no answer
        self.assertFalse([x for x in self.run_for(5) if x.startswith("SAYBATTLE ")])

    def balances(self, sent):
        return [x for x in sent if x == "SAYBATTLE !balance"]

    def test_balance_after_setup_and_when_the_team_fills(self):
        self.c = self.client(BARHOST_BALANCE="1", BARHOST_BOT_COUNT="2")
        sent = self.boss_and_setup()
        self.assertEqual(len(self.balances(sent)), 1)
        adds = [i for i, x in enumerate(sent) if x.startswith("ADDBOT ")]
        self.assertTrue(adds and sent.index("SAYBATTLE !balance") > adds[-1])    # after the bots
        self.echo_bots(sent)
        self.human("P1", ready=0)
        self.assertEqual(self.balances(self.run_for(30)), [])       # 1 against 2
        self.human("P2", ready=0)
        self.assertEqual(len(self.balances(self.run_for(5))), 1)    # 2 against 2: full
        self.assertEqual(self.balances(self.run_for(60)), [])       # once while full
        self.feed("LEFTBATTLE 12 P2")
        self.run_for(5)
        self.human("P2", ready=0)
        self.assertEqual(len(self.balances(self.run_for(5))), 1)    # full again

    def test_ring_and_balance_also_in_the_post_game_delay(self):
        self.c = self.client(BARHOST_BALANCE="1", BARHOST_BOT_COUNT="2", BARHOST_RING_EVERY="60",
                             BARHOST_MATCHED_TEAMS="1")
        self.echo_bots(self.boss_and_setup())
        self.human("P1", ready=0)
        self.human("P2", ready=0)
        self.run_for(40)
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.run_for(30)
        self.proc.code = 0
        self.feed("LEFTBATTLE 12 P2", "CLIENTSTATUS %s 64" % HOST)   # a game; P2 leaves after it
        self.run_for(1)
        self.c.ai_updated([], [], None)                              # the after-game AI check ends
        self.run_for(3)
        self.human("P2", ready=0)                                    # inside the 60 s post-game delay
        sent = self.run_for(25)
        self.assertEqual(len(self.rings(sent)), 1)
        self.assertEqual(len(self.balances(sent)), 1)

    def test_at_most_one_balance_per_30_s(self):
        self.c = self.client(BARHOST_BALANCE="1", BARHOST_BOT_COUNT="2")
        sent = self.boss_and_setup()
        self.echo_bots(sent)
        self.run_for(30)
        self.human("P1", ready=0)
        self.human("P2", ready=0)
        sent = self.run_for(5)
        for _ in range(4):                                           # the seat changes hands
            self.feed("LEFTBATTLE 12 P2")
            sent += self.run_for(3)
            self.human("P2", ready=0)
            sent += self.run_for(3)
        self.assertEqual(len(self.balances(sent)), 1)                # 29 s: one
        self.assertEqual(len(self.balances(self.run_for(10))), 1)    # still full: one more after 30 s

    def test_no_balance_by_default(self):
        self.c = self.client(BARHOST_BOT_COUNT="1")
        sent = self.boss_and_setup()
        self.echo_bots(sent)
        self.human("P1", ready=0)
        self.assertEqual(self.balances(sent + self.run_for(30)), [])

    def test_no_ring_by_default(self):
        self.c = self.client(BARHOST_BOT_COUNT="1")
        self.echo_bots(self.boss_and_setup())
        self.human("P1", ready=0)
        self.assertEqual(self.rings(self.run_for(300)), [])


MANAGER = "Host[EU1]"
PRIVATE_HOST = "Host[EU1][7]"


def my_modes(sent):
    """The seat (1 player, 0 spectator) of each MYBATTLESTATUS sent."""
    return [lc.unpack_battle_status(x.split(" ")[1])["mode"] for x in sent if x.startswith("MYBATTLESTATUS")]


class HeadlessSeat(Harness):
    """A headless host never plays: a player seat only to be made boss and,
    briefly, to add bots."""

    def humans_in(self, names, ready=0):
        for n in names:
            self.feed("JOINEDBATTLE 12 " + n,
                      "CLIENTBATTLESTATUS %s %d 0" % (n, lc.battle_status(ready=ready, mode=1, sync=1)))
        return self.run_for(5)

    def test_spectates_once_boss_and_stays_so(self):
        sent = self.boss_and_setup()
        modes = my_modes(sent)
        adds = [i for i, x in enumerate(sent) if x.startswith("ADDBOT")]
        boss = sent.index("SAYBATTLE !boss " + ME)
        first_setting = min(i for i, x in enumerate(sent) if x.startswith("SAYBATTLE !") and i > boss)
        statuses = [i for i, x in enumerate(sent) if x.startswith("MYBATTLESTATUS")]
        # a spectator before the first setting; a player only around the adds
        self.assertEqual(lc.unpack_battle_status(sent[max(i for i in statuses if i < first_setting)]
                                                 .split(" ")[1])["mode"], 0)
        player_at = [i for i in statuses if lc.unpack_battle_status(sent[i].split(" ")[1])["mode"] == 1
                     and i > boss]
        self.assertTrue(all(i < adds[0] for i in player_at))
        self.assertEqual(modes[-1], 0)
        for names in (["P1"], ["P1", "P2"], ["P%d" % i for i in range(3, 10)]):
            self.assertEqual(my_modes(self.humans_in(names)), [])
        self.assertFalse(self.c.playing())

    def test_still_a_spectator_after_a_game(self):
        self.boss_and_setup()
        self.humans_in(["Alice"], ready=1)
        self.run_for(25)
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST,
                  "CLIENTBATTLESTATUS Alice %d 0" % lc.battle_status(ready=0, mode=1, sync=1))
        self.assertNotIn(1, my_modes(self.run_for(30)))
        self.assertFalse(self.c.playing())

    def test_never_left_in_a_player_seat(self):
        self.boss_and_setup()
        self.c.set_seat(True, "test")
        self.assertEqual(my_modes(self.run_for(7)), [1, 0])     # seated, then back at once
        self.assertFalse(self.c.playing())

    def test_a_start_called_while_seated_spectates_first(self):
        self.boss_and_setup()
        self.c.pending.append("!some queued chat")
        self.c.set_seat(True, "adding bots")
        self.feed("SAIDBATTLE Alice !cv start")
        self.assertEqual(lc.unpack_battle_status(self.c.pending[0][1])["mode"], 0)


class Boost(Harness):
    """BARHOST_BOOST_BELOW: a player seat while few humans play (the battle
    list counts players), a spectator from BARHOST_BOOST_UNTIL humans, in a
    full room, once everyone is ready, or when a start is called."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_BOOST_BELOW="5", BARHOST_BOOST_UNTIL="7")

    def set_up(self):
        sent = self.boss_and_setup()
        for x in sent:                                      # the server echoes the bots
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))
        return sent + self.run_for(5)

    def players(self, names, ready=0):
        for n in names:
            self.feed("JOINEDBATTLE 12 " + n,
                      "CLIENTBATTLESTATUS %s %d 0" % (n, lc.battle_status(ready=ready, mode=1, sync=1)))
        return self.run_for(5)

    def leave(self, names):
        for n in names:
            self.feed("LEFTBATTLE 12 " + n)
        return self.run_for(5)

    def test_plays_in_an_empty_room_on_the_humans_side(self):
        sent = self.set_up()
        self.assertTrue(self.c.playing())
        st = lc.unpack_battle_status(self.c.my_status)
        bot_teams = {x["status"]["team"] for x in self.c.battle.bots.values()}
        self.assertEqual(st["ally"], 0)
        self.assertNotIn(st["team"], bot_teams)
        self.assertEqual(st["ready"], 0)
        self.assertEqual(len([x for x in sent if x.startswith("ADDBOT")]), 8)

    def test_bots_added_while_seated_take_other_teams(self):
        self.c = self.client(BARHOST_BOOST_BELOW="5", BARHOST_FOLLOW=OWNER)
        self.set_up()
        self.players(["Alice"])
        self.assertTrue(self.c.playing())
        mine = lc.unpack_battle_status(self.c.my_status)["team"]
        self.feed("SAIDPRIVATE %s !bots 12" % OWNER)
        adds = [x.split(" ") for x in self.run_for(30) if x.startswith("ADDBOT")]
        self.assertEqual(len(adds), 4)
        self.assertNotIn(mine, {lc.unpack_battle_status(a[2])["team"] for a in adds})

    def test_spectates_from_seven_and_plays_again_below_five(self):
        self.set_up()
        self.players(["P1", "P2", "P3", "P4"])
        self.assertTrue(self.c.playing())                    # 4: boosting
        self.players(["P5", "P6"])
        self.assertTrue(self.c.playing())                    # 6: kept
        self.players(["P7"])
        self.assertFalse(self.c.playing())                   # 7: a spectator
        self.leave(["P7", "P6"])
        self.assertFalse(self.c.playing())                   # 5: kept
        self.leave(["P5"])
        self.assertTrue(self.c.playing())                    # 4: boosting again

    def test_a_full_room_spectates(self):
        self.set_up()
        self.c.battle.max_players = 12                       # 8 bots + 3 humans + Clyde
        self.players(["P1", "P2", "P3"])
        self.assertFalse(self.c.playing())
        self.leave(["P3"])
        self.assertTrue(self.c.playing())

    def test_everyone_ready_spectates_and_stays_so(self):
        self.set_up()
        sent = self.players(["Alice"], ready=1)
        self.assertFalse(self.c.playing())
        self.assertIn("SAYBATTLE " + lc.START_HINT, sent)
        self.assertNotIn(1, my_modes(self.run_for(60)))

    def test_a_start_called_spectates_first_and_holds(self):
        self.set_up()
        self.players(["Alice"])
        self.assertTrue(self.c.playing())
        self.feed("SAIDBATTLE Alice !cv start")
        self.assertEqual(lc.unpack_battle_status(self.c.pending[0][1])["mode"], 0)
        self.assertNotIn(1, my_modes(self.run_for(lc.START_HOLD - 10)))
        self.assertIn(1, my_modes(self.run_for(20)))        # no game came: boosting again

    def test_not_during_a_game(self):
        self.set_up()
        self.players(["Alice"], ready=1)
        self.feed("SAIDBATTLE Alice !cv start", "CLIENTSTATUS %s 65" % HOST)
        self.leave(["Alice"])
        self.assertEqual(my_modes(self.run_for(300)), [])

    def test_off_by_default(self):
        self.c = self.client()
        self.boss_and_setup()
        self.assertFalse(self.c.playing())
        self.assertEqual(my_modes(self.run_for(30)), [])



class Promote(Harness):
    """BARHOST_PROMOTE_EVERY: !promote by PM to the autohost while the player
    team is not full, as often as its cooldown allows."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_PROMOTE_EVERY="600")

    def promotes(self, sent):
        return [x for x in sent if x == "SAYPRIVATE %s !promote" % HOST]

    def answer(self, text):
        self.feed("SAIDPRIVATE %s %s" % (HOST, text))

    def test_promotes_by_pm_once_set_up(self):
        sent = self.boss_and_setup() + self.run_for(10)
        self.assertEqual(len(self.promotes(sent)), 1)
        self.assertFalse([x for x in sent if x.startswith("SAYBATTLE") and "promote" in x])

    def test_waits_out_the_cooldown_the_autohost_names(self):
        self.boss_and_setup()
        self.run_for(10)
        self.answer("Please wait 4 minutes and 10 seconds before promoting battle (promote flood protection)")
        self.assertEqual(self.promotes(self.run_for(250)), [])
        self.assertEqual(len(self.promotes(self.run_for(10))), 1)

    def test_after_a_promote_waits_the_delay(self):
        self.boss_and_setup()
        self.run_for(10)
        self.answer("Promoting battle in #main")
        self.assertEqual(self.promotes(self.run_for(595)), [])
        self.assertEqual(len(self.promotes(self.run_for(15))), 1)

    def test_someone_elses_promote_restarts_the_wait(self):
        self.boss_and_setup()
        self.run_for(10)
        self.answer("Please wait 10 seconds before promoting battle (promote flood protection)")
        self.feed("SAIDBATTLEEX %s * Promoting battle in #main" % HOST)
        self.assertEqual(self.promotes(self.run_for(300)), [])

    def test_a_refusal_retries_later(self):
        self.boss_and_setup()
        self.run_for(10)
        self.answer("Unable to promote battle, battle lobby is locked")
        self.assertEqual(self.promotes(self.run_for(lc.PROMOTE_RETRY - 5)), [])
        self.assertEqual(len(self.promotes(self.run_for(10))), 1)

    def test_not_when_the_team_is_full(self):
        self.boss_and_setup()
        self.answer("Please wait 1 second before promoting battle (promote flood protection)")
        self.c.battle.max_players = len(self.c.battle.bots) + 2   # the bots + 2 humans
        for n in ("P1", "P2"):
            self.feed("JOINEDBATTLE 12 " + n,
                      "CLIENTBATTLESTATUS %s %d 0" % (n, lc.battle_status(mode=1, sync=1)))
        self.assertEqual(self.promotes(self.run_for(30)), [])
        self.feed("LEFTBATTLE 12 P2")
        self.assertEqual(len(self.promotes(self.run_for(10))), 1)

    def test_not_a_private_room(self):
        self.boss_and_setup()
        self.c.battle.passworded = True
        self.c.privacy = "private"
        self.assertEqual(self.promotes(self.run_for(30)), [])

    def test_not_when_everyone_is_ready_or_in_a_game(self):
        self.boss_and_setup()
        self.feed("JOINEDBATTLE 12 Alice",
                  "CLIENTBATTLESTATUS Alice %d 0" % lc.battle_status(ready=1, mode=1, sync=1))
        self.assertEqual(self.promotes(self.run_for(30)), [])
        self.feed("SAIDBATTLE Alice !cv start", "CLIENTSTATUS %s 65" % HOST)
        self.assertEqual(self.promotes(self.run_for(600)), [])

    def test_off_by_default(self):
        self.c = self.client()
        sent = self.boss_and_setup() + self.run_for(30)
        self.assertEqual(self.promotes(sent), [])

    def set_up_with_bots(self):
        sent = self.boss_and_setup()
        for x in sent:                                      # the server echoes the bots
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))
        self.c.battle.max_players = 16
        return sent + self.run_for(5)

    def humans_join(self, names, ready=0):
        for n in names:
            self.feed("JOINEDBATTLE 12 " + n,
                      "CLIENTBATTLESTATUS %s %d 0" % (n, lc.battle_status(ready=ready, mode=1, sync=1)))

    def test_until_as_many_humans_as_bots(self):
        self.set_up_with_bots()
        self.assertEqual(len(self.c.battle.bots), 8)
        self.humans_join(["P%d" % k for k in range(1, 8)])  # 7 against 8, room not full
        self.answer("Please wait 1 second before promoting battle (promote flood protection)")
        self.assertEqual(len(self.promotes(self.run_for(10))), 1)
        self.humans_join(["P8"])                             # 8 against 8: matched
        self.answer("Please wait 1 second before promoting battle (promote flood protection)")
        self.assertEqual(self.promotes(self.run_for(60)), [])
        self.feed("LEFTBATTLE 12 P8")
        self.assertEqual(len(self.promotes(self.run_for(10))), 1)

    def play_a_game(self, seconds=60):
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.run_for(seconds)
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)

    def test_promotes_as_soon_as_a_game_is_over(self):
        self.set_up_with_bots()
        self.humans_join(["P1"])
        self.answer("Unable to promote battle, battle lobby is locked")   # a retry in 120 s
        self.play_a_game()
        self.assertEqual(len(self.promotes(self.run_for(3))), 1)

    def test_after_a_game_still_waits_out_a_named_cooldown(self):
        self.set_up_with_bots()
        self.humans_join(["P1"])
        self.answer("Promoting battle in #main")             # the next one in 605 s
        self.play_a_game(100)
        self.assertEqual(self.promotes(self.run_for(480)), [])
        self.assertEqual(len(self.promotes(self.run_for(40))), 1)

    def test_not_allowed_waits_the_full_delay(self):
        self.set_up_with_bots()
        self.answer('%s, you are not allowed to call command "promote" in current context.' % ME)
        self.assertEqual(self.promotes(self.run_for(590)), [])
        self.assertEqual(len(self.promotes(self.run_for(20))), 1)


class Privacy(Harness):
    """!public / !private, remembered on the data volume; a private room is
    opened as Chobby does, through a region manager's !privatehost."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_FOLLOW=OWNER)

    def pm(self, text, user=OWNER):
        self.feed("SAIDPRIVATE %s %s" % (user, text))
        return self.run_for(5)

    def log_in(self, manager=True):
        self.feed("TASSERVER 0.38 * 8201 0", "ACCEPTED " + ME, "ADDUSER %s XX 0 1" % HOST,
                  *(["ADDUSER %s XX 0 9" % MANAGER] if manager else []),
                  opened("12"), "LOGININFOEND")
        self.take()

    def open_private(self):
        """Ask the manager, get the password, see the room; return what was sent."""
        sent = self.run_for(6)
        self.assertIn("SAYPRIVATE %s !privatehost" % MANAGER, sent)
        self.feed("SAIDPRIVATE %s Starting a new private instance in %s password=ab12 (share it)"
                  % (MANAGER, PRIVATE_HOST),
                  opened("20", founder=PRIVATE_HOST, passworded=1, title=ME + "'s private battle"))
        return self.run_for(6)

    def test_private_room_is_opened_joined_with_its_password_and_spectated(self):
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_ROOM_PRIVACY="private")
        self.log_in()
        join = [x for x in self.open_private() if x.startswith("JOINBATTLE")]
        self.assertEqual([x.split(" ")[1:3] for x in join], [["20", "ab12"]])
        self.feed("JOINBATTLE 20 hash", "JOINEDBATTLE 20 %s %s" % (ME, join[0].split(" ")[3]))
        sent = self.run_for(2)
        self.feed("SAIDBATTLEEX %s * Boss mode enabled for %s" % (PRIVATE_HOST, ME))
        sent += self.run_for(40)
        self.assertIn("SAYBATTLE !boss " + ME, sent)
        self.assertEqual(len([x for x in sent if x.startswith("ADDBOT")]), 8)
        self.assertEqual(my_modes(sent)[-1], 0)                             # private: spectate
        self.assertTrue([x for x in sent if x.startswith("SAYPRIVATE %s Private room 20" % OWNER)
                         and "password ab12" in x], sent)

    def test_choice_is_remembered_across_a_restart(self):
        self.log_in()
        self.assertEqual(self.c.privacy, "public")
        self.assertTrue([x for x in self.pm("!private") if "OK: I host a private room" in x])
        self.assertEqual(self.client(BARHOST_FOLLOW=OWNER).privacy, "private")
        self.pm("!public")
        self.assertEqual(self.client(BARHOST_FOLLOW=OWNER, BARHOST_ROOM_PRIVACY="private").privacy, "public")

    def test_switching_to_private_leaves_the_public_room_then_opens_one(self):
        self.boss_and_setup()
        self.feed("ADDUSER %s XX 0 9" % MANAGER)
        sent = self.pm("!private") + self.run_for(30)
        self.assertIn("LEAVEBATTLE", sent)
        self.assertIn("SAYPRIVATE %s !privatehost" % MANAGER, sent)

    def test_not_moved_during_a_game(self):
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        sent = self.pm("!private") + self.run_for(30)
        self.assertTrue([x for x in sent if "after this game" in x], sent)
        self.assertNotIn("LEAVEBATTLE", sent)

    def test_not_moved_while_a_start_may_be_under_way(self):
        self.boss_and_setup()
        self.feed("JOINEDBATTLE 12 Alice",
                  "CLIENTBATTLESTATUS Alice %d 0" % lc.battle_status(ready=1, mode=1, sync=1))
        self.run_for(5)                                     # everyone ready: the start hint
        self.feed("ADDUSER %s XX 0 9" % MANAGER)
        self.assertNotIn("LEAVEBATTLE", self.pm("!private") + self.run_for(60))
        self.assertIn("LEAVEBATTLE", self.run_for(90))      # no game came

    def test_reconnecting_during_a_game_takes_no_room_until_it_ends(self):
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)             # the game starts; the engine runs
        self.assertEqual(len(self.launched), 1)
        self.c.reset()                                      # the lobby connection drops
        self.feed("TASSERVER 0.38 * 8201 0", "ACCEPTED " + ME, "ADDUSER %s XX 0 1" % HOST,
                  opened("30"), "LOGININFOEND")
        self.assertEqual(self.c.state, "ingame")
        self.assertFalse([x for x in self.run_for(60) if x.startswith("JOINBATTLE")])
        self.proc.code = 0                                  # the game ends
        self.assertTrue([x for x in self.run_for(30) if x.startswith("JOINBATTLE 30")])

    def test_no_manager_online_is_reported(self):
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_ROOM_PRIVACY="private")
        self.log_in(manager=False)
        self.assertFalse([x for x in self.run_for(10) if x.startswith("SAYPRIVATE")])
        self.assertIn("no autohost manager online", self.c.last_error)

    def test_a_silent_manager_times_out_then_is_asked_again(self):
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_ROOM_PRIVACY="private")
        self.log_in()
        self.assertIn("SAYPRIVATE %s !privatehost" % MANAGER, self.run_for(6))
        sent = self.run_for(40)
        self.assertIn("opened no private room", self.c.last_error)
        self.assertNotIn("SAYPRIVATE %s !privatehost" % MANAGER, sent)
        self.assertIn("SAYPRIVATE %s !privatehost" % MANAGER, self.run_for(70))


class ProfilerAndSay(Harness):
    """!profiler on|off|default: the engine profiler for the next games, kept
    across restarts; !say <text>: said in the room."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_ENGINE_PROFILER="1")

    def pm(self, text, user=OWNER):
        self.feed("SAIDPRIVATE %s %s" % (user, text))
        return [x for x in self.run_for(5) if x.startswith("SAYPRIVATE %s " % user)]

    def widget(self, client):
        d = Path(self.tmp.name) / "run"
        d.mkdir(exist_ok=True)
        lc.prepare_write_dir(client.s, d)
        return (d / "LuaUI" / "Widgets" / "rjm_host_metrics.lua").read_text()

    def test_the_choice_reaches_the_widget_and_is_kept_across_a_restart(self):
        self.log_in()
        self.assertIn("Engine profiler: on (the default)", self.pm("!profiler")[0])
        self.assertIn("Engine profiler: off (chosen by PM", self.pm("!profiler off")[0])
        self.assertIn("\nlocal RJM_PROFILER = false ", self.widget(self.c))
        again = self.client(BARHOST_FOLLOW=OWNER, BARHOST_ENGINE_PROFILER="1")
        self.assertFalse(again.s.engine_profiler)
        self.assertIn("\nlocal RJM_PROFILER = false ", self.widget(again))
        self.c = again
        self.assertIn("Engine profiler: on (the default)", self.pm("!profiler default")[0])
        self.assertTrue(self.client(BARHOST_FOLLOW=OWNER, BARHOST_ENGINE_PROFILER="1").s.engine_profiler)
        self.assertIn("!profiler on, !profiler off", self.pm("!profiler maybe")[0])

    def test_the_widget_passes_the_frame_data_flag(self):
        # Engines before 2026.09.01 raise without it, and every timer read came
        # back empty (rjm_host_metrics.lua, since()).
        w = self.widget(self.c)
        self.assertIn("pcall(GetTimeRecord, name, false)", w)
        self.assertNotRegex(w, r"GetTimeRecord, name\)")

    def test_only_authorised_users(self):
        self.log_in()
        self.assertEqual(self.pm("!profiler off", user="Mallory"), [])
        self.assertEqual(self.pm("!widgets on", user="Mallory"), [])
        self.assertTrue(self.c.s.engine_profiler)
        self.assertFalse(self.c.s.bar_widgets)

    def test_widgets_choice_is_kept_across_a_restart(self):
        self.log_in()
        self.assertIn("BAR's widgets: off (only my metrics widget) (the default)", self.pm("!widgets")[0])
        self.assertIn("BAR's widgets: on (chosen by PM", self.pm("!widgets on")[0])
        self.assertTrue(self.client(BARHOST_FOLLOW=OWNER).s.bar_widgets)
        self.assertIn("BAR's widgets: off (only my metrics widget) (the default)", self.pm("!widgets default")[0])
        self.assertTrue(self.client(BARHOST_FOLLOW=OWNER, BARHOST_BAR_WIDGETS="1").s.bar_widgets)
        self.assertIn("!widgets on, !widgets off", self.pm("!widgets some")[0])

    def test_say_in_the_room(self):
        self.assertIn("I am not in a room", self.pm("!say hello")[0])
        self.boss_and_setup()
        self.feed("SAIDPRIVATE %s !say  Good luck, have fun!  " % OWNER)
        sent = self.run_for(5)
        self.assertIn("SAYBATTLE Good luck, have fun!", sent)
        self.assertIn("SAYPRIVATE %s Said in room 12." % OWNER, sent)
        self.assertIn("PM me !say <text>", self.pm("!say")[0])



class ChatAudit(Harness):
    """The chat audit log (BARHOST_CHAT_DIR): every private message, both
    ways, per day; room chat per room session; and no reply of any kind to a
    private message from anyone but an authorised user."""

    PROBES = ["!help", "!status", "!bots 3", "!bots 16 barb", "!say hi", "!stop", "!rehost", "!public",
              "!private", "!ai barb", "!ai smrt-test v1.0", "!perf", "!perf 1", "!games", "!join", "!join pw",
              "!room 12", "!leave", "!profiler off", "!widgets on", "!follow", "hello", "!", "!HELP",
              "!status " + OWNER, "!say " + OWNER + " !boss Mallory"]

    def setUp(self):
        super().setUp()
        self.chat = Path(self.tmp.name) / "chat"
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_CHAT_DIR=str(self.chat))

    def text(self, folder):
        return "".join(f.read_text(encoding="utf-8") for f in sorted((self.chat / folder).rglob("*.log")))

    def assert_silent_to(self, user, sent):
        self.assertFalse([x for x in sent if user in x], sent)

    def test_no_reply_to_anyone_else_in_or_out_of_a_room(self):
        self.log_in()
        self.take()
        for user in ("Mallory", "[smrt]centrifugal", "[SMRT]Centrifuga", OWNER + "_"):
            for text in self.PROBES:
                self.feed("SAIDPRIVATE %s %s" % (user, text), "SAIDPRIVATEEX %s %s" % (user, text))
            self.assert_silent_to(user, self.run_for(10))
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_CHAT_DIR=str(self.chat),   # again, as boss of a room
                             BARHOST_ENGINE_PROFILER="1")
        self.boss_and_setup()
        self.take()
        for text in self.PROBES:
            self.feed("SAIDPRIVATE Mallory %s" % text)
        sent = self.run_for(20)
        self.assert_silent_to("Mallory", sent)
        self.assertNotIn("SAYBATTLE hi", sent)
        self.assertIsNotNone(self.c.battle)
        self.assertEqual(self.c.privacy, "public")
        self.assertEqual((self.c.s.engine_profiler, self.c.s.bar_widgets), (True, False))

    def test_private_messages_both_ways_by_day(self):
        self.log_in()
        self.feed("SAIDPRIVATE Mallory !say gg", "SAIDPRIVATEEX Mallory waves", "SAIDPRIVATE %s !status" % OWNER)
        self.run_for(5)
        files = list((self.chat / "private").iterdir())
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^\d{4}-\d\d-\d\d\.log$")
        log = self.text("private")
        self.assertRegex(log, r"(?m)^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ <- Mallory \[not authorised, ignored\]: !say gg$")
        self.assertIn("<- Mallory [not authorised, ignored]: /me waves\n", log)
        self.assertIn("<- %s [authorised]: !status\n" % OWNER, log)
        self.assertRegex(log, r"-> %s: \S" % re.escape(OWNER))
        self.assertNotIn("-> Mallory", log)

    def test_room_chat_per_room_session(self):
        self.boss_and_setup()
        self.feed("JOINEDBATTLE 12 Alice", "SAIDBATTLE Alice hi Clyde", "SAIDBATTLEEX Alice dances",
                  "SAIDBATTLE Bob !cv start", "LEFTBATTLE 12 Alice")
        self.feed("SAIDPRIVATE %s !say  hello room " % OWNER)
        self.run_for(5)
        self.feed("LEFTBATTLE 12 " + ME)
        files = sorted((self.chat / "rooms").rglob("*.log"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^\d{8}-\d{6}Z_room12\.log$")
        log = files[0].read_text(encoding="utf-8")
        for line in ("* room 12 (Empty room) on %s" % HOST, "* joined", "* Alice joined", "Alice: hi Clyde",
                     "Alice: /me dances", "Bob: !cv start", "* Alice left", "%s: hello room" % ME,
                     "%s: !bSet" % ME, "* left the room: left the room"):
            self.assertIn(line, log)
        self.assertEqual(log.count("%s: hello room" % ME), 1)
        self.assertNotIn("Said in room", log)       # PM replies go to the private log only
        self.assertIn("-> %s: Said in room 12." % OWNER, self.text("private"))

    def test_nothing_without_a_chat_or_archive_dir(self):
        self.c = self.client(BARHOST_FOLLOW=OWNER)
        self.assertIsNone(self.c.s.chat_dir)
        self.boss_and_setup()
        self.feed("SAIDPRIVATE Mallory hi", "SAIDBATTLE Alice hi")
        self.assertFalse(self.chat.exists())

    def test_with_the_archive_by_default(self):
        s = self.client(BARHOST_ARCHIVE_DIR="/a", BARHOST_ARCHIVE_SOURCE="bar-lobbies.clyde").s
        self.assertEqual(s.chat_dir, Path("/a") / "_chat" / "bar-lobbies.clyde")


ROTATION = "All That Simmers;Cells;Forge"


class MapRotation(Harness):
    """BARHOST_MAP_ROTATION: one map per game, in order, kept across restarts;
    any map change re-downloads before a game can start."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_MAP_ROTATION=ROTATION)

    def map_is(self, name):
        self.feed("UPDATEBATTLEINFO 12 1 0 123 " + name)

    def boss(self):
        """Join and become boss; return what was sent up to the !map."""
        self.join()
        self.run_for(2)
        self.c.fetched(self.c.battle, True)
        self.feed("SAIDBATTLEEX %s * Boss mode enabled for %s" % (HOST, ME))
        return self.run_for(30)

    def test_sets_the_first_map_and_starts_only_once_it_is_downloaded(self):
        sent = self.boss()
        self.assertIn("SAYBATTLE !map All That Simmers", sent)
        self.feed("JOINEDBATTLE 12 Alice",
                  "CLIENTBATTLESTATUS Alice %d 0" % lc.battle_status(ready=1, mode=1, sync=1))
        self.assertFalse([x for x in self.run_for(5) if "To start the game" in x])    # map not switched yet
        self.map_is("All That Simmers v1.2")
        sent = self.run_for(5)
        self.assertEqual(lc.unpack_battle_status(
            [x for x in sent if x.startswith("MYBATTLESTATUS")][0].split(" ")[1])["sync"], 2)
        self.assertFalse([x for x in sent if "To start the game" in x])               # downloading
        self.c.fetched(self.c.battle, True)
        self.assertTrue([x for x in self.run_for(5) if "To start the game" in x])

    def test_next_map_after_each_game_and_the_place_is_kept(self):
        self.boss()
        self.map_is("All That Simmers v1.2")
        self.c.fetched(self.c.battle, True)
        self.feed("CLIENTSTATUS %s 65" % HOST)                # a game is played
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.assertIn("SAYBATTLE !map Cells", self.run_for(5))
        self.assertEqual(self.client(BARHOST_MAP_ROTATION=ROTATION).map_index, 1)

    def test_a_map_the_autohost_does_not_take_is_skipped(self):
        self.boss()
        sent = self.run_for(25)                               # no UPDATEBATTLEINFO comes
        self.assertIn("SAYBATTLE !map Cells", sent)

    def test_a_map_that_cannot_be_downloaded_is_skipped_not_the_room(self):
        self.boss()
        self.map_is("All That Simmers v1.2")
        self.c.fetched(self.c.battle, False)
        sent = self.run_for(5)
        self.assertNotIn("LEAVEBATTLE", sent)
        self.assertIn("SAYBATTLE !map Cells", sent)

    def test_a_download_for_an_earlier_map_is_not_taken_as_the_current_ones(self):
        # 2026-11-07: a boss's `!map Forge v2.3` during the previous map's
        # download; that download failed, and the room was moved off Forge.
        self.boss()
        self.map_is("All That Simmers v1.2")
        self.map_is("Forge v2.3")
        self.c.fetched(self.c.battle, False, "All That Simmers v1.2")
        sent = self.run_for(5)
        self.assertNotIn("SAYBATTLE !map Cells", sent)
        self.assertNotIn("LEAVEBATTLE", sent)
        self.c.fetched(self.c.battle, True, "All That Simmers v1.2")
        self.assertFalse(self.c.synced)                       # not Forge's content
        self.c.fetched(self.c.battle, True, "Forge v2.3")
        self.assertTrue(self.c.synced)

    def test_downloads_run_one_at_a_time_for_the_rooms_map_then(self):
        # Two pr-downloaders in one data dir race on the rapid index.
        import asyncio
        log = Path(self.tmp.name) / "prd.log"
        prd = Path(self.tmp.name) / "prd.sh"
        prd.write_text("#!/bin/sh\necho \"start $4\" >> %s\nsleep 0.2\necho \"end $4\" >> %s\n" % (log, log))
        prd.chmod(0o755)
        os.environ["PRD"] = str(prd)
        self.addCleanup(os.environ.pop, "PRD", None)

        class C:
            pass
        c, b = C(), lc.Battle.__new__(lc.Battle)
        b.game, b.map = "", "Old Map"
        c.battle, results = b, []
        c.fetched = lambda bb, ok, m: results.append((ok, m))

        async def both():
            first = asyncio.ensure_future(lc.fetch_content(c, b))
            await asyncio.sleep(0.05)
            b.map = "New Map"
            await asyncio.gather(first, lc.fetch_content(c, b))
        lc._fetch_lock = None
        asyncio.run(both())
        self.assertEqual(log.read_text().splitlines(),
                         ["start Old Map", "end Old Map", "start New Map", "end New Map"])
        self.assertEqual(results, [(True, "Old Map"), (True, "New Map")])

    def test_rotation_wraps_around(self):
        self.c.map_index = 2
        self.c.advance_map()
        self.assertEqual(self.c.map_index, 0)

    def test_any_map_change_downloads_it_first(self):
        self.c = self.client()                                # no rotation: someone else changes the map
        self.join()
        self.c.fetched(self.c.battle, True)
        self.run_for(2)
        self.map_is("Glacier Pass 1.2")
        self.assertFalse(self.c.synced)
        self.assertIs(self.c.need_fetch, self.c.battle)


FRIEND = "[iS]BarscrewlSports"


def vote(user, command):
    return "SAIDBATTLEEX %s * %s called a vote for command \"%s\" [!vote y, !vote n, !vote b]" % (HOST, user, command)


class Guard(Harness):
    """Room mode, as boss: end votes that would unboss this client or an
    authorised user; boss authorised users when they join."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_FOLLOW="%s,%s" % (OWNER, FRIEND))

    def ended(self, command, user="Troll"):
        self.feed(vote(user, command))
        sent = self.run_for(2)
        return "SAYBATTLE !ev" in sent

    def test_ends_a_vote_to_unboss_clyde_at_once(self):
        self.boss_and_setup()
        self.assertTrue(self.ended("unboss " + ME))

    def test_ends_a_vote_to_unboss_everyone(self):
        self.boss_and_setup()
        self.assertTrue(self.ended("unboss"))

    def test_ends_a_vote_to_unboss_an_authorised_user(self):
        self.boss_and_setup()
        self.assertTrue(self.ended("unboss " + FRIEND.lower()))

    def test_shames_a_vote_to_unboss_clyde_or_everyone(self):
        self.boss_and_setup()
        for command in ("unboss " + ME, "unboss"):
            self.feed(vote("Troll", command))
            sent = self.run_for(4)
            self.assertEqual(sent[sent.index("SAYBATTLE !ev") + 1], "SAYBATTLE Shame on you, Troll!")
        for command in ("unboss " + FRIEND, "boss Troll", "boss " + ME):
            self.feed(vote("Troll", command))
            sent = self.run_for(4)
            self.assertIn("SAYBATTLE !ev", sent)
            self.assertFalse([x for x in sent if "Shame" in x])

    def test_ends_every_boss_vote_from_a_random_player(self):
        self.boss_and_setup()
        for command in ("boss Troll", "boss " + FRIEND, "BOSS troll", "unboss SomeTroll", "unboss"):
            self.assertTrue(self.ended(command), command)

    def test_trusted_players_may_vote_boss_changes_but_not_unboss_the_trusted(self):
        self.c = self.client(BARHOST_FOLLOW="%s,%s" % (OWNER, FRIEND), BARHOST_AUTO_BOSS="Trusted")
        self.boss_and_setup()
        for caller in (FRIEND, "trusted"):
            self.assertFalse(self.ended("boss Newbie", user=caller), caller)
            self.assertFalse(self.ended("unboss SomeTroll", user=caller), caller)
            for command in ("unboss " + ME, "unboss", "unboss " + OWNER, "unboss Trusted"):
                self.assertTrue(self.ended(command, user=caller), (caller, command))

    START_BOX_VOTES = ("addBox 0 0 50 200 1", "clearbox 2", "loadBoxes", "split h 25", "preset ffa",
                       "bPreset default", "bSet mapmetadata_startbox_override 0", "bset startPosType 1",
                       "mapmetadata_startboxes_set 1", "bSet mapmetadata_startpos 0", "startpostype 2")

    def test_ends_a_random_players_vote_to_change_the_start_boxes(self):
        self.boss_and_setup()
        for command in self.START_BOX_VOTES:
            self.assertTrue(self.ended(command), command)
        self.assertFalse([x for x in self.run_for(4) if "Shame" in x])

    def test_trusted_players_may_vote_start_box_changes(self):
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_AUTO_BOSS="Trusted")
        self.boss_and_setup()
        for caller in (OWNER, "Trusted"):
            for command in self.START_BOX_VOTES:
                self.assertFalse(self.ended(command, user=caller), (caller, command))

    def test_leaves_other_votes_alone(self):
        self.boss_and_setup()
        self.assertFalse(self.ended("bSet critters 0"))
        self.assertFalse(self.ended("splitter"))
        self.assertFalse(self.ended("bossy thing"))
        self.assertFalse(self.ended("set map Tetrad_V2"))
        self.assertFalse(self.ended("forcestart"))

    def test_only_the_autohost_announces_votes(self):
        self.boss_and_setup()
        self.feed("SAIDBATTLE Troll * X called a vote for command \"unboss %s\" [!vote y]" % ME)
        self.assertNotIn("SAYBATTLE !ev", self.run_for(2))

    def test_not_boss_does_not_end_votes(self):
        self.join()
        self.feed(vote("Troll", "unboss " + ME))
        self.assertNotIn("SAYBATTLE !ev", self.run_for(2))

    def test_bosses_authorised_users_when_they_join(self):
        self.boss_and_setup()
        self.feed("JOINEDBATTLE 12 " + FRIEND, "JOINEDBATTLE 12 Stranger")
        sent = self.run_for(5)
        self.assertIn("SAYBATTLE !boss " + FRIEND, sent)
        self.assertFalse([x for x in sent if x == "SAYBATTLE !boss Stranger"])

    def test_bosses_and_guards_auto_boss_users_who_get_no_commands(self):
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_AUTO_BOSS="Trusted, Pal")
        self.boss_and_setup()
        self.feed("JOINEDBATTLE 12 Trusted", "JOINEDBATTLE 12 Pal", "JOINEDBATTLE 12 " + OWNER)
        sent = self.run_for(10)
        for n in ("Trusted", "Pal", OWNER):
            self.assertIn("SAYBATTLE !boss " + n, sent)
        self.feed("SAIDPRIVATE Trusted !private", "SAIDPRIVATE Trusted !status")
        sent = self.run_for(10)
        self.assertFalse([x for x in sent if x.startswith("SAYPRIVATE Trusted")])
        self.assertEqual(self.c.privacy, "public")
        self.assertTrue(self.ended("unboss trusted"))
        self.assertFalse([x for x in self.run_for(4) if "Shame" in x])

    def test_bosses_authorised_users_who_rejoin(self):
        self.boss_and_setup()
        self.feed("JOINEDBATTLE 12 " + OWNER, "LEFTBATTLE 12 " + OWNER)
        self.run_for(5)
        self.feed("JOINEDBATTLE 12 " + OWNER)
        self.assertIn("SAYBATTLE !boss " + OWNER, self.run_for(5))


class RingHint(Harness):
    """A ring in the room gets one line on how to vote an away player to
    spectator; the client takes no action itself."""

    def hints(self, sent):
        return [x for x in sent if x.startswith("SAYBATTLE If ")]

    def test_a_ring_gets_the_vote_spec_hint(self):
        self.boss_and_setup()
        self.feed("SAIDBATTLE Alice !ring Bob")
        sent = self.run_for(3)
        self.assertEqual(self.hints(sent), ["SAYBATTLE If Bob is away: vote them to spectator with !cv spec Bob, "
                                            "then !vote y."])
        self.assertFalse([x for x in sent if x.startswith("SAYBATTLE !spec") or x.startswith("SAYBATTLE !cv")])

    def test_at_most_once_a_minute(self):
        self.boss_and_setup()
        self.feed("SAIDBATTLE Alice !ring Bob", "SAIDBATTLE Carol !RING Bob")
        self.assertEqual(len(self.hints(self.run_for(30))), 1)
        self.feed("SAIDBATTLE Alice !ring Dave")
        self.assertEqual(len(self.hints(self.run_for(3))), 0)
        self.run_for(30)
        self.feed("SAIDBATTLE Alice !ring Dave")
        self.assertEqual(len(self.hints(self.run_for(3))), 1)

    def test_ignores_the_autohost_and_itself_and_other_chat(self):
        self.boss_and_setup()
        self.feed("SAIDBATTLEEX %s * !ring Bob" % HOST, "SAIDBATTLE %s !ring Bob" % ME,
                  "SAIDBATTLE Alice ring the bell", "SAIDBATTLE Alice !ringer")
        self.assertEqual(self.hints(self.run_for(3)), [])


class Welcome(Harness):
    """BARHOST_WELCOME is said privately, in the room, to each joiner."""

    TEXT = "Welcome to the room. Clyde is the auto host bot."

    def test_each_joiner_is_welcomed_privately(self):
        self.c = self.client(BARHOST_WELCOME=self.TEXT)
        self.boss_and_setup()
        self.feed("JOINEDBATTLE 12 Alice", "JOINEDBATTLE 12 Bob")
        sent = self.run_for(5)
        self.assertIn("SAYBATTLEPRIVATEEX Alice " + self.TEXT, sent)
        self.assertIn("SAYBATTLEPRIVATEEX Bob " + self.TEXT, sent)
        self.assertFalse([x for x in sent if x.startswith("SAYBATTLE ") and self.TEXT in x])

    def test_not_itself_nor_the_autohost_nor_other_rooms(self):
        self.c = self.client(BARHOST_WELCOME=self.TEXT)
        self.join()
        self.feed(opened("13"), "JOINEDBATTLE 13 Carol", "JOINEDBATTLE 12 " + HOST)
        self.assertFalse([x for x in self.run_for(5) if x.startswith("SAYBATTLEPRIVATEEX")])

    def test_unset_says_nothing(self):
        self.boss_and_setup()
        self.feed("JOINEDBATTLE 12 Alice")
        self.assertFalse([x for x in self.run_for(5) if x.startswith("SAYBATTLEPRIVATEEX")])


class SwitchAI(Harness):
    """!ai <channel>: the bots' AI (the game's own, or a release kept on the
    data volume), remembered; the engine follows between games, and the bots
    are replaced (as a player) only when the AI's name changes."""

    CHANNELS = ("barb=BARb;smrt-prod=SMRTBARb:github:o/r@latest;"
                "smrt-test=SMRTBARb:github:o/r@newest#*-test-linux.tar.gz")
    RELEASES = {"smrt-prod": "v2-prod", "smrt-test": "v3-test"}

    def setUp(self):
        super().setUp()
        self.c = self.make()

    def make(self, **extra):
        env = dict(BARHOST_FOLLOW=OWNER, BARHOST_AI_CHANNELS=self.CHANNELS)
        env.update(extra)
        c = self.client(**env)
        self.activated = []
        c.activate = self.activate
        return c

    def activate(self, channel):
        self.activated.append(channel)
        if self.c.s.ai_channels[channel][1] and channel not in self.c.ai_releases:
            raise ValueError("%s is not downloaded yet" % channel)
        return self.c.ai_releases.get(channel, "")

    def fetched(self, releases=None, changes=()):
        self.c.ai_updated(list(changes), [], dict(self.RELEASES if releases is None else releases))

    def pm(self, text):
        self.feed("SAIDPRIVATE %s %s" % (OWNER, text))
        return self.run_for(30)

    def replies(self, sent):
        return [x[len("SAYPRIVATE %s " % OWNER):] for x in sent if x.startswith("SAYPRIVATE %s " % OWNER)]

    def echo_bots(self, sent):
        for x in sent:
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))

    def set_up(self):
        """In its room with 8 BARb bots, the releases fetched, spectating."""
        self.fetched()
        self.echo_bots(self.boss_and_setup())
        self.c.set_seat(False, "test")
        self.run_for(2)
        self.activated.clear()

    def test_fetched_at_start_then_the_chosen_one_is_put_in_place(self):
        self.assertTrue(self.c.need_ai_update)
        self.fetched()
        self.run_for(1)
        self.assertEqual(self.activated, ["barb"])
        self.assertEqual(self.c.ai_active, ("barb", ""))

    def test_ai_lists_the_channels(self):
        self.set_up()
        reply = self.replies(self.pm("!ai"))
        self.assertEqual(reply, ["AI now: barb (BARb, the game's own). Choose with !ai <name> [version]: barb "
                                 "(BARb, the game's own), smrt-prod (SMRTBARb v2-prod), smrt-test (SMRTBARb v3-test)."])

    def test_another_ai_replaces_the_bots_as_a_player(self):
        self.set_up()
        sent = self.pm("!ai smrt-prod")
        self.assertEqual(self.activated, ["smrt-prod"])
        self.assertIn("OK: smrt-prod (SMRTBARb v2-prod) from the next game.", self.replies(sent))
        self.assertIn("SAYBATTLE Reloading the AI for the next game: SMRTBARb version v2-prod.", sent)
        self.assertEqual(len([x for x in sent if x.startswith("REMOVEBOT")]), 8)
        adds = [i for i, x in enumerate(sent) if x.startswith("ADDBOT")]
        self.assertEqual({sent[i].split(" ")[4] for i in adds}, {"SMRTBARb"})
        self.assertEqual(len(adds), 8)
        mine = [i for i, x in enumerate(sent) if x.startswith("MYBATTLESTATUS")]
        self.assertLess(mine[0], adds[0])                    # a player first

    def test_test_and_prod_swap_without_touching_the_bots(self):
        self.set_up()
        self.echo_bots(self.pm("!ai smrt-prod"))
        self.activated.clear()
        sent = self.pm("!ai smrt-test")
        self.assertEqual(self.activated, ["smrt-test"])
        self.assertIn("SAYBATTLE Reloading the AI for the next game: SMRTBARb version v3-test.", sent)
        self.assertFalse([x for x in sent if x.startswith(("REMOVEBOT", "ADDBOT"))])

    def test_the_choice_is_remembered(self):
        self.set_up()
        self.pm("!ai smrt-test")
        c = self.make()
        self.assertEqual((c.ai_channel, c.bot_ai), ("smrt-test", "SMRTBARb"))

    def test_the_same_ai_is_said_not_redone(self):
        self.set_up()
        sent = self.pm("!ai barb")
        self.assertEqual(self.replies(sent), ["OK: the bots already run barb (BARb, the game's own)."])
        self.assertEqual(self.activated, [])

    def test_an_unknown_name_changes_nothing(self):
        self.set_up()
        sent = self.pm("!ai smrt-dev")
        self.assertTrue(self.replies(sent)[0].startswith("No AI named smrt-dev. AI now: barb"))
        self.assertEqual((self.c.ai_channel, self.activated), ("barb", []))

    def test_during_a_game_it_waits_for_the_game_to_end(self):
        self.set_up()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        sent = self.pm("!ai smrt-prod")
        self.assertIn("OK: smrt-prod (SMRTBARb v2-prod) after this game.", self.replies(sent))
        self.assertEqual(self.activated, [])
        self.assertFalse([x for x in sent if x.startswith(("REMOVEBOT", "ADDBOT"))])
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(2)
        self.assertTrue(self.c.ai_updating)                    # the post-game check first
        self.assertEqual(self.activated, [])
        self.fetched()
        sent = self.run_for(30)
        self.assertEqual(self.activated, ["smrt-prod"])
        self.assertEqual({x.split(" ")[4] for x in sent if x.startswith("ADDBOT")}, {"SMRTBARb"})

    def test_a_release_not_downloaded_yet_is_fetched_first(self):
        self.fetched({})
        self.echo_bots(self.boss_and_setup())
        self.activated.clear()
        self.c.need_ai_update = False
        sent = self.pm("!ai smrt-test")
        self.assertIn("OK: smrt-test (SMRTBARb not downloaded yet) once it is downloaded.", self.replies(sent))
        self.assertTrue(self.c.need_ai_update)
        self.assertEqual(self.activated, [])
        self.fetched({"smrt-test": "v3-test"}, ["smrt-test: none -> v3-test"])
        self.run_for(5)
        self.assertEqual(self.activated, ["smrt-test"])
        self.assertEqual(self.c.ai_active, ("smrt-test", "v3-test"))

    def test_a_new_release_of_the_chosen_one_is_used_next_game(self):
        self.set_up()
        self.pm("!ai smrt-test")
        self.activated.clear()
        self.fetched({"smrt-prod": "v2-prod", "smrt-test": "v4-test"}, ["smrt-test: v3-test -> v4-test"])
        sent = self.run_for(5)
        self.assertIn("New AI release: smrt-test: v3-test -> v4-test; the next game uses it.", self.replies(sent))
        self.assertEqual(self.activated, ["smrt-test"])
        self.assertIn("SAYBATTLE Reloading the AI for the next game: SMRTBARb version v4-test.", sent)

    def test_a_new_release_of_another_one_is_only_reported(self):
        self.set_up()
        self.fetched({"smrt-prod": "v5-prod", "smrt-test": "v3-test"}, ["smrt-prod: v2-prod -> v5-prod"])
        sent = self.run_for(5)
        self.assertIn("New AI release: smrt-prod: v2-prod -> v5-prod; PM me !ai smrt-prod to use it.",
                      self.replies(sent))
        self.assertFalse([x for x in sent if x.startswith(("SAYBATTLE Reloading", "REMOVEBOT"))])

    def test_a_release_fetched_during_a_game_waits_for_the_end(self):
        self.set_up()
        self.pm("!ai smrt-test")
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.run_for(2)
        self.activated.clear()
        self.fetched({"smrt-prod": "v2-prod", "smrt-test": "v4-test"}, ["smrt-test: v3-test -> v4-test"])
        self.run_for(30)
        self.assertEqual(self.activated, [])
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(2)
        self.fetched({"smrt-prod": "v2-prod", "smrt-test": "v4-test"})    # the post-game check
        self.run_for(5)
        self.assertEqual(self.activated, ["smrt-test"])

    def test_a_version_pins_the_channel_to_that_release(self):
        self.set_up()
        sent = self.pm("!ai smrt-test v1-test")
        self.assertIn("OK: smrt-test v1-test, pinned, once it is downloaded.", self.replies(sent))
        self.assertTrue(self.c.need_ai_update)
        self.assertEqual(self.activated, [])                # not before the download
        self.assertEqual(self.c.channel_sources()["smrt-test"],
                         ("SMRTBARb", "github:o/r@v1-test#*-test-linux.tar.gz"))
        self.assertEqual(self.c.channel_sources()["smrt-prod"], ("SMRTBARb", "github:o/r@latest"))
        self.fetched({"smrt-prod": "v2-prod", "smrt-test": "v1-test"}, ["smrt-test: v3-test -> v1-test"])
        sent = self.run_for(5)
        self.assertIn("Pinned AI release in place: smrt-test: v3-test -> v1-test; the next game uses it.",
                      self.replies(sent))
        self.assertEqual(self.c.ai_active, ("smrt-test", "v1-test"))
        self.assertIn("SAYBATTLE Reloading the AI for the next game: SMRTBARb version v1-test.", sent)
        self.assertIn("smrt-test (SMRTBARb v1-test, pinned)", self.replies(self.pm("!ai"))[0])

    def test_a_pin_is_remembered(self):
        self.set_up()
        self.pm("!ai smrt-test v1-test")
        c = self.make()
        self.assertEqual((c.ai_channel, c.ai_pins), ("smrt-test", {"smrt-test": "v1-test"}))
        self.assertEqual(c.channel_sources()["smrt-test"][1], "github:o/r@v1-test#*-test-linux.tar.gz")

    def test_the_name_alone_follows_the_newest_again(self):
        self.set_up()
        self.pm("!ai smrt-test v1-test")
        self.fetched({"smrt-prod": "v2-prod", "smrt-test": "v1-test"})
        self.run_for(5)
        sent = self.pm("!ai smrt-test")
        self.assertIn("OK: smrt-test, following its newest release, once it is downloaded.", self.replies(sent))
        self.assertEqual(self.c.ai_pins, {})
        self.assertEqual(self.c.channel_sources()["smrt-test"][1], "github:o/r@newest#*-test-linux.tar.gz")
        self.assertTrue(self.c.need_ai_update)

    def test_newest_or_latest_also_unpins(self):
        self.set_up()
        self.pm("!ai smrt-prod v1-prod")
        self.fetched({"smrt-prod": "v1-prod", "smrt-test": "v3-test"})
        self.run_for(5)
        self.pm("!ai smrt-prod latest")
        self.assertEqual(self.c.ai_pins, {})

    def test_a_pin_asked_during_a_game_is_fetched_after_it(self):
        self.set_up()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        sent = self.pm("!ai smrt-test v1-test")
        self.assertIn("OK: smrt-test v1-test, pinned, after this game.", self.replies(sent))
        self.assertFalse(self.c.ai_updating)
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(2)
        self.assertTrue(self.c.ai_updating)
        self.assertEqual(self.activated, [])

    def test_a_bad_version_changes_nothing(self):
        self.set_up()
        self.assertEqual(self.replies(self.pm("!ai smrt-test v1/../x")),
                         ["v1/../x is not a release tag (such as v1.1150-test)."])
        self.assertEqual(self.replies(self.pm("!ai barb v1")), ["barb is the game's own AI: it has no versions."])
        self.assertEqual((self.c.ai_pins, self.c.ai_channel), ({}, "barb"))

    def test_a_release_that_cannot_be_had_is_said_and_unpinned(self):
        self.set_up()
        self.pm("!ai smrt-test v9-test")
        self.c.ai_updated([], ["smrt-test: HTTP Error 404: Not Found"], dict(self.RELEASES))
        sent = self.run_for(5)
        self.assertIn("Could not get smrt-test v9-test (HTTP Error 404: Not Found); it follows its newest "
                      "release again.", self.replies(sent))
        self.assertEqual(self.c.ai_pins, {})

    def test_a_later_failure_keeps_the_pin(self):
        self.set_up()
        self.pm("!ai smrt-test v1-test")
        self.fetched({"smrt-prod": "v2-prod", "smrt-test": "v1-test"})
        self.run_for(5)
        self.c.ai_updated([], ["smrt-test: timed out"], {"smrt-prod": "v2-prod", "smrt-test": "v1-test"})
        self.run_for(5)
        self.assertEqual(self.c.ai_pins, {"smrt-test": "v1-test"})

    def test_a_failed_switch_keeps_the_bots_and_says_why(self):
        self.set_up()
        self.c.activate = lambda channel: (_ for _ in ()).throw(OSError("disk full"))
        sent = self.pm("!ai smrt-prod")
        self.assertFalse([x for x in sent if x.startswith(("REMOVEBOT", "ADDBOT"))])
        self.assertEqual(self.c.bot_ai, "BARb")
        self.assertIn("disk full", self.c.last_error)

    def test_bots_takes_a_count_and_an_ai(self):
        self.set_up()
        sent = self.pm("!bots 10 smrt-test")
        self.assertIn("OK: 10 bots, smrt-test (SMRTBARb v3-test) from the next game.", self.replies(sent))
        self.assertEqual(self.c.want_bots, 10)
        self.assertEqual(self.c.ai_channel, "smrt-test")
        sent = self.pm("!bots 4 nope")
        self.assertTrue(self.replies(sent)[0].startswith("No AI named nope."))
        self.assertEqual(self.c.want_bots, 10)

    def test_old_commands_are_gone(self):
        self.set_up()
        self.assertEqual(self.replies(self.pm("!smrt")), ["Unknown command !smrt. PM me !help for the list."])
        self.assertEqual(self.c.ai_channel, "barb")

    def test_the_welcome_names_the_release_in_use(self):
        self.c = self.make(BARHOST_WELCOME="Welcome.")
        self.set_up()
        self.feed("JOINEDBATTLE 12 Alice")
        self.assertIn("SAYBATTLEPRIVATEEX Alice Welcome.", self.run_for(5))     # stock BARb: no version
        self.pm("!ai smrt-test")
        self.feed("JOINEDBATTLE 12 Bob")
        self.assertIn("SAYBATTLEPRIVATEEX Bob Welcome. Current AI: SMRTBARb version v3-test.", self.run_for(5))

    def test_back_to_the_games_own_ai_is_announced(self):
        self.set_up()
        self.echo_bots(self.pm("!ai smrt-prod"))
        sent = self.pm("!ai barb")
        self.assertIn("SAYBATTLE Reloading the AI for the next game: BARb (the game's own).", sent)
        self.assertFalse([x for x in sent if "github" in x.lower() or "http" in x.lower()])

    def test_strangers_cannot_switch(self):
        self.set_up()
        self.feed("SAIDPRIVATE Troll !ai smrt-test")
        self.run_for(5)
        self.assertEqual(self.c.ai_channel, "barb")

    def test_a_bad_channel_list_stops_the_client(self):
        with self.assertRaises(SystemExit):
            self.client(BARHOST_AI_CHANNELS="smrt=SMRTBARb:https://example.com/a.tar.gz")
        with self.assertRaises(SystemExit):
            self.client(BARHOST_AI_CHANNELS=self.CHANNELS, BARHOST_AI_CHANNEL="nope")


class AIUpdate(Harness):
    """New AI releases are checked and installed only between games."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_AI_UPDATE_EVERY="900")

    def test_checked_after_each_game_and_no_start_until_done(self):
        self.boss_and_setup()
        self.c.need_ai_update = self.c.ai_updating = False
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.clock.t += 2000
        self.run_for(5)
        self.assertFalse(self.c.need_ai_update)                         # never during a game
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(1)
        self.assertTrue(self.c.need_ai_update)                          # right after it
        self.feed("JOINEDBATTLE 12 Alice",
                  "CLIENTBATTLESTATUS Alice %d 0" % lc.battle_status(ready=1, mode=1, sync=1))
        self.assertFalse([x for x in self.run_for(70) if "To start the game" in x])   # updating
        self.c.ai_updated(["github:o/r@latest: v1.958-prod -> v1.960-prod"], [])
        sent = self.run_for(5)
        self.assertTrue([x for x in sent if x.startswith("SAYPRIVATE %s AI release updated (v1.958-prod -> "
                                                         "v1.960-prod)" % OWNER)], sent)
        self.assertTrue([x for x in sent if "To start the game" in x])

    def test_checked_every_interval_while_idle(self):
        self.boss_and_setup()                                  # waiting in its room, no game
        self.c.need_ai_update = self.c.ai_updating = False
        self.c.next_ai_check = self.clock.t + 900
        self.clock.t += 899
        self.c.tick()
        self.assertFalse(self.c.need_ai_update)
        self.clock.t += 2
        self.c.tick()
        self.assertTrue(self.c.need_ai_update)

    def test_a_failed_check_changes_nothing(self):
        self.c.ai_updating = True
        self.c.ai_updated([], ["github:o/r@latest: HTTP 403"])
        self.assertFalse(self.c.ai_updating)
        self.assertFalse([x for x in self.take() if x.startswith("SAYPRIVATE")])

    def test_zero_turns_it_off(self):
        self.c = self.client(BARHOST_AI_UPDATE_EVERY="0")
        self.log_in()
        self.clock.t += 100000
        self.c.tick()
        self.assertFalse(self.c.need_ai_update)


class Bonus(Harness):
    """"bonus" in chat gets the vote hint; a passed bonus vote is applied to
    every AI and kept for this room's later adds."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_BOT_SETUP="!setallaibonus 50", BARHOST_FOLLOW=OWNER)

    def hints(self, sent):
        return [x for x in sent if x.startswith("SAYBATTLE Bonus: ")]

    def passed(self, command):
        self.feed('SAIDBATTLEEX %s * Vote for command "%s" passed.' % (HOST, command))
        return self.run_for(3)

    def test_the_word_bonus_gets_the_hint_once_a_minute(self):
        self.boss_and_setup()
        self.feed("SAIDBATTLE Alice can we get more BONUS for the AI?")
        sent = self.run_for(3)
        self.assertEqual(self.hints(sent), ["SAYBATTLE Bonus: vote for all AI with !cv setallaibonus <0-100>, "
                                            "or one with !cv force %<AI name> bonus <0-100>, then !vote y."])
        self.feed("SAIDBATTLE Bob bonus pls")
        self.assertEqual(self.hints(self.run_for(30)), [])
        self.run_for(30)
        self.feed("SAIDBATTLE Bob bonus pls")
        self.assertEqual(len(self.hints(self.run_for(3))), 1)

    def test_not_for_other_words_the_autohost_or_itself(self):
        self.boss_and_setup()
        self.feed("SAIDBATTLE Alice bonuses and bonusround", "SAIDBATTLEEX %s * bonus set" % HOST,
                  "SAIDBATTLE %s bonus" % ME)
        self.assertEqual(self.hints(self.run_for(3)), [])

    def test_a_passed_vote_for_all_or_one_is_applied_to_every_ai(self):
        self.boss_and_setup()
        self.assertIn("SAYBATTLE !setallaibonus 30", self.passed("setallaibonus 30"))
        self.assertIn("SAYBATTLE !setallaibonus 80", self.passed("force SMRTBARbstable(2) bonus 80"))
        self.assertNotIn("SAYBATTLE !setallaibonus 5", self.passed("set map Tetrad_V2"))

    def test_the_voted_bonus_is_kept_for_later_adds_in_the_room(self):
        sent = self.boss_and_setup()
        for x in sent:
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))
        self.passed("setallaibonus 20")
        self.feed("SAIDPRIVATE %s !bots 10" % OWNER)
        sent = self.run_for(30)
        self.assertIn("SAYBATTLE !setallaibonus 20", sent)
        self.assertNotIn("SAYBATTLE !setallaibonus 50", sent)

    def test_only_the_autohosts_pass_message_counts(self):
        self.boss_and_setup()
        self.feed('SAIDBATTLE Troll * Vote for command "setallaibonus 100" passed.')
        self.assertNotIn("SAYBATTLE !setallaibonus 100", self.run_for(3))


class AiBonusVote(Harness):
    """BARHOST_BONUS_VOTE: the players vote the AI bonus through the client
    (`aibonus <n>`, then `y` / `n`): SPADS lets no non-boss vote it while
    the client is boss."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_BOT_SETUP="!setallaibonus 50", BARHOST_BONUS_VOTE="1")

    def players(self, *names):
        for n in names:
            self.feed("JOINEDBATTLE 12 " + n,
                      "CLIENTBATTLESTATUS %s %d 0" % (n, lc.battle_status(mode=1, sync=1)))

    def say(self, user, text, seconds=6):
        self.feed("SAIDBATTLE %s %s" % (user, text))
        return self.run_for(seconds)

    def applied(self, sent):
        return [x for x in sent if x.startswith("SAYBATTLE !setallaibonus")]

    def test_a_majority_of_the_players_passes_it(self):
        self.boss_and_setup()
        self.players("A", "B", "C", "D")
        sent = self.say("A", "aibonus 30")
        self.assertTrue([x for x in sent if "called a vote: every AI to 30% bonus" in x], sent)
        self.assertEqual(self.applied(sent), [])
        self.assertEqual(self.applied(self.say("B", "y")), [])        # 2 of 4: not a majority
        sent = self.say("C", "yes")
        self.assertEqual(self.applied(sent), ["SAYBATTLE !setallaibonus 30"])
        self.assertEqual(self.c.voted_bonus, 30)

    def test_enough_no_fails_it_and_the_deadline_counts_the_votes(self):
        self.boss_and_setup()
        self.players("A", "B", "C", "D")
        self.say("A", "aibonus 90")
        self.say("B", "n")
        sent = self.say("C", "no")
        self.assertTrue([x for x in sent if "vote failed (1 yes, 2 no)" in x], sent)
        self.say("B", "aibonus 10")                                    # B's own call
        self.say("C", "y")
        sent = self.run_for(lc.AIBONUS_VOTE_TIME)                     # 2 yes, 0 no at the deadline
        self.assertEqual(self.applied(sent), ["SAYBATTLE !setallaibonus 10"])

    def test_alone_it_passes_at_once_and_each_caller_waits_five_minutes(self):
        self.boss_and_setup()
        self.players("A")
        self.assertEqual(self.applied(self.say("A", "!aibonus 20")), ["SAYBATTLE !setallaibonus 20"])
        sent = self.say("A", "aibonus 40")
        self.assertTrue([x for x in sent if "you can call another AI bonus vote in 4:5" in x], sent)
        self.assertEqual(self.applied(sent), [])
        self.run_for(300)
        self.assertEqual(self.applied(self.say("A", "aibonus 40")), ["SAYBATTLE !setallaibonus 40"])

    def test_not_from_spectators_or_out_of_range(self):
        self.boss_and_setup()
        self.players("A")
        self.feed("JOINEDBATTLE 12 Spec")
        sent = self.say("Spec", "aibonus 30") + self.say("A", "aibonus 300")
        self.assertEqual(self.applied(sent), [])
        self.assertTrue([x for x in sent if "only players" in x])
        self.assertTrue([x for x in sent if "0 to 100" in x])
        self.assertEqual(self.say("A", "y"), [])                      # no vote: plain chat

    def test_the_word_bonus_points_to_the_command(self):
        self.boss_and_setup()
        self.players("A")
        sent = self.say("A", "more bonus please")
        self.assertIn("SAYBATTLE " + lc.AIBONUS_HINT % "50%", sent)
        self.say("A", "aibonus 70")
        self.run_for(60)
        self.assertIn("SAYBATTLE " + lc.AIBONUS_HINT % "70%", self.say("A", "bonus?"))

    def bots_in(self, sent, bonus):
        names = []
        for x in sent:
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %d 0 %s" % (w[1], ME, lc.battle_status(
                    ready=1, team=len(names) + 1, ally=1, mode=1, sync=1, bonus=bonus), w[4]))
                names.append(w[1])
        return names

    def set_bonus(self, names, bonus):
        for i, n in enumerate(names):
            self.feed("UPDATEBOT 12 %s %d 0" % (n, lc.battle_status(
                ready=1, team=i + 1, ally=1, mode=1, sync=1, bonus=bonus)))

    def test_a_boss_setting_the_bonus_is_seen_and_kept_for_later_adds(self):
        self.c = self.client(BARHOST_BOT_SETUP="!setallaibonus 50", BARHOST_BONUS_VOTE="1", BARHOST_FOLLOW=OWNER)
        names = self.bots_in(self.boss_and_setup(), 30)
        self.players("A")
        self.assertIn("SAYBATTLE " + lc.AIBONUS_HINT % "30%", self.say("A", "bonus?"))
        self.set_bonus(names, 70)                   # the boss: !setallaibonus 70
        self.run_for(60)
        self.assertIn("SAYBATTLE " + lc.AIBONUS_HINT % "70%", self.say("A", "bonus?"))
        self.set_bonus(names[:1], 80)               # the boss: !force <one> bonus 80
        self.run_for(60)
        self.assertIn("SAYBATTLE " + lc.AIBONUS_HINT % "70-80%", self.say("A", "bonus?"))
        self.feed("SAIDPRIVATE %s !bots %d" % (OWNER, len(names) + 2))
        sent = self.run_for(30)
        self.assertIn("SAYBATTLE !setallaibonus 70", sent)     # the last bonus all AIs had
        self.assertNotIn("SAYBATTLE !setallaibonus 50", sent)

    def test_the_title_follows_the_bonus(self):
        self.c = self.client(BARHOST_BOT_SETUP="!setallaibonus 50", BARHOST_BONUS_VOTE="1",
                             BARHOST_ROOM_NAME="Human vs SMRTBARb {bonus}+")
        sent = self.boss_and_setup()
        self.assertIn("SAYBATTLE !rename Human vs SMRTBARb 50+", sent)
        names = self.bots_in(sent, 50)
        self.assertEqual([x for x in self.run_for(30) if "!rename" in x], [])     # unchanged
        self.set_bonus(names, 70)                   # a boss's !setallaibonus 70
        sent = self.run_for(30)
        self.assertEqual([x for x in sent if "!rename" in x], ["SAYBATTLE !rename Human vs SMRTBARb 70+"])
        self.set_bonus(names[:1], 80)
        self.assertIn("SAYBATTLE !rename Human vs SMRTBARb 70-80+", self.run_for(30))

    def test_an_empty_player_team_resets_the_room_once(self):
        names = self.bots_in(self.boss_and_setup(), 50)
        self.assertNotIn("SAYBATTLE " + lc.IDLE_RESET_SAY, self.run_for(120))   # nobody played yet
        self.players("A")
        self.say("A", "aibonus 20")
        self.set_bonus(names, 20)
        self.run_for(30)
        self.feed("CLIENTBATTLESTATUS A %d 0" % lc.battle_status(mode=0, sync=1))   # A spectates
        self.assertNotIn("SAYBATTLE " + lc.IDLE_RESET_SAY, self.run_for(lc.IDLE_RESET_AFTER - 10))
        sent = self.run_for(20)
        self.assertIn("SAYBATTLE " + lc.IDLE_RESET_SAY, sent)
        for c in ("!preset custom", "!bSet experimentallegionfaction 1", "!setallaibonus 50"):
            self.assertIn("SAYBATTLE " + c, sent)
        self.assertIsNone(self.c.voted_bonus)
        self.assertNotIn("SAYBATTLE " + lc.IDLE_RESET_SAY, self.run_for(300))  # once per idle spell
        self.feed("CLIENTBATTLESTATUS A %d 0" % lc.battle_status(mode=1, sync=1))   # A plays again
        self.run_for(10)
        self.feed("CLIENTBATTLESTATUS A %d 0" % lc.battle_status(mode=0, sync=1))
        self.assertNotIn("SAYBATTLE " + lc.IDLE_RESET_SAY, self.run_for(lc.IDLE_RESET_EVERY))  # changed nothing
        self.feed("CLIENTBATTLESTATUS A %d 0" % lc.battle_status(mode=1, sync=1))
        self.say("A", "!bSet experimentallegionfaction 0")
        self.feed("CLIENTBATTLESTATUS A %d 0" % lc.battle_status(mode=0, sync=1))
        self.assertIn("SAYBATTLE " + lc.IDLE_RESET_SAY, self.run_for(lc.IDLE_RESET_AFTER + 10))

    def test_joining_and_leaving_alone_never_resets(self):
        self.boss_and_setup()
        for _ in range(5):
            self.players("A")
            self.say("A", "hi")
            self.say("A", "!ring")
            self.say(HOST, '* Vote for command "start" passed.')
            self.run_for(20)
            self.feed("LEFTBATTLE 12 A")
            self.assertNotIn("SAYBATTLE " + lc.IDLE_RESET_SAY, self.run_for(lc.IDLE_RESET_AFTER + 60))

    def test_resets_at_most_once_per_cooldown(self):
        self.boss_and_setup()

        def change_and_leave():
            self.players("A")
            self.say(HOST, '* Vote for command "bSet startmetal 2000" passed.')
            self.feed("LEFTBATTLE 12 A")

        change_and_leave()
        self.assertIn("SAYBATTLE " + lc.IDLE_RESET_SAY, self.run_for(lc.IDLE_RESET_AFTER + 10))
        sent = []
        for _ in range(4):                  # a change, then everyone leaves, every 3 minutes
            change_and_leave()
            sent += self.run_for(180)
        self.assertNotIn("SAYBATTLE " + lc.IDLE_RESET_SAY, sent)       # within 15 minutes of the last
        self.assertEqual(self.run_for(200).count("SAYBATTLE " + lc.IDLE_RESET_SAY), 1)

    def test_reminded_after_each_game(self):
        self.boss_and_setup()
        self.players("A")
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.run_for(30)
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        sent = self.run_for(10)
        self.assertEqual(sent.count("SAYBATTLE " + lc.AIBONUS_HINT % "50%"), 1)
        self.assertNotIn("SAYBATTLE " + lc.AIBONUS_HINT % "50%", self.run_for(120))



class MapVote(Harness):
    """BARHOST_MAP_VOTE: the players vote the rotation's next map through the
    client (`votemap`, then `y` / `n`), as the AI bonus vote."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_MAP_ROTATION=ROTATION, BARHOST_MAP_VOTE="1", BARHOST_BONUS_VOTE="1")

    players = AiBonusVote.players
    say = AiBonusVote.say

    def boss_on_first_map(self):
        MapRotation.boss(self)
        self.feed("UPDATEBATTLEINFO 12 1 0 123 All That Simmers v1.2")
        self.c.fetched(self.c.battle, True)
        self.run_for(3)

    def maps(self, sent):
        return [x for x in sent if x.startswith("SAYBATTLE !map ")]

    def play_a_game(self):
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.run_for(30)
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        return self.run_for(10)

    def nextmap_passed(self, user=HOST):
        self.feed('SAIDBATTLEEX %s * Vote for command "nextMap" passed.' % user)
        return self.run_for(3)

    def test_a_passed_nextmap_vote_is_followed_by_the_rotations_next_map(self):
        self.boss_on_first_map()
        sent = self.nextmap_passed()
        self.assertEqual(self.maps(sent), ["SAYBATTLE !map Cells"])
        self.assertIn("SAYBATTLE " + lc.NEXTMAP_SAY % "Cells", sent)
        self.feed("UPDATEBATTLEINFO 12 1 0 123 Some Autohost Map 1.0")     # the autohost's own next map
        self.assertEqual(self.maps(self.run_for(3)), [])
        self.feed("UPDATEBATTLEINFO 12 1 0 123 Cells 2")
        self.c.fetched(self.c.battle, True)
        self.assertEqual(self.maps(self.run_for(30)), [])                  # Cells sticks
        self.assertEqual(self.c.map_index, 1)

    def test_a_bosss_nextmap_sticks(self):
        self.boss_on_first_map()
        self.feed("SAIDBATTLE %s !nextmap" % OWNER, "SAIDBATTLEEX %s * Next map: Tundra" % HOST,
                  "UPDATEBATTLEINFO 12 1 0 123 Tundra 1.0")
        self.assertEqual(self.maps(self.run_for(30)), [])

    def test_only_the_autohosts_nextmap_pass_counts(self):
        self.boss_on_first_map()
        self.assertEqual(self.maps(self.nextmap_passed(user="Troll")), [])

    def test_a_passed_vote_moves_to_the_next_map_and_the_rotation_goes_on(self):
        self.boss_on_first_map()
        self.players("A", "B", "C")
        sent = self.say("A", "votemap")
        self.assertTrue([x for x in sent if "called a vote: next map Cells" in x], sent)
        self.assertEqual(self.maps(sent), [])
        sent = self.say("B", "y")                                      # 2 of 3
        self.assertTrue([x for x in sent if "map vote passed (2 yes, 0 no)" in x], sent)
        self.assertEqual(self.maps(sent), ["SAYBATTLE !map Cells"])
        self.feed("UPDATEBATTLEINFO 12 1 0 123 Cells 1.2")
        self.c.fetched(self.c.battle, True)
        self.run_for(3)
        self.assertEqual(self.maps(self.play_a_game()), ["SAYBATTLE !map Forge"])

    def test_a_failed_vote_keeps_the_map_and_one_vote_runs_at_a_time(self):
        self.boss_on_first_map()
        self.players("A", "B", "C")
        self.say("A", "votemap")
        sent = self.say("B", "aibonus 30") + self.say("C", "!votemap")
        self.assertEqual(len([x for x in sent if "a vote is already running (next map Cells)" in x]), 2, sent)
        self.say("B", "n")
        sent = self.say("C", "no")
        self.assertTrue([x for x in sent if "map vote failed (1 yes, 2 no)" in x], sent)
        self.assertEqual(self.maps(sent + self.run_for(10)), [])
        self.assertEqual(self.c.map_index, 0)

    def test_between_games_only_and_each_caller_waits_the_cooldown(self):
        self.boss_on_first_map()
        self.players("A")
        self.assertEqual(self.maps(self.say("A", "votemap")), ["SAYBATTLE !map Cells"])   # alone: at once
        self.feed("UPDATEBATTLEINFO 12 1 0 123 Cells 1.2")
        self.c.fetched(self.c.battle, True)
        sent = self.say("A", "votemap")
        self.assertTrue([x for x in sent if "you can call another map vote in 4:5" in x], sent)
        self.run_for(300)
        self.feed("CLIENTSTATUS %s 65" % HOST)                # a game starts
        self.run_for(5)
        sent = self.say("A", "votemap")
        self.assertTrue([x for x in sent if "the map can be changed between games" in x], sent)
        self.assertEqual(self.maps(sent), [])

    def test_not_from_spectators(self):
        self.boss_on_first_map()
        self.players("A")
        self.feed("JOINEDBATTLE 12 Spec")
        sent = self.say("Spec", "votemap")
        self.assertTrue([x for x in sent if "only players can call a map vote" in x], sent)
        self.assertEqual(self.maps(sent), [])

    def test_a_map_command_and_the_end_of_a_game_get_the_how_to(self):
        self.boss_on_first_map()
        self.players("A")
        self.assertIn("SAYBATTLE " + lc.VOTEMAP_HINT % "Cells", self.say("A", "!cv map Tundra"))
        self.assertNotIn("SAYBATTLE " + lc.VOTEMAP_HINT % "Cells", self.say("A", "!map Tundra"))  # once a minute
        self.feed("UPDATEBATTLEINFO 12 1 0 123 All That Simmers v1.2")
        sent = self.play_a_game()                                      # the rotation moves on to Cells
        self.feed("UPDATEBATTLEINFO 12 1 0 123 Cells 1.2")
        self.c.fetched(self.c.battle, True)
        sent += self.run_for(10)
        self.assertEqual(sent.count("SAYBATTLE " + lc.VOTEMAP_HINT % "Forge"), 1, sent)

    def test_a_trusted_boss_changing_the_map_gets_no_how_to(self):
        # A boss's `!map` changes the map at once; it is no vote.
        self.c = self.client(BARHOST_MAP_ROTATION=ROTATION, BARHOST_MAP_VOTE="1", BARHOST_AUTO_BOSS="Trusty")
        self.boss_on_first_map()
        self.players("Trusty")
        self.assertNotIn("SAYBATTLE " + lc.VOTEMAP_HINT % "Cells", self.say("Trusty", "!map Tundra"))
        self.assertIn("SAYBATTLE " + lc.VOTEMAP_HINT % "Cells", self.say("Trusty", "!cv map Tundra"))

    def test_off_by_default(self):
        self.c = self.client(BARHOST_MAP_ROTATION=ROTATION)
        self.boss_on_first_map()
        self.players("A")
        self.assertEqual(self.maps(self.say("A", "votemap")), [])


ADVERT = "Scores and leaderboards: https://scoreboard.example"


class Advert(Harness):
    """BARHOST_ADVERT: said once a game is over and the next map is in, then
    every BARHOST_ADVERT_EVERY seconds while no game runs; never in a game."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_MAP_ROTATION=ROTATION, BARHOST_ADVERT=ADVERT)

    def ads(self, sent):
        return [x for x in sent if x == "SAYBATTLE " + ADVERT]

    def test_after_a_game_once_the_next_map_is_in_then_every_10_minutes(self):
        MapVote.boss_on_first_map(self)
        AiBonusVote.players(self, "A")
        sent = MapVote.play_a_game(self)
        self.assertEqual(MapVote.maps(self, sent), ["SAYBATTLE !map Cells"])
        self.assertEqual(self.ads(sent), [])                     # not before the map change
        self.feed("UPDATEBATTLEINFO 12 1 0 123 Cells 1.2")
        self.c.fetched(self.c.battle, True)
        self.assertEqual(len(self.ads(self.run_for(10))), 1)
        self.assertEqual(self.ads(self.run_for(580)), [])
        self.assertEqual(len(self.ads(self.run_for(30))), 1)     # 10 minutes on

    def test_every_10_minutes_between_games_but_never_during_one(self):
        MapVote.boss_on_first_map(self)
        AiBonusVote.players(self, "A")
        self.assertEqual(len(self.ads(self.run_for(610))), 1)
        self.feed("CLIENTSTATUS %s 65" % HOST)                 # a game starts
        self.assertEqual(self.ads(self.run_for(1300)), [])

    def test_not_to_an_empty_room(self):
        MapVote.boss_on_first_map(self)
        self.assertEqual(self.ads(self.run_for(1300)), [])

    def test_off_without_the_setting(self):
        self.c = self.client(BARHOST_MAP_ROTATION=ROTATION)
        MapVote.boss_on_first_map(self)
        AiBonusVote.players(self, "A")
        self.assertEqual(self.ads(MapVote.play_a_game(self) + self.run_for(1300)), [])


class StartBoxes(Harness):
    """BARHOST_START_BOXES_FILE: after every map change, as boss, the map's start
    boxes are set (first of BARHOST_START_BOX_KEYS), or "0" for a map with none."""

    def setUp(self):
        super().setUp()
        self.file = os.path.join(self.tmp.name, "boxes.json")
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump({"All That Simmers": {"override": "OVR", "team2_ai": "T2A"},
                       "All That Smolders": {"team2_ai": None, "override": "SMOVR"},
                       "Coastlines_Dry": {"team2_ai": "COAST"},
                       "Cells": {"note": "no boxes"}}, f)
        self.c = self.client(BARHOST_START_BOXES_FILE=self.file)

    def boxes(self, sent):
        return [x.split(" ", 3)[3] for x in sent if x.startswith("SAYBATTLE !bSet mapmetadata_startbox_override ")]

    def boss(self):
        self.join()
        self.run_for(2)
        self.c.fetched(self.c.battle, True)
        self.feed("SAIDBATTLEEX %s * Boss mode enabled for %s" % (HOST, ME))
        return self.run_for(30)

    def map_is(self, name):
        self.feed("UPDATEBATTLEINFO 12 1 0 123 " + name)
        return self.run_for(5)

    def test_lookup(self):
        b = self.c.s.start_boxes
        self.assertEqual(lc.start_box_for(b, "All That Simmers v1.2"), "T2A")
        self.assertEqual(lc.start_box_for(b, "All That Smolders 1.0"), "SMOVR")   # null team2_ai: override
        self.assertEqual(lc.start_box_for(b, "Coastlines Dry 1.1"), "COAST")
        self.assertIsNone(lc.start_box_for(b, "Cells 2"))
        self.assertIsNone(lc.start_box_for(b, "All That Glitters v2"))

    def test_set_on_the_rooms_map_once_boss(self):
        self.assertEqual(self.boxes(self.run_for(1)), [])
        sent = self.boss()
        self.assertEqual(self.boxes(sent), ["0"])          # the room's map has none

    def test_every_map_change_by_anyone_once_each(self):
        self.boss()
        self.assertEqual(self.boxes(self.map_is("All That Simmers v1.2")), ["T2A"])
        self.assertEqual(self.boxes(self.map_is("All That Simmers v1.2")), [])
        self.feed("SAIDBATTLE %s !map Coastlines" % OWNER)
        self.assertEqual(self.boxes(self.map_is("Coastlines Dry 1.1")), ["COAST"])
        self.assertEqual(self.boxes(self.map_is("Cells 2")), ["0"])

    def test_not_before_boss(self):
        self.join()
        self.run_for(2)
        self.assertEqual(self.boxes(self.map_is("All That Simmers v1.2")), [])

    def test_no_file_no_command(self):
        c = self.client()
        self.assertEqual(c.s.start_boxes, {})
        self.c = c
        self.assertEqual(self.boxes(self.boss()), [])

    def test_unreadable_file_is_none(self):
        self.assertEqual(self.client(BARHOST_START_BOXES_FILE=self.file + ".missing").s.start_boxes, {})

    def pm(self, text, user=OWNER):
        self.feed("SAIDPRIVATE %s %s" % (user, text))
        return self.run_for(5)

    def told(self, sent, user=OWNER):
        return [x for x in sent if x.startswith("SAYPRIVATE %s " % user)]

    def test_off_every_map_gets_its_own_boxes(self):
        self.c = self.client(BARHOST_START_BOXES_FILE=self.file, BARHOST_CUSTOM_START_BOXES="0")
        self.assertEqual(self.boxes(self.boss()), ["0"])
        self.assertEqual(self.boxes(self.map_is("All That Simmers v1.2")), ["0"])
        self.assertEqual(self.boxes(self.map_is("Coastlines Dry 1.1")), ["0"])

    def test_boxes_by_pm_at_once_and_kept_across_a_restart(self):
        env = dict(BARHOST_START_BOXES_FILE=self.file, BARHOST_CUSTOM_START_BOXES="0", BARHOST_FOLLOW=OWNER)
        self.c = self.client(**env)
        self.boss()
        self.map_is("All That Simmers v1.2")
        self.assertIn("Start boxes: off (each map's own) (the default)", self.told(self.pm("!boxes"))[0])
        sent = self.pm("!boxes on")
        self.assertEqual(self.boxes(sent), ["T2A"])
        self.assertIn("Start boxes: on (mine for each map) (chosen by PM", self.told(sent)[0])
        self.assertTrue(self.client(**env).s.custom_start_boxes)
        sent = self.pm("!boxes default")
        self.assertEqual(self.boxes(sent), ["0"])
        self.assertIn("off (each map's own) (the default)", self.told(sent)[0])
        self.assertFalse(self.client(**env).s.custom_start_boxes)
        self.assertIn("!boxes on, !boxes off", self.told(self.pm("!boxes maybe"))[0])

    def test_boxes_only_for_authorised_users(self):
        self.c = self.client(BARHOST_START_BOXES_FILE=self.file, BARHOST_FOLLOW=OWNER)
        self.boss()
        sent = self.pm("!boxes off", user="Mallory")
        self.assertEqual((self.told(sent, "Mallory"), self.boxes(sent)), ([], []))
        self.assertTrue(self.c.s.custom_start_boxes)

    def test_boxes_without_a_file(self):
        self.c = self.client(BARHOST_FOLLOW=OWNER)
        self.boss()
        sent = self.pm("!boxes on")
        self.assertIn("no start box file", self.told(sent)[0])
        self.assertEqual(self.boxes(sent), [])


class Rehost(Harness):
    """!rehost: end any game (the bots resign: the engine leaves), leave the
    room, and take a new one (never the room just left)."""

    OTHER = "Host[EU1][4]"

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_MAP_ROTATION="Map A;Map B;Map C")

    def pm(self, text, user=OWNER, wait=5):
        self.feed("SAIDPRIVATE %s %s" % (user, text))
        return self.run_for(wait)

    def set_up(self):
        sent = self.boss_and_setup()
        for x in sent:
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))
        self.feed("ADDUSER %s XX 0 2" % self.OTHER, opened("14", founder=self.OTHER))

    def joins(self, sent):
        return [x.split(" ")[1] for x in sent if x.startswith("JOINBATTLE ")]

    def test_without_a_game_it_moves_at_once(self):
        self.set_up()
        sent = self.pm("!rehost", wait=20)
        self.assertIn("SAYBATTLE %s asked me to rehost: moving to a new room." % OWNER, sent)
        self.assertEqual(len([x for x in sent if x.startswith("REMOVEBOT")]), 8)
        self.assertIn("LEAVEBATTLE", sent)
        self.assertLess(sent.index("SAYBATTLE %s asked me to rehost: moving to a new room." % OWNER),
                        sent.index("LEAVEBATTLE"))
        self.assertEqual(self.joins(sent), ["14"])                  # not room 12 again
        self.assertIsNone(self.c.battle)
        self.assertEqual(self.c.map_index, 0)                        # no game was played

    def test_in_a_game_the_bots_resign_then_it_moves(self):
        self.set_up()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        before = self.c.map_index
        sent = self.pm("!rehost")
        self.assertEqual(getattr(self.proc, "terminated", 0), 1)
        self.assertIn("SAYBATTLE %s ended this game: resigning the AI bots and moving to a new room." % OWNER, sent)
        self.assertNotIn("LEAVEBATTLE", sent)                         # not while the engine is in the game
        self.assertTrue([x for x in sent if x.startswith("SAYPRIVATE %s Ending the game" % OWNER)])
        self.assertEqual(self.pm("!rehost"), ["SAYPRIVATE %s Already rehosting." % OWNER])
        self.proc.code = 0                                            # the engine left; the game goes on
        sent = self.run_for(20)
        self.assertIn("LEAVEBATTLE", sent)
        self.assertEqual(self.joins(sent), ["14"])
        self.assertTrue([x for x in sent if x.startswith("SAYPRIVATE %s Game over for my bots" % OWNER)])
        self.assertEqual(self.c.map_index, (before + 1) % 3)          # the next map in the next room
        self.assertIsNone(self.c.game_proc)

    def test_an_engine_that_does_not_quit_is_killed(self):
        self.set_up()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.pm("!rehost")
        self.run_for(20)
        self.assertEqual(getattr(self.proc, "killed", 0), 1)

    def test_only_authorised_users(self):
        self.set_up()
        self.assertFalse([x for x in self.pm("!rehost", user="Troll", wait=20) if x == "LEAVEBATTLE"])
        self.assertIsNotNone(self.c.battle)

    def test_follow_mode_has_no_rehost(self):
        self.c = self.client(BARHOST_CLIENT_MODE="follow", BARHOST_FOLLOW=OWNER)
        self.log_in()
        self.assertTrue([x for x in self.pm("!rehost") if "Unknown command !rehost" in x])



class FrozenGame(Harness):
    """BARHOST_FROZEN_GAME_MINUTES: a game whose frame stands still while the
    engine's widget keeps writing (paused with every player resigned never
    reaches game over) is given up: the bots resign and the client rehosts."""

    OTHER = Rehost.OTHER
    set_up = Rehost.set_up
    joins = Rehost.joins

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_MAP_ROTATION="Map A;Map B;Map C",
                             BARHOST_FROZEN_GAME_MINUTES="10")
        self.run = Path(self.tmp.name) / "run"
        (self.run / "LuaUI/Config").mkdir(parents=True)
        self.proc.write_dir = self.run
        self.rows = ["unix_s,mono_s,game_frame,server_frame"]
        self.stamp = 0

    def row(self, frame):
        self.stamp += 5
        self.rows.append("%d,%d.0,%d,%d" % (1800000000 + self.stamp, self.stamp, frame, frame))
        (self.run / "LuaUI/Config/rjm-engine-timeline.csv").write_text("\n".join(self.rows) + "\n")

    def play(self, minutes, frame=None):
        """Run for `minutes`, the widget writing a row each minute (the
        frame moving on unless `frame` is given)."""
        out = []
        for _ in range(int(minutes)):
            self.row(frame if frame is not None else self.stamp * 30)
            out += self.run_for(60)
        return out

    def start(self):
        self.set_up()
        self.feed("CLIENTSTATUS %s 65" % HOST)

    def test_a_game_standing_still_is_given_up(self):
        self.start()
        self.play(30)
        self.assertEqual(getattr(self.proc, "terminated", 0), 0)
        sent = self.play(9, frame=44974)
        self.assertEqual(getattr(self.proc, "terminated", 0), 0)
        sent = self.play(3, frame=44974)
        self.assertEqual(self.proc.terminated, 1)
        self.assertTrue([x for x in sent if x.startswith("SAYBATTLE This game has stood still for 1")])
        self.assertNotIn("LEAVEBATTLE", sent)
        self.proc.code = 0                                    # the engine left; the frozen game goes on
        sent = self.run_for(20)
        self.assertIn("LEAVEBATTLE", sent)
        self.assertEqual(self.joins(sent), ["14"])
        self.assertFalse([x for x in sent if x.startswith("SAYPRIVATE")])   # nobody asked: nobody told

    def seat(self, *names):
        for n in names:
            self.feed("JOINEDBATTLE 12 " + n,
                      "CLIENTBATTLESTATUS %s %d 0" % (n, lc.battle_status(ready=1, mode=1, sync=1)))

    def console(self, *lines):
        with (self.run / "engine-console.log").open("a") as f:
            for line in lines:
                f.write("[t=00:26:47.668125][f=0044974] %s\n" % line)

    def test_a_pause_with_players_in_the_game_is_theirs(self):
        self.set_up()
        self.seat("Alice", "Bob")
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.play(5)
        self.console("Player Alice resigned from the game: forced spec")
        self.play(30, frame=44974)                             # Bob is still in it
        self.assertEqual(getattr(self.proc, "terminated", 0), 0)
        self.console("Player Bob left the game:  normal quit")
        self.play(2, frame=44974)
        self.assertEqual(self.proc.terminated, 1)

    def test_an_abandoned_pause_is_given_up_after_an_hour(self):
        self.set_up()
        self.seat("Alice")
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.play(5)
        self.play(59, frame=44974)
        self.assertEqual(getattr(self.proc, "terminated", 0), 0)
        self.play(3, frame=44974)
        self.assertEqual(self.proc.terminated, 1)

    def test_a_short_pause_is_not(self):
        self.start()
        self.play(5)
        self.play(8, frame=999)
        self.play(20)
        self.assertEqual(getattr(self.proc, "terminated", 0), 0)

    def test_a_silent_widget_or_an_unstarted_game_is_no_evidence(self):
        self.start()
        self.row(500)
        self.run_for(30 * 60)                                  # no new rows
        self.play(30, frame=0)                                 # loading
        self.assertEqual(getattr(self.proc, "terminated", 0), 0)

    def test_off_by_default(self):
        self.c = self.client(BARHOST_FOLLOW=OWNER)
        self.start()
        self.play(30, frame=44974)
        self.assertEqual(getattr(self.proc, "terminated", 0), 0)



class BrokenAutohost(Harness):
    """Room mode leaves an autohost whose game server crashes at every start,
    or that holds a game update until its room is empty (it never is while
    the client sits in it), and takes a new room."""

    OTHER = Rehost.OTHER
    set_up = Rehost.set_up
    joins = Rehost.joins

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_FOLLOW=OWNER, BARHOST_MAP_ROTATION="Map A;Map B;Map C")

    def host(self, text):
        self.feed("SAIDBATTLEEX %s * %s" % (HOST, text))

    def failed_start(self):
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.run_for(2)
        self.host("Spring crashed ! (running time: 7 seconds)")
        out = self.run_for(2)
        self.feed("CLIENTSTATUS %s 64" % HOST)
        return out

    def test_two_failed_starts_in_a_row_move_it(self):
        self.set_up()
        self.assertNotIn("LEAVEBATTLE", self.failed_start())
        self.proc.code = 233                                  # the engine timed out connecting
        self.run_for(40)
        self.proc = type(self.proc)()
        sent = self.failed_start()
        self.assertTrue([x for x in sent if x.startswith("SAYBATTLE The autohost crashed at 2 starts in a row")])
        self.assertNotIn("LEAVEBATTLE", sent)                 # not while the engine is in the game
        self.proc.code = 233
        sent = self.run_for(20)
        self.assertIn("LEAVEBATTLE", sent)
        self.assertEqual(self.joins(sent), ["14"])
        self.assertFalse([x for x in sent if x.startswith("SAYPRIVATE")])

    def test_a_crash_late_in_a_game_or_a_normal_game_between_does_not(self):
        self.set_up()
        self.host("Spring crashed ! (running time: 7 seconds)")
        self.host("Server stopped (running time: 36 minutes and 56 seconds)")
        self.host("Spring crashed ! (running time: 6 seconds)")
        self.host("Spring crashed ! (running time: 25 minutes and 3 seconds)")
        self.assertNotIn("LEAVEBATTLE", self.run_for(20))

    def test_a_waiting_game_update_moves_it_once_nobody_plays(self):
        self.set_up()
        self.feed("JOINEDBATTLE 12 Alice",
                  "CLIENTBATTLESTATUS Alice %d 0" % lc.battle_status(ready=1, mode=1, sync=1))
        self.host("New version of current mod detected (Beyond All Reason test-31783-a096d9f), "
                  "switching when battle is empty (use !rehost to force)")
        self.assertNotIn("LEAVEBATTLE", self.run_for(20))     # Alice plays
        self.feed("LEFTBATTLE 12 Alice")
        sent = self.run_for(20)
        self.assertTrue([x for x in sent if x.startswith("SAYBATTLE The autohost has a game update waiting")])
        self.assertIn("LEAVEBATTLE", sent)
        self.assertEqual(self.joins(sent), ["14"])

    def test_only_the_autohost_counts(self):
        self.set_up()
        for _ in range(3):
            self.feed("SAIDBATTLE Troll Spring crashed ! (running time: 7 seconds)")
        self.assertNotIn("LEAVEBATTLE", self.run_for(10))


class StopRequest(Harness):
    """An authorised user's !stop: while this engine is in the game it leaves,
    which resigns the bots; if it is not (the game goes on without it), the
    players are asked to vote."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_FOLLOW=OWNER)

    def pm(self, text, user=OWNER):
        self.feed("SAIDPRIVATE %s %s" % (user, text))
        return self.run_for(5)

    def test_in_a_game_the_bots_are_resigned(self):
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.assertEqual(len(self.launched), 1)
        sent = self.pm("!stop")
        self.assertEqual(getattr(self.proc, "terminated", 0), 1)
        self.assertIn("SAYBATTLE %s ended this game: resigning the AI bots." % OWNER, sent)
        self.assertTrue([x for x in sent if x.startswith("SAYPRIVATE %s Resigning the bots" % OWNER)])
        self.assertFalse([x for x in sent if "!cv stop" in x])
        again = self.pm("!stop")
        self.assertEqual(self.proc.terminated, 1)
        self.assertTrue([x for x in again if "Already resigning" in x])

    def test_an_engine_still_running_after_the_game_is_stopped(self):
        """BAR's Autoquit quits the engine 12 s after game over; if it does not
        (widget off, a hang), the client stops it a minute after the game."""
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.run_for(30)
        self.feed("CLIENTSTATUS %s 64" % HOST)              # the autohost's game ends
        self.run_for(lc.ENGINE_QUIT_AFTER - 5)
        self.assertEqual(getattr(self.proc, "terminated", 0), 0)
        self.run_for(10)
        self.assertEqual(self.proc.terminated, 1)
        self.run_for(lc.RESIGN_KILL_AFTER + 1)              # still there: killed
        self.assertEqual(getattr(self.proc, "killed", 0), 1)
        self.proc.code = -9
        self.run_for(2)
        self.assertEqual(self.c.state, "waiting")

    def test_a_new_game_stops_the_last_games_engine_first(self):
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        old = self.proc
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(10)
        self.proc = Proc()                                  # the next launch's engine
        self.feed("CLIENTSTATUS %s 65" % HOST)              # a new game before the old engine quit
        self.assertEqual(old.terminated, 1)
        self.assertEqual(len(self.launched), 2)
        self.assertIs(self.c.game_proc, self.proc)
        self.assertEqual(self.c.state, "ingame")

    def test_an_engine_that_does_not_quit_is_killed(self):
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.pm("!stop")
        self.run_for(10)
        self.assertFalse(getattr(self.proc, "killed", 0))
        self.run_for(10)
        self.assertEqual(self.proc.killed, 1)

    def test_resigned_then_back_to_the_room_after_the_game(self):
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.pm("!stop")
        self.proc.code = 0                       # the engine left
        self.run_for(3)
        self.assertEqual(self.c.state, "ingame")  # the autohost's game is still on
        self.assertEqual(len(self.launched), 1)   # never rejoined
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(3)
        self.assertEqual(self.c.state, "waiting")

    def test_engine_gone_but_game_on_asks_for_a_vote_once(self):
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.proc.code = 0
        self.run_for(3)
        sent = self.pm("!stop")
        self.assertIn("SAYBATTLE %s asks to end this game: a player can type !cv stop, then the others "
                      "!vote y." % OWNER, sent)
        self.assertFalse(getattr(self.proc, "terminated", 0))
        again = self.pm("!stop")
        self.assertTrue([x for x in again if "less than a minute ago" in x])

    def test_no_game_no_message(self):
        self.boss_and_setup()
        sent = self.pm("!stop")
        self.assertFalse([x for x in sent if x.startswith("SAYBATTLE")])
        self.assertTrue([x for x in sent if "No game is running" in x])

    def test_strangers_are_ignored(self):
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.assertEqual(self.pm("!stop", user="Troll"), [])
        self.assertFalse(getattr(self.proc, "terminated", 0))


class FriendRequests(Harness):
    """BARHOST_FRIEND_JOINERS=1: a friend request to each player joining the
    room, by account id, once per account across restarts."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_FRIEND_JOINERS="1")

    def joins(self, name, aid):
        self.feed("ADDUSER %s US %s 0" % (name, aid), "JOINEDBATTLE 12 " + name)
        return [x for x in self.run_for(5) if x.startswith("c.user.add_friend")]

    def test_each_joiner_once_and_kept(self):
        self.boss_and_setup()
        self.assertEqual(self.joins("Alice", "4242"), ["c.user.add_friend 4242"])
        self.feed("LEFTBATTLE 12 Alice")
        self.assertEqual(self.joins("Alice", "4242"), [])
        self.assertEqual(self.joins("Bob", "77"), ["c.user.add_friend 77"])
        self.assertEqual(self.client(BARHOST_FRIEND_JOINERS="1").friended, {"4242", "77"})

    def test_not_the_autohost_itself_or_unknown_ids(self):
        self.boss_and_setup()
        self.feed("ADDUSER Ghost US x 0", "JOINEDBATTLE 12 Ghost")
        self.assertFalse([x for x in self.run_for(5) if x.startswith("c.user.add_friend")])

    def test_off_by_default(self):
        self.c = self.client()
        self.boss_and_setup()
        self.assertEqual(self.joins("Alice", "4242"), [])

    def test_a_refusal_is_only_logged(self):
        self.boss_and_setup()
        self.feed("s.user.add_friend 4242	failure	existing friend request")
        self.assertEqual(self.c.state, "waiting")


class FriendUpkeep(Harness):
    """BARHOST_FRIEND_PENDING_DAYS / BARHOST_FRIEND_OFFLINE_DAYS: the friend
    list is read 60 s after login and every 6 h; requests pending too long are
    withdrawn, friends not seen online too long removed (never an authorised
    or auto-boss user), and requests pause while the server says it is full."""

    DAY = 86400

    def setUp(self):
        self.wall_t = 1_800_000_000.0
        super().setUp()
        self.c = self.client()

    def client(self, **extra):
        e = {"BARHOST_LOBBY_LOGIN": ME, "BARHOST_LOBBY_PASSWORD": "secret",
             "BAR_ENGINE_VERSION": ENGINE, "BARHOST_LOBBY_CLUSTER": "EU",
             "BARHOST_STATUS_FILE": os.path.join(self.tmp.name, "status.json"),
             "BARHOST_STATE_FILE": os.path.join(self.tmp.name, "state.json"),
             "BARHOST_FRIEND_JOINERS": "1", "BARHOST_FOLLOW": OWNER, "BARHOST_AUTO_BOSS": "Pal",
             "BARHOST_FRIEND_PENDING_DAYS": "7", "BARHOST_FRIEND_OFFLINE_DAYS": "30"}
        e.update(extra)
        return lc.LobbyClient(lc.Settings(e), now=self.clock, launch=self.launch, wall=lambda: self.wall_t)

    @staticmethod
    def lists(friends=(), outgoing=(), incoming=()):
        raw = json.dumps({"friends": list(friends), "outgoing_friend_requests": list(outgoing),
                          "incoming_friend_requests": list(incoming), "follows": [], "ignores": []})
        import base64
        return "s.user.list_relationships " + base64.b64encode(raw.encode()).decode().rstrip("=")

    @staticmethod
    def whois(aid, name):
        import base64
        return "s.user.whois %s %s" % (aid, base64.b64encode(json.dumps({"name": name}).encode()).decode())

    def sweep(self, **lists):
        self.take()
        self.feed(self.lists(**lists))
        return self.take()

    def test_listed_after_login_then_every_six_hours(self):
        self.log_in()
        sent = self.run_for(70)
        self.assertEqual(sent.count("c.user.list_relationships"), 1)
        self.assertEqual(self.run_for(lc.FRIEND_SWEEP_EVERY - 30).count("c.user.list_relationships"), 0)
        self.assertEqual(self.run_for(60).count("c.user.list_relationships"), 1)

    def test_off_by_default(self):
        self.c = self.client(BARHOST_FRIEND_PENDING_DAYS="0", BARHOST_FRIEND_OFFLINE_DAYS="0")
        self.log_in()
        self.assertNotIn("c.user.list_relationships", self.run_for(lc.FRIEND_SWEEP_EVERY + 100))

    def test_pending_requests_are_withdrawn_after_seven_days(self):
        self.log_in()
        self.feed("s.user.add_friend 11\tsuccess")         # sent now
        sent = self.sweep(outgoing=[10, 11])                 # 10: sent before ages were kept
        self.assertEqual(sent, ["c.user.rescind_friend_request 10"])
        self.wall_t += 6 * self.DAY
        self.assertEqual(self.sweep(outgoing=[11]), [])
        self.wall_t += self.DAY
        self.assertEqual(self.sweep(outgoing=[11]), ["c.user.rescind_friend_request 11"])
        self.assertNotIn("11", self.c.friend_sent)

    def test_many_are_withdrawn_in_batches(self):
        self.log_in()
        sent = self.sweep(outgoing=range(1, 121))
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0], "c.user.rescind_friend_request " + "\t".join(str(i) for i in range(1, 51)))

    def test_friends_unseen_for_thirty_days_are_removed_but_never_owners_or_bosses(self):
        self.log_in()
        for name, aid in (("Alice", "1"), (OWNER, "3"), ("Pal", "4")):
            self.feed("ADDUSER %s US %s 0" % (name, aid))
        self.assertEqual(self.sweep(friends=[1, 2, 3, 4]), [])     # tracked from now
        for name in ("Alice", OWNER, "Pal"):
            self.feed("REMOVEUSER " + name)
        self.wall_t += 29 * self.DAY
        self.assertEqual(self.sweep(friends=[1, 2, 3, 4]), [])
        self.wall_t += self.DAY
        sent = self.sweep(friends=[1, 2, 3, 4])
        self.assertIn("c.user.remove_friend 1", sent)                # Alice
        self.assertIn("c.user.whois 2", sent)                        # never seen: ask who first
        self.assertFalse([x for x in sent if x.startswith("c.user.remove_friend") and x != "c.user.remove_friend 1"])
        self.feed(self.whois("2", "pal"))                             # an auto-boss user, any case
        self.assertEqual(self.take(), [])
        self.feed("s.user.remove_friend 1\tsuccess")
        self.assertNotIn("1", self.c.friend_seen)
        # a friend seen online again restarts the count
        self.feed("ADDUSER Bob US 5 0")
        self.sweep(friends=[2, 3, 4, 5])
        self.feed("REMOVEUSER Bob")
        self.wall_t += 20 * self.DAY
        self.feed("ADDUSER Bob US 5 0", "REMOVEUSER Bob")
        self.wall_t += 15 * self.DAY
        sent = self.sweep(friends=[2, 3, 4, 5])
        self.assertNotIn("c.user.remove_friend 5", sent)
        self.assertNotIn("c.user.remove_friend 3", sent)

    def test_a_stranger_named_by_whois_is_removed(self):
        self.log_in()
        self.sweep(friends=[2])
        self.wall_t += 31 * self.DAY
        self.assertEqual(self.sweep(friends=[2]), ["c.user.whois 2"])
        self.feed(self.whois("2", "Mallory"))
        self.assertEqual(self.take(), ["c.user.remove_friend 2"])

    def test_kept_across_a_restart(self):
        self.log_in()
        self.feed("s.user.add_friend 11\tsuccess")
        self.sweep(friends=[1], outgoing=[11])
        again = self.client()
        self.assertEqual(again.friend_sent, {"11": self.wall_t})
        self.assertEqual(again.friend_seen, {"1": self.wall_t})

    def test_a_full_list_pauses_requests_until_room_is_made(self):
        self.boss_and_setup()
        self.feed("ADDUSER Alice US 4242 0", "JOINEDBATTLE 12 Alice")
        self.assertIn("c.user.add_friend 4242", self.run_for(5))
        self.feed("s.user.add_friend 4242\tfailure\t" + lc.FRIEND_FULL)
        self.assertNotIn("4242", self.c.friended)                     # asked again later
        self.feed("ADDUSER Bob US 77 0", "JOINEDBATTLE 12 Bob")
        self.assertFalse([x for x in self.run_for(5) if x.startswith("c.user.add_friend")])
        self.sweep(outgoing=[9])                                      # an old request withdrawn
        self.feed("LEFTBATTLE 12 Alice", "JOINEDBATTLE 12 Alice")
        self.assertIn("c.user.add_friend 4242", self.run_for(5))

    def test_earlier_requests_not_pending_are_forgotten_once(self):
        state = Path(self.tmp.name) / "state.json"
        state.write_text(json.dumps({"friended": ["1", "2", "3"]}))
        self.c = self.client()
        self.log_in()
        self.sweep(friends=[1], outgoing=[2])
        self.assertEqual(self.c.friended, {"1", "2"})
        self.c.friended.add("8")
        self.sweep(friends=[1])
        self.assertIn("8", self.c.friended)
        self.assertTrue(self.client().friends_reconciled)


class FakePerf:
    def __init__(self):
        self.started, self.stopped, self.last_summary = [], 0, None

    def start(self, label, run_dir=None, now=None):
        self.started.append(label)

    def stop(self):
        self.stopped += 1
        self.last_summary = {"label": self.started[-1], "seconds": 600, "cores_avg": 2.1, "cores_p95": 2.9,
                             "cores_peak_10s": 3.4, "cores_peak_1s": 4.2, "limit_cores": 16.0,
                             "throttled_pct": 0.0, "throttled_s": 0.0, "mem_peak_gib": 6.1}
        return self.last_summary

    def sample(self, now):
        pass


class Perf(Harness):
    """Each game is measured: started with the engine, summarised at its exit."""

    def setUp(self):
        super().setUp()
        self.perf = FakePerf()
        self.c = lc.LobbyClient(lc.Settings({
            "BARHOST_LOBBY_LOGIN": ME, "BARHOST_LOBBY_PASSWORD": "secret", "BAR_ENGINE_VERSION": ENGINE,
            "BARHOST_LOBBY_CLUSTER": "EU", "BARHOST_FOLLOW": OWNER,
            "BARHOST_STATUS_FILE": os.path.join(self.tmp.name, "status.json"),
            "BARHOST_STATE_FILE": os.path.join(self.tmp.name, "state.json")}),
            now=self.clock, launch=self.launch, perf=self.perf)

    def test_a_game_is_measured_and_summarised_in_the_log(self):
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.assertEqual(self.perf.started, ["game 1 All That Glitters v2.2.3"])
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        out = []
        old = lc.log
        lc.log = out.append
        try:
            self.run_for(2)
        finally:
            lc.log = old
        self.assertEqual(self.perf.stopped, 1)
        self.assertTrue([x for x in out if x.startswith("perf game 1 All That Glitters v2.2.3: 600 s; cores avg 2.10,"
                                                         " p95 2.90, peak 3.40 (10 s) 4.20 (1 s)/16; throttled 0.00%")],
                        out)

    def test_perf_command_replies_with_the_last_game(self):
        self.boss_and_setup()
        self.feed("SAIDPRIVATE %s !perf" % OWNER)
        self.assertTrue([x for x in self.run_for(3)
                         if x == "SAYPRIVATE %s No game measured yet. PM me !games for the list." % OWNER])
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(2)
        self.feed("SAIDPRIVATE %s !perf" % OWNER)
        sent = self.run_for(10)
        self.assertTrue([x for x in sent if x.startswith("SAYPRIVATE %s Game 1: All That Glitters v2.2.3" % OWNER)],
                        sent)
        self.assertTrue([x for x in sent if x.startswith("SAYPRIVATE %s Whole game: game 1 All That Glitters"
                                                         % OWNER)], sent)


class FakeMetrics:
    def __init__(self):
        self.started, self.stopped = [], 0

    def start(self, run_dir, pid, now):
        self.started.append((run_dir, pid))

    def stop(self):
        self.stopped += 1


class GameRecord(Harness):
    """Each game's lobby data and logs, written into its run dir, which is
    handed to the archive once the engine exits."""

    def setUp(self):
        super().setUp()
        self.run = Path(self.tmp.name) / "run"
        self.run.mkdir()
        self.proc.write_dir = self.run
        self.proc.proc = type("P", (), {"pid": 777})()
        self.metrics = FakeMetrics()
        self.archived = []
        self.c = lc.LobbyClient(lc.Settings({
            "BARHOST_LOBBY_LOGIN": ME, "BARHOST_LOBBY_PASSWORD": "secret", "BAR_ENGINE_VERSION": ENGINE,
            "BARHOST_LOBBY_CLUSTER": "EU", "BARHOST_FOLLOW": OWNER,
            "BARHOST_STATUS_FILE": os.path.join(self.tmp.name, "status.json"),
            "BARHOST_STATE_FILE": os.path.join(self.tmp.name, "state.json")}),
            now=self.clock, launch=self.launch, metrics=self.metrics, archive=self.archived.append)
        self.addCleanup(self.close_files)

    def close_files(self):
        """A game left running keeps its files open (module-wide for the log)."""
        for f in (lc.GAME_LOG["file"], self.c.game_events):
            if f is not None:
                f.close()
        lc.GAME_LOG["file"] = self.c.game_events = None

    def play(self):
        sent = self.boss_and_setup()
        for x in sent:
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))
        self.feed("SETSCRIPTTAGS game/modoptions/critters=2\tgame/players/alice/skill=[21.5]",
                  "JOINEDBATTLE 12 Alice", "ADDUSER Alice US 1234",
                  "CLIENTBATTLESTATUS Alice %d 0" % lc.battle_status(ready=1, mode=1, sync=1, team=3))
        self.feed("CLIENTSTATUS %s 65" % HOST)

    def end(self):
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(2)

    def test_the_room_is_recorded_at_the_start(self):
        self.play()
        g = json.loads((self.run / "game.json").read_text())
        self.assertEqual((g["battle"], g["founder"], g["map"]), ("12", HOST, "All That Glitters v2.2.3"))
        self.assertEqual(g["scriptTags"], {"game/modoptions/critters": "2", "game/players/alice/skill": "[21.5]"})
        alice = [p for p in g["players"] if p["name"] == "Alice"][0]
        self.assertEqual((alice["player"], alice["team"], alice["ready"], alice["accountId"]), (True, 3, 1, "1234"))
        self.assertEqual(len(g["bots"]), 8)
        self.assertEqual({b["ai"] for b in g["bots"]}, {"BARb"})
        self.assertTrue(g["startedUtc"].endswith("Z"))
        self.assertEqual(self.metrics.started, [(self.run, 777)])

    def test_room_traffic_and_own_lines_are_kept_during_the_game(self):
        self.play()
        self.feed("SAIDBATTLE Alice gl hf", "SAIDBATTLE 13 elsewhere", "JOINEDBATTLE 99 Bob",
                  "SAIDPRIVATE %s !status" % OWNER)
        self.run_for(2)
        self.end()
        events = (self.run / "lobby-events.log").read_text()
        self.assertIn(" SAIDBATTLE Alice gl hf", events)
        self.assertNotIn("JOINEDBATTLE 99", events)                 # another room
        self.assertNotIn("SAIDPRIVATE", events)                     # private messages are not room traffic
        client = (self.run / "client.log").read_text()
        self.assertIn("engine exited with code 0", client)
        self.feed("SAIDBATTLE Alice after")                         # the game is over: no longer kept
        self.assertNotIn("after", (self.run / "lobby-events.log").read_text())

    def test_the_end_is_recorded_and_archived_once(self):
        self.play()
        self.end()
        ended = json.loads((self.run / "ended.json").read_text())
        self.assertEqual((ended["engineExit"], ended["resigned"]), (0, False))
        self.assertEqual(self.archived, [self.run])
        self.assertEqual(self.metrics.stopped, 1)
        self.run_for(30)
        self.assertEqual(self.archived, [self.run])


class UsableCpus(unittest.TestCase):
    """The CPUs the client may run on, as ranges (dedicated cores show here)."""

    def cpus(self, ids):
        from unittest import mock
        with mock.patch.object(lc.os, "sched_getaffinity", create=True, return_value=set(ids)):
            return lc.usable_cpus()

    def test_ranges(self):
        self.assertEqual(self.cpus([2, 3, 4, 5, 42, 43, 44, 45]), "2-5,42-45")
        self.assertEqual(self.cpus([7]), "7")
        self.assertEqual(self.cpus([1, 3, 4]), "1,3-4")


class WriteDir(unittest.TestCase):
    """The engine's write dir before a game: the metrics widget with the
    client's settings, and the cache shared between games."""

    def test_widget_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = lc.Settings({"BARHOST_LOBBY_LOGIN": ME, "BARHOST_LOBBY_PASSWORD": "x",
                             "BARHOST_WORK_DIR": os.path.join(tmp, "work"), "BARHOST_METRICS_EVERY": "10",
                             "BARHOST_ENGINE_PROFILER": "1"})
            d = Path(tmp) / "work" / "run1"
            d.mkdir(parents=True)
            lc.prepare_write_dir(s, d)
            widget = (d / "LuaUI" / "Widgets" / "rjm_host_metrics.lua").read_text()
            self.assertIn("\nlocal EVERY = 10 ", widget)
            self.assertIn("\nlocal RJM_PROFILER = true ", widget)
            # BAR's sandbox: user widgets write only under LuaUI/Config/ (game_archive places them).
            self.assertIn('\nlocal DIR = "LuaUI/Config/"', widget)
            for path in ("rjm-engine-timeline.csv", "rjm-engine-events.csv", "rjm-engine-profile.csv"):
                self.assertIn('DIR .. "%s"' % path, widget)
                self.assertIn("LuaUI/Config/" + path, lc.game_archive.PLACED)
            if (d / "cache").is_symlink():                          # needs symlink rights on Windows
                self.assertEqual((d / "cache").resolve(), (Path(tmp) / "work" / ".engine-cache").resolve())

    # The shape BAR's table.save writes (luaui/barwidgets.lua SaveConfigData).
    BYAR = ('-- Widget Custom data and order, order = 0 disabled widget\nreturn {\n\tallowUserWidgets = true,\n'
            '\tdata = {\n\t\t["Widget Selector"] = {\n\t\t\tshow = true,\n\t\t},\n\t},\n'
            '\torder = {\n\t\tAdvPlayersList = 12,\n\t\tAutoquit = 69,\n\t\t["Say \\"hi\\""] = 0,\n'
            '\t\t["Widget Selector"] = 24,\n\t\t["rjm host metrics"] = 23,\n\t},\n}\n')

    def settings(self, tmp, **extra):
        e = {"BARHOST_LOBBY_LOGIN": ME, "BARHOST_LOBBY_PASSWORD": "x", "BARHOST_WORK_DIR": tmp,
             "BARHOST_WIDGET_NAMES_FILE": os.path.join(tmp, "names.json")}
        e.update(extra)
        return lc.Settings(e)

    def test_bar_widgets_off_keeps_only_the_metrics_widget(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.settings(tmp)                              # the default: off
            ended = Path(tmp) / "run0"
            (ended / "LuaUI" / "Config").mkdir(parents=True)
            (ended / "LuaUI" / "Config" / "BYAR.lua").write_text(self.BYAR)
            lc.learn_widget_names(s, ended)                     # after a game
            self.assertEqual(json.loads(Path(tmp, "names.json").read_text()),
                             ['"AdvPlayersList"', '"Autoquit"', '"Say \\"hi\\""', '"Widget Selector"',
                              '"rjm host metrics"'])
            d = Path(tmp) / "run1"
            d.mkdir()
            lc.prepare_write_dir(s, d)
            cfg = (d / "LuaUI" / "Config" / "BYAR.lua").read_text()
            self.assertIn('\t\t["AdvPlayersList"] = 0,\n', cfg)
            self.assertIn('\t\t["Say \\"hi\\""] = 0,\n', cfg)
            self.assertIn('\t\t["rjm host metrics"] = 1,\n', cfg)
            self.assertNotIn('["rjm host metrics"] = 0', cfg)
            self.assertIn('\t\t["Autoquit"] = 2,\n', cfg)      # quits the engine after game over
            self.assertNotIn('["Autoquit"] = 0', cfg)
            self.assertIn("allowUserWidgets = true", cfg)
            self.assertEqual(sorted(lc.widget_names_in(cfg)), sorted(lc.widget_names_in(self.BYAR)))

    def test_names_from_the_archive_when_none_learnt_yet(self):
        import gzip
        with tempfile.TemporaryDirectory() as tmp:
            s = self.settings(tmp, BARHOST_BAR_WIDGETS="0", BARHOST_ARCHIVE_DIR=os.path.join(tmp, "archive"))
            cfg = Path(tmp, "archive", "2026", "10", "26", "g1", "files", "LuaUI", "Config")
            cfg.mkdir(parents=True)
            (cfg / "BYAR.lua.gz").write_bytes(gzip.compress(self.BYAR.encode()))
            self.assertIn('"AdvPlayersList"', lc.known_widget_names(s))

    def test_bar_widgets_on_or_no_names_writes_no_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("1", "0"):                            # on; off with nothing learnt
                d = Path(tmp) / ("run" + value)
                d.mkdir()
                lc.prepare_write_dir(self.settings(tmp, BARHOST_BAR_WIDGETS=value), d)
                self.assertFalse((d / "LuaUI" / "Config" / "BYAR.lua").exists())

    def test_metrics_off_installs_no_widget(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = lc.Settings({"BARHOST_LOBBY_LOGIN": ME, "BARHOST_LOBBY_PASSWORD": "x",
                             "BARHOST_WORK_DIR": tmp, "BARHOST_METRICS_EVERY": "0"})
            d = Path(tmp) / "run1"
            d.mkdir()
            lc.prepare_write_dir(s, d)
            self.assertFalse((d / "LuaUI").exists())


class GamesAndPerf(Harness):
    """!games: recent games newest first (the one running, ended ones, the
    archive's); !perf [n]: one game's lag, CPU, memory; !help."""

    def setUp(self):
        super().setUp()
        base = Path(self.tmp.name)
        self.archive = base / "archive"
        self.work = base / "work"
        self.run = self.work / "run-now"
        self.run.mkdir(parents=True)
        self.proc.write_dir = self.run
        self.proc.proc = type("P", (), {"pid": 777})()
        self.c = lc.LobbyClient(lc.Settings({
            "BARHOST_LOBBY_LOGIN": ME, "BARHOST_LOBBY_PASSWORD": "secret", "BAR_ENGINE_VERSION": ENGINE,
            "BARHOST_LOBBY_CLUSTER": "EU", "BARHOST_FOLLOW": OWNER, "BARHOST_ARCHIVE_DIR": str(self.archive),
            "BARHOST_ARCHIVE_SOURCE": "bar-lobbies.clyde", "BARHOST_WORK_DIR": str(self.work),
            "BARHOST_STATUS_FILE": os.path.join(self.tmp.name, "status.json"),
            "BARHOST_STATE_FILE": os.path.join(self.tmp.name, "state.json")}),
            now=self.clock, launch=self.launch)
        self.addCleanup(self.close_files)
        for started, folder, mapname, lag, cores in (
                ("2026-10-22T20:00:00.000000Z", "2026/10/22/a", "Cells v1", 2.0, 3.1),
                ("2026-10-23T18:00:00.000000Z", "2026/10/23/b", "Bismuth Valley v2.4.1", 11.4, 8.4)):
            self.archived(started, folder, mapname, lag, cores)

    def close_files(self):
        for f in (lc.GAME_LOG["file"], self.c.game_events):
            if f is not None:
                f.close()
        lc.GAME_LOG["file"] = self.c.game_events = None

    def archived(self, started, folder, mapname, lag, cores):
        d = self.archive / folder
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({
            "startedUtc": started, "map": mapname, "outcome": "winner", "winningAllyTeams": [0],
            "gameSeconds": 3720, "humans": ["Alice", "Bob"], "bots": 8,
            "ai": {"name": "SMRTBARb", "release": "v1.1153-prod"},
            "lag": {"behind_s": {"max": lag, "p95": 6.2, "avg": 1.3, "min": 0.0, "now": 0.1},
                    "sim_fps": {"max": 60, "p95": 45, "avg": 29, "min": 12, "now": 30}},
            "load": {"cores": {"max": 8.6, "p95": cores, "avg": 4.7, "min": 1.0, "now": 2.0},
                     "main_thread_cores": {"max": 1.0, "p95": 0.98, "avg": 0.7, "min": 0.1, "now": 0.5},
                     "io_pressure_pct": {"max": 0.2, "p95": 0.0, "avg": 0.0, "min": 0.0, "now": 0.0}},
            "perf": {"label": "game 3 %s" % mapname, "seconds": 3727, "cores_avg": 4.68, "cores_p95": cores,
                     "cores_peak_10s": 8.59, "cores_peak_1s": 16.0, "limit_cores": 16, "throttled_pct": 0.04,
                     "throttled_s": 1.5, "mem_peak_gib": 7.75}}))
        idx = self.archive / "_index" / "bar-lobbies.clyde.jsonl"
        idx.parent.mkdir(parents=True, exist_ok=True)
        with idx.open("a") as f:
            f.write(json.dumps({"folder": folder, "run": folder.replace("/", "-"), "startedUtc": started,
                                "map": mapname, "outcome": "winner", "gameSeconds": 3720, "humans": 2,
                                "lagMaxS": lag, "coresP95": cores}) + "\n")

    def pm(self, text, wait=40):
        self.feed("SAIDPRIVATE %s %s" % (OWNER, text))
        return [x[len("SAYPRIVATE %s " % OWNER):] for x in self.run_for(wait)
                if x.startswith("SAYPRIVATE %s " % OWNER)]

    def live(self):
        self.boss_and_setup()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        (self.run / "engine-timeline.csv").write_text(
            "unix_s,behind_s,sim_fps,ping_s,units\n1,0.5,30,0.2,800\n2,9.0,14,4.0,2100\n3,2.3,40,1.0,2200\n")
        (self.run / "timeline.csv").write_text(
            "unix_s,cores,main_thread_cores,main_preempt_per_s,mem_gib,throttled_pct\n"
            "1,3.0,0.90,10,5.0,0\n2,6.0,1.00,200,6.5,0.5\n")
        self.clock.t += 600

    def test_games_newest_first_with_the_one_running(self):
        self.live()
        lines = self.pm("!games")
        self.assertTrue(lines[0].startswith("0. NOW "), lines)
        self.assertIn("All That Glitters v2.2.3, 10 min in; behind 2.3 s now, max 9.0 s", lines[0])
        self.assertEqual(lines[1], "1. 10-23 18:00Z Bismuth Valley v2.4.1, 62 min, winner, 2 humans; "
                                   "lag max 11.4 s; cores p95 8.4")
        self.assertTrue(lines[2].startswith("2. 10-22 20:00Z Cells v1"))
        self.assertEqual(lines[-1], "Details: !perf <number>.")

    def test_games_count(self):
        self.assertEqual(len(self.pm("!games 1")), 2)               # one game and the hint

    def test_perf_now(self):
        self.live()
        lines = self.pm("!perf")
        self.assertTrue(lines[0].startswith("Game 0 (now): All That Glitters v2.2.3, started "), lines)
        self.assertEqual(lines[1], "Behind the server: now 2.3, max 9.0, p95 9.0, avg 3.9 s; sim speed now 40, "
                                   "min 14, avg 28 frames/s (30 = real time); ping max 4.0 s.")
        self.assertIn("main (sim) thread now 1.00, max 1.00", lines[2])
        self.assertIn("pushed off its CPU p95 200/s", lines[2])
        self.assertIn("Memory max 6.5 GiB", lines[-1])
        self.assertIn("units max 2200", lines[-1])

    def test_perf_of_an_archived_game(self):
        lines = self.pm("!perf 1")
        self.assertEqual(lines[0], "Game 1: 2026-10-23 18:00Z, Bismuth Valley v2.4.1, 62 min, winner (ally 0), "
                                   "2 humans, 8 bots, SMRTBARb v1.1153-prod.")
        self.assertTrue(lines[1].startswith("Behind the server: max 11.4, p95 6.2, avg 1.3 s; sim speed min 12"))
        self.assertTrue([x for x in lines if x.startswith("Whole game: game 3 Bismuth Valley v2.4.1: 3727 s")])
        self.assertEqual(lines[-1], "Files: 2026/10/23/b")

    def test_a_game_ended_but_not_yet_archived(self):
        self.live()
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(2)
        lines = self.pm("!games")
        self.assertTrue(lines[0].startswith("1. ") and "All That Glitters" in lines[0] and "being archived" in lines[0],
                        lines)
        report = self.pm("!perf 1")
        self.assertIn("being archived", report[0])
        self.assertTrue(report[1].startswith("Behind the server: max 9.0"))

    def test_unknown_numbers_and_words(self):
        self.assertEqual(self.pm("!perf 9"), ["No game 9. PM me !games for the list."])
        self.assertEqual(self.pm("!perf last"),
                         ["PM me !perf for the game now (or the last one), or !perf <number> from !games."])

    def test_profile_lines(self):
        p = {"simS": 250.6, "aiS": 133.3, "aiPctOfSim": 53.2, "drawS": 282.2, "luaCallinsS": 114.0,
             "profilerS": 175.1, "aiByTeamS": {"2": 27.4, "3": 30.8}}
        self.assertEqual(lc.profile_lines(p), [
            "Engine time over the game: sim 251 s, of it the AIs 133 s (53%), draw 282 s, Lua call-ins 114 s, "
            "the profiler itself 175 s.", "AI time by team: 2 27 s, 3 31 s."])
        self.assertEqual(lc.profile_lines({}), [])

    def test_no_lag_data_is_said(self):
        self.assertEqual(lc.perf_lines({}, {}, None), ["No lag data for this game (the engine's metrics widget "
                                                       "wrote none)."])

    def test_help(self):
        lines = self.pm("!help")
        self.assertEqual(len(lines), 5)
        text = " ".join(lines)
        for cmd in ("!status", "!games", "!perf", "!bots", "!public", "!private", "!stop", "!help",
                    "!profiler", "!say"):
            self.assertIn(cmd, text)
        self.assertTrue(all(len(x) < 300 for x in lines))

    def test_strangers_get_nothing(self):
        self.feed("SAIDPRIVATE Troll !games", "SAIDPRIVATE Troll !help")
        self.assertFalse([x for x in self.run_for(10) if x.startswith("SAYPRIVATE Troll")])


class EngineExit(Harness):
    """The engine's exit is reported in the client's (the pod's) log, with its
    console's tail when the exit looks wrong."""

    def started(self):
        self.boss_and_setup()
        self.proc.write_dir = __import__("pathlib").Path(self.tmp.name)
        (self.proc.write_dir / "engine-console.log").write_text("line a\nError: no map\n")
        self.feed("CLIENTSTATUS %s 65" % HOST)

    def logged(self, fn):
        out = []
        old = lc.log
        lc.log = out.append
        try:
            fn()
        finally:
            lc.log = old
        return out

    def test_a_clean_exit_after_the_game_is_one_line(self):
        self.started()
        self.clock.t += 600
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        out = self.logged(lambda: self.run_for(2))
        self.assertTrue([x for x in out if x.startswith("engine exited with code 0 after 60") and x.endswith(" s")], out)
        self.assertFalse([x for x in out if "engine: " in x])

    def test_a_crash_mid_game_brings_the_console_tail(self):
        self.started()
        self.proc.code = 1                                    # the game goes on
        out = self.logged(lambda: self.run_for(2))
        self.assertTrue([x for x in out if x.startswith("engine exited with code 1") and "goes on" in x], out)
        self.assertIn("  engine: Error: no map", out)
        self.assertEqual(len([x for x in self.logged(lambda: self.run_for(5)) if "engine exited" in x]), 0)


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


class FollowHarness(Harness):
    """follow mode: the owner hosts; the client brings the bots."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_CLIENT_MODE="follow", BARHOST_FOLLOW=OWNER, BARHOST_LOBBY_CLUSTER=".*")

    def owner_in_room(self, bid="12", passworded=0, engine=ENGINE):
        self.feed("TASSERVER 0.38 * 8201 0", "ACCEPTED " + ME, "ADDUSER %s XX 0 1" % HOST,
                  "ADDUSER %s XX 0 2" % OWNER, opened(bid, passworded=passworded, engine=engine),
                  "JOINEDBATTLE %s %s" % (bid, OWNER),
                  "CLIENTBATTLESTATUS %s %d 0" % (OWNER, lc.battle_status(team=0, ally=0, mode=1, sync=1)))
        self.take()

    def joined(self):
        self.owner_in_room()
        sent = self.run_for(1)
        join = [x for x in sent if x.startswith("JOINBATTLE")]
        self.assertEqual(len(join), 1, sent)
        self.feed("JOINBATTLE 12 hash", "JOINEDBATTLE 12 %s %s" % (ME, join[0].split(" ")[3]))
        sent = self.run_for(30)
        for x in sent:      # the server echoes the bots
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))
        return sent

    def pm(self, text, user=OWNER):
        self.feed("SAIDPRIVATE %s %s" % (user, text))
        return self.run_for(30)      # commands go out 1.5 s apart


class Follow(FollowHarness):
    """A server that reports every room's joins (the owner is seen)."""

    def test_joins_the_owners_room_and_brings_8_bots_as_a_spectator(self):
        sent = self.joined()
        bots = [lc.unpack_battle_status(x.split(" ")[2]) for x in sent if x.startswith("ADDBOT")]
        self.assertEqual(len(bots), 8)
        self.assertNotIn(0, {b["team"] for b in bots})            # the owner's team
        self.assertEqual({b["ally"] for b in bots}, {1})
        mine = [lc.unpack_battle_status(x.split(" ")[1]) for x in sent if x.startswith("MYBATTLESTATUS")]
        self.assertEqual(mine[0]["mode"], 0)
        self.assertFalse([x for x in sent if "!boss" in x or "!preset" in x or "!cv" in x], sent)

    def test_never_starts_the_game_itself(self):
        self.joined()
        self.c.fetched(self.c.battle, True)
        self.feed("CLIENTBATTLESTATUS %s %d 0" % (OWNER, lc.battle_status(ready=1, team=0, mode=1, sync=1)))
        self.assertFalse([x for x in self.run_for(120) if "!cv start" in x])

    def test_bots_command_changes_the_count(self):
        self.joined()
        sent = self.pm("!bots 3")
        self.assertEqual(len([x for x in sent if x.startswith("REMOVEBOT")]), 5)
        self.assertTrue([x for x in sent if x.startswith("SAYPRIVATE %s OK: 3 BARb" % OWNER)], sent)
        self.assertEqual(self.c.want_bots, 3)

    def test_bots_command_can_switch_ai(self):
        self.joined()
        sent = self.pm("!bots 2 SMRTBARb")
        self.assertEqual(len([x for x in sent if x.startswith("REMOVEBOT")]), 8)
        self.assertEqual([x.split(" ")[4] for x in sent if x.startswith("ADDBOT")], ["SMRTBARb", "SMRTBARb"])

    def test_ignores_strangers(self):
        self.joined()
        self.assertEqual(self.pm("!bots 0", user="Stranger"), [])
        self.assertEqual(self.c.want_bots, 8)

    def test_leaves_with_its_bots_when_the_owner_leaves(self):
        self.joined()
        self.feed("LEFTBATTLE 12 " + OWNER)
        sent = self.run_for(20)
        self.assertEqual(len([x for x in sent if x.startswith("REMOVEBOT")]), 8)
        self.assertIn("LEAVEBATTLE", sent)
        self.assertIsNone(self.c.battle)

    def test_follows_the_owner_to_a_new_room(self):
        self.joined()
        self.feed("LEFTBATTLE 12 " + OWNER, opened("13"), "JOINEDBATTLE 13 " + OWNER)
        sent = self.run_for(30)
        self.assertIn("13", [x.split(" ")[1] for x in sent if x.startswith("JOINBATTLE")])

    def test_passworded_room_needs_the_password(self):
        self.owner_in_room(passworded=1)
        sent = self.run_for(10)
        self.assertFalse([x for x in sent if x.startswith("JOINBATTLE")])
        self.assertEqual(len([x for x in sent if x.startswith("SAYPRIVATE %s Your room has a password" % OWNER)]), 1)
        sent = self.pm("!join hunter2")
        self.assertEqual([x.split(" ")[:3] for x in sent if x.startswith("JOINBATTLE")], [["JOINBATTLE", "12", "hunter2"]])

    def test_explains_an_engine_mismatch(self):
        self.owner_in_room(engine="2025.06.12")
        sent = self.run_for(10)
        self.assertFalse([x for x in sent if x.startswith("JOINBATTLE")])
        self.assertTrue([x for x in sent if "engine 2025.06.12" in x])

    def test_leave_command_stays_away_until_join(self):
        self.joined()
        sent = self.pm("!leave")
        self.assertIn("LEAVEBATTLE", sent)
        self.assertFalse([x for x in self.run_for(30) if x.startswith("JOINBATTLE")])
        self.assertTrue([x for x in self.pm("!join") if x.startswith("JOINBATTLE")])

    def test_runs_the_bots_when_the_game_starts(self):
        self.joined()
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.c.state, "ingame")


class FollowUnseen(FollowHarness):
    """BAR's real server: this client is not a lobby Teiserver lists, so it is
    never told who joins other rooms (protocol_optimisation :full). It sees the
    rooms, not that the owner is in one; the owner names the room with !room."""

    def owner_in_unseen_room(self, passworded=0):
        self.feed("TASSERVER 0.38 * 8201 0", "ACCEPTED " + ME, "ADDUSER %s XX 0 1" % HOST,
                  "ADDUSER %s XX 0 2" % OWNER, opened("12", passworded=passworded), "LOGININFOEND")
        self.take()

    def enter(self, sent, members=(HOST, OWNER)):
        """Answer a JOINBATTLE as Teiserver does: who is in the room comes only
        as CLIENTBATTLESTATUS, then JOINEDBATTLE for this client alone."""
        join = [x for x in sent if x.startswith("JOINBATTLE")]
        self.assertEqual(len(join), 1, sent)
        self.feed("JOINBATTLE 12 hash")
        self.c.tick()
        for m in members:
            self.feed("CLIENTBATTLESTATUS %s %d 0" % (m, lc.battle_status(team=0, ally=0, mode=1, sync=1)))
        self.feed("JOINEDBATTLE 12 %s %s" % (ME, join[0].split(" ")[3]))
        return self.run_for(30)

    def test_waits_unseen_and_status_says_why(self):
        self.owner_in_unseen_room()
        self.assertFalse([x for x in self.run_for(30) if x.startswith("JOINBATTLE")])
        status = [x for x in self.pm("!status") if x.startswith("SAYPRIVATE")]
        self.assertIn("1 rooms seen", status[0])
        self.assertIn("!room", status[0])
        self.assertTrue([x for x in self.pm("!join") if "cannot see you in a room" in x])

    def test_room_by_host_name_joins_and_stays_with_the_owner(self):
        self.owner_in_unseen_room()
        sent = self.pm("!room host[eu1][3]")
        self.assertTrue([x for x in sent if x.startswith("SAYPRIVATE %s Joining room 12" % OWNER)], sent)
        sent = self.enter(sent)
        self.assertEqual(len([x for x in sent if x.startswith("ADDBOT")]), 8)
        self.assertNotIn("LEAVEBATTLE", sent + self.run_for(60))
        self.assertIsNotNone(self.c.battle)

    def test_room_by_number_with_password(self):
        self.owner_in_unseen_room(passworded=1)
        sent = self.pm("!room 12 hunter2")
        self.assertEqual([x.split(" ")[:3] for x in sent if x.startswith("JOINBATTLE")],
                         [["JOINBATTLE", "12", "hunter2"]])

    def test_named_room_without_the_owner_is_left_and_not_rejoined(self):
        self.owner_in_unseen_room()
        sent = self.enter(self.pm("!room 12"), members=(HOST,))
        self.assertIn("LEAVEBATTLE", sent)
        self.assertFalse([x for x in self.run_for(60) if x.startswith("JOINBATTLE")])

    def test_owner_leaving_the_named_room_is_seen(self):
        self.owner_in_unseen_room()
        self.enter(self.pm("!room 12"))
        self.feed("LEFTBATTLE 12 " + OWNER)
        self.assertIn("LEAVEBATTLE", self.run_for(20))
        self.assertFalse([x for x in self.run_for(60) if x.startswith("JOINBATTLE")])

    def test_unknown_room_is_explained(self):
        self.owner_in_unseen_room()
        sent = self.pm("!room Host[XX9][9]")
        self.assertFalse([x for x in sent if x.startswith("JOINBATTLE")])
        self.assertTrue([x for x in sent if "I see no such room among 1" in x], sent)

    def test_join_failure_is_reported_and_not_retried(self):
        self.owner_in_unseen_room()
        self.pm("!room 12")
        self.feed("JOINBATTLEFAILED Room is locked")
        sent = self.run_for(30)
        self.assertTrue([x for x in sent if "could not join room 12: Room is locked" in x], sent)
        self.assertFalse([x for x in sent if x.startswith("JOINBATTLE ")])

    def test_adds_bots_as_a_player_then_spectates_again(self):
        # Teiserver drops ADDBOT from a spectator; a bot stays when its owner spectates.
        self.owner_in_unseen_room()
        sent = self.enter(self.pm("!room 12"))
        adds = [i for i, x in enumerate(sent) if x.startswith("ADDBOT")]
        self.assertEqual(len(adds), 8)
        mine = [(i, lc.unpack_battle_status(x.split(" ")[1])["mode"]) for i, x in enumerate(sent)
                if x.startswith("MYBATTLESTATUS")]
        self.assertEqual([m for i, m in mine if i < adds[0]][-1], 1)
        self.assertEqual([m for i, m in mine if i > adds[-1]][0], 0)
        self.assertEqual(lc.unpack_battle_status(self.c.my_status)["mode"], 0)

    def test_bots_take_teams_the_members_do_not_use(self):
        self.owner_in_unseen_room()
        sent = self.pm("!room 12")
        join = [x for x in sent if x.startswith("JOINBATTLE")][0]
        self.feed("JOINBATTLE 12 hash")
        self.c.tick()
        self.feed("CLIENTBATTLESTATUS %s %d 0" % (OWNER, lc.battle_status(team=1, ally=0, mode=1, sync=1)),
                  "JOINEDBATTLE 12 %s %s" % (ME, join.split(" ")[3]))
        teams = [lc.unpack_battle_status(x.split(" ")[2])["team"] for x in self.run_for(30) if x.startswith("ADDBOT")]
        self.assertEqual(len(teams), 8)
        self.assertNotIn(1, teams)

    def test_more_bots_later_also_turn_player(self):
        self.owner_in_unseen_room()
        for x in self.enter(self.pm("!room 12")):
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))
        sent = self.pm("!bots 10")
        statuses = [lc.unpack_battle_status(x.split(" ")[1])["mode"] for x in sent if x.startswith("MYBATTLESTATUS")]
        self.assertEqual(len([x for x in sent if x.startswith("ADDBOT")]), 2)
        self.assertEqual(statuses, [1, 0])


class RoomByName(FollowHarness):
    """!room <room number, host, player or title> [password]."""

    def rooms(self):
        self.feed("TASSERVER 0.38 * 8201 0", "ACCEPTED " + ME, "ADDUSER %s XX 0 2" % OWNER,
                  "ADDUSER Friend XX 0 3",
                  opened("12", title="Centrifugal's co-op"), opened("13", founder="SomePlayer", title="1v1 me"),
                  opened("14", founder="Host[US4][001]", title="Team FFA", passworded=1),
                  opened("15", founder="Host[US4][002]", title="Team chaos"), "LOGININFOEND")
        self.take()

    def joined_room(self, text):
        return [x.split(" ")[1:3] for x in self.pm(text) if x.startswith("JOINBATTLE")]

    def test_by_player_hosting_it(self):
        self.rooms()
        self.assertEqual(self.joined_room("!room someplayer"), [["13", "empty"]])

    def test_by_whole_title_any_case(self):
        self.rooms()
        self.assertEqual(self.joined_room("!room centrifugal's CO-OP"), [["12", "empty"]])

    def test_by_part_of_a_title_with_password(self):
        self.rooms()
        self.assertEqual(self.joined_room("!room ffa hunter2"), [["14", "hunter2"]])

    def test_by_host_with_password(self):
        self.rooms()
        self.assertEqual(self.joined_room("!room Host[US4][001] hunter2"), [["14", "hunter2"]])

    def test_by_a_player_seen_in_the_room(self):
        self.rooms()
        self.feed("JOINEDBATTLE 15 Friend")
        self.assertEqual(self.joined_room("!room friend"), [["15", "empty"]])

    def test_ambiguous_title_is_explained(self):
        self.rooms()
        sent = self.pm("!room team")
        self.assertFalse([x for x in sent if x.startswith("JOINBATTLE")])
        self.assertTrue([x for x in sent if '2 room titles contain "team"' in x], sent)

    def test_online_player_in_an_unseen_room_is_explained(self):
        self.rooms()
        sent = self.pm("!room Friend")
        self.assertFalse([x for x in sent if x.startswith("JOINBATTLE")])
        self.assertTrue([x for x in sent if "Friend is online, but" in x], sent)


if __name__ == "__main__":
    unittest.main()
