#!/bin/sh
# Homestead's installer and node doctor: one line, then a menu.
#
#   curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
#
# It says what it found on the machine and offers what fits: installing
# Homestead (and a cluster under it), or - on a node of a k3s, RKE2, Harvester
# or plain Kubernetes cluster - checking the node and its cluster and fixing
# what is wrong, cleaning up, and taking or restoring etcd snapshots.
#
# Installing looks at the machine first and does what fits:
#   - a bare Linux machine: a new k3s cluster with Longhorn (and KubeVirt, if
#     wanted) and Homestead - or this machine joined to one, as a server or a
#     worker;
#   - a k3s server already running: Longhorn and Homestead added to it;
#   - a Harvester host: Homestead installed into Harvester, on an address you
#     choose.
# Before anything changes it checks the machine - memory, disk, the network,
# ports, the clock, the firewall - says what it found, and shows what it is
# about to do.
#
# The questions come as menus where the machine has whiptail or dialog, as
# plain prompts otherwise. Every answer can be given ahead instead, for an
# unattended install:
#   HS_ROLE=new|server|agent|addons|harvester   what to do
#   HS_NODE_IP=192.168.1.50                     this machine's address
#   HS_SERVER=192.168.1.50  HS_TOKEN=...        the cluster to join
#   HS_LONGHORN=yes|no  HS_KUBEVIRT=yes|no      what a new cluster gets
#   HS_VIP=192.168.1.242  HS_CLASS=harvester-longhorn   Harvester: Homestead's address and storage
#   HS_YES=1                                    no "go ahead?" question
# The doctor checks the host (the Kubernetes service, disk, memory, clock,
# the iSCSI and multipath settings Longhorn needs, certificates, containerd)
# and the cluster (API, etcd, this node, the others, failing or stuck pods,
# Longhorn volumes, DNS, Homestead, etcd snapshots), lists what it found worst
# first, and offers each fix - asking before every change.
#
# Options: --install / --doctor   go straight there instead of the menu
#          --report      check, print, exit 0 well / 1 worth a look / 2 wrong
#          --fix-safe    check, then apply every fix marked safe
#          --dry-run     show the commands, change nothing
#          --text        plain prompts instead of menus
#          --ref v2.8.176  that release's scripts and Homestead
#          --skip-checks   installing: report the checks, but go on regardless
set -u

RAW="https://raw.githubusercontent.com/wjcloudy/homestead"
REF="${HOMESTEAD_REF:-main}"
DRY=0
ACTION=""
SKIP_CHECKS=0
UI=""
TTY=/dev/tty
WIKI="https://github.com/wjcloudy/homestead/wiki"
FAILED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --install) ACTION=install; shift ;;
    --doctor) ACTION=doctor; shift ;;
    --report) ACTION=report; UI=text; shift ;;
    --fix-safe) ACTION=fix-safe; UI=text; shift ;;
    --skip-checks) SKIP_CHECKS=1; shift ;;
    --text) UI=text; shift ;;
    --ref) REF="$2"; shift 2 ;;
    -h|--help) sed -n '2,46p' "$0" 2>/dev/null; exit 0 ;;
    *) printf 'unknown option %s (try --help)\n' "$1" >&2; exit 2 ;;
  esac
done

# ------------------------------------------------------------------ output
say() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }
run() {
  # Every change goes through here, so --dry-run shows it instead.
  { printf '%s run: %s\n' "$(date '+%F %T' 2>/dev/null)" "$*" >> /var/log/homestead-doctor.log; } 2>/dev/null || true
  if [ "$DRY" = 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi
}

# ------------------------------------------------------------------ questions
have() { command -v "$1" >/dev/null 2>&1; }
interactive() { [ -r "$TTY" ] && [ -w "$TTY" ] && (: < "$TTY") 2>/dev/null; }

# HS_UI=whiptail|dialog|text picks the look; otherwise the best there is.
[ -z "$UI" ] && [ -n "${HS_UI:-}" ] && UI="$HS_UI"
if [ -z "$UI" ]; then
  if ! interactive || [ "${TERM:-dumb}" = dumb ]; then UI=text
  elif have whiptail; then UI=whiptail
  elif have dialog; then UI=dialog
  else UI=text; fi
fi
BOX="$UI"

# A menu box as tall as its text and list need, within the terminal.
box_menu() { # title text tag item...
  title="$1"; text="$2"; shift 2
  items=$(( $# / 2 ))
  rows=$(stty size < "$TTY" 2>/dev/null | cut -d' ' -f1); rows=${rows:-24}
  lines=$(printf '%s\n' "$text" | wc -l | tr -d ' ')
  list=$items; [ "$list" -gt $((rows - lines - 9)) ] && list=$((rows - lines - 9)); [ "$list" -lt 3 ] && list=3
  height=$((lines + list + 8)); [ "$height" -gt "$rows" ] && height=$rows
  "$BOX" --title "$title" --cancel-button "Back" --menu "$text" "$height" 90 "$list" "$@" 3>&1 1>"$TTY" 2>&3 < "$TTY"
}

# The answer given ahead in the environment, if there is one.
given() { eval "printf '%s' \"\${$1:-}\""; }

need_tty() { interactive || fail "no terminal to ask on: give the answer as $1=... (see --help)"; }

msg() { # title text
  if [ "$UI" = text ]; then printf '\n-- %s --\n%s\n' "$1" "$2"
  else "$BOX" --title "$1" --msgbox "$2" 20 76 < "$TTY" > "$TTY" 2>&1; fi
}

yesno() { # var title text default(yes|no) -> 0 for yes
  answer=$(given "$1")
  if [ -n "$answer" ]; then case "$answer" in y*|Y*|1|true) return 0 ;; *) return 1 ;; esac; fi
  need_tty "$1"
  if [ "$UI" = text ]; then
    printf '\n%s\n%s [%s] ' "$2" "$3" "$([ "$4" = yes ] && echo Y/n || echo y/N)" > "$TTY"
    read -r reply < "$TTY" || reply=""
    [ -z "$reply" ] && reply="$4"
    case "$reply" in y*|Y*) return 0 ;; *) return 1 ;; esac
  fi
  if [ "$4" = no ]; then "$BOX" --title "$2" --defaultno --yesno "$3" 18 76 < "$TTY" > "$TTY" 2>&1
  else "$BOX" --title "$2" --yesno "$3" 18 76 < "$TTY" > "$TTY" 2>&1; fi
}

