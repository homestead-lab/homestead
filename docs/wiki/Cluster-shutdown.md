# Shut down the whole cluster

Open **Cluster → More → Shut down cluster** as an admin. Review the hosts,
storage and blockers, then type **SHUT DOWN CLUSTER**. This is an outage for
every application, including Homestead. It does not turn hosts back on.

Before starting, stop running VMs gracefully, finish other jobs and arrange
console or physical access. Every host must be Ready and support systemd power
control. Homestead must have one replica and host power control enabled.

The job verifies an independent helper on every host before cordoning anything.
It then cordons every host and evicts application pods, respecting disruption
budgets and each pod's termination grace period. Admission webhook backends, Kubernetes and Longhorn system
pods, DaemonSets and static pods stay until host shutdown. Controllers and
persistent volumes are retained; emptyDir contents are lost during eviction.

Progress can be reopened from Jobs or the Cluster menu. Homestead stays online
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
Applications can then restart. Shutdown never deletes a disruption budget,
forces pod deletion, stops a VM by killing its launcher, or deletes a volume.

If the coordinator itself fails or the API is unavailable, scheduling may remain
cordoned. The journal is retained even when submission has an uncertain outcome:

```sh
kubectl -n lab get configmap homestead-cluster-shutdown -o yaml
kubectl -n lab get pods -l homestead.io/task=cluster-shutdown
kubectl -n lab logs <helper-pod> -c shutdown
```

Use your Homestead namespace if it differs from `lab`. Do not delete the journal
to retry a shutdown: pending helpers may still exist. The UI refuses another
shutdown until recovery releases the journal.

## Starting again

Power the hosts on through their consoles or power buttons. Cordons persist
across boots. From a control-plane console, verify node health and use
`kubectl uncordon <host>` on a suitable host so Homestead can start. Open
**Cluster → More → Shut down cluster → Recover scheduling** once the helper
deadline has passed (up to 33 minutes from the original request). Recovery
requires all original hosts to be Ready, all shutdown helpers to have terminated,
and (after a power handoff) every host to have a new boot ID. If a host stayed on,
inspect it and restart it through its console before recovery. Recovery restores only scheduling that was
allowed before shutdown. It never replays the power request.

Inspect application and storage health, then start VMs explicitly. A recovered
job means scheduling was restored, not that physical shutdown was verified.

A failed shutdown that never committed power can be recovered sooner, once its
helpers have all terminated. Recovery never uncordons an unrelated host cordon.
