"""The job that follows a host network change, for a bond and for a bridge.

Both kinds apply a change on a host with a rollback timer armed, then: check
the host from here, disarm the timer once it answers in full, start kube-vip
again, restart k3s or RKE2 on a cluster, and wait for its kubelet. That sequence
is here, once; a fix to it is made here. Each kind gives a Spec: what it asks
of its host, the words a person reads, and what only it does. Its planning and
the scripts it sends stay in its own module (homestead_host_bonds,
homestead_host_bridge), and so does the host's rollback timer.

What a job saves (ref) belongs to the running job: a job started before an
upgrade carries on after it, so its fields and stage names do not change here.
"""
import calendar
import re
import time
import urllib.error

import homestead_pod_exec as POD_EXEC

# A change script still running at its limit (homestead_pod_exec.ExecTimeout)
# may have changed the network part-way: its job keeps the host held and
# needs attention until a person cancels it, renewing the hold this often.
RENEW_UNCONFIRMED = 60

# Proof the kubelet came back after k3s or RKE2 restarted. Ready alone stays
# True about 40 s after the kubelet's last heartbeat, so a host the new network
# cut off still reads Ready at first. When the restart is asked for, the step
# keeps what the kubelet last reported (restart_baseline); it then needs a
# Lease renewal more than BACK_AFTER seconds after that renewal, or a Ready
# heartbeat or transition after that heartbeat. Both sides come from the host's
# own clock, so a host clock ahead of or behind Homestead's changes nothing.
# systemd-run restarts the service 2 s on, and the old kubelet may renew once
# more (every 10 s) while it stops: that renewal is inside BACK_AFTER.
BACK_AFTER = 15
NODE_LEASE = "/apis/coordination.k8s.io/v1/namespaces/kube-node-lease/leases/{}"


class Spec:
    """What one kind of network change asks, says and adds."""
    rollback_seconds = restart_seconds = hold_seconds = 0
    confirm_script = ""
    # Progress where the kinds differ: a job shows these as it moves on.
    confirming_at = confirmed_at = restarting_at = waiting_at = 0

    @staticmethod
    def bound():
        """The kind's hostrun, kget and ksend, as they are bound now."""
        raise NotImplementedError

    def unnamed(self, ref):
        """What a follow-up runs as, for a job saved with no name for its change."""
        raise NotImplementedError

    def check(self, ref):
        """The script that looks at the host."""
        raise NotImplementedError

    def needs(self, ref):
        """What the host must show besides its armed rollback: its marker
        and, in the order they are named, the words for it being missing."""
        raise NotImplementedError

    def also_waiting(self, ref, lines):
        """Words for anything else that keeps it from being confirmed."""
        return []

    def host_gave_up(self, ref, words):
        """How the job ends when the host says it put itself back, or None."""
        return None

    def before_confirm(self, ref, words, elapsed, now):
        """The job's next step when a host that is up must not be confirmed
        yet, or None."""
        return None

    def carries_on(self, ref):
        """The interface the host's network is on now."""
        raise NotImplementedError

    def never_up(self, ref):
        raise NotImplementedError

    def confirming(self, ref):
        raise NotImplementedError

    def confirmed(self, ref, lines):
        raise NotImplementedError

    def restarts(self, ref):
        """Whether kube-vip and k3s or RKE2 follow the change."""
        return True

    def done(self, ref):
        raise NotImplementedError

    def finished(self, ref):
        """What a job past its last step says."""
        return self.done(ref)

    def other_stage(self, ref, elapsed):
        """A stage only this kind has; any other is a job already finished."""
        return "succeeded", 100, self.finished(ref)


def status(spec, item, now):
    """The job's next step: (status, progress, message). The host is let go
    of when the job ends."""
    hostrun = spec.bound()[0]
    try:
        result = _step(spec, item, now)
    except urllib.error.HTTPError as error:
        if error.code == 404:    # the job fails on it (homestead_operations)
            let_go(hostrun, item["ref"])
        raise
    if result[0] in ("succeeded", "failed"):
        let_go(hostrun, item["ref"])
    return result


def let_go(hostrun, ref):
    if ref.get("lease"):
        hostrun.let_go(ref["node"], ref.pop("lease"))


def held(spec, ref):
    """What a follow-up change runs with: inside the job's hold."""
    return {"change": ref.get("change") or spec.unnamed(ref), "holder": ref.get("lease", "")}


