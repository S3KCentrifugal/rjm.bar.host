# rjm.bar.host

Containerised Beyond All Reason headless autohost. Opens a multiplayer lobby on
BAR's official lobby server, applies battle settings, adds 8 BARbarian AI, and
starts the game. Deployed to the private k3s cluster by GitHub Actions.

Design: [`docs/autohost-design.md`](docs/autohost-design.md).

## What this is

SPADS (the Spring Perl Autohost) configured for BAR, packaged with a pinned
`engine-headless` build and the BARb Skirmish AI. SPADS is a **client** of BAR's
lobby server — this project operates no lobby infrastructure of its own.

The engine build matters: `engine-dedicated` compiles no `ExternalAI` sources and
cannot host a Skirmish AI. `springServerType:headless` is required for BARb.

## Status

Design complete. Nothing is built.

| Stage | State |
| --- | --- |
| Design document | Done |
| Dockerfiles (`docker/spads`, `docker/updater`) | Not started |
| SPADS config templates (`config/`) | Not started |
| Kustomize base + overlays (`k8s/`) | Not started |
| Deploy workflow (`.github/workflows/deploy.yml`) | Not started |

## Blocking question

**Do AI count toward the 8-player non-botflag lobby cap?** If they do, 8 BARb
exceeds it and a botflag from BAR is required before any of this works. Ask in
`#teiserver-spads` on the BAR Discord before building. See design §10.

## Prerequisites outside this repo

- A dedicated bot lobby account (never a playing account).
- A botflag from BAR devs for rooms above 8 players.
- **Router UDP forward for ports 53200-53205 to `mcgill-data`.** Without it the
  battle room lists publicly and every player times out on join.

## Related repositories

Siblings under `C:\bardev`.

| Path | What it is |
| --- | --- |
| `../rjm.bar.docs` | shared BAR and Recoil game knowledge |
| `../s3k-CircuitAI` | the BARb/CircuitAI fork this hosts |
| `../bar-RecoilEngine` | engine source; read-only reference |
| `../bar-Beyond-All-Reason` | game source; read-only reference |
