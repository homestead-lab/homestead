#!/usr/bin/env python3
"""Write charts/homestead, the Helm chart, from the manifests Homestead ships.

The chart installs the same objects as deploy/deploy.yaml and, optionally,
deploy/nodeprobe.yaml: its permissions are taken from deploy.yaml word for
word and its probe from homestead_probe, so the three cannot disagree. Only
the parts someone chooses at install - namespaces, address, storage - become
values. tests/test_chart.py fails if the checked-in chart and this output
disagree.

    python scripts/render_chart.py
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "homestead"
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))

import homestead_probe as probe  # noqa: E402
import render_nodeprobe  # noqa: E402

RBAC_KINDS = ("ServiceAccount", "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding")
APPS = "{{ .Values.workloadNamespace.name }}"
SELF = "{{ .Release.Namespace }}"


def version():
    return render_nodeprobe.current_version()


CHART_YAML = """apiVersion: v2
name: homestead
description: Homelab control panel for Harvester, k3s and RKE2 - containers, VMs, Longhorn storage, shares and backups in one place.
type: application
version: {version}
appVersion: "{version}"
kubeVersion: ">=1.25.0-0"
home: https://github.com/wjcloudy/homestead
sources:
  - https://github.com/wjcloudy/homestead
icon: https://raw.githubusercontent.com/wjcloudy/homestead/main/web/icons/icon-192.png
keywords: [homelab, harvester, longhorn, kubevirt, k3s, rke2, dashboard]
maintainers:
  - name: wjcloudy
    url: https://github.com/wjcloudy
annotations:
  artifacthub.io/license: MIT
  artifacthub.io/category: monitoring-logging
  artifacthub.io/links: |
    - name: Source
      url: https://github.com/wjcloudy/homestead
  artifacthub.io/images: |
    - name: homestead
      image: ghcr.io/wjcloudy/homestead:{version}
    - name: probe
      image: python:3.12-alpine
"""

VALUES = """# Homestead's Helm values. Everything else is fixed by the release.

image:
  repository: ghcr.io/wjcloudy/homestead
  # Empty: the chart's appVersion, which is the release this chart came with.
  tag: ""
  pullPolicy: IfNotPresent

# Where Homestead deploys containers, shares and the node probe. It can be the
# namespace Homestead itself is installed in. An existing namespace is used as
# it is; one Homestead creates is kept when the chart is uninstalled, because
# your apps live in it.
workloadNamespace:
  name: lab
  create: true

service:
  type: LoadBalancer
  port: 8088
  # The LAN address Homestead is reached on. Empty lets the load balancer pick.
  loadBalancerIP: ""
  # Harvester's load balancer (kube-vip) takes the address from an annotation.
  kubeVip: true
  annotations: {}

persistence:
  # Settings, users, history and the audit log. Off keeps them only until the
  # pod restarts.
  enabled: true
  # Empty: the cluster's default class. On Harvester a migratable class
  # (harvester-longhorn, longhorn-r2) cannot be mounted by a second node, so
  # running more than one copy of Homestead needs a shareable class later.
  storageClass: ""
  accessMode: ReadWriteOnce
  size: 2Gi
  existingClaim: ""

# The class new volumes default to on the Deploy and Import pages. Empty: the
# cluster's default.
storageClass: ""

# Homestead published through a Cloudflare Tunnel behind Cloudflare Access:
# requests that came through Cloudflare without Access's signature are refused.
cloudflareAccess:
  teamDomain: ""
  aud: ""

resources:
  requests:
    cpu: 50m
    memory: 96Mi
  limits:
    memory: 256Mi

# Homestead's permissions. Off only if you manage an equivalent ClusterRole
# yourself; Homestead keeps its own role current after install either way.
rbac:
  create: true

# Temperatures, host devices (a Coral, a Zigbee stick, an iGPU), every disk and
# drive health from each node. Its SMART reader runs privileged, because
# smartctl needs the raw drives; turn it off to go without drive health.
nodeprobe:
  enabled: true
"""

HELPERS = """{{- define "homestead.labels" -}}
app: homestead
app.kubernetes.io/name: homestead
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "homestead.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}
"""

NOTES = """Homestead {{ .Chart.AppVersion }} is starting in {{ .Release.Namespace }}.

