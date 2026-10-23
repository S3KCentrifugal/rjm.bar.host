#!/usr/bin/env python3
"""Install custom Skirmish AIs into the engine's AI/Skirmish directory.

The engine discovers Skirmish AIs at ``<engine>/AI/Skirmish/<shortName>/<version>/``
-- on Windows that is ``data\\engine\\recoil_<version>\\AI\\Skirmish``. This puts
custom AIs there at container start, from any mix of:

``BARHOST_CUSTOM_AI_DIR`` (default ``/bar/custom-ai``)
    A mounted folder. Every ``<Name>/`` directly inside it is linked into
    AI/Skirmish, so mounting a Windows-style ``SMRTBARb`` folder works as-is,
    provided it carries the Linux library (see below).

``BARHOST_AI_ARCHIVES``
    Whitespace- or semicolon-separated sources, each one of:

    * ``https://.../<file>.tar.gz`` or ``.zip`` -- downloaded
    * ``/path/to/<file>.tar.gz`` or ``.zip``    -- a local file
    * ``github:OWNER/REPO@TAG[#GLOB]``          -- a GitHub release asset.
      TAG is a tag, ``latest`` (newest non-prerelease) or ``newest`` (newest
      including pre-releases). GLOB picks the asset, default ``*-linux.tar.gz``.
      ``GITHUB_TOKEN`` is used if set (needed for private repositories).

    An archive may hold ``<Name>/<version>/...`` or ``AI/Skirmish/<Name>/...``;
    every directory that contains an ``AIInfo.lua`` is treated as one AI version.

An AI version directory is only installable on Linux if it has
``libSkirmishAI.so``. A folder copied from a Windows install has
``SkirmishAI.dll`` instead, and the engine would silently skip it; that is
reported as an error here rather than discovered as a missing bot mid-game.

A custom AI with the same name as a stock one (e.g. ``BARb``) replaces it.

Exit status is non-zero if any source failed, so a misconfigured AI stops the
container before a match or lobby starts.

The release tag each ``github:`` source installed is kept in
``AI/Skirmish/.rjm-releases.json``. ``update()`` (also ``install_ai.py
--update``) reinstalls a source on a moving tag (``latest`` / ``newest``) when
it now resolves to another release; the lobby client calls it between games.

Channels (``BARHOST_AI_CHANNELS``, used by the lobby client) are named AI
choices, ``name=AI`` (the engine's own AI) or ``name=AI:github:...`` (a
release). Each channel's release is downloaded into its own folder on the
data volume, ``<cache>/<channel>/<tag>/<AI>/<version>/``, so several releases
of one AI (a test and a prod build, both ``SMRTBARb/stable``) are kept side
by side and survive restarts; ``activate_channel`` points the engine's
``AI/Skirmish/<AI>`` at one of them with a symlink.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

LINUX_LIB = "libSkirmishAI.so"
WINDOWS_LIB = "SkirmishAI.dll"


def log(msg: str) -> None:
    print("[install_ai] " + msg, file=sys.stderr, flush=True)


def skirmish_dir() -> Path:
    engine = os.environ.get("BAR_ENGINE_DIR", "").strip()
    if not engine:
        raise SystemExit("BAR_ENGINE_DIR is not set")
    path = Path(engine) / "AI" / "Skirmish"
    path.mkdir(parents=True, exist_ok=True)
    return path


def check_version_dir(path: Path) -> "str | None":
    """Return an error message if this AI version dir cannot load on Linux."""
    if not (path / "AIInfo.lua").is_file():
        return "%s has no AIInfo.lua" % path
    if (path / LINUX_LIB).is_file():
        return None
    if (path / WINDOWS_LIB).is_file():
        return ("%s is a Windows build (%s, no %s). The Linux engine cannot load it; "
                "use the AI's -linux archive instead." % (path, WINDOWS_LIB, LINUX_LIB))
    return "%s has no %s" % (path, LINUX_LIB)


def check_ai_dir(ai: Path) -> "list[str]":
    """Validate every version directory of one AI (``<Name>/<version>/``)."""
    versions = [d for d in ai.iterdir() if d.is_dir() and (d / "AIInfo.lua").is_file()]
    if not versions:
        return ["%s has no <version>/AIInfo.lua" % ai]
    return [e for e in (check_version_dir(v) for v in versions) if e]


def replace_with(target: Path, make) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    make(target)


def install_mounted(root: Path, dest: Path) -> "list[str]":
    errors = []
    if not root.is_dir():
        return errors
    for ai in sorted(p for p in root.iterdir() if p.is_dir()):
        if ai.name.startswith("."):
            continue
        problems = check_ai_dir(ai)
        if problems:
            errors.extend(problems)
            continue
        target = dest / ai.name
        if target.resolve() == ai.resolve():
            continue
        existed = target.exists() or target.is_symlink()
        # A symlink, not a copy: edits to a mounted folder show up on the next
        # match without restarting anything but the engine.
        replace_with(target, lambda t: t.symlink_to(ai, target_is_directory=True))
        log("%s %s -> %s" % ("replaced" if existed else "linked", target, ai))
    return errors


def github_asset_url(spec: str) -> str:
    return github_release(spec)[1]


def github_release(spec: str) -> "tuple[str, str]":
    """(release tag, asset URL) that a ``github:`` spec resolves to now."""
    body = spec[len("github:"):]
    pattern = "*-linux.tar.gz"
    if "#" in body:
        body, pattern = body.split("#", 1)
    if "@" not in body:
        raise ValueError("expected github:OWNER/REPO@TAG[#GLOB], got " + spec)
    repo, tag = body.split("@", 1)

    api = "https://api.github.com/repos/%s/releases" % repo
    if tag == "latest":
        releases = [json.loads(fetch(api + "/latest", accept="application/vnd.github+json"))]
    elif tag == "newest":
        # Newest by publish time that actually carries a matching asset: older
        # releases of a project may predate its Linux builds.
        listed = json.loads(fetch(api + "?per_page=50", accept="application/vnd.github+json"))
        releases = sorted((r for r in listed if not r.get("draft")),
                          key=lambda r: r.get("published_at") or "", reverse=True)
    else:
        releases = [json.loads(fetch(api + "/tags/" + tag, accept="application/vnd.github+json"))]

    matches, release = [], None
    for release in releases:
        matches = [a for a in release.get("assets", []) if fnmatch.fnmatch(a["name"], pattern)]
        if matches or tag != "newest":
            break
    if release is None:
        raise ValueError("no release found")
    if len(matches) != 1:
        names = ", ".join(a["name"] for a in release.get("assets", []))
        raise ValueError("%d assets match %r in %s (assets: %s)"
                         % (len(matches), pattern, release.get("tag_name"), names))
    log("%s resolved to %s %s" % (spec, release.get("tag_name"), matches[0]["name"]))
    # The API URL works for private repositories with a token; the browser URL
    # only for public ones.
    url = matches[0]["url"] if os.environ.get("GITHUB_TOKEN") else matches[0]["browser_download_url"]
    return release.get("tag_name") or "", url


def fetch(url: str, accept: str = "") -> bytes:
    req = urllib.request.Request(url)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and ("api.github.com" in url or "github.com" in url):
        # Unredirected: asset downloads redirect to a pre-signed storage URL,
        # which rejects a request that also carries an Authorization header.
        req.add_unredirected_header("Authorization", "Bearer " + token)
    if accept:
        req.add_header("Accept", accept)
    elif "api.github.com" in url and "/assets/" in url:
        req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def extract(archive: Path, into: Path) -> None:
    name = archive.name.lower()
    if name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(archive) as tar:
            # "data" rejects absolute paths, .. escapes and device files.
            tar.extractall(into, filter="data")
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                target = (into / member).resolve()
                if not str(target).startswith(str(into.resolve()) + os.sep) and target != into.resolve():
                    raise ValueError("unsafe path in %s: %s" % (archive, member))
            zf.extractall(into)
            # zip does not keep the executable bit; the library needs only read.
    else:
        raise ValueError("unsupported archive type: " + archive.name)


def ai_dirs_in(tree: Path) -> "list[Path]":
    """``<Name>`` directories: parents of version dirs that hold AIInfo.lua."""
    found = []
    for info in tree.rglob("AIInfo.lua"):
        ai = info.parent.parent
        if ai not in found and ai != tree:
            found.append(ai)
    return found


RELEASES_FILE = ".rjm-releases.json"   # in AI/Skirmish: github spec -> installed tag


def installed_releases(dest: Path) -> dict:
    try:
        data = json.loads((dest / RELEASES_FILE).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_release(dest: Path, spec: str, tag: str) -> None:
    data = installed_releases(dest)
    data[spec] = tag
    tmp = dest / (RELEASES_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(dest / RELEASES_FILE)


def swap_in(target: Path, src: Path) -> None:
    """Copy `src` next to `target`, then swap it in, so the AI is missing for
    two renames rather than for a whole copy."""
    new = target.with_name("." + target.name + ".new")
    old = target.with_name("." + target.name + ".old")
    for p in (new, old):
        if p.exists() or p.is_symlink():
            replace_with(p, lambda _: None)
    shutil.copytree(src, new)
    if target.exists() or target.is_symlink():
        target.rename(old)
    new.rename(target)
    if old.exists() or old.is_symlink():
        replace_with(old, lambda _: None)


def install_archive(source: str, dest: Path, work: Path, resolved: "tuple[str, str] | None" = None) -> "list[str]":
    tag = None
    if source.startswith("github:"):
        tag, url = resolved or github_release(source)
        filename = source.split("#", 1)[-1] if "#" in source else "ai.tar.gz"
        filename = "ai.zip" if filename.endswith(".zip") else "ai.tar.gz"
    elif source.startswith(("https://", "http://")):
        url = source
        filename = source.rsplit("/", 1)[-1].split("?", 1)[0]
    else:
        url = None
        filename = Path(source).name

    archive = work / filename
    if url:
        log("downloading " + url)
        archive.write_bytes(fetch(url))
    else:
        shutil.copyfile(source, archive)

    unpacked = work / (filename + ".d")
    unpacked.mkdir()
    extract(archive, unpacked)

    ais = ai_dirs_in(unpacked)
    if not ais:
        return ["%s contains no <Name>/<version>/AIInfo.lua" % source]
    errors = []
    for ai in ais:
        problems = check_ai_dir(ai)
        if problems:
            errors.extend(problems)
            continue
        target = dest / ai.name
        existed = target.exists() or target.is_symlink()
        swap_in(target, ai)
        log("%s %s from %s%s" % ("replaced" if existed else "installed", target, source,
                                 (" (%s)" % tag) if tag else ""))
    if tag is not None and not errors:
        record_release(dest, source, tag)
    return errors


def update() -> "tuple[list[str], list[str]]":
    """Reinstall each ``github:`` source on a moving tag (``latest`` or
    ``newest``) whose release changed since it was installed. Returns
    (changes as "<spec>: <old> -> <new>", errors). Run between games: the
    engine loads an AI's library when a game starts."""
    dest = skirmish_dir()
    sources = os.environ.get("BARHOST_AI_ARCHIVES", "").replace(";", " ").split()
    moving = [s for s in sources if s.startswith("github:")
              and s.split("#", 1)[0].rsplit("@", 1)[-1] in ("latest", "newest")]
    changes, errors = [], []
    with tempfile.TemporaryDirectory(prefix="install_ai.") as tmp:
        for i, spec in enumerate(moving):
            try:
                tag, url = github_release(spec)
                old = installed_releases(dest).get(spec)
                if tag == old:
                    continue
                work = Path(tmp) / str(i)
                work.mkdir()
                problems = install_archive(spec, dest, work, resolved=(tag, url))
                if problems:
                    errors.extend(problems)
                else:
                    changes.append("%s: %s -> %s" % (spec, old or "unknown", tag))
            except Exception as exc:  # a failed check keeps what is installed
                errors.append("%s: %s" % (spec, exc))
    return changes, errors


