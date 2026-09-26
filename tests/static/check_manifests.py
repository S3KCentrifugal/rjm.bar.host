#!/usr/bin/env python3
"""Static checks of what `kubectl kustomize` renders for each environment.

Catches the mistakes that only show up on the cluster: a claim binding across
environments, an uppercase image name, test and prod SPADS colliding on the
node's ports, a liveness probe that would kill a lobby game, a lost pipeline
marker, or a credential in a ConfigMap.

Needs kubectl (for its built-in kustomize) and PyYAML.
"""

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ERRORS = []


def check(cond, msg):
    if not cond:
        ERRORS.append(msg)


def render(env):
    out = subprocess.run(["kubectl", "kustomize", str(ROOT / "k8s" / "overlays" / env)],
                         capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def by_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def one(docs, kind, name):
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    check(len(found) == 1, f"expected one {kind}/{name}, found {len(found)}")
    return found[0] if found else {}


ports = {}
for env in ("test", "prod"):
    ns = f"bar-{env}"
    docs = render(env)
    p = f"[{env}] "

    # Namespace and scoping
    check([d["metadata"]["name"] for d in by_kind(docs, "Namespace")] == [ns], p + f"Namespace must be {ns}")
    for d in docs:
        if d["kind"] not in ("Namespace", "PersistentVolume"):
            check(d["metadata"].get("namespace") == ns, p + f"{d['kind']}/{d['metadata']['name']} not in {ns}")
        check(d["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") == "rjm-bar-host",
              p + f"{d['kind']}/{d['metadata']['name']} lacks part-of label")

    # Storage: every PV pre-bound to this environment's claim, on mcgill-data
    pvcs = {d["metadata"]["name"]: d for d in by_kind(docs, "PersistentVolumeClaim")}
    for pv in by_kind(docs, "PersistentVolume"):
        name, spec = pv["metadata"]["name"], pv["spec"]
        check(name.endswith(f"-pv-{env}"), p + f"PV {name} not suffixed -{env}")
        check(spec["claimRef"]["namespace"] == ns, p + f"PV {name} claimRef namespace {spec['claimRef']['namespace']}")
        claim = spec["claimRef"]["name"]
        check(claim in pvcs, p + f"PV {name} claims unknown PVC {claim}")
        if claim in pvcs:
            check(pvcs[claim]["spec"].get("volumeName") == name, p + f"PVC {claim} not bound to {name}")
        check(spec["local"]["path"].startswith(f"/mnt/data2/k8s-volumes/rjm-bar-host/{env}/"), p + f"PV {name} path")
        check(spec["persistentVolumeReclaimPolicy"] == "Retain", p + f"PV {name} must Retain")
        check(spec["storageClassName"] == "data2", p + f"PV {name} storage class")

    # Images: one lowercase repository, tag left for the pipeline
    for d in by_kind(docs, "Job") + by_kind(docs, "Deployment"):
        pod = d["spec"]["template"]["spec"]
        for c in pod.get("initContainers", []) + pod["containers"]:
            check(c["image"] == "ghcr.io/s3kcentrifugal/rjm-bar-host:IMAGE_TAG",
                  p + f"{d['metadata']['name']}/{c['name']} image {c['image']}")
            check("resources" in c and "requests" in c["resources"] and "limits" in c["resources"],
                  p + f"{d['metadata']['name']}/{c['name']} needs requests and limits")
            check(c.get("securityContext", {}).get("allowPrivilegeEscalation") is False,
                  p + f"{d['metadata']['name']}/{c['name']} must not allow privilege escalation")
        check(pod["securityContext"]["runAsUser"] == 10001, p + f"{d['metadata']['name']} runAsUser")
        check(pod["imagePullSecrets"] == [{"name": "mcgill-k8s-pull-2026"}], p + f"{d['metadata']['name']} pull secret")

    # The match Job: a batch with nothing exposed
    job = one(docs, "Job", "rjm-bar-host-match")
    jpod = job["spec"]["template"]["spec"]
    check(not jpod.get("hostNetwork"), p + "match Job must not use hostNetwork")
    check(job["spec"]["backoffLimit"] == 0, p + "match Job backoffLimit must be 0")

    # SPADS: hostNetwork, Recreate, readiness only, pipeline markers intact
    dep = one(docs, "Deployment", "rjm-bar-host-spads")
    spod = dep["spec"]["template"]["spec"]
    check(spod.get("hostNetwork") is True, p + "SPADS must use hostNetwork")
    check(dep["spec"]["strategy"]["type"] == "Recreate", p + "SPADS must use Recreate")
    spads = spod["containers"][0]
    check("livenessProbe" not in spads, p + "SPADS must not have a liveness probe (it would end games)")
    check(spads["readinessProbe"]["exec"]["command"] == ["/opt/rjm-bar-host/spads/ready.sh"], p + "SPADS readiness probe")
    check(dep["spec"]["template"]["metadata"]["annotations"].get("rjm-bar-host/config-hash") == "SPADS_CONFIG_HASH",
          p + "SPADS config-hash marker missing (deploy.yml substitutes it)")
    check(any(r.get("secretRef", {}).get("name") == "rjm-bar-host-secrets" for r in spads["envFrom"]),
          p + "SPADS must take credentials from rjm-bar-host-secrets")

    cm = one(docs, "ConfigMap", "rjm-bar-host-spads-config")["data"]
    game_port = int(cm["SPADS_GAME_PORT"])
    check(spads["ports"][0]["containerPort"] == game_port, p + "SPADS containerPort != SPADS_GAME_PORT")
    check(spads["ports"][0]["protocol"] == "UDP", p + "SPADS game port must be UDP")
    ports[env] = (game_port, int(cm["SPADS_AUTOHOST_PORT"]))

    # No credentials in ConfigMaps
    for c in by_kind(docs, "ConfigMap"):
        for k in c.get("data", {}):
            check(not any(w in k for w in ("PASSWORD", "TOKEN", "SECRET")), p + f"ConfigMap key {k} looks like a credential")

    # Nothing renders a Secret: credentials are created by the pipeline or by hand
    check(not by_kind(docs, "Secret"), p + "a Secret is rendered; credentials must not come from git")

check(len(set(ports["test"]) & set(ports["prod"])) == 0,
      f"test and prod SPADS share host ports: {ports}")
check((ROOT / "k8s/base/spads-deployment.yaml").read_text().count("replicas: 1 # SPADS_REPLICAS") == 1,
      "deploy.yml's SPADS_REPLICAS marker is missing from spads-deployment.yaml")

if ERRORS:
    for e in ERRORS:
        print("FAIL:", e)
    sys.exit(1)
print(f"manifests OK (test ports {ports['test']}, prod ports {ports['prod']})")
