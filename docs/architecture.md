# Architecture

This page is for contributors. It describes three parts of the server that
most changes touch: how code talks to Kubernetes, which Homestead replica
leads, and how a job runs. Each part names the modules to read first. Their
docstrings carry the detail, including the timeouts and limits, which this
page leaves out so that it stays true.

[CONTRIBUTING](../CONTRIBUTING.md) covers the folders, routes and tests. For
how several replicas share the data volume, the leader Lease and the job
store files, read [Redundancy](reference.md#redundancy-more-than-one-homestead)
first. `tests/test_architecture_doc.py` fails when this page names a module,
function or file that no longer exists.

## Talking to Kubernetes

### One client

Every read and write goes through `homestead_kube`. A request leaves the
process in one place, `homestead_kube.transport`, over a pool of kept-open
connections. Most modules receive the client's `kget` and `ksend` through
their `bind()` in `server/server.py`, and then call the functions below,
passing their bound reader as `read=` or their sender as `send=`. A module
whose `bind()` never ran passes `None`, so it fails instead of reading
through the real client.

### Reads

- `homestead_kube.get` returns the object and raises on any failure.
- `homestead_kube.get_optional` returns `None` when the object is not there
  (404). Any other failure raises. A read that failed is not a missing
  object.
- `homestead_kube.items` returns every row of a list, or raises
  `homestead_kube.Incomplete`. It never returns a list that was cut short.
  `optional_group` names an API group that may not be installed, so a 404
  there is an empty list only when discovery confirms the group is absent.
- `homestead_kube.kget_metadata` asks for a list as metadata only.

`homestead_kube.kget` sets the timeout and asks again, a few times, when the
API server answers 429. During a GET request all its reads share one
deadline (`homestead_read_deadline`, started by `homestead_request_scope`).
A route whose reads ran out answers 503 (`homestead_routes.call`) rather than
a page with parts missing. Background loops have no deadline.

### Writes

- `homestead_kube.send` (the old name is `ksend`) sends one write.
- `homestead_kube.delete_if_present` deletes and returns `None` when the
  object was already gone.
- `homestead_kube.update_json` changes JSON kept in one key of a ConfigMap or
  Secret without losing a change another writer made meanwhile.
- `homestead_kube.put_past_status` replaces an object read a moment ago when
  a controller's status write has moved its version.

Every write passes the guards in `homestead_kube.WRITE_GUARDS`, in order:

1. `homestead_kube.self_data_fence` refuses writes while Homestead moves its
   own data.
2. `homestead_kube.workload_access` refuses a workload that a person who is
   not an admin may not give host access to (`homestead_host_access`).
3. `homestead_kube.change_note` records the write in change history
   (`homestead_changes`).
4. `homestead_kube.storage_guard` protects storage that a move holds
   (`homestead_storage_guard`).
5. `homestead_kube.fresh_reads` drops the cached page reads that the write
   affects, so the next refresh shows it.

After the guards, every write is refused while a cluster shutdown runs
(`homestead_cluster_shutdown`), apart from the shutdown's own writes and the
sign-in store's. A call to `homestead_kube.transport` itself
would pass none of this, which is why code calls `send` instead.

### Preconditions

Kubernetes refuses a write with 409 when the object changed after the read it
names. Homestead uses this in three ways:

- **Updates name the version they read.** `homestead_kube.update_json`
  writes with the resourceVersion it read, and on a 409 reads again and calls
  the change function again. `homestead_kube.put_past_status` tries once
  more only when the fresh read is the same object (uid) at the same
  generation. The leader Lease is renewed the same way
  (`homestead_leader.attempt`).
- **Deletes name the object they decided on.** A delete that follows a check
  sends `DeleteOptions` in its body with `preconditions` taken from that
  same read: the uid, and the resourceVersion where the call has one. A
  second read to get the uid would let an object made in between, under the
  same name, pass. Examples are `homestead_drain_blockers._remove_one` and
  `homestead_cancel_rerun.delete_saved`. A 409 then means another object has
  that name, and the delete is refused.
- **Only some refusals are certain.** `homestead_kube.definite_refusal`
  says which answers mean a write was certainly not made. A timeout or a
  5xx never does.

### Testing against Kubernetes

`tests/fakes/kube.py` is the shared fake API server. Its `Kube` replaces
`homestead_kube.transport`, so a test goes through the real guards, retries
and preconditions. `no_cluster()` makes any read that escapes fail at once.
`tests/test_kube_fake_ratchet.py` stops new hand-written cluster stand-ins in
test files.

## The leader

### Choosing the leader

Every replica answers the browser. Work that must happen once runs on the
leader, chosen by `homestead_leader`:

- `homestead_leader.run` runs on its own thread in every replica, started by
  `server/server.py`. Each round calls `homestead_leader.attempt`, which
  takes the Lease if it is free or has run out, and renews it if it is this
  replica's.
- The renewal writes with the resourceVersion it read, so when two replicas
  take a lease that has run out at the same time, the second is refused.
- `homestead_leader.is_leader` also turns false by the replica's own clock
  when its last renewal is older than the renew deadline. That deadline is
  shorter than the lease, so an old leader stops before a new one can start.
  A renewal that fails stops leading too.
- `homestead_leader.release` hands the lease over when the process receives
  SIGTERM.
- `homestead_leader.when_leading` registers a function to call when a term
  begins. The jobs loop uses it to start a round at once.

### What only the leader does

Background loops run in every replica and check `is_leader()` on every round,
since leadership can move at any time. Only the leader's loops act. Among
them:

- the jobs loop (`homestead_cluster_server._jobs_loop`);
- alerts and push notifications (`homestead_overview_server._alerts_loop`);
- scheduled stops and starts, and the uptime checks
  (`homestead_overview_server._schedules_loop`,
  `homestead_overview_server._uptime_loop`);
- hardware, auto-updates and OS updates
  (`homestead_cluster_server._hardware_loop`,
  `homestead_cluster_server._autoupdate_loop`,
  `homestead_cluster_server._os_updates_loop`);
- most of the storage loops in `homestead_storage_server`;
- the page snapshot, which reads the cluster once for the main pages
  (`homestead_page_snapshot`).

Some modules receive `is_leader` through `bind()` instead, for example
`homestead_changes`. Work whose state must outlive a change of leader keeps
that state on the data volume, so the next leader carries on.

### What every replica does

Each replica answers requests, reads the saved jobs
(`homestead_operations.snapshot`) and serves the leader's page parts, which
the leader writes to the data volume (`homestead_page_snapshot`). The
leader's background state, such as its loops' heartbeats
(`homestead_heartbeats.beat` with `leader_only`), reaches the others through
`homestead_leader_state`.

## How a job runs

A job is a record of work that outlives a request and a restart: a rollout,
an import, a data move, a host update. Its code is in `homestead_operations`.

### The store

- `homestead_operations.start` adds a job. It refuses, under the store's
  lock, when the job would clash with one already running:
  `homestead_operations.CONFLICTS` holds the rules for one kind, and
  `homestead_storage_conflicts` the rules for storage.
- The store is a file on the data volume, changed under a lock every replica
  honours (`homestead_shared.SharedLock`). `homestead_operations.transaction`
  reads, checks and writes in one step. `homestead_operations.stored` is a
  strict read, and `homestead_operations.snapshot` is what the pages show.
  A damaged store raises. It is never read as an empty list.
- A job's own state is in its `ref`: identifiers, phases and what it has
  made. Request bodies and credentials are never stored.

### Kinds

A kind of job is a name in the job store's registries:
`homestead_operations.RESOLVERS` (how it moves on),
`homestead_operations.CANCELLERS` (what cancelling does),
`homestead_operations.RESUMABLE` (whether a failed one can carry on) and
`homestead_operations.LOGGERS`, with `homestead_operations.VIEWS`,
`homestead_operations.CANCEL_BLOCKS` and `homestead_operations.CONFLICTS`
for the kind's view, cancel refusals and start rules. A module adds its kinds
in its `register(ops)`, which `server/server.py` calls; see
`homestead_copy_move.register`. `homestead_cancel.register` and
`homestead_joblogs.register` add the cancellers and logs of many kinds.

`homestead_job_kinds.kinds` reads every registry into one
`homestead_job_kinds.Kind` record per kind. `tests/test_job_kinds.py` fails
for a kind that is only half registered, such as one that can be cancelled
but has no resolver.

### Resolvers and the jobs loop

Only the leader moves jobs on. Its loop is `homestead_operations.run_jobs`,
which calls `homestead_operations.tick` every few seconds while a job is
active and less often when none is. `homestead_operations.advance_soon`
starts a round at once. Requests never run a resolver.

A resolver takes the job and returns `(status, progress, message)`. It reads
Kubernetes, takes the job one step further and records the step in the job's
`ref`. `homestead_operations._refresh` calls it and handles its errors:

- A 404 fails the job and names what went missing.
- Any other error keeps the job running with "Status temporarily
  unavailable". When the resolver has failed for longer than its kind's
  limit, `homestead_job_errors` either fails the job
  (`homestead_job_errors.FAILS`) or keeps it, with its guards, and asks a
  person to look (`homestead_job_errors.KEEPS_GUARDS`).

Most kinds are resolved in turn under the store's lock. The slow kinds in
`homestead_operations.OUTSIDE_LOCK`, such as host steps, copies and the
weekly trim, run on their own threads, a few at a time
(`homestead_job_steps`). Each holds only its job's own lock
(`homestead_operations._job_lock`), and its result is saved only if nobody
changed the job meanwhile.

### Checkpoints

A step that writes to the cluster saves where it is first.
`homestead_operations.checkpoint` writes the job to the store before the
step's write, so a restart, or a new leader, carries on from the saved phase
instead of repeating the write. `homestead_autoupdate.resolve` shows the
shape. It records the next phase in `ref`, then calls `checkpoint`. A
checkpoint is refused when the job has ended, is being cancelled, or was
changed by someone else while the step ran
(`homestead_operations.Superseded`).

Work driven from a request rather than the loop records its progress with
`homestead_operations.record_phase`, which is refused when another replica
has taken the job over. Two kinds of write keep a record of each write as
well: VM writes through `homestead_vm_write.ResourceWriter`, which records an
intent before each write and a receipt after it, and storage handoffs
through `homestead_storage_journal.Journal`.

### Cancel

- `homestead_operations.cancel_plan` says what a cancel would do before
  anything changes. The kind's entry in `CANCELLERS` is a pair: a plan
  function and a run function. The modes are in
  `homestead_operations.MODES`: put back, stop, or stop tracking. A kind
  with no canceller can only stop being tracked. `CANCEL_BLOCKS` refuses a
  cancel while a step must not be interrupted. The cancellers of most kinds
  are in `homestead_cancel`, whose docstring says what each mode removes.
- `homestead_operations.cancel` runs it. For a kind in `OUTSIDE_LOCK` it only
  records the request; the loop applies it once the step in flight has ended,
  after checking the plan again. For any other kind it runs on the request's
  thread, possibly on a replica that is not the leader, holding the job's own
  lock. The job is marked `homestead_operations.CANCELLING` first, with what
  the person chose. If putting it back fails, the job runs again and says
  why.

### Cancel after a restart

A job marked as cancelling whose lock is free has lost its canceller. The
process running it went away. The leader's loop finds it and calls
`homestead_operations._resume_cancel`, which checks the plan again and either
runs the canceller again with what the person chose or leaves the job for a
person, with the reason.

So a canceller must be safe to run twice. `homestead_cancel_rerun.SAFE` lists
the kinds the leader may run again, and `homestead_cancel_rerun.PERSON` the
kinds that wait for a person. `tests/test_cancel_rerun.py` fails for a
canceller on neither list, and `tests/test_cancel_rerun_kinds.py` names the
test that proves each kind on `SAFE`. A canceller deletes through
`homestead_cancel_rerun.delete_saved`: the first run saves each object's uid
(`homestead_cancel_rerun.remember`, kept by
`homestead_operations.keep_cancel_ref`), and a later run deletes only that
object. What a person sees in this case is in
[Updating Homestead](reference.md#updating-homestead).

### Carrying on after a failure

`homestead_operations.resume` carries a failed job on from the step it
stopped at, when the kind's `RESUMABLE` check allows it.

### Testing jobs

`tests/fakes/job_store.py` gives a stand-in job store the same `stored()` and
`transaction()` as the real one. The loop and its steps are tested in
`tests/test_jobs_loop.py` and `tests/test_job_steps.py`. Jobs that cordon a
host or stop an app use the shared helpers `homestead_cordon` and
`homestead_app_hold`; `tests/test_job_safety_ratchets.py` keeps new code on
them.
