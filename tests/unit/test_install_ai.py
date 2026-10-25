"""install_ai.py: custom AI installation into $BAR_ENGINE_DIR/AI/Skirmish."""

import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("RJM_BIN", "/opt/rjm-bar-host/bin"))
import install_ai  # noqa: E402


def make_ai(root: Path, name: str, version: str = "stable", lib: str = "libSkirmishAI.so") -> Path:
    v = root / name / version
    v.mkdir(parents=True)
    (v / "AIInfo.lua").write_text("return {}\n")
    (v / lib).write_bytes(b"\x7fELF")
    return root / name


def tar_of(src: Path, members: list, dest: Path) -> Path:
    with tarfile.open(dest, "w:gz") as t:
        for m in members:
            t.add(src / m, arcname=m)
    return dest


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.engine = self.tmp / "engine"
        (self.engine / "AI" / "Skirmish").mkdir(parents=True)
        make_ai(self.engine / "AI" / "Skirmish", "BARb")
        self.env = mock.patch.dict(os.environ, {
            "BAR_ENGINE_DIR": str(self.engine),
            "BARHOST_CUSTOM_AI_DIR": str(self.tmp / "custom"),
            "BARHOST_AI_ARCHIVES": "",
            "GITHUB_TOKEN": "",
        })
        self.env.start()
        self.skirmish = self.engine / "AI" / "Skirmish"

    def tearDown(self):
        self.env.stop()


class MountedFolders(Env):
    def test_linux_ai_is_linked(self):
        make_ai(self.tmp / "custom", "SMRTBARb")
        self.assertEqual(install_ai.main(), 0)
        link = self.skirmish / "SMRTBARb"
        self.assertTrue(link.is_symlink())
        self.assertTrue((link / "stable" / "libSkirmishAI.so").exists())

    def test_windows_build_is_refused_with_reason(self):
        make_ai(self.tmp / "custom", "WinAI", lib="SkirmishAI.dll")
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(install_ai.main(), 1)
        self.assertIn("is a Windows build", err.getvalue())
        self.assertFalse((self.skirmish / "WinAI").exists())

    def test_custom_ai_replaces_stock_one_of_same_name(self):
        make_ai(self.tmp / "custom", "BARb")
        self.assertEqual(install_ai.main(), 0)
        self.assertTrue((self.skirmish / "BARb").is_symlink())

    def test_missing_mount_is_not_an_error(self):
        self.assertEqual(install_ai.main(), 0)

    def test_folder_without_version_dir_is_refused(self):
        (self.tmp / "custom" / "Empty").mkdir(parents=True)
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(install_ai.main(), 1)


class Archives(Env):
    def test_release_layout_tar_installs(self):
        src = self.tmp / "src"
        make_ai(src, "SMRTBARb")
        archive = tar_of(src, ["SMRTBARb"], self.tmp / "SMRTBARb-v1-linux.tar.gz")
        os.environ["BARHOST_AI_ARCHIVES"] = str(archive)
        self.assertEqual(install_ai.main(), 0)
        self.assertTrue((self.skirmish / "SMRTBARb" / "stable" / "libSkirmishAI.so").is_file())

    def test_engine_tree_layout_tar_installs(self):
        src = self.tmp / "src"
        make_ai(src / "AI" / "Skirmish", "Other")
        archive = tar_of(src, ["AI"], self.tmp / "other.tar.gz")
        os.environ["BARHOST_AI_ARCHIVES"] = str(archive)
        self.assertEqual(install_ai.main(), 0)
        self.assertTrue((self.skirmish / "Other" / "stable" / "AIInfo.lua").is_file())

    def test_zip_installs(self):
        z = self.tmp / "ai.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("ZipAI/stable/AIInfo.lua", "return {}")
            f.writestr("ZipAI/stable/libSkirmishAI.so", "x")
        os.environ["BARHOST_AI_ARCHIVES"] = str(z)
        self.assertEqual(install_ai.main(), 0)
        self.assertTrue((self.skirmish / "ZipAI" / "stable" / "libSkirmishAI.so").is_file())

    def test_zip_path_traversal_is_rejected(self):
        z = self.tmp / "evil.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("../../escape.txt", "x")
        os.environ["BARHOST_AI_ARCHIVES"] = str(z)
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(install_ai.main(), 1)
        self.assertIn("unsafe path", err.getvalue())
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_tar_path_traversal_is_rejected(self):
        t = self.tmp / "evil.tar.gz"
        with tarfile.open(t, "w:gz") as f:
            data = b"x"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(data)
            f.addfile(info, io.BytesIO(data))
        os.environ["BARHOST_AI_ARCHIVES"] = str(t)
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(install_ai.main(), 1)
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_windows_archive_is_refused(self):
        src = self.tmp / "src"
        make_ai(src, "SMRTBARb", lib="SkirmishAI.dll")
        archive = tar_of(src, ["SMRTBARb"], self.tmp / "win.tar.gz")
        os.environ["BARHOST_AI_ARCHIVES"] = str(archive)
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(install_ai.main(), 1)
        self.assertIn("is a Windows build", err.getvalue())

    def test_every_source_is_reported_before_failing(self):
        os.environ["BARHOST_AI_ARCHIVES"] = "/nope/a.tar.gz;/nope/b.tar.gz"
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(install_ai.main(), 1)
        self.assertIn("/nope/a.tar.gz", err.getvalue())
        self.assertIn("/nope/b.tar.gz", err.getvalue())