{{- if eq .Values.service.type "LoadBalancer" }}
{{- if .Values.service.loadBalancerIP }}
Open http://{{ .Values.service.loadBalancerIP }}:{{ .Values.service.port }}
{{- else }}
Its address, once the load balancer has given one:
  kubectl -n {{ .Release.Namespace }} get service homestead
{{- end }}
{{- end }}

The first visit sets up the admin account. Apps are deployed to the
{{ .Values.workloadNamespace.name }} namespace.
{{- if .Values.nodeprobe.enabled }}
The node probe reports temperatures, devices and drive health from every node.
{{- end }}

There is only one Homestead per cluster: its objects have fixed names.
"""

NAMESPACE = """{{- if and .Values.workloadNamespace.create (ne .Values.workloadNamespace.name .Release.Namespace) }}
{{- if not (lookup "v1" "Namespace" "" .Values.workloadNamespace.name) }}
apiVersion: v1
kind: Namespace
metadata:
  name: {{ .Values.workloadNamespace.name }}
  annotations:
    # Your apps live here: uninstalling Homestead must not take them with it.
    helm.sh/resource-policy: keep
{{- end }}
{{- end }}
"""

HOMESTEAD = """{{- $image := include "homestead.image" . }}
{{- if and .Values.persistence.enabled (not .Values.persistence.existingClaim) }}
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: homestead-data
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "homestead.labels" . | nindent 4 }}
  annotations:
    # Settings, users and history outlive an uninstall.
    helm.sh/resource-policy: keep
spec:
  accessModes: [{{ .Values.persistence.accessMode }}]
  {{- with .Values.persistence.storageClass }}
  storageClassName: {{ . }}
  {{- end }}
  resources:
    requests:
      storage: {{ .Values.persistence.size }}
---
{{- end }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: homestead
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "homestead.labels" . | nindent 4 }}
  annotations:
    homestead.io/update-sources: {{ printf "{\\"homestead\\":\\"%s\\"}" $image | squote }}
