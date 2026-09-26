# BAR Headless Host — Design

A reusable, containerised Beyond All Reason headless host, published to GHCR
and deployed to the private k3s cluster by GitHub Actions.

| Mode | Purpose | State |
| --- | --- | --- |
| **`match`** | play an unattended AI-vs-AI simulation to game over and report it, so an AI change can be built, played and scored without touching a machine | built; verified locally end to end |
| **`spads`** | a SPADS autohost on BAR's lobby server that hosts Skirmish AI bots in its own process, for people to play with or against custom AIs | built; verified locally up to the lobby login |

Status: rebuilt 2026-09-26 around BAR's official engine release and runtime AI
installation. Neither mode has yet run on the cluster: the deploy job waits on
the cluster secrets (README "Cluster secrets").

Verified against the official release `recoil_2026.07.04` (RecoilEngine tag
`2026.07.04` = `de69361239`), RecoilEngine `92efda5e60` and Beyond All Reason
`1d267c20d1` sources (both 2026-09-13), SPADS 0.13.55 (`Yaribz/SPADS@4bbb2b8`)
and `spads_config_bar@ca95fa1`.

---

## Contents

- [1. Scope](#1-scope)
- [2. Engine: headless, not dedicated](#2-engine-headless-not-dedicated)
- [3. Engine release and custom AIs](#3-engine-release-and-custom-ais)
- [4. Running a match](#4-running-a-match)
- [5. Container design](#5-container-design)
- [6. Content](#6-content)
- [7. Kubernetes design](#7-kubernetes-design)
- [8. CI/CD pipeline](#8-cicd-pipeline)
- [9. Deviations from the cluster standard](#9-deviations-from-the-cluster-standard)
- [10. The lobby host (SPADS)](#10-the-lobby-host-spads)
- [11. Risks and open questions](#11-risks-and-open-questions)

---

## 1. Scope

**In scope.** One image that other repositories can pull to run a BAR
simulation or host a lobby room, with any custom Skirmish AI; its deployment to
`mcgill-data` in namespace `bar-<env>`; and the pipelines that build,
publish, smoke-test and deploy it.

**Out of scope, permanently.** Modifying the BAR game archive, which changes its
checksum and breaks compatibility with stock clients. Operating lobby
infrastructure: SPADS is a *client* of BAR's lobby server.

**Project name.** `rjm-host`. Namespaces `bar-test`, `bar-prod`.
Image `ghcr.io/s3kcentrifugal/rjm-host` (private).

---

## 2. Engine: headless, not dedicated

**Still the load-bearing decision.** Recoil ships two non-graphical builds:

| Target | Simulation | Can host Skirmish AI | Cost |
| --- | --- | --- | --- |
| `spring-dedicated` | **no** | **no** | low — a net relay |
| `spring-headless` | yes | **yes** | high — full sim |

- `rts/builds/dedicated/CMakeLists.txt` references no `ExternalAI`,
  `SkirmishAI` or `AIInterface` source; its `main.cpp:225` constructs
  `CGameServer` and nothing else.
- A Skirmish AI always executes inside the process of the player hosting it —
  `GameSetup.cpp:47-48` refuses an `[AIn]` section without `Host`.

So both modes run `spring-headless`: the match runner as a spectator that hosts
every AI, and SPADS with `springServerType:headless`, which puts the autohost
into the game as a player so bots added with `!addBot` run in its process
(`spads.pl:5710-5715`, `SpringLobbyInterface.pm:513`).

---

## 3. Engine release and custom AIs

**Decision: ship BAR's official Linux engine release, checksum-verified, and
install custom AIs at container start.** This replaces the earlier design of
compiling engine and AI from one source tree.

Why the official release:

1. **A lobby host must match its players.** People joining a room run the
   engine the BAR launcher installed (`data/engine/recoil_2026.07.04` today). A
   self-compiled binary from the same tag is not guaranteed to be sync-identical,
   and a desync ends the game for everyone.
2. **Simulations should measure what people play.** Same argument, weaker form.
3. **Build time.** A source build is a long cold compile; downloading and
   verifying a 33 MB archive takes seconds. The image now builds in about a
   minute.
4. **The AI already has a build pipeline.** `S3KCentrifugal/CircuitAI`'s release
   workflow compiles SMRTBARb for Windows and Linux with Recoil's own docker
   harness and publishes `SMRTBARb-v<version>-<channel>-linux.tar.gz`, laid out
   to unpack straight into `engine/<version>/AI/Skirmish/`. Rebuilding it here
   would duplicate that.

The release's binaries are static apart from libc (checked with `ldd`:
`libdl`, `libm`, `libpthread`, `librt`, `libc` only), so the runtime needs no
engine libraries at all.

### 3.1 Custom AIs

`scripts/install_ai.py` runs before every match and before SPADS starts. It puts
AIs into `$BAR_ENGINE_DIR/AI/Skirmish/` — the same place a Windows install
keeps them — from any combination of a mounted folder (`/bar/custom-ai/<Name>/`,
linked), archive URLs, local archives, and `github:OWNER/REPO@TAG[#GLOB]`
release assets (`latest`, or `newest` to include pre-releases). A custom AI
named like a stock one replaces it.

**A Windows build is refused, not skipped.** A folder copied from a Windows
install carries `SkirmishAI.dll`; the Linux engine ignores it silently and the
bot simply never appears. The installer requires `libSkirmishAI.so` in every
version directory and fails the container with an explanation otherwise.

### 3.2 The interface pairing

CircuitAI's README records the failure mode: a `libSkirmishAI.so` incompatible
with the `AI/Interfaces/C/0.1/libAIInterface.so` beside it is dead at match
start. The engine release provides the interface, so an AI must be built against
headers compatible with that engine.

SMRTBARb v1.961-test is built against RecoilEngine `92efda5e60` (its
`.github/recoil-engine.ref`), which is *newer* than `2026.07.04`. Measured on
2026-09-26: it loads, is listed by `--list-skirmish-ais`, and played a full
10-minute 1v1 on the `2026.07.04` release. The C interface is stable across that
range; keep the AI's engine ref and `config/engine.pin` close, and treat an
engine bump as needing a matching AI release.

---
## 4. Running a match

### 4.1 The script

`scripts/run_match.py` generates a GameSetup script. Every key is taken from
the parser rather than from examples found elsewhere:

| Section | Keys | Source |
| --- | --- | --- |
| `GAME` | `IsHost`, `HostIP`, `HostPort`, `MyPlayerName`, `AutohostIP`, `AutohostPort` | `ClientSetup.cpp:49-77` |
| `GAME` | `GameType`, `MapName`, `StartPosType` | `GameSetup.cpp:621,622,646` |
| `PLAYERn` | `Name`, `Spectator`, `Team` | `PlayerBase.cpp:31` |
| `AIn` | `Team`, `Host`, `ShortName`, `Version`, `Name`, `[OPTIONS]` | `GameSetup.cpp` LoadSkirmishAIs |
| `TEAMn` | `TeamLeader`, `AllyTeam`, `RGBColor`, `Side`, `Handicap` | `TeamBase.cpp:55` |
| `ALLYTEAMn` | `NumAllies`, `StartRect*` | `AllyTeam.cpp:23` |

Keys are case-insensitive: `TdfParser` lowercases everything it looks up.

**One spectator is mandatory.** `PLAYER0` is a non-playing spectator because
every `[AIn]` must name a `Host` player. It fields no units and cannot win.

**`GameType` accepts a rapid tag directly** — `GameSetup.cpp:668` runs the value
through `GetRapidPackageFromTag`, so `byar:test` works without pre-resolving it.

### 4.2 Driving the match

The engine talks to an autohost over **UDP on loopback**
(`AutohostInterface.h:9-12`; `AutohostIP` defaults to `127.0.0.1`,
`GameServer.cpp:78`). The supervisor binds that port and speaks the protocol
directly; SPADS is not involved in this mode.

Events received (`AutohostInterface.cpp:36-130`):

| Code | Event | Used for |
| --- | --- | --- |
| 0 | `SERVER_STARTED` | the cue to configure and start |
| 2 | `SERVER_STARTPLAYING` | the match is live; carries the demo path |
| 3 | `SERVER_GAMEOVER` | winners; the cue to shut down |
| 1 | `SERVER_QUIT` | the server is going away |
| 5 | `SERVER_WARNING` | captured into the result |

Commands sent: a datagram beginning `/` becomes a server action
(`GameServer.cpp:861-863`).

| Command | Effect | Source |
| --- | --- | --- |
| `/setmaxspeed N`, `/setminspeed N` | **run the simulation faster than real time** | `:2485`, `:2497` |
| `/forcestart` | begin without waiting on readiness | `:2505` |
| `/kill` | `quitServer = true`, a graceful stop | `:2600` |

`setminspeed` is clamped to `maxUserSpeed` (`:2500`), so the ceiling must be
raised first. The runner sends them in that order.

The speed lever is the single biggest win for iteration: a match that takes 25
minutes in real time completes in a small fraction of that, bounded in practice
by one core, because the simulation is largely single-threaded.

### 4.3 Why a supervisor is required

**The engine does not exit when the game ends.** There is no `QuitOnGameOver`
or equivalent CONFIG anywhere in the tree; `Game.cpp:1874` merely sets
`gameOver = true`. Without something to stop it, a Kubernetes Job would hang
forever after the match finished.

The alternative — a Lua gadget calling `Spring.Quit` — would mean modifying the
game archive, which is ruled out. The autohost socket achieves the same thing
from outside, using a documented interface.

Shutdown ladder: `/kill` on game over → 120s to finalise the replay → on the
wall-clock budget, `/kill` again → 30s → `SIGTERM` → 30s → `SIGKILL`. Both the
timeout and the never-started cases exit non-zero so the Job goes `Failed`
rather than quietly reporting success.

### 4.4 Engine configuration and output

The runner writes a `springsettings.cfg` per run and passes it with `--config`.
Two settings earn their place there:

**`ServerRecordDemos` must be enabled explicitly.** `CONFIG(bool,
ServerRecordDemos).defaultValue(false).dedicatedValue(true)`
(`GameServer.cpp:75`) — only the *dedicated* build defaults it on, so a headless
server records nothing unless told to, and reports an empty demo name over the
autohost socket (`:2316-2318`). Measured: without this, runs finish with
`"demo": ""` and no replay. Note this is a different setting from
`GAME\RecordDemo` in the script (`GameSetup.cpp:628`), which does not control
the server-side recorder.

**`WorkerThreadCount` is sized to the container, not the host.** See
[§7.5](#75-resources) — both the engine and the AI size their thread pools from
the host's CPU topology, which inside a container is the wrong number. The
runner reads the cgroup quota (`cpu.max`, or the v1 `cpu.cfs_quota_us` /
`cpu.cfs_period_us`) and sets the engine's pool from that.

Per run, into `/bar/out/matches/<run-id>/`: `script.txt` (exactly what was
played), `springsettings.cfg` (exactly how the engine was configured),
`result.json` (winners, demo path, exit reason, wall-clock, engine version, the
AI's build stamp, engine warnings), `engine-console.log` (the engine's
stdout/stderr, tee'd by the runner so it outlives the pod), plus the engine's own
`infolog.txt`, `demos-server/` and `demos/`. The full list is in
[§7.3](#73-output-what-is-captured).

### 4.5 Spawning

**`StartPosType` 2 works for AI.** BAR branches on it at
`game_initial_spawn.lua:710-728`: 0 and 1 spawn at the map's positions in fixed
or random order; 2 is choose-in-game, and BAR places every team that did not
choose — every AI team — with `GuessStartSpot` inside its start box at
GameStart (`:676-699`). Measured 2026-09-26 on the official release: with 2,
both SMRTBARb teams of a 1v1 spawned and played 18,455 frames (~10 minutes).
The default is 2 because it is what multiplayer uses; 0 gives identical
placement across runs for benchmarking.

An earlier version of this document said 2 left AI unspawned. The symptom it
described — allyteam 0 eliminated within seconds — is real, but it is
[risk 1](#11-risks-and-open-questions) and has nothing to do with spawning.

**Start boxes are left to BAR** (`BARHOST_START_BOXES=map`): its startbox gadget
uses the map's own layout, falling back to halves chosen from the aspect ratio
(`startbox_utilities.lua:233`). With `StartPosType` other than 2, BAR widens
every box to the whole map (`game_startbox_config.lua:30-40`).

**Teams are interleaved across allyteams** by default: BAR maps alternate start
positions by index, so contiguous blocks (0-3 against 4-7) would seat each team
beside an enemy under `StartPosType` 0.

**AI placement cannot be influenced from the AI side.** BAR vetoes AI-chosen
start positions (`game_initial_spawn.lua:478-479`) and places them itself.

---

## 5. Container design

One image, `ghcr.io/s3kcentrifugal/rjm-host`, from a three-stage build:

```text
engine (official release, sha256-verified) ─┐
spads  (Yaribz repos at pinned commits)     ─┴─▶ runtime (debian:stable-slim)
```

| Path | Windows equivalent | Content |
| --- | --- | --- |
| `/bar/engine/recoil_<ver>/` (`$BAR_ENGINE_DIR`) | `data\engine\recoil_<ver>\` | engine release, minus the graphical `spring` |
| `$BAR_ENGINE_DIR/AI/Skirmish/` | `...\AI\Skirmish\` | stock BARb, CircuitAI, NullAI + custom AIs at start |
| `/bar/data` | `data\` | game and maps (volume) |
| `/bar/custom-ai` | — | custom AI folders (optional volume) |
| `/bar/out` | — | everything produced (volume) |
| `/opt/spads`, `/opt/rjm-host/spads/etc` | — | SPADS and its configuration |

Runtime packages: `python3` (runner, installer), `perl` with `FFI::Platypus`,
`IO::Socket::SSL` and `DBD::SQLite` (SPADS: unitsync over FFI, lobby TLS,
preferences), `tini` as PID 1, `ca-certificates`.

Runs as uid 10001, non-root, no added capabilities. uid 10001 owns the engine's
`AI/Skirmish` so AIs can be installed at start, which is why the root
filesystem cannot be read-only ([§9](#9-deviations-from-the-cluster-standard)).

**Build-time check.** The build runs `spring-headless --list-skirmish-ais` and
fails unless `BARb stable` is listed, matching the row format of
`AILibraryManager::OutputSkirmishAIInfo`:

```text
# [Name]               [Version]            [Interface-name]     [Interface-version]
# BARb                 stable               C                    0.1
```

**Pre-publish smoke test** (`scripts/smoke_test.sh`, run by CI before any push):
stock AI listed; a custom AI from a GitHub release installs and is listed; a
mounted folder is linked and listed; a Windows-only AI is refused with its
explanation; SPADS's configuration loads through SPADS's own parser.

Entrypoint commands: `match` (default), `spads`, `install-ai`, `list-ais`,
`fetch-content`, or any command.

---

## 6. Content

Stock BAR, fetched with the release's `pr-downloader` into the `/bar/data`
volume — not baked into the image (the rapid pool alone is ~3.3 GB).

**pr-downloader needs BAR's launcher settings.** Its defaults point at
springrts.com, which carries neither the `byar` repository nor BAR's maps, and
BAR's CDN answers the rapid *streamer* endpoint with an HTTP error — measured:
`Sdp.cpp:422 ... HTTP response code said error`, `Couldn't download files`. The
installed BAR launcher sets exactly three variables, and the entrypoint sets the
same:

```text
PRD_RAPID_REPO_MASTER=https://repos-cdn.beyondallreason.dev/repos.gz
PRD_RAPID_USE_STREAMER=false
PRD_HTTP_SEARCH_URL=https://files-cdn.beyondallreason.dev/find
```

**Pinning.** `byar:test` tracks BAR's test branch and moves; pin `byar:git:<sha>`
for a benchmark that must stay comparable.

**Maps** are named by their *versioned* archive name — `All That Glitters v2.2.3`,
`Comet Catcher Remake 1.8` — which is what `GAME\MapName` resolves
(`ArchiveScanner.cpp:187-196`, `GameSetup.cpp:679`) and what `--download-map`
expects. The match map is always fetched, whether or not it is in
`BARHOST_MAPS`.

**Discovery.** The runner starts the engine with `--isolation --isolation-dir
/bar/data` and `SPRING_DATADIR=$BAR_ENGINE_DIR`, which LEVEL 3 of
`LocateDataDirs` honours even in isolation (`DataDirLocater.cpp:434-441`). SPADS
cannot rely on unitsync finding the engine's `base/` beside itself — measured:
`Required base file 'base/springcontent.sdz' does not exist` — so its
`springDataDir` lists both `/bar/data` and the engine directory.

---

## 7. Kubernetes design

Kustomize base + `test`/`prod` overlays, manual PVs with `claimRef`,
`mcgill-k8s-pull-2026` pull secret.

```text
k8s/
  base/     rbac.yaml  network-policies.yaml  pvc.yaml  match-job.yaml
            spads-deployment.yaml  match.env  spads.env  secrets.yaml.example
  overlays/{test,prod}/  namespace.yaml  persistent-volumes.yaml  kustomization.yaml
```

### 7.1 Workloads

**`rjm-host-match`, a Job.** One simulation per deploy; `backoffLimit: 0` (a
failed match is a result to read, and a retry would bury its infolog);
`ttlSecondsAfterFinished: 86400`. A Job's pod template is immutable, so the
pipeline deletes and recreates it — and refuses to while one is running.

**`rjm-host-spads`, a Deployment** of one replica, `strategy: Recreate`,
`hostNetwork: true` ([§10](#10-the-lobby-host-spads)). Held at zero replicas by
the pipeline until the `rjm-host-secrets` Secret exists. A readiness probe
(`docker/spads/ready.sh`) marks it ready when *its own* SPADS process holds an
established TCP connection to the lobby — matched by socket inode, because under
`hostNetwork` `/proc/net/tcp` lists every socket on the node, including the
other environment's SPADS;
there is deliberately **no liveness probe**, because a restart ends any game in
progress and SPADS reconnects to the lobby by itself.

Both use an init container that runs `fetch-content`, so the main container
starts with content in place.

### 7.2 Storage

Three volumes on `storageClassName: data2`, host path
`/mnt/data2/k8s-volumes/rjm-host/<env>/<purpose>`, `mcgill-data` affinity,
`Retain`, every PV carrying `claimRef`:

| PVC | Mount | Purpose | Size |
| --- | --- | --- | --- |
| `rjm-host-data-pvc` | `/bar/data` | rapid pool + maps | 40Gi |
| `rjm-host-out-pvc` | `/bar/out` | every log and replay | 50Gi |
| `rjm-host-ai-pvc` | `/bar/custom-ai` (read-only) | custom AI folders | 5Gi |

Both workloads mount all three; they run on the one node, so `ReadWriteOnce`
does not keep them apart. The pipeline creates the host directories and
`chown`s them to 10001.

**Loading a custom AI folder onto the cluster:** `scripts/upload_ai.sh <env>
<folder>...` — both workloads mount the volume read-only, so it starts a
short-lived pod that mounts it read-write, rejects a folder without
`libSkirmishAI.so`, copies, and deletes the pod. Alternatively place it under
`/mnt/data2/k8s-volumes/rjm-host/<env>/ai/` on the node.

### 7.3 Output: what is captured

Everything lands on `rjm-host-out-pvc`:

| Path | Written by |
| --- | --- |
| `matches/<pod>/result.json` | runner: winners, exit reason, engine version, AI build stamp |
| `matches/<pod>/infolog.txt` | engine, including every Skirmish AI log line |
| `matches/<pod>/engine-console.log` | runner: the engine's stdout/stderr, tee'd |
| `matches/<pod>/demos-server/*.sdfz`, `demos/*.sdfz` | engine: server and client replays |
| `matches/<pod>/script.txt`, `springsettings.cfg` | runner: exactly what was played, and how |
| `spads/log/spads.log`, `log/chat/`, `log/spring-headless.log` | SPADS; engine console of every lobby game |
| `spads/var/demos*/*.sdfz` | engine: every lobby game's replay |
| `spads/games/<time>-<gameId>/` | `archive_game.sh` (SPADS `endGameCommand`): that game's `infolog.txt`, `startscript.txt`, `game.json`, a link to its replay |

SPADS overwrites `infolog.txt` and `startscript.txt` every game; the end-game
hook is what keeps them per game. Nothing prunes the volume yet
([risk 7](#11-risks-and-open-questions)).

### 7.4 Networking

The match Job exposes nothing: the autohost channel (53200) and the game server
(53210) are loopback. A default-deny ingress NetworkPolicy covers the namespace.
Egress stays open — pr-downloader, GitHub and the lobby server have no fixed
addresses a policy could name. SPADS's networking is in [§10](#10-the-lobby-host-spads).

### 7.5 Resources

**Measured, not estimated.** `scripts/measure_match.py` reproduces the numbers.

#### Why it has to be measured

Two facts about how this workload uses CPU pull in opposite directions, so
neither "N AI needs N cores" nor "it is single-threaded" is true:

- **Skirmish AI updates are serial.** `CEngineOutHandler::Update` loops over
  every AI in turn (`EngineOutHandler.cpp:85-88`) from inside `CGame::SimFrame`
  (`Game.cpp:1748`). Adding AI multiplies the per-frame AI cost **on one
  thread**, not across cores.
- **CircuitAI's worker pool is process-wide, not per-AI.** It is a `static`
  vector sized once at `clamp(hardware_concurrency() - 1, 2, MAX_JOB_THREADS)`
  with `MAX_JOB_THREADS` of 8 (`Scheduler.cpp:17,21,36`) and shared by every
  instance through `gInstanceCount`. Eight AI do not get eight pools.

#### What a match actually costs

16-core / 24-thread host, BARb from `recoil_2026.07.04`, All That Glitters
v2.2.3, `deathmode=neverend` so the sample is not truncated, ~8 minute window:

| | 8 AI, engine defaults | 16 AI, engine defaults | 8 AI, **requested settings** |
| --- | --- | --- | --- |
| sustained CPU | 2.44 cores | 2.87 cores | **2.82 cores** |
| peak RSS | 4.39 GiB | 6.34 GiB | **6.01 GiB** |
| memory growth | ~9 MiB/min of game time | ~22 MiB/min | ~22 MiB/min |
| process threads | 25–29 | 25–29 | 29 |
| simulated | 106,761 frames ≈ 59 min | 83,406 frames ≈ 46 min | 74,568 frames ≈ 41 min |
| effective speed | ~7.5× real time | ~5.8× real time | ~5.1× real time |

All eight AI were live for the whole of every run: each sampled match ends with
all AI destroyed on the same final frame, at shutdown, never earlier. That
matters because [risk 1](#11-risks-and-open-questions) kills an allyteam three
seconds into a *normal* match — `deathmode=neverend` removes the gadget
responsible, so these figures measure eight working AI rather than four.

"Requested settings" is the lobby configuration the owner asked for:
Legion enabled, co-op on, and every AI on +50% income. It is the one to size
against, and it is materially more expensive than bare defaults — **8 AI with
these settings costs roughly what 16 AI cost without them**: +16% CPU and +37%
memory over the same 8 AI on defaults. That is what a +50% resource bonus buys:
more units built, so more to simulate and more to hold.

Three things follow, and they are not what a naive reading would predict.

**Doubling the AI barely moves CPU.** 8 AI to 16 AI took sustained use from 2.44
to 2.87 cores — an 18% increase for twice the AI. That is the serial-update and
shared-pool structure above showing through: the AI think-time lands on one
thread either way, so what grows is the time per frame, not the core count.
Settings that produce more *units*, by contrast, cost more than adding AI does.

**More CPU does not make matches finish sooner.** Only ~2.4 of 24 available
cores were ever in use, and the engine asked for 100× speed but achieved ~7.5×.
Wall-clock per match is bounded by single-core performance, so the CPU *limit*
exists to avoid throttling, not to buy speed. Sizing the pod at 12 cores would
waste the reservation.

**Memory is the number that matters, and it grows without plateauing.** Across
both runs RSS rose roughly linearly for as long as the match went on: about
10 MiB per minute of game time with 8 AI, 24 MiB with 16. Extrapolating the
16-AI rate, a two-hour game lands near 8 GiB. CPU starvation only makes a match
slower; running out of memory kills the pod and loses the run, so the limit is
sized for a long match rather than a typical one.

#### Sizing

```yaml
requests: { cpu: "2000m", memory: "8Gi" }
limits:   { cpu: "6000m", memory: "16Gi" }
```

The memory request sits above the 6.01 GiB observed peak on purpose: RSS had not
plateaued when the sample ended, and an over-committed node that reclaims from
this pod kills the run outright. Note these are binary GiB, matching what
Kubernetes `Gi` means — `measure_match.py` reports decimal MB.

Burstable rather than Guaranteed, because `mcgill-data` is a single node that
also carries the cluster's eight shared database instances: a batch job should
reserve modestly and burst into whatever headroom exists. The wide memory gap is
deliberate — the request reflects a typical match, the limit covers a long one.

**For an 8v8 with human players** (16 slots, 8 of them AI) the compute is
bounded above by the 16-AI column: the same 16 teams of units, but 8 AI brains
instead of 16. It fits the sizing above. What it *does* change is everything in
[§10](#10-the-lobby-host-spads) — human players mean the lobby,
inbound UDP, `hostNetwork`, and a long-running Deployment instead of a Job.
The resource question is the easy half of that.

**Before the first prod run**, check `kubectl describe node mcgill-data` for
allocatable CPU and memory against the sum of what is already requested. A 6 GiB
request on a node that also runs five PostgreSQL instances, MongoDB, Redis and
Qdrant may simply not schedule, and that is a capacity decision rather than a
manifest one.

#### The container/host thread mismatch

Both thread pools size themselves from the **host's** CPU topology, which inside
a container is the wrong number:

- the engine from `CPUID::GetNumLogicalCores` (`Threading.cpp:348-352`, via
  `ThreadPool.cpp:115-120`) — observed as
  `[ThreadPool::SetThreadCount] wanted=8 ... maximum=24` on the 24-thread host;
- CircuitAI from `hardware_concurrency()` (`Scheduler.cpp:36`).

On a many-core node with a modest CPU limit, the process starts workers for
every host core and then spends its quota being throttled and context-switched.
The runner addresses the engine half by setting `WorkerThreadCount` from the
cgroup quota ([§4.4](#44-engine-configuration-and-output)).

CircuitAI's pool has no such knob — the count is computed in its constructor
with nothing reading configuration. Since that repository is ours, the cheap fix
is a few lines there to honour an environment variable; until then, keep the CPU
limit at or above 8 cores' worth of slack, or accept the throttling.

### 7.5 Security

### 7.6 Security

Non-root (uid 10001), `allowPrivilegeEscalation: false`, all capabilities
dropped, `RuntimeDefault` seccomp, no service-account token. The lobby
credentials live only in the `rjm-host-secrets` Secret, applied by hand and
never committed; SPADS refuses to start with an empty owner login, because
`users.conf` would then grant owner rights to every user.

---

## 8. CI/CD pipeline

Three workflows in `.github/workflows/`:

| Workflow | Trigger | Does |
| --- | --- | --- |
| `ci.yml` | pull requests into `test` or `prod` | shellcheck, Python compile, render both overlays, build the image, smoke test — publishes nothing |
| `deploy.yml` | push to `test`/`prod`, `workflow_dispatch`, `repository_dispatch: ai-updated` | `set-env → build → deploy`: publish the image, deploy, run a match |
| `simulate.yml` | `workflow_call` (reusable), `workflow_dispatch` | run one simulation on a GitHub runner with the image, upload `/bar/out` as an artifact |

**`set-env`** resolves the environment and the commit to deploy. A push deploys
its branch; a `workflow_dispatch` must be run *from* the `test` or `prod` branch
it deploys; a `repository_dispatch` (which GitHub runs on the default branch,
`test`) checks out `test` explicitly, so changing the default branch can never
make an AI release deploy an unreviewed branch. It lowercases the owner for GHCR, reads
`config/engine.pin`, validates any AI override (passed through the environment,
limited to characters inert in the `sed` that uses it), and decides whether a
build is needed: on a push, from everything pushed (`before..sha`, not just the
last commit) touching `docker/`, `scripts/` or the pin, or a `[force-build]`
commit; always on a manual run; never on an AI dispatch, which reruns the match
on the image test already runs.

**`build`**: images are keyed by the commit's **source tree**
(`:tree-<hash>`). Prod is updated by squash-merging `test`, which creates a new
commit hash but, when test's tip is merged whole, the same tree — so prod finds
`:tree-<hash>` already published and retags the exact image test ran instead of
rebuilding it. Otherwise the build job builds into the runner's daemon with the
GHA layer cache, runs `scripts/smoke_test.sh`, and only then pushes
`:tree-<hash>`. It then points `:<sha>` (only if new: commit tags are never
repointed), `:<env>`, and from prod `:recoil-<engine>` and `:latest` at that
image with `buildx imagetools create`.

**`deploy`** runs in `mcgill-deployer` with `--privileged` for WireGuard, and is
**skipped with a warning** while the cluster secrets are missing, so the image
still publishes. Every refusal check runs **before anything is changed**:

1. WireGuard. 2. kubectl. 3. Namespace. 4. Pull secret (private image).
5. Image tag: the commit's, or the deployed one if nothing was built.
6. **Render once**: tag, AI override, SPADS replicas (0 without the Secret), and
   the SPADS template hash — a hash of the SPADS manifest, its ConfigMap source
   and the base and overlay kustomizations, so *any* change to SPADS changes it.
7. **Guards**: refuse while the match Job is running; refuse when SPADS would
   restart (hash or replicas changed) while a `spring-headless` process — a
   lobby game — runs in it. The SPADS guard fails closed: if the pod cannot be
   inspected, a game is assumed. `interrupt_lobby` overrides it.
8. Host directories, owned by 10001. 9. Delete `Released` PVs. 10. Delete the
   finished match Job. 11. Apply the rendered file; wait for SPADS if it runs.
12. Wait for the match (5400 s), print its log, fail the run if it failed.
   The VPN is torn down always.

**Secrets.** The standard seven. `GHCR_PULL_TOKEN` must read both
`mcgill-software/mcgill-deployer` and this account's private package. No
`AI_REPO_TOKEN`: the AI repository is public and its releases are fetched at
runtime.

---

## 9. Deviations from the cluster standard

| Standard | Here | Why |
| --- | --- | --- |
| Image per environment (`<project>-<component>-<env>`) | one image, `rjm-host`, tagged `<sha>`/`<env>` | it is a reusable artifact consumed by other repositories; an image is built once per source tree and promoted by retagging, never rebuilt per environment (convention-deployment §6) |
| Namespace `mcgill-<service>-<env>` | `bar-<env>` | requested by the owner: the namespace names the game it hosts; resource names keep the `rjm-host-` prefix |
| Namespace in `base/` | in each overlay | avoids relying on the namespace transformer renaming a Namespace object |
| Deployment + RollingUpdate | a Job for matches; SPADS uses `Recreate` | a match is a batch; a hostNetwork surge pod would collide on the game port |
| Liveness + readiness probes | Job: none; SPADS: readiness only | a batch has nothing to probe; a liveness restart would end a lobby game |
| Service / Ingress per workload | none | nothing is served over HTTP; SPADS needs raw UDP, which ingress cannot carry |
| `readOnlyRootFilesystem` | not set | custom AIs are installed into the engine's `AI/Skirmish` at start |
| Default-deny egress | ingress only | CDN, GitHub and lobby have no fixed addresses; hostNetwork bypasses policy anyway |
| `rollout status` 120 s | Job wait 5400 s; SPADS 900 s | a cold 3.5 GB content fetch plus a full match; SPADS indexes content at start |
| Pull secret in manifests only | also deploy-job `container.credentials` | the deployer image lives in another account's registry |

---

## 10. The lobby host (SPADS)

**Built.** A private, optionally passworded room on BAR's lobby server, hosted
by the owner's whitelisted secondary account, with SPADS running Skirmish AI
bots in its own `spring-headless`.

### 10.1 How it is put together

SPADS 0.13.55 is installed from pinned commits of `Yaribz/SPADS` and its five
libraries, laid flat in `/opt/spads` exactly as its installer would — not with
`spadsInstaller.pl`, which downloads packages over plain HTTP and needs the
engine at install time. Configuration is `docker/spads/etc/` (four customised
files; the other five are SPADS's stock ones), parametrised with SPADS's own
`%ENV{..}%` / `%(ENV{..}|default)%` macros (`SpadsConf.pm:904-905`), so the
ConfigMap `spads.env` and the Secret feed it directly. `run_spads.sh --check`
loads it through `SpadsConf` without connecting anywhere; CI runs that.

| Setting | Value | Why |
| --- | --- | --- |
| `springServerType` | `headless` | only headless can run bots in the autohost (`spads.pl:7698-7700`) |
| `lobbyHost:lobbyPort`, `lobbyTls` | `server4.beyondallreason.info:8200`, `on` | BAR's current SPADS configs; STARTTLS on the same port |
| `autoHostPort` | 8454, loopback | SPADS binds it to 127.0.0.1 (`SpringAutoHostInterface.pm:395-405`) |
| hosting `port` | 8452 UDP | what players connect to |
| `modName` | `rapid://byar:test` | resolved from the content volume; must be the tag `fetch-content` downloads |
| `allowedLocalAIs` | `SMRTBARb;BARb` | `!addBot` accepts only these; the engine picks the highest installed version |
| `autoBalance`, `autoStart`, `autoLock` | off | manual control by the owner |
| `varDir`, `logDir` | `/bar/out/spads/{var,log}` | everything on the output volume |
| `endGameCommand` | `archive_game.sh` | keeps each game's infolog and start script |
| owner | `SPADS_OWNER_LOBBY_LOGIN`, level 140 | can `!addBot`, `!force`, `!start` by private message |

Adding bots: `!addBot <name> <side> <AI>` (e.g. `!addBot Bot1 0 SMRTBARb`), or
`!addBot <name>` for a name in `SPADS_LOCAL_BOTS`; `!force %Bot1 bonus 50` for a
+50% handicap; `!force %Bot1 team N`. `autoStart` never starts with bots
present, so start with `!start` or `!forceStart`. SPADS preset syntax uses `|`
for alternatives, so an AI's version cannot be pinned in configuration.

**Verified locally (2026-09-26):** SPADS starts, loads unitsync `2026.07.04`,
indexes the game and maps in 23 s, runs in headless mode, binds the autohost
port to loopback, and reaches the lobby-connect step. **Not yet verified:** the
login to BAR's server and hosting a bot game, which need the real account.

### 10.2 What it costs

1. **Inbound UDP.** The cluster's only internet path is the router forward to
   the HAProxy NodePorts (TCP). Players connect to the engine over UDP, which
   ingress cannot carry, so the pod uses `hostNetwork: true` and listens on
   8452/UDP (test) or 8462/UDP (prod) on `mcgill-data` — the environments share
   the node's ports, so prod's overlay moves both SPADS ports (8462 game, 8464
   autohost). **For players on the internet, the router must forward the UDP
   port to `mcgill-data`** — configuration outside this
   repository. Without it the room is visible and unjoinable. The address the
   lobby sees for SPADS's outbound TCP connection must be the same public
   address that forwards the UDP.
2. **`hostNetwork` removes the pod network boundary.** NetworkPolicy does not
   apply and the pod can reach everything on the node, including the shared
   databases. Mitigated by running as non-root with all capabilities dropped,
   and by SPADS binding its control port to loopback; not eliminated.
3. **A long-running Deployment**: `Recreate`, no liveness probe, and a deploy
   guard against live games (§8).
4. **Lobby permissions.** Without a bot flag the lobby caps a room at 8
   players. The owner reports the account is whitelisted to host bot servers;
   whether it carries the bot flag is visible in SPADS's log at login (it warns
   when it does not, `spads.pl:14043-14044`).

---

## 11. Risks and open questions

Ordered by how much they could invalidate the work.

**1. Team games collapse: allyteam 0 is wiped out within ~5 seconds.**
*Open; cause located, not fixed.* Reproduced 2026-09-26 on the official
release. It happens whenever an allyteam holds two or more AI teams, and never
in a 1v1:

| Run | Result |
| --- | --- |
| 1v1 SMRTBARb | full game, 18,455 frames, game over by elimination |
| 2v2 SMRTBARb, 4v4 SMRTBARb, 4v4 stock BARb | allyteam 0 dead by frame 113–193 |
| 4v4 with `draft_mode` removed, or `coop` removed, or blocked layout | no change |

The cause is in BAR's `luarules/gadgets/game_end.lua`:

- `playerQuitIsDead` is true for every game except a 1v1 (`:76`, cleared at
  `:263`), which is why only team games fail.
- In a team game an allyteam is wiped out when none of its teams is
  "controlled" (`:128`). For an engine AI, "controlled" copies whether the
  player hosting it is `active` (`:212`), and here that host is a spectator.
- The check runs only for the allyteam of the player being evaluated
  (`:214-217`). The only player is the spectator host, and it sits on team 0,
  which is why only allyteam 0 dies.

The fix is not in the AI. The likely levers are the host player's standing (not
a spectator, or on its own allyteam) or an upstream change to `game_end.lua`.
The game archive must not be modified here (AGENTS fact 3). Until it is
resolved, the cluster's default match is a 1v1 (`match.env`), so each deploy's
match is a real game. It probably does not affect SPADS lobby games, where
human players are the active hosts of their own teams, but that is unverified.

**2. Team statistics are missing from replays.** `demo_stats.py` finds no team
statistics even in a 1v1 that reached game over by elimination, and reports
`teams 1` for a two-team game — its demo parsing likely predates this engine's
format. The runner's `result.json` (winners, frames, exit reason) is unaffected.

**3. SPADS against the real lobby is unverified** (§10.1): login, TLS
certificate acceptance, `rapid://byar:test` resolution at battle open, and a
bots-only `!forceStart`.

**4. Router UDP forward** for internet players is outside this repository (§10.2).

**5. Resource sizing is from a desktop.** [§7.5](#75-resources). `mcgill-data`
also carries the cluster's databases; check allocatable capacity before prod.
Match and SPADS together request 4 cores and 16 GiB.

**6. Engine/AI coupling.** SMRTBARb is built against a newer engine commit than
the release it runs on (§3.2). It works today; an engine bump needs a matching
AI release, checked by the pipeline's smoke test.

**7. Disk growth.** Replays and logs accumulate on the output volume with no
pruning. Tens of MB per match.

**8. Content drift.** `byar:test` moves; pin `byar:git:<sha>` for comparable
benchmarks. Match Job and SPADS may run `pr-downloader` against the content
volume at the same time.

**9. Shared WireGuard peer.** If the deploy job reuses another repository's
WireGuard key and address, concurrent runs of both knock each other off.

---

## Related

- `../rjm.bar.docs/knowledge/` — game and unit facts
- `../s3k-CircuitAI/doc/release.md` — how SMRTBARb releases are built and laid out
- [`Yaribz/SPADS`](https://github.com/Yaribz/SPADS) — the autohost
- [`spads_config_bar`](https://github.com/beyond-all-reason/spads_config_bar) — BAR's reference SPADS configuration
- [`BAR-Devtools`](https://github.com/beyond-all-reason/BAR-Devtools) — local Teiserver + SPADS + client, for lobby testing