ask() { # var title text default [secret] -> the answer on stdout
  answer=$(given "$1")
  if [ -n "$answer" ]; then printf '%s' "$answer"; return; fi
  need_tty "$1"
  if [ "$UI" = text ]; then
    printf '\n%s\n%s%s: ' "$2" "$3" "$([ -n "$4" ] && printf ' [%s]' "$4")" > "$TTY"
    if [ "${5:-}" = secret ]; then stty -echo < "$TTY" 2>/dev/null; fi
    read -r reply < "$TTY" || reply=""
    if [ "${5:-}" = secret ]; then stty echo < "$TTY" 2>/dev/null; printf '\n' > "$TTY"; fi
    printf '%s' "${reply:-$4}"; return
  fi
  kind=--inputbox; [ "${5:-}" = secret ] && kind=--passwordbox
  "$BOX" --title "$2" "$kind" "$3" 14 76 "$4" 3>&1 1>"$TTY" 2>&3 < "$TTY" || exit 1
}

choose() { # var title text tag item [tag item...] -> the tag on stdout
  answer=$(given "$1")
  if [ -n "$answer" ]; then printf '%s' "$answer"; return; fi
  var="$1"; title="$2"; text="$3"; shift 3
  need_tty "$var"
  if [ "$UI" = text ]; then
    # Tags and items alternate: the items are listed, and the nth tag chosen.
    printf '\n%s\n%s\n' "$title" "$text" > "$TTY"
    i=0; for word in "$@"; do i=$((i+1)); [ $((i % 2)) = 0 ] && printf '  %d) %s\n' $((i / 2)) "$word" > "$TTY"; done
    printf 'Choose 1-%d [1]: ' $(( $# / 2 )) > "$TTY"
    read -r reply < "$TTY" || reply=""
    [ -z "$reply" ] && reply=1
    i=0; for word in "$@"; do i=$((i+1)); if [ $((i % 2)) = 1 ] && [ $(( (i + 1) / 2 )) = "$reply" ]; then printf '%s' "$word"; return; fi; done
    fail "no choice $reply"
  fi
  box_menu "$title" "$text" "$@" || exit 1
}

# ------------------------------------------------------------------ the machine
os_name() { . /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-$ID}"; }
mem_mb() { awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0; }
disk_gb() { df -Pk /var 2>/dev/null | awk 'NR==2 {print int($4/1048576)}'; }
default_ip() { ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}'; }
all_ips() { ip -4 -o addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print $2, a[1]}'; }
dynamic_ip() { ip -4 addr show 2>/dev/null | grep -F "inet $1/" | grep -q dynamic; }
listening() { (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | awk '{print $4}' | grep -Eq "[:.]$1\$"; }
reachable() { code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1" 2>/dev/null); [ -n "$code" ] && [ "$code" != 000 ]; }
is_harvester() { grep -qi harvester /etc/os-release 2>/dev/null || [ -f /etc/rancher/rancherd/config.yaml ]; }
active() { systemctl is-active --quiet "$1" 2>/dev/null; }
kvm() { [ -e /dev/kvm ] || grep -Eq 'vmx|svm' /proc/cpuinfo 2>/dev/null; }

# ------------------------------------------------------------------ checks
REPORT=""
check() { # ok|warn|fail text
  case "$1" in
    ok) REPORT="$REPORT
  ok    $2" ;;
    warn) REPORT="$REPORT
  note  $2" ;;
    fail) REPORT="$REPORT
  STOP  $2"; FAILED=1 ;;
  esac
}

prechecks() { # role
  REPORT=""; FAILED=0
  [ "$(id -u)" = 0 ] && check ok "running as root" || check fail "run with sudo: curl ... | sudo sh"
  have curl && check ok "curl is here" || check fail "curl is needed"
  have systemctl && check ok "systemd runs this machine" || check fail "k3s needs systemd"
  case "$(uname -m)" in
    x86_64|amd64|aarch64|arm64) check ok "$(os_name) on $(uname -m)" ;;
    *) check fail "$(uname -m) is not a platform k3s and Homestead run on (x86-64 or arm64)" ;;
  esac
  mem=$(mem_mb); cpus=$(nproc 2>/dev/null || echo 1)
  if [ "$1" = agent ]; then want=1024; else want=2048; fi
  if [ "$mem" -lt "$want" ]; then check fail "${mem} MB of memory: a $1 needs at least $want MB"
  elif [ "$mem" -lt 4096 ] && [ "$1" = new ]; then check warn "${mem} MB of memory is enough to start; Longhorn and VMs want 8 GB or more"
  else check ok "${mem} MB of memory, $cpus CPUs"; fi
  free=$(disk_gb); free=${free:-0}
  if [ "$free" -lt 10 ]; then check fail "${free} GB free under /var: k3s and its images need at least 10 GB"
  elif [ "$free" -lt 30 ]; then check warn "${free} GB free under /var - enough to start; volumes and images fill it quickly"
  else check ok "${free} GB free under /var"; fi
  host=$(hostname)
  if printf '%s' "$host" | grep -Eq '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$' && [ "$host" != localhost ]; then
    check ok "hostname $host"
  else
    check fail "hostname '$host' cannot be a Kubernetes node name: lower-case letters, digits and dashes (hostnamectl set-hostname node1)"
  fi
  if reachable https://get.k3s.io && reachable https://ghcr.io/v2/; then check ok "the internet is reachable (get.k3s.io, ghcr.io)"
  else check fail "cannot reach get.k3s.io or ghcr.io: this machine needs the internet to install"; fi
  if [ "$1" != addons ]; then
    for port in 6443 10250; do
      listening "$port" && check fail "port $port is in use: another Kubernetes, or something else, is running here"
    done
  fi
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; then check ok "the clock is synchronised"
  else check warn "the clock is not synchronised (timedatectl): certificates and joining can fail on a wrong clock"; fi
  if active ufw || active firewalld; then
    check warn "a firewall is on: open 6443/tcp, 10250/tcp, 8472/udp and 2379-2380/tcp between the machines, and 8088/tcp for Homestead"
  fi
  if [ -n "${NODE_IP:-}" ] && dynamic_ip "$NODE_IP"; then
    check warn "$NODE_IP comes from DHCP: reserve it on your router, or a changed address breaks the cluster"
  fi
  if [ "$1" = new ]; then
    kvm && check ok "hardware virtualisation is on (VMs run at full speed)" \
      || check warn "no hardware virtualisation (/dev/kvm): VMs would be emulated and slow"
  fi
  if [ "$FAILED" = 1 ] && [ "$SKIP_CHECKS" = 0 ]; then
    msg "Checks - cannot go on" "These have to be put right first:
$REPORT"
    fail "the checks above stopped the install"
  fi
  msg "Checks" "This machine:
$REPORT"
}

