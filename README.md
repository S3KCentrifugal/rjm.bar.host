# rjm.bar.host

A reusable **Beyond All Reason headless host** container, built, tested and
published to GHCR by this repository. It runs under Docker or Kubernetes; the
cluster's lobbies are deployed from it by
[`rjm.bar.lobbies`](https://github.com/S3KCentrifugal/rjm.bar.lobbies).

One image, `ghcr.io/s3kcentrifugal/rjm-bar-host`, does four jobs:

| Mode | What it does | Where it runs |
| --- | --- | --- |
| `match` | an unattended AI-vs-AI simulation, played to game over at high speed, leaving a replay, logs and `result.json` | any Docker host, a GitHub runner via the reusable `simulate` workflow, or the cluster (a Job; not deployed yet) |
| `spads` | a [SPADS](https://github.com/Yaribz/SPADS) autohost on BAR's lobby server that hosts Skirmish AI bots in its own process, so people can play with or against custom AIs. Needs inbound UDP | any host that players can reach over UDP |
| `client` | a headless **lobby client** that takes a room on BAR's own autohosts, adds AI bots it owns, and runs them in its own `spring-headless` as a spectator client of each game. Outbound connections only ([`scripts/lobby_client.py`](scripts/lobby_client.py)) | the cluster, deployed by `rjm.bar.lobbies` |
| `host` / `join` | a direct-connect (LAN) game between containers, no lobby and no internet: e.g. two AI builds, each in its own container, playing each other ([`docs/lan.md`](docs/lan.md)) | any Docker hosts that can reach each other over UDP |

Both run BAR's **official** Linux engine release (`recoil_2026.07.04`, pinned in
[`config/engine.pin`](config/engine.pin)), so what they host is exactly what
players run. Custom AIs are installed at start, not baked in.

Design: [`docs/autohost-design.md`](docs/autohost-design.md). Lobby operation:
[`docs/lobby.md`](docs/lobby.md). Tests: [`docs/testing.md`](docs/testing.md). Backend setup —
keys, tokens, cluster identity, router: [`docs/setup.md`](docs/setup.md). Agent
and contributor rules: [`AGENTS.md`](AGENTS.md).

## Container layout

| Path | Windows install equivalent | Content |
| --- | --- | --- |
| `/bar/engine/recoil_2026.07.04/` (`$BAR_ENGINE_DIR`) | `data\engine\recoil_2026.07.04\` | the engine, from the image |
| `$BAR_ENGINE_DIR/AI/Skirmish/` | `data\engine\recoil_2026.07.04\AI\Skirmish\` | stock AIs (`BARb`, `CircuitAI`, `NullAI`) plus custom ones installed at start |
| `/bar/data` (volume) | `data\` (games, maps, pool, packages) | game and maps, fetched by pr-downloader; persist it |
| `/bar/custom-ai` (volume, optional) | — | custom AI folders, each `<Name>/<version>/`, linked into `AI/Skirmish` at start |
| `/bar/out` (volume) | — | **everything a run produces**, see below |

### Custom AIs

Any of these, combined:

- **Mount a folder** at `/bar/custom-ai/<Name>` (or straight onto
  `$BAR_ENGINE_DIR/AI/Skirmish/<Name>`). Same layout as a Windows install:
  `<Name>/<version>/{AIInfo.lua, AIOptions.lua, libSkirmishAI.so, config/, script/}`.
- **`BARHOST_AI_ARCHIVES`**: `.tar.gz`/`.zip` URLs, local archive paths, or
  `github:OWNER/REPO@TAG[#GLOB]` (TAG may be `latest` or `newest`; GLOB defaults
  to `*-linux.tar.gz`). Example: `github:S3KCentrifugal/CircuitAI@newest`.

A folder copied from a Windows install has `SkirmishAI.dll`, which Linux cannot
load. It is **refused with an error** rather than silently skipped: use the
AI's `-linux` build (CircuitAI's release workflow publishes one per release).

### What lands in `/bar/out`

```
matches/<run-id>/
  result.json            winners, exit reason, engine + AI build, timings
  infolog.txt            the engine's log, including every Skirmish AI line
  engine-console.log     engine stdout/stderr, including what precedes infolog
  demos-server/*.sdfz    the server replay (open it in BAR to watch)
  demos/*.sdfz           the host client's replay
  script.txt             exactly what was played
  springsettings.cfg     exactly how the engine was configured
spads/
  log/spads.log          SPADS log; log/chat/ holds chat logs
  log/spring-headless.log  engine console of every lobby game
  var/demos*/*.sdfz      every lobby game's replay
  games/<time>-<gameId>/ per game: infolog.txt, startscript.txt, game.json, replay.sdfz link
```

`<run-id>` is `$BARHOST_RUN_ID` (the pod name in Kubernetes) or a timestamp.

## Run it locally

