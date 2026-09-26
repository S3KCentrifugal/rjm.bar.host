# rjm.bar.host

A reusable **Beyond All Reason headless host** container, and its deployment to
the private k3s cluster.

One image, `ghcr.io/s3kcentrifugal/rjm-host`, does two jobs:

| Mode | What it does | Where it runs |
| --- | --- | --- |
| `match` | an unattended AI-vs-AI simulation, played to game over at high speed, leaving a replay, logs and `result.json` | the cluster (a Job), any Docker host, or a GitHub runner via the reusable `simulate` workflow |
| `spads` | a [SPADS](https://github.com/Yaribz/SPADS) autohost on BAR's lobby server that hosts Skirmish AI bots in its own process, so people can play with or against custom AIs | the cluster (a Deployment) |

Both run BAR's **official** Linux engine release (`recoil_2026.07.04`, pinned in
[`config/engine.pin`](config/engine.pin)), so what they host is exactly what
players run. Custom AIs are installed at start, not baked in.

Design: [`docs/autohost-design.md`](docs/autohost-design.md). Agent and
contributor rules: [`AGENTS.md`](AGENTS.md).

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
  ghcr.io/s3kcentrifugal/rjm-host:test match
```

The first run downloads the game (~3.5 GB) into the `bar-data` volume. Every
setting is documented in [`k8s/base/match.env`](k8s/base/match.env). Other
entrypoint commands: `list-ais`, `install-ai`, `fetch-content`, `spads`, or any
shell command.

## Using the image from another repository

The package is **private**. For another repository to pull it:

1. GitHub → your profile → Packages → `rjm-host` → Package settings →
   **Manage Actions access** → add the repository (role: Read).
2. In that repository's workflow, log in to `ghcr.io` with its own
   `GITHUB_TOKEN` (`permissions: packages: read`), then `docker run` as above.

Or call the reusable workflow, which runs a simulation on a GitHub runner and
uploads the whole output directory as an artifact:

```yaml
jobs:
  sim:
    uses: S3KCentrifugal/rjm.bar.host/.github/workflows/simulate.yml@main
    permissions: { contents: read, packages: read }
    with:
      ai_archives: github:S3KCentrifugal/CircuitAI@v1.961-test
      ai_shortname: SMRTBARb
      ai_count: 2
```

A calling repository must also be allowed to use this private repository's
workflows (Settings → Actions → General → Access: "Accessible from repositories
owned by the user"; already set).

Image tags: `<commit sha>` (written once, never re-pushed), `test` and `prod`
(what each environment runs), and from prod `recoil-<engine version>` and
`latest`. Promoting a commit from test to prod retags its tested image rather
than rebuilding it.

## Cluster deployment

Namespace `rjm-host-<env>` on `mcgill-data`, deployed by
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) on push to the
`test` or `prod` branch:

| Object | Purpose |
| --- | --- |
| Job `rjm-host-match` | one simulation per deploy; the deploy waits for it and fails if it fails |
| Deployment `rjm-host-spads` | the lobby autohost; held at 0 replicas until `rjm-host-secrets` exists |
| PVCs `rjm-host-{data,out,ai}-pvc` | content, output, custom AIs, under `/mnt/data2/k8s-volumes/rjm-host/<env>/` |

**Loading a custom AI folder onto the cluster**: `scripts/upload_ai.sh test
path/to/AI/Skirmish/SMRTBARb` copies it onto the custom-AI volume (Linux build
only; a Windows folder is rejected). It is installed at the next match, or the
next SPADS restart.

A deploy never interrupts a game: it refuses while the match Job is running, and
refuses to restart SPADS while a lobby game is in progress (dispatch with
`interrupt_lobby` to override).

### Cluster secrets

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
(the deploy job's container) and this account's private `rjm-host` package (the
cluster's pull secret) — a classic PAT with `read:packages` from an account that
can see both.

The WireGuard peer must be one not already in use by another runner: two
tunnels with the same private key and address knock each other off.

### The lobby host (SPADS)

1. Create the lobby credentials once per namespace (never committed) from
   [`k8s/base/secrets.yaml.example`](k8s/base/secrets.yaml.example):
   the whitelisted autohost account, its password, **your** account as owner,
   and the room password. Then redeploy; SPADS starts and joins the lobby.
2. For players outside your network, forward **UDP 8452** (test; 8462 for
   prod) on the router to `mcgill-data`. Without it the room is visible but nobody can join.
3. In the room (or by private message to the autohost), as owner:
   - `!addBot Bot1 0 SMRTBARb` — add a server-side bot (`SPADS_ALLOWED_AIS`
     lists which AIs are allowed)
   - `!force %Bot1 bonus 50` — +50% resource bonus; `!force %Bot1 team 1` — move it
   - `!forceStart` — start (SPADS never auto-starts a room with bots in it)

Room settings — name, map, allowed AIs, AI releases — are in
[`k8s/base/spads.env`](k8s/base/spads.env). Every game's replay, infolog and
start script land under `spads/` on the output volume.

## Layout

| Path | Contents |
| --- | --- |
| `docker/runtime/Dockerfile` | engine release → runtime image (+ SPADS) |
| `docker/runtime/entrypoint.sh` | `match`, `spads`, `install-ai`, `list-ais`, `fetch-content` |
| `docker/spads/` | SPADS configuration templates and launcher |
| `scripts/run_match.py` | writes the game script, drives the match over the Autohost Protocol, exits at game over |
| `scripts/install_ai.py` | installs custom AIs into `AI/Skirmish` |
| `scripts/upload_ai.sh` | copies AI folders onto the cluster's custom-AI volume |
| `scripts/smoke_test.sh` | the checks CI runs before publishing an image |
| `scripts/demo_stats.py` | per-team statistics from a replay |
| `scripts/measure_match.py` | CPU/RSS sampling behind the resource numbers |
| `config/engine.pin` | engine release version and checksum |
| `k8s/base/`, `k8s/overlays/{test,prod}/` | Kustomize manifests |
| `.github/workflows/` | `deploy` (publish + deploy), `ci` (checks), `simulate` (reusable) |
