#!/usr/bin/env bash
# ==============================================================================
# GCE startup script — installs Docker and launches self-hosted Neo4j Community.
# ==============================================================================
# Runs automatically on first boot when passed to a Compute Engine VM via
# `--metadata-from-file startup-script=...` (see provision-gcp-vm.sh).
#
# It reads the Neo4j password from the instance metadata attribute
# `neo4j-password` and the VM's external IP from the metadata server, then
# brings up Neo4j with persistent storage and `restart: always` so it survives
# reboots and never auto-pauses.
#
# Target image family: Debian / Ubuntu (GCP default). For Oracle Cloud, follow
# the manual steps in README.md instead.
# ==============================================================================
set -euo pipefail

LOG() { echo "[neo4j-startup] $*"; }

METADATA="http://metadata.google.internal/computeMetadata/v1"
HDR="Metadata-Flavor: Google"

NEO4J_PASSWORD="$(curl -fs -H "$HDR" "$METADATA/instance/attributes/neo4j-password" || true)"
EXTERNAL_IP="$(curl -fs -H "$HDR" "$METADATA/instance/network-interfaces/0/access-configs/0/external-ip" || true)"

if [ -z "${NEO4J_PASSWORD}" ]; then
  LOG "ERROR: instance metadata 'neo4j-password' is empty. Aborting."
  exit 1
fi
[ -z "${EXTERNAL_IP}" ] && EXTERNAL_IP="localhost"

# Memory settings (overridable via instance metadata; conservative defaults so
# Neo4j fits a ~1 GB e2-micro free-tier VM).
NEO4J_HEAP="$(curl -fs -H "$HDR" "$METADATA/instance/attributes/neo4j-heap" || true)"
NEO4J_PAGECACHE="$(curl -fs -H "$HDR" "$METADATA/instance/attributes/neo4j-pagecache" || true)"
[ -z "${NEO4J_HEAP}" ] && NEO4J_HEAP="256m"
[ -z "${NEO4J_PAGECACHE}" ] && NEO4J_PAGECACHE="256m"

# ── Swap file (idempotent) ────────────────────────────────────────────────────
# A swap file is essential on tiny VMs: it absorbs JVM/page-cache spikes so the
# OOM killer doesn't take down Neo4j (or sshd) under load.
if ! swapon --show | grep -q '/swapfile'; then
  LOG "Creating 2G swap file..."
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ── Install Docker Engine + compose plugin (idempotent) ───────────────────────
if ! command -v docker >/dev/null 2>&1; then
  LOG "Installing Docker..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null \
    || curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
fi

# ── Write compose + env, then launch ──────────────────────────────────────────
APP_DIR="/opt/neo4j"
mkdir -p "$APP_DIR"

cat > "$APP_DIR/neo4j.env" <<EOF
NEO4J_PASSWORD=${NEO4J_PASSWORD}
NEO4J_ADVERTISED_HOST=${EXTERNAL_IP}
NEO4J_HEAP_INITIAL=${NEO4J_HEAP}
NEO4J_HEAP_MAX=${NEO4J_HEAP}
NEO4J_PAGECACHE=${NEO4J_PAGECACHE}
EOF
chmod 600 "$APP_DIR/neo4j.env"

cat > "$APP_DIR/docker-compose.yml" <<'EOF'
services:
  neo4j:
    image: neo4j:5-community
    container_name: graphrag-neo4j
    restart: always
    ports:
      - "7474:7474"
      - "7687:7687"
    environment:
      NEO4J_AUTH: "neo4j/${NEO4J_PASSWORD:?Set NEO4J_PASSWORD}"
      # APOC omitted by default to keep the footprint small on tiny free VMs.
      NEO4J_server_default__listen__address: "0.0.0.0"
      NEO4J_server_default__advertised__address: "${NEO4J_ADVERTISED_HOST:-localhost}"
      NEO4J_server_bolt_advertised__address: "${NEO4J_ADVERTISED_HOST:-localhost}:7687"
      NEO4J_server_http_advertised__address: "${NEO4J_ADVERTISED_HOST:-localhost}:7474"
      NEO4J_server_memory_heap_initial__size: "${NEO4J_HEAP_INITIAL:-512m}"
      NEO4J_server_memory_heap_max__size: "${NEO4J_HEAP_MAX:-512m}"
      NEO4J_server_memory_pagecache_size: "${NEO4J_PAGECACHE:-512m}"
    volumes:
      - neo4j_data:/data
      - neo4j_logs:/logs
volumes:
  neo4j_data:
  neo4j_logs:
EOF

LOG "Starting Neo4j (advertised host: ${EXTERNAL_IP})..."
cd "$APP_DIR"
docker compose --env-file neo4j.env up -d
LOG "Done. Bolt: bolt://${EXTERNAL_IP}:7687  Browser: http://${EXTERNAL_IP}:7474"
