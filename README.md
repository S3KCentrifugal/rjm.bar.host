# rjm.bar.host

A reusable **Beyond All Reason headless host**: one container image,
`ghcr.io/s3kcentrifugal/rjm-bar-host`, built, tested and published to GHCR by
this repository. It runs BAR's official Linux engine with no graphics, and can
play every role a server-side BAR process can play. Run it under Docker
(`examples/`) or Kubernetes (deployments live in
[`rjm.bar.lobbies`](https://github.com/S3KCentrifugal/rjm.bar.lobbies)).

Agent and contributor rules: [`AGENTS.md`](AGENTS.md). Design:
[`docs/autohost-design.md`](docs/autohost-design.md). Tests:
[`docs/testing.md`](docs/testing.md).

## Use cases

The container's first argument picks the mode. Each mode has a settings
reference and a compose file under `examples/<mode>/`.

| Mode | Use case | Game server | Bots run in | Lobby server | Inbound network | Account | Example |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `client` | server-side bots in BAR rooms with no router changes: **follow** a person into the room they host and bring the bots, or run a **room** alone | BAR's autohost | this container | yes | **none** | one per room | [`examples/client`](examples/client) |
| `spads` | your own room with full control (presets, vote rules, takeover protection) | this container | this container | yes | **UDP game port**, router forward | one per room (bot flag for more than 8 players) | [`examples/spads`](examples/spads) |
| `match` | testing AIs: unattended AI-vs-AI to game over at high speed; replay, logs, `result.json` | this container | this container | no | none | none | [`examples/match`](examples/match) |
| `host` / `join` | AI builds in separate containers playing each other, or LAN play with no lobby | the `host` container | each AI's named container | no | between the containers | none | [`examples/lan`](examples/lan) |

The table leaves out two cases:
- **Normal play and local skirmish** happen on a player's PC with Chobby. With
  normal play, bots run on the PC of whoever added them.
- **The dedicated server build** (`engine-dedicated`) cannot run Skirmish AIs,
  so it adds nothing here.

### Choosing between `client` and `spads`

| | `client` | `spads` |
| --- | --- | --- |
| Network | outbound only | inbound UDP: a router forward or a public IP |
| Who runs the room | BAR's autohost; the client is its boss | you |
| Engine | must match BAR's autohosts | yours (players must match it) |
| Room settings | what a boss may set with chat commands | anything SPADS allows, including vote rules and takeover protection |
| If the container restarts | the room goes back to BAR's pool; the client takes another | the room closes and reopens |
| Verified | protocol unit-tested; follow mode logs in on BAR's real server, where it must be told the room (`!room`) | end to end against a fake lobby server; not yet against BAR's real servers |

### How each mode works

- **`client`, follow mode** ([`scripts/lobby_client.py`](scripts/lobby_client.py))
  1. Logs in as Chobby does and waits. When a user in `BARHOST_FOLLOW` is in a
     room, joins it as a spectator and adds `BARHOST_BOT_COUNT` bots it owns.
  2. That user stays boss and starts the game, and can PM the client
     `!bots <n> [AI]`, `!join [password]` (for a passworded room), `!leave` or
     `!status`.
     BAR's server only tells a client who joins *other* rooms if it is a lobby
     the server lists (Chobby, skylobby, modlobby), so on BAR's server the
     client usually cannot see which room the user is in. The user then names
     it: `!room <room> [password]`, where `<room>` is the room's number, its
     host (the `Host[..][..]` autohost, or a player hosting it), a player the
     client can see in it, or its title, whole or a unique part. Once inside,
     the client sees the room's members, and leaves when the user does.
     BAR's server accepts `ADDBOT` only from players, so the client turns
     player to add its bots and then spectates again; the bots stay.
  3. When the game starts, runs `spring-headless` as a spectator client, which
     runs the bots. When the user leaves the room, removes them and leaves.
- **`client`, room mode**
  1. Logs in and takes a **public** room (an empty `Host[XXn][k]` autohost) or
     a **private** one (PMs the region manager `Host[XXn]` `!privatehost`, as
     Chobby's "passworded private battle" does, and joins with the password
     it answers). A `BARHOST_FOLLOW` user switches with `!public` /
     `!private` by PM; the choice is kept on the data volume across restarts.
  2. Joins as a player and checks the room is free: BAR's server shows a
     room's members only to its members, so the client looks from inside and
     leaves at once, untouched, if anyone or any other bot is there. It
     never sets up a room it was not made boss of. Then it makes itself
     boss, sends the room setup commands
     (`BARHOST_ROOM_SETUP`, e.g. `!bSet experimentallegionfaction 1`) and
     adds bots it owns; in a private room it PMs the password. It is a
     headless host and never plays a game: once boss it spectates, and takes
     a player seat again for the moment it adds bots (BAR's server takes
     `ADDBOT` only from players). With `BARHOST_BOOST_BELOW`, it also sits as
     a player while fewer humans play, so the battle list shows the room
     busier, and spectates again from `BARHOST_BOOST_UNTIL` humans or a full
     room (in between it keeps its seat). Once everyone is ready, or a start
     is called, it spectates at once, so its engine never joins as a player:
     a player's lag slows the whole game; a spectator's does not
     (`GameServer.cpp:905`).
  3. While boss, gives boss to `BARHOST_FOLLOW` users as they join, and ends
     (`!ev`) any `boss` or `unboss` vote called by an untrusted player
     (someone who won boss by vote could unboss the others and take the
     room), and from anyone a vote to unboss the client, everyone, or a
     trusted user (`BARHOST_FOLLOW`, `BARHOST_AUTO_BOSS`). A boss's direct
     `!boss <name>` is not a vote and is left alone. An untrusted player's
     vote to change the start boxes the client set is ended too: `addBox`,
     `clearBox`, `loadBoxes`, `split`, `preset`, `bPreset`, and the start
     settings (`mapmetadata_start*`, `startPosType`, by `bSet` or alone). A vote to unboss the client or everyone also gets "Shame on
     you, <caller>!". `BARHOST_AUTO_BOSS` users are also made boss as they
     join, but they cannot PM it commands.
     With `BARHOST_PROMOTE_EVERY`, it PMs the autohost `!promote` while a
     public room has fewer humans than bots (with no bots, a free seat): once
     the room is set up, as soon as a game is over, and again as soon as each
     cooldown ends (it reads the autohost's "Please wait ..." answer; every
     answer it gets by PM is logged).
     An AI from `BARHOST_AI_ARCHIVES` on a moving tag (`@latest`, `@newest`)
     is updated between games: checked after every game and every
     `BARHOST_AI_UPDATE_EVERY` seconds, installed only when no game runs, and
     no start hint is given until it is done.
     Those users pick the bots' AI with `!ai <name>` from
     `BARHOST_AI_CHANNELS` (`!ai` alone lists them): the game's own BARb, or
     a GitHub release such as SMRTBARb test or prod. Each channel's release
     is kept under `BARHOST_AI_RELEASES_DIR` on the data volume, so it
     survives restarts; every channel is fetched at start and checked between
     games like the moving tags above, and the engine's `AI/Skirmish/<AI>` is
     pointed at the chosen release (a symlink) only between games. The choice
     is kept across restarts. `!ai <name> <tag>` holds a release channel to
     that exact release tag (`!ai smrt-test v1.1150-test`), kept across
     restarts too, until `!ai <name>` has it follow its newest again; a tag
     that cannot be downloaded is said and dropped. Swapping releases of one AI (test and prod are
     both `SMRTBARb/stable`) leaves the bots alone; another AI replaces them,
     as a player (after the game if one is running). Each
     player who joins gets `BARHOST_WELCOME` privately in the room chat,
     with the AI release in use added ("Current AI: SMRTBARb version v1.1153-prod.").
     During a game, their `!stop` resigns the bots: the client's engine,
     which hosts them, leaves the game, so BAR wipes out the bots' ally team
     (`game_end.lua`: an AI host's AIs count as uncontrolled once it is gone)
     and the game ends. If its engine has already left a game that goes on,
     it asks the players to vote `!cv stop` instead (only players may).
     With `BARHOST_FRIEND_JOINERS=1`, each player who joins the room gets one
     friend request (once per account, kept across restarts).
     After every map change (its rotation's or a boss's), sets the map's start
     boxes from `BARHOST_START_BOXES_FILE` with `!bSet
     mapmetadata_startbox_override`, or the map's defaults (`0`) for a map
     without them.
     Each game is measured from the container's own cgroup (CPU cores,
     throttling, memory; `scripts/cgroup_perf.py`, a few file reads a
     second): one `perf` line in the log, a row in
     `$BARHOST_OUT_DIR/client/perf-summary.csv`, per-second samples in the
     game's `perf.csv`. By PM, `!games [count]` lists recent games newest
     first (the one running, those ended, then the archive's index), and
     `!perf [number]` reports one: how far behind the server the engine
     was, sim speed, CPU (the main thread apart), memory and pressure,
     live for the game running. `!profiler on|off|default` and `!widgets on|off|default` switch the engine profiler and BAR's own
     widgets (`BARHOST_BAR_WIDGETS`) from the next game (kept across restarts), `!say <text>` says it in the room. `!help` lists every command. `!rehost` ends any game (the bots resign, as
     with `!stop`), leaves the room and takes a new one, never the same.
     Each game also runs in its own engine write dir under
     `BARHOST_WORK_DIR` (fast storage: the engine writes its logs there during
     the game) and is diagnosed while it runs, every `BARHOST_METRICS_EVERY`
     seconds: a widget the client installs in that write dir
     (`scripts/rjm_host_metrics.lua`) logs how far the engine is behind the
     server, its sim speed, ping, profiler shares and unit count, and with
     `BARHOST_ENGINE_PROFILER=1` every engine timer's time per interval
     (`LuaUI/Config/rjm-engine-*.csv`: BAR lets user widgets write only under
     `LuaUI/Config/`; archived as `metrics/engine-*.csv`); the client logs the engine's CPU per thread
     (the main thread runs the simulation), memory, disk and the container's
     CPU / disk / memory pressure (`timeline.csv`, `threads.csv`;
     `scripts/engine_metrics.py`), its own lines (`client.log`) and the
     room's lobby traffic (`lobby-events.log`), and records the room at the
     start (`game.json`: players, bots, script tags, AI). When the engine
     exits, a low-priority process (`scripts/game_archive.py`) moves the
     game into `BARHOST_ARCHIVE_DIR`: one folder per game,
     `YYYY/MM/DD/<start>Z_<source>_<map>_<game id>/`, with the replay,
     metrics, gzipped logs, lobby data and a `manifest.json` (outcome,
     winners, lag and load summaries, files with sha256), plus a line per
     game in `_index/<source>.jsonl`. The layout is the same for every
     source, so one archive can gather games from many clients and pods.
     Lobby chat is kept for audit next to it (`BARHOST_CHAT_DIR`, default
     `_chat/<source>/`): every private message both ways, by day, tagged
     with how the client treated the sender (only authorised users ever get
     a reply), and each room session's chat in its own file.
     When a player mentions "bonus", says how to vote the AIs' bonus (all
     with `!cv setallaibonus <n>`, one with `!cv force %<AI> bonus <n>`); a
     passed bonus vote is applied to every AI and kept for later bot adds.
     When a player rings another, says once how to vote an away player to
     spectator (`!cv spec <name>`), leaving it to the room. Reports its
     engine's exit in its own log, with the engine's last lines if it failed.
     With `BARHOST_MAP_ROTATION`, sends `!map <next>` once boss and after
     every game, keeping its place across restarts, and skips a map the
     autohost will not take or that cannot be downloaded. Any map change is
     downloaded before a game can start.
  4. Never starts a game itself: once every human is ready (and the content
     is downloaded) it says how to vote the start (`!cv start`), at most once
     a minute; with `BARHOST_MATCHED_TEAMS=1`, only once at least as many
     humans play as there are bots. With `BARHOST_RING_EVERY`, once the teams
     are matched it says `!ring` (the autohost rings every unready player)
     after `BARHOST_RING_FIRST` seconds, then that often until all are ready. With `BARHOST_BALANCE=1`, it says
     `!balance` once the room is set up and each time the player team
     fills. With `BARHOST_BONUS_VOTE=1`, it runs the players' AI bonus vote
     itself (`aibonus <0-100>`, then `y` / `n`; SPADS lets no non-boss
     vote it while the client is boss), once per caller every
     `BARHOST_BONUS_VOTE_COOLDOWN` seconds, and says how after every game.
     It never leaves a room or takes a new one while its engine
     is in a game, including after reconnecting mid-game; a `!public` /
     `!private` switch waits for a room with no game in progress, and for two
     minutes after a start is called or everyone is ready.
  5. When the autohost's game starts, runs `spring-headless` as a spectator
     client of it. That engine runs the bots.
- **`spads`** ([`docker/spads/`](docker/spads))
  - Runs [SPADS](https://github.com/Yaribz/SPADS) from pinned commits with BAR's
    map boxes. The configuration is parametrised by environment variables.
  - The Autopilot plugin adds the bot bonus, auto-start, a pre-game force
    start, takeover protection, and a control socket that `spadsctl` talks to.
  - Operation guide: [`docs/lobby.md`](docs/lobby.md).
- **`match`** ([`scripts/run_match.py`](scripts/run_match.py))
  1. Writes the start script and runs the engine as host.
  2. Drives the game over the engine's Autohost channel at `BARHOST_SPEED`,
     and stops the engine at game over.
- **`host` / `join`** ([`scripts/lan_game.py`](scripts/lan_game.py))
  - `host` runs a game described by a JSON spec. It starts the game only when
    every participant has loaded.
  - `join` connects to it and runs the AIs the spec assigns to that
    participant.
  - Guide: [`docs/lan.md`](docs/lan.md).

Utility commands: `fetch-content`, `install-ai`, `list-ais`. Anything else is
executed verbatim (e.g. `bash`).

## One image, not several

Every mode needs the same parts:
- the engine (`spring-headless`, `pr-downloader`);
- the AI installer;
- the shared library ([`scripts/barcommon.py`](scripts/barcommon.py)).

Only `spads` adds SPADS (Perl), which is small. Splitting images would
duplicate the engine layer and multiply what has to be kept on one engine
version.

Variants are tags of the same image:
- **Another engine:** for example a fixed older engine for reproducible
  simulations. Build `docker/runtime/Dockerfile` with other `ENGINE_VERSION` /
  `ENGINE_SHA256` build arguments. Releases are tagged `recoil-<version>`.
- **Custom AIs:** never baked in. They are installed at start from a release or
  a mounted folder, so a new AI build needs no image rebuild.

## Shared code

| Module | Used by | What it holds |
| --- | --- | --- |
| `scripts/barcommon.py` | every mode, and the e2e fake lobby | settings (`env`, `parse_kv`), mod-option defaults, the start-script writer (`Tdf`), the client start script, the engine (`Engine`, `engine_settings`: replays on, worker threads sized to the CPU quota), the engine's Autohost channel, and the lobby protocol's encodings (`password_hash`, `battle_status`) |
| `scripts/install_ai.py` | every mode, at start | custom AIs from folders, archives or GitHub releases; refuses Windows builds |
| `docker/runtime/entrypoint.sh` | every mode | content fetch (`pr-downloader` with BAR's CDN settings), AI install, mode dispatch |

## Container layout

| Path | Windows install equivalent | Content |
| --- | --- | --- |
| `/bar/engine/recoil_2026.07.04/` (`$BAR_ENGINE_DIR`) | `data\engine\recoil_2026.07.04\` | the engine, from the image (`config/engine.pin`) |
| `$BAR_ENGINE_DIR/AI/Skirmish/` | the same | stock AIs (`BARb`, `CircuitAI`, `NullAI`) plus custom ones installed at start |
| `/bar/data` (volume) | `data\` | game and maps, fetched by pr-downloader; persist it (~3.5 GB) |
| `/bar/custom-ai` (volume, optional) | — | custom AI folders, `<Name>/<version>/`, installed at start |
| `/bar/out` (volume) | — | everything a run produces |

### Custom AIs

Use either of these, or both:
- **Mount a folder** at `/bar/custom-ai/<Name>`, with the same layout as a
  Windows install:
  `<Name>/<version>/{AIInfo.lua, AIOptions.lua, libSkirmishAI.so, config/, script/}`.
- **Set `BARHOST_AI_ARCHIVES`** to `.tar.gz`/`.zip` URLs, local archive paths,
  or `github:OWNER/REPO@TAG[#GLOB]`. `TAG` may be `latest` or `newest`, and
  `GLOB` defaults to `*-linux.tar.gz`. For example:
  `github:S3KCentrifugal/CircuitAI@newest`.

A Windows build (`SkirmishAI.dll`) is **refused with an error**: use the AI's
Linux release.

### What lands in `/bar/out`

```
matches/<run-id>/        match: result.json, infolog.txt, engine-console.log, demos*/, script.txt, springsettings.cfg
lan/<run-id>[-name]/     host / join: the same, per participant
spads/                   spads: log/ (SPADS, chat, engine), var/demos*/, games/<time>-<gameId>/
client/games/<time>-<battle>/  client: script.txt, game.json, infolog.txt, engine-console.log, demos/
```

`<run-id>` is `$BARHOST_RUN_ID` (the pod name in Kubernetes) or a timestamp.

## Run it

```bash
# an AI-vs-AI match (settings: examples/match/match.env)
docker compose -f examples/match/docker-compose.yml up

# a headless client in a room on BAR's autohosts (add the password first)
cp examples/client/client.secrets.env.example examples/client/client.secrets.env
docker compose -f examples/client/docker-compose.yml up
```

The first run downloads the game into the `bar-data` volume. Each
`examples/<mode>/*.env` file documents every setting of that mode;
`tests/static/check_examples.py` keeps them in step with what the image reads.

## Using the image from another repository

The package is **private**. For another repository to pull it, choose one:
- grant it access in GitHub: your profile → Packages → `rjm-bar-host` →
  Package settings → **Manage Actions access** → add the repository (role:
  Read). It then logs in with its own `GITHUB_TOKEN`
  (`permissions: packages: read`);
- or give it a PAT with `read:packages`, as `rjm.bar.lobbies` does
  (`GHCR_PULL_TOKEN`). The same token becomes the cluster's pull secret.

Or call the reusable workflow, which runs a simulation on a GitHub runner and
uploads the output directory as an artifact:

```yaml
jobs:
  sim:
    uses: S3KCentrifugal/rjm.bar.host/.github/workflows/simulate.yml@test
    permissions: { contents: read, packages: read }
    with:
      ai_archives: github:S3KCentrifugal/CircuitAI@v1.961-test
      ai_shortname: SMRTBARb
      ai_count: 2
```

**Image tags:**
- `<commit sha>` and `tree-<source tree hash>`: each written once, never
  re-pushed.
- `test` and `prod`: always the newest image published from that branch. This
  is what `rjm.bar.lobbies` resolves to a digest at each deploy.
- `recoil-<engine version>` and `latest`: written from `prod`.

A squash merge from test to prod has the same source tree, so prod reuses the
image test ran instead of rebuilding it.

## Branches and pipeline

`test` is the default branch. Every push to it runs
[`publish.yml`](.github/workflows/publish.yml), which builds the image, runs
the smoke and unit tests, publishes it, and moves the `test` tag. `prod`
changes only by a squash-merged pull request from `test`.

Pull requests run [`ci.yml`](.github/workflows/ci.yml) (lint, example checks,
build, smoke test, unit tests). The lobby end-to-end test is
[`e2e.yml`](.github/workflows/e2e.yml). Publishing needs no repository
secrets.

This repository deploys nothing. Cluster deployments, their settings, accounts
and pipeline are in `rjm.bar.lobbies`.

## Tests

All tests run offline. Details: [`docs/testing.md`](docs/testing.md).

| Command | What it proves |
| --- | --- |
| `scripts/smoke_test.sh <image>` | the engine lists its AIs; AI install from a release and a folder; Windows builds refused; the SPADS configuration renders |
| `tests/run_unit.sh <image>` | unit tests for every mode (match scripts, LAN specs, the lobby client protocol, SPADS scripts, the Autopilot guard), plus the example checks |
| `tests/lan/run.sh <image>` | three containers: SMRTBARb against BARb over the network, 150 s of play |
| `tests/e2e/run.sh <image>` | real SPADS, engine and bots, with a real headless human client, against a fake lobby server |

## Layout

| Path | Contents |
| --- | --- |
| `docker/runtime/Dockerfile`, `entrypoint.sh` | engine release (sha256-verified) and SPADS (pinned) into one runtime image; mode dispatch |
| `docker/spads/` | `spads` mode: configuration, launcher, Autopilot plugin, `spadsctl`, per-game archiver, readiness probe |
| `scripts/barcommon.py` | shared library (see above) |
| `scripts/lobby_client.py`, `run_match.py`, `lan_game.py` | the `client`, `match` and `host`/`join` modes |
| `scripts/install_ai.py` | custom AI installer |
| `scripts/smoke_test.sh`, `demo_stats.py`, `measure_match.py` | pre-publish checks, replay statistics, the resource measurements |
| `examples/<mode>/` | per mode: `<mode>.env` (every setting), secrets template, `docker-compose.yml` |
| `tests/` | unit tests, example checks, the LAN and lobby end-to-end tests |
| `config/engine.pin` | engine release version and checksum |
| `.github/workflows/` | `publish`, `ci`, `e2e`, `simulate` (reusable) |
