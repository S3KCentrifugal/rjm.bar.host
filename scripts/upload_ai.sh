#!/usr/bin/env bash
# Copy custom AI folders onto the cluster's custom-AI volume.
#
#   scripts/upload_ai.sh <test|prod> <AI folder>...
#
# Each folder is one AI as it sits in a BAR install's AI/Skirmish, e.g.
#   .../data/engine/recoil_2026.07.04/AI/Skirmish/SMRTBARb
# and must hold the *Linux* build (<version>/libSkirmishAI.so); a Windows
# SkirmishAI.dll cannot load in the container and is rejected here first.
#
# The match Job and SPADS mount the volume read-only, so this starts a short-
# lived pod that mounts it read-write, copies, and is deleted. Installed AIs
# take effect at the next match, or when SPADS next restarts.
set -euo pipefail

env="${1:?usage: upload_ai.sh <test|prod> <AI folder>...}"
shift
[ "$#" -gt 0 ] || { echo "usage: upload_ai.sh <test|prod> <AI folder>..." >&2; exit 2; }
case "$env" in test|prod) ;; *) echo "environment must be test or prod" >&2; exit 2 ;; esac
ns="bar-$env"
pod="rjm-bar-host-ai-upload"

for dir in "$@"; do
	[ -d "$dir" ] || { echo "not a folder: $dir" >&2; exit 1; }
	found=0
	for v in "$dir"/*/; do
		[ -f "$v/AIInfo.lua" ] || continue
		found=1
		if [ ! -f "$v/libSkirmishAI.so" ]; then
			echo "$v has no libSkirmishAI.so$([ -f "$v/SkirmishAI.dll" ] && echo ' (it is a Windows build)'); use the AI's Linux release" >&2
			exit 1
		fi
	done
	[ "$found" = 1 ] || { echo "$dir has no <version>/AIInfo.lua; pass the <Name> folder itself" >&2; exit 1; }
done

cleanup() { kubectl delete pod "$pod" -n "$ns" --ignore-not-found --wait=false > /dev/null; }
trap cleanup EXIT

kubectl delete pod "$pod" -n "$ns" --ignore-not-found --wait=true > /dev/null
kubectl apply -n "$ns" -f - > /dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $pod
  labels:
    app.kubernetes.io/name: $pod
    app.kubernetes.io/part-of: rjm-bar-host
spec:
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
  containers:
    - name: upload
      image: busybox:1.36
      command: ["sleep", "600"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      resources:
        requests: { cpu: "10m", memory: "16Mi" }
        limits: { cpu: "200m", memory: "64Mi" }
      volumeMounts:
        - name: custom-ai
          mountPath: /bar/custom-ai
  volumes:
    - name: custom-ai
      persistentVolumeClaim:
        claimName: rjm-bar-host-ai-pvc
EOF
kubectl wait --for=condition=Ready "pod/$pod" -n "$ns" --timeout=120s > /dev/null

for dir in "$@"; do
	name="$(basename "$dir")"
	kubectl exec -n "$ns" "$pod" -- rm -rf "/bar/custom-ai/$name"
	kubectl cp "$dir" "$ns/$pod:/bar/custom-ai/$name"
	echo "uploaded $name to $ns"
done
kubectl exec -n "$ns" "$pod" -- ls -la /bar/custom-ai