# ------------------------------------------------------------------ progress
LOG=/var/log/homestead-install.log

# A long step, run with a progress bar that follows its "==> " stages - the
# whole output goes to $LOG, and is shown if the step fails. Without menus
# the output simply scrolls past, as it always did.
progress() { # title stages command...
  title="$1"; stages="$2"; shift 2
  if [ "$UI" = text ] || [ "$DRY" = 1 ]; then run "$@"; return; fi
  : > "$LOG" 2>/dev/null || LOG=/tmp/homestead-install.log
  status=/tmp/homestead-install.status
  rm -f "$status"
  { "$@" > "$LOG" 2>&1; echo $? > "$status"; } &
  job=$!
  {
    while [ ! -s "$status" ]; do
      done_stages=$(grep -c '^==> ' "$LOG" 2>/dev/null || echo 0)
      stage=$(grep '^==> ' "$LOG" 2>/dev/null | tail -n 1 | cut -c5-)
      pct=$(( done_stages * 95 / stages )); [ "$pct" -gt 95 ] && pct=95
      printf 'XXX\n%d\n%s\n\n%s\nXXX\n' "$pct" "${stage:-Starting}" "$(tail -n 1 "$LOG" 2>/dev/null | cut -c1-70)"
      sleep 1
    done
    printf 'XXX\n100\nDone\nXXX\n'
  } | "$BOX" --title "$title" --gauge "Starting" 12 78 0 > "$TTY" 2>&1
  wait "$job" 2>/dev/null
  code=$(cat "$status" 2>/dev/null || echo 1)
  if [ "$code" != 0 ]; then
    tail -n 40 "$LOG" > /tmp/homestead-install.tail 2>/dev/null
    "$BOX" --title "$title - stopped" --scrolltext --textbox /tmp/homestead-install.tail 24 100 < "$TTY" > "$TTY" 2>&1
    fail "the install stopped; the whole log is in $LOG"
  fi
}

# ------------------------------------------------------------------ bootstrap
bootstrap() { # stages, then args for bootstrap-k3s.sh
  stages="$1"; shift
  script=/tmp/homestead-bootstrap-k3s.sh
  run curl -sfL "$RAW/$REF/scripts/bootstrap-k3s.sh" -o "$script" || fail "could not download bootstrap-k3s.sh"
  progress "Installing" "$stages" sh "$script" "$@" || fail "the install stopped; the lines above say where"
}

versions() {
  if [ "$REF" != main ]; then printf -- '--homestead-version %s' "${REF#v}"; fi
}

# ------------------------------------------------------------------ flows
flow_new() {
  role=$(choose HS_ROLE "Homestead" "What should this machine be?" \
    new "Start a new cluster here (the first server)" \
    server "Join a cluster as another server (control plane)" \
    agent "Join a cluster as a worker")
  [ "$role" = harvester ] && fail "this is not a Harvester host"
  pick_ip
  prechecks "$role"
  case "$role" in
    new) new_cluster ;;
    server|agent) join_cluster "$role" ;;
    addons) add_to_k3s ;;
    *) fail "unknown role $role" ;;
  esac
}

pick_ip() {
  NODE_IP=$(given HS_NODE_IP)
  [ -n "$NODE_IP" ] && return
  best=$(default_ip)
  count=$(all_ips | wc -l | tr -d ' ')
  if [ "${count:-0}" -le 1 ]; then NODE_IP="$best"; return; fi
  # More than one address: which one the other machines reach this one on.
  set --
  while read -r dev addr; do set -- "$@" "$addr" "$dev$([ "$addr" = "$best" ] && printf ' (default route)')"; done <<EOF
$(all_ips)
EOF
  NODE_IP=$(choose HS_NODE_IP "This machine's address" "Which address do the other machines reach this one on? The cluster registers it by this address." "$@")
}

new_cluster() {
  longhorn=yes; kubevirt=no
  kvm && kubevirt=yes
  yesno HS_LONGHORN "Storage" "Install Longhorn? It keeps copies of each volume on several machines, takes snapshots and backups, and is what Homestead's Volumes and Data protection pages use. Without it, k3s's local-path storage keeps each volume on one machine only." yes || longhorn=no
  yesno HS_KUBEVIRT "Virtual machines" "Install KubeVirt and CDI, to run VMs beside your containers?$(kvm || printf ' This machine has no hardware virtualisation, so VMs would be emulated and slow.')" "$kubevirt" && kubevirt=yes || kubevirt=no
  args="server --node-ip $NODE_IP"
  [ "$longhorn" = no ] && args="$args --no-longhorn"
  [ "$kubevirt" = yes ] && args="$args --kubevirt"
  v=$(versions); [ -n "$v" ] && args="$args $v"
  yesno HS_YES "Ready" "About to:
  - install k3s on this machine, as the first server of a new cluster, at $NODE_IP
  - $( [ "$longhorn" = yes ] && echo "install Longhorn (open-iscsi and an NFS client go on the host first)" || echo "use k3s's local-path storage")
  - $( [ "$kubevirt" = yes ] && echo "install KubeVirt and CDI" || echo "leave VMs out (KubeVirt can be added later)")
  - install Homestead, at http://$NODE_IP:8088

Go ahead?" yes || fail "stopped - nothing was changed"
  stages=5; [ "$longhorn" = yes ] && stages=$((stages + 2)); [ "$kubevirt" = yes ] && stages=$((stages + 1))
  # shellcheck disable=SC2086
  bootstrap "$stages" $args
  finish_new
}

