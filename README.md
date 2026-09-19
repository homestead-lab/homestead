# HarvUI

An Unraid-style control panel for a Harvester / Longhorn / KubeVirt cluster.

Runs as a single container inside the cluster. Pure Python standard library on the
backend — **no pip install at runtime, no build step, no framework** — so it starts
even when the node has no internet access.

![status](https://img.shields.io/badge/status-alpha-orange) ![license](https://img.shields.io/badge/license-MIT-blue)

---

## What it does

| Area | Capability |
|---|---|
| **Dashboard** | Live cluster CPU/RAM with real sparklines (server keeps a rolling series), node health, top consumers |
| **Architecture** | Storage replicas → Longhorn volume → claim → workload → port → VIP, with hover path tracing |
| **Containers** | Search, logs, restart, start/stop, delete, clickable links straight to each service's UI |
| **Deploy** | Unraid-style form: image, ports, volumes, env, iGPU toggle, with a live config summary and manifest preview |
| **App Store** | Live Unraid Community Applications catalogue, converted to Kubernetes workloads |
| **Shares** | SMB shares backed by replicated Longhorn volumes |
| **Volumes / Nodes / Events** | Longhorn health, node detail, cluster activity |

## Layout

```
server/server.py     stdlib HTTP server + Kubernetes API client
web/index.html       shell, SVG icon sprite, settings drawer
web/style.css        design system (glass surfaces, themes)
web/js/app.js        views and chart primitives
deploy/deploy.yaml   ServiceAccount, RBAC, Deployment, Service
scripts/deploy.sh    push sources into the cluster as ConfigMaps
```

Code ships as ConfigMaps mounted into a stock `python:3.12-alpine` image, so
iterating is an `apply` plus a `rollout restart` — never an image rebuild.

## Deploying

```bash
NS=lab HOST=rancher@192.0.2.210 ./scripts/deploy.sh
```

First install also needs the RBAC and Deployment:

```bash
kubectl apply -f deploy/deploy.yaml
```

## Optional: node temperatures

Kubernetes exposes no thermal data. `deploy/nodeprobe.yaml` adds a small
DaemonSet that reads the host's sensors:

```bash
kubectl apply -f deploy/nodeprobe.yaml
```

It mounts `/sys` **read-only**, is **not privileged**, drops all capabilities
and uses a read-only root filesystem. It serves one JSON document on a
cluster-internal port. HarvUI works without it and says so on the node page.

## Configuration

Set on the Deployment:

| Env | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | listen port |
| `DEFAULT_NS` | `lab` | namespace new workloads land in |
| `SMB_NAMESPACE` | `lab` | where the samba deployment lives |
| `STORAGE_CLASS` | `longhorn-r2` | StorageClass for new volumes |
| `LB_IP` | — | VIP that kube-vip advertises for services |
| `SESSION_TTL_HOURS` | `12` | how long a sign-in lasts |
| `ENABLE_NODE_POWER` | unset | `true` allows host reboot/shutdown |

## Security

**Authentication.** On first visit HarvUI asks you to create an administrator
account; until then every API route returns 401. Passwords are PBKDF2-HMAC-SHA256
(600k iterations, per-user salt) stored in the `harvui-auth` Secret. Sessions are
stateless HMAC-signed tokens in an `HttpOnly; SameSite=Strict` cookie, valid 12
hours, so a pod restart does not sign everyone out. Mutating requests must also
carry an `X-HarvUI-Auth` header, which a cross-site form cannot set. Login is
rate-limited to 8 attempts per 5 minutes per client. Changing a password bumps a
per-user version counter, which invalidates that user's other sessions.

> **Transport is plain HTTP.** The session cookie cannot be marked `Secure`, so
> it travels in the clear on your LAN. Put HarvUI behind a TLS ingress before
> trusting it on an untrusted network, and never expose it to the internet.

> **Host power control is disabled by default.** Reboot/shutdown creates a
> privileged pod that enters the host namespaces. Set `ENABLE_NODE_POWER=true`
> on the Deployment to enable it. Cordon and drain work regardless.

> **There are no roles.** Every account can deploy, move, drain and delete.

The ServiceAccount is scoped in `deploy/deploy.yaml`.

## Licence

MIT
