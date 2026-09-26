# Backend setup

Everything that has to exist outside this repository before `rjm.bar.host`
can deploy to the cluster and host lobbies: keys, tokens, cluster identities,
GitHub settings and the router. Do it once, in order. Each step ends with a
check.

Nothing here goes into git. Where a value is secret, it goes into a GitHub
repository secret, a Kubernetes Secret, or your password manager — never a
file in this working tree.

| # | What | Who uses it | Stored in |
| --- | --- | --- | --- |
| 1 | GitHub repository settings | — | GitHub |
| 2 | WireGuard peer for CI | deploy job → cluster network | 5 repo secrets `WG_*` |
| 3 | Cluster deploy identity (kubeconfig) | deploy job → Kubernetes API | repo secret `KUBE_CONFIG_DATA` |
| 4 | GHCR pull token | deploy job container; cluster image pulls | repo secret `GHCR_PULL_TOKEN` |
| 5 | Lobby credentials | SPADS | Kubernetes Secret `rjm-host-secrets` |
| 6 | Router UDP forward | players joining lobby games | router |
| 7 | Package access for other repositories | consumers of the image | GitHub package settings |
| 8 | First deploy and verification | — | — |

Commands are for a Linux or WSL shell unless marked PowerShell. `gh` must be
logged in as `S3KCentrifugal`, and cluster-admin steps need an admin
kubeconfig on a machine inside the network (or over your own VPN).

---

## 1. GitHub repository settings

Already done on 2026-09-26, listed so they can be checked or redone:

- **Default branch `test`.** Every push to `test` builds, publishes and deploys
  to namespace `bar-test`.
- **Reusable workflows callable from your other repositories**:
  ```bash
  gh api -X PUT repos/S3KCentrifugal/rjm.bar.host/actions/permissions/access -f access_level=user
  ```

Recommended, not yet done — protect `prod` so it only changes through a
reviewed squash merge from `test`:

```bash
gh api -X PATCH repos/S3KCentrifugal/rjm.bar.host \
  -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=true
gh api -X PUT repos/S3KCentrifugal/rjm.bar.host/branches/prod/protection --input - <<'EOF'
{
  "required_status_checks": { "strict": true, "contexts": ["lint", "image"] },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
```

`lint` and `image` are the `ci` workflow's job names, so a pull request into
`prod` must pass them before it can merge. Branch protection on a private
repository needs a paid GitHub plan; without it, the rule "only squash-merge
from test" is yours to keep.

**Check:** `gh repo view S3KCentrifugal/rjm.bar.host --json defaultBranchRef -q .defaultBranchRef.name` prints `test`.

---

## 2. WireGuard peer for CI

The deploy job runs on a GitHub-hosted runner, joins the cluster network over
WireGuard, then talks to the Kubernetes API.

**GitHub secrets cannot be read back or copied between repositories.** The
`mcgill-software` repositories' `WG_*` secrets are not available here; you
need the values themselves. Give this repository **its own peer** rather than
reusing another runner's key and address: two tunnels with the same private key
and address knock each other off whenever their deploys overlap.

### 2.1 Generate the key pair

On any machine with `wireguard-tools`:

```bash
umask 077
wg genkey | tee bar-ci.key | wg pubkey > bar-ci.pub
cat bar-ci.pub          # public: goes on the gateway
```

PowerShell with the WireGuard for Windows app installed:

```powershell
$wg = "C:\Program Files\WireGuard\wg.exe"
$priv = & $wg genkey
$priv | & $wg pubkey      # public: goes on the gateway
```

Keep the private key only until it is stored as a secret (§2.3), then delete
the file.

### 2.2 Add the peer to the gateway

On the WireGuard gateway (the host behind `WG_ENDPOINT`), choose an unused
tunnel address, e.g. `10.8.0.12`, and add the peer. Live, without a restart:

```bash
sudo wg set wg0 peer "<contents of bar-ci.pub>" allowed-ips 10.8.0.12/32
```

and permanently, in the gateway's `/etc/wireguard/wg0.conf`:

```ini
[Peer]
# rjm.bar.host GitHub Actions
PublicKey = <contents of bar-ci.pub>
AllowedIPs = 10.8.0.12/32
```

While there, note the three values the client needs:

| Value | Where to find it on the gateway |
| --- | --- |
| server public key | `sudo wg show wg0 public-key` |
| endpoint | the gateway's public address and `ListenPort`, e.g. `203.0.113.5:51820` |
| routed networks | the tunnel subnet and the LAN that holds the Kubernetes API, e.g. `10.8.0.0/24, 192.168.2.0/24` |

The Kubernetes API server's address (the `server:` line of your admin
kubeconfig) must fall inside the routed networks.

### 2.3 Store the secrets