def unconfirmed(spec, ref):
    """What a job says while its change script's outcome is not known."""
    node = ref["node"]
    still = (f"{node} stays held, so no other change starts there" if not ref.get("renew_error") else
             f"Homestead could not renew its hold on {node} ({ref['renew_error']}), so another change may start "
             f"there once the hold runs out")
    return (f"{ref['change']} needs attention: the script on {node} did not finish within {ref['timeout']:g} seconds, "
            f"so Homestead cannot tell whether it changed {node}'s network. {still}. Check its network (if the change got as far as arming its rollback, {node} puts its "
            f"old network back by itself within {spec.rollback_seconds // 60} minutes), then cancel this job to let go of {node}")


def unconfirmed_ref(node, what, lease, error, now):
    """What such a job saves."""
    return {"node": node, "stage": "unconfirmed", "since": now, "renewed": now,
            "change": what, "lease": lease, "timeout": error.timeout or 0}


def _still_unconfirmed(spec, ref, now):
    if now - ref.get("renewed", 0) >= RENEW_UNCONFIRMED:
        try:
            spec.bound()[0].hold(ref["node"], ref["change"], seconds=spec.hold_seconds, holder=ref.get("lease", ""))
            ref["renewed"] = now
            ref.pop("renew_error", None)
        except Exception as error:
            ref["renew_error"] = str(error)[:160] or type(error).__name__    # asked again next round
    return "running", 20, unconfirmed(spec, ref)


# kube-vip's pod and the host count, after the rollback is disarmed. Neither is
# taken on trust: until each is proved the job keeps trying, for as long as it
# waits for a restart, and then fails saying what was not done. ref["kube_vip"]
# remembers it: {"pods": [the pods found first], "error": ...} until done, and
# {"done": True} once it is. A job saved restarting by a version without it
# has none, and is not sent back to kube-vip.
KUBE_VIP_PODS = "/api/v1/namespaces/kube-system/pods"
# RKE2 can split roles: a server labelled etcd only still holds a vote in etcd.
SERVER_LABELS = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master", "node-role.kubernetes.io/etcd")


def _said(error):
    return str(error)[:160] or type(error).__name__


def _pod_key(pod):
    return pod["metadata"].get("uid") or pod["metadata"]["name"]


def _restart_kube_vip(read, send, node, ref):
    """Delete the kube-vip pods the job first found on node; True once none is
    left to delete. A reply can be lost after the delete landed, so a retry goes
    by what the cluster shows: a pod gone or going counts, and a replacement,
    even under the same name, is left alone."""
    state = ref.get("kube_vip") or {}
    if state.get("done"):
        return True
    try:
        found = read(f"{KUBE_VIP_PODS}?fieldSelector=spec.nodeName%3D{node}").get("items", [])
        pods = [pod for pod in found if pod["metadata"]["name"].startswith("kube-vip")]
        if "pods" not in state:
            state = {"pods": [_pod_key(pod) for pod in pods]}
            ref["kube_vip"] = state
        for pod in pods:
            if _pod_key(pod) in state["pods"] and not pod["metadata"].get("deletionTimestamp"):
                try:
                    send("DELETE", f"{KUBE_VIP_PODS}/{pod['metadata']['name']}")
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
    except Exception as error:
        ref["kube_vip"] = {**state, "error": _said(error)}
        return False
    ref["kube_vip"] = {"done": True}
    return True


# A server counts as up only while it is Ready and its kubelet renews its node
# Lease: Ready alone stays True about 40 s after the kubelet goes quiet (#1318).
# The kubelet renews about every quarter of the Lease's leaseDurationSeconds
# (10 s of the usual 40), so a renewal half that length old means at least one
# was missed. The stamp is on the server's clock and the age on Homestead's, so
# a stamp ahead counts within the same window, and one further ahead proves
# nothing. A Ready server whose stamp is further ahead, or more than
# CLOCK_BEHIND seconds behind (Ready would have dropped long before), points at
# clocks that disagree, and the words say so. When unsure, it counts as down.
SERVER_HEARD = 20       # the window when a Lease gives no length
CLOCK_BEHIND = 120


def _heartbeat(read, name, now):
    """name's kubelet, by its node Lease: "fresh", "stale", "clock" (the stamp
    is too far from Homestead's clock to judge) or "missing". A Lease not found
    is no proof; any other failure to read it is the caller's, as a failure to
    read the hosts is."""
    try:
        spec = read(NODE_LEASE.format(name)).get("spec") or {}
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "missing"
        raise
    renewed = kube_time(spec.get("renewTime"))
    if renewed is None:
        return "missing"
    try:
        window = int(spec.get("leaseDurationSeconds") or 0) / 2 or SERVER_HEARD
    except (TypeError, ValueError):
        window = SERVER_HEARD
    age = now - renewed
    if -window <= age < window:
        return "fresh"
    return "clock" if age < -window or age > CLOCK_BEHIND else "stale"


