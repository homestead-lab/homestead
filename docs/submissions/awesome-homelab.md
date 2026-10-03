# AwesomeHomelab

Status: **hold for licence eligibility**. The list describes its scope as open-source apps. Homestead now uses Apache 2.0 with Commons Clause 1.0, which is source-available. Obtain an explicit maintainer decision before using this conditional draft. Rules and source data checked 3 October 2026.

The [README](https://github.com/AwesomeHomelab/awesome-homelab) links to a Submit App issue route. Its generated table uses concise functional descriptions. The [source guide](https://github.com/AwesomeHomelab/awesome-homelab/blob/master/AGENTS.md) permits `name`, `url` and optional `description` in category YAML files. The best fit is [data/infra-management.yaml](https://github.com/AwesomeHomelab/awesome-homelab/blob/master/data/infra-management.yaml), alongside Cockpit, KubeVirt and other infrastructure tools. No hard description limit was found; this draft uses a one-sentence description.

## Issue title

```text
Submit App: Homestead — hyperconvergence for the homelab
```

## Issue body

```markdown
Please consider Homestead for the Infra Management category.

- Repository: https://github.com/wjcloudy/homestead
- License: Apache 2.0 + Commons Clause 1.0 (source-available)
- Documentation: https://github.com/wjcloudy/homestead/wiki
- Demo: https://wjcloudy.github.io/homestead/

Suggested description:
Hyperconverged homelab platform with a simple web interface for apps, VMs, storage and networking, plus guided cluster setup and upgrades.

Homestead brings compute, storage and networking together across ordinary machines, with a friendly interface designed around apps, volumes and VMs. Its guided installer creates or joins k3s or RKE2 clusters; it also runs on Harvester or existing Kubernetes clusters. Manage nodes, platform upgrades, apps and VMs through one interface, with Longhorn for replicated volumes and KubeVirt for virtual machines. It also provides snapshots, backups, network shares and virtual IP management.

The project is currently beta. The demo uses sample data and does not operate a live cluster. Installation instructions and screenshots are in the README.
```

For a maintainer submission, add a truthful affiliation disclosure in your own words. Do not imply that this is an independent user's recommendation.

## Optional pull request route

Append the record in [awesome-homelab-entry.yaml](awesome-homelab-entry.yaml) to `data/infra-management.yaml` in an upstream fork. Do not replace the existing category file or hand-edit the generated README. Follow upstream's `pnpm lint` / `pnpm build` instructions when preparing an actual PR. Those upstream checks have not been run for this local candidate record.

Suggested PR title: `Add Homestead to Infra Management`.

```text
Adds Homestead, a source-available hyperconverged homelab platform, to the infrastructure management category. It combines compute, storage and networking through a friendly web interface, with guided Kubernetes cluster setup and integrated upgrade controls. The project is currently beta and provides installation documentation and an interactive demo using sample data.
```

Use the issue or the PR route, not both. No match appeared in the category file or issue/PR search on the preparation date; recheck before submitting.