def release(tag, assets, prerelease=False, published="2026-01-01T00:00:00Z"):
    return {"tag_name": tag, "prerelease": prerelease, "draft": False, "published_at": published,
            "assets": [{"name": a, "url": f"https://api.github.com/assets/{a}",
                        "browser_download_url": f"https://github.com/dl/{a}"} for a in assets]}


class GithubSpecs(Env):
    def fake_fetch(self, responses):
        def fetch(url, accept=""):
            for prefix, body in responses.items():
                if url.startswith(prefix):
                    return json.dumps(body).encode()
            raise AssertionError("unexpected URL " + url)
        return mock.patch.object(install_ai, "fetch", side_effect=fetch)

    def test_newest_skips_releases_without_a_linux_asset(self):
        api = "https://api.github.com/repos/o/r/releases"
        releases = [
            release("v2-test", ["AI-v2-test-linux.tar.gz", "AI-v2-test-windows.zip"], True, "2026-09-25T23:00:00Z"),
            release("v1", ["AI-v1.zip"], False, "2026-09-26T00:00:00Z"),   # newer, but Windows-only
        ]
        with self.fake_fetch({api + "?per_page": releases}):
            url = install_ai.github_asset_url("github:o/r@newest")
        self.assertEqual(url, "https://github.com/dl/AI-v2-test-linux.tar.gz")

    def test_latest_uses_the_latest_endpoint(self):
        api = "https://api.github.com/repos/o/r/releases"
        with self.fake_fetch({api + "/latest": release("v3", ["AI-v3-linux.tar.gz"])}):
            self.assertTrue(install_ai.github_asset_url("github:o/r@latest").endswith("AI-v3-linux.tar.gz"))

    def test_explicit_tag_and_glob(self):
        api = "https://api.github.com/repos/o/r/releases"
        with self.fake_fetch({api + "/tags/v4": release("v4", ["a-linux.tar.gz", "a.zip"])}):
            self.assertTrue(install_ai.github_asset_url("github:o/r@v4#*.zip").endswith("/a.zip"))

    def test_ambiguous_glob_is_an_error(self):
        api = "https://api.github.com/repos/o/r/releases"
        with self.fake_fetch({api + "/tags/v5": release("v5", ["a-linux.tar.gz", "a-linux-dbg.tar.gz"])}):
            with self.assertRaises(ValueError):
                install_ai.github_asset_url("github:o/r@v5#*-linux*.tar.gz")

    def test_token_selects_the_api_asset_url(self):
        api = "https://api.github.com/repos/o/r/releases"
        os.environ["GITHUB_TOKEN"] = "t"
        with self.fake_fetch({api + "/tags/v6": release("v6", ["a-linux.tar.gz"])}):
            self.assertTrue(install_ai.github_asset_url("github:o/r@v6").startswith("https://api.github.com/assets/"))

    def test_malformed_spec(self):
        with self.assertRaises(ValueError):
            install_ai.github_asset_url("github:o/r")


