"""The last step of powering off or rebooting a cluster's only host.

Homestead runs on that host, and its data volume is a Longhorn volume: if
the host goes down with Homestead still writing to it, Longhorn stops under
a mounted filesystem and the volume comes back faulted, to be salvaged.

So Homestead hands the last step to this helper and stops itself. The helper
runs on the host from Homestead's own image. Before power, it waits for
Homestead to stop and its data volume to detach, then asks systemd for the
reboot or power-off. The power kills it; Kubernetes restarts it on the new
boot (restartPolicy OnFailure), and it starts Homestead again and exits.

If the volume does not detach, or the host never goes down, it starts
Homestead again without sending anything. It only ever undoes a hold that
names its own job, so a person who has since changed Homestead wins.
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

HELD_BY = "homestead.io/held-for-power"
HELD_AS = "homestead.io/held-as"
HOST = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh", "-c"]
LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes"
DETACH_LIMIT = 300        # Homestead stopped and its volume detached by then, or no power
POWER_LIMIT = 600         # the host down by then, or Homestead comes back


def say(message):
    print(time.strftime("%H:%M:%S ") + message, flush=True)


class Handoff:
    def __init__(self, request, namespace, deployment, job, action, boot, claim,
                 execute=subprocess.check_output, sleep=time.sleep, clock=time.time):
        if action not in ("reboot", "poweroff"):
            raise ValueError("action must be reboot or poweroff")
        self.request, self.ns, self.deployment, self.job = request, namespace, deployment, job
        self.action, self.boot, self.claim = action, boot, claim
        self.execute, self.sleep, self.clock = execute, sleep, clock
        self.path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"

    def boot_id(self):
        return self.execute(HOST + ["cat /proc/sys/kernel/random/boot_id"], timeout=15).decode().strip()

    def held(self, deployment):
        return ((deployment.get("metadata") or {}).get("annotations") or {}).get(HELD_BY) == self.job

    def start_homestead(self, why):
        """Put Homestead back as the job found it - only while the hold is ours."""
        deployment = self.request("GET", self.path)
        if not self.held(deployment):
            say(f"{why}; Homestead is no longer held by this job, so it is left as it is")
            return
        was = ((deployment.get("metadata") or {}).get("annotations") or {}).get(HELD_AS, "1")
        self.request("PATCH", self.path, {"metadata": {"annotations": {HELD_BY: None, HELD_AS: None}},
                                          "spec": {"replicas": int(was) if str(was).isdigit() else 1}},
                     "application/merge-patch+json")
        say(f"{why}; Homestead is starting again")

    def volume_detached(self):
        if not self.claim:
            return True
        claim = self.request("GET", f"/api/v1/namespaces/{self.ns}/persistentvolumeclaims/{self.claim}")
        name = (claim.get("spec") or {}).get("volumeName")
        if not name:
            return True
        try:
            volume = self.request("GET", f"{LH}/{name}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return True        # not a Longhorn volume: nothing of Longhorn's to wait for
            raise
        return (volume.get("status") or {}).get("state") == "detached"

    def run(self):
        if self.boot_id() != self.boot:
            # Restarted on the new boot: the host went down and is back.
            self.wait_for_api()
            self.start_homestead("The host is back")
            return 0
        say(f"Waiting for Homestead to stop and its data volume to detach before the {self.action}")
        deadline = self.clock() + DETACH_LIMIT
        while True:
            deployment = self.request("GET", self.path)
            if not self.held(deployment):
                say("Homestead is no longer held by this job; nothing is sent")
                return 0
            running = int((deployment.get("status") or {}).get("replicas") or 0)
            if not running and self.volume_detached():
                break
            if self.clock() >= deadline:
                self.start_homestead("Homestead's data volume did not detach within 5 minutes, so power was not sent")
                return 0
            self.sleep(2)
        # systemd owns the timer once this pod is gone. Nothing from the
        # request reaches the shell: the job ID and action are checked.
        unit = "homestead-power-" + "".join(c for c in self.job if c.isalnum())[:24]
        self.execute(HOST + [f'systemd-run --unit={unit} --on-active=3s --timer-property=AccuracySec=1s "$(command -v systemctl)" {self.action}'],
                     timeout=15)
        say(f"Homestead has stopped and its data volume detached; the {self.action} is requested in 3 seconds")
        deadline = self.clock() + POWER_LIMIT
        while self.clock() < deadline:
            self.sleep(5)          # the power stops this pod; it restarts on the new boot
        self.start_homestead("The host did not go down within 10 minutes of the request")
        return 0

    def wait_for_api(self):
        for _ in range(120):
            try:
                self.request("GET", self.path)
                return
            except (OSError, urllib.error.URLError):
                self.sleep(5)


def main():
    namespace, deployment, job, action, boot, claim = sys.argv[1:7]
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    root = "https://" + ("[" + host + "]" if ":" in host else host) + ":" + os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    sa = "/var/run/secrets/kubernetes.io/serviceaccount/"
    context = ssl.create_default_context(cafile=sa + "ca.crt")

    def request(method, path, body=None, ctype="application/json"):
        with open(sa + "token", encoding="utf-8") as handle:
            token = handle.read().strip()
        req = urllib.request.Request(root + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": ctype})
        with urllib.request.urlopen(req, context=context, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    sys.exit(Handoff(request, namespace, deployment, job, action, boot, claim).run())


if __name__ == "__main__":
    main()