def other_servers(nodes, node, read, now):
    """The cluster's other control-plane servers: (their names, those down,
    those of them Ready but down because their clock disagrees). A server is
    down when it is not Ready or its kubelet was not heard from lately (read:
    the Kubernetes reader; now: Homestead's clock)."""
    names, down, clocks = [], [], []
    for found in nodes:
        meta = found.get("metadata") or {}
        if meta.get("name") == node or not any(k in (meta.get("labels") or {}) for k in SERVER_LABELS):
            continue
        name = meta["name"]
        names.append(name)
        if not any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in (found.get("status") or {}).get("conditions") or []):
            down.append(name)
            continue
        heard = _heartbeat(read, name, now)
        if heard != "fresh":
            down.append(name)
        if heard == "clock":
            clocks.append(name)
    return names, down, clocks


def _servers_down(node, count, clocks=0, names=()):
    """Why the other servers being down holds a change on node back, cause first."""
    who = f"{count} other server{'s' if count != 1 else ''}" + (f" ({', '.join(names)})" if names else "")
    one = count == 1
    if clocks and clocks == count:
        cause = (f"{who} {'is' if one else 'are'} Ready, but {'its clock disagrees' if one else 'their clocks disagree'} "
                 f"with Homestead's clock. Homestead cannot check {'its' if one else 'their'} heartbeat.")
    else:
        cause = f"{who} {'is' if one else 'are'} not Ready or {'has' if one else 'have'} sent no recent heartbeat."
        if clocks:
            cause += (f" The clock of {clocks} of them disagrees with Homestead's clock." if clocks == 1 else
                      f" The clocks of {clocks} of them disagree with Homestead's clock.")
    return f"{cause} If {node} stops answering too, the cluster could lose its quorum."


def guard_refusals(node, busy, servers_ready):
    """Why network_guard holds a change on node back: the same words for a
    bond and a bridge, since both take the host off the network for a moment
    and restart k3s or RKE2 there."""
    refusals = []
    if busy:
        refusals.append(f"another host's network is changing ({busy}): one host at a time")
    if servers_ready and servers_ready[1] and servers_ready[0] < servers_ready[1]:
        down, clocks = servers_ready[1] - servers_ready[0], (servers_ready[2:] or (0,))[0]
        way_out = "Set the same time on every host, then try again" if clocks == down else "Try again when every server is Ready"
        refusals.append(f"{_servers_down(node, down, clocks)} {way_out}")
    return refusals


def _before_restart(read, node, now):
    """What the restart of k3s or RKE2 on node goes by, read fresh each round:
    (hosts, why the list has no answer, the other servers down, those of them
    down by their clock). Restarting
    takes node off the network as the change did, so it waits for what
    network_guard does. The host itself is in the list, so an empty one is no answer."""
    try:
        nodes = read("/api/v1/nodes").get("items", [])
        _, down, clocks = other_servers(nodes, node, read, now)
    except Exception as error:
        return None, _said(error), [], []
    return (len(nodes), "", down, clocks) if nodes else (None, "the list came back empty", [], [])


def _restart_undone(node, service, unread, down, refused, clocks=()):
    """Why the restart was not sent, and what a person does about it."""
    undone = []
    if unread:
        undone.append(f"Homestead could not read the cluster's hosts ({unread}), so it did not restart {service} on {node}, "
                      "and flannel may still carry pod traffic by the old interface. If the cluster has other hosts, run "
                      f"`systemctl restart {service}` on {node}.")
    if down:
        undone.append(f"Homestead did not restart {service} on {node}, and flannel may still carry pod traffic by the old interface. "
                      f"{_servers_down(node, len(down), len(clocks), down)} "
                      f"When {'the clocks agree' if len(clocks) == len(down) else 'every server is Ready'}, "
                      f"run `systemctl restart {service}` on {node}.")
    if refused:
        undone.append(f"{node} did not schedule the restart of {service} ({refused}), so it did not restart, and flannel may still "
                      f"carry pod traffic by the old interface. Run `systemctl restart {service}` on {node}.")
    return undone


def confirmed_keeps(node):
    """What stopping a job says once the host's rollback is disarmed, for a
    bond and a bridge: nothing else would restart kube-vip, k3s or RKE2."""
    return [f"{node} keeps its new network: its rollback is already disarmed",
            f"Homestead stops restarting kube-vip and k3s or RKE2 on {node}: restart any it had not restarted by hand, "
            "or VIPs and flannel may stay on the old interface",
            f"Other changes on {node} can start again at once"]