```bash
docker run --rm \
  -v bar-data:/bar/data -v "$PWD/out:/bar/out" \
  -e BARHOST_AI_ARCHIVES=github:S3KCentrifugal/CircuitAI@newest \
  -e BARHOST_AI_SHORTNAME=SMRTBARb -e BARHOST_AI_COUNT=2 \
  -e "BARHOST_MAP=All That Glitters v2.2.3" \
  ghcr.io/s3kcentrifugal/rjm-bar-host:test match
```

The first run downloads the game (~3.5 GB) into the `bar-data` volume. Every
setting is documented in [`k8s/base/match.env`](k8s/base/match.env). Other
entrypoint commands: `list-ais`, `install-ai`, `fetch-content`, `spads`, or any
shell command.

## Using the image from another repository

The package is **private**. For another repository to pull it:

1. GitHub → your profile → Packages → `rjm-bar-host` → Package settings →
   **Manage Actions access** → add the repository (role: Read).
2. In that repository's workflow, log in to `ghcr.io` with its own
   `GITHUB_TOKEN` (`permissions: packages: read`), then `docker run` as above.

Or call the reusable workflow, which runs a simulation on a GitHub runner and
uploads the whole output directory as an artifact:

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

A calling repository must also be allowed to use this private repository's
workflows (Settings → Actions → General → Access: "Accessible from repositories
owned by the user"; already set).

Image tags: `<commit sha>` and `tree-<source tree hash>` (each written once,
never re-pushed), `test` and `prod` (what each environment runs), and from prod
`recoil-<engine version>` and `latest`. `test` and `prod` always name the
newest image published from that branch; `rjm.bar.lobbies` resolves them to a
digest at each deploy. A squash merge from test to prod has a
new commit hash but the same source tree, so prod reuses the exact image test
ran instead of rebuilding it.

## Branches

`test` is the default branch: every push to it builds, tests and publishes the
image ([`.github/workflows/publish.yml`](.github/workflows/publish.yml)) and
moves the `test` tag. `prod` changes only when you squash-merge a pull request
from `test`; nothing merges to it automatically. Pull requests into either
branch run the `ci` checks. Publishing needs no repository secrets: it uses the
workflow's own `GITHUB_TOKEN`.

## Cluster deployment

This repository no longer deploys anything. The lobby is deployed from
[`rjm.bar.lobbies`](https://github.com/S3KCentrifugal/rjm.bar.lobbies)
(namespace `bar-lobbies-<env>`) in `client` mode, which needs no ports or
router forward. That repository owns the lobby's settings, account and
pipeline. `k8s/` here is kept as the reference for the match Job and the SPADS
Deployment, still covered by the static checks, but no pipeline applies it.
The AI-vs-AI match Job will be deployed in a follow-up.

For that repository's workflows to read this private package, its
`GHCR_PULL_TOKEN` must be able to read `rjm-bar-host` (or grant it Actions
access: [`docs/setup.md`](docs/setup.md) §7).

## Tests

`scripts/smoke_test.sh`, `tests/run_unit.sh` and `tests/e2e/run.sh`. All
offline. The end-to-end test runs real SPADS, a real engine and real
SMRTBARb, with a real headless human client, against a fake lobby server. See
[`docs/testing.md`](docs/testing.md).

## Layout

| Path | Contents |
| --- | --- |
| `docker/runtime/Dockerfile` | engine release → runtime image (+ SPADS) |
| `docker/runtime/entrypoint.sh` | `match`, `spads`, `install-ai`, `list-ais`, `fetch-content` |
| `docker/spads/` | SPADS configuration, launcher, Autopilot plugin, `spadsctl`, per-game archiver, readiness probe |
| `tests/` | unit tests, static manifest checks, the lobby and LAN end-to-end tests |
| `examples/lan/` | a docker compose LAN game: SMRTBARb against BARb from separate containers |
| `scripts/lan_game.py` | LAN games: host a spec'd game or join one |
| `scripts/lobby_client.py` | `client` mode: headless lobby client on BAR's own autohosts, runs AI bots |
| `scripts/run_match.py` | writes the game script, drives the match over the Autohost Protocol, exits at game over |
| `scripts/install_ai.py` | installs custom AIs into `AI/Skirmish` |
| `scripts/upload_ai.sh` | copies AI folders onto the cluster's custom-AI volume |
| `scripts/smoke_test.sh` | the checks CI runs before publishing an image |
| `scripts/demo_stats.py` | per-team statistics from a replay |
| `scripts/measure_match.py` | CPU/RSS sampling behind the resource numbers |
| `config/engine.pin` | engine release version and checksum |
| `k8s/base/`, `k8s/overlays/{test,prod}/` | Kustomize manifests (reference; not applied by any pipeline) |
| `.github/workflows/` | `publish` (build, test, publish), `ci` (checks), `e2e` (lobby end-to-end), `simulate` (reusable) |