join_cluster() { # server|agent
  server=$(ask HS_SERVER "The cluster" "The address of a server already in the cluster (its IP or name):" "")
  [ -n "$server" ] || fail "no server given"
  case "$server" in https://*) url="$server" ;; *) url="https://$server:6443" ;; esac
  if [ "$DRY" = 0 ]; then
    curl -sk --max-time 10 "$url/cacerts" 2>/dev/null | grep -q "BEGIN CERTIFICATE" \
      || fail "cannot reach the cluster at $url - check the address, and that port 6443 is open"
  fi
  token=$(ask HS_TOKEN "The cluster's token" "On that server: sudo cat /var/lib/rancher/k3s/server/node-token
(Homestead's Cluster > Add a host shows where it is too.) Paste it:" "" secret)
  [ -n "$token" ] || fail "no token given"
  mode=agent; [ "$1" = server ] && mode=join
  yesno HS_YES "Ready" "About to install k3s here and join the cluster at $url as a $([ "$1" = server ] && echo "server (control plane and etcd)" || echo worker), registered as $(hostname) at $NODE_IP. Longhorn's host tools (open-iscsi, an NFS client) go on first.

Go ahead?" yes || fail "stopped - nothing was changed"
  bootstrap 3 "$mode" "$url" "$token" --node-ip "$NODE_IP"
  say "$(hostname) has joined. It shows on Homestead's Nodes page within a minute or two."
}

add_to_k3s() {
  if k3s kubectl -n lab get deployment homestead >/dev/null 2>&1; then
    ip=$(default_ip)
    msg "Already installed" "Homestead is running on this cluster already: http://$ip:8088. It updates itself from Settings > Updates."
    exit 0
  fi
  v=$(versions)
  yesno HS_YES "Ready" "k3s is running here. About to add Longhorn (unless it is there) and Homestead to it, at http://$(default_ip):8088.

Go ahead?" yes || fail "stopped - nothing was changed"
  # shellcheck disable=SC2086
  bootstrap 5 addons $v
  finish_new
}

finish_new() {
  ip="${NODE_IP:-$(default_ip)}"
  token_file=/var/lib/rancher/k3s/server/node-token
  msg "Homestead is running" "Open http://$ip:8088 and create the first account.

To add machines, run the same line on each:
  curl -sfL $RAW/main/scripts/install.sh | sudo sh
choose to join, and give this address ($ip) and the token from:
  sudo cat $token_file

In Homestead: Settings > Cluster > Add-ons adds the node probe (drive
health), Multus (LAN addresses for containers and VMs) and kube-vip (an
address of its own for each app)."
}

flow_harvester() {
  KC=kubectl
  if [ -x /var/lib/rancher/rke2/bin/kubectl ] && [ -f /etc/rancher/rke2/rke2.yaml ]; then
    KC="/var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml"
  fi
  $KC get nodes >/dev/null 2>&1 || [ "$DRY" = 1 ] \
    || fail "this Harvester host cannot reach its cluster with kubectl - run this on a management host, as root"
  if $KC -n lab get deployment homestead >/dev/null 2>&1; then
    msg "Already installed" "Homestead is running on this Harvester cluster already. It updates itself from Settings > Updates."
    exit 0
  fi
  REPORT=""; FAILED=0
  reachable https://ghcr.io/v2/ && check ok "ghcr.io is reachable" || check fail "cannot reach ghcr.io, where Homestead's image is"
  if [ "$FAILED" = 1 ] && [ "$SKIP_CHECKS" = 0 ]; then msg "Checks - cannot go on" "$REPORT"; fail "the checks above stopped the install"; fi
  vip=$(ask HS_VIP "Homestead's address" "Harvester found. Homestead needs an address of its own on your LAN - free, and outside your router's DHCP range (for example 192.168.1.242):" "")
  printf '%s' "$vip" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || fail "$vip is not an address like 192.168.1.242"
  if [ "$DRY" = 0 ] && ping -c 1 -W 1 "$vip" >/dev/null 2>&1; then
    yesno HS_TAKEN "Address in use?" "Something already answers at $vip. Use it anyway?" no || fail "choose a free address"
  fi
  set --
  for c in $($KC get storageclass -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do set -- "$@" "$c" "storage class $c"; done
  [ $# -gt 0 ] || set -- harvester-longhorn "Harvester's default"
  class=$(choose HS_CLASS "Homestead's data" "Which storage class holds Homestead's own data? harvester-longhorn, Harvester's default, is the usual choice." "$@")
  yesno HS_YES "Ready" "About to install Homestead into this Harvester cluster:
  - at http://$vip:8088
  - its data on $class
  - in the namespace lab

Go ahead?" yes || fail "stopped - nothing was changed"
  say "Installing Homestead"
  manifest=/tmp/homestead-deploy.yaml
  run curl -sfL "$RAW/$REF/deploy/deploy.yaml" -o "$manifest" || fail "could not download Homestead's manifest"
  run sed -i -e "s/192\\.168\\.1\\.242/$vip/g" -e "s/longhorn-r2/$class/g" \
    -e "s/accessModes: \\[ReadWriteMany\\]/accessModes: [ReadWriteOnce]/" "$manifest"
  # shellcheck disable=SC2086
  run $KC apply -f "$manifest" || fail "kubectl could not apply the manifest"
  say "Waiting for Homestead to start"
  # shellcheck disable=SC2086
  progress "Starting Homestead" 1 sh -c "echo '==> Waiting for Homestead to start'; $KC -n lab rollout status deployment/homestead --timeout=10m" \
    || fail "Homestead did not start: $KC -n lab get pods says why"
  msg "Homestead is running" "Open http://$vip:8088 and create the first account.

Next: Networking > Your VIPs, to keep addresses for your apps; Settings >
Cluster > Add-ons for the node probe. More in $WIKI/Installing-on-Harvester"
}

# ------------------------------------------------------------------ the doctor's questions
DLOG=/var/log/homestead-doctor.log
DMODE=menu
FOUND=$(mktemp 2>/dev/null || echo /tmp/homestead-doctor.$$)
trap 'rm -f "$FOUND"' EXIT
logline() { { printf '%s %s\n' "$(date '+%F %T' 2>/dev/null)" "$*" >> "$DLOG"; } 2>/dev/null || true; }
confirm() { # title text -> 0 for yes
  [ "$DMODE" = fix ] && return 0
  if [ "$UI" = text ]; then
    interactive || return 1
    printf '\n%s\n%s [y/N] ' "$1" "$2" > "$TTY"; read -r reply < "$TTY" || reply=""
    case "$reply" in y*|Y*) return 0 ;; *) return 1 ;; esac
  fi
  "$BOX" --title "$1" --defaultno --yesno "$2" 20 78 < "$TTY" > "$TTY" 2>&1
}
typed() { # title text word -> 0 when the word is typed
  if [ "$UI" = text ]; then
    printf '\n%s\n%s\nType %s to go ahead: ' "$1" "$2" "$3" > "$TTY"; read -r reply < "$TTY" || reply=""
  else
    reply=$("$BOX" --title "$1" --inputbox "$2

Type $3 to go ahead:" 20 78 "" 3>&1 1>"$TTY" 2>&3 < "$TTY") || return 1
  fi
  [ "$reply" = "$3" ]
}
menu() { # title text tag item... -> tag, or 1 when cancelled
  title="$1"; text="$2"; shift 2
  if [ "$UI" = text ]; then
    printf '\n%s\n%s\n' "$title" "$text" > "$TTY"
    i=0; for word in "$@"; do i=$((i+1)); [ $((i % 2)) = 0 ] && printf '  %2d) %s\n' $((i / 2)) "$word" > "$TTY"; done
    printf 'Choose 1-%d (Enter to go back): ' $(( $# / 2 )) > "$TTY"; read -r reply < "$TTY" || reply=""
    [ -z "$reply" ] && return 1
    i=0; for word in "$@"; do i=$((i+1)); if [ $((i % 2)) = 1 ] && [ $(( (i + 1) / 2 )) = "$reply" ]; then printf '%s' "$word"; return 0; fi; done
    return 1
  fi
  box_menu "$title" "$text" "$@"
}

# ------------------------------------------------------------------ the node
KIND=none; SERVICE=""; DATA=""; KC=""
detect_node() {
  if grep -qi harvester /etc/os-release 2>/dev/null || [ -f /etc/rancher/rancherd/config.yaml ]; then KIND=harvester; fi
  for s in k3s k3s-agent rke2-server rke2-agent kubelet; do
    if systemctl list-unit-files "$s.service" 2>/dev/null | grep -q "^$s.service"; then SERVICE=$s; break; fi
  done
  case "$SERVICE" in
    k3s) [ "$KIND" = none ] && KIND=k3s-server; DATA=/var/lib/rancher/k3s; KC="k3s kubectl" ;;
    k3s-agent) KIND=k3s-agent; DATA=/var/lib/rancher/k3s ;;
    rke2-server) [ "$KIND" = none ] && KIND=rke2-server; DATA=/var/lib/rancher/rke2
      KC="/var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml" ;;
    rke2-agent) [ "$KIND" = none ] && KIND=rke2-agent; DATA=/var/lib/rancher/rke2 ;;
    kubelet) [ "$KIND" = none ] && KIND=kubernetes; have kubectl && KC=kubectl ;;
  esac
  [ -n "$KC" ] || { have kubectl && [ -n "${KUBECONFIG:-}" ] && KC=kubectl; }
  CRICTL=""
  for c in "$DATA/bin/crictl" /var/lib/rancher/rke2/bin/crictl; do [ -x "$c" ] && CRICTL="$c" && break; done
  if [ -z "$CRICTL" ] && have k3s && { [ "$SERVICE" = k3s ] || [ "$SERVICE" = k3s-agent ]; }; then CRICTL="k3s crictl"; fi
  [ -z "$CRICTL" ] && have crictl && CRICTL=crictl
  NODE=$(hostname)
}
kc() { [ -n "$KC" ] && $KC "$@" 2>/dev/null; }