def _kube_vip_undone(ref, interface):
    state = ref.get("kube_vip") or {}
    if "kube_vip" not in ref or state.get("done"):
        return []
    return [f"Homestead could not restart kube-vip on {ref['node']} ({state.get('error', 'no answer')}), so it may still announce "
            f"its addresses on the old interface. Delete the kube-vip pod on {ref['node']}, and it starts again on {interface}."]


def _not_finished(spec, ref, progress, elapsed, service="", unread="", down=(), refused="", clocks=()):
    """The next step of a job with kube-vip or the restart still not done:
    (status, progress, message). It fails past the deadline, saying what is undone."""
    node, interface = ref["node"], spec.carries_on(ref)
    vip_undone = _kube_vip_undone(ref, interface)
    if elapsed > spec.restart_seconds:
        return ("failed", 100, f"{node} carries {ref['address']} on {interface} and keeps it, but the change is not finished. "
                + " ".join(vip_undone + _restart_undone(node, service, unread, down, refused, clocks)))
    reach = (["the cluster's hosts"] if unread else []) + ([f"kube-vip on {node}"] if vip_undone else [])
    doing = []
    if reach:
        doing.append(f"still trying to reach {' and '.join(reach)} ({unread or ref['kube_vip'].get('error', 'no answer')})")
    if down:
        doing.append(f"waiting for {', '.join(down)} to be Ready with a recent heartbeat before it restarts {service}")
    if refused:
        doing.append(f"still trying to schedule the restart of {service} ({refused})")
    return ("running", progress, f"{node} is on its new network; {' and '.join(doing)}, "
            f"for up to {max(0, int(spec.restart_seconds - elapsed))}s more")


def _not_back(spec, ref):
    """Why a change failed whose host was not Ready again in time after k3s or
    RKE2 restarted on its new interface, and what to check on that host."""
    node, interface = ref["node"], spec.carries_on(ref)
    service = next((s for s in ref.get("services") or [] if s.startswith(("k3s", "rke2"))), "k3s")
    return (f"{node} was not Ready again {spec.restart_seconds // 60} minutes after {service} restarted on {interface}. "
            f"On {node}, check `journalctl -u {service}` and that {interface} carries {ref['address']}. "
            "If it becomes Ready later, nothing more is needed: the new network is kept. "
            f"The old configuration is in {ref['backup']}")


def kube_time(text):
    """Seconds since the epoch of a Kubernetes Time or MicroTime
    (2026-01-01T12:00:00Z, 2026-01-01T12:00:00.123456Z); None if none."""
    text = str(text or "")
    try:
        whole = calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    fraction = re.match(r"\.(\d+)", text[19:])
    return whole + (float("0." + fraction[1]) if fraction else 0)


def _ready(read, node):
    conditions = (read(f"/api/v1/nodes/{node}").get("status") or {}).get("conditions") or []
    return next((c for c in conditions if c.get("type") == "Ready"), {})


def _heard(ready):
    """Ready's heartbeat and transition times that are there."""
    return [t for t in (kube_time(ready.get("lastHeartbeatTime")), kube_time(ready.get("lastTransitionTime")))
            if t is not None]


def _renewed(read, node):
    """When the host's kubelet last renewed its Lease, or None. Any failure
    to read it (not found, forbidden, unreachable) gives None: the step then
    goes by Ready's times, never by less."""
    try:
        return kube_time((read(NODE_LEASE.format(node)).get("spec") or {}).get("renewTime"))
    except Exception:
        return None


def restart_baseline(read, node):
    """What the host's kubelet last reported, read right after the restart
    was asked for: renew0, its Lease's renewTime, and hb0, the later of
    Ready's heartbeat and transition. None for what could not be read."""
    try:
        heard = _heard(_ready(read, node))
    except Exception:
        heard = []
    return {"renew0": _renewed(read, node), "hb0": max(heard) if heard else None}


def kubelet_back(read, node, ref):
    """Whether node is Ready and its kubelet was heard from after the restart
    the step asked for (ref: its renew0, hb0 and since)."""
    ready = _ready(read, node)
    if ready.get("status") != "True":
        return False
    heard, renewed = _heard(ready), _renewed(read, node)
    renew0, hb0 = ref.get("renew0"), ref.get("hb0")
    if renew0 is not None and renewed is not None:
        return renewed > renew0 + BACK_AFTER
    if hb0 is not None:
        return any(t > hb0 for t in heard)
    # No baseline: nothing was readable when the restart was asked for, or the
    # job began before Homestead kept one. Only then compare with when the step
    # started, on Homestead's clock - a host clock far ahead can mislead this.
    after = ref["since"] + BACK_AFTER
    return any(t > after for t in heard) or (renewed is not None and renewed > after)