```bash
R=S3KCentrifugal/rjm.bar.host
gh secret set WG_PRIVATE_KEY       -R $R < bar-ci.key
gh secret set WG_CLIENT_ADDRESS    -R $R --body "10.8.0.12/32"
gh secret set WG_SERVER_PUBLIC_KEY -R $R --body "<server public key>"
gh secret set WG_ENDPOINT          -R $R --body "<host>:<port>"
gh secret set WG_ALLOWED_IPS       -R $R --body "<routed networks>"
shred -u bar-ci.key 2>/dev/null || rm -f bar-ci.key
```

`gh secret set` without `--body` reads the value from stdin or prompts for
it, so secrets never land in shell history.

**Check (after §3):** the deploy job's "Configure kubectl" step prints the
server version. If it times out, the tunnel is up but the API address is not
in `WG_ALLOWED_IPS`, or the gateway does not forward to it.

---

## 3. Cluster deploy identity

### Option A — reuse the existing runner identity (quick)

The `mcgill-software` pipelines authenticate as client certificate user
`runner`, bound to ClusterRole `github-runner-role`
(`C:\dev\mcgill.k8s.config`). If you still have that kubeconfig, it works here
unchanged:

```bash
base64 -w0 < runner.kubeconfig | gh secret set KUBE_CONFIG_DATA -R S3KCentrifugal/rjm.bar.host
```

Know what it grants: `github-runner-role` can create and bind any Role or
ClusterRole (`bind`, `escalate`), so it is effectively cluster-admin. Anything
that can run this repository's deploy job — including a compromised
dependency of it — gets that.

### Option B — a dedicated, scoped identity (recommended)

A ServiceAccount `bar-deployer` that can do exactly what `deploy.yml` does:
anything inside `bar-test` and `bar-prod`, plus the cluster-scoped objects the
overlays own (their Namespace and PersistentVolumes). It cannot touch any
other namespace, and it cannot grant itself more.

As a **cluster admin**, create the namespaces, the identity and its
permissions:

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata: { name: bar-ci }         # holds only the deployer's identity
---
apiVersion: v1
kind: Namespace
metadata: { name: bar-test }
---
apiVersion: v1
kind: Namespace
metadata: { name: bar-prod }
---
apiVersion: v1
kind: ServiceAccount
metadata: { name: bar-deployer, namespace: bar-ci }
---
# A long-lived token for the ServiceAccount. Rotate it by deleting and
# re-creating this Secret, then updating KUBE_CONFIG_DATA.
apiVersion: v1
kind: Secret
metadata:
  name: bar-deployer-token
  namespace: bar-ci
  annotations: { kubernetes.io/service-account.name: bar-deployer }
type: kubernetes.io/service-account-token
---
# Cluster-scoped objects the overlays apply. Create cannot be limited by name
# in RBAC; get/patch/delete can.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: { name: bar-deployer }
rules:
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["namespaces"]
    resourceNames: ["bar-test", "bar-prod"]
    verbs: ["patch", "update"]
  - apiGroups: [""]
    resources: ["persistentvolumes"]
    verbs: ["get", "list", "create"]
  - apiGroups: [""]
    resources: ["persistentvolumes"]
    resourceNames:
      - rjm-host-data-pv-test
      - rjm-host-out-pv-test
      - rjm-host-ai-pv-test
      - rjm-host-data-pv-prod
      - rjm-host-out-pv-prod
      - rjm-host-ai-pv-prod
    verbs: ["patch", "update", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: { name: bar-deployer }
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: bar-deployer }
subjects: [{ kind: ServiceAccount, name: bar-deployer, namespace: bar-ci }]
---
# Inside each environment: the built-in "admin" role (workloads, secrets,
# network policies, pods/exec; not the namespace itself or quotas).
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: bar-deployer, namespace: bar-test }
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: admin }
subjects: [{ kind: ServiceAccount, name: bar-deployer, namespace: bar-ci }]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: bar-deployer, namespace: bar-prod }
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: admin }
subjects: [{ kind: ServiceAccount, name: bar-deployer, namespace: bar-ci }]
EOF
```

Then build its kubeconfig from the token and the cluster's CA, using the API
address from your admin kubeconfig (it must be reachable through the VPN of
§2):

```bash
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
kubectl get secret bar-deployer-token -n bar-ci -o jsonpath='{.data.ca\.crt}' | base64 -d > ca.crt
TOKEN=$(kubectl get secret bar-deployer-token -n bar-ci -o jsonpath='{.data.token}' | base64 -d)

umask 077
KUBECONFIG=bar-deployer.kubeconfig kubectl config set-cluster mcgill --server="$SERVER" \
  --certificate-authority=ca.crt --embed-certs=true
KUBECONFIG=bar-deployer.kubeconfig kubectl config set-credentials bar-deployer --token="$TOKEN"
KUBECONFIG=bar-deployer.kubeconfig kubectl config set-context bar --cluster=mcgill \
  --user=bar-deployer --namespace=bar-test