spec:
  replicas: 1
  strategy:
    # One pod mounts the data volume at a time; see deploy/deploy.yaml.
    type: Recreate
  selector:
    matchLabels:
      app: homestead
  template:
    metadata:
      labels:
        app: homestead
        lab-workload: "true"
    spec:
      serviceAccountName: homestead
      securityContext:
        fsGroup: 10001
        fsGroupChangePolicy: OnRootMismatch
      terminationGracePeriodSeconds: 10
      tolerations:
        - key: node.kubernetes.io/unreachable
          operator: Exists
          effect: NoExecute
          tolerationSeconds: 15
        - key: node.kubernetes.io/not-ready
          operator: Exists
          effect: NoExecute
          tolerationSeconds: 15
      initContainers:
        - name: data-permissions
          image: {{ $image }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command: ["sh", "-c", "chown 10001:10001 /data && chmod 0770 /data"]
          securityContext:
            runAsUser: 0
            runAsGroup: 0
            runAsNonRoot: false
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
              add: ["CHOWN", "FOWNER", "DAC_OVERRIDE"]
          volumeMounts:
            - name: data
              mountPath: /data
      containers:
        - name: homestead
          image: {{ $image }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          env:
            - name: PORT
              value: "8080"
            - name: WEBROOT
              value: "/web"
            - name: DATA_DIR
              value: "/data"
            - name: DEFAULT_NS
              value: {{ .Values.workloadNamespace.name | quote }}
            - name: SMB_NAMESPACE
              value: {{ .Values.workloadNamespace.name | quote }}
            - name: STORAGE_CLASS
              value: {{ .Values.storageClass | quote }}
            - name: LB_IP
              value: {{ .Values.service.loadBalancerIP | quote }}
            - name: CF_ACCESS_TEAM_DOMAIN
              value: {{ .Values.cloudflareAccess.teamDomain | quote }}
            - name: CF_ACCESS_AUD
              value: {{ .Values.cloudflareAccess.aud | quote }}
          ports:
            - containerPort: 8080
              name: http
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
          securityContext:
            runAsUser: 10001
            runAsGroup: 10001
            runAsNonRoot: true
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 3
            timeoutSeconds: 3
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 15
            timeoutSeconds: 3
            periodSeconds: 20
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          {{- if not .Values.persistence.enabled }}
          emptyDir: {}
          {{- else }}
          persistentVolumeClaim:
            claimName: {{ .Values.persistence.existingClaim | default "homestead-data" }}
          {{- end }}
---
apiVersion: v1
kind: Service
metadata:
  name: homestead
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "homestead.labels" . | nindent 4 }}
  {{- $annotations := deepCopy .Values.service.annotations }}
  {{- if and .Values.service.loadBalancerIP .Values.service.kubeVip }}
  {{- $_ := set $annotations "kube-vip.io/loadbalancerIPs" .Values.service.loadBalancerIP }}
  {{- end }}
  {{- with $annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  type: {{ .Values.service.type }}
  {{- if and .Values.service.loadBalancerIP (not .Values.service.kubeVip) }}
  loadBalancerIP: {{ .Values.service.loadBalancerIP }}
  {{- end }}
  selector:
    app: homestead
  ports:
    - name: http
      port: {{ .Values.service.port }}
      targetPort: 8080
      protocol: TCP
"""

PROBE_CONFIG = """{{- if .Values.nodeprobe.enabled }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: homestead-nodeprobe
  namespace: {{ .Values.workloadNamespace.name }}
data:
  {{- (.Files.Glob "files/probe/*").AsConfig | nindent 2 }}
{{- end }}
"""


def rbac():
    """The permission objects from deploy.yaml, with their namespaces made
    Homestead's own (the service account) or the apps' (the console role)."""
    text = (ROOT / "deploy" / "deploy.yaml").read_text(encoding="utf-8")
    docs = [d.strip("\n") for d in re.split(r"(?m)^---\s*$", text)]
    out = []
    for doc in docs:
        kind = re.search(r"(?m)^kind: (\w+)\s*$", doc)
        if not kind or kind.group(1) not in RBAC_KINDS:
            continue
        # metadata.namespace sits at two spaces, a subject's at four.
        if kind.group(1) in ("Role", "RoleBinding"):
            doc = re.sub(r"(?m)^  namespace: lab$", f"  namespace: {APPS}", doc)
        else:
            doc = re.sub(r"(?m)^  namespace: lab$", f"  namespace: {SELF}", doc)
        doc = re.sub(r"(?m)^    namespace: lab$", f"    namespace: {SELF}", doc)
        out.append(doc)
    return "{{- if .Values.rbac.create }}\n" + "\n---\n".join(out) + "\n{{- end }}\n"


def probe_daemonset(release):
    daemonset = [d for d in probe.manifest(release, namespace="__APPS__") if d["kind"] == "DaemonSet"][0]
    text = "\n".join(render_nodeprobe.emit(daemonset)) + "\n"
    text = text.replace("namespace: __APPS__", f"namespace: {APPS}")
    text = text.replace(f"ghcr.io/wjcloudy/homestead:{release}", '{{ include "homestead.image" . }}')
    # The probe restarts when its scripts change, as Homestead does itself.
    marker = "    metadata:\n      labels:\n        app: homestead-nodeprobe\n"
    assert marker in text, "the probe's pod template changed shape"
    text = text.replace(marker, marker + "      annotations:\n"
                        "        checksum/scripts: {{ (.Files.Glob \"files/probe/*\").AsConfig | sha256sum }}\n", 1)
    return "{{- if .Values.nodeprobe.enabled }}\n" + text + "{{- end }}\n"


def files(release):
    """Every file of the chart: path -> text."""
    out = {
        "Chart.yaml": CHART_YAML.format(version=release),
        "values.yaml": VALUES,
        ".helmignore": "*.tgz\n.git/\n",
        "templates/_helpers.tpl": HELPERS,
        "templates/NOTES.txt": NOTES,
        "templates/namespace.yaml": NAMESPACE,
        "templates/rbac.yaml": rbac(),
        "templates/homestead.yaml": HOMESTEAD,
        "templates/nodeprobe-scripts.yaml": PROBE_CONFIG,
        "templates/nodeprobe.yaml": probe_daemonset(release),
    }
    for name, text in probe.shipped_scripts().items():
        out[f"files/probe/{name}"] = text
    return out


if __name__ == "__main__":
    release = version()
    if CHART.exists():
        shutil.rmtree(CHART)
    for rel, text in files(release).items():
        path = CHART / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote charts/homestead for {release}")