def _applied(spec, ref, node, elapsed, now):
    """The change is applied: look, and confirm only on a full answer."""
    if elapsed < 15:
        return "running", 20, f"Applying on {node}"
    hostrun = spec.bound()[0]
    try:
        out, _ = hostrun.run(node, spec.check(ref), timeout=45)
    except Exception:
        out = ""
    words, lines = set(out.split()), set(out.split("\n"))
    gave_up = spec.host_gave_up(ref, words)
    if gave_up:
        return gave_up
    needs, also = spec.needs(ref), spec.also_waiting(ref, lines)
    if {*needs, "ARMED"} <= words and not also:
        wait = spec.before_confirm(ref, words, elapsed, now)
        if wait:
            return wait
        out, _ = hostrun.run(node, spec.confirm_script, timeout=45, **held(spec, ref))
        if "CONFIRMED" not in out:
            return "running", spec.confirming_at, spec.confirming(ref)
        ref.update(stage="confirmed", since=now)
        return "running", spec.confirmed_at, spec.confirmed(ref, lines)
    if elapsed > spec.rollback_seconds + 60:
        return "failed", 100, spec.never_up(ref)
    waiting = [words_for for marker, words_for in needs.items() if marker not in words] + also
    return ("running", 30, f"Waiting for {', '.join(waiting or ['an answer from the host'])}; "
            f"{node} puts itself back in {max(0, int(spec.rollback_seconds - elapsed))}s")


def _confirmed(spec, ref, node, elapsed, now):
    """The rollback is disarmed: kube-vip, then k3s or RKE2 on a cluster."""
    if not spec.restarts(ref):
        ref.update(stage="done")
        return "succeeded", 100, spec.done(ref)
    hostrun, kget, ksend = spec.bound()
    # kube-vip finds its interface when it starts: its pod here starts again.
    vip = _restart_kube_vip(kget, ksend, node, ref)
    service = next((s for s in ref.get("services") or [] if s.startswith(("k3s", "rke2"))), "")
    hosts, unread, down, clocks = _before_restart(kget, node, now) if service else (1, "", [], [])
    refused = ""
    if hosts and hosts > 1 and not down:
        try:
            out, err = hostrun.run(node, f"systemd-run --on-active=2 systemctl restart {service} >/dev/null && echo OK", timeout=45,
                                   **held(spec, ref))
            refused = "" if "OK" in out.split() else (err or out or "no answer")[-160:]
        except POD_EXEC.ExecTimeout:
            pass    # it may have been scheduled: never twice; the Ready check below follows
        if not refused:
            ref.update(stage="restarting", since=now, **restart_baseline(kget, node))
            return ("running", spec.restarting_at,
                    f"Restarting {service} on {node}, so flannel carries pod traffic by {spec.carries_on(ref)}")
    if not vip or unread or down or refused:
        return _not_finished(spec, ref, spec.confirmed_at, elapsed, service, unread, down, refused, clocks)
    ref.update(stage="done")
    return "succeeded", 100, spec.done(ref)


def _restarting(spec, ref, node, elapsed):
    """k3s or RKE2 was asked to restart: end on its kubelet, or on a deadline.
    A kube-vip still unproved is tried again; one saved without the field is not."""
    _, kget, ksend = spec.bound()
    vip = "kube_vip" not in ref or _restart_kube_vip(kget, ksend, node, ref)
    if elapsed > 30 and kubelet_back(kget, node, ref):
        if not vip:
            return _not_finished(spec, ref, spec.waiting_at, elapsed)
        ref.update(stage="done")
        return "succeeded", 100, spec.done(ref)
    if elapsed > spec.restart_seconds:
        return "failed", 100, " ".join([_not_back(spec, ref), *_kube_vip_undone(ref, spec.carries_on(ref))])
    return "running", spec.waiting_at, f"Waiting for {node} to be Ready again"


def _step(spec, item, now):
    ref = item["ref"]
    node, elapsed = ref["node"], now - ref.get("since", now)
    if ref["stage"] == "unconfirmed":
        return _still_unconfirmed(spec, ref, now)
    if ref["stage"] == "applied":
        return _applied(spec, ref, node, elapsed, now)
    if ref["stage"] == "confirmed":
        return _confirmed(spec, ref, node, elapsed, now)
    if ref["stage"] == "restarting":
        return _restarting(spec, ref, node, elapsed)
    return spec.other_stage(ref, elapsed)
