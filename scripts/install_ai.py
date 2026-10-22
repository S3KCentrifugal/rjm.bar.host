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
"""

from __future__ import annotations

import fnmatch
import json
import os
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