# ------------------------------------------------------------------ findings
# One line each: id|level|title|detail|fix|safe
found() { printf '%s|%s|%s|%s|%s|%s\n' "$1" "$2" "$3" "$4" "${5:-}" "${6:-no}" >> "$FOUND"; }

check_service() {
  [ -n "$SERVICE" ] || { found service bad "No Kubernetes service here" "Neither k3s, RKE2 nor a kubelet is installed on this machine. The installer sets one up: install.sh"; return; }
  if active "$SERVICE"; then found service ok "$SERVICE is running" "systemd reports $SERVICE active."
  else
    why=$(journalctl -u "$SERVICE" -p err -n 5 --no-pager 2>/dev/null | tail -n 5 | cut -c1-200)
    found service bad "$SERVICE is not running" "systemd reports $SERVICE $(systemctl is-active "$SERVICE" 2>/dev/null). Its last errors:
$why" fix_restart yes
  fi
}
check_disk() {
  for m in / /var; do
    use=$(df -P "$m" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
    case "$use" in ''|*[!0-9]*) continue ;; esac
    [ "$use" -le 100 ] || continue
    if [ "$use" -ge 90 ]; then found "disk$m" bad "$m is ${use}% full" "Kubernetes starts evicting pods and refuses new ones as a node fills. Cleaning up removes images nothing uses and trims the journal." fix_cleanup yes
    elif [ "$use" -ge 80 ]; then found "disk$m" warn "$m is ${use}% full" "Getting full. Cleaning up removes images nothing uses and trims the journal." fix_cleanup yes
    else found "disk$m" ok "$m is ${use}% full" "Room to spare."; fi
    ino=$(df -Pi "$m" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
    case "$ino" in ''|*[!0-9]*) ino=0 ;; esac
    if [ "$ino" -le 100 ] && [ "$ino" -ge 90 ]; then
      found "inodes$m" bad "$m has used ${ino}% of its inodes" "Files cannot be created even with space left - usually many small files in container layers or logs." fix_cleanup yes
    fi
  done
}
check_memory() {
  total=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null); avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null)
  [ -n "$total" ] && [ -n "$avail" ] || return
  pct=$(( avail * 100 / total ))
  if [ "$pct" -lt 5 ]; then found memory bad "Only ${pct}% of memory is free" "The kernel will start killing processes. Homestead's Nodes page shows which apps use the most; move or stop some."
  elif [ "$pct" -lt 15 ]; then found memory warn "${pct}% of memory is free" "Tight. Homestead's Nodes page shows which apps use the most."
  else found memory ok "${pct}% of memory is free" "$(( avail / 1024 )) of $(( total / 1024 )) MB available."; fi
}
check_clock() {
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; then found clock ok "The clock is synchronised" "NTP keeps it right."
  else found clock warn "The clock is not synchronised" "A clock that drifts breaks certificates, etcd and joining. Turning on time sync fixes it." fix_clock yes; fi
}
check_iscsi() {
  { have iscsiadm || kc get crd volumes.longhorn.io >/dev/null; } || return
  if active iscsid || active iscsid.socket; then found iscsid ok "iscsid is running" "Longhorn attaches volumes through it."
  else found iscsid bad "iscsid is not running" "Longhorn attaches every volume over iSCSI; without it volumes cannot attach on this node." fix_iscsid yes; fi
  if active multipathd && [ "$KIND" != harvester ] && ! grep -qs 'devnode "\^sd\[a-z0-9\]+"' /etc/multipath.conf; then
    found multipath warn "multipathd may grab Longhorn's disks" "multipathd claims the block devices Longhorn makes, and volumes then fail to mount (\"already mounted or mount point busy\"). Longhorn's advice is a blacklist for them in /etc/multipath.conf." fix_multipath no
  fi
}
check_certs() {
  [ -d "$DATA/server/tls" ] || return
  soonest=""; soonest_file=""
  for f in "$DATA"/server/tls/*.crt; do
    [ -f "$f" ] || continue
    end=$(openssl x509 -enddate -noout -in "$f" 2>/dev/null | cut -d= -f2)
    [ -n "$end" ] || continue
    secs=$(date -d "$end" +%s 2>/dev/null) || continue
    if [ -z "$soonest" ] || [ "$secs" -lt "$soonest" ]; then soonest=$secs; soonest_file=$f; fi
  done
  [ -n "$soonest" ] || return
  days=$(( (soonest - $(date +%s)) / 86400 ))
  if [ "$days" -lt 7 ]; then found certs bad "A certificate expires in $days days" "$(basename "$soonest_file") ends in $days days; then the API stops answering. Restarting $SERVICE renews the ones due within 90 days." fix_restart yes
  elif [ "$days" -lt 90 ]; then found certs warn "Certificates expire in $days days" "$SERVICE renews them on a restart within 90 days of expiry; a restart now does it." fix_restart yes
  else found certs ok "Certificates are good for $days days" "The soonest to end is $(basename "$soonest_file")."; fi
}
check_runtime() {
  [ -n "$CRICTL" ] || return
  if $CRICTL info >/dev/null 2>&1; then found runtime ok "containerd answers" "The container runtime is up."
  else found runtime bad "containerd does not answer" "No container on this node can start. Restarting $SERVICE restarts it." fix_restart yes; fi
}

check_cluster() {
  [ -n "$KC" ] || { found api warn "The cluster is not checked from here" "kubectl cannot reach the cluster from this machine (a worker has no admin access). Run the doctor on a server to check the cluster too."; return; }
  if kc get --raw /readyz >/dev/null; then found api ok "The API server answers" "kubectl reaches it."
  else found api bad "The API server does not answer" "Nothing in the cluster can be changed until it does. On a server, restarting $SERVICE usually brings it back." fix_restart yes; return; fi
  case "$KIND" in k3s-server|rke2-server|harvester)
    if kc get --raw /readyz/etcd >/dev/null; then found etcd ok "etcd is healthy" "The cluster's database answers."
    else found etcd bad "etcd is not healthy" "The cluster's database is not answering - often too few servers are up to form a quorum. Check the other servers first." ; fi ;;
  esac
  state=$(kc get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status} {.spec.unschedulable}')
  case "$state" in
    "True true"*) found node warn "$NODE is cordoned" "It runs what it has but takes nothing new - left over from maintenance?" fix_uncordon yes ;;
    True*) found node ok "$NODE is Ready" "Kubernetes sees this node as healthy." ;;
    "") found node warn "$NODE is not in the cluster" "The cluster has no node by this machine's name." ;;
    *) found node bad "$NODE is not Ready" "Kubernetes does not see this node as healthy; its kubelet or network is the usual cause." fix_restart yes ;;
  esac
  down=$(kc get nodes --no-headers | awk '$2 !~ /^Ready/ {print $1}' | tr '\n' ' ')
  [ -n "$down" ] && found nodes warn "Nodes not ready: $down" "Look at those machines - run the doctor there. One gone for good is removed in Homestead: Cluster > Remove a host."
  broken=$(kc get pods -A --no-headers | awk '$4 ~ /CrashLoopBackOff|Error|ImagePullBackOff|ErrImagePull|CreateContainerConfigError/ {print $1"/"$2" ("$4")"}' | head -n 12)
  if [ -n "$broken" ]; then found pods warn "Pods failing: $(printf '%s\n' "$broken" | wc -l | tr -d ' ')" "These keep failing:
$broken

Each one's log says why (Homestead: Containers > Logs). Restarting them gives each a fresh start." fix_restart_broken no; fi
  failed=$(kc get pods -A --field-selector=status.phase=Failed --no-headers | wc -l | tr -d ' ')
  [ "${failed:-0}" -gt 0 ] && found failed warn "$failed failed pods left behind" "Pods that ended in failure - evicted, or out of their deadline - are kept until deleted. Nothing uses them." fix_failed_pods yes
  stuck=$(kc get pods -A --no-headers | awk '$4=="Terminating" {print $1"/"$2}' | head -n 20)
  if [ -n "$stuck" ]; then found terminating warn "Pods stuck terminating" "These have been told to stop and have not:
$stuck

Usually their node is gone. Force-deleting them lets their apps start elsewhere." fix_terminating no; fi
  if kc get crd volumes.longhorn.io >/dev/null; then
    bad=$(kc -n longhorn-system get volumes.longhorn.io --no-headers -o custom-columns=N:.metadata.name,R:.status.robustness | awk '$2=="faulted" || $2=="degraded" {print $1" ("$2")"}')
    if [ -n "$bad" ]; then found longhorn warn "Longhorn volumes not healthy" "$bad

Degraded ones rebuild on their own when a node with room is up; faulted ones need a backup restored (Homestead: Data protection)."
    else found longhorn ok "Longhorn volumes are healthy" "Every volume has its copies."; fi
  fi
  if [ "$(kc -n kube-system get deploy coredns -o jsonpath='{.status.readyReplicas}')" = "" ] && kc -n kube-system get deploy coredns >/dev/null; then
    found dns bad "CoreDNS is not ready" "Pods cannot find each other or the internet by name." fix_dns yes
  fi
  if kc -n lab get deploy homestead >/dev/null; then
    ready=$(kc -n lab get deploy homestead -o jsonpath='{.status.readyReplicas}')
    if [ -n "$ready" ] && [ "$ready" -gt 0 ]; then found homestead ok "Homestead is running" "Its web page should answer."
    else found homestead bad "Homestead is not running" "Its Deployment has no ready copy. A restart often clears it; if not, its pod's events say why." fix_homestead yes; fi
  fi
  if [ "$KIND" = k3s-server ] && [ -d "$DATA/server/db/etcd" ]; then
    newest=$(ls -t "$DATA"/server/db/snapshots 2>/dev/null | head -n 1)
    if [ -z "$newest" ]; then found snapshot warn "No etcd snapshot on this server" "k3s snapshots the cluster twice a day by default; none here means none can be restored." fix_snapshot yes
    else
      age=$(( ($(date +%s) - $(stat -c %Y "$DATA/server/db/snapshots/$newest" 2>/dev/null || date +%s)) / 3600 ))
      if [ "$age" -gt 48 ]; then found snapshot warn "The newest etcd snapshot is $age hours old" "k3s normally takes one every 12 hours. Take one now." fix_snapshot yes
      else found snapshot ok "etcd snapshot $age hours old" "$newest"; fi
    fi
  fi
}

run_checks() {
  : > "$FOUND"
  steps="check_service check_disk check_memory check_clock check_runtime check_certs check_iscsi check_cluster"
  if [ "$UI" = text ]; then for s in $steps; do $s; done; return; fi
  { n=0; for s in $steps; do n=$((n+1)); printf 'XXX\n%d\nChecking: %s\nXXX\n' $(( n * 100 / 9 )) "$(printf '%s' "${s#check_}" | tr _ ' ')"; $s; done; } \
    | "$BOX" --title "Homestead doctor" --gauge "Checking" 8 70 0 > "$TTY" 2>&1
}

# ------------------------------------------------------------------ fixes
fix_restart() { run systemctl restart "$SERVICE" && sleep 5; }
fix_iscsid() { run systemctl enable --now iscsid; }
fix_clock() {
  if systemctl list-unit-files chronyd.service 2>/dev/null | grep -q chronyd; then run systemctl enable --now chronyd
  else run timedatectl set-ntp true; fi
}
fix_uncordon() { run $KC uncordon "$NODE"; }
fix_failed_pods() { run $KC delete pods -A --field-selector=status.phase=Failed; }
fix_restart_broken() {
  kc get pods -A --no-headers | awk '$4 ~ /CrashLoopBackOff|Error|ImagePullBackOff|ErrImagePull|CreateContainerConfigError/ {print $1, $2}' |
    while read -r ns pod; do run $KC -n "$ns" delete pod "$pod" --wait=false; done
}
fix_terminating() {
  kc get pods -A --no-headers | awk '$4=="Terminating" {print $1, $2}' |
    while read -r ns pod; do run $KC -n "$ns" delete pod "$pod" --grace-period=0 --force; done
}
fix_dns() { run $KC -n kube-system rollout restart deployment/coredns; }
fix_homestead() { run $KC -n lab rollout restart deployment/homestead; }
fix_snapshot() {
  case "$KIND" in
    k3s-server) run k3s etcd-snapshot save --name "doctor-$(date +%Y%m%d-%H%M)" ;;
    rke2-server) run rke2 etcd-snapshot save --name "doctor-$(date +%Y%m%d-%H%M)" ;;
    *) msg "Snapshots" "Etcd snapshots are taken on a k3s or RKE2 server." ;;
  esac
}
fix_multipath() {
  run sh -c 'grep -qs "^blacklist" /etc/multipath.conf || printf "blacklist {\n}\n" >> /etc/multipath.conf'
  run sh -c 'grep -qs "devnode \"\\^sd\[a-z0-9\]+\"" /etc/multipath.conf || sed -i "/^blacklist {/a\    devnode \"^sd[a-z0-9]+\"" /etc/multipath.conf'
  run systemctl restart multipathd
}
fix_cleanup() {
  before=$(df -Pm /var 2>/dev/null | awk 'NR==2 {print $4}')
  [ -n "$CRICTL" ] && run $CRICTL rmi --prune
  have journalctl && run journalctl --vacuum-size=200M
  if [ -d "$DATA/server/db/snapshots" ]; then
    # Keep the ten newest etcd snapshots.
    ls -t "$DATA"/server/db/snapshots 2>/dev/null | tail -n +11 | while read -r old; do run rm -f "$DATA/server/db/snapshots/$old"; done
  fi
  after=$(df -Pm /var 2>/dev/null | awk 'NR==2 {print $4}')
  [ -n "$before" ] && [ -n "$after" ] && [ "$DRY" = 0 ] && msg "Cleaned up" "$(( after - before )) MB freed under /var."
}

apply_fix() { # id
  line=$(grep "^$1|" "$FOUND" | head -n 1)
  title=$(printf '%s' "$line" | cut -d'|' -f3); detail=$(printf '%s' "$line" | cut -d'|' -f4)
  fix=$(printf '%s' "$line" | cut -d'|' -f5)
  [ -n "$fix" ] || { msg "$title" "$detail

There is nothing the doctor can do for this one by itself."; return; }
  if confirm "$title" "$detail

Fix it now?"; then
    logline "fix: $1 ($fix)"
    if $fix; then msg "Done" "$title - fixed. Check again to see it cleared." ; else msg "Did not work" "The fix did not finish; $DLOG has what ran."; fi
  fi
}

# ------------------------------------------------------------------ restore
restore() {
  [ "$KIND" = k3s-server ] || { msg "Restore" "Restoring from an etcd snapshot is done here on a k3s server. On RKE2: rke2 server --cluster-reset --cluster-reset-restore-path=<snapshot>; on Harvester, follow Harvester's own guide."; return; }
  dir="$DATA/server/db/snapshots"
  set --
  for f in $(ls -t "$dir" 2>/dev/null | head -n 15); do set -- "$@" "$f" "$(date -d "@$(stat -c %Y "$dir/$f")" '+%F %H:%M' 2>/dev/null)"; done
  [ $# -gt 0 ] || { msg "Restore" "There are no etcd snapshots on this server."; return; }
  snap=$(menu "Restore from a snapshot" "Which snapshot? The whole cluster - every app, setting and secret - goes back to that moment. Volumes' data is not in it; Longhorn keeps that." "$@") || return
  servers=$(kc get nodes --no-headers | grep -c 'control-plane' || echo 1)
  typed "Restore the cluster" "This stops k3s, resets the cluster to $snap, and starts it again. Everything changed since then is lost.$([ "${servers:-1}" -gt 1 ] && printf '\n\nThis cluster has %s servers: stop k3s on the others first, and afterwards delete %s/server/db on each of them and start it, so they join again from this one.' "$servers" "$DATA")" RESTORE || { msg "Restore" "Not restored."; return; }
  logline "restore: $snap"
  run systemctl stop k3s
  run k3s server --cluster-reset --cluster-reset-restore-path="$dir/$snap" || { msg "Restore" "The reset did not finish; journalctl -u k3s and $LOG say why. k3s is stopped."; return; }
  run systemctl start k3s
  msg "Restored" "The cluster is back at $snap. Check again in a minute, once k3s is up."
}

cleanup_menu() {
  choice=$(menu "Clean up" "What to clear away:" \
    all "Everything below" \
    images "Container images no pod uses (crictl rmi --prune)" \
    journal "The system journal, down to 200 MB" \
    failed "Failed and evicted pods" \
    snapshots "etcd snapshots beyond the ten newest") || return
  confirm "Clean up" "Clear away: $choice?" || return
  case "$choice" in
    all) fix_cleanup; fix_failed_pods ;;
    images) [ -n "$CRICTL" ] && run $CRICTL rmi --prune ;;
    journal) run journalctl --vacuum-size=200M ;;
    failed) fix_failed_pods ;;
    snapshots) ls -t "$DATA"/server/db/snapshots 2>/dev/null | tail -n +11 | while read -r old; do run rm -f "$DATA/server/db/snapshots/$old"; done ;;
  esac
  msg "Clean up" "Done."
}

doctor_report() { # the findings, worst first, as text
  for level in bad warn ok; do
    grep "|$level|" "$FOUND" | while IFS='|' read -r id lvl title detail fix safe; do
      case "$lvl" in bad) mark="[!!]" ;; warn) mark="[ !]" ;; *) mark="[ok]" ;; esac
      printf '%s %s%s\n' "$mark" "$title" "$([ -n "$fix" ] && [ "$lvl" != ok ] && printf ' (fixable)')"
    done
  done
}


# ------------------------------------------------------------------ the doctor
doctor() { # menu | report | fix
  DMODE="$1"
  detect_node
  run_checks
  if [ "$DMODE" = report ]; then
    printf 'Homestead doctor - %s (%s)\n\n' "$NODE" "$KIND"; doctor_report
    grep -q '|bad|' "$FOUND" && exit 2; grep -q '|warn|' "$FOUND" && exit 1; exit 0
  fi
  if [ "$DMODE" = fix ]; then
    printf 'Homestead doctor - %s (%s): fixing what is safe\n' "$NODE" "$KIND"
    grep -E '\|(bad|warn)\|' "$FOUND" | while IFS='|' read -r id lvl title detail fix safe; do
      [ -n "$fix" ] && [ "$safe" = yes ] && { printf '  %s\n' "$title"; $fix; }
    done
    exit 0
  fi
  while :; do
    set --
    for level in bad warn ok; do
      while IFS='|' read -r id lvl title detail fix safe; do
        [ -n "$id" ] || continue
        case "$lvl" in bad) mark="[!!]" ;; warn) mark="[ !]" ;; *) mark="[ok]" ;; esac
        set -- "$@" "$id" "$mark $title$([ -n "$fix" ] && [ "$lvl" != ok ] && printf ' - fix')"
      done <<EOF
$(grep "|$level|" "$FOUND")
EOF
    done
    bad=$(grep -c '|bad|' "$FOUND"); warn=$(grep -c '|warn|' "$FOUND")
    set -- "$@" "---" "------------" \
      "@fix" "Fix everything marked safe" "@again" "Check again" "@save" "Save this report"
    pick=$(menu "Health - $NODE" "$KIND · $bad wrong, $warn worth a look. Choose one to see it and fix it:" "$@") || return 0
    case "$pick" in
      ---) ;;
      @fix)
        if confirm "Fix everything safe" "Apply every fix marked safe - restarting a stopped service, turning on time sync and iscsid, uncordoning this node, clearing failed pods, cleaning up a full disk, restarting CoreDNS or Homestead?"; then
          grep -E '\|(bad|warn)\|' "$FOUND" | while IFS='|' read -r id lvl title detail fix safe; do [ -n "$fix" ] && [ "$safe" = yes ] && $fix; done
          run_checks
        fi ;;
      @again) run_checks ;;
      @save) out="/var/log/homestead-doctor-$(date +%Y%m%d-%H%M).txt"
        { printf 'Homestead doctor - %s (%s) - %s\n\n' "$NODE" "$KIND" "$(date)"; doctor_report; } > "$out" && msg "Saved" "The report is in $out" ;;
      *) apply_fix "$pick" ;;
    esac
  done
}
# ------------------------------------------------------------------ installing
do_install() {
  if [ "$(given HS_ROLE)" = harvester ] || { [ -z "$(given HS_ROLE)" ] && is_harvester; }; then
    flow_harvester
  elif { [ -z "$(given HS_ROLE)" ] || [ "$(given HS_ROLE)" = addons ]; } && active k3s; then
    HS_ROLE=addons
    prechecks addons
    add_to_k3s
  elif active k3s-agent; then
    msg "A worker already" "This machine is a worker in a k3s cluster already. Run the installer on one of the cluster's servers to add Homestead."
  elif active rke2-server || active rke2-agent; then
    msg "RKE2 is running here" "This machine runs RKE2. Homestead installs onto it as onto any cluster: $WIKI/Installing-on-an-existing-cluster"
  else
    flow_new
  fi
}

# ------------------------------------------------------------------ start
what_is_here() {
  found="a machine with no Kubernetes yet"
  is_harvester && found="a Harvester host"
  active k3s && found="a k3s server"
  active k3s-agent && found="a k3s worker"
  { active rke2-server || active rke2-agent; } && ! is_harvester && found="an RKE2 machine"
  active kubelet && [ "$found" = "a machine with no Kubernetes yet" ] && found="a Kubernetes node"
  printf '%s' "$found"
}

[ "$(id -u)" = 0 ] || [ "$DRY" = 1 ] || fail "run as root: curl ... | sudo sh"
case "$ACTION" in
  install) say "Homestead installer ($REF)$([ "$DRY" = 1 ] && printf ' - dry run, nothing changes')"; do_install; exit 0 ;;
  doctor) doctor menu; exit 0 ;;
  report) UI=text; doctor report ;;
  fix-safe) UI=text; doctor fix ;;
esac
# No choice made ahead: an answer in the environment means an unattended install.
if [ -n "$(given HS_ROLE)" ] || ! interactive; then
  say "Homestead installer ($REF)$([ "$DRY" = 1 ] && printf ' - dry run, nothing changes')"; do_install; exit 0
fi

here=$(what_is_here)
while :; do
  detect_node
  if [ "$here" = "a machine with no Kubernetes yet" ]; then
    set -- install "Install Homestead - a new cluster, or join this machine to one" \
      checks "Check this machine is ready, without changing anything"
  else
    set -- doctor "Check this node and its cluster, and fix what is wrong"
    kc -n lab get deployment homestead >/dev/null || set -- "$@" install "Add Homestead to this cluster"
    set -- "$@" clean "Clean up - old images, the journal, failed pods, old snapshots"
    case "$KIND" in k3s-server|rke2-server) set -- "$@" snapshot "Take an etcd snapshot now" restore "Restore the cluster from a snapshot..." ;; esac
    set -- "$@" report "Save a health report"
  fi
  pick=$(menu "Homestead" "This machine: $(hostname), $(os_name), $(uname -m)
Found: $here

What would you like to do?" "$@") || { say "Bye."; exit 0; }
  case "$pick" in
    install) do_install ;;
    checks) ( prechecks new ) ;;
    doctor) doctor menu ;;
    clean) cleanup_menu ;;
    snapshot) confirm "Snapshot" "Take an etcd snapshot of the cluster now?" && fix_snapshot && msg "Snapshot" "Taken." ;;
    restore) restore ;;
    report) DMODE=menu; run_checks; out="/var/log/homestead-doctor-$(date +%Y%m%d-%H%M).txt"
      { printf 'Homestead doctor - %s (%s) - %s\n\n' "$NODE" "$KIND" "$(date)"; doctor_report; } > "$out" && msg "Saved" "The report is in $out" ;;
  esac
done
