# BAR Headless Autohost — Design

Design for a containerised Beyond All Reason headless host, deployed to the
private k3s cluster by GitHub Actions.

It has two phases, and they want different things:

| Phase | Purpose | State |
| --- | --- | --- |
| **1. Match harness** | run AI-vs-AI matches on demand so a change to the AI can be built, played and scored without touching a machine | **designed and built** |
| **2. Public lobby autohost** | keep a battle room open on BAR's lobby server for human players | designed, not built, not needed yet |

Phase 1 is the active work. It exists to serve AI development: the loop is
*change the AI → build → play a match → read the result*, and every decision
below is made to keep that loop short. Phase 2 is documented in
[§10](#10-phase-2-the-public-lobby-autohost) because its analysis is still
valid and most of its cost is avoidable until somebody actually wants to play.

Status: phase 1 implemented, not yet run on the cluster. Written 2026-09-20,
reframed 2026-09-21.

Verified against RecoilEngine `92efda5e60` and Beyond All Reason `1d267c20d1`
(both 2026-09-13), the shipped engine release `recoil_2026.07.04`, and the
cluster inventory as of this date.

---

## Contents

- [1. Scope](#1-scope)
- [2. Engine build: headless, not dedicated](#2-engine-build-headless-not-dedicated)
- [3. Build: engine and AI from one tree](#3-build-engine-and-ai-from-one-tree)
- [4. Running a match](#4-running-a-match)
- [5. Container design](#5-container-design)
- [6. Content](#6-content)
- [7. Kubernetes design](#7-kubernetes-design)
- [8. CI/CD pipeline](#8-cicd-pipeline)
- [9. Deviations from the cluster standard](#9-deviations-from-the-cluster-standard)
- [10. Phase 2: the public lobby autohost](#10-phase-2-the-public-lobby-autohost)
- [11. Risks and open questions](#11-risks-and-open-questions)

---

## 1. Scope

**In scope (phase 1).** A container that plays one Beyond All Reason match
between N Skirmish AI instances, start to finish, unattended, and reports what
happened. Triggered by CI when the AI changes. Deployed to `mcgill-data` as a
Kubernetes Job.

**Out of scope (phase 1).** The lobby server, human players, inbound UDP, map
rotation, chat, votes, and SPADS. None of it is needed to play an AI-vs-AI
match, and all of it carries real cost — see [§10](#10-phase-2-the-public-lobby-autohost).

**Out of scope, permanently.** Modifying the BAR game archive, which changes
its checksum and breaks compatibility with stock clients.

**Project name.** `barhost`. Namespaces `barhost-test`, `barhost-prod`.

---

## 2. Engine build: headless, not dedicated

**This is still the load-bearing decision.**

Recoil ships two non-graphical builds. They are not interchangeable:

| Target | Simulation | Can host Skirmish AI | Cost |
| --- | --- | --- | --- |
| `engine-dedicated` | **no** | **no** | low — a net relay |
| `engine-headless` | yes | **yes** | high — full sim |

Evidence, re-verified against the current tree:

- `rts/builds/dedicated/CMakeLists.txt` contains no reference to `ExternalAI`,
  `SkirmishAI` or `AIInterface` anywhere. Its `main.cpp:225` constructs
  `CGameServer` and nothing else.
- `rts/builds/headless/CMakeLists.txt:64` builds `engine-headless` from the
  full engine sources. BAR's release packaging installs that binary as
  `spring-headless`, which is the name present in a real release tree.
- A Skirmish AI always carries a `hostPlayer` and executes inside a full engine
  process — `GameSetup.cpp:47-48` refuses a script whose `[AIn]` section has no
  `Host`.

So: `spring-headless`, and size for a simulation with N AI rather than for a
relay.

---

## 3. Build: engine and AI from one tree

**Decision: build the engine and the Skirmish AI in a single CMake
configuration, in Recoil's own build container.**

Two facts force this:

1. **The AI cannot be built alone.** `s3k-CircuitAI/CMakeLists.txt:12` pulls
   `${CMAKE_SOURCE_DIR}/rts/System/StringUtil.cpp` out of the engine tree. The
   AI is a subdirectory of an engine checkout, not a standalone project.
2. **The two shared libraries must match.** CircuitAI's own README:25 records
   the failure and the cure: *"Dead AI upon match start: ensure that
   `libSkirmishAI.so` is compatible with `AI/Interfaces/C/0.1/libAIInterface.so`
   (i.e. replace it with own build)."* Building both from one tree makes the
   pairing structural rather than something to remember.

`docker/runtime/Dockerfile` does this as a multi-stage build, so CI and a
laptop run the same definition. `scripts/build-engine-ai.sh` is a thin wrapper
around `docker buildx` that arranges the two checkouts and can extract just the
install tree (`--target export`).

### 3.1 Why the stages are split where they are

```text
engine-build ──▶ ai-build ──┬──▶ runtime
                            └──▶ export
```

`engine-build` compiles the engine, the C AI interface and the C++ wrapper with
**no Skirmish AI present**. `ai-build` then adds the AI and reconfigures *in
place*, so CMake discovers the new target while `/build/out` still holds the
engine's object files, and Ninja compiles only the AI.

The split exists because of how the work is distributed in practice: the AI
changes several times a day and the engine changes when the pin moves. Keeping
them in separate layers means an AI-only change reuses the cached engine layer
and recompiles the small half. A single combined stage would rebuild everything
on every AI edit, because the `COPY` of the AI source would invalidate it.

`cmake --fresh` is therefore used in the first stage and deliberately **not** in
the second — it would discard exactly the object files the split exists to
preserve.

Two details that are easy to get wrong:

- **AI discovery is a glob, not git.** `AI/Skirmish/CMakeLists.txt` calls
  `get_list_of_submodules`, but that macro is `file(GLOB ... */CMakeLists.txt)`
  (`rts/build/cmake/Util.cmake:169`). A plain directory is found. No submodule
  wiring is needed, so our source can simply replace the vendored `BARb`
  submodule directory.
- **The directory name is the identity.** It becomes the CMake target name and
  the installed path, and must match `shortName` in the AI's `data/AIInfo.lua`.
  For this fork that is `BARb`, version `stable` — the version string is the
  literal word, read from `VERSION`, not a number.
- **`AI_EXCLUDE_REGEX` is not a safe way to drop AIs.** It is matched against
  the C interface and the C++ wrapper too (`AI/Interfaces/C/CMakeLists.txt:26,81`
  and `AI/Wrappers/Cpp/CMakeLists.txt:22`), both of which CircuitAI links
  against, so a broad pattern silently removes its own dependencies. The engine
  stage deletes `AI/Skirmish/*/` instead.
- **Only the headless build is wanted.** `BUILD_spring-legacy` and
  `BUILD_spring-dedicated` default to on (`rts/builds/CMakeLists.txt:76,105-107`).
  Legacy is the full graphical engine and the largest target in the tree;
  dedicated cannot host a Skirmish AI at all. Both are turned off.

The build container is pinned by digest from
`docker-build-v2/images_versions.sh`:
`ghcr.io/beyond-all-reason/recoil-build-amd64-linux@sha256:31b3667…`. It is
Ubuntu 18.04 with gcc-13 and `PREFER_STATIC_LIBS=TRUE`, deliberately old so the
output runs widely (`images/all-linux/Dockerfile:1-3`).

Both refs live in `config/engine.pin`. Moving `ENGINE_REF` is an engine upgrade
and must be a reviewed diff: it silently invalidates the interface pairing.

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
directly. No SPADS.

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
[§7.4](#74-resources) — both the engine and the AI size their thread pools from
the host's CPU topology, which inside a container is the wrong number. The
runner reads the cgroup quota (`cpu.max`, or the v1 `cpu.cfs_quota_us` /
`cpu.cfs_period_us`) and sets the engine's pool from that.

Per run, into `/out/<pod-name>/`: `script.txt` (exactly what was played),
`springsettings.cfg` (exactly how the engine was configured), `result.json`
(winners, demo path, exit reason, wall-clock, engine warnings), plus the
engine's own `infolog.txt` and `demos/`.

### 4.5 Spawning, which is where an all-AI match goes wrong

Three settings decide whether the AI ever get a commander. All three were found
by running real matches rather than by reading alone, and each failure looks the
same from outside: a match that ends in seconds, with no error.

**`StartPosType` must be 0, not 2.** BAR branches on it at
`game_initial_spawn.lua:710-728`:

| Value | BAR's behaviour |
| --- | --- |
| 0 | automatic spawning at the map's default start positions, **fixed order** |
| 1 | the same, random order |
| 2 | "manual spawning based on positions chosen by players in start boxes" |

An all-AI match has nobody to choose, so with 2 the teams never spawn and are
eliminated within seconds. Measured: allyteam 0's four teams died at frame 106
having never placed a commander. Fixed order also keeps runs comparable, which
matters more here than variety.

**Start rectangles must be set.** `AllyTeam.h:25-28` defaults every rectangle to
the whole map — left/top 0, right/bottom 1 — so without them every allyteam
shares one box covering everything.

**Teams must be interleaved across allyteams.** BAR maps alternate their start
positions by index: All That Glitters puts even-numbered positions on one half
of the map and odd-numbered on the other. With `StartPosType` 0 the engine gives
team *i* the map's position *i*, so assigning teams to allyteams in contiguous
blocks (0-3 against 4-7) scatters each side across both halves and seats every
team beside an enemy. `BARHOST_TEAM_LAYOUT` defaults to `interleaved` for that
reason; `blocked` stays available for maps that group positions by side.

**A limitation no setting fixes.** BAR vetoes AI-chosen start positions outright
— `game_initial_spawn.lua:478-479` returns `false` for `isAiTeam` — and places
them with `GuessStartSpot` (`common/lib_startpoint_guesser.lua:397`). AI
placement cannot be influenced from the AI side. The only stock-game levers are
start box geometry and post-spawn relocation.

---

## 5. Container design

One image, `barhost-match`, produced by the `runtime` stage of the multi-stage
build in [§3](#3-build-engine-and-ai-from-one-tree). The compiler and its
toolchain stay behind in the build stages; only the install tree crosses over.

| Layer | Contents |
| --- | --- |
| base | `debian:stable-slim` + runtime libs + `python3` |
| engine | `COPY --from=ai-build`: `spring-headless`, `pr-downloader`, `AI/Interfaces/C/0.1/`, `AI/Skirmish/<name>/<version>/` |
| runner | `run_match.py`, `entrypoint.sh` |

Debian's glibc is newer than the Ubuntu 18.04 build stage's, which is the
direction that works; the reverse would not. The build stage also links most
dependencies statically via `spring-static-libs`, so the runtime package list
is short.

Runs as uid 10001, non-root, no added capabilities. Nothing here needs
privilege: the ports used (53200, 53210) are unprivileged and loopback-only.

**Build-time smoke test.** The image build runs
`spring-headless --list-skirmish-ais` (`SpringApp.cpp:138`) and fails unless the
expected AI and version appear. That turns the "dead AI at match start" failure
into a build error found in seconds, instead of a mystery five minutes into a
game. It also catches a missing runtime library, since the engine has to load to
answer at all.

The check matches the exact output format of
`AILibraryManager::OutputSkirmishAIInfo` (`AILibraryManager.cpp:450-466`),
confirmed against a real engine release:

```text
# Available Spring Skirmish AIs
# -----------------------------
# [Name]               [Version]            [Interface-name]     [Interface-version]
# BARb                 stable               C                    0.1
```

It also fails on the duplicate-AI warning the same function emits (`:474`),
which is what two copies of one AI installed side by side would look like.

Entrypoint subcommands: `match` (default), `fetch-content`, `list-ais`.

---

## 6. Content

Stock BAR, fetched with `pr-downloader` into a persistent volume, not baked
into the image.

**Why the volume and not the image.** The rapid pool plus maps is gigabytes;
the AI changes several times a day. Keeping content out of the image keeps each
AI rebuild a small push and a small pull onto an HDD-backed single node. The
init container is idempotent — rapid skips what the pool already has — so a warm
volume costs seconds.

**A trap worth recording.** `pr-downloader --help` shows its rapid master
defaults to `https://repos.springrts.com/repos.gz`, which does not carry BAR's
repo. `byar:` tags will not resolve against it. The entrypoint sets
`PRD_RAPID_REPO_MASTER=https://repos-cdn.beyondallreason.dev/repos.gz`, which is
the master a real BAR install uses.

**Pinning.** Rapid exposes named tags (`byar:test`, `byar:stable`,
`byar:br-master`) and a `byar:git:<sha>` form — over 12,000 of them in the
current index. For a benchmark that should stay comparable across AI versions,
pin the sha; `byar:test` tracks BAR's test branch and will drift.

**Maps.** A small fixed set, named by the archive's internal display name
(`mapinfo.lua` `name`), which is both what `GAME\MapName` resolves
(`GameSetup.cpp:679`) and what `--download-map` expects. Defaults are
`All That Glitters v2.2.3` and `Comet Catcher Remake`, read out of the
archives rather than guessed. A fixed map set matters more than variety: it is
what makes two runs comparable.

`--isolation --isolation-dir /spring-data` pins content discovery to the
volume. Because isolation mode would otherwise hide the engine's own `AI/` and
`base/` trees, the runner also sets `SPRING_DATADIR` to the engine directory —
LEVEL 3 of `LocateDataDirs` honours it even in isolation mode
(`DataDirLocater.cpp:434-441`), and `AddDirs` splits on colons (`:178`).

---

## 7. Kubernetes design

Kustomize base + `test`/`prod` overlays, manual PVs with `claimRef`,
`mcgill-k8s-pull-2026` pull secret.

```text
k8s/
  base/     rbac.yaml  pvc.yaml  job.yaml  match.env  kustomization.yaml
  overlays/{test,prod}/  namespace.yaml  persistent-volumes.yaml  kustomization.yaml
```

### 7.1 Workload type

**A `Job`, not a Deployment.** The work is a batch that ends. `backoffLimit: 0`
— a failed match is a result to read, and a retry would overwrite the infolog
explaining it. `ttlSecondsAfterFinished: 86400` reaps pods while leaving the
replay and `result.json` on the volume.

A Job's pod template is immutable, so the pipeline deletes and recreates rather
than patching.

This choice removes most of the original design's difficulty at a stroke: no
`hostNetwork`, so no port collisions and no router forwarding; no rolling-update
hazard; no liveness probe that must never fire mid-match.

### 7.2 Storage

Two volumes on `storageClassName: data2`, host path
`/mnt/data2/k8s-volumes/barhost/<env>/<purpose>`, `mcgill-data` node affinity,
`Retain`, every PV carrying `claimRef`.

| PVC | Purpose | Size |
| --- | --- | --- |
| `barhost-spring-data-pvc` | rapid pool + curated maps | 20Gi |
| `barhost-runs-pvc` | replays, infologs, `result.json` per run | 20Gi |

Host directories are created by the pipeline and `chown`ed to 10001 to match
the container user.

### 7.3 Networking

**Nothing is exposed.** Both sockets are loopback inside the pod: the autohost
channel on 53200 and the local game server on 53210. No Service, no Ingress, no
NodePort, no UDP forwarding.

### 7.4 Resources

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

"Requested settings" is the configuration in [§10.1](#101-the-requested-configuration):
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
[§10](#10-phase-2-the-public-lobby-autohost) — human players mean the lobby,
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

Non-root (uid 10001), `allowPrivilegeEscalation: false`, all capabilities
dropped, `RuntimeDefault` seccomp, and no service account token mounted — the
runner has no reason to reach the Kubernetes API.

---

## 8. CI/CD pipeline

Standard three jobs: `set-env → build → deploy`, in
`.github/workflows/deploy.yml`. Branch name is the environment; `main` never
deploys.

**Triggers.** `push` to `test`/`prod`, `workflow_dispatch` (choose an AI ref and
environment), and `repository_dispatch` with type `ai-updated` — the last is
what lets a push in the AI repository start a match here, which is the point of
the whole pipeline.

`concurrency` pins one match per environment: the node has one set of volumes
and one CPU budget, and overlapping runs would make the timings meaningless.

**`set-env`** resolves the environment, reads `config/engine.pin`, and lets an
explicitly requested AI ref override the pinned default.

**`build`** checks out this repo, the engine at its pinned SHA (with
submodules), and the AI at its ref; restores a ccache keyed on the engine ref so
an AI-only change reuses the engine objects; runs
`scripts/build-engine-ai.sh`; then builds and pushes the image with
`docker/build-push-action@v6` and `cache-from: type=gha`.

The first build is slow — a full engine compile. Later ones are dominated by
the AI alone, which is what the cache key is arranged to achieve.

**`deploy`** runs in `mcgill-deployer` with `--privileged` for WireGuard. The
12-step process applies, with these project-specific points:

- **Step 5 is a guard.** If a match is still running, the deploy fails with a
  clear message instead of killing it. The same intent as the original design's
  rollout guard, in the form a Job needs.
- **Step 6** creates both volume paths and `chown`s them to 10001.
- **Step 10** deletes the previous Job, because a Job is immutable.
- **Step 12** waits for the Job to succeed or fail — not `rollout status`,
  which does not apply — then prints the match log either way.

**Secrets.** The standard seven, plus **`AI_REPO_TOKEN`**, a PAT that can read
the private AI repository; the workflow's own `GITHUB_TOKEN` cannot.

---

## 9. Deviations from the cluster standard

| Standard | Here | Why |
| --- | --- | --- |
| Deployment | `Job` | the work is a batch that ends |
| RollingUpdate | n/a | a Job is replaced, not rolled |
| Liveness + readiness probes | none | a batch pod has nothing to probe; success is the exit code |
| Service per workload | none | nothing listens outside the pod |
| Ingress per service | none | nothing to expose |
| `rollout status` wait | poll Job conditions | Jobs have no rollout |
| Namespace `mcgill-<service>-<env>` | `barhost-<env>` | follows the existing design and the skill's own Kustomize templates; the standard's two references disagree — **confirm before prod** |
| Namespace in `base/` | in each overlay | avoids relying on the namespace transformer renaming a Namespace object |
| 120s rollout wait | 5400s job wait | a cold content fetch plus a full match |
| 7 GitHub secrets | 8 | `AI_REPO_TOKEN` for the private AI repository |

---

## 10. Phase 2: the private lobby host

**Now requested, not yet built.** The target is a *private, passworded* battle
room on BAR's lobby server, hosted by the secondary account, with humans playing
co-op against 8 AI. That is a different product from phase 1 and it re-opens
every cost phase 1 was designed to avoid.

### 10.1 The requested configuration

| Setting | Value | Where it lives |
| --- | --- | --- |
| Room | private, password-protected | SPADS battle setting |
| Host role | the headless host is boss | SPADS owner/boss |
| AI | 8 × BARb, all on one team (the human side opposite) | `autoAddBotNb` / `!addBot` |
| AI bonus | +50% each | team `Handicap` — `TeamBase.cpp:59-64` divides by 100 into `SetAdvantage`, which is `SetIncomeMultiplier(v + 1)` (`TeamBase.h:73`) |
| Balancing | auto-balance **off** | SPADS `autoBalance` |
| Co-op | on | modoption `coop` (`modoptions.lua:690`) |
| Start positions | chosen in game, **skill-ordered draft** | `StartPosType` 2 + modoption `draft_mode=skill` (`modoptions.lua:224-257`) |
| Start boxes | the map's own | leave to `game_startbox_config.lua`; see [§4.5](#45-spawning-which-is-where-an-all-ai-match-goes-wrong) |
| Legion | enabled | modoption `experimentallegionfaction` (`modoptions.lua:1855`) |

The modoption half of this is already implemented and shared with phase 1 —
`run_match.py` sets `experimentallegionfaction`, `draft_mode=skill` and `coop`
by default, and gives every AI team `Handicap=50`. `draft_mode` only does
anything with `StartPosType` 2, since `game_initial_spawn.lua` consults it only
on the choose-in-game path.

The SPADS half is not. `autoBalance`, the room password, boss handling and
placing all bots on one team are SPADS settings, and **none of them can be
verified from this workspace** — SPADS is not vendored here. Diff them against
upstream `Yaribz/SPADS` `etc/spads.conf` and BAR's `spads_config_bar` before
writing `config/`, the same way engine claims are cited by `file:line`.

### 10.2 What it costs, all of which returns together

1. **Inbound UDP.** The cluster's only internet path is a router forward to
   HAProxy NodePorts 32080/32443, which are TCP/HTTP. Players need inbound UDP
   to the engine. Ingress cannot carry it, and the default NodePort range
   (30000–32767) excludes SPADS's usual base ports. The workable answer is
   `hostNetwork: true` plus **a router UDP forward to `mcgill-data`**, which is
   configuration outside this repository. Without it the room is visible and
   unjoinable — a failure that looks like success. A password does not remove
   this: the room is private, not unreachable.
2. **`hostNetwork` erases the pod network boundary.** Pod NetworkPolicies do not
   apply to such a pod, and it can reach everything on the node, including the
   shared databases. The autohost control socket must be bound explicitly to
   `127.0.0.1`.
3. **A long-running Deployment**, which brings back `strategy: Recreate` (a
   surging pod collides on host ports), a liveness probe that must never fire
   mid-match, and a deploy guard against live games.
4. **The botflag**, below.
5. **A social prerequisite.** BAR discourages third-party autohosts by default
   and asks to be contacted first. A private room is a weaker version of this
   concern, not an exemption.

Secrets it needs, none of which phase 1 uses: `SPADS_LOBBY_LOGIN`,
`SPADS_LOBBY_PASSWORD`, `SPADS_OWNER_LOBBY_LOGIN`, `SPADS_REGISTRATION_EMAIL`,
plus the room password.

### 10.3 The botflag is now on the critical path

Phase 1 did not care about this. The requested room does: **8 AI plus 8 human
players is 16 slots**, and without a botflag a room is capped at 8 players plus
spectators. Even if AI turn out not to count against the cap, 8 humans plus the
host is already at it.

So the question AGENTS.md records as blocking — *do AI count toward the
8-player non-botflag cap?* — has to be answered before this is worth building,
and the answer is very likely "request a botflag either way". Ask in
`#teiserver-spads` on the BAR Discord.

---

## 11. Risks and open questions

Ordered by how much they could invalidate phase 1.

**1. An all-AI match with no human players loses one allyteam immediately.**
*Open, reproducible, cause unknown.* In every unattended run, every team in
allyteam 0 is eliminated between frames 86 and 121 — roughly three seconds — and
the other side is declared the winner. Ruled out by experiment, not argument:

| Varied | Values tried | Result |
| --- | --- | --- |
| map | All That Glitters v2.2.3, Comet Catcher Remake 1.8 | no change |
| `StartPosType` | 0 (fixed), 2 (choose-in-game) | no change |
| start boxes | map default, even bands, explicit rects | no change |
| team layout | blocked (0-3 vs 4-7), interleaved (evens vs odds) | no change; the dead side follows allyteam 0, not a faction |
| spectator's team | 0, 1 | no change |

`deathmode=neverend` removes `game_end.lua` entirely (`:246`) and the match then
runs indefinitely, which is what makes the measurements in
[§7.4](#74-resources) possible. That also points at the likely culprit —
`game_end.lua` deciding an allyteam is dead when there is no human player in the
game at all — but that is a hypothesis, not a finding.

It matters for the CI harness, where there are never human players. It probably
does **not** affect [§10](#10-phase-2-the-private-lobby-host), where humans are
in the room. Resolve before trusting an unattended match's *result*; the
resource numbers do not depend on it.

**2. Team statistics only exist for games that reach game over.** The recorder
fills them in on the game-over path (`GameServer.cpp:313-322`), so a run cut
short by the wall clock, or one using `deathmode=neverend`, has none.
`scripts/demo_stats.py` reports this rather than showing zeros as if they were
data. Combined with risk 1, this currently leaves no clean way to score an
unattended match objectively.

**3. Resource sizing is measured but on a desktop, not the node.**
[§7.4](#74-resources). `mcgill-data` also carries eight database instances.
Check allocatable capacity before prod.

**4. Build time.** A cold engine build is long. The ccache key is arranged so
an AI-only change is cheap, but a cold cache — or an engine pin bump — is not.

**5. Engine/AI version coupling.** Mitigated structurally by building both from
one tree and by the `--list-skirmish-ais` smoke test, but an engine pin bump
still needs a matching AI.

**6. Content drift.** `byar:test` moves. Pin `byar:git:<sha>` for anything whose
results you intend to compare over time.

**7. Disk growth.** Replays and infologs accumulate on the runs volume with no
pruning yet. Tens of MB per match adds up; a full volume fails silently-ish.

**8. Namespace naming.** The cluster standard's own references disagree
(`<project>-<env>` vs `mcgill-<service>-<env>`). `barhost-<env>` is assumed.

**9. The botflag now blocks phase 2.** *Do AI count toward the 8-player
non-botflag lobby cap?* Still unresolved, and no longer hypothetical: the
requested room is 8 AI plus 8 humans, so even if AI are exempt, 8 humans plus
the host is already at the cap. See [§10.3](#103-the-botflag-is-now-on-the-critical-path).
It does **not** affect phase 1, which never touches the lobby server.

---

## Related

- `../rjm.bar.docs/knowledge/` — game and unit facts
- `../s3k-CircuitAI/doc/roles/` — BARb AI role behaviour
- [`RecoilEngine docker-build-v2`](https://github.com/beyond-all-reason/RecoilEngine/tree/master/docker-build-v2) — the pinned build container this uses
- [`Yaribz/SPADS`](https://github.com/Yaribz/SPADS) — phase 2's autohost
- [`spads_config_bar`](https://github.com/beyond-all-reason/spads_config_bar) — BAR's reference SPADS configuration
- [`BAR-Devtools`](https://github.com/beyond-all-reason/BAR-Devtools) — local Teiserver + SPADS + client, for phase 2 testing
