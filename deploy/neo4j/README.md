# Self-hosted Neo4j Community (always-free VM)

Managed **Neo4j Aura Free** pauses after a few days of inactivity (and is
eventually deleted). This folder lets you run **Neo4j Community yourself** on an
always-free VM so it never auto-pauses. It speaks the same **Bolt + Cypher**
protocol, so the app needs **no code changes** — only the `NEO4J_URI`,
`NEO4J_USERNAME`, and `NEO4J_PASSWORD` env vars change.

```
deploy/neo4j/
├── docker-compose.yml      # Neo4j Community, persistent volumes, restart: always
├── neo4j.env.example       # copy to neo4j.env and set the password (gitignored)
├── startup-script.sh       # GCE startup script: installs Docker + runs Neo4j
├── provision-gcp-vm.sh     # one-command GCP free-tier VM provisioning
└── README.md
```

---

## Option A — GCP free-tier e2-micro (stay in your GCP project)

Fully automated. The VM auto-installs Docker and boots Neo4j on first boot.

> **Free-tier rules:** `e2-micro` is always-free only in `us-west1`,
> `us-central1`, `us-east1`, and only **one** such instance per billing account.
> It has ~1 GB RAM, so the startup script keeps Neo4j memory small (256m heap +
> 256m pagecache), **adds a 2 GB swap file**, and omits APOC. Without these,
> Neo4j exhausts the 1 GB and the VM (including sshd) thrashes. Override memory
> via instance metadata `neo4j-heap` / `neo4j-pagecache` on larger machines.
>
> **First boot is slow:** on e2-micro, Docker install + Neo4j startup takes
> ~3–4 minutes before Bolt accepts connections. A `connection refused` on 7687
> during that window is expected; wait and re-check.
>
> For anything beyond a demo, prefer Option B (Oracle Ampere, up to 24 GB free).

```bash
cd deploy/neo4j
# Restrict access to your IP for safety (recommended):
#   ALLOW_CIDR="$(curl -s ifconfig.me)/32" \
NEO4J_PASSWORD='use-a-long-random-password' ./provision-gcp-vm.sh
```

The script prints the VM's external IP and the exact env vars to set. First boot
takes ~2–4 minutes while Docker installs.

Verify:

```bash
gcloud compute ssh graphrag-neo4j --zone us-central1-a --command \
  'sudo docker ps && sudo journalctl -u google-startup-scripts --no-pager | tail -n 30'
```

Tear down:

```bash
gcloud compute instances delete graphrag-neo4j --zone us-central1-a
gcloud compute firewall-rules delete allow-neo4j-bolt-http
```

---

## Option B — Oracle Cloud Always Free (more RAM, recommended for real use)

Oracle's Always Free Ampere (ARM) instances give up to 4 OCPU / 24 GB RAM for
free — far more headroom than e2-micro.

1. Create an **Always Free** VM (Ampere/Arm, Ubuntu 22.04) in the OCI console.
2. In the VCN security list (or an NSG), add **ingress** rules for TCP `7687`
   and `7474` (restrict the source CIDR to your IP / Cloud Run egress if you
   can).
3. SSH in and install Docker, then run the compose:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker

# copy docker-compose.yml + neo4j.env.example to the VM, then:
cp neo4j.env.example neo4j.env
nano neo4j.env   # set NEO4J_PASSWORD and NEO4J_ADVERTISED_HOST=<vm public ip>
#                  on a big Ampere VM, bump heap/pagecache to e.g. 2g
sudo docker compose --env-file neo4j.env up -d
```

> Oracle images also have a host firewall (`iptables`/`firewalld`). Open the
> ports there too, e.g.:
> `sudo iptables -I INPUT -p tcp --dport 7687 -j ACCEPT` (and `7474`), then
> persist with `netfilter-persistent save`.

---

## Point the app at your self-hosted Neo4j

Update the backend env (Cloud Run service env vars and/or `cloudrun.env`):

```bash
NEO4J_URI=bolt://<VM_EXTERNAL_IP>:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<the password you set>
```

Then redeploy or update the running service:

```bash
gcloud run services update codegraph-api \
  --region <your-region> \
  --update-env-vars NEO4J_URI=bolt://<VM_EXTERNAL_IP>:7687,NEO4J_USERNAME=neo4j,NEO4J_PASSWORD=<password>
```

For local dev, set the same values in `.env`.

---

## Hardening (do this before relying on it)

- **Strong password.** Bolt is exposed to the internet; use a long random one.
- **Restrict source IPs.** Prefer `ALLOW_CIDR=<your.ip>/32` (GCP) or a tight OCI
  ingress CIDR instead of `0.0.0.0/0`. Cloud Run egress IPs are dynamic unless
  you attach a static egress IP / VPC connector, so for a demo a strong password
  + open Bolt is the pragmatic trade-off.
- **TLS (optional).** For `neo4j+s://` connections, mount certificates and set
  `NEO4J_dbms_ssl_policy_bolt_*`. Without certs, use plain `bolt://`.
- **Backups.** Data lives in the `neo4j_data` Docker volume; snapshot the disk
  or run `neo4j-admin database dump` periodically.

## Why not Redis?

Redis has no maintained graph engine — **RedisGraph reached end-of-life in 2023**
and was removed from Redis Stack. The agent generates and runs **Cypher** over a
Bolt connection, so a Bolt/Cypher-compatible store (self-hosted Neo4j, or
Memgraph) is a drop-in; Redis would require rewriting the entire graph layer.
