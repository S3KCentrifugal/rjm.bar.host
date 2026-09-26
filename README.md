# rjm.bar.host

Containerised Beyond All Reason headless host, deployed to the private k3s
cluster by GitHub Actions.

Its job is to make AI development fast: change the AI, and CI builds it,
plays a full match against itself in a container, and hands back a replay and a
result — without anyone setting up a machine.

Design: [`docs/autohost-design.md`](docs/autohost-design.md). Agent and
contributor rules: [`AGENTS.md`](AGENTS.md).

## What this is

A Kubernetes `Job` that runs one AI-vs-AI Beyond All Reason match to completion
and reports the outcome.

The engine and the Skirmish AI are compiled **from a single source tree** in
Recoil's own pinned build container. That is not a convenience: CircuitAI's
`CMakeLists.txt` reaches into the engine tree for its sources, and its README
records that mismatched `libSkirmishAI.so` / `libAIInterface.so` produce an AI
that is silently dead at match start. Building both together makes that
impossible rather than merely unlikely.

`engine-dedicated` compiles no `ExternalAI` sources and cannot host a Skirmish
AI, so the headless build is required.

There is no lobby server, no SPADS, no inbound UDP and no exposed port. A
public player-facing autohost is designed but deliberately not built — see
design §10 for what it would cost.

## The loop

```
push to the AI repo
  -> repository_dispatch: ai-updated
    -> engine-build   (cached layer; only rebuilt when the engine pin moves)
    -> ai-build       (recompiles the AI alone against that exact engine)
    -> runtime image  -> GHCR
      -> Job on mcgill-data: fetch content, play the match
        -> replay + result.json on the runs volume
```

A match runs at an elevated simulation speed, so it finishes in a fraction of
real time.

## Layout

| Path | Contents |
| --- | --- |
| `docker/runtime/Dockerfile` | multi-stage build: engine → AI → runtime image; fails the build if the AI will not load |
| `scripts/build-engine-ai.sh` | `docker buildx` wrapper for local builds and for extracting the install tree |
| `scripts/run_match.py` | generates the GameSetup script, drives the match over the Autohost Protocol, exits when it ends |
| `scripts/measure_match.py` | runs a match and samples CPU, RSS and threads — how the resource numbers were derived |
| `scripts/demo_stats.py` | per-team units built/killed, metal and damage from a replay — the objective scoreboard for comparing AI builds |
| `config/engine.pin` | the engine commit and AI ref everything is built from |
| `k8s/base/`, `k8s/overlays/{test,prod}/` | Kustomize Job, PVCs, PVs |
| `.github/workflows/deploy.yml` | `set-env` → `build` → `deploy` |

## Status

Phase 1 is implemented and not yet run on the cluster.

| Stage | State |
| --- | --- |
| Design document | Done |
| Multi-stage build | Written, not yet run in CI |
| Match runner | **Exercised against the real engine**, see below |
| Runtime image | Written, not yet built |
| Kustomize base + overlays | Written; both overlays render |
| Deploy workflow | Written, not yet run |
| First real match on the cluster | **Not done** |

## Before the first run

- `AI_REPO_TOKEN` — a PAT that can read the private AI repository. The
  workflow's own `GITHUB_TOKEN` cannot, and this is the one secret beyond the
  cluster's standard seven.
- Check `kubectl describe node mcgill-data` for allocatable CPU and memory
  against what is already requested. The node also runs the shared databases,
  and the match's resource numbers are an unmeasured starting point.
- Decide whether the namespace should be `barhost-<env>` or
  `mcgill-barhost-<env>`; the cluster standard's own references disagree.

## Related repositories

Siblings under `C:\bardev`.

| Path | What it is |
| --- | --- |
| `../s3k-CircuitAI` | the BARb/CircuitAI fork this builds and tests |
| `../rjm.bar.docs` | shared BAR and Recoil game knowledge |
| `../bar-RecoilEngine` | engine source; read-only reference |
| `../bar-Beyond-All-Reason` | game source; read-only reference |
