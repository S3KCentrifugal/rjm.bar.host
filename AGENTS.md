# rjm.bar.host - Agent Instructions

These instructions apply to the whole repository and are the canonical guidance
for every coding agent (Claude Code, Codex, Copilot, Cursor, Gemini CLI, Aider,
Cline, and any other). Vendor files route here; they must not duplicate or
override this file.

## Project

A **containerised Beyond All Reason headless host**, deployed to the private
k3s cluster. It has two phases, and they are not the same product:

- **Phase 1, the match harness (built).** A Kubernetes `Job` that plays one
  AI-vs-AI match to completion and reports the result. It exists to make AI
  development fast: change the AI, CI builds it, plays a match, hands back a
  replay. No lobby, no SPADS, no players, no exposed ports.
- **Phase 2, the public lobby autohost (designed, not built).** SPADS as a
  client of BAR's lobby server so humans can play. Deferred because nearly all
  of this project's cost and risk lives there.

**Status: phase 1 implemented, never yet run on the cluster.** The design is
[`docs/autohost-design.md`](docs/autohost-design.md) - read it before writing
anything here.

This project **operates no lobby infrastructure** in either phase.

### Facts an agent must not get wrong

1. **`engine-dedicated` cannot host a Skirmish AI.** It compiles zero
   `ExternalAI` sources (`rts/builds/dedicated/CMakeLists.txt`) and its
   `main.cpp` constructs only `CGameServer`. BARb requires
   `springServerType:headless`. Any design that runs AI on the dedicated build
   is wrong.
2. **The cluster has no UDP path to the internet.** HAProxy ingress is TCP/HTTP
   on NodePorts 32080/32443. Human players would need inbound UDP, which
   requires `hostNetwork: true` plus a **router port-forward to `mcgill-data`**
   - configuration outside this repository. **Phase 1 sidesteps this entirely**:
   both its sockets are loopback and nothing is exposed. Do not reintroduce
   host networking for a workload that has no human players.
3. **Never modify the BAR game archive to make something work here.** Changing
   `../Beyond-All-Reason` alters the archive checksum, so stock clients can no
   longer join. That rules it out for public hosting regardless of merit.
4. **Never interrupt a running match.** In phase 1 the workload is a `Job`, so
   there is no rollout; the pipeline instead refuses to deploy while a match is
   active, and deletes the old Job only once it is finished (a Job's pod
   template is immutable). In phase 2 the same rule returns as
   `strategy: Recreate` plus a guarded rollout restart, because `hostNetwork`
   makes a surging pod collide on ports.
5. **Secrets never enter git.** Lobby credentials, `rclone` tokens, kubeconfigs
   and WireGuard keys are gitignored. Templates end in `.example` or `.sample`.

### Open question (phase 2 only)

**Do AI count toward the 8-player non-botflag lobby cap?** Unresolved. If they
do, 8 AI exceeds it and a botflag from BAR devs is required before a public
lobby host works. Ask in `#teiserver-spads` on the BAR Discord. Do not assume
either answer in design or code.

It **does not block phase 1**, which never contacts the lobby server.

## Repository map

| Path | Content | State |
| --- | --- | --- |
| `docs/autohost-design.md` | the design: both phases, with phase 2 quarantined in §10 | written |
| `docker/runtime/Dockerfile` | multi-stage build: engine, then AI against that engine, then the runtime image | written |
| `scripts/build-engine-ai.sh` | `docker buildx` wrapper for local builds and for extracting the install tree | written |
| `scripts/measure_match.py` | runs a match and samples CPU/RSS/threads; the resource numbers in the design come from it | written |
| `scripts/run_match.py` | generates the GameSetup script, drives the match over the Autohost Protocol, terminates the engine | written |
| `config/engine.pin` | the engine commit and AI ref everything is built from | written |
| `config/` | phase 2 SPADS templates, when that happens | to write |
| `k8s/base/` | rbac, pvc, job, match.env, kustomization | written |
| `k8s/overlays/{test,prod}/` | namespace, persistent volumes, image tags | written |
| `.github/workflows/deploy.yml` | `set-env` -> `build` -> `deploy` | written |

Nothing here has been run on the cluster yet. The first real match is the test
that matters.

**The engine and the AI are built together, always.** `s3k-CircuitAI` cannot
build on its own - its `CMakeLists.txt` reaches into `${CMAKE_SOURCE_DIR}/rts/`
- and shipping a `libSkirmishAI.so` built against a different engine than the
`libAIInterface.so` beside it yields an AI that is dead at match start with no
useful error. Do not introduce a path that builds or downloads either one
separately.

Match settings in `k8s/base/match.env` are a **ConfigMap**, not a build input:
changing a map or a speed should not require a new image. The engine and AI
refs in `config/engine.pin` are the opposite - those are build inputs, and
moving them must be a reviewed diff.

## Skills that govern this project

Deployment work here follows the user's cluster skills. Load the relevant one
before authoring infrastructure rather than inventing conventions:

| Skill | Use for |
| --- | --- |
| `author-k8s-manifests` | Deployments, Services, PVs/PVCs, RBAC, Kustomize base/overlay |
| `convention-kustomize` | manifest naming, labels, claimRef binding, validation |
| `configure-k8s-pipeline` | `.github/workflows/deploy.yml`, the 3-job architecture, WireGuard, the 12-step deploy |
| `convention-deployment` | Dockerfiles, image tagging, health checks, observability |
| `troubleshoot-k8s` | ImagePullBackOff, CrashLoopBackOff, Released PVs, 503s |
| `scaffold-k8s-project` | the new-project checklist, if starting a component from scratch |

Documented deviations from those standards are in the design, §8.4. Keep that
table current: a deviation that is not written down reads as a mistake.

## Workspace

Every project sits side by side under `C:\bardev`. Paths below are relative to
this repository's parent.

| Project | Path | What it is | Write policy |
| --- | --- | --- | --- |
| **rjm.bar.host** | `.` | this repository - containerised BAR autohost | writable |
| **rjm.bar.docs** | `../rjm.bar.docs` | central game knowledge base for BAR + Recoil, shared by all projects | writable; **game facts only** |
| **rjm.bar.tools** | `../rjm.bar.tools` | Rust `smrt-bar-map`: reads `.sd7` archives, parses SMF, derives terrain facts | writable |
| **rjm.bar.ai** | `../rjm.bar.ai` | Rust BAR skirmish AI, first-principles design; requirements capture only | writable |
| **rjm.bar.editor** | `../rjm.bar.editor` | Godot BAR map editor; requirements capture only | writable |
| **s3k-CircuitAI** | `../s3k-CircuitAI` | the user's CircuitAI/BARb fork (`S3KCentrifugal/CircuitAI`, branch `smrt`) - **the AI this project hosts** | writable |
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
| `src/circuit/setup/SetupManager.cpp` | start position and commander selection |

The shipped AI layout this project must reproduce in its image:
`AI/Skirmish/BARb/<version>/{AIInfo.lua, AIOptions.lua, config/, script/, libSkirmishAI.so}`
alongside `AI/Interfaces/C/0.1/{InterfaceInfo.lua, libAIInterface.so}`. On Linux
these are `.so`; a Windows install's `.dll` files are unusable. Both libraries
must come from the **same engine build** or the AI dies at match start.

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
