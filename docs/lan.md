# LAN games: direct-connect multiplayer between containers

Several `rjm-bar-host` containers can play **one game together** with no lobby
server and no internet during the game. One container hosts, the others join
it over UDP, and any of them can run Skirmish AIs in its own process. This is
the engine's own networking, unmodified; nothing in the game, the engine or
any official repository is changed.

Typical uses:
- **AI build A against AI build B, from separate containers.** Each container
  installs its own AI release, so two builds that share a name (e.g. two
  SMRTBARb versions) can meet in one game, which one engine cannot load.
- **Humans and AIs on a LAN.** Players run the BAR client with a direct-connect
  start script; bots run in a container.
- **Spreading AI load** across machines.

## How it works

```text
 container "host"                    container "box-a"            container "box-b"
 ┌─────────────────────────┐  UDP   ┌──────────────────────┐     ┌──────────────────────┐
 │ lan_game.py host        │◀──────│ lan_game.py join     │     │ lan_game.py join     │
 │  spring-headless (host) │◀──────┼──────────────────────┼─────│  spring-headless     │
 │  supervisor on loopback │  8452  │  spring-headless     │     │   + BARb (team 1)    │
 └─────────────────────────┘        │   + SMRTBARb (team 0)│     └──────────────────────┘
                                    └──────────────────────┘
```

1. **The host** writes the start script from the game spec and starts
   `spring-headless` listening on UDP `port`.
2. **Each joiner** starts `spring-headless` with a short script (host address,
   its name and password) and loads the game.
3. **The host's supervisor** watches the engine's Autohost channel. It waits
   until *every* declared participant has finished loading, then waits
   `forceStartDelay` and starts the game. It never starts early, because BAR
   treats the team of someone still loading as abandoned.
4. **The game** runs in lockstep on every engine. Each AI runs only in the
   engine the spec assigns it to, and its orders reach everyone else.
5. **The end** comes at game over, at `maxGameSeconds` of play, or at `timeout`.
   The host stops the server and the joiners' engines exit. Each container
   writes `result.json`, `infolog.txt`, `engine-console.log` and the replays
   under `/bar/out/lan/<run-id>[-<name>]/`.

## Running it

**The same image for every participant**, which guarantees one engine build.
Game content is downloaded once, the only step that needs internet, into a
shared volume.

```bash
docker compose -f examples/lan/docker-compose.yml up --abort-on-container-exit
```

[`examples/lan/docker-compose.yml`](../examples/lan/docker-compose.yml) and
[`examples/lan/ai-vs-ai.json`](../examples/lan/ai-vs-ai.json): SMRTBARb (from
its GitHub release) in `box-a` against stock BARb in `box-b`, hosted by `host`.

By hand:

```bash
docker network create bar-lan
docker run -d --name host  --network bar-lan -v bar-data:/bar/data -v "$PWD/out:/bar/out" \
  -e BARHOST_LAN_SPEC_FILE=/spec.json -v "$PWD/examples/lan/ai-vs-ai.json:/spec.json:ro" \
  ghcr.io/s3kcentrifugal/rjm-bar-host:test host
docker run -d --name box-a --network bar-lan -v bar-data:/bar/data -v "$PWD/out:/bar/out" \
  -e BARHOST_LAN_HOST=host -e BARHOST_LAN_NAME=BoxA -e BARHOST_LAN_PASSWORD=boxa1 \
  -e BARHOST_AI_ARCHIVES=github:S3KCentrifugal/CircuitAI@newest \
  ghcr.io/s3kcentrifugal/rjm-bar-host:test join
docker run -d --name box-b --network bar-lan -v bar-data:/bar/data -v "$PWD/out:/bar/out" \
  -e BARHOST_LAN_HOST=host -e BARHOST_LAN_NAME=BoxB -e BARHOST_LAN_PASSWORD=boxb1 \
  ghcr.io/s3kcentrifugal/rjm-bar-host:test join
```

**Across machines**, publish the host's port (`-p 8452:8452/udp`) and give
joiners the host machine's LAN address as `BARHOST_LAN_HOST`.

