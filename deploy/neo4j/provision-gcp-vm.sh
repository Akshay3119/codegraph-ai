#!/usr/bin/env bash
# ==============================================================================
# Provision a free-tier GCP VM running self-hosted Neo4j Community.
# ==============================================================================
# Creates an always-free-eligible e2-micro VM, a firewall rule for Bolt/HTTP,
# and boots Neo4j automatically via startup-script.sh. Re-runnable: it skips
# resources that already exist.
#
# Prereqs: gcloud is installed and authenticated, and a project is selected
#   (gcloud config set project <id>). Compute Engine API must be enabled.
#
# GCP Always Free note: e2-micro is free only in us-west1, us-central1, and
# us-east1, and only ONE such instance per account. e2-micro has ~1 GB RAM, so
# Neo4j memory is kept small. For more headroom, prefer an Oracle Cloud Ampere
# Always Free VM (see README.md).
#
# Usage:
#   NEO4J_PASSWORD='a-long-random-password' ./provision-gcp-vm.sh
#
# Optional overrides (env vars):
#   VM_NAME (default: graphrag-neo4j)
#   ZONE    (default: us-central1-a)   # must be in a free-tier region
#   MACHINE (default: e2-micro)
#   ALLOW_CIDR (default: 0.0.0.0/0)    # restrict to your IP for safety!
# ==============================================================================
set -euo pipefail

VM_NAME="${VM_NAME:-graphrag-neo4j}"
ZONE="${ZONE:-us-central1-a}"
MACHINE="${MACHINE:-e2-micro}"
ALLOW_CIDR="${ALLOW_CIDR:-0.0.0.0/0}"
FW_RULE="allow-neo4j-bolt-http"
TAG="neo4j-server"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "${NEO4J_PASSWORD:-}" ]; then
  echo "ERROR: set NEO4J_PASSWORD env var (a long random password)." >&2
  exit 1
fi

PROJECT="$(gcloud config get-value project 2>/dev/null)"
echo "Project: ${PROJECT}   Zone: ${ZONE}   Machine: ${MACHINE}"

if [ "${ALLOW_CIDR}" = "0.0.0.0/0" ]; then
  echo "WARNING: exposing Neo4j ports to the whole internet. Use a strong"
  echo "         password, or set ALLOW_CIDR=<your.ip>/32 to restrict access."
fi

# ── Firewall: allow Bolt (7687) + HTTP browser (7474) to tagged VMs ───────────
if ! gcloud compute firewall-rules describe "${FW_RULE}" >/dev/null 2>&1; then
  echo "Creating firewall rule ${FW_RULE}..."
  gcloud compute firewall-rules create "${FW_RULE}" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:7687,tcp:7474 \
    --source-ranges="${ALLOW_CIDR}" \
    --target-tags="${TAG}"
else
  echo "Firewall rule ${FW_RULE} already exists; leaving as-is."
fi

# ── VM: e2-micro with startup script that installs Docker + runs Neo4j ─────────
if ! gcloud compute instances describe "${VM_NAME}" --zone "${ZONE}" >/dev/null 2>&1; then
  echo "Creating VM ${VM_NAME}..."
  gcloud compute instances create "${VM_NAME}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE}" \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --boot-disk-size=30GB \
    --boot-disk-type=pd-standard \
    --tags="${TAG}" \
    --metadata="neo4j-password=${NEO4J_PASSWORD}" \
    --metadata-from-file="startup-script=${SCRIPT_DIR}/startup-script.sh"
else
  echo "VM ${VM_NAME} already exists; leaving as-is."
fi

EXTERNAL_IP="$(gcloud compute instances describe "${VM_NAME}" --zone "${ZONE}" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"

cat <<EOF

==============================================================================
VM provisioned. Neo4j is installing in the background (first boot ~2-4 min).

  External IP : ${EXTERNAL_IP}
  Bolt URI    : bolt://${EXTERNAL_IP}:7687
  Browser UI  : http://${EXTERNAL_IP}:7474   (user: neo4j)

Point the app at it (Cloud Run / cloudrun.env):
  NEO4J_URI=bolt://${EXTERNAL_IP}:7687
  NEO4J_USERNAME=neo4j
  NEO4J_PASSWORD=<the password you set>

Check progress:
  gcloud compute ssh ${VM_NAME} --zone ${ZONE} --command \\
    'sudo docker ps && sudo journalctl -u google-startup-scripts --no-pager | tail -n 30'
==============================================================================
EOF
