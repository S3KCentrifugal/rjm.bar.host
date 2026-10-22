# Tests

Everything here runs offline: no BAR lobby server and no cluster. Only the
first run downloads BAR content (for the end-to-end test) and AI releases
from GitHub.

| Suite | Command | Needs | Time | Runs in CI |
| --- | --- | --- | --- | --- |
| Smoke | `scripts/smoke_test.sh <image>` | the image | ~1 min | every build (`ci`, `publish`) |
| Unit + static | `tests/run_unit.sh <image>` | the image, PyYAML | ~1 min | every build (`ci`, `publish`) |
| LAN end-to-end | `tests/lan/run.sh <image>` | the image, the content volume, ~12 GB | ~5 min | no: three engines exceed CI's 7 GB runner |
| Lobby end-to-end | `tests/e2e/run.sh <image>` | the image, ~2 GB content volume | ~10 min | `e2e` workflow: weekly, on demand, on PRs touching the lobby host |

Build the image first: `docker build -f docker/runtime/Dockerfile -t rjm-bar-host:dev .`

## Smoke test — `scripts/smoke_test.sh`

The gate before any image is published:

1. the engine lists its stock AI;
2. a custom AI installs from a GitHub release and loads;
3. a mounted AI folder is linked and loads;
4. a Windows-only AI is refused with an explanation;
5. SPADS's configuration loads through SPADS's own parser.

## Unit and static tests — `tests/run_unit.sh`

Python `unittest` suites run inside the image against the installed code
(`tests/unit/`):

| File | Covers |
| --- | --- |
| `test_install_ai.py` | mounted folders, tar and zip archives, both archive layouts, Windows refusal, zip and tar path traversal, reporting every failed source, GitHub `latest`/`newest`/tag/glob resolution (mocked), token handling |
| `test_run_match.py` | the observer team (on by default, and off), spectator host of every AI, interleaved and blocked teams, handicap, modoption defaults and removal, start boxes, loopback autohost, config validation, engine config |
| `test_autopilot_guard.py` | the real Autopilot plugin under a stub SPADS API: boss votes refused in every form, the owner and autohost protected (including via aliases and case), room settings unvotable, only `set map` votable, ordinary votes allowed, every policy setting honoured, `commands.conf` makes `!boss` owner-only |
| `test_lan_game.py` | LAN game spec validation (players, teams, AI hosts, names, passwords, allyteams, observer rules) and the host and client start scripts |
| `test_spads_scripts.py` | `run_spads.sh --check` (config loads, prod ports, owner required, owner-name validation, password file, missing credentials), `archive_game.sh` (archiving, hostile arguments), `ready.sh` (the probe follows *this* SPADS process's lobby socket and ignores other processes'), `spadsctl` error handling |

`test_lobby_client.py` drives the `client` mode's protocol state machine
with lobby lines and a fake clock. It covers:
- the login line and password hash;
- refusal on DENIED or an unaccepted agreement;
- room choice: cluster autohost, region, engine version, empty, unlocked, not
  in game;
- boss, setup commands, bots on their ally team with distinct teams and
  cycled sides, then still a player in a public room;
- the public seat (`PublicSeat`): spectating at 8 humans playing, playing
  again below 3 (not at 3 to 7), spectating before `!cv start`, playing again
  after the game;
- `!public` / `!private` (`Privacy`): the choice kept across a restart, a
  private room asked of the region manager with `!privatehost`, joined with
  its password, spectated, the password PM'd; leaving the other kind of room
  between games but not during one or its countdown; after reconnecting
  mid-game, no room taken until the engine's game ends; no manager online, and a silent manager
  timing out then asked again; replies sent during a game;
- flood spacing;
- setup without a boss confirmation;
- the countdown, its cancellation, and no start before content is ready or
  with unsynced players;
- the engine launch with the battle's address and script password;
- the post-game cooldown;
- taking another room when kicked, and leaving on a failed download;
- follow mode with a server that reports every room's joins (`Follow`), and
  with one that does not, as BAR's does for this client (`FollowUnseen`):
  `!status` and `!join` explain it, `!room` by number or host name (with a
  password), members learned from CLIENTBATTLESTATUS on joining, leaving when
  the owner is absent or leaves, a failed join reported, not retried, and the
  bots added as a player on teams no member uses, then spectating again (also
  for a later `!bots`), since BAR's server drops ADDBOT from a spectator;
- `!room` by a player hosting the room, a player seen in it, the whole title
  or a unique part of it, with a password, and explained when a title part is
  ambiguous or the player is online in a room the client cannot see
  (`RoomByName`).