# -- channels: named AI choices, their releases kept on the data volume -------

CHANNEL_FILE = "release.json"   # in <cache>/<channel>: {"tag": the current release}
CHANNEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,30}$")
AI_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,100}$")


def parse_channels(text: str) -> "dict[str, tuple[str, str]]":
    """``BARHOST_AI_CHANNELS``: ';'-separated ``name=AI`` (the engine's own
    AI) or ``name=AI:github:OWNER/REPO@TAG[#GLOB]`` -> {name: (AI, source)},
    in the order given. Names are lower case."""
    out = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError("expected name=AI[:github:...], got %r" % part)
        name, rest = (x.strip() for x in part.split("=", 1))
        ai, _, source = (x.strip() for x in rest.partition(":"))
        name = name.lower()
        if not CHANNEL_NAME_RE.match(name) or not AI_NAME_RE.match(ai):
            raise ValueError("bad AI channel %r" % part)
        if source and not source.startswith("github:"):
            raise ValueError("an AI channel's source must be github:OWNER/REPO@TAG[#GLOB]: %r" % part)
        out[name] = (ai, source)
    return out


def channel_release(cache: Path, name: str) -> str:
    """The release tag downloaded for a channel, "" if none."""
    try:
        data = json.loads((cache / name / CHANNEL_FILE).read_text())
    except (OSError, ValueError):
        return ""
    tag = data.get("tag", "") if isinstance(data, dict) else ""
    return tag if isinstance(tag, str) and TAG_RE.match(tag) and (cache / name / tag).is_dir() else ""


