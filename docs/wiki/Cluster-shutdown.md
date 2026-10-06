# Shut down the whole cluster

Open **Cluster → More → Shut down cluster** as an admin. Review the hosts,
storage and blockers, then type **SHUT DOWN CLUSTER**. This is an outage for
every application, including Homestead. It does not turn hosts back on.

Before starting, stop running VMs gracefully, finish other jobs and arrange
console or physical access. Every host must be Ready and support systemd power
control. Homestead must have one replica and host power control enabled.

The job verifies an independent helper on every host before cordoning anything.
It then cordons every host and evicts application pods, respecting disruption
budgets and each pod's termination grace period. With every host cordoned, a
budget that refuses an eviction can never be met again - no host is left to
start a replacement (KubeVirt's virt-controller is one) - so after 30 seconds
that pod is stopped with an ordinary graceful delete instead, as
`kubectl drain --disable-eviction` would. Admission webhook backends, Kubernetes and Longhorn system
pods, DaemonSets and static pods stay until host shutdown. Controllers and
persistent volumes are retained; emptyDir contents are lost during eviction.

Progress can be reopened from Jobs or the Cluster menu. While hosts drain it
shows how many of the reviewed pods have stopped, and each one still stopping
with its host and what it waits for. Homestead stays online
until the other applications have drained. An independent coordinator then
evicts Homestead and waits for every reviewed Longhorn volume to detach.
It commits power requests only after those checks pass. Host-side systemd
timers request power-off after 30 seconds, with Homestead's host last at 90
seconds. This final stage continues without Homestead or a working Kubernetes
API after each helper has received the commit. A helper that misses the
30-second commit window does not power off later.

Disconnection and NotReady are **not proof of power-off**. Check host consoles
or physical power. An API interruption can leave some hosts on; do not assume
that all helpers received the final request. Inspect their logs and the journal.

## Blocked drains and cancellation

You can cancel until the final power handoff. A blocked drain gets up to ten
minutes; Longhorn detachment gets up to five. If either fails, no power commit
is issued, and the coordinator restores the original scheduling where possible.
Applications can then restart, and the VMs it shut down are started again.
Shutdown never deletes a disruption budget, force-deletes a pod (skipping its
grace period), stops a VM by killing its launcher, or deletes a volume.

If the coordinator itself fails or the API is unavailable, scheduling may remain
cordoned. The journal is retained even when submission has an uncertain outcome:

```sh
kubectl -n lab get secret homestead-cluster-shutdown -o jsonpath={.data.state} | base64 -d
kubectl -n lab get pods -l homestead.io/task=cluster-shutdown
kubectl -n lab logs <helper-pod> -c shutdown
```

Use your Homestead namespace if it differs from `lab`. Do not delete the journal
to retry a shutdown: pending helpers may still exist. The UI refuses another
shutdown until recovery releases the journal.

The authoritative journal is a Kubernetes Secret. Its approved image, host
identities, readiness and final power commit require Secret access; ConfigMap
writers cannot authorize shutdown helpers. Secret write access in Homestead's
namespace is privileged, just like access to its account Secret. No additional
RBAC permissions are needed by this change.

An older preview's ConfigMap journal is never migrated automatically. If one is
found without the Secret journal, inspect the original run from a cluster console,
wait for its helper deadlines and verify that all helpers have terminated before
retiring that old journal. Do not treat editing its phase as recovery.

## Starting again

Power the hosts on through their consoles or power buttons. **Homestead comes
back by itself**: just before power was committed, the shutdown left a small
recovery helper (no volumes, tolerating the cordons). It starts with the hosts,
and once every original host is Ready on a new boot it recovers scheduling as
it was before the shutdown, starts again the VMs the shutdown stopped and
Homestead's copies, and removes itself. Homestead then starts on the uncordoned
hosts - a few minutes after the last host is Ready.

If a host stays off, the helper waits for it. To start without it, use
`kubectl uncordon <host>` on a suitable host from a control-plane console so
Homestead can start, then open **Cluster → More → Shut down cluster → Recover
scheduling** once the helper deadline has passed (up to 33 minutes from the
original request). Recovery by hand requires all original hosts to be Ready, all
shutdown helpers to have terminated, and (after a power handoff) every host to
have a new boot ID. If a host stayed on, inspect it and restart it through its
console before recovery. Either way, recovery restores only scheduling that was
allowed before shutdown, and never replays the power request.

Sign-in, password changes and session revocation remain available during shutdown
and recovery. Login attempt limits still persist across restarts; ordinary
workload and Secret edits remain held by the shutdown guard.

Inspect application and storage health. A recovered
job means scheduling was restored, not that physical shutdown was verified.

A failed shutdown that never committed power can be recovered sooner, once its
helpers have all terminated. Recovery never uncordons an unrelated host cordon.
