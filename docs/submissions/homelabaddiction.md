# HomelabAddiction

Licence disclosure for the editor: **Apache 2.0 with Commons Clause 1.0; source-available, free to self-host, with a restriction on selling Homestead functionality.** Include this with the description; do not classify it as open source.

Status: email draft prepared. The [contact page](https://homelabaddiction.com/contact/) now specifies email, not a contact form. It accepts free directory suggestions for maintained, documented projects whose core self-hosted features do not require a paid licence. Route and address verified in the live page on 3 October 2026.

To: `contact@homelabaddiction.com`

Subject follows the site's suggestion format:

```text
Suggestion — Homestead
```

## Email body — editorial target: 120–180 words, excluding URLs

```text
Hello,

Please consider Homestead for the self-hosted directory:
https://github.com/wjcloudy/homestead

Homestead is a source-available hyperconverged homelab platform, bringing compute, storage and networking together across ordinary machines. Its friendly web interface simplifies managing apps, Longhorn volumes, KubeVirt VMs, backups, shares and virtual IPs, with integrated controls for nodes and platform upgrades.

It runs on k3s, RKE2, Harvester or an existing Kubernetes cluster. A guided Linux installer can set up k3s or RKE2; Helm and manifest installation are also documented. Kubernetes is required, so a standalone Docker Compose recipe would not describe its deployment accurately.

The project is currently beta, and its core features are free to self-host. The demo and screenshots use sample data. I am suggesting a standard free directory entry.

Installation: https://github.com/wjcloudy/homestead#quick-install
Documentation: https://github.com/wjcloudy/homestead/wiki
Demo: https://wjcloudy.github.io/homestead/
Releases and screenshots: https://github.com/wjcloudy/homestead/releases

Thank you for considering it.
```

Add your chosen signature and, if applicable, a maintainer-affiliation sentence before sending.

## Suggested directory line

```text
Hyperconverged homelab platform with a simple web interface for apps, VMs, storage and networking, plus guided cluster setup and upgrades.
```

## Technical answers if requested

| Field | Verified answer |
|---|---|
| Suggested category | Server administration / infrastructure management; editor chooses the available category |
| Deployment | Linux installer, Helm, Kubernetes manifest |
| Dependencies | Kubernetes; Longhorn for storage features; KubeVirt for VMs |
| Separate database service | None specified for the dashboard; persistent application state is stored under `/data` |
| Dashboard access | TCP 8088 Service → TCP 8080 container; installer and chart can configure exposure |
| Dashboard persistence | Manifest requests 2Gi PVC, mounted at `/data`; workload storage is additional |
| Container resources | Manifest request: 50m CPU / 96Mi RAM; memory limit: 256Mi |
| Starting host guidance | k3s guide: 2 cores / 4 GB RAM to start; workload and cluster requirements are additional |
| Maturity | Beta |

Evidence: [product facts](product-facts.md). Do not present container requests as the whole system's minimum specification. Send no deployment recipe that bypasses the documented Kubernetes requirements.
