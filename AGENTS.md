# rjm.bar.host - Agent Instructions

These instructions apply to the whole repository and are the canonical guidance
for every coding agent (Claude Code, Codex, Copilot, Cursor, Gemini CLI, Aider,
Cline, and any other). Vendor files route here; they must not duplicate or
override this file.

## Project

A **reusable Beyond All Reason headless host image**,
`ghcr.io/s3kcentrifugal/rjm-bar-host` (private), built, tested and published to
GHCR by `.github/workflows/publish.yml`. It runs under Docker (`examples/`) or
Kubernetes. **This repository deploys nothing**: cluster deployments, their
settings, accounts and pipeline live in `../rjm.bar.lobbies`, which pulls this
image by digest.

One image, every server-side role (README "Use cases"):

- **`client`**: a headless lobby client (`scripts/lobby_client.py`). It takes
  a room on BAR's own autohosts, adds AI bots it owns, and runs them in its own
  `spring-headless` as a spectator client of each game. Outbound only.
- **`spads`**: a SPADS autohost on BAR's lobby server (`docker/spads/`). It
  runs Skirmish AI bots inside its own `spring-headless`. It needs inbound UDP:
  host networking plus a router forward.
- **`match`**: an unattended AI-vs-AI simulation played to game over
  (`scripts/run_match.py`). It leaves a replay, logs and `result.json`.
- **`host` / `join`**: direct-connect games between containers with no lobby
  (`scripts/lan_game.py`).

All modes share `scripts/barcommon.py`, which holds settings, start scripts,
the engine launcher, the Autohost channel and the lobby protocol's encodings.
They also share `scripts/install_ai.py` and the entrypoint's content fetch.
**Put anything two modes need in `barcommon.py`, not in one mode's script.**
Keep it one image: a variant (another engine) is a build-arg tag of the same
Dockerfile, never a second Dockerfile.

The engine is BAR's **official Linux release** (`config/engine.pin`). Custom
AIs are installed into `$BAR_ENGINE_DIR/AI/Skirmish` at container start.

**Status:**
- `match`, `spads` and `host`/`join` are verified locally, end to end.
- `client` is unit-tested and logs in on BAR's real lobby server. There,
  follow mode cannot see who is in other rooms: Teiserver reports other
  rooms' joins only to the lobbies it lists (`spring_in.ex`
  `@optimisation_level`: Chobby, skylobby, modlobby), so the followed user
  names the room with `!room`. A request to BAR to list this client is open.
- `../rjm.bar.lobbies` deploys `client` (app `bar-lobbies`). It also holds
  `spads` (`bar-spads`) and `match` (`bar-sims`), both disabled.

The design is [`docs/autohost-design.md`](docs/autohost-design.md); read it
before writing anything here.

This project **operates no lobby infrastructure**: SPADS and the client are
lobby *clients*.

### Facts an agent must not get wrong

1. **`engine-dedicated` cannot host a Skirmish AI.** It compiles zero
   `ExternalAI` sources (`rts/builds/dedicated/CMakeLists.txt`) and its
   `main.cpp` constructs only `CGameServer`. BARb requires
   `springServerType:headless`. Any design that runs AI on the dedicated build
   is wrong.
2. **Only `spads` needs inbound networking.** Its players connect to it over
   UDP (`SPADS_GAME_PORT`), so it needs host networking and a router UDP
   forward. `client`, `match` and `join` only connect out; `host` needs UDP
   from its participants only. The cluster has no inbound UDP path (HAProxy
   ingress is TCP/HTTP), which is why `../rjm.bar.lobbies` prefers `client`.
   Do not add host networking to a mode that has no human players connecting
   in.
3. **Never modify the BAR game archive to make something work here.** Changing
   `../Beyond-All-Reason` alters the archive checksum, so stock clients can no
   longer join. That rules it out for public hosting regardless of merit.