def prune_channel(chan: Path, keep: set) -> None:
    """Remove a channel's releases other than `keep`, and leftovers."""
    for p in chan.iterdir():
        if p.is_dir() and p.name not in keep:
            shutil.rmtree(p, ignore_errors=True)


def linked_releases(chan: Path, dest: "Path | None") -> set:
    """The channel's release folders the engine's AI links point into."""
    used = set()
    if dest is None or not dest.is_dir():
        return used
    root = chan.resolve()
    for p in dest.iterdir():
        if p.is_symlink():
            target = p.resolve()
            if root in target.parents:
                used.add(target.relative_to(root).parts[0])
    return used


def fetch_channel(cache: Path, name: str, source: str, work: Path, dest: "Path | None" = None) -> "tuple[str, str]":
    """Download a channel's release if it moved: (old tag, new tag), equal
    when nothing changed. On any failure it raises, and the channel keeps
    what it had. Never touches the engine: activate_channel does that."""
    old = channel_release(cache, name)
    tag, url = github_release(source)
    if tag == old:
        return old, old
    if not TAG_RE.match(tag):
        raise ValueError("unexpected release tag %r" % tag)
    archive = work / ("ai.zip" if source.split("#", 1)[-1].endswith(".zip") else "ai.tar.gz")
    log("downloading %s %s: %s" % (name, tag, url))
    archive.write_bytes(fetch(url))
    unpacked = work / "unpacked"
    unpacked.mkdir()
    extract(archive, unpacked)
    ais = ai_dirs_in(unpacked)
    if not ais:
        raise ValueError("%s %s contains no <Name>/<version>/AIInfo.lua" % (name, tag))
    problems = [p for ai in ais for p in check_ai_dir(ai)]
    if problems:
        raise ValueError("; ".join(problems))
    chan = cache / name
    chan.mkdir(parents=True, exist_ok=True)
    new = chan / ("." + tag + ".new")
    if new.exists():
        shutil.rmtree(new)
    new.mkdir()
    for ai in ais:
        shutil.copytree(ai, new / ai.name)
    final = chan / tag
    if final.exists():                          # an earlier try that was never recorded
        shutil.rmtree(final)
    new.rename(final)
    tmp = chan / (CHANNEL_FILE + ".tmp")
    tmp.write_text(json.dumps({"tag": tag, "source": source}))
    tmp.replace(chan / CHANNEL_FILE)
    # The release before stays until the next one, and any the engine still
    # points at: the client activates the new one between games.
    prune_channel(chan, {tag, old} | linked_releases(chan, dest))
    log("%s: %s -> %s" % (name, old or "none", tag))
    return old, tag