KUBECONFIG=bar-deployer.kubeconfig kubectl config use-context bar
unset TOKEN
```

**Check** what it can and cannot do, before storing it:

```bash
export KUBECONFIG=bar-deployer.kubeconfig
kubectl auth can-i create jobs -n bar-test        # yes
kubectl auth can-i create deployments -n bar-prod # yes
kubectl auth can-i create persistentvolumes       # yes
kubectl auth can-i get secrets -n bar-test        # yes (its own environments)
kubectl auth can-i get secrets -n mcgill          # no
kubectl auth can-i create clusterrolebindings     # no
kubectl auth can-i delete namespaces              # no
unset KUBECONFIG
```

Store it, then delete the local copies:

```bash
base64 -w0 < bar-deployer.kubeconfig | gh secret set KUBE_CONFIG_DATA -R S3KCentrifugal/rjm.bar.host
shred -u bar-deployer.kubeconfig ca.crt 2>/dev/null || rm -f bar-deployer.kubeconfig ca.crt
```

**Pod security.** The deploy job's "Ensure host directories" step runs a
short-lived `busybox` pod as root with a `hostPath` mount of `/mnt/data2`, to
create and `chown` the volume directories. RBAC allows it (the `admin` role
creates pods); if you add Pod Security Admission labels to these namespaces,
use `privileged` for `bar-test`/`bar-prod`, or create the directories by hand
instead (§8.1) and drop that step.

---

## 4. GHCR pull token

One classic personal access token serves two purposes:

- the deploy job's container, `ghcr.io/mcgill-software/mcgill-deployer`, is a
  private package in another account;
- the cluster pulls this repository's private image,
  `ghcr.io/s3kcentrifugal/rjm-host`, with the pull secret the deploy job
  creates from it (`mcgill-k8s-pull-2026` in each namespace).

It must be **classic** (fine-grained tokens do not cover GHCR packages) and
**long-lived** (the cluster uses it on every pod start, long after the workflow
that created the pull secret has ended — `GITHUB_TOKEN` would expire).

1. GitHub → Settings → Developer settings → Personal access tokens → Tokens
   (classic) → Generate new token (classic).
2. Note: `rjm.bar.host cluster pulls`. Expiration: your choice (no expiry, or a
   long one with a calendar reminder). Scope: **`read:packages` only**.
3. The account that creates it must be able to read both packages: yours, and
   a member of `mcgill-software` with access to `mcgill-deployer`.
4. Store it:
   ```bash
   gh secret set GHCR_PULL_TOKEN -R S3KCentrifugal/rjm.bar.host
   ```

**Check:** `echo <token> | docker login ghcr.io -u S3KCentrifugal --password-stdin`,
then `docker pull ghcr.io/mcgill-software/mcgill-deployer:latest` and
`docker pull ghcr.io/s3kcentrifugal/rjm-host:test` both succeed.

When it expires, set a new value and rerun the deploy: the job recreates the
pull secret in the namespace every time.

---

## 5. Lobby credentials (SPADS)

SPADS needs three names and two passwords. Until the Secret exists, the deploy
holds SPADS at zero replicas; the simulation Job runs regardless.

| Key | Value |
| --- | --- |
| `SPADS_LOBBY_LOGIN` | the whitelisted secondary account SPADS logs in as |
| `SPADS_LOBBY_PASSWORD` | its password |
| `SPADS_OWNER_LOBBY_LOGIN` | **your main account**: SPADS gives it owner rights (`!addBot`, `!force`, `!forceStart`). Must not be empty. |
| `SPADS_BATTLE_PASSWORD` | the room password; `*` makes the room public |

Create it directly, so the password never touches a file (the namespace must
exist — it does after §3 option B, or after the first deploy):

```bash
read -rsp "Lobby password: " LOBBY_PW; echo
read -rsp "Room password: "  ROOM_PW;  echo
kubectl create secret generic rjm-host-secrets -n bar-test \
  --from-literal=SPADS_LOBBY_LOGIN='<autohost account>' \
  --from-literal=SPADS_LOBBY_PASSWORD="$LOBBY_PW" \
  --from-literal=SPADS_OWNER_LOBBY_LOGIN='<your account>' \
  --from-literal=SPADS_BATTLE_PASSWORD="$ROOM_PW"