class Update(Env):
    """update(): reinstall a moving-tag release when its tag changes."""

    SPEC = "github:o/r@latest"

    def build(self, tag):
        """A release archive whose AI records its tag."""
        built = self.tmp / ("SMRTBARb-%s-linux.tar.gz" % tag)
        if built.exists():
            return built.read_bytes()
        src = self.tmp / ("src-" + tag)
        make_ai(src, "SMRTBARb")
        (src / "SMRTBARb" / "stable" / "release.txt").write_text(tag)
        return tar_of(src, ["SMRTBARb"], self.tmp / ("SMRTBARb-%s-linux.tar.gz" % tag)).read_bytes()

    def serve(self, tag, broken=False):
        api = "https://api.github.com/repos/o/r/releases/latest"
        asset = "SMRTBARb-%s-linux.tar.gz" % tag
        body = b"not a tarball" if broken else self.build(tag)
        self.fetched = []

        def fetch(url, accept=""):
            self.fetched.append(url)
            if url == api:
                return json.dumps(release(tag, [asset, "SMRTBARb-%s-linux-dbg.tar.gz" % tag])).encode()
            if url == "https://github.com/dl/" + asset:
                return body
            raise AssertionError("unexpected URL " + url)
        return mock.patch.object(install_ai, "fetch", side_effect=fetch)

    def installed(self):
        return (self.skirmish / "SMRTBARb" / "stable" / "release.txt").read_text()

    def setUp(self):
        super().setUp()
        os.environ["BARHOST_AI_ARCHIVES"] = self.SPEC
        with self.serve("v1.958-prod"):
            self.assertEqual(install_ai.main(), 0)

    def test_start_records_the_installed_tag(self):
        self.assertEqual(install_ai.installed_releases(self.skirmish), {self.SPEC: "v1.958-prod"})
        self.assertEqual(self.installed(), "v1.958-prod")

    def test_same_release_is_not_downloaded_again(self):
        with self.serve("v1.958-prod"):
            self.assertEqual(install_ai.update(), ([], []))
        self.assertEqual(len(self.fetched), 1)                 # only the release lookup

    def test_a_new_release_replaces_the_installed_one(self):
        with self.serve("v1.960-prod"):
            changes, errors = install_ai.update()
        self.assertEqual(errors, [])
        self.assertEqual(changes, ["%s: v1.958-prod -> v1.960-prod" % self.SPEC])
        self.assertEqual(self.installed(), "v1.960-prod")
        self.assertEqual(install_ai.installed_releases(self.skirmish)[self.SPEC], "v1.960-prod")
        self.assertFalse([p for p in self.skirmish.iterdir() if p.name.startswith(".SMRTBARb")])

    def test_a_broken_release_keeps_the_installed_one(self):
        with self.serve("v1.961-prod", broken=True):
            changes, errors = install_ai.update()
        self.assertEqual(changes, [])
        self.assertTrue(errors)
        self.assertEqual(self.installed(), "v1.958-prod")
        self.assertEqual(install_ai.installed_releases(self.skirmish)[self.SPEC], "v1.958-prod")

    def test_a_fixed_tag_is_never_checked(self):
        os.environ["BARHOST_AI_ARCHIVES"] = "github:o/r@v1.958-prod"
        with self.serve("v1.960-prod"):
            self.assertEqual(install_ai.update(), ([], []))
        self.assertEqual(self.fetched, [])


class PinSource(unittest.TestCase):
    """!ai <channel> <tag>: the channel's source with an exact tag."""

    def test_the_tag_replaces_the_moving_one_and_the_glob_stays(self):
        self.assertEqual(install_ai.pin_source("github:o/r@newest#*-test-linux.tar.gz", "v1.1150-test"),
                         "github:o/r@v1.1150-test#*-test-linux.tar.gz")
        self.assertEqual(install_ai.pin_source("github:o/r@latest", "v1.1153-prod"), "github:o/r@v1.1153-prod")

    def test_a_bad_tag_is_refused(self):
        for tag in ("", "v1/../x", "a b", "x" * 101):
            with self.assertRaises(ValueError):
                install_ai.pin_source("github:o/r@latest", tag)


