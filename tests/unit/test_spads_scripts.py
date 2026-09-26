"""SPADS wrapper scripts: configuration check, owner guard, game archiving,
readiness probe, and the spadsctl client's error handling."""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

SPADS = Path("/opt/rjm-host/spads")
CREDS = {"SPADS_LOBBY_LOGIN": "AutoHost", "SPADS_LOBBY_PASSWORD": "pw", "SPADS_OWNER_LOBBY_LOGIN": "Owner"}


def run(cmd, env=None, **kw):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(cmd, env=e, capture_output=True, text=True, timeout=120, **kw)


class RunSpads(unittest.TestCase):
    def check(self, extra):
        env = dict(CREDS, BARHOST_OUT_DIR=tempfile.mkdtemp())
        env.update(extra)
        return run([str(SPADS / "run_spads.sh"), "--check"], env)

    def test_config_loads(self):
        r = self.check({})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("springServerType = headless", r.stdout)
        self.assertIn("configuration OK", r.stdout)

    def test_prod_ports(self):
        r = self.check({"SPADS_GAME_PORT": "8462", "SPADS_AUTOHOST_PORT": "8464"})
        self.assertIn("port = 8462", r.stdout)
        self.assertIn("autoHostPort = 8464", r.stdout)

    def test_owner_is_required(self):
        r = self.check({"SPADS_OWNER_LOBBY_LOGIN": ""})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("every user the owner", r.stderr)

    def test_owner_must_be_a_lobby_name(self):
        r = self.check({"SPADS_OWNER_LOBBY_LOGIN": "a b"})
        self.assertNotEqual(r.returncode, 0)
        r = self.check({"SPADS_OWNER_LOBBY_LOGIN": "[S3K]Owner_1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_password_file(self):
        f = Path(tempfile.mkdtemp()) / "pw"
        f.write_text("from-file\n")
        r = self.check({"SPADS_LOBBY_PASSWORD": "", "SPADS_LOBBY_PASSWORD_FILE": str(f)})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_credentials(self):
        r = self.check({"SPADS_LOBBY_LOGIN": ""})
        self.assertNotEqual(r.returncode, 0)


class ArchiveGame(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        var = self.out / "spads" / "var"
        (var / "demos").mkdir(parents=True)
        (var / "infolog.txt").write_text("infolog")
        (var / "startscript.txt").write_text("script")
        (var / "demos" / "g.sdfz").write_text("demo")

    def archive(self, *args):
        return run([str(SPADS / "archive_game.sh"), *args], {"BARHOST_OUT_DIR": str(self.out)})

    def test_archives_logs_and_links_replay(self):
        r = self.archive("abc123", "1790000000", "1790000600", "gameOver")
        self.assertEqual(r.returncode, 0, r.stderr)
        games = list((self.out / "spads" / "games").iterdir())
        self.assertEqual(len(games), 1)
        g = games[0]
        self.assertTrue(g.name.endswith("-abc123"))
        self.assertEqual((g / "infolog.txt").read_text(), "infolog")
        self.assertEqual((g / "startscript.txt").read_text(), "script")
        self.assertTrue((g / "replay.sdfz").is_symlink())
        meta = json.loads((g / "game.json").read_text())
        self.assertEqual(meta["result"], "gameOver")
        self.assertEqual(meta["endTimestamp"], 1790000600)

    def test_hostile_arguments_cannot_become_paths(self):
        r = self.archive("../../x", "1;rm", "$(id)", "a b")
        self.assertEqual(r.returncode, 0, r.stderr)
        games = list((self.out / "spads" / "games").iterdir())
        self.assertEqual(len(games), 1)
        self.assertTrue(games[0].name.endswith("-unknown"))
        meta = json.loads((games[0] / "game.json").read_text())
        self.assertEqual(meta["result"], "unknown")
        self.assertEqual(meta["startTimestamp"], 0)


class ReadyProbe(unittest.TestCase):
    """ready.sh passes only for this container's SPADS process's own lobby socket."""

    def test_ready_tracks_the_spads_process_connection(self):
        port = 18200
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(5)
        conns = []
        t = threading.Thread(target=lambda: conns.append(srv.accept()), daemon=True)
        t.start()

        env = {"SPADS_LOBBY_PORT": str(port)}
        # Not connected yet: no process named spads.pl at all.
        self.assertNotEqual(run([str(SPADS / "ready.sh")], env).returncode, 0)

        d = Path(tempfile.mkdtemp())
        fake = d / "spads.pl"
        fake.write_text(f"use IO::Socket::INET; my $s=IO::Socket::INET->new('127.0.0.1:{port}') or die; sleep 60;\n")
        proc = subprocess.Popen(["perl", str(fake)])
        try:
            for _ in range(50):
                if run([str(SPADS / "ready.sh")], env).returncode == 0:
                    break
                time.sleep(0.1)
            self.assertEqual(run([str(SPADS / "ready.sh")], env).returncode, 0)
        finally:
            proc.kill()
            proc.wait()
        time.sleep(0.3)
        self.assertNotEqual(run([str(SPADS / "ready.sh")], env).returncode, 0)

        # A connection to the lobby port by some *other* process does not count.
        other = socket.create_connection(("127.0.0.1", port))
        try:
            self.assertNotEqual(run([str(SPADS / "ready.sh")], env).returncode, 0)
        finally:
            other.close()
            srv.close()


class Spadsctl(unittest.TestCase):
    def test_no_socket_is_a_clear_error(self):
        r = run(["spadsctl", "status"], {"AUTOPILOT_SOCKET": "/nonexistent.sock"})
        self.assertEqual(r.returncode, 3)
        self.assertIn("cannot reach the Autopilot socket", r.stderr)

    def test_refused_reply_exits_nonzero(self):
        path = Path(tempfile.mkdtemp()) / "s.sock"
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(str(path))
        srv.listen(1)

        def serve():
            c, _ = srv.accept()
            c.recv(100)
            c.sendall(b'{"ok": false, "error": "nope"}\n')
            c.close()
        threading.Thread(target=serve, daemon=True).start()
        r = run(["spadsctl", "frobnicate"], {"AUTOPILOT_SOCKET": str(path)})
        self.assertEqual(r.returncode, 1)
        self.assertIn('"error": "nope"', r.stdout)
        srv.close()


if __name__ == "__main__":
    unittest.main()