def update_channels(channels: dict, cache: Path, dest: "Path | None" = None) -> "tuple[list[str], list[str], dict]":
    """Fetch every channel with a source. Returns (changes as "<name>:
    <old> -> <new>", errors, {name: current tag}). `dest` (AI/Skirmish)
    keeps the releases the engine links to."""
    changes, errors = [], []
    with tempfile.TemporaryDirectory(prefix="install_ai.") as tmp:
        for i, (name, (_ai, source)) in enumerate(channels.items()):
            if not source:
                continue
            work = Path(tmp) / str(i)
            work.mkdir()
            try:
                old, new = fetch_channel(cache, name, source, work, dest)
                if new != old:
                    changes.append("%s: %s -> %s" % (name, old or "none", new))
            except Exception as exc:  # a failed check keeps what is there
                errors.append("%s: %s" % (name, exc))
    return changes, errors, {n: channel_release(cache, n) for n, (_a, s) in channels.items() if s}


def activate_channel(cache: Path, name: str, channels: dict, dest: Path) -> str:
    """Point the engine's ``AI/Skirmish/<AI>`` at the channel's release (a
    symlink, swapped in one rename). A channel without a source uses the
    engine's own AI: nothing to do. Returns the release tag ("" for none).
    Call only between games: the engine loads the AI when a game starts."""
    _ai, source = channels[name]
    if not source:
        return ""
    tag = channel_release(cache, name)
    if not tag:
        raise ValueError("%s is not downloaded yet" % name)
    release = cache / name / tag
    for ai in sorted(p for p in release.iterdir() if p.is_dir()):
        target = dest / ai.name
        if target.is_symlink() and target.resolve() == ai.resolve():
            continue
        link = dest / ("." + ai.name + ".link")
        if link.exists() or link.is_symlink():
            replace_with(link, lambda _: None)
        link.symlink_to(ai, target_is_directory=True)
        if target.is_symlink() or target.is_file():
            try:
                os.replace(link, target)        # one atomic rename on Linux
            except OSError:                     # Windows: not over a directory link
                target.unlink()
                link.rename(target)
        elif target.is_dir():                   # installed by an earlier image or source
            old = dest / ("." + ai.name + ".old")
            if old.exists() or old.is_symlink():
                replace_with(old, lambda _: None)
            target.rename(old)
            link.rename(target)
            shutil.rmtree(old, ignore_errors=True)
        else:
            link.rename(target)
        log("%s -> %s (%s %s)" % (target, ai, name, tag))
    return tag


def main() -> int:
    dest = skirmish_dir()
    errors = []

    mounted = os.environ.get("BARHOST_CUSTOM_AI_DIR", "").strip()
    if mounted:
        errors.extend(install_mounted(Path(mounted), dest))

    sources = os.environ.get("BARHOST_AI_ARCHIVES", "").replace(";", " ").split()
    with tempfile.TemporaryDirectory(prefix="install_ai.") as tmp:
        for i, source in enumerate(sources):
            work = Path(tmp) / str(i)
            work.mkdir()
            try:
                errors.extend(install_archive(source, dest, work))
            except Exception as exc:  # report every source, then fail once
                errors.append("%s: %s" % (source, exc))

    installed = sorted(p.name for p in dest.iterdir() if p.is_dir())
    log("AI/Skirmish now holds: " + ", ".join(installed))
    for err in errors:
        log("ERROR: " + err)
    return 1 if errors else 0


def main_update() -> int:
    changes, errors = update()
    for c in changes:
        log("updated " + c)
    for err in errors:
        log("ERROR: " + err)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main_update() if "--update" in sys.argv[1:] else main())