4. **Never interrupt a running game.** A running `spring-headless` in a
   container means a game is in progress (in `client` mode, the bots' engine).
   Deployers must not restart or replace it then. `../rjm.bar.lobbies`'s
   `scripts/deploy_app.sh` enforces this, so keep `spring-headless` as the
   process name.
5. **Secrets never enter git.** Lobby passwords go in `examples/<mode>/*.secrets.env`
   (gitignored) or a Kubernetes Secret. Templates end in `.example` or
   `.sample`.
6. **`client` must match BAR's engine.** It only takes rooms whose engine
   version contains `BAR_ENGINE_VERSION`. When BAR's autohosts move engine,
   `config/engine.pin` must follow, or the client finds no room.

### Open questions

- **All-AI team games collapse unless spectators sit on an observer team**
  (design §11 risk 1). BAR's `game_end.lua` wipes out the allyteam a spectator
  sits in when it holds two or more AI teams. `run_match.py` and `lan_game.py`
  seat the host and spectators on an extra empty team in its own allyteam
  (default on), which fixes it without touching the game. Never "fix" it by
  editing the game archive (fact 3).
- **SPADS against BAR's real lobby server is unverified**: TLS, login flags,
  bot flag, and whether it accepts bots added by a spectating host. The rest of
  the lobby flow is proven offline by `tests/e2e/run.sh`; run it after any
  change to `docker/spads/` or `examples/spads/spads.env`.
- **`client` against BAR's real lobby server is unverified**: whether BAR's
  autohost keeps bots whose owner spectates, and BAR's rules for automated
  accounts. The protocol is unit-tested (`tests/unit/test_lobby_client.py`).

## Repository map

| Path | Content |
| --- | --- |
| `docs/autohost-design.md` | the design: engine choice, AI install, the modes, resources, risks (its cluster sections are history: deployments moved to `../rjm.bar.lobbies`) |
| `docs/setup.md` | GitHub settings, package access, WireGuard peer generation, lobby accounts, router forward |
| `docs/lobby.md` | the automated lobby: lifecycle, `spadsctl` control socket, settings |
| `docs/testing.md` | the test suites and what each proves |
| `docs/mcp-server-design.md` | design (not built): an MCP server that runs simulations and lobbies in one namespace |
| `docker/runtime/Dockerfile` | engine release (sha256-verified) + SPADS (pinned commits) -> runtime image |
| `docker/runtime/entrypoint.sh` | mode dispatch: `client`, `spads`, `match`, `host`, `join`, `fetch-content`, `install-ai`, `list-ais` |
| `scripts/barcommon.py` | shared by every mode: settings, mod-option defaults, `Tdf`, `client_script`, `Engine`, `engine_settings`, `Autohost`, lobby `password_hash` / `battle_status` |
| `docker/spads/` | SPADS config (`etc/`), `run_spads.sh` (incl. `--check`), `plugins/Autopilot.pm` (bot bonus, auto-start, control socket), `spadsctl`, `archive_game.sh`, `ready.sh` |
| `examples/<mode>/` | per mode: `<mode>.env` (every setting: the reference), `*.secrets.env.example`, `docker-compose.yml` |
| `tests/` | `run_unit.sh` (unit tests + `static/check_examples.py`), `lan/run.sh`, `e2e/run.sh` (offline end-to-end) |
| `scripts/run_match.py` | writes the GameSetup script, drives the match over the Autohost Protocol, exits at game over |
| `scripts/lan_game.py`, `examples/lan/`, `docs/lan.md` | direct-connect (LAN) games between containers: `host` / `join` entrypoint modes |
| `scripts/lobby_client.py`, `tests/unit/test_lobby_client.py` | `client` mode: the Spring lobby protocol as Chobby speaks it (login, room choice, `!boss`, `ADDBOT`, start script), as a state machine fed lines and a clock |
| `scripts/install_ai.py` | installs custom AIs from folders, archives or GitHub releases; refuses Windows builds |
| `scripts/smoke_test.sh` | the checks CI runs before any image is published |
| `scripts/demo_stats.py`, `scripts/measure_match.py` | replay statistics; the resource measurements |
| `config/engine.pin` | engine release version and archive sha256 |
| `.github/workflows/` | `publish.yml` (build, test, publish to GHCR), `ci.yml` (checks), `e2e.yml` (lobby end-to-end), `simulate.yml` (reusable simulation) |

**Engine and AI must be interface-compatible.** A `libSkirmishAI.so` built
against an engine whose C AI interface differs from the
`AI/Interfaces/C/0.1/libAIInterface.so` beside it is dead at match start with no
useful error (CircuitAI README). The engine here is the official release; AIs
come from their own repositories' release builds (SMRTBARb:
`S3KCentrifugal/CircuitAI` releases, `*-linux.tar.gz`). Moving
`config/engine.pin` requires checking the AI still loads - the smoke test does.
Never ship a Windows build (`SkirmishAI.dll`) into the image or a volume.

Mode settings (`examples/<mode>/<mode>.env`) are **runtime environment**, not
build inputs: a deployment copies and adjusts them (a ConfigMap in
`../rjm.bar.lobbies`). `tests/static/check_examples.py` fails when a mode reads
a setting its example does not document, or an example documents one nothing
reads. When adding a setting, document it there. `config/engine.pin` is the
opposite: a build input whose change must be a reviewed diff.

## Skills that govern this project

| Skill | Use for |
| --- | --- |
| `convention-deployment` | the Dockerfile, image tagging, health checks |
| `configure-k8s-pipeline` | `.github/workflows/publish.yml` (its set-env and build jobs); the deploy half lives in `../rjm.bar.lobbies` |
| `author-k8s-manifests`, `convention-kustomize`, `troubleshoot-k8s` | only in `../rjm.bar.lobbies`: no manifests live here |

