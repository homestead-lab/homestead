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

## Configuration

Set on the Deployment:

| Env | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | listen port |
| `DEFAULT_NS` | `lab` | namespace new workloads land in |
| `SMB_NAMESPACE` | `lab` | where the samba deployment lives |
| `STORAGE_CLASS` | `longhorn-r2` | StorageClass for new volumes |
| `LB_IP` | — | VIP that kube-vip advertises for services |

## Security

> **HarvUI has no authentication.** Anyone who can reach its port can deploy and
> delete workloads. It is intended for a trusted LAN. Do not expose it to the
> internet. Adding auth is tracked in [PLAN.md](PLAN.md).

The ServiceAccount is scoped in `deploy/deploy.yaml`: read on nodes, pods,
Longhorn and KubeVirt; write limited to Deployments, Services, PVCs and
ConfigMaps.

## Licence

MIT
