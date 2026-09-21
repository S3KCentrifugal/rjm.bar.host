# BAR Headless Autohost — Design

Design for a containerised Beyond All Reason autohost that opens a multiplayer
lobby, applies battle settings, adds 8 BARbarian AI, and starts the game —
deployed to the private k3s cluster by GitHub Actions.

Status: design, not built. Written 2026-09-20.

Verified against RecoilEngine `92efda5e60` and Beyond All Reason `1d267c20d1`
(both 2026-09-13), and the cluster inventory as of this date.

---

## Contents

- [1. Scope](#1-scope)
- [2. Build versus configure](#2-build-versus-configure)
- [3. Engine build: headless, not dedicated](#3-engine-build-headless-not-dedicated)
- [4. Component architecture](#4-component-architecture)
- [5. The four operations](#5-the-four-operations)
- [6. Container design](#6-container-design)
- [7. Kubernetes design](#7-kubernetes-design)
- [8. CI/CD pipeline](#8-cicd-pipeline)
- [9. Secrets](#9-secrets)
- [10. Risks and open questions](#10-risks-and-open-questions)

---

## 1. Scope

**In scope.** A long-running service that maintains a battle room on BAR's
official lobby server, rotates maps, greets and advertises in chat, fills the
room with 8 BARb AI, starts matches, and restarts the room afterwards.
Containerised, deployed to `mcgill-data` via the standard pipeline.

**Out of scope.** Running our own lobby server (Teiserver). BAR's lobby is used
as-is; this service connects to it as a client. Also out of scope: modifying the
BAR game archive, which would break checksum compatibility with stock clients.

**Project name.** `barhost`. Namespaces `barhost-test`, `barhost-prod`.

---

## 2. Build versus configure

**Decision: configure SPADS. Do not write a lobby client.**

SPADS (Spring Perl Autohost for Dedicated Server) already implements all four
required operations, and BAR maintains a containerised configuration of it.
Writing a Tachyon or SpringLobbyProtocol client from scratch would reimplement a
mature, in-production component for no gain.

Every requirement maps to an existing setting:

| Requirement | SPADS mechanism |
| --- | --- |
| Start a lobby | connects to the lobby server, opens a battle room automatically |
| Apply lobby settings | `spads.conf` presets + `battlePresets.conf` mod options |
| Rotate maps | `rotationType`, `rotationEndGame`, `rotationDelay`, `rotationEmpty`, `mapList` |
| Automated chat | `welcomeMsg`, `welcomeMsgInGame`, `advertMsg`, `advertDelay` |
| Add BARb AI | `springServerType:headless` + `localBots`, `autoAddBotNb`, `maxLocalBots`, `allowedLocalAIs` |
| Start the game | `autoStart`, `autoBalance`, `autoFixColors` |
| Monitoring | `JsonStatus` plugin |

**What we actually build** is therefore thin:

1. A container image pinning a known-good engine + BARb + SPADS combination.
2. Config templating from environment variables and Kubernetes secrets.
3. A health/readiness shim, because SPADS exposes no HTTP endpoint.
4. Kustomize manifests and the deploy workflow.

Upstream references: [`Yaribz/SPADS`](https://github.com/Yaribz/SPADS) (the
program), [`beyond-all-reason/spads_config_bar`](https://github.com/beyond-all-reason/spads_config_bar)
(BAR's config, `Dockerfile`, `docker-compose.yml`, `.env.sample`).

> **Successor note.** BAR is building `recoil-autohost` for the Tachyon protocol.
> It is not production-ready. Keep customisation shallow so migration stays cheap:
> no SPADS plugins of our own beyond configuration.

---

## 3. Engine build: headless, not dedicated

**This is the load-bearing decision of the design.**

Recoil ships two non-graphical builds. They are not interchangeable:

| Target | Simulation | Can host Skirmish AI | Cost |
| --- | --- | --- | --- |
| `engine-dedicated` | **no** | **no** | low — a net relay |
| `engine-headless` | yes | **yes** | high — full sim |

Evidence, from the engine tree:

- `rts/builds/dedicated/CMakeLists.txt` compiles **zero** `ExternalAI` sources.
  Its `main.cpp` constructs only `CGameServer`.
- Skirmish AI instances carry a `hostPlayer` (`rts/ExternalAI/SkirmishAIData.h:30`,
  `SkirmishAIHandler::GetSkirmishAIsInTeam(teamId, hostPlayerId)`). An AI always
  executes inside a full engine process.
- SPADS documents the same split: headless "allows running AI bots and LUA
  scripts on server side"; local bots **require** spring-headless.

Therefore `springServerType:headless`, and the container must size for a
simulation with 8 AI, not for a relay. See [§7.5](#75-resources).

---

## 4. Component architecture

```text
                    BAR lobby server (Teiserver)          ← BAR operates
                              ▲
                              │ TCP 8200, outbound only
┌─────────────────────────────┼──────────────────────────────────────┐
│ pod: barhost                │                        (hostNetwork) │
│                             │                                      │
│  ┌──────────────────────────┴───────────┐   ┌──────────────────┐   │
│  │ spads                                │   │ content-updater  │   │
│  │  • lobby room state machine          │   │  • maps (rclone) │   │
│  │  • map rotation, chat, votes         │   │  • games (pr-dl) │   │
│  │  • spawns + controls the engine      │   │  • sidecar       │   │
│  │            │ autohost proto, loopback│   └────────┬─────────┘   │
│  │            ▼                         │            │             │
│  │  ┌──────────────────────────────┐    │            │             │
│  │  │ engine-headless (per battle) │    │            │             │
│  │  │  • simulation                │    │            │             │
│  │  │  • 8 × BARb libSkirmishAI.so │    │            │             │
│  │  └──────────────────────────────┘    │            │             │
│  └───────────────────┬──────────────────┘            │             │
│                      │ UDP 53200-53205               │             │
└──────────────────────┼───────────────────────────────┼─────────────┘
                       ▼                               ▼
              players on the internet          PVC: spring-data
              (via router UDP forward)         PVC: engines
                                               PVC: spads-var
```

Three network paths, with very different requirements:

| Path | Direction | Protocol | Exposure |
| --- | --- | --- | --- |
| SPADS → lobby server | outbound | TCP 8200 | none needed |
| SPADS ↔ engine | loopback | UDP 53100+ | **localhost only** |
| players → engine | **inbound** | UDP 53200+ | **public** |

Only the third needs exposure. This is the hard part — see [§7.3](#73-networking-the-hard-part).

---

## 5. The four operations

### 5.1 Start a lobby

SPADS logs in with the bot account and opens a battle room on connect. No code.

```ini
lobbyLogin:%SPADS_LOBBY_LOGIN%
lobbyPassword:%SPADS_LOBBY_PASSWORD%
```

The room's name in the public battle list comes from `SPADS_BATTLENAME_PREFIX`.
Make it recognisable — it is the only discovery mechanism players have.

**Account constraint.** A dedicated bot account, never a playing account: a
botflagged account misbehaves if played on. Without a botflag the room is capped
at **8 players** plus unlimited spectators. Botflags are requested from BAR devs
(`#teiserver-spads` on Discord). See [§10](#10-risks-and-open-questions) — it is
unconfirmed whether AI count against that cap, which would be decisive here.

### 5.2 Apply lobby settings

Two layers. `spads.conf` presets hold autohost behaviour; `battlePresets.conf`
holds engine/game mod options.

```ini
# rotation
rotationType:map
rotationEndGame:order        # fixed cycle; 'random' to shuffle
rotationDelay:300
rotationEmpty:random
mapList:<curated list>

# chat
welcomeMsg:Welcome %u. 8 BARb AI, type !help for commands.
welcomeMsgInGame:Game in progress — you are spectating.
advertMsg:Map rotation is on. !vote to change settings.
advertDelay:30

# start conditions
autoStart:on
autoBalance:advanced
autoFixColors:advanced
```

Mod options of note, in the battle preset:

```ini
startpostype=2               # choose-in-game; BAR's default
```

`startpostype` is worth setting explicitly rather than inheriting. It governs
whether start positions may be chosen at all (`rts/Game/GameSetup.cpp:646`,
`GameServer.cpp:1194`).

> **Known limitation.** BAR's own `game_initial_spawn.lua:479` rejects start
> positions from AI teams outright (`isAiTeam → return false`) and assigns them
> via `GuessStartSpot` at `GameStart`. AI placement therefore **cannot** be
> influenced from the AI side, regardless of `startpostype`. The only stock-game
> levers are start box geometry and post-spawn relocation. Do not spend effort
> here expecting a different outcome.

### 5.3 Add BARb AI

Only possible with `springServerType:headless`.

```ini
springServerType:headless
springServer:/opt/bar/engine/<version>/spring-headless
springDataDir:/spring-data

maxLocalBots:8
autoAddBotNb:8
localBots:BARb;BARb;BARb;BARb;BARb;BARb;BARb;BARb
allowedLocalAIs:BARb
```

Runtime control is `!addBot` / `!removeBot`.

The AI must be present on disk in the engine's expected layout, with matching
interface:

```text
<engine>/AI/Skirmish/BARb/<version>/
    AIInfo.lua  AIOptions.lua  config/  script/  libSkirmishAI.so
<engine>/AI/Interfaces/C/0.1/
    InterfaceInfo.lua  libAIInterface.so
```

**Linux naming.** `libSkirmishAI.so` and `libAIInterface.so` — the `.dll` files
from a Windows install are unusable. Pull BAR's Linux engine release.

**Version coupling is a real failure mode.** From the CircuitAI README: *"Dead AI
upon match start: ensure that `libSkirmishAI.so` is compatible with
`AI/Interfaces/C/0.1/libAIInterface.so`."* Both must come from the same engine
build. This is why engine version is pinned in the image rather than resolved at
runtime ([§6](#6-container-design)).

`config/` and `script/` sit **beside** the library and are part of the AI — the
AngelScript behaviour layer lives in `script/`. Ship the whole directory.

### 5.4 Start the game

`autoStart:on` launches when the room's start conditions are met. With 8 AI
auto-added, the room is full from the moment it opens, so the practical trigger
is the rotation/restart cycle rather than player count.

SPADS then spawns `engine-headless`, which loads the map, instantiates 8 BARb,
and runs the simulation. Game events flow back to SPADS over the Autohost
Protocol (`rts/Net/AutohostInterface.h`), which is what `JsonStatus` surfaces.

---

## 6. Container design

Two images, mirroring BAR's own split.

### 6.1 `barhost-spads`

| Layer | Contents | Rationale |
| --- | --- | --- |
| base | `debian:stable-slim` | Perl + engine runtime deps |
| perl | SPADS + required modules | pinned SPADS release |
| engine | `spring-headless` + `AI/` tree for **one pinned version** | version coupling (§5.3) |
| config | templated `spads.conf`, `battlePresets.conf`, `springsettings.cfg` | rendered at start from env |
| entrypoint | render config → exec SPADS | secrets never baked |

**Baked into the image:** engine binary, `AI/Interfaces/C/0.1/`,
`AI/Skirmish/BARb/`, SPADS itself, config templates.

**Not baked:** maps, game archives, credentials, SPADS runtime state. Those are
volumes.

Pinning the engine in the image means an engine upgrade is a deliberate image
rebuild with a matching AI, not a silent runtime drift that produces dead AI.

### 6.2 `barhost-updater`

Sidecar. Syncs maps (rclone, from BAR's shared Drive) and game archives
(pr-downloader) into the shared `spring-data` volume. Mirrors
`badosu/spads-services`.

Runs as a sidecar rather than an init container: content updates should not
block pod start, and maps change independently of deploys.

### 6.3 Config templating

Render `spads.conf` from environment at entrypoint. Keep the template in the repo
so settings are reviewable in git; keep values in `ConfigMap` (non-secret) and
`Secret` (credentials).

---

## 7. Kubernetes design

Follows the cluster deployment standard: Kustomize base + `test`/`prod`
overlays, manual PVs with `claimRef`, `mcgill-k8s-pull-2026` pull secret.

```text
k8s/
  base/
    namespace.yaml  rbac.yaml  configmap.yaml
    deployment.yaml  service.yaml  pvc.yaml  pdb.yaml
    secrets.yaml.example        # gitignored actual
    kustomization.yaml
  overlays/
    test/  { kustomization.yaml, persistent-volumes.yaml }
    prod/  { kustomization.yaml, persistent-volumes.yaml }
```

### 7.1 Workload type

**Deployment, `replicas: 1`, `strategy: Recreate`.**

Not RollingUpdate. With `hostNetwork` the new pod would bind the same UDP ports
as the old one and fail. `Recreate` accepts a gap instead — correct here, because
a rolling update would corrupt a live match anyway.

Not a StatefulSet: there is one replica with no ordinal identity and no
per-replica volume templates. A Deployment with RWO PVCs on a single node is
simpler and sufficient.

### 7.2 Storage

Three volumes on `storageClassName: data2`, host path pattern
`/mnt/data2/k8s-volumes/barhost/<env>/<purpose>`.

| PVC | Purpose | Size (initial) | Notes |
| --- | --- | --- | --- |
| `barhost-spring-data-pvc` | maps + game archives | **250Gi** | dominated by maps; 500+ maps is realistic |
| `barhost-engines-pvc` | engine versions | 20Gi | several versions retained |
| `barhost-spads-var-pvc` | SPADS state, logs, preferences | 5Gi | small, but must survive restarts |

Every PV carries `claimRef` — without it `WaitForFirstConsumer` can bind across
environments:

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: barhost-spring-data-pv-prod
spec:
  capacity: { storage: 250Gi }
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: data2
  claimRef:
    namespace: barhost-prod
    name: barhost-spring-data-pvc
  local:
    path: /mnt/data2/k8s-volumes/barhost/prod/spring-data
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: [mcgill-data]
```

Host directories must exist before first deploy — created by the pipeline's
busybox step (deploy step 6).

### 7.3 Networking — the hard part

**The cluster is private and its only internet path is a router port-forward to
HAProxy NodePorts 32080/32443, which are TCP/HTTP.** BAR players need **inbound
UDP** to the engine. Ingress cannot carry this. This is the single largest
obstacle in the design and it is not solvable inside Kubernetes alone.

Three options considered:

| Option | Verdict |
| --- | --- |
| HAProxy Ingress | **Rejected.** HTTP/TLS only. No UDP. |
| `NodePort` (UDP) | **Viable with a caveat.** Default NodePort range is 30000–32767, so `SPADS_BASEGAME_PORT=53200` is **out of range**. Would require remapping SPADS base ports into 30000–32767, or changing the apiserver's `--service-node-port-range`. |
| **`hostNetwork: true`** | **Recommended.** Matches BAR's own compose (`network_mode: host`), avoids the NodePort range problem entirely, and is deterministic on a single-node cluster. SPADS allocates game ports dynamically per battle; hostNetwork makes that work without per-port Service plumbing. |

**Recommendation: `hostNetwork: true`**, with a documented port block.

```yaml
spec:
  template:
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet    # required, else cluster DNS breaks
```

Port allocation, sized to `SPADS_MAX_INSTANCES`:

| Range | Purpose | Exposure |
| --- | --- | --- |
| UDP 53200–53205 | game servers (one per battle) | **router must forward to `mcgill-data`** |
| UDP 53100–53105 | SPADS ↔ engine autohost channel | localhost only — **do not forward** |
| TCP 8200 outbound | SPADS → lobby server | egress only |

**Action required outside Kubernetes:** add UDP forwards for 53200–53205 on the
router, pointed at `mcgill-data`. Without this the room lists publicly and every
player times out on join — a failure mode that looks like the autohost working.

Set `SPRING_HOSTIP` to the public address. SPADS advertises this to players; left
unset behind NAT it advertises an unroutable private IP.

**Bandwidth.** The game server relays every player's commands to every other
player. Uplink, not just downlink, is a real constraint on a home connection.

### 7.4 Health probes

SPADS exposes no HTTP endpoint, so HTTP probes are unavailable. Use the
`JsonStatus` plugin's output file and an exec probe on its freshness:

```yaml
readinessProbe:
  exec:
    command: ["/usr/local/bin/check-lobby-connected.sh"]
  initialDelaySeconds: 60
  periodSeconds: 30
livenessProbe:
  exec:
    command: ["/usr/local/bin/check-spads-alive.sh"]
  initialDelaySeconds: 120
  periodSeconds: 60
  failureThreshold: 3
```

`check-lobby-connected.sh` asserts the JsonStatus file is fresh **and** reports a
lobby connection. `check-spads-alive.sh` asserts only that the process lives —
deliberately weaker, so a lobby-server outage does not restart a pod mid-match.

> **Design rule:** the liveness probe must never fire during a running game.
> Restarting kills the match for every connected player.

### 7.5 Resources

8 BARb plus the simulation in one process. BARb is a full AI — threat maps,
pathfinding, per-team task scheduling — and the Recoil simulation is largely
single-threaded, so this is CPU-bound with limited parallelism.

```yaml
resources:
  requests: { cpu: "3000m", memory: "6Gi" }
  limits:   { cpu: "6000m", memory: "12Gi" }
```

**These numbers are an unmeasured starting point.** No profiling of 8 BARb on
one headless instance has been done. Measure before committing to `prod`:
run a match in `test`, watch `kubectl top pod`, and revise. Under-provisioning
CPU does not crash the game — it slows the simulation below real time, which
players experience as lag.

Guaranteed QoS is not achievable with these ranges; if games prove sensitive to
eviction, set requests equal to limits.

### 7.6 Disruption

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
spec:
  maxUnavailable: 0
  selector:
    matchLabels:
      app.kubernetes.io/name: barhost
```

With one replica this blocks voluntary eviction entirely — intended. Node
maintenance must be scheduled deliberately, between matches.

### 7.7 Ingress

**None for the game itself.** Optionally one `haproxy-private` Ingress exposing
the JsonStatus feed for monitoring over VPN. It must not be `haproxy-public` —
it carries player names and battle state.

---

## 8. CI/CD pipeline

Standard three-job workflow: `set-env → build → deploy`, in
`.github/workflows/deploy.yml`.

```yaml
on:
  push:
    branches: [test, prod]
  workflow_dispatch:
    inputs:
      force_build:
        description: 'Force rebuild all images'
        type: boolean
        default: false
```

Branch name is the environment. `main` never deploys.

### 8.1 `set-env`

Outputs `ENVIRONMENT` (from `github.ref_name`), `NAMESPACE` (`barhost-<env>`),
and per-component change flags.

| Component | Source dir | Image |
| --- | --- | --- |
| spads | `docker/spads/`, `config/` | `ghcr.io/S3KCentrifugal/barhost-spads-<env>` |
| updater | `docker/updater/` | `ghcr.io/S3KCentrifugal/barhost-updater-<env>` |

`config/` is deliberately a build input for the spads image: a rotation or
message change should produce a new image and a clean rollback point, not a live
edit.

### 8.2 `build`

Conditional per component, `docker/build-push-action@v6`, `cache-from: type=gha`,
tagged `:${{ github.sha }}`.

The spads image downloads the pinned engine release at build time. Pin it
explicitly — an engine change must be a reviewable diff, because it silently
invalidates the AI interface pairing (§5.3).

### 8.3 `deploy`

Runs in `ghcr.io/S3KCentrifugal/mcgill-deployer:latest` with `--privileged`
(WireGuard), `permissions: { contents: read, packages: read }`.

The 12-step process applies unchanged, with three project-specific notes:

- **Step 6 (host directories).** Create all three volume paths under
  `/mnt/data2/k8s-volumes/barhost/<env>/`. `spring-data` is large; confirm free
  space on `/mnt/data2` before first deploy.
- **Step 11 (rollout restart).** Guard it. A restart during a live match
  disconnects players. Check JsonStatus for an active game and either wait or
  fail the deploy with a clear message. **Never restart blindly.**
- **Step 12 (rollout wait).** 120s is too short — content volumes are large and
  SPADS must reach the lobby server before readiness passes. Use **300s**.

`sed` substitution order for `IMAGE_TAG` must match the `images:` order in the
overlay: spads first, updater second.

VPN teardown in an `always()` step.

### 8.4 Deviations from the standard

| Standard | Here | Why |
| --- | --- | --- |
| RollingUpdate | `Recreate` | hostNetwork port conflict |
| HTTP health probes | exec probes | SPADS has no HTTP endpoint |
| 120s rollout wait | 300s | large volumes, lobby handshake |
| Ingress per service | none public | UDP, not HTTP |
| Unconditional rollout restart | guarded on active game | player-facing disruption |

---

## 9. Secrets

One `Secret` per namespace, gitignored, applied manually. Template at
`k8s/base/secrets.yaml.example`.

| Key | Purpose |
| --- | --- |
| `SPADS_LOBBY_LOGIN` | bot account |
| `SPADS_LOBBY_PASSWORD` | bot account |
| `SPADS_OWNER_LOBBY_LOGIN` | owner/admin account |
| `SPADS_REGISTRATION_EMAIL` | account registration |
| `RCLONE_DRIVE_TOKEN` | map sync |

Non-secret values — port bases, instance counts, battle name prefix, rotation and
message settings — belong in the `ConfigMap`, not here.

GitHub secrets are the standard seven: `WG_PRIVATE_KEY`, `WG_SERVER_PUBLIC_KEY`,
`WG_ENDPOINT`, `WG_CLIENT_ADDRESS`, `WG_ALLOWED_IPS`, `KUBE_CONFIG_DATA`,
`GHCR_PULL_TOKEN`. The last must be a long-lived PAT — `GITHUB_TOKEN` expires
with the workflow and causes `ImagePullBackOff` on later pod restarts.

---

## 10. Risks and open questions

Ordered by how much they could invalidate the design.

**1. Do AI count toward the 8-player non-botflag cap?** *Unresolved, and
decisive.* If they do, 8 BARb exceeds the cap and nothing works until a botflag
is granted. **Resolve this before building anything** — ask in
`#teiserver-spads`.

**2. Router UDP forwarding.** Outside Kubernetes and outside this repo. Without
UDP 53200–53205 forwarded to `mcgill-data`, the room is visible but unjoinable.
Verify with an external client before declaring the service working.

**3. Third-party autohosts are discouraged by default.** BAR asks to be contacted
first. This is a social prerequisite, not a technical one, but it gates public
operation.

**4. Resource sizing is unmeasured.** §7.5 is an estimate. Insufficient CPU
degrades simulation speed rather than failing loudly.

**5. Home uplink bandwidth.** The relay is upload-heavy and shares a consumer
connection with everything else on it.

**6. Engine/AI version coupling.** Mitigated by pinning both in one image, but an
engine bump without a matching AI produces dead AI at match start with no useful
error.

**7. SPADS is being superseded.** `recoil-autohost` + Tachyon is the future.
Keeping customisation to configuration bounds the migration cost.

**8. Single node, single replica.** No redundancy by design. Node maintenance
means downtime.

---

## Related

- `../rjm.bar.docs/AGENTS.md` — repository layout and sibling projects
- `../s3k-CircuitAI/doc/roles/` — BARb AI role behaviour
- [`spads_config_bar`](https://github.com/beyond-all-reason/spads_config_bar) — BAR's reference Docker configuration
- [`BAR-Devtools`](https://github.com/beyond-all-reason/BAR-Devtools) — local Teiserver + SPADS + client, for testing before public deployment