class Channels(Env):
    """AI channels: each channel's release kept in its own folder (the data
    volume), the engine pointed at one of them by a symlink."""

    CHANNELS = {"barb": ("BARb", ""),
                "smrt-prod": ("SMRTBARb", "github:o/r@latest"),
                "smrt-test": ("SMRTBARb", "github:o/r@newest#*-test-linux.tar.gz")}

    def setUp(self):
        super().setUp()
        self.cache = self.tmp / "data" / "ai-releases"
        self.served = {}            # channel kind -> tag
        self.broken = set()
        self.fetched = []

    def build(self, tag):
        src = self.tmp / ("src-" + tag)
        if not src.exists():
            make_ai(src, "SMRTBARb")
            (src / "SMRTBARb" / "stable" / "release.txt").write_text(tag)
        return tar_of(src, ["SMRTBARb"], self.tmp / ("SMRTBARb-%s-linux.tar.gz" % tag)).read_bytes()

    def fetch(self, url, accept=""):
        self.fetched.append(url)
        api = "https://api.github.com/repos/o/r/releases"
        if url == api + "/latest":
            tag = self.served["prod"]
            return json.dumps(release(tag, ["SMRTBARb-%s-linux.tar.gz" % tag])).encode()
        if url.startswith(api + "?per_page"):
            tag = self.served["test"]
            return json.dumps([release(tag, ["SMRTBARb-%s-linux.tar.gz" % tag,
                                             "SMRTBARb-%s-linux-dbg.tar.gz" % tag], True)]).encode()
        for tag in self.served.values():
            if url == "https://github.com/dl/SMRTBARb-%s-linux.tar.gz" % tag:
                return b"not a tarball" if tag in self.broken else self.build(tag)
        raise AssertionError("unexpected URL " + url)

    def update(self):
        with mock.patch.object(install_ai, "fetch", side_effect=self.fetch):
            return install_ai.update_channels(self.CHANNELS, self.cache, self.skirmish)

    def active(self):
        return (self.skirmish / "SMRTBARb" / "stable" / "release.txt").read_text()

    def test_parse(self):
        self.assertEqual(install_ai.parse_channels(
            " barb=BARb ; Smrt-Prod=SMRTBARb:github:o/r@latest;smrt-test=SMRTBARb:github:o/r@newest#*-test-linux.tar.gz;"),
            self.CHANNELS)
        for bad in ("barb", "=BARb", "x=", "x=SMRT BARb", "x=A:https://e.com/a.tar.gz", "-x=A"):
            with self.assertRaises(ValueError, msg=bad):
                install_ai.parse_channels(bad)

    def test_each_channel_is_downloaded_into_its_own_folder(self):
        self.served = {"prod": "v2-prod", "test": "v3-test"}
        changes, errors, releases = self.update()
        self.assertEqual(errors, [])
        self.assertEqual(changes, ["smrt-prod: none -> v2-prod", "smrt-test: none -> v3-test"])
        self.assertEqual(releases, {"smrt-prod": "v2-prod", "smrt-test": "v3-test"})
        for channel, tag in releases.items():
            self.assertEqual((self.cache / channel / tag / "SMRTBARb" / "stable" / "release.txt").read_text(), tag)
        self.assertFalse((self.skirmish / "SMRTBARb").exists())     # the engine is untouched

    def test_an_unchanged_release_is_not_downloaded_again(self):
        self.served = {"prod": "v2-prod", "test": "v3-test"}
        self.update()
        self.fetched.clear()
        self.assertEqual(self.update()[:2], ([], []))
        self.assertEqual(len(self.fetched), 2)                      # the two release lookups

    def test_activation_swaps_the_engine_between_releases(self):
        self.served = {"prod": "v2-prod", "test": "v3-test"}
        self.update()
        self.assertEqual(install_ai.activate_channel(self.cache, "smrt-test", self.CHANNELS, self.skirmish), "v3-test")
        self.assertTrue((self.skirmish / "SMRTBARb").is_symlink())
        self.assertEqual(self.active(), "v3-test")
        self.assertEqual(install_ai.activate_channel(self.cache, "smrt-prod", self.CHANNELS, self.skirmish), "v2-prod")
        self.assertEqual(self.active(), "v2-prod")
        self.assertEqual(install_ai.activate_channel(self.cache, "barb", self.CHANNELS, self.skirmish), "")
        self.assertTrue((self.skirmish / "BARb" / "stable" / "AIInfo.lua").exists())   # the game's own, untouched
        self.assertFalse([p for p in self.skirmish.iterdir() if p.name.startswith(".")])

    def test_activation_replaces_an_installed_folder(self):
        make_ai(self.skirmish, "SMRTBARb")                          # from BARHOST_AI_ARCHIVES at start
        self.served = {"prod": "v2-prod", "test": "v3-test"}
        self.update()
        install_ai.activate_channel(self.cache, "smrt-prod", self.CHANNELS, self.skirmish)
        self.assertEqual(self.active(), "v2-prod")
        self.assertFalse([p for p in self.skirmish.iterdir() if p.name.startswith(".")])

    def test_activation_of_a_release_not_downloaded_fails(self):
        with self.assertRaises(ValueError):
            install_ai.activate_channel(self.cache, "smrt-test", self.CHANNELS, self.skirmish)

    def test_a_new_release_keeps_the_one_the_engine_runs(self):
        self.served = {"prod": "v2-prod", "test": "v3-test"}
        self.update()
        install_ai.activate_channel(self.cache, "smrt-test", self.CHANNELS, self.skirmish)
        self.served["test"] = "v4-test"
        self.assertEqual(self.update()[0], ["smrt-test: v3-test -> v4-test"])
        self.assertEqual(self.active(), "v3-test")                  # until activated between games
        self.served["test"] = "v5-test"
        self.update()                                               # v3 is still linked: kept
        self.assertEqual(sorted(p.name for p in (self.cache / "smrt-test").iterdir() if p.is_dir()),
                         ["v3-test", "v4-test", "v5-test"])
        install_ai.activate_channel(self.cache, "smrt-test", self.CHANNELS, self.skirmish)
        self.assertEqual(self.active(), "v5-test")
        self.served["test"] = "v6-test"
        self.update()
        self.assertEqual(sorted(p.name for p in (self.cache / "smrt-test").iterdir() if p.is_dir()),
                         ["v5-test", "v6-test"])

    def test_a_broken_release_keeps_what_was_there(self):
        self.served = {"prod": "v2-prod", "test": "v3-test"}
        self.update()
        self.served["test"] = "v4-test"
        self.broken.add("v4-test")
        changes, errors, releases = self.update()
        self.assertEqual(changes, [])
        self.assertTrue(errors and errors[0].startswith("smrt-test: "))
        self.assertEqual(releases["smrt-test"], "v3-test")
        self.assertEqual([p.name for p in (self.cache / "smrt-test").iterdir() if p.is_dir()], ["v3-test"])

    def test_releases_survive_a_restart(self):
        self.served = {"prod": "v2-prod", "test": "v3-test"}
        self.update()
        self.assertEqual(install_ai.channel_release(self.cache, "smrt-test"), "v3-test")
        self.served = {}                                            # GitHub unreachable
        with mock.patch.object(install_ai, "fetch", side_effect=OSError("no network")):
            changes, errors, releases = install_ai.update_channels(self.CHANNELS, self.cache, self.skirmish)
        self.assertEqual(len(errors), 2)
        self.assertEqual(releases, {"smrt-prod": "v2-prod", "smrt-test": "v3-test"})
        self.assertEqual(install_ai.activate_channel(self.cache, "smrt-test", self.CHANNELS, self.skirmish), "v3-test")


if __name__ == "__main__":
    unittest.main()
