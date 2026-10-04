# Release tests

Real clusters, built the way a person builds one, then driven through
Homestead's API: host reboots and power-offs, the whole cluster shut down and
back, rolling OS restarts, storage repair and balancing - on **k3s and RKE2**.

## How it works

- **Hosts are VMs**, Ubuntu under QEMU/KVM on one machine, on a bridge
  (`10.10.0.0/24`, NAT for downloads). A container cannot stand in for a host:
  it never gets a new boot ID, has no systemd to power off and cannot be pulled
  from the wall. The runner is the hand on the power button - it presses it,
  pulls the plug, and powers hosts on again after a shutdown
  (`harness/vms.py`).
- **The cluster** comes from Homestead's own installer, unattended, from the
  release under test: a new cluster on `node-1`, the others joined as servers
  (`harness/install.py`). What is tested is what was published.
- **Scenarios** (`scenarios/`) drive Homestead only through its API, as the
  browser does (`harness/api.py`), and check the outcome on the hosts and in
  Kubernetes. Test apps write to their Longhorn volumes, and a marker written
  before each reboot or shutdown must be there after it.
- **Suites** (`suites.py`) group scenarios on 1 to 4 hosts, and split them
  into CI jobs: each scenario its own job, or a few short ones together, so
  no job takes much over fifteen minutes. Every job has its own runner and
  cluster, all at once; a failure saves diagnostics - Homestead's log and
  jobs, events, Longhorn, each host's journal and console - as an artifact.
- **Speed**: the hosts start from a cached base image (Ubuntu updated, with
  the installer's packages; remade weekly by the `base` job), and servers
  join side by side - each starts once the last has joined etcd. A prod
  release runs every job on k3s and, on RKE2, those where the distribution
  matters (`"rke2"` in `suites.py`): twenty jobs, what a repository runs at
  once.

| Suite | Hosts | Covers |
|---|---|---|
| `core` | 3 | every GET route as an administrator; the node doctor on each host |
| `power` | 3 | a host reboot with apps that move and apps that wait; uncordon; data |
| `single` | 1 | routes; reboot through the handoff (no I/O errors on Homestead's volume); power-off and power-on |
| `shutdown` | 3 | the whole cluster down, powered on, recovered; apps and data back |
| `rolling` | 3 | OS updates restarting every host, never more than one down |
| `outage` | 3 | a container crashing and restarting; a host failing with no warning, its apps back elsewhere |
| `migration` | 2 clusters | an app and its data moved between two clusters, as Linked clusters does |
| `network` | 3 + worker | an app VIP reached from outside and carried over when its host fails; a firewall letting one namespace in; an app on the LAN through Multus; a worker joined, the doctor's safe fixes, the installer run again changing nothing |
| `self-data` | 3 | Homestead's own data moved to a new volume twice, the copy and Homestead on different hosts; account, jobs and old volumes kept, helpers gone |
| `storage` | 3 | a detached volume rebuilt offline; Balance hosts moving copies and containers |

## When it runs

- Every published **prod** release, by itself; dev releases are skipped.
- **On demand** for any published version, dev releases included: Actions ›
  Release tests › Run workflow, with the suites and distributions to run.
- A branch named **`e2e/<distro>-<suite>`** (`e2e/k3s-single`, `e2e/rke2-all`,
  `e2e/all`), against the newest published release - for working on the
  suite itself.

## Running one locally

Linux with KVM, `qemu-system-x86`, `qemu-utils`, `cloud-image-utils`, `kubectl`
and sudo (for the bridge):

```bash
python3 tests/e2e/run.py --distro k3s --suite single --version 2.8.312 --keep
```

`--keep` leaves the VMs running (SSH as `e2e@10.10.0.11` with the key in the
artifacts folder); `--scenario` runs one scenario of the suite.

## Adding a scenario

A scenario is `run(ctx)`: `ctx.api` for Homestead, `ctx.kube` to look,
`ctx.lab.nodes` for the hosts and their power, `ctx.app()` for a test app.
Drive the change through Homestead's API - never kubectl in its place - assert
what a person would check afterwards, and add it to a suite in `suites.py`.
