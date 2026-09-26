# rjm-sims MCP Server — Design

An MCP server that lets an agent (Claude Code, Claude Desktop) bring BAR
simulations and lobby hosts up and down on the k3s cluster, many side by side,
with its power confined to **one namespace**.

Status: design, 2026-09-26. Nothing here is built. It builds on the
`rjm-host` image and its environment contract ([`autohost-design.md`](autohost-design.md)).

---

## 1. What it does

| You say | MCP tool | Kubernetes object |
| --- | --- | --- |
| "Run SMRTBARb v1.961 vs BARb on All That Glitters, 20 times" | `sim_batch` | one **Indexed Job**, 20 completions, N in parallel |
| "Run one match with these modoptions" | `sim_run` | one Job |
| "How did batch b-7f3 go?" | `sim_results` | reads `result.json` of every run |
| "Open a lobby with 8 SMRTBARb, password X" | `lobby_up` | one SPADS Deployment |
| "Close the lobby" | `lobby_down` | deletes it (refuses mid-game unless `force`) |
| "What is running / how much room is left?" | `list`, `capacity` | Jobs, Deployments, ResourceQuota status |

The server holds **no state of its own**: every object it creates carries labels
and annotations describing it, and every read is a Kubernetes query. It can be
restarted, redeployed or run twice without losing track of anything.

---

## 2. The confinement model

"Limited to one namespace" is three layers deep, because the obvious one —
a namespaced Role — is not enough on its own.

### 2.1 Layer 1: RBAC — only this namespace, only these verbs

The server runs as ServiceAccount `rjm-sims` with a **Role** (never a
ClusterRole) in namespace `rjm-sims`:

| Resource | Verbs | Why |
| --- | --- | --- |
| `batch/jobs` | create, get, list, watch, delete | simulations |
| `apps/deployments` | create, get, list, watch, patch, delete | lobbies |
| `configmaps` | create, get, list, delete | batch matrices, lobby settings |
| `pods` | get, list, watch | status |
| `pods/log` | get | `sim_logs` |
| `pods/exec` | create | only to ask a lobby pod "is a game running?" (§5.3) |
| `resourcequotas` | get | `capacity` |

Nothing cluster-scoped: no nodes, no PVs, no namespaces, no RBAC, and **no
`secrets`** — the server never reads a credential; pods reference Secrets by
name.

### 2.2 Layer 2: admission policy — pods can only be what the server means