`tests/static/check_examples.py` keeps `examples/` the truthful reference for
every mode. It checks that:
- every documented key is read by the image;
- every setting SPADS or the lobby client reads is documented;
- every compose file runs this image and names env files that exist, with a
  template for each secrets file.

The cluster manifests, and their checks, live in `rjm.bar.lobbies`
(`scripts/check_manifests.py`).

## Lobby end-to-end — `tests/e2e/run.sh`

The whole automated lobby, with nothing faked except the lobby server:

```text
 driver container                                  spads container (system under test)
 ┌───────────────────────────────┐   TCP 8200     ┌──────────────────────────────┐
 │ fake lobby (lobby_e2e.py) ◀───┼───────────────┼──▶ SPADS + Autopilot           │
 │  virtual users: Owner,        │                │     │ launches                │
 │  Human1, Spammer              │   UDP 8452     │     ▼                         │
 │ Human1's spring-headless ─────┼───────────────┼──▶ spring-headless (host)     │
 │  (a real game client)         │                │      + 8 × SMRTBARb           │
 └───────────────────────────────┘                └──────────────────────────────┘
```

The fake lobby implements the part of the Spring lobby protocol SPADS uses,
using the wire format of `SpringLobbyProtocol.pm` (the library SPADS itself
uses). Every line is recorded in `transcript.log`.

The scenario, each step an assertion with a deadline:

1. SPADS logs in with the configured account and password.
2. It opens the room: title, engine 2026.07.04, BAR game, map, 16 slots.
3. Humans-vs-bots mode adds 8 SMRTBARb bots, owned by the autohost.
4. Autopilot sets every bot's bonus to 50%.
5. The owner's `!status`, sent by private message, is answered privately.
6. A spammer flooding the room is kicked.
7. **A troll cannot take the room over.** Its `!boss` and `!cv boss` are
   refused by SPADS (`commands.conf`). Votes to kick the owner and to spec the
   owner (through the `spec` alias) are refused by the guard. A vote to change
   the password is refused. A vote to strip the bots (`set autoAddBotNb 0`)
   is refused, and on that third refusal the troll is kicked.
8. Boss mode given to a user below the trusted level is switched off.
9. Human1 joins and readies up; Autopilot counts down and starts the game.
10. Balance puts every bot on one team and Human1 on the other.
11. SPADS launches its engine; Human1's real engine connects over UDP.
12. Autopilot force-starts the stalled pre-game; the game runs.
13. **The bot team survives 120 s of game time** — the team-collapse check
    (design §11 risk 1) for lobby games.
14. The owner's `!stop` ends the game; the room stays open with its bots.
15. A second game starts and stops the same way.

Then, against the live room, through the control socket:
- `spadsctl status` shows the room open with 8 bots;
- `spadsctl spads map …` changes the map, and the lobby sees it;
- an unknown verb is refused;
- the socket is owner-only (no group or other access) and owned by the container user;
- both games' logs were archived and a replay recorded.

**Two profiles.**
- **Full** (default locally): everything above, including Human1's real
  headless game client and the bot-team survival check. It runs two BAR
  engines at once and needs ~16 GB.
- **Lobby** (`E2E_HUMAN_ENGINE=0`, what CI runs): no second game client. The
  host engine still launches with all 8 bots and is stopped from pre-game.
  Every lobby, guard, balance, start/stop, persistence and control-socket
  check runs; only the game-play and survival steps are skipped.

The GitHub runner for a private repository is 2 vCPUs / 7 GB. Two engines
exhausted it: the host engine was OOM-killed (signal 9), then the runner was
lost. The `e2e` workflow can run the full profile (`full` input) on a larger
runner.

Options: `E2E_AI=BARb E2E_AI_ARCHIVES=` for the stock AI (no GitHub
download), `E2E_SURVIVE_SECONDS`, `E2E_KEEP=1` to leave the containers up,
`E2E_ARTIFACTS` for where logs go. Artifacts: the lobby transcript,
`result.json`, both of Human1's game logs, SPADS's logs, and each game's
archive.

### What it does not cover

- **BAR's real lobby server (Teiserver)** — its TLS, login flags and
  permissions, and how it handles the autohost adding bots.
- **Chobby's own behaviour.**
- **Internet players through the router.**

Those are checked by the first real deploy (`setup.md` §8).

## LAN end-to-end — `tests/lan/run.sh`

Three containers and one direct-connect game, with no lobby server: `host`
(the game server only), `box-a` running SMRTBARb from its GitHub release, and
`box-b` running stock BARb. It passes when:
- both joiners finish loading;
- the host starts the game only then;
- the game is still running at `LAN_PLAY_SECONDS` (default 150), or ended by
  a real game over after that long;
- each joiner's own log shows its AI running as a local AI.

See [`lan.md`](lan.md).
