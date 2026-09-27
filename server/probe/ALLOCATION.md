# Allocation reader contract

`allocation.snapshot()` only calls the local kubelet PodResources v1
`GetAllocatableResources` and `List` methods. It has no command execution, HTTP
listener, Kubernetes token, checkpoint access, or mutation method. Importing
the module does not load its gRPC dependencies until a snapshot is requested.

It can now be explicitly enabled through the node probe's VM allocation settings;
default/manual-install manifests do not enable it. It is not yet consumed by admission.
Do not enable NUMA placement solely because this reader reports `complete`.
The backend must also verify current probe/host identity, freshness, supported
kubelet version and actual CPU/topology-manager policy, online topology and
local hugepage reservations. CPU hotplug can leave the kubelet's allocatable
pool stale until kubelet restart. A snapshot is not a scheduler reservation.

The observation brackets two lists with two capacity reads, within one total
deadline (default three seconds, maximum five). It rejects a replaced socket,
changed CPU/memory inventory, malformed/oversized messages, unknown protobuf
fields, duplicate identities, out-of-pool CPUs and exclusive CPU overlap across
Pods. Failure returns unknown (`None`), never an empty/free capacity estimate.
No resources are credited back by workload name: this protocol has no Pod UIDs.
Init containers can legitimately reuse CPU IDs within one Pod.

Multi-node memory allocations remain multi-node; the collector never divides
their bytes among cells. Device-plugin inventory is not modelled by this reader.
DRA presence is flagged, not interpreted as proof of resource availability.
The CPU/memory bracket does not claim to detect device-only changes.

Only a regular local Unix socket is accepted, not a symlink or TCP endpoint.
Installation is opt-in with a validated minimal socket directory,
not a broad kubelet root or credentials mount. A read-only filesystem mount does
not itself restrict what RPCs can be sent through a socket; the fixed client
methods are part of the trust boundary. `allocation_http.py` uses a separate
DaemonSet-owned Secret, request/response domain separation, target node/Pod UID,
short-lived nonces, a bounded replay cache, response size limits and boot identity
checks. HTTP health only means the process is up, not that allocations are known.
It serves one request at a time with socket timeouts. There is no public Service.
Signed HTTP is not confidentiality; the cluster network and host root are trusted.
Backend verification of response authentication, ownership, current policy and
freshness is still required before this output can authorize placement.

The sidecar mounts an empty read-only directory at the standard service-account
path, so Kubernetes' service-account admission does not inject its token into this
container; other existing probe containers' token policy is unchanged. See the
upstream [service-account admission implementation](https://github.com/kubernetes/kubernetes/blob/v1.32.0/plugin/pkg/admission/serviceaccount/admission.go).
Arbitrary mutating webhooks and a compromised host are outside this assertion.

## Schema maintenance

The schema is adapted from Kubernetes v1.32.0
`staging/src/k8s.io/kubelet/pkg/apis/podresources/v1/api.proto`. It retains wire
numbers and types, drops Go/gogo options, and omits the unused `Get` method.
Source attribution and Apache-2.0 licence are included in the repository/image.
Unknown fields fail closed, including fields added by newer kubelets. Supporting
a newer schema requires reviewing its allocation semantics, not merely stripping
unknown fields or regenerating code.

`podresources_pb2.py` was generated with `grpcio-tools==1.84.0` (bundled protoc
7.35.1). In a separate Python 3.12 development environment, generate it with:

```sh
python -m grpc_tools.protoc -I server/probe --python_out=server/probe server/probe/podresources.proto
```

Only the hash-locked runtime wheels are installed in the production image;
neither the generator nor a compiler is shipped. Run `test_probe_allocation.py`
on Linux to exercise the real bounded RPC transport against a disposable fake
kubelet, as well as validation and redaction regressions. No live socket is used.