## Workspace

Every project sits side by side under `C:\bardev`. Paths below are relative to
this repository's parent.

| Project | Path | What it is | Write policy |
| --- | --- | --- | --- |
| **rjm.bar.host** | `.` | this repository - reusable BAR headless host image | writable |
| **rjm.bar.lobbies** | `../rjm.bar.lobbies` | configuration and CI/CD for everything deployed to the cluster, including every deployment of this image (`apps/bar-lobbies`, `bar-spads`, `bar-sims`) | writable |
| **rjm.bar.rustai** | `../rjm.bar.rustai` | Rust BAR Skirmish AI (`libSkirmishAI.so` over the C interface) - a consumer of this image | writable |
| **rjm.bar.telemetry** | `../rjm.bar.telemetry` | replay (`.sdfz`) telemetry into Postgres - can consume this image's replays | writable |
| **rjm.bar.docs** | `../rjm.bar.docs` | central game knowledge base for BAR + Recoil, shared by all projects | writable; **game facts only** |
| **rjm.bar.tools** | `../rjm.bar.tools` | Rust `smrt-bar-map`: reads `.sd7` archives, parses SMF, derives terrain facts | writable |
| **rjm.bar.ai** | `../rjm.bar.ai` | Rust BAR skirmish AI, first-principles design; requirements capture only | writable |
| **rjm.bar.editor** | `../rjm.bar.editor` | Godot BAR map editor; requirements capture only | writable |
| **s3k-CircuitAI** | `../s3k-CircuitAI` | the user's CircuitAI/BARb fork (`S3KCentrifugal/CircuitAI`, branch `smrt`) - **the AI this project hosts** | writable |
| **BYAR-Chobby** | `../BYAR-Chobby` | BAR's lobby client (upstream `beyond-all-reason/BYAR-Chobby`) | read-only by convention |
| **barb-playtest** | `../barb-playtest` | local playtest scratch directory (not a git repository) | inspect only |
| **rlcevg-CircuitAI** | `../rlcevg-CircuitAI` | upstream CircuitAI (`rlcevg/CircuitAI`) for comparison | read-only by convention |
| **bar-Beyond-All-Reason** | `../bar-Beyond-All-Reason` | upstream BAR game mirror (`beyond-all-reason/Beyond-All-Reason`, `master`) | **strictly read-only** |
| **Beyond-All-Reason** | `../Beyond-All-Reason` | the user's BAR game fork (`S3KCentrifugal/Beyond-All-Reason`) | writable, but see fact 3 above |
| **bar-RecoilEngine** | `../bar-RecoilEngine` | upstream Recoil engine mirror (`beyond-all-reason/RecoilEngine`, `master`) | **strictly read-only** |
| **bar-RecoilEngine-s3k-build** | `../bar-RecoilEngine-s3k-build` | local build clone (origin = `C:\bardev\bar-RecoilEngine`) | build tree; do not author source here |
| Game install | `%LOCALAPPDATA%\Programs\Beyond-All-Reason\data` | runtime logs (`infolog.txt`, `log/`), replays (`demos/`), engine releases with shipped `AI/Skirmish/BARb/` | read for evidence; never edit |

### Read-only policy

`bar-Beyond-All-Reason` and `bar-RecoilEngine` are authoritative local
references and are strictly read-only. Never edit, create, delete, rename,
format, generate, build, test, checkout, reset, clean, or update submodules in
either tree. Limit commands there to inspection: file reads, listings,
`git status`, `git log`, `git show`, `git diff`, `git grep`, `git ls-files`. If
a change belongs upstream, report it and name the target file without applying
it. Do not follow a path into a read-only tree and write through it.

Do not assume another machine has this layout; verify paths before use.

## Navigation

Where to look, by repository. Grep is fine - unit ids, gadget filenames, setting
names and command topics are stable tokens.

### `../bar-RecoilEngine` - engine (read-only)

The authority on what the server and the AI interface actually permit.

| Path | Content |
| --- | --- |
| `rts/builds/dedicated/CMakeLists.txt` | proves the dedicated build has no AI support |
| `rts/builds/headless/CMakeLists.txt` | `engine-headless`, built with `-DHEADLESS` from full engine sources |
| `rts/Net/GameServer.cpp` | server-side netmsg handling; start-position validation at `NETMSG_STARTPOS` |
| `rts/Net/NetCommands.cpp` | client-side netmsg handling; the `AllowStartPosition` gate |
| `rts/Net/AutohostInterface.h` | the Autohost Protocol SPADS speaks to the engine |
| `rts/Game/GameSetup.cpp/.h` | `script.txt` parsing; `StartPosType` enum and defaults |
| `rts/ExternalAI/Interface/` | skirmish AI C ABI: `AISEvents.h`, `AISCommands.h`, `SSkirmishAICallback.h` |
| `AI/Skirmish/`, `AI/Interfaces/C/` | bundled AIs and the C interface; `BARb` is a submodule of `rlcevg/CircuitAI` branch `barbarian` |