unset LOBBY_PW ROOM_PW
kubectl label secret rjm-host-secrets -n bar-test app.kubernetes.io/part-of=rjm-host
```

The file route also works: copy `k8s/base/secrets.yaml.example` to
`k8s/overlays/test/secrets.yaml` (gitignored), fill it in, `kubectl apply -f`
it, and delete the file.

Repeat with `-n bar-prod` for prod. Test and prod need **different lobby
accounts** if both run SPADS at once: an account can be logged in only once.

**Check:** after the next deploy, `kubectl logs deploy/rjm-host-spads -n bar-test -c spads`
shows the lobby login, and `kubectl get pods -n bar-test` shows the SPADS pod
`1/1 Ready` (ready means it holds its lobby connection). SPADS warns in its log
at login if the account lacks the bot flag.

**Rotate:** `kubectl delete secret rjm-host-secrets -n bar-test`, create it
again, then `kubectl rollout restart deployment/rjm-host-spads -n bar-test` —
not during a game.

---

## 6. Router UDP forward

Players connect straight to the engine over UDP. The SPADS pod listens on the
node itself (`hostNetwork`):

| Environment | Game port (UDP) | Forward to |
| --- | --- | --- |
| test | 8452 | `mcgill-data` |
| prod | 8462 | `mcgill-data` |

On the router, forward each **UDP** port from the public address to
`mcgill-data`'s LAN address, same port number. Only UDP; the SPADS control port
(8454/8464) must never be forwarded.

The public address players are sent to is the one the lobby server sees for
SPADS's outbound connection, so the forward must be on the router whose public
IP the node uses for outbound traffic.

Without the forward, the room is listed and people on your LAN can join, but
nobody from outside can.

**Check:** host a game, have someone outside join; or from outside,
`nc -u -v <public ip> 8452` while a game is loading shows the port open (UDP
checks are unreliable; a real join is the proof).

---

## 7. Package access for other repositories

The image is private. For another of your repositories to pull it in its
workflows:

1. GitHub → your profile → **Packages** → `rjm-host` → **Package settings**.
2. **Manage Actions access** → **Add repository** → pick it → role **Read**.
3. In that repository's workflow: `permissions: { packages: read }`, log in to
   `ghcr.io` with its own `GITHUB_TOKEN`, then `docker run` the image, or call
   `S3KCentrifugal/rjm.bar.host/.github/workflows/simulate.yml@test`.

Candidates in this workspace: `rjm.bar.rustai` (simulate its AI),
`rjm.bar.telemetry` (consume replays), `CircuitAI` (simulate each release).

For a machine outside GitHub Actions, log in with a classic PAT that has
`read:packages`.

---

## 8. First deploy and verification

### 8.1 Host directories

The deploy job creates these itself (§3, pod security note). By hand, on
`mcgill-data`:

```bash
sudo mkdir -p /mnt/data2/k8s-volumes/rjm-host/{test,prod}/{data,out,ai}
sudo chown -R 10001:10001 /mnt/data2/k8s-volumes/rjm-host
```

### 8.2 Capacity

The simulation Job requests 2 CPU / 8 GiB (limit 6 CPU / 16 GiB), and SPADS
the same. `mcgill-data` also runs the cluster's databases. Before the first
deploy, compare what is free:

```bash
kubectl describe node mcgill-data | sed -n '/Allocatable/,/Events/p'
```

If 4 CPU / 16 GiB of requests will not fit next to what is already requested,
lower the requests in `k8s/base/match-job.yaml` and
`k8s/base/spads-deployment.yaml` before deploying.

### 8.3 Deploy

```bash
gh workflow run deploy.yml -R S3KCentrifugal/rjm.bar.host --ref test -f force_build=true
gh run watch -R S3KCentrifugal/rjm.bar.host
```

Or push any commit to `test`.

### 8.4 Verify

```bash
kubectl get all,pvc -n bar-test
kubectl get pv | grep rjm-host
kubectl logs job/rjm-host-match -n bar-test -c match --tail=30
```

Expect:

- the Job `Complete`;
- three PVCs `Bound` to `rjm-host-*-pv-test`;
- the log ending `finished: game over`;
- on the node, `/mnt/data2/k8s-volumes/rjm-host/test/out/matches/<pod>/`
  holding `result.json`, `infolog.txt` and `demos-server/*.sdfz`.

### 8.5 Load a custom AI folder (optional)

Linux builds only:

```bash
scripts/upload_ai.sh test ./SMRTBARb
```

Used by the next match, and by SPADS after its next restart.

---

## Rotation and revocation summary

| Credential | Rotate | Revoke immediately |
| --- | --- | --- |
| WireGuard peer | new key pair (§2.1), `wg set` the new public key, update `WG_PRIVATE_KEY` | `sudo wg set wg0 peer <public key> remove` on the gateway |
| `bar-deployer` token | delete and re-create `bar-deployer-token`, rebuild kubeconfig, update `KUBE_CONFIG_DATA` | `kubectl delete secret bar-deployer-token -n bar-ci` |
| GHCR pull token | new classic PAT, update `GHCR_PULL_TOKEN`, redeploy | delete the token in GitHub settings (running pods keep their pulled images) |
| Lobby password | change it in the lobby, then §5 rotate | change it in the lobby |