A Role that can create Jobs can create *any* pod in the namespace: any image,
any command, mounting any Secret there. With the Role alone, one prompt-injected
tool call could run a pod that prints the lobby passwords. So the namespace gets
a **ValidatingAdmissionPolicy** (CEL, built into Kubernetes ≥ 1.30 — check the
cluster's k3s version first) that rejects any pod in `rjm-sims` unless:

- every container image is `ghcr.io/s3kcentrifugal/rjm-host@sha256:…` or
  `…/rjm-host:<tag>` from an allowlist;
- `command` is unset and `args[0]` is one of `match`, `spads`, `fetch-content`
  — the image's entrypoint otherwise runs *any* command (`exec "$@"`);
- no `hostNetwork`, `hostPID`, `hostIPC`, `privileged`, added capabilities or
  `hostPath` volumes;
- `hostPort` appears only on pods labelled `rjm-sims/kind=lobby`, only in the
  lobby port range (§5.2);
- the lobby-accounts Secret is mounted only by lobby pods;
- `runAsNonRoot` and uid 10001.

The same policy binds to *everyone* acting in the namespace, not just the
server, so it also protects against a mistake in the server's own code.

### 2.3 Layer 3: ResourceQuota and LimitRange — it cannot starve the node

`mcgill-data` is a single node that also runs the cluster's databases. The
namespace gets a hard ceiling that no tool call can exceed:

```yaml
kind: ResourceQuota        # starting values; confirm against node allocatable
spec:
  hard:
    requests.cpu: "8"
    requests.memory: 32Gi
    limits.cpu: "16"
    limits.memory: 64Gi
    count/jobs.batch: "40"
    count/deployments.apps: "4"
    pods: "24"
```

plus a LimitRange with default requests, so a pod can never be created without
them. The server reads the quota's `status.used` for `capacity` and refuses a
request that would not fit, with a message saying how much room is left —
better than letting the API server reject pods one by one.

### 2.4 The server's own tool surface is typed, not free-form

No tool takes an image, a command, raw YAML or arbitrary environment
variables. Tools take typed arguments that the server validates and maps onto
the image's `BARHOST_*` / `SPADS_*` contract:

- AI sources must match `github:<allowlisted owner>/<repo>@<tag>` or be the
  name of a folder already on the custom-AI volume;
- maps are chosen from what is on the content volume (`list_maps`);
- modoptions are `key=value` pairs matching `^[a-z0-9_]+=[A-Za-z0-9_.:-]*$`;
- counts, speeds and timeouts are bounded.

---

## 3. Where it runs

**In the cluster**, as a Deployment in `rjm-sims`, reached over the
**private** ingress (`haproxy-private`, VPN only) with MCP's Streamable HTTP
transport:

```text
Claude Code ──VPN──▶ haproxy-private ──▶ rjm-sims-mcp (Deployment, SA rjm-sims)
                                           │ Kubernetes API (namespace-scoped token)
                                           ▼
                            Jobs / Deployments in rjm-sims
```

- The Kubernetes credential never leaves the cluster: it is the pod's own
  ServiceAccount token.
- The MCP endpoint requires a bearer token (a Secret in the namespace, given to
  Claude Code once):
  `claude mcp add --transport http rjm-sims https://sims.<domain>/mcp --header "Authorization: Bearer <token>"`.
  MCP's OAuth flow is the upgrade path if more than one person uses it.
- It sits on the private ingress, so it is unreachable without the VPN, and
  TLS comes from cert-manager like every other service here.

**Local stdio mode** for development: the same server run on a laptop with a
kubeconfig for the `rjm-sims` ServiceAccount. Layers 1-3 still apply, because
they are enforced by the cluster, not by the server.

Rejected: giving Claude Code a kubeconfig and letting it run `kubectl`. That
throws away layer 2.4 and makes every call a free-form shell command.

---

## 4. Simulations

### 4.1 One run: `sim_run`

Creates a Job from a fixed template: the `rjm-host` image, `args: ["match"]`,
the `BARHOST_*` variables from the tool arguments, the content volume
read-only, the output volume, `backoffLimit: 0`, `activeDeadlineSeconds` from
the timeout, `ttlSecondsAfterFinished: 86400`. Returns an id like `s-20260926-a1b2`.

### 4.2 Many side by side: `sim_batch` as an Indexed Job

A batch is **one Job** with `completionMode: Indexed`, `completions: N` and
`parallelism: P`. Kubernetes then does the scheduling: it runs P matches at
once and starts the next as each finishes, with no queue in the server.

```text
sim_batch(matrix=[{ai:"SMRTBARb", vs:"BARb", map:"All That Glitters v2.2.3"}], repeats=20, parallel=4)
   └─▶ ConfigMap b-7f3-matrix   (runs.json: 20 entries, one per index)
   └─▶ Job b-7f3                completions=20, parallelism=4
          pod index 0 ─▶ runs.json[0] ─▶ /bar/out/batches/b-7f3/000/
          pod index 1 ─▶ runs.json[1] ─▶ /bar/out/batches/b-7f3/001/
          ...
```

This needs one small addition to the image's entrypoint: if
`BARHOST_MATRIX_FILE` is set, read entry `$JOB_COMPLETION_INDEX` and export its
fields as `BARHOST_*` before running the match. Everything else is the existing
`match` mode.

`parallel` defaults to what fits in the quota at the batch's per-pod requests;
the server reports that number rather than guessing.

### 4.3 Content is fetched once, not per pod

Twenty pods each running `pr-downloader` against the same pool is wasted work
(the review found it safe, just redundant). The namespace keeps a warm content
volume: a `fetch-content` Job runs when a batch names a game tag or map not
yet present, and simulation pods mount the volume **read-only** with
`BARHOST_FETCH_CONTENT=0`. Read-only also means no simulation can corrupt the
pool for the others.

### 4.4 Results

Every run writes the usual `result.json`, infolog, engine console and replays
under `/bar/out/batches/<batch>/<index>/`. The MCP server mounts the output
volume **read-only** and serves them:

- `sim_results(batch)` — one table: index, AI per side, map, winner, game
  frames, wall-clock, exit reason; plus totals (wins per AI, failures).
- MCP **resources** for the files: `rjm-sims://batches/b-7f3/003/result.json`,
  `…/infolog.txt`, `…/replay` (the replay's path and size; replays are
  megabytes and are fetched with `kubectl cp` or from the node, not streamed
  through the model).

### 4.5 Sizing, and a limit that applies today

Per-pod defaults, to be measured with `scripts/measure_match.py` before being
trusted: 1v1 requests 1 CPU / 2 GiB, limits 3 CPU / 6 GiB. The 8-AI numbers in
the host design (2.8 cores, 6 GiB peak) are the upper bound. At those requests
the quota above fits about 8 simultaneous 1v1s.

Until design risk 1 in `autohost-design.md` is fixed, **all-AI team games end
within seconds**. The tools should refuse team layouts (or run them and flag
every result as invalid) rather than return meaningless win rates.

---

## 5. Lobbies

### 5.1 `lobby_up`

Creates a SPADS Deployment (`strategy: Recreate`, one replica, readiness via
`ready.sh`, no liveness probe) from the same image with `args: ["spads"]`, a
ConfigMap of room settings (name, map, allowed AIs, auto-added bots, bot
bonus), and one lobby account (§5.4). Returns the lobby id, its room name, and
the UDP port players connect to.

### 5.2 `hostPort`, not `hostNetwork`, and a port pool

Several lobbies on one node each need their own public UDP port. Two changes
from the current `rjm-host-spads` Deployment:

- **`hostPort` instead of `hostNetwork`.** The pod keeps its own network
  namespace: NetworkPolicy applies, it cannot see or reach the node's
  loopback, the SPADS autohost port stays private to the pod (no collisions
  between lobbies), and the scheduler refuses a second pod on a taken port
  instead of letting it crash. Only the game port is published, UDP.
  *To verify on k3s first:* UDP hostPort goes through the CNI `portmap`
  plugin; confirm a player can join through it, and that the lobby server sees
  the same public address for SPADS's outbound connection.
- **A port pool**, e.g. `8500-8519/udp`, forwarded on the router **once** as a
  range. The server assigns the lowest free port (known from the existing
  Deployments' labels) and passes it as `SPADS_GAME_PORT`.

The existing `rjm-host-spads` in `bar-<env>` would benefit from the same
change.

### 5.3 `lobby_down` never ends a game by accident

Before deleting, the server runs the same check the deploy pipeline uses: a
fixed `exec` into the lobby pod looking for a `spring-headless` process. If a
game is running it refuses, unless `force=true`. The tool is annotated
`destructiveHint`, so the client asks before calling it. `pods/exec` is the
most powerful verb in the Role; the admission policy (§2.2) is what keeps it
from mattering, since every pod it could exec into is an `rjm-host` pod.

### 5.4 One lobby account per concurrent lobby

**This is the real limit on "many lobbies", and it is not technical.** A lobby
account can be logged in once, so N simultaneous lobbies need N whitelisted
accounts. The accounts live in one Secret (`rjm-sims-lobby-accounts`, keys
`account-1`, `account-2`, …). The server assigns a free one per lobby, and the
pod reads only its own key, selected by index. The alternative is SPADS
`ClusterManager`, which spawns rooms from one account but needs lobby admin
rights. Either way, ask BAR before running more than one.

---

## 6. Tool catalogue

| Tool | Hints | Arguments | Returns |
| --- | --- | --- | --- |
| `capacity` | read-only | — | quota used/free, simultaneous sims that fit, free lobby ports and accounts |
| `list_ais` | read-only | — | AIs on the custom-AI volume, plus recent releases of allowlisted repos |
| `list_maps` | read-only | — | maps and game tags on the content volume |
| `sim_run` | — | ai, opponent, map, speed, modoptions, ai_options, timeout | sim id |
| `sim_batch` | — | matrix or ai/opponent/maps, repeats, parallel | batch id, completions, parallelism |
| `sim_list` | read-only | state filter | runs and batches with progress |
| `sim_status` | read-only | id | phase, pod states, succeeded/failed counts |
| `sim_logs` | read-only | id, index, tail | last lines, `[Progress]` noise stripped |
| `sim_results` | read-only | batch or id | per-run table + totals |
| `sim_cancel` | destructive | id | deletes the Job; finished output is kept |
| `lobby_up` | — | name, map, password, allowed AIs, bots, bonus | lobby id, UDP port, room name |
| `lobby_list` | read-only | — | lobbies, ready state, game in progress |
| `lobby_status` | read-only | id | readiness, players and bots from the SPADS log |
| `lobby_down` | destructive | id, force | refuses mid-game without force |
| `content_fetch` | — | game tag, maps | fetch Job id |

Every created object is labelled `app.kubernetes.io/managed-by=rjm-sims-mcp`,
`rjm-sims/kind=sim|batch|lobby`, `rjm-sims/id=…`, and the server only ever
lists, reads or deletes objects carrying that label.

---

## 7. Build and deploy

| Choice | Decision | Why |
| --- | --- | --- |
| Language | Python, official MCP SDK (`FastMCP`, Streamable HTTP) + `kubernetes` client | matches the repository's tooling; the SDK does transport and schemas |
| Location | `mcp/` in this repository | it is bound to the `rjm-host` image's env contract; changing one without the other must be one reviewed diff |
| Image | `ghcr.io/s3kcentrifugal/rjm-sims-mcp` | a second component in the existing `set-env → build → deploy` pipeline |
| Namespace | `rjm-sims` (one, as asked) | the Role, admission policy, quota and workloads all live there |
| Storage | new PVs under `/mnt/data2/k8s-volumes/rjm-sims/{data,out,ai}` | separate from `bar-<env>`, so a batch cannot fill the host's volumes |
| Ingress | `haproxy-private`, cert-manager TLS | VPN-only |
| Probes | `/healthz` (process) and `/readyz` (API reachable) | a normal long-running service |

The Role, RoleBinding, admission policy and binding, quota and LimitRange are
applied by the pipeline's deploy job, **not** by the MCP server. The server
cannot widen its own permissions.

---

## 8. Build order

1. **Namespace guard rails**: namespace, Role, quota, LimitRange, admission
   policy. Prove them with `kubectl auth can-i --as=system:serviceaccount:rjm-sims:rjm-sims`
   and by trying to create a forbidden pod.
2. **Image change**: `BARHOST_MATRIX_FILE` + `JOB_COMPLETION_INDEX` in the
   entrypoint; output under `/bar/out/batches/<batch>/<index>`.
3. **Read-only tools + `sim_run`/`sim_batch`/`sim_results`**, stdio mode first.
4. **In-cluster deployment** behind the private ingress with the bearer token.
5. **Lobbies**: prove UDP `hostPort` joinability with one lobby, then the port
   pool and account pool.

## 9. Open questions

1. **k3s version**: ValidatingAdmissionPolicy needs Kubernetes ≥ 1.30. On an
   older cluster, layer 2 becomes a small Kyverno install or a validating
   webhook — or stays missing, which leaves the Role's pod-creation right broad.
2. **Node capacity**: the quota numbers are placeholders until `kubectl describe
   node mcgill-data` is compared with what is already requested.
3. **Lobby accounts**: how many whitelisted accounts BAR will allow.
4. **UDP hostPort** through k3s's CNI: verify before building the lobby pool.
5. **Team-game collapse** (host design, risk 1) limits sims to 1v1 until fixed.