## The game spec

JSON, in `BARHOST_LAN_SPEC` or the file named by `BARHOST_LAN_SPEC_FILE`. Only
the host reads it. The full reference is at the top of
[`scripts/lan_game.py`](../scripts/lan_game.py).

```json
{
  "map": "All That Glitters v2.2.3",
  "game": "byar:test",
  "port": 8452,
  "startPosType": 2,
  "modoptions": {"coop": "1"},
  "players": [
    {"name": "Alice", "password": "a1"},
    {"name": "BoxA",  "password": "b1", "spectator": true}
  ],
  "teams": [
    {"allyTeam": 0, "player": "Alice", "side": "Armada"},
    {"allyTeam": 1, "ai": {"shortName": "SMRTBARb", "host": "BoxA"}, "handicap": 50},
    {"allyTeam": 1, "ai": {"shortName": "BARb", "version": "stable", "host": "host"}}
  ]
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `map` | required | versioned map archive name |
| `game` | `byar:test` | rapid tag or game archive name; every participant needs it on its data volume |
| `port` | 8452 | UDP port the host listens on |
| `startPosType` | 2 | 0 fixed, 1 random, 2 choose in game (BAR places anyone who does not choose) |
| `modoptions` | BAR defaults + Legion, draft by skill, co-op | extra `key: value` mod options |
| `speed` | 1 | game speed; every engine must keep up |
| `waitForPlayers` | 600 | seconds for everyone to finish loading, else the game is abandoned |
| `forceStartDelay` | 10 | seconds after everyone has loaded before the start |
| `maxGameSeconds` | 0 | stop after this much play (a benchmark length); 0 = play to game over |
| `timeout` | 7200 | wall-clock budget for the whole run |
| `observerTeam` | `auto` | seat spectators on their own team; `auto` = on when no human plays (below) |
| `players[]` | — | everyone but the host: `name`, `password`, `spectator` (only runs AIs) |
| `teams[]` | — | each is played by a `player`, or by an `ai` with `shortName`, optional `version` and `options`, and `host` (a player's name, or `host`) |

Every AI must be installed in the container that runs it (`BARHOST_AI_ARCHIVES`
or a mounted custom-AI folder — see the README), and nowhere else.

**Joiners** need only `BARHOST_LAN_HOST`, `BARHOST_LAN_NAME` and
`BARHOST_LAN_PASSWORD` (plus `BARHOST_LAN_PORT` if not 8452).

`BARHOST_ENGINE_SETTINGS=InitialNetworkTimeout=300,NetworkTimeout=300` gives
slow machines longer to load. The engine drops a client that has not
connected within 30 s by default.

## The observer team

When the only participants are spectators — an all-AI game — BAR's
`game_end.lua` wipes out the allyteam a spectator sits in within seconds if it
holds two or more AI teams (design §11, risk 1). The fix needs no game change:
the host and every spectator are seated on an **extra, empty team in an
allyteam of its own**, so BAR never evaluates an AI allyteam through them.

Measured: with it, a 4v4 of SMRTBARb ran 18 minutes to a real elimination;
without it, the same game ended at frame 113.

The cost is one idle commander for the observer team. The AIs treat it as an
enemy and eventually destroy it. `observerTeam: auto` enables it only when no
human plays. Humans in the game make BAR evaluate their own allyteams, which
is safe (the lobby e2e test runs exactly that).

## What is verified

- **`tests/lan/run.sh`** runs host, box-a (SMRTBARb) and box-b (BARb) in three
  containers. Both joiners load; the host starts the game only then; it plays
  150 s to the time limit; each container's log shows its own AI running.
  Three engines need ~12 GB, so this runs locally, not on CI's 7 GB runner.
- **`tests/unit/test_lan_game.py`** covers spec validation (players, teams, AI
  hosts, names, passwords, allyteam numbering, observer rules) and both start
  scripts.

Not verified: a human playing through the BAR desktop client against a
container host. The client needs the same direct-connect start script
(`HostIP`, `HostPort`, `MyPlayerName`, `MyPasswd`) handed to its engine.
