#!/usr/bin/env python3
"""Static checks that examples/ stays the truthful reference for every mode.

  - every key an example sets (or shows commented out) is read by the image:
    a script in scripts/, the entrypoint, or a SPADS config template
  - every setting the SPADS config templates read is documented in
    examples/spads/ (or is one the image sets itself)
  - every setting lobby_client.py reads is documented in examples/client/
  - every compose file parses, runs this image, and names env files that exist
    (a *.secrets.env must have its *.secrets.env.example template)

Needs PyYAML.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EX = ROOT / "examples"
IMAGE = "ghcr.io/s3kcentrifugal/rjm-bar-host"
ERRORS = []

# Set by the image or the entrypoint, or deliberately advanced: not in examples.
IMAGE_SET = {"BAR_ENGINE_DIR", "BAR_ENGINE_VERSION", "BARHOST_OUT_DIR", "BARHOST_DATA_DIR",
             "BARHOST_CUSTOM_AI_DIR", "SPADS_FORCE_HOST_IP", "SPADS_SPRING_CONFIG", "SPADS_PLUGINS",
             "SPADS_LOBBY_TLS", "BARHOST_STATUS_FILE", "SPADS_LOBBY_LOGIN", "SPADS_LOBBY_PASSWORD"}

KEY = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", re.M)


def check(cond, msg):
    if not cond:
        ERRORS.append(msg)


def keys_in(path: Path) -> set:
    return set(KEY.findall(path.read_text(encoding="utf-8")))


def sources() -> str:
    files = list((ROOT / "scripts").glob("*.py")) + list((ROOT / "docker").rglob("*"))
    return "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in files if f.is_file())


src = sources()

# 1. Every documented key is read somewhere.
for f in sorted(EX.glob("*/*.env")) + sorted(EX.glob("*/*.env.example")):
    for k in sorted(keys_in(f)):
        check(k in src, "%s documents %s, which nothing in scripts/ or docker/ reads" % (f.relative_to(ROOT), k))

# 2. SPADS templates' settings are documented.
spads_doc = keys_in(EX / "spads" / "spads.env") | keys_in(EX / "spads" / "spads.secrets.env.example")
conf = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "docker" / "spads" / "etc").glob("*.conf"))
plugin = (ROOT / "docker" / "spads" / "plugins" / "Autopilot.pm").read_text(encoding="utf-8")
read = set(re.findall(r"ENV\{([A-Z0-9_]+)\}", conf)) | set(re.findall(r"\$ENV\{['\"]?([A-Z0-9_]+)", plugin))
for k in sorted(read - spads_doc - IMAGE_SET):
    check(False, "SPADS reads %s, but examples/spads/ does not document it" % k)

# 3. The lobby client's settings are documented.
client_doc = keys_in(EX / "client" / "client.env") | keys_in(EX / "client" / "client.secrets.env.example")
client_src = (ROOT / "scripts" / "lobby_client.py").read_text(encoding="utf-8")
client_read = set(re.findall(r'g\("([A-Z0-9_]+)"', client_src))
for k in sorted(client_read - client_doc - IMAGE_SET):
    check(False, "lobby_client.py reads %s, but examples/client/ does not document it" % k)

# 4. Compose files.
for f in sorted(EX.glob("*/docker-compose.yml")):
    rel = f.relative_to(ROOT)
    doc = yaml.safe_load(f.read_text(encoding="utf-8"))
    for name, svc in doc.get("services", {}).items():
        image = svc.get("image") or doc.get("x-common", {}).get("image", "")
        check(image.startswith(IMAGE + ":"), "%s %s: image %r is not %s" % (rel, name, image, IMAGE))
        for e in svc.get("env_file", []) or []:
            p = f.parent / e
            if e.endswith(".secrets.env"):
                check((f.parent / (e + ".example")).is_file(), "%s %s: %s has no %s.example" % (rel, name, e, e))
            else:
                check(p.is_file(), "%s %s: env file %s missing" % (rel, name, e))

if ERRORS:
    for e in ERRORS:
        print("FAIL:", e)
    sys.exit(1)
print("examples OK (%d compose files)" % len(list(EX.glob("*/docker-compose.yml"))))
