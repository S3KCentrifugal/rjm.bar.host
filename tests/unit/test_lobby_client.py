"""lobby_client.py: the protocol state machine, driven with lobby lines and a fake clock."""

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
    def test_boss_then_options_and_bots_playing_in_a_public_room(self):
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
        # A public room with fewer than 3 humans playing: Clyde stays a player.
        last = [lc.unpack_battle_status(x.split(" ")[1]) for x in sent if x.startswith("MYBATTLESTATUS")][-1]
        self.assertEqual((last["mode"], last["sync"]), (1, 1))

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


MANAGER = "Host[EU1]"
PRIVATE_HOST = "Host[EU1][7]"


def my_modes(sent):
    """The seat (1 player, 0 spectator) of each MYBATTLESTATUS sent."""
    return [lc.unpack_battle_status(x.split(" ")[1])["mode"] for x in sent if x.startswith("MYBATTLESTATUS")]


class PublicSeat(Harness):
    """A public room: a player while few humans play, a spectator from 8, a
    player again below 3; always a spectator when the game starts."""

    def humans_in(self, names, ready=0):
        for n in names:
            self.feed("JOINEDBATTLE 12 " + n,
                      "CLIENTBATTLESTATUS %s %d 0" % (n, lc.battle_status(ready=ready, mode=1, sync=1)))
        return self.run_for(5)

    def test_spectates_at_8_humans_and_plays_again_below_3(self):
        self.boss_and_setup()
        self.assertTrue(self.c.playing())
        names = ["P%d" % i for i in range(8)]
        self.assertEqual(self.humans_in(names[:7]), [])                     # 7: still a player
        self.assertEqual(my_modes(self.humans_in(names[7:])), [0])          # 8: spectate
        for n in names[:5]:                                                 # 3 left: hysteresis
            self.feed("LEFTBATTLE 12 " + n)
        self.assertEqual(my_modes(self.run_for(5)), [])
        self.feed("LEFTBATTLE 12 " + names[5])                              # 2 left: play
        self.assertEqual(my_modes(self.run_for(5)), [1])

    def test_spectates_before_the_game_starts_and_plays_after(self):
        self.boss_and_setup()
        sent = self.humans_in(["Alice"], ready=1) + self.run_for(25)
        self.assertIn("SAYBATTLE !cv start", sent)
        self.assertEqual(my_modes(sent[:sent.index("SAYBATTLE !cv start")])[-1], 0)
        self.feed("CLIENTSTATUS %s 65" % HOST)
        self.proc.code = 0
        self.feed("CLIENTSTATUS %s 64" % HOST)
        sent = self.run_for(5)
        self.assertEqual(self.c.state, "waiting")
        self.assertEqual(my_modes(sent), [1])                               # 1 human: play again


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
        return self.run_for(20)

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

    def test_bosses_authorised_users_already_in_the_room(self):
        self.join()
        self.feed("JOINEDBATTLE 12 " + OWNER)
        sent = self.run_for(2)
        self.assertNotIn("SAYBATTLE !boss " + OWNER, sent)      # not boss yet
        self.feed("SAIDBATTLEEX %s * Boss mode enabled for %s" % (HOST, ME))
        self.assertIn("SAYBATTLE !boss " + OWNER, self.run_for(10))


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
