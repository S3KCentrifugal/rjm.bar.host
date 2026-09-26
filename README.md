# rjm.bar.host

A reusable **Beyond All Reason headless host** container, and its deployment to
the private k3s cluster.

One image, `ghcr.io/s3kcentrifugal/rjm-bar-host`, does two jobs:

| Mode | What it does | Where it runs |
| --- | --- | --- |
| `match` | an unattended AI-vs-AI simulation, played to game over at high speed, leaving a replay, logs and `result.json` | the cluster (a Job), any Docker host, or a GitHub runner via the reusable `simulate` workflow |
| `spads` | a [SPADS](https://github.com/Yaribz/SPADS) autohost on BAR's lobby server that hosts Skirmish AI bots in its own process, so people can play with or against custom AIs | the cluster (a Deployment) |

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
`recoil-<engine version>` and `latest`. A squash merge from test to prod has a
new commit hash but the same source tree, so prod reuses the exact image test
ran instead of rebuilding it.

## Branches

`test` is the default branch: every push to it builds, publishes and deploys to
`bar-test`. `prod` changes only when you squash-merge a pull request from
`test`; nothing merges to it automatically. Pull requests into either branch
run the `ci` checks.

## Cluster deployment

Namespace `bar-<env>` on `mcgill-data`, deployed by
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) on push to the
`test` or `prod` branch:

| Object | Purpose |
| --- | --- |
| Job `rjm-bar-host-match` | one simulation per deploy; the deploy waits for it and fails if it fails |
| Deployment `rjm-bar-host-spads` | the lobby autohost; held at 0 replicas until `rjm-bar-host-secrets` exists |
| PVCs `rjm-bar-host-{data,out,ai}-pvc` | content, output, custom AIs, under `/mnt/data2/k8s-volumes/rjm-bar-host/<env>/` |

**Loading a custom AI folder onto the cluster**: `scripts/upload_ai.sh test
path/to/AI/Skirmish/SMRTBARb` copies it onto the custom-AI volume (Linux build
only; a Windows folder is rejected). It is installed at the next match, or the
next SPADS restart.

A deploy never interrupts a game: it refuses while the match Job is running, and
refuses to restart SPADS while a lobby game is in progress (dispatch with
`interrupt_lobby` to override).

### Cluster secrets

Full provisioning steps for every key and token: [`docs/setup.md`](docs/setup.md).

The image publishes without them; the deploy job is skipped (with a warning)
until they are set on this repository. They are the same values the
`mcgill-software` repositories use:

```bash
gh secret set WG_PRIVATE_KEY       -R S3KCentrifugal/rjm.bar.host
gh secret set WG_CLIENT_ADDRESS    -R S3KCentrifugal/rjm.bar.host
gh secret set WG_SERVER_PUBLIC_KEY -R S3KCentrifugal/rjm.bar.host
gh secret set WG_ENDPOINT          -R S3KCentrifugal/rjm.bar.host
gh secret set WG_ALLOWED_IPS       -R S3KCentrifugal/rjm.bar.host
gh secret set KUBE_CONFIG_DATA     -R S3KCentrifugal/rjm.bar.host   # base64 kubeconfig
gh secret set GHCR_PULL_TOKEN      -R S3KCentrifugal/rjm.bar.host   # PAT: read:packages
```

`GHCR_PULL_TOKEN` must be able to read **both** `ghcr.io/mcgill-software/mcgill-deployer`
(the deploy job's container) and this account's private `rjm-bar-host` package (the
cluster's pull secret) — a classic PAT with `read:packages` from an account that
can see both.

The WireGuard peer must be one not already in use by another runner: two
tunnels with the same private key and address knock each other off.

### The lobby host (SPADS)

A co-op room on BAR's lobby server that runs itself: humans against a team of
8 server-side SMRTBARb bots at +50%. It starts games automatically once
players are ready, stays open between games, kicks spammers, and archives
every game. Full guide: [`docs/lobby.md`](docs/lobby.md).

1. **Credentials** (your second Chobby account): set the repository secrets
   `LOBBY_LOGIN_TEST`, `LOBBY_PASSWORD_TEST`, `LOBBY_OWNER_TEST` (your main
   account) and optionally `LOBBY_ROOM_PASSWORD_TEST`, then push to `test`.
   The deploy writes them into the cluster. See
   [`docs/setup.md`](docs/setup.md) §5.
2. **Router**: forward **UDP 8452** (test; 8462 for prod) to `mcgill-data`.
   Without it the room is visible but nobody outside can join.
3. **Control it** from anywhere with cluster access:
   ```bash
   kubectl exec -n bar-test deploy/rjm-bar-host-spads -c spads -- spadsctl status
   kubectl exec -n bar-test deploy/rjm-bar-host-spads -c spads -- spadsctl spads map Comet Catcher Remake 1.8
   ```
   Or as owner, by private message to the autohost: `!map …`, `!forceStart`,
   `!stop`.

Room settings are in [`k8s/base/spads.env`](k8s/base/spads.env).

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
| `tests/` | unit tests, static manifest checks, the lobby end-to-end test |
| `scripts/run_match.py` | writes the game script, drives the match over the Autohost Protocol, exits at game over |
| `scripts/install_ai.py` | installs custom AIs into `AI/Skirmish` |
| `scripts/upload_ai.sh` | copies AI folders onto the cluster's custom-AI volume |
| `scripts/smoke_test.sh` | the checks CI runs before publishing an image |
| `scripts/demo_stats.py` | per-team statistics from a replay |
| `scripts/measure_match.py` | CPU/RSS sampling behind the resource numbers |
| `config/engine.pin` | engine release version and checksum |
| `k8s/base/`, `k8s/overlays/{test,prod}/` | Kustomize manifests |
| `.github/workflows/` | `deploy` (publish + deploy), `ci` (checks), `simulate` (reusable) |