### `../bar-Beyond-All-Reason` - game (read-only)

| Path | Content |
| --- | --- |
| `luarules/gadgets/game_initial_spawn.lua` | start-position and spawn rules; **vetoes AI-chosen start positions** |
| `common/lib_startpoint_guesser.lua` | `GuessStartSpot`, which places AI instead |
| `modoptions.lua` | mod options, at the repository root (there is a `gamedata/`, but this is not in it). Note `startpostype` is **not** a mod option: the engine reads it from `GAME\StartPosType` (`GameSetup.cpp:646`), separately from `GAME\ModOptions` (`:643`) |
| `luarules/gadgets/` | synced/unsynced game logic |

### `../s3k-CircuitAI` - the AI this project hosts

| Path | Content |
| --- | --- |
| `AGENTS.md` | canonical instructions for that repository |
| `doc/roles/` | per-role references for the AngelScript role layer |
| `data/script/src/roles/` | the six AI roles (`air`, `front`, `sea`, `support`, `tactical`, `tech`) |
| `data/config/` | JSON behaviour profiles; profile names map to SPADS AI options |
| `.github/workflows/smrtbarb-release.yml`, `doc/release.md` | builds and publishes SMRTBARb for Windows and Linux; the Linux archive is what this image installs |
| `src/circuit/setup/SetupManager.cpp` | start position and commander selection |

The AI layout the engine loads, and that `install_ai.py` enforces:
`AI/Skirmish/<shortName>/<version>/{AIInfo.lua, AIOptions.lua, config/, script/, libSkirmishAI.so}`
alongside `AI/Interfaces/C/0.1/{InterfaceInfo.lua, libAIInterface.so}` from the
engine release. On Linux these are `.so`; a Windows install's `.dll` files are
unusable.

### `../rjm.bar.docs` - game truth

Start at `knowledge/README.md`. Relevant here mainly for map selection
(`mapList`, rotation curation) and for understanding what BARb is doing. Do not
copy game facts into this repository; name the knowledge-base path instead.

### Upstream projects (network, not local)

| Project | What it is |
| --- | --- |
| [`Yaribz/SPADS`](https://github.com/Yaribz/SPADS) | the autohost program; `etc/spads.conf` is the settings reference |
| [`beyond-all-reason/spads_config_bar`](https://github.com/beyond-all-reason/spads_config_bar) | BAR's reference `Dockerfile`, `docker-compose.yml`, `.env.sample` |
| [`beyond-all-reason/BAR-Devtools`](https://github.com/beyond-all-reason/BAR-Devtools) | local Teiserver + SPADS + client; test here before public deployment |
| [`beyond-all-reason/recoil-autohost`](https://github.com/beyond-all-reason/recoil-autohost) | the Tachyon-protocol successor to SPADS; not production-ready |

SPADS is consumed as an upstream release, not forked. Keep customisation to
configuration so the eventual migration to `recoil-autohost` stays cheap.

## Source-of-truth routing

Use the narrowest authoritative source:

- Autohost behaviour, container and deployment design: this repository.
- **What the engine and server permit** (netmsg validation, AI interface, build
  targets): `../bar-RecoilEngine`, read-only.
- **What the game permits** (spawn rules, mod options, gadget behaviour):
  `../bar-Beyond-All-Reason`, read-only.
- SPADS settings and semantics: upstream `etc/spads.conf` and BAR's config repo.
- Cluster conventions (manifests, pipeline, storage, ingress): the user's
  skills listed above, not this file and not improvisation.
- Game and unit facts: `../rjm.bar.docs/knowledge/`.
- BARb behaviour: `../s3k-CircuitAI/doc/`.

## Working rules

- Read `docs/autohost-design.md` before writing code. If reality contradicts it,
  fix the design in the same change - do not let them drift.
- Keep the two read-only trees pristine; inspection commands only.
- `test` is the default branch; every push to it publishes the image and moves
  the `test` tag, which `../rjm.bar.lobbies` deploys on its next run. Never
  push or merge to `prod`: the owner squash-merges pull requests from `test` by
  hand.
- Do not commit automatically. Commit only when asked, and only the change
  asked for.
- Never commit a secret. Check `.gitignore` covers any new credential file
  before creating it.
- Cite engine and game claims by `file:line` so they can be re-verified when
  upstream moves.
- Record the BAR and Recoil commits used in design front-matter so staleness is
  detectable.
- When a fact here contradicts `../rjm.bar.docs`, that repository wins for game
  facts and this one wins for hosting and deployment facts; fix the other side
  and say so.
