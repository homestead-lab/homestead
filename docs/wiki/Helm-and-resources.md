# Helm and Resources

Homestead's own pages cover what a homelab mostly does. These two cover
everything else, for anyone used to Headlamp, Lens or kubectl.

## Helm

**Helm** lists every Helm release in the cluster - chart, version, status,
revision - whoever installed it: you, Rancher, Fleet or the platform. A release
opens to the values it was installed with, its notes, history and the objects it
made. The platform's own releases (Harvester, Rancher, Longhorn) are hidden
until you ask.

![Helm](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-helm.png)

**Install chart** searches [Artifact Hub](https://artifacthub.io), shows a
chart's versions and default values beside yours, and installs it through the
Helm controller k3s and RKE2 run - as a `HelmChart` object anyone can see with
kubectl. Changing the version or values upgrades it; uninstalling removes it and
what it made. A repository can be typed in by hand, `oci://` registries
included.

Releases installed some other way (the `helm` command, Rancher) are shown but
not changed, since whatever installed them would change them back. A cluster
without a Helm controller (kubeadm, most managed clusters) lists releases but
cannot install.

## Resources

**System → Resources** lists every kind the cluster serves - built-in,
Harvester's, Longhorn's, KubeVirt's, any custom resource - grouped as Headlamp
groups them, each with the columns `kubectl get` prints for it.

![Resources](https://github.com/wjcloudy/homestead/releases/latest/download/homestead-resources.png)

An object opens as YAML (without the managed-fields clutter), with its recent
events, and a pod with its logs. Admins can:

- **edit and save** - refused, not overwritten, if someone changed it meanwhile;
- **delete**, after typing its name;
- **create** from pasted YAML, several documents at once.

Secrets show their keys; values only when an admin asks to reveal them.
