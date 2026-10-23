"""lobby_client.py: the protocol state machine, driven with lobby lines and a fake clock."""

import json
import os
import sys
import tempfile
import unittest

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

    def test_not_moved_during_a_countdown_either(self):
        self.boss_and_setup()
        self.feed("JOINEDBATTLE 12 Alice",
                  "CLIENTBATTLESTATUS Alice %d 0" % lc.battle_status(ready=1, mode=1, sync=1))
        self.run_for(5)
        self.assertEqual(self.c.state, "countdown")
        sent = self.pm("!private")
        self.assertTrue([x for x in sent if "after this game" in x], sent)
        self.assertNotIn("LEAVEBATTLE", sent)

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
        self.assertFalse([x for x in self.run_for(5) if "starting in" in x])    # map not switched yet
        self.map_is("All That Simmers v1.2")
        sent = self.run_for(5)
        self.assertEqual(lc.unpack_battle_status(
            [x for x in sent if x.startswith("MYBATTLESTATUS")][0].split(" ")[1])["sync"], 2)
        self.assertFalse([x for x in sent if "starting in" in x])               # downloading
        self.c.fetched(self.c.battle, True)
        self.assertTrue([x for x in self.run_for(5) if "starting in" in x])

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

    def test_leaves_other_votes_alone(self):
        self.boss_and_setup()
        self.assertFalse(self.ended("unboss SomeTroll"))
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
    """!barb / !smrt: the bots' AI, remembered; switched as a player."""

    def setUp(self):
        super().setUp()
        self.c = self.client(BARHOST_FOLLOW=OWNER)

    def pm(self, text):
        self.feed("SAIDPRIVATE %s %s" % (OWNER, text))
        return self.run_for(30)

    def echo_bots(self, sent):
        for x in sent:
            if x.startswith("ADDBOT "):
                w = x.split(" ")
                self.feed("ADDBOT 12 %s %s %s 0 %s" % (w[1], ME, w[2], w[4]))

    def test_switching_replaces_the_bots_as_a_player(self):
        self.echo_bots(self.boss_and_setup())
        self.c.set_seat(False, "test")                       # a spectator, as with 8 humans
        self.run_for(2)
        sent = self.pm("!smrt")
        self.assertTrue([x for x in sent if "switching the bots to SMRTBARb" in x], sent)
        self.assertEqual(len([x for x in sent if x.startswith("REMOVEBOT")]), 8)
        adds = [i for i, x in enumerate(sent) if x.startswith("ADDBOT")]
        self.assertEqual({sent[i].split(" ")[4] for i in adds}, {"SMRTBARb"})
        self.assertEqual(len(adds), 8)
        self.assertEqual(my_modes(sent)[0], 1)                # a player first
        mine = [i for i, x in enumerate(sent) if x.startswith("MYBATTLESTATUS")]
        self.assertLess(mine[0], adds[0])

    def test_the_choice_is_remembered(self):
        self.boss_and_setup()
        self.pm("!smrt")
        self.assertEqual(self.client(BARHOST_FOLLOW=OWNER).bot_ai, "SMRTBARb")
        self.pm("!barb")
        self.assertEqual(self.client(BARHOST_FOLLOW=OWNER, BARHOST_BOT_AI="SMRTBARb").bot_ai, "BARb")

    def test_same_ai_is_said_not_redone(self):
        self.boss_and_setup()
        sent = self.pm("!barb")
        self.assertTrue([x for x in sent if "already run BARb" in x])
        self.assertFalse([x for x in sent if x.startswith("REMOVEBOT")])

    def test_during_a_game_it_waits_for_the_game_to_end(self):
        self.echo_bots(self.boss_and_setup())
        self.feed("CLIENTSTATUS %s 65" % HOST)
        sent = self.pm("!smrt")
        self.assertTrue([x for x in sent if "after this game" in x], sent)
        self.assertFalse([x for x in sent if x.startswith("REMOVEBOT") or x.startswith("ADDBOT")])
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        sent = self.run_for(30)
        self.assertEqual({x.split(" ")[4] for x in sent if x.startswith("ADDBOT")}, {"SMRTBARb"})

    def test_strangers_cannot_switch(self):
        self.boss_and_setup()
        self.feed("SAIDPRIVATE Troll !smrt")
        self.run_for(5)
        self.assertEqual(self.c.bot_ai, "BARb")


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
        self.assertFalse([x for x in self.run_for(70) if "starting in" in x])   # updating
        self.c.ai_updated(["github:o/r@latest: v1.958-prod -> v1.960-prod"], [])
        sent = self.run_for(5)
        self.assertTrue([x for x in sent if x.startswith("SAYPRIVATE %s AI release updated (v1.958-prod -> "
                                                         "v1.960-prod)" % OWNER)], sent)
        self.assertTrue([x for x in sent if "starting in" in x])

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
        self.assertTrue([x for x in self.run_for(3) if x == "SAYPRIVATE %s No game measured yet." % OWNER])
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        self.run_for(2)
        self.feed("SAIDPRIVATE %s !perf" % OWNER)
        self.assertTrue([x for x in self.run_for(3) if x.startswith("SAYPRIVATE %s Last game 1" % OWNER)])


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
