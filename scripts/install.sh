#!/bin/sh
# Homestead installer and node doctor.
#
#   curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/install.sh | sudo sh
#
# The installer detects what is already on the machine and offers the options
# that apply:
#   - on a machine without Kubernetes: create a new cluster (k3s, or RKE2 for
#     hardened upstream Kubernetes) with Longhorn, optionally KubeVirt, and
#     Homestead; or join the machine to an existing cluster as a server or
#     worker node;
#   - on a k3s or RKE2 server node: install Longhorn and Homestead on the
#     running cluster;
#   - on a Harvester node: install Homestead on the Harvester cluster.
# System checks run before any change is made, and an installation summary
# shows the settings and component versions for review. Versions are chosen
# from the current releases of each component.
#
# On a node that is already part of a cluster, the node doctor checks the node
# and the cluster, lists the results by severity, and offers a fix for each
# issue, asking before every change. It can also clean up disk space and take
# or restore etcd snapshots.
#
# Menus use whiptail or dialog. If neither is installed, whiptail is installed
# from the distribution's repositories with a bounded, visible attempt. On
# failure, or with --text, plain prompts are used instead.
#
# Unattended installation: set the answers in the environment.
#   HS_ROLE=new|server|agent|addons|harvester   installation mode
#   HS_DIST=k3s|rke2                            distribution for a new cluster
#   HS_NODE_IP=192.0.2.50                     this node's IP address
#   HS_SERVER=192.0.2.50  HS_TOKEN=...        the cluster to join
#   HS_LONGHORN=yes|no  HS_KUBEVIRT=yes|no      components for a new cluster
#   HS_KUBEVIP=yes|no  HS_MULTUS=yes|no         kube-vip and Multus, installed by Homestead (default yes)
#   HS_KUBEVIP_VERSION=0.11.1  HS_MULTUS_VERSION=v4.3.102   their chart versions (default: tested)
#   HS_NODEPROBE=yes|no                         the node probe, installed by Homestead (default yes)
#   HS_LONGHORN_VOLUME=200|0                    GB for Longhorn's own LVM volume where there is room (0: none)
#   HS_K8S_VERSION=v1.33.4+k3s1                 k3s or RKE2 version
#   HS_LONGHORN_VERSION=v1.9.1  HS_KUBEVIRT_VERSION=v1.6.0  HS_CDI_VERSION=v1.62.0
#   HS_VERSION=2.8.184                          Homestead version
#   HS_VIP=192.0.2.242                        the VIP for Homestead and apps (k3s/RKE2: optional)
#   HS_CLASS=harvester-longhorn                 Harvester: storage class
#   HS_YES=1                                    install without the summary
# Versions not set are the current recommended releases.
#
# Options: --install / --doctor   open the installer or the node doctor directly
#          --report      run the health checks, print the results and exit
#                        (0 healthy, 1 warnings, 2 failures)
#          --fix-safe    run the health checks and apply every safe fix
#          --dry-run     print the commands instead of running them
#          --text        use plain prompts instead of menus
#          --ref v2.8.176  use the scripts from this release
#          --skip-checks   continue the installation when system checks fail
set -u

RAW="https://raw.githubusercontent.com/wjcloudy/homestead"
REF="${HOMESTEAD_REF:-main}"
DRY=0
DIST=k3s
ROLE=""
ACTION=""
SKIP_CHECKS=0
UI=""
TTY=/dev/tty
WIKI="https://github.com/wjcloudy/homestead/wiki"
FAILED=0
BACK=Back
MENU_DEFAULT=""

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
    -h|--help) sed -n '2,51p' "$0" 2>/dev/null; exit 0 ;;
    *) printf 'Unknown option: %s (see --help)\n' "$1" >&2; exit 2 ;;
  esac
done

# ------------------------------------------------------------------ output
say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nError: %s\n' "$*" >&2; exit 1; }
run() {
  # Every change goes through here, so --dry-run shows it instead.
  { printf '%s run: %s\n' "$(date '+%F %T' 2>/dev/null)" "$*" >> /var/log/homestead-doctor.log; } 2>/dev/null || true
  if [ "$DRY" = 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi
}
cancelled() { fail "Installation cancelled. No changes were made."; }

# ------------------------------------------------------------------ questions
have() { command -v "$1" >/dev/null 2>&1; }
interactive() { [ -r "$TTY" ] && [ -w "$TTY" ] && (: < "$TTY") 2>/dev/null; }

# Menu packages are optional. Keep their input separate from the script when
# invoked as curl | sh: package hooks must not consume the remaining program.
# Bound the whole attempt, including an apt index refresh, and show output so
# a package lock or repository failure is not mistaken for a frozen installer.
get_menus() {
  if ! have timeout; then
    printf 'No timeout command; skipping optional menu installation. Using text prompts.\n' > "$TTY"
    return 1
  fi
  menu_manager=""
  for manager in apt-get dnf yum zypper apk; do
    if have "$manager"; then menu_manager="$manager"; break; fi
  done
  if [ -z "$menu_manager" ]; then
    printf 'No supported package manager; using text prompts.\n' > "$TTY"
    return 1
  fi
  printf 'Installing optional whiptail menus with %s (up to 120 seconds; --text skips this).\n' "$menu_manager" > "$TTY"
  timeout -k 10 120 sh -c '
    # These commands contain no user credentials. Print each command as well
    # as its output so the exact failing step is visible on the terminal.
    set -x
    case "$1" in
      apt-get)
        export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
        apt_menus() {
          apt-get -o DPkg::Lock::Timeout=15 -o Acquire::Retries=0 \
            -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 "$@"
        }
        apt_menus install -y whiptail && exit 0
        apt_menus update && apt_menus install -y whiptail
        ;;
      dnf) dnf install -y newt ;;
      yum) yum install -y newt ;;
      zypper) zypper --non-interactive install -y newt ;;
      apk) apk add newt ;;
    esac
  ' homestead-menus "$menu_manager" < /dev/null > "$TTY" 2>&1
  menu_status=$?
  case "$menu_status" in
    0)
      if have whiptail; then
        printf 'Whiptail installed; opening the installer.\n' > "$TTY"
        return 0
      fi
      printf 'Package command completed but whiptail is unavailable. Using text prompts.\n' > "$TTY"
      ;;
    # BusyBox returns 143 when its deadline sends SIGTERM; GNU returns 124.
    124|137|143)
      printf 'Menu installation timed out. Using text prompts. Check package-manager status before retrying package installation.\n' > "$TTY"
      ;;
    130) exit "$menu_status" ;;
    *) printf 'Menu installation failed (exit %s). Using text prompts; see the package-manager output above.\n' "$menu_status" > "$TTY" ;;
  esac
  return 1
}

# sudo-rs can start a piped command outside the terminal foreground group.
# The first terminal read must come from this shell, not a command-substitution
# child (stty/whiptail), so sudo can hand terminal control to the command.
# Reports and fully specified unattended installs must never wait for input.
prepare_terminal() {
  [ -n "${SUDO_USER:-}" ] && [ ! -t 0 ] && interactive || return 0
  [ -z "${HS_ROLE:-}" ] || return 0
  case "$ACTION" in report|fix-safe) return 0 ;; esac
  printf 'Press Enter to open Homestead setup (Ctrl+C to cancel): ' > "$TTY"
  read -r terminal_ready < "$TTY" || fail "Could not read the terminal. Download install.sh and run sudo sh install.sh."
}

# HS_UI=whiptail|dialog|text selects the interface; otherwise the best available.
prepare_terminal
[ -z "$UI" ] && [ -n "${HS_UI:-}" ] && UI="$HS_UI"
if [ -z "$UI" ]; then
  if ! interactive || [ "${TERM:-dumb}" = dumb ]; then UI=text
  elif have whiptail; then UI=whiptail
  elif have dialog; then UI=dialog
  elif [ "$DRY" = 0 ] && [ "$(id -u)" = 0 ] && get_menus; then UI=whiptail
  else UI=text; fi
fi
BOX="$UI"

# Boxes are as tall as their contents, within the terminal: the text's lines
# as the box wraps them, plus the frame and buttons.
term_rows() {
  # stty can apply terminal settings even for a size query. Under curl | sudo
  # sh, doing this in a menu command substitution can stop the process group
  # before any menu is drawn. tput only queries the terminal; minimal images
  # without tput or a usable terminfo entry get a conventional 24-row layout.
  rows=$(tput lines 2>/dev/null < "$TTY") || rows=""
  case "$rows" in ''|*[!0-9]*|0) rows=24 ;; esac
  printf '%s\n' "$rows"
}
text_lines() { # text width
  printf '%s\n' "$1" | awk -v w="$2" '{ n += int((length($0) + w - 1) / w); if (!length($0)) n++ } END { print n }'
}
fit() { # text frame-rows -> a height for a 78-wide box
  h=$(( $(text_lines "$1" 72) + $2 )); rows=$(term_rows)
  [ "$h" -gt "$rows" ] && h=$rows; echo "$h"
}

box_menu() { # title text tag item...
  title="$1"; text="$2"; shift 2
  items=$(( $# / 2 ))
  rows=$(term_rows); lines=$(text_lines "$text" 84)
  list=$items; [ "$list" -gt $((rows - lines - 9)) ] && list=$((rows - lines - 9)); [ "$list" -lt 3 ] && list=3
  height=$((lines + list + 8)); [ "$height" -gt "$rows" ] && height=$rows
  # The tags are for the script; the items are what people read.
  if [ "$BOX" = dialog ]; then notags=--no-tags; cancel=--cancel-label; else notags=--notags; cancel=--cancel-button; fi
  "$BOX" --title "$title" "$cancel" "$BACK" "$notags" --menu "$text" "$height" 90 "$list" "$@" 3>&1 1>"$TTY" 2>&3 < "$TTY"
}

infobox() { # text
  if [ "$UI" = text ]; then printf '%s\n' "$1" > "$TTY"
  else "$BOX" --infobox "$1" 7 60 > "$TTY" 2>&1; fi
}

# The answer set in the environment, if there is one.
given() { eval "printf '%s' \"\${$1:-}\""; }

need_tty() { interactive || fail "No terminal is available for input. Set $1 to provide this value (see --help)."; }

msg() { # title text
  if [ "$UI" = text ]; then printf '\n-- %s --\n%s\n' "$1" "$2"
  else "$BOX" --title "$1" --msgbox "$2" "$(fit "$2" 7)" 78 < "$TTY" > "$TTY" 2>&1; fi
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
  if [ "$4" = no ]; then "$BOX" --title "$2" --defaultno --yesno "$3" "$(fit "$3" 7)" 78 < "$TTY" > "$TTY" 2>&1
  else "$BOX" --title "$2" --yesno "$3" "$(fit "$3" 7)" 78 < "$TTY" > "$TTY" 2>&1; fi
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
  "$BOX" --title "$2" "$kind" "$3" "$(fit "$3" 9)" 78 "$4" 3>&1 1>"$TTY" 2>&3 < "$TTY" || cancelled
}

choose() { # var title text tag item [tag item...] -> the tag on stdout
  answer=$(given "$1")
  if [ -n "$answer" ]; then printf '%s' "$answer"; return; fi
  var="$1"; title="$2"; text="$3"; shift 3
  need_tty "$var"
  if [ "$UI" = text ]; then
    # Tags and items alternate: the items are listed, and the nth tag returned.
    printf '\n%s\n%s\n' "$title" "$text" > "$TTY"
    i=0; for word in "$@"; do i=$((i+1)); [ $((i % 2)) = 0 ] && printf '  %d) %s\n' $((i / 2)) "$word" > "$TTY"; done
    printf 'Select 1-%d [1]: ' $(( $# / 2 )) > "$TTY"
    read -r reply < "$TTY" || reply=""
    [ -z "$reply" ] && reply=1
    i=0; for word in "$@"; do i=$((i+1)); if [ $((i % 2)) = 1 ] && [ $(( (i + 1) / 2 )) = "$reply" ]; then printf '%s' "$word"; return; fi; done
    fail "Invalid selection: $reply"
  fi
  box_menu "$title" "$text" "$@" || cancelled
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

# ------------------------------------------------------------------ system checks
REPORT=""
check() { # pass|warn|fail text
  # Long lines wrap under their own text, not under the status column.
  text=$(printf '%s\n' "$2" | fold -s -w 64 | sed 's/ *$//; 2,$s/^/        /')
  case "$1" in
    pass) REPORT="$REPORT
  PASS  $text" ;;
    warn) REPORT="$REPORT
  WARN  $text" ;;
    fail) REPORT="$REPORT
  FAIL  $text"; FAILED=1 ;;
  esac
}

prechecks() { # role
  REPORT=""; FAILED=0
  [ "$(id -u)" = 0 ] && check pass "Running as root" || check fail "Not running as root. Run the installer with sudo."
  have curl && check pass "curl is installed" || check fail "curl is not installed."
  have systemctl && check pass "systemd is available" || check fail "systemd is not available. $(dist_name) requires systemd."
  case "$(uname -m)" in
    x86_64|amd64|aarch64|arm64) check pass "$(os_name), $(uname -m)" ;;
    *) check fail "Unsupported architecture: $(uname -m). x86_64 and arm64 are supported." ;;
  esac
  mem=$(mem_mb); cpus=$(nproc 2>/dev/null || echo 1)
  if [ "$1" = agent ]; then want=1024; node="worker"; else want=2048; node="server"; fi
  if [ "$mem" -lt "$want" ]; then check fail "${mem} MB memory. A $node node requires at least $want MB."
  elif [ "$mem" -lt 4096 ] && [ "$1" = new ]; then check warn "${mem} MB memory. 4 GB is the minimum recommended for $(dist_name) with Longhorn; 8 GB or more for virtual machines."
  elif [ "$mem" -lt 4096 ] && [ "$1" = server ] && [ "$DIST" = rke2 ]; then check warn "${mem} MB memory. 4 GB or more is recommended for an RKE2 server node."
  else check pass "${mem} MB memory, $cpus CPUs"; fi
  free=$(disk_gb); free=${free:-0}
  if [ "$free" -lt 10 ]; then check fail "${free} GB free in /var. At least 10 GB is required."
  elif [ "$free" -lt 30 ]; then check warn "${free} GB free in /var. 30 GB or more is recommended for container images and volumes."
  else check pass "${free} GB free in /var"; fi
  host=$(hostname)
  if printf '%s' "$host" | grep -Eq '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$' && [ "$host" != localhost ]; then
    check pass "Hostname $host is a valid node name"
  else
    check fail "Hostname '$host' is not a valid node name. Use lowercase letters, digits and hyphens (hostnamectl set-hostname node1)."
  fi
  get="get.$DIST.io"
  if reachable "https://$get" && reachable https://ghcr.io/v2/; then check pass "Internet access ($get, ghcr.io)"
  else check fail "Cannot reach $get or ghcr.io. Internet access is required."; fi
  if [ "$1" != addons ]; then
    ports="6443 10250"; [ "$DIST" = rke2 ] && ports="6443 9345 10250"
    busy=""
    for port in $ports; do listening "$port" && busy="$busy $port"; done
    if [ -n "$busy" ]; then check fail "Port(s)$busy in use. Another Kubernetes installation or service may be running."
    else check pass "Ports $(echo $ports | sed 's/ /, /g') are available"; fi
  fi
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; then check pass "System clock is synchronised"
  else check warn "System clock is not synchronised. Certificate validation and cluster join may fail."; fi
  if active ufw || active firewalld; then
    check warn "A firewall is active. Between nodes, allow TCP 6443,$([ "$DIST" = rke2 ] && echo " 9345,") 10250 and 2379-2380, and UDP 8472. Allow TCP 8088 for Homestead."
  fi
  if [ -n "${NODE_IP:-}" ] && dynamic_ip "$NODE_IP"; then
    check warn "$NODE_IP is assigned by DHCP. Reserve it on the DHCP server; a changed address breaks the cluster."
  fi
  if [ "$1" = new ]; then
    kvm && check pass "Hardware virtualisation is available" \
      || check warn "Hardware virtualisation is not available. Virtual machines would run in emulation mode."
  fi
  if [ "$FAILED" = 1 ] && [ "$SKIP_CHECKS" = 0 ]; then
    msg "System Checks Failed" "Resolve the following before installing:
$REPORT"
    fail "Installation stopped: system checks failed."
  fi
  msg "System Checks" "Results for $(hostname):
$REPORT"
}

# ------------------------------------------------------------------ versions
# Release lists are retrieved only when a version menu opens, never for an
# unattended installation. k3s and RKE2 publish release channels; the other
# components are listed from their GitHub releases.
K8S_VERSION=$(given HS_K8S_VERSION); K8S_NOTE=""
LONGHORN_VERSION=$(given HS_LONGHORN_VERSION); LONGHORN_NOTE=""
KUBEVIRT_VERSION=$(given HS_KUBEVIRT_VERSION); KUBEVIRT_NOTE=""
CDI_VERSION=$(given HS_CDI_VERSION); CDI_NOTE=""
HS_RELEASE=$(given HS_VERSION); HS_NOTE=""
[ -z "$HS_RELEASE" ] && [ "$REF" != main ] && HS_RELEASE="${REF#v}"
HS_RELEASE="${HS_RELEASE#v}"
VCACHE="${TMPDIR:-/tmp}/homestead-releases.$$"

fetch() { curl -sfL --max-time 15 "$1" 2>/dev/null; }
newest_first() { sed 's/^v//' | sort -t. -k1,1nr -k2,2nr -k3,3nr | sed 's/^/v/'; }
cached() { # key command... -> the command's output, retrieved once per run
  mkdir -p "$VCACHE" 2>/dev/null
  f="$VCACHE/$1"; shift
  [ -s "$f" ] || "$@" > "$f" 2>/dev/null
  cat "$f" 2>/dev/null
}
channels() { # k3s|rke2 -> "channel version" lines, releases only
  fetch "https://update.$1.io/v1-release/channels" | tr -d '\n' \
    | grep -o '"name":"[^"]*","latest":"[^"]*"' \
    | sed 's/"name":"\([^"]*\)","latest":"\([^"]*\)"/\1 \2/' \
    | grep -v '^testing ' | grep -Ev ' v[0-9.]+-'
}
releases() { # owner/repo -> release tags, newest first, without pre-releases
  fetch "https://api.github.com/repos/$1/releases?per_page=50" | grep -o '"tag_name": *"[^"]*"' \
    | sed 's/.*"\([^"]*\)"$/\1/' | grep -E '^v?[0-9]+\.[0-9]+\.[0-9]+$' | newest_first
}
kubevirt_stable() { fetch https://storage.googleapis.com/kubevirt-prow/release/kubevirt/kubevirt/stable.txt | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$'; }
channel_version() { cached "channels-$DIST" channels "$DIST" | awk -v c="$1" '$1==c {print $2; exit}'; }
recommended() { # component -> its recommended release
  case "$1" in
    k8s) channel_version stable ;;
    longhorn) cached longhorn releases longhorn/longhorn | head -n 1 ;;
    kubevirt) cached kubevirt-stable kubevirt_stable ;;
    cdi) cached cdi releases kubevirt/containerized-data-importer | head -n 1 ;;
    homestead) cached homestead releases wjcloudy/homestead | head -n 1 | sed 's/^v//' ;;
  esac
}
component_name() {
  case "$1" in k8s) dist_name ;; longhorn) echo Longhorn ;; kubevirt) echo KubeVirt ;; cdi) echo CDI ;; homestead) echo Homestead ;; esac
}
get_version() {
  case "$1" in k8s) echo "$K8S_VERSION" ;; longhorn) echo "$LONGHORN_VERSION" ;; kubevirt) echo "$KUBEVIRT_VERSION" ;;
    cdi) echo "$CDI_VERSION" ;; homestead) echo "$HS_RELEASE" ;; esac
}
set_version() { # component version note
  case "$1" in
    k8s) K8S_VERSION="$2"; K8S_NOTE="$3" ;;
    longhorn) LONGHORN_VERSION="$2"; LONGHORN_NOTE="$3" ;;
    kubevirt) KUBEVIRT_VERSION="$2"; KUBEVIRT_NOTE="$3" ;;
    cdi) CDI_VERSION="$2"; CDI_NOTE="$3" ;;
    homestead) HS_RELEASE="${2#v}"; HS_NOTE="$3" ;;
  esac
}
get_note() {
  case "$1" in k8s) echo "$K8S_NOTE" ;; longhorn) echo "$LONGHORN_NOTE" ;; kubevirt) echo "$KUBEVIRT_NOTE" ;;
    cdi) echo "$CDI_NOTE" ;; homestead) echo "$HS_NOTE" ;; esac
}
default_note() {
  case "$1" in k8s) echo "stable channel" ;; kubevirt) echo "stable release" ;; *) echo "latest release" ;; esac
}

# Versions not set are resolved to the current recommended release, so the
# summary shows exactly what will be installed.
resolve_versions() { # components...
  said=""
  for c in "$@"; do
    [ -n "$(get_version "$c")" ] && { [ -n "$(get_note "$c")" ] || set_version "$c" "$(get_version "$c")" "set"; continue; }
    [ -n "$said" ] || { infobox "Retrieving release information..."; said=1; }
    if [ "$c" = k8s ] && [ -n "${JOIN_VERSION:-}" ]; then set_version k8s "$JOIN_VERSION" "matches the cluster"; continue; fi
    set_version "$c" "$(recommended "$c")" "$(default_note "$c")"
  done
}

version_line() { # component -> a summary line
  v=$(get_version "$1"); n=$(get_note "$1")
  printf '%-20s %s%s' "$(component_name "$1") version" "${v:-default}" "${n:+  ($n)}"
}

version_choices() { # component -> "version|label" lines, newest first
  rec=$(recommended "$1")
  if [ "$1" = k8s ]; then
    stable=$(channel_version stable); latest=$(channel_version latest)
    [ -n "$stable" ] && printf '%s|%-18s %s
' "$stable" "$stable" "stable channel (recommended)"
    [ -n "$latest" ] && [ "$latest" != "$stable" ] && printf '%s|%-18s %s
' "$latest" "$latest" "latest channel"
    cached "channels-$DIST" channels "$DIST" | awk '$1 ~ /^v[0-9]+\.[0-9]+$/ {print $2}' | newest_first | head -n 6 |
      while read -r v; do
        [ "$v" = "$stable" ] || [ "$v" = "$latest" ] || printf '%s|%-18s %s channel
' "$v" "$v" "$(printf '%s' "$v" | cut -d. -f1-2)"
      done
    return
  fi
  case "$1" in
    longhorn) list=$(cached longhorn releases longhorn/longhorn) ;;
    kubevirt) list=$(cached kubevirt releases kubevirt/kubevirt) ;;
    cdi) list=$(cached cdi releases kubevirt/containerized-data-importer) ;;
    homestead) list=$(cached homestead releases wjcloudy/homestead); rec="v$rec" ;;
  esac
  printf '%s\n' "$list" | head -n 8 | while read -r v; do
    [ -n "$v" ] || continue
    if [ "$v" = "$rec" ]; then printf '%s|%-18s %s
' "$v" "$v" "recommended"; else echo "$v|$v"; fi
  done
}

pick_version() { # component
  comp="$1"; name=$(component_name "$comp")
  choices=$(version_choices "$comp")
  set --
  while IFS='|' read -r v label; do [ -n "$v" ] && set -- "$@" "$v" "$label"; done <<EOF
$choices
EOF
  text="Select the $name version to install."
  [ $# -eq 0 ] && text="$text

The release list could not be retrieved. Check internet access, or enter a version manually."
  [ "$comp" = k8s ] && [ "$ROLE" != new ] && text="$text Use the same version as the existing server nodes."
  set -- "$@" "@manual" "Enter a version manually"
  v=$(menu "$name Version" "$text" "$@") || return 0
  if [ "$v" = "@manual" ]; then
    example=$(printf '%s\n' "$choices" | head -n 1 | cut -d'|' -f1); example=${example:-v1.0.0}
    if [ "$UI" = text ]; then
      printf 'Enter the %s version (for example, %s): ' "$name" "$example" > "$TTY"; read -r v < "$TTY" || v=""
    else
      v=$("$BOX" --title "$name Version" --inputbox "Enter the $name version (for example, $example):" 9 78 "" 3>&1 1>"$TTY" 2>&3 < "$TTY") || return 0
    fi
    [ -n "$v" ] || return 0
    printf '%s' "$v" | grep -Eq '^v?[0-9]+\.[0-9]+\.[0-9]+' || { msg "Invalid Version" "'$v' is not a valid version number."; return 0; }
    case "$v" in v*) ;; *) [ "$comp" = homestead ] || v="v$v" ;; esac
  fi
  if [ "${v#v}" = "$(recommended "$comp" | sed 's/^v//')" ]; then set_version "$comp" "$v" "$(default_note "$comp")"
  else set_version "$comp" "$v" "selected"; fi
}

version_args() { # components... -> the bootstrap options that pin them
  for c in "$@"; do
    v=$(get_version "$c"); [ -n "$v" ] || continue
    case "$c" in
      k8s) printf ' --%s-version %s' "$DIST" "$v" ;;
      longhorn) printf ' --longhorn-version %s' "$v" ;;
      kubevirt) printf ' --kubevirt-version %s' "$v" ;;
      cdi) printf ' --cdi-version %s' "$v" ;;
      homestead) printf ' --homestead-version %s' "${v#v}" ;;
    esac
  done
}

# The final screen before any change: the settings and each component's
# version. Selecting a component lists its releases. HS_YES skips it.
summary() { # text components...
  summary_text="$1"; shift
  answer=$(given HS_YES)
  if [ -n "$answer" ]; then case "$answer" in y*|Y*|1|true) return 0 ;; *) cancelled ;; esac; fi
  need_tty HS_YES
  resolve_versions "$@"
  comps="$*"
  while :; do
    set -- install "Install"
    for c in $comps; do set -- "$@" "$c" "$(version_line "$c")"; done
    set -- "$@" cancel "Cancel installation"
    BACK=Cancel; MENU_DEFAULT=1
    pick=$(menu "Installation Summary" "$summary_text" "$@") || pick=cancel
    BACK=Back; MENU_DEFAULT=""
    case "$pick" in
      install) return 0 ;;
      cancel) cancelled ;;
      *) pick_version "$pick" ;;
    esac
  done
}

# kube-vip (virtual IPs) and Multus (dedicated LAN addresses) are installed
# by Homestead after it starts, unless HS_KUBEVIP or HS_MULTUS is "no".
network_line() {
  kv=yes; mu=yes
  case "$(given HS_KUBEVIP)" in n*|N*|0|false) kv=no ;; esac
  case "$(given HS_MULTUS)" in n*|N*|0|false) mu=no ;; esac
  if [ "$kv" = yes ] && [ "$mu" = yes ]; then echo "kube-vip and Multus"
  elif [ "$kv" = yes ]; then echo "kube-vip"
  elif [ "$mu" = yes ]; then echo "Multus"
  else echo "Not installed"; fi
}
# The address Homestead and apps answer on: a VIP, which kube-vip moves to
# another node when one goes down, rather than one node's own address. Left
# empty, everything stays on the nodes' addresses and a VIP can come later.
pick_vip() {
  VIP=""
  case "$(given HS_KUBEVIP)" in n*|N*|0|false) return 0 ;; esac
  if [ -z "$(given HS_VIP)" ] && ! interactive; then return 0; fi
  VIP=$(ask HS_VIP "Homestead and Apps Address" "Enter an unused address on your LAN, outside your router's DHCP range, for Homestead and your apps (for example, 192.0.2.200). It is a VIP: it moves to another node if one goes down.

Homestead will be at http://VIP:8088, as well as on each node's address, and apps share the VIP on their own ports. Its backup storage and network shares go there too.

Leave it empty to use the nodes' own addresses for now; add a VIP later under Networking." "")
  [ -n "$VIP" ] || return 0
  printf '%s' "$VIP" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || fail "'$VIP' is not a valid IPv4 address."
  [ "$VIP" = "${NODE_IP:-}" ] && fail "$VIP is this machine's own address; a VIP must be an unused one."
  if [ "$DRY" = 0 ] && ping -c 1 -W 1 "$VIP" >/dev/null 2>&1; then
    yesno HS_TAKEN "Address In Use" "A device already responds at $VIP. Use this address anyway?" no || fail "Installation cancelled. Choose an unused address."
  fi
}
vip_args() { [ -n "${VIP:-}" ] && printf -- ' --vip %s' "$VIP"; true; }
homestead_url() { if [ -n "${VIP:-}" ]; then echo "http://$VIP:8088 (and http://$1:8088)"; else echo "http://$1:8088"; fi; }

network_args() {
  case "$(given HS_KUBEVIP)" in n*|N*|0|false) printf ' --no-kube-vip' ;; esac
  case "$(given HS_MULTUS)" in n*|N*|0|false) printf ' --no-multus' ;; esac
  case "${NODEPROBE:-$(given HS_NODEPROBE)}" in n*|N*|0|false) printf ' --no-node-probe' ;; esac
  v=$(given HS_KUBEVIP_VERSION); [ -n "$v" ] && printf ' --kube-vip-version %s' "$v"
  v=$(given HS_MULTUS_VERSION); [ -n "$v" ] && printf ' --multus-version %s' "$v"
  true
}

# ------------------------------------------------------------------ progress
LOG=/var/log/homestead-install.log

# A long step, run with a progress bar that follows its "==> " stages. The
# full output goes to $LOG and is shown if the step fails. Without menus the
# output is printed as it runs.
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
      # grep -c prints 0 and fails before the first stage; "|| echo 0" made
      # that "0 0", the arithmetic below failed, and the gauge closed at once.
      done_stages=$(grep -c '^==> ' "$LOG" 2>/dev/null); done_stages=${done_stages:-0}
      stage=$(grep '^==> ' "$LOG" 2>/dev/null | tail -n 1 | cut -c5-)
      pct=$(( done_stages * 95 / stages )); [ "$pct" -gt 95 ] && pct=95
      printf 'XXX\n%d\n%s\n\n%s\nXXX\n' "$pct" "${stage:-Starting}" "$(tail -n 1 "$LOG" 2>/dev/null | cut -c1-70)"
      sleep 1
    done
    printf 'XXX\n100\nComplete\nXXX\n'
  } | "$BOX" --title "$title" --gauge "Starting" 12 78 0 > "$TTY" 2>&1
  wait "$job" 2>/dev/null
  code=$(cat "$status" 2>/dev/null || echo 1)
  if [ "$code" != 0 ]; then
    tail -n 40 "$LOG" > /tmp/homestead-install.tail 2>/dev/null
    "$BOX" --title "Installation Failed" --scrolltext --textbox /tmp/homestead-install.tail 24 100 < "$TTY" > "$TTY" 2>&1
    fail "Installation failed. The full log is in $LOG."
  fi
}

# ------------------------------------------------------------------ bootstrap
bootstrap() { # stages, then args for bootstrap-k3s.sh
  stages="$1"; shift
  script=/tmp/homestead-bootstrap-k3s.sh
  run curl -sfL "$RAW/$REF/scripts/bootstrap-k3s.sh" -o "$script" || fail "Could not download bootstrap-k3s.sh."
  progress "Installing" "$stages" sh "$script" "$@" || fail "Installation failed. See the output above."
}

# ------------------------------------------------------------------ flows
flow_new() {
  ROLE=$(choose HS_ROLE "Installation Mode" "Select how to install this machine:" \
    new "Create a new cluster" \
    server "Join an existing cluster as a server node" \
    agent "Join an existing cluster as a worker node")
  [ "$ROLE" = harvester ] && fail "This machine is not a Harvester node."
  case "$ROLE" in
    new) pick_dist ;;
    server|agent) find_cluster ;;
  esac
  pick_ip
  prechecks "$ROLE"
  case "$ROLE" in
    new) new_cluster ;;
    server|agent) join_cluster "$ROLE" ;;
    addons) add_to_cluster ;;
    *) fail "Unknown installation mode: $ROLE" ;;
  esac
}

pick_ip() {
  NODE_IP=$(given HS_NODE_IP)
  [ -n "$NODE_IP" ] && return
  best=$(default_ip)
  count=$(all_ips | wc -l | tr -d ' ')
  if [ "${count:-0}" -le 1 ]; then NODE_IP="$best"; return; fi
  set --
  while read -r dev addr; do set -- "$@" "$addr" "$addr   $dev$([ "$addr" = "$best" ] && printf ', default route')"; done <<EOF
$(all_ips)
EOF
  NODE_IP=$(choose HS_NODE_IP "Node IP Address" "Select the IP address that other nodes use to reach this machine. The node is registered with this address." "$@")
}

# The distribution for a new cluster. Unattended, k3s unless HS_DIST is set.
pick_dist() {
  if [ -z "$(given HS_DIST)" ] && ! interactive; then DIST=k3s; return; fi
  DIST=$(choose HS_DIST "Kubernetes Distribution" "Select the Kubernetes distribution for the new cluster. Homestead supports both." \
    k3s "k3s    Lightweight Kubernetes; suited to small machines" \
    rke2 "RKE2   Hardened upstream Kubernetes, as used by Harvester")
  case "$DIST" in k3s|rke2) ;; *) fail "Unsupported distribution: $DIST. Set HS_DIST to k3s or rke2." ;; esac
}

# The cluster to join: its address and distribution. An RKE2 server answers
# on port 9345, a k3s server on port 6443.
find_cluster() {
  server=$(ask HS_SERVER "Join Cluster" "Enter the IP address or hostname of an existing server node - the machine's own address, not the VIP Homestead and apps use (that one carries apps, not the cluster):" "")
  [ -n "$server" ] || fail "No server address was entered."
  host=${server#https://}; host=${host%%/*}; host=${host%%:*}
  given_dist=$(given HS_DIST)
  if [ -n "$given_dist" ]; then DIST="$given_dist"
  elif [ "$DRY" = 1 ]; then DIST=k3s
  elif curl -sk --max-time 10 "https://$host:9345/cacerts" 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then DIST=rke2
  elif curl -sk --max-time 10 "https://$host:6443/cacerts" 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then DIST=k3s
  else fail "Unable to reach a cluster at $host. Check the address, and that port 6443 (k3s) or 9345 (RKE2) is open."; fi
  case "$server" in
    https://*) url="$server" ;;
    *) if [ "$DIST" = rke2 ]; then url="https://$host:9345"; else url="https://$host:6443"; fi ;;
  esac
  # The cluster's own version, where its API server reports it without credentials.
  JOIN_VERSION=""
  [ "$DRY" = 1 ] || JOIN_VERSION=$(curl -sk --max-time 5 "https://$host:6443/version" 2>/dev/null \
    | sed -n 's/.*"gitVersion": *"\([^"]*\)".*/\1/p' | head -n 1)
}

# Room for Longhorn in a volume of its own: "vg usable reserve" in GB, when
# the system is on LVM and its group has 20 GB or more past the reserve.
lvm_room() {
  have lvs || return 0
  root=$(findmnt -n -o SOURCE / 2>/dev/null)
  vg=$(lvs --noheadings -o vg_name,lv_path,lv_dm_path 2>/dev/null </dev/null | awk -v r="$root" '$2 == r || $3 == r {print $1; exit}')
  [ -n "$vg" ] || return 0
  lvs "$vg/longhorn" >/dev/null 2>&1 </dev/null && return 0
  mountpoint -q /var/lib/longhorn 2>/dev/null && return 0
  vgs --noheadings --units g --nosuffix -o vg_size,vg_free "$vg" 2>/dev/null </dev/null |
    awk -v vg="$vg" '{s = int($1); f = int($2); r = int(s / 10); if (r < 10) r = 10; if (f - r >= 20) print vg, f - r, r}'
}

# How much of it Longhorn gets; LH_VOLUME is a size in GB, "none", or empty
# where there is no room (the bootstrap script then looks for itself).
pick_longhorn_volume() {
  LH_VOLUME=""; LH_LINE=""
  room=$(lvm_room)
  given_size=$(given HS_LONGHORN_VOLUME)
  [ -n "$room" ] || { [ "$given_size" = 0 ] && LH_VOLUME=none; return 0; }
  set -- $room
  # Unattended, the bootstrap script's own default: all the room there is.
  if [ -z "$given_size" ] && ! interactive; then LH_VOLUME=auto; LH_LINE="$1/longhorn, $2 GB of its own"; return 0; fi
  size=$(ask HS_LONGHORN_VOLUME "Longhorn Volume" "This machine's system is on LVM, and $1 has $2 GB free beyond $3 GB kept for the system.

Longhorn can have a volume of its own there, mounted at /var/lib/longhorn, so its data can never fill the system's filesystem. Space left out stays free: for the system, or later for Longhorn's V2 engine (Nodes > Disks > Use free space).

Size in GB (0 keeps Longhorn on the system's filesystem):" "$2")
  case "$size" in ''|*[!0-9]*) fail "The Longhorn volume size must be a number of GB." ;; esac
  if [ "$size" = 0 ]; then LH_VOLUME=none; LH_LINE="on the system's filesystem"
  else [ "$size" -gt "$2" ] && size=$2; LH_VOLUME=$size; LH_LINE="$1/longhorn, $size GB of its own"; fi
}
volume_args() { [ -n "${LH_VOLUME:-}" ] && printf -- ' --longhorn-volume %s' "$LH_VOLUME"; true; }

dist_name() { if [ "$DIST" = rke2 ]; then echo RKE2; else echo k3s; fi; }
token_file() { echo "/var/lib/rancher/$DIST/server/node-token"; }
dist_flag() { [ "$DIST" = rke2 ] && printf -- '--rke2'; true; }

new_cluster() {
  longhorn=yes; kubevirt=no
  kvm && kubevirt=yes
  if [ "$DIST" = k3s ]; then
    # RKE2 has no storage of its own, so Longhorn is always installed there.
    yesno HS_LONGHORN "Storage" "Install Longhorn distributed block storage?

Longhorn replicates volumes across nodes and provides snapshots and backups. The Homestead Volumes and Data Protection pages require it. Without Longhorn, k3s local-path storage keeps each volume on a single node." yes || longhorn=no
  fi
  # Unattended, it is installed unless HS_NODEPROBE says no, as kube-vip and Multus are.
  NODEPROBE=yes
  [ "$longhorn" = yes ] && pick_longhorn_volume
  if [ -n "$(given HS_NODEPROBE)" ] || interactive; then
    yesno HS_NODEPROBE "Node Probe" "Install the node probe on every node?

It reports temperatures, disk SMART health and each node's network interfaces to Homestead, and keeps kube-vip announcing on the right interface as nodes join. It runs a privileged container on each node to read them." yes || NODEPROBE=no
  fi
  yesno HS_KUBEVIRT "Virtual Machines" "Install KubeVirt and CDI to run virtual machines alongside containers?$(kvm || printf '\n\nHardware virtualisation is not available on this machine. Virtual machines would run in emulation mode, with reduced performance.')" "$kubevirt" && kubevirt=yes || kubevirt=no
  pick_vip
  comps="k8s"
  [ "$longhorn" = yes ] && comps="$comps longhorn"
  [ "$kubevirt" = yes ] && comps="$comps kubevirt cdi"
  comps="$comps homestead"
  # shellcheck disable=SC2086
  summary "Review the settings below. Select a component to change its version, or select Install to begin.

  Installation mode    New cluster (first server node)
  Distribution         $(dist_name)
  Node IP address      $NODE_IP
  Storage              $([ "$longhorn" = yes ] && echo "Longhorn${LH_LINE:+, $LH_LINE}" || echo "k3s local-path")
  Virtual machines     $([ "$kubevirt" = yes ] && echo "KubeVirt and CDI" || echo "Not installed")
  Networking           $(network_line)
  Node probe           $([ "$NODEPROBE" = yes ] && echo "Installed by Homestead" || echo "Not installed")
  Apps VIP             $([ -n "${VIP:-}" ] && echo "$VIP - Homestead, its storage and shares, and apps" || echo "None yet - the nodes' own addresses")
  Homestead URL        $(homestead_url "$NODE_IP")" $comps
  args="server --node-ip $NODE_IP"
  [ "$longhorn" = no ] && args="$args --no-longhorn"
  [ "$kubevirt" = yes ] && args="$args --kubevirt"
  [ "$DIST" = rke2 ] && args="$args --rke2"
  # shellcheck disable=SC2086
  args="$args$(version_args $comps)$(network_args)$(volume_args)$(vip_args)"
  stages=6; [ "$longhorn" = yes ] && stages=$((stages + 2)); [ "$kubevirt" = yes ] && stages=$((stages + 1)); [ "$DIST" = rke2 ] && stages=$((stages + 1))
  # shellcheck disable=SC2086
  bootstrap "$stages" $args
  finish_new
}

join_cluster() { # server|agent
  token=$(ask HS_TOKEN "Cluster Token" "Enter the cluster token. To display it, run this command on an existing server node:

  sudo cat $(token_file)" "" secret)
  [ -n "$token" ] || fail "No cluster token was entered."
  mode=agent; [ "$1" = server ] && mode=join
  pick_longhorn_volume
  summary "Review the settings below. Select a component to change its version, or select Install to begin.

  Installation mode    Join an existing cluster as a $([ "$1" = server ] && echo "server node" || echo "worker node")
  Distribution         $(dist_name)
  Cluster              $url
  Node name            $(hostname)
  Node IP address      $NODE_IP${LH_LINE:+
  Longhorn data        $LH_LINE}

Longhorn host packages (open-iscsi, NFS client) are installed first." k8s
  # shellcheck disable=SC2046
  bootstrap 3 "$mode" "$url" "$token" --node-ip "$NODE_IP" $(dist_flag) $(version_args k8s)$(volume_args)
  msg "Installation Complete" "$(hostname) has joined the cluster. It appears on the Homestead Nodes page within a few minutes."
}

add_to_cluster() {
  if [ "$DIST" = rke2 ]; then kcmd="/var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml"; else kcmd="k3s kubectl"; fi
  if $kcmd -n lab get deployment homestead >/dev/null 2>&1; then
    msg "Homestead Already Installed" "Homestead is already running on this cluster at http://$(default_ip):8088. Update it from Settings > About > Homestead updates."
    exit 0
  fi
  comps="homestead"; storage="Longhorn (already installed)"
  if ! $kcmd get crd volumes.longhorn.io >/dev/null 2>&1; then comps="longhorn homestead"; storage="Longhorn (to be installed)"; fi
  pick_vip
  # shellcheck disable=SC2086
  summary "Review the settings below. Select a component to change its version, or select Install to begin.

  Installation mode    Install on this $(dist_name) cluster
  Storage              $storage
  Networking           $(network_line)
  Apps VIP             $([ -n "${VIP:-}" ] && echo "$VIP - Homestead, its storage and shares, and apps" || echo "None yet - the nodes' own addresses")
  Homestead URL        $(homestead_url "$(default_ip)")" $comps
  # shellcheck disable=SC2086,SC2046
  bootstrap 5 addons $(dist_flag) $(version_args $comps) $(network_args)$(vip_args)
  finish_new
}

finish_new() {
  ip="${NODE_IP:-$(default_ip)}"
  msg "Installation Complete" "Homestead is available at:

  http://$ip:8088$([ -n "${VIP:-}" ] && printf '\n  http://%s:8088 - once kube-vip is up, a minute or two after Homestead' "$VIP")

Open this address to create the administrator account.

To add a node, run the installer on the new machine and select Join an existing cluster. Give it this server's own address ($ip)$([ -n "${VIP:-}" ] && printf ', not the apps VIP (%s): the VIP carries apps, not the cluster' "$VIP") - and the cluster token, which this command displays:

  sudo cat $(token_file)

kube-vip, Multus$(case "${NODEPROBE:-$(given HS_NODEPROBE)}" in n*|N*|0|false) ;; *) printf ' and the node probe' ;; esac) are installed by Homestead after it starts; their progress is shown in its job tray. Anything left out can be added under Settings > Cluster > Add-ons."
}

flow_harvester() {
  KC=kubectl
  if [ -x /var/lib/rancher/rke2/bin/kubectl ] && [ -f /etc/rancher/rke2/rke2.yaml ]; then
    KC="/var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml"
  fi
  $KC get nodes >/dev/null 2>&1 || [ "$DRY" = 1 ] \
    || fail "kubectl cannot reach the Harvester cluster. Run the installer as root on a Harvester management node."
  if $KC -n lab get deployment homestead >/dev/null 2>&1; then
    msg "Homestead Already Installed" "Homestead is already running on this Harvester cluster. Update it from Settings > About > Homestead updates."
    exit 0
  fi
  REPORT=""; FAILED=0
  reachable https://ghcr.io/v2/ && check pass "Internet access (ghcr.io)" || check fail "Cannot reach ghcr.io, which hosts the Homestead image."
  if [ "$FAILED" = 1 ] && [ "$SKIP_CHECKS" = 0 ]; then msg "System Checks Failed" "$REPORT"; fail "Installation stopped: system checks failed."; fi
  vip=$(ask HS_VIP "Homestead IP Address" "Harvester detected. Enter an unused IP address for Homestead on your LAN, outside the DHCP range (for example, 192.0.2.242):" "")
  printf '%s' "$vip" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || fail "'$vip' is not a valid IPv4 address."
  if [ "$DRY" = 0 ] && ping -c 1 -W 1 "$vip" >/dev/null 2>&1; then
    yesno HS_TAKEN "Address In Use" "A device already responds at $vip. Use this address anyway?" no || fail "Installation cancelled. Choose an unused address."
  fi
  set --
  for c in $($KC get storageclass -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do set -- "$@" "$c" "$c"; done
  [ $# -gt 0 ] || set -- harvester-longhorn "harvester-longhorn"
  class=$(choose HS_CLASS "Storage Class" "Select the storage class for Homestead's data. harvester-longhorn is the Harvester default." "$@")
  summary "Review the settings below. Select a component to change its version, or select Install to begin.

  Installation mode    Install on this Harvester cluster
  Homestead URL        http://$vip:8088
  Storage class        $class
  Namespace            lab" homestead
  say "Installing Homestead"
  manifest=/tmp/homestead-deploy.yaml
  ref="$REF"; [ -n "$HS_RELEASE" ] && ref="v$HS_RELEASE"
  run curl -sfL "$RAW/$ref/deploy/deploy.yaml" -o "$manifest" || fail "Could not download the Homestead manifest."
  run sed -i -e "s/192\\.0\\.2\\.242/$vip/g" -e "s/longhorn-r2/$class/g" \
    -e "s/accessModes: \\[ReadWriteMany\\]/accessModes: [ReadWriteOnce]/" "$manifest"
  # shellcheck disable=SC2086
  run $KC apply -f "$manifest" || fail "kubectl could not apply the Homestead manifest."
  # shellcheck disable=SC2086
  progress "Installing" 1 sh -c "echo '==> Waiting for Homestead to start'; $KC -n lab rollout status deployment/homestead --timeout=10m" \
    || fail "Homestead did not start. Run: $KC -n lab get pods"
  msg "Installation Complete" "Homestead is available at:

  http://$vip:8088

Open this address to create the administrator account.

Next steps: reserve addresses for applications under Networking > Your VIPs, and install the node probe from Settings > Cluster > Add-ons. See $WIKI/Installing-on-Harvester"
}

# ------------------------------------------------------------------ doctor prompts
DLOG=/var/log/homestead-doctor.log
DMODE=menu
FOUND=$(mktemp 2>/dev/null || echo /tmp/homestead-doctor.$$)
trap 'rm -rf "$FOUND" "$VCACHE"' EXIT
logline() { { printf '%s %s\n' "$(date '+%F %T' 2>/dev/null)" "$*" >> "$DLOG"; } 2>/dev/null || true; }
confirm() { # title text -> 0 for yes
  [ "$DMODE" = fix ] && return 0
  if [ "$UI" = text ]; then
    interactive || return 1
    printf '\n%s\n%s [y/N] ' "$1" "$2" > "$TTY"; read -r reply < "$TTY" || reply=""
    case "$reply" in y*|Y*) return 0 ;; *) return 1 ;; esac
  fi
  "$BOX" --title "$1" --defaultno --yesno "$2" "$(fit "$2" 7)" 78 < "$TTY" > "$TTY" 2>&1
}
typed() { # title text word -> 0 when the word is typed
  if [ "$UI" = text ]; then
    printf '\n%s\n%s\nType %s to continue: ' "$1" "$2" "$3" > "$TTY"; read -r reply < "$TTY" || reply=""
  else
    reply=$("$BOX" --title "$1" --inputbox "$2

Type $3 to continue:" "$(fit "$2" 11)" 78 "" 3>&1 1>"$TTY" 2>&3 < "$TTY") || return 1
  fi
  [ "$reply" = "$3" ]
}
menu() { # title text tag item... -> tag, or 1 when cancelled
  title="$1"; text="$2"; shift 2
  if [ "$UI" = text ]; then
    printf '\n%s\n%s\n' "$title" "$text" > "$TTY"
    i=0; for word in "$@"; do i=$((i+1)); [ $((i % 2)) = 0 ] && printf '  %2d) %s\n' $((i / 2)) "$word" > "$TTY"; done
    if [ -n "$MENU_DEFAULT" ]; then printf 'Select 1-%d [1]: ' $(( $# / 2 )) > "$TTY"
    else printf 'Select 1-%d (Enter for %s): ' $(( $# / 2 )) "$BACK" > "$TTY"; fi
    read -r reply < "$TTY" || reply=""
    if [ -z "$reply" ]; then [ -n "$MENU_DEFAULT" ] && reply=1 || return 1; fi
    i=0; for word in "$@"; do i=$((i+1)); if [ $((i % 2)) = 1 ] && [ $(( (i + 1) / 2 )) = "$reply" ]; then printf '%s' "$word"; return 0; fi; done
    return 1
  fi
  box_menu "$title" "$text" "$@"
}

# ------------------------------------------------------------------ the node
KIND=none; SERVICE=""; DATA=""; KC=""
detect_node() {
  KIND=none; SERVICE=""; DATA=""; KC=""
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
# A cluster that does not respond must not stall the checks.
kc() {
  [ -n "$KC" ] || return 1
  if have timeout; then timeout 60 $KC "$@" 2>/dev/null; else $KC "$@" 2>/dev/null; fi
}

# Main-menu discovery has a shorter deadline than a full health check. All
# requests are read-only, and an unavailable API must still leave a usable menu.
summary_kc() {
  [ -n "$KC" ] || return 1
  if have timeout; then timeout 4 $KC --request-timeout=3s "$@" 2>/dev/null
  else $KC --request-timeout=3s "$@" 2>/dev/null; fi
}
summary_line() { # Keep the main menu usable on a 24-row terminal.
  printf '%s\n' "$*" | awk '{if (length > 80) print substr($0,1,77) "..."; else print}'
}
summary_addresses() { # label value: wrap with an aligned continuation indent.
  printf '%s\n' "$2" | awk -v label="$1" '
    BEGIN {prefix=sprintf("%-12s",label); line=prefix}
    {for(i=1;i<=NF;i++) {
      word=$i
      if(length(line)>12 && length(line)+1+length(word)>80) {print line; line=sprintf("%12s", "")}
      if(length(line)>12) line=line " "
      while(length(line)+length(word)>80) {
        take=80-length(line); print line substr(word,1,take); word=substr(word,take+1); line=sprintf("%12s", "")
      }
      line=line word
    }} END {print line}'
}
prepare_overview() {
  CLUSTER_ADDRESS_LINES="$(summary_addresses Members "$CLUSTER_MEMBERS")
$(summary_addresses VIPs "$CLUSTER_VIP_SUMMARY")"
  # Keep status and menu actions visible. Large lists continue on further
  # menu pages rather than being silently truncated or pushing actions away.
  CLUSTER_PAGE_SIZE=$(( $(term_rows) - 20 ))
  [ "$CLUSTER_PAGE_SIZE" -ge 2 ] || CLUSTER_PAGE_SIZE=2
  count=$(printf '%s\n' "$CLUSTER_ADDRESS_LINES" | wc -l | tr -d ' ')
  CLUSTER_PAGES=$(( (count + CLUSTER_PAGE_SIZE - 1) / CLUSTER_PAGE_SIZE ))
  CLUSTER_PAGE=${CLUSTER_PAGE:-1}
  [ "$CLUSTER_PAGE" -le "$CLUSTER_PAGES" ] || CLUSTER_PAGE=1
}
component_summary() { # name|image|ready|desired, from an existing workload
  awk -F '|' '{
    image=$2; sub(/^.*\//,"",image)
    if (image == "") image="image unavailable"
    if (image ~ /@sha256:/) image=substr(image,1,index(image,"@sha256:")+19) " (digest pinned)"
    ready=$3+0; desired=$4+0
    state=(desired == 0 ? "not scheduled" : ready >= desired ? "ready" : "not ready")
    printf "%s (%d/%d); %s", state, ready, desired, image
  }'
}
cluster_overview() {
  CLUSTER_NODES=""; CLUSTER_VIPS=""; CLUSTER_API="unavailable"
  CLUSTER_K8S="unavailable"; CLUSTER_LONGHORN="unavailable"
  CLUSTER_HOMESTEAD="unavailable"; HOMESTEAD_PRESENT=unknown
  CLUSTER_NOTE=""; CLUSTER_MEMBERS="unavailable"; CLUSTER_VIP_SUMMARY="unavailable"
  [ "$KIND" != none ] || return 0
  if [ -z "$KC" ]; then
    CLUSTER_NOTE="Cluster details require server-node credentials; run setup on a server."
    return 0
  fi
  CLUSTER_API=$(summary_kc config view --minify -o jsonpath='{.clusters[0].cluster.server}') || CLUSTER_API="unavailable"
  if ! CLUSTER_NODES=$(summary_kc get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.addresses[?(@.type=="InternalIP")].address}{"|"}{.status.conditions[?(@.type=="Ready")].status}{"|"}{.status.nodeInfo.kubeletVersion}{"\n"}{end}'); then
    CLUSTER_NOTE="Cluster API unavailable or access denied; status could not be read."
    return 0
  fi
  CLUSTER_K8S=$(printf '%s\n' "$CLUSTER_NODES" | awk -F '|' 'NF>=4 && !seen[$4]++ {printf "%s%s", sep,$4; sep=", "}')
  CLUSTER_MEMBERS=$(printf '%s\n' "$CLUSTER_NODES" | awk -F '|' 'NF>=4 {printf "%s%s %s%s",sep,$1,$2,($3=="True" ? "" : " (not ready)"); sep=", "}')
  CLUSTER_NODES=$(printf '%s\n' "$CLUSTER_NODES" | awk -F '|' 'NF>=4 {printf "%s  %s  %s  %s\n",$1,$2,($3=="True" ? "Ready" : "NotReady"),$4}')
  if [ -z "$CLUSTER_NODES" ]; then CLUSTER_MEMBERS="no members returned"; CLUSTER_K8S="unavailable"; fi
  if lh=$(summary_kc -n longhorn-system get daemonset longhorn-manager --ignore-not-found -o jsonpath='{.metadata.name}{"|"}{.spec.template.spec.containers[?(@.name=="longhorn-manager")].image}{"|"}{.status.numberReady}{"|"}{.status.desiredNumberScheduled}'); then
    # Empty JSONPath literals can survive --ignore-not-found: require a name.
    case "$lh" in longhorn-manager\|*) CLUSTER_LONGHORN=$(printf '%s\n' "$lh" | component_summary) ;;
      *) CLUSTER_LONGHORN="not installed" ;; esac
  fi
  if hs=$(summary_kc -n lab get deployment homestead --ignore-not-found -o jsonpath='{.metadata.name}{"|"}{.spec.template.spec.containers[?(@.name=="homestead")].image}{"|"}{.status.readyReplicas}{"|"}{.spec.replicas}'); then
    case "$hs" in homestead\|*) HOMESTEAD_PRESENT=yes; CLUSTER_HOMESTEAD=$(printf '%s\n' "$hs" | component_summary) ;;
      *) HOMESTEAD_PRESENT=no; CLUSTER_HOMESTEAD="not installed" ;; esac
  fi
  if services=$(summary_kc get services -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}{"/"}{.metadata.name}{"|"}{range .status.loadBalancer.ingress[*]}{.ip}{.hostname}{" "}{end}{"|"}{.spec.loadBalancerIP}{" "}{.metadata.annotations.kube-vip\.io/loadbalancerIPs}{"\n"}{end}'); then
    CLUSTER_VIPS=$(printf '%s\n' "$services" | awk -F '|' 'NF>=3 {
      gsub(/^ +| +$/,"",$2); gsub(/^ +| +$/,"",$3)
      if ($2!="") print $1 "  " $2 " (assigned)"
      else if ($3!="") print $1 "  " $3 " (requested; pending)"
      else print $1 "  pending address"
    }')
    CLUSTER_VIP_SUMMARY=$(printf '%s\n' "$CLUSTER_VIPS" | awk 'NF {printf "%s%s",sep,$0; sep=", "}')
    [ -n "$CLUSTER_VIP_SUMMARY" ] || CLUSTER_VIP_SUMMARY="no LoadBalancer services"
  fi
}
main_overview() {
  summary_line "Host        $(hostname)  $(default_ip)"
  summary_line "Detected    $here"
  if [ "$KIND" = none ]; then
    summary_line "System      $(os_name), $(uname -m)"
  else
    summary_line "Kubernetes  $CLUSTER_K8S"
    summary_line "API         $CLUSTER_API"
    prepare_overview
    printf '%s\n' "$CLUSTER_ADDRESS_LINES" | awk -v page="$CLUSTER_PAGE" -v size="$CLUSTER_PAGE_SIZE" 'NR>(page-1)*size && NR<=page*size'
    summary_line "Longhorn    $CLUSTER_LONGHORN"
    summary_line "Homestead   $CLUSTER_HOMESTEAD"
  fi
}
cluster_details() {
  details="Host: $(hostname)  $(default_ip)
Detected: $here
API endpoint: $CLUSTER_API
Kubernetes node versions: $CLUSTER_K8S

Members (name, internal IPs, readiness, Kubernetes):
${CLUSTER_NODES:-Unavailable}

Service VIPs (assigned addresses or pending requests):
${CLUSTER_VIPS:-$CLUSTER_VIP_SUMMARY}

Longhorn managers: $CLUSTER_LONGHORN
Homestead deployment: $CLUSTER_HOMESTEAD
Versions above are workload image tags; digest pins may have no version tag.
Readiness is ready/desired replicas, not a full cluster or volume health check.
API endpoint may be local; service VIPs are separate from the control-plane endpoint.
$CLUSTER_NOTE"
  if [ "$UI" = text ]; then msg "Cluster details" "$details"
  else "$BOX" --title "Cluster details" --scrolltext --msgbox "$details" "$(fit "$details" 7)" 78 < "$TTY" > "$TTY" 2>&1; fi
}

# ------------------------------------------------------------------ findings
# One line each: id|level|title|detail|fix|safe
found() { printf '%s|%s|%s|%s|%s|%s\n' "$1" "$2" "$3" "$4" "${5:-}" "${6:-no}" >> "$FOUND"; }

check_service() {
  [ -n "$SERVICE" ] || { found service bad "No Kubernetes service found" "k3s, RKE2 and kubelet are not installed on this node. Select Install Homestead from the main menu to create or join a cluster."; return; }
  if active "$SERVICE"; then found service ok "$SERVICE service is running" "systemd reports the service as active."
  else
    why=$(journalctl -u "$SERVICE" -p err -n 5 --no-pager 2>/dev/null | tail -n 5 | cut -c1-200)
    found service bad "$SERVICE service is not running" "systemd reports the service as $(systemctl is-active "$SERVICE" 2>/dev/null). Recent errors:
$why" fix_restart yes
  fi
}
check_disk() {
  for m in / /var; do
    use=$(df -P "$m" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
    case "$use" in ''|*[!0-9]*) continue ;; esac
    [ "$use" -le 100 ] || continue
    if [ "$use" -ge 90 ]; then found "disk$m" bad "$m is ${use}% full" "Kubernetes evicts pods and rejects new ones when a node runs low on disk space. The fix removes unused container images and reduces the system journal." fix_cleanup yes
    elif [ "$use" -ge 80 ]; then found "disk$m" warn "$m is ${use}% full" "Disk usage is high. The fix removes unused container images and reduces the system journal." fix_cleanup yes
    else found "disk$m" ok "$m is ${use}% full" "Sufficient free space."; fi
    ino=$(df -Pi "$m" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
    case "$ino" in ''|*[!0-9]*) ino=0 ;; esac
    if [ "$ino" -le 100 ] && [ "$ino" -ge 90 ]; then
      found "inodes$m" bad "$m has used ${ino}% of its inodes" "New files cannot be created once inodes are exhausted, even when disk space remains. This is usually caused by many small files in container layers or logs." fix_cleanup yes
    fi
  done
}
check_memory() {
  total=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null); avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null)
  [ -n "$total" ] && [ -n "$avail" ] || return
  pct=$(( avail * 100 / total ))
  if [ "$pct" -lt 5 ]; then found memory bad "Only ${pct}% of memory is available" "The kernel may terminate processes to reclaim memory. Review workload memory use on the Homestead Nodes page, and stop or move workloads."
  elif [ "$pct" -lt 15 ]; then found memory warn "${pct}% of memory is available" "Available memory is low. Review workload memory use on the Homestead Nodes page."
  else found memory ok "${pct}% of memory is available" "$(( avail / 1024 )) MB of $(( total / 1024 )) MB available."; fi
}
check_clock() {
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; then found clock ok "System clock is synchronised" "NTP synchronisation is active."
  else found clock warn "System clock is not synchronised" "Clock drift causes certificate, etcd and cluster join failures. The fix enables time synchronisation." fix_clock yes; fi
}
check_iscsi() {
  { have iscsiadm || kc get crd volumes.longhorn.io >/dev/null; } || return
  if active iscsid || active iscsid.socket; then found iscsid ok "iscsid is running" "Longhorn attaches volumes over iSCSI."
  else found iscsid bad "iscsid is not running" "Longhorn attaches volumes over iSCSI. Volumes cannot attach to this node until iscsid is running." fix_iscsid yes; fi
  if active multipathd && [ "$KIND" != harvester ] && ! grep -qs 'devnode "\^sd\[a-z0-9\]+"' /etc/multipath.conf; then
    found multipath warn "multipathd may claim Longhorn devices" "multipathd can claim the block devices that Longhorn creates, which makes volume mounts fail with \"already mounted or mount point busy\". Longhorn recommends excluding these devices in /etc/multipath.conf." fix_multipath no
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
  if [ "$days" -lt 7 ]; then found certs bad "A certificate expires in $days days" "$(basename "$soonest_file") expires in $days days, after which the API server stops responding. Restarting $SERVICE renews certificates that expire within 90 days." fix_restart yes
  elif [ "$days" -lt 90 ]; then found certs warn "Certificates expire in $days days" "$SERVICE renews certificates that expire within 90 days when it restarts. The fix restarts it now." fix_restart yes
  else found certs ok "Certificates are valid for $days days" "The next to expire is $(basename "$soonest_file")."; fi
}
check_runtime() {
  [ -n "$CRICTL" ] || return
  if $CRICTL info >/dev/null 2>&1; then found runtime ok "Container runtime is responding" "containerd is running."
  else found runtime bad "Container runtime is not responding" "No containers can start on this node. Restarting $SERVICE restarts containerd." fix_restart yes; fi
}

check_cluster() {
  [ -n "$KC" ] || { found api warn "Cluster checks skipped" "kubectl cannot reach the cluster from this node. Worker nodes do not hold administrator credentials; run the doctor on a server node to check the cluster."; return; }
  if kc get --raw /readyz >/dev/null; then found api ok "API server is responding" "kubectl can reach the API server."
  else found api bad "API server is not responding" "The cluster cannot be changed until the API server responds. Restarting $SERVICE on a server node usually restores it." fix_restart yes; return; fi
  case "$KIND" in k3s-server|rke2-server|harvester)
    if kc get --raw /readyz/etcd >/dev/null; then found etcd ok "etcd is healthy" "The cluster datastore is responding."
    else found etcd bad "etcd is not healthy" "The cluster datastore is not responding, usually because too few server nodes are available for quorum. Check the other server nodes first." ; fi ;;
  esac
  state=$(kc get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status} {.spec.unschedulable}')
  case "$state" in
    "True true"*) found node warn "Node $NODE is cordoned" "The node keeps its running workloads but accepts no new ones. Nodes are usually cordoned for maintenance. The fix uncordons it." fix_uncordon yes ;;
    True*) found node ok "Node $NODE is ready" "Kubernetes reports this node as healthy." ;;
    "") found node warn "Node $NODE is not registered" "The cluster has no node named $NODE." ;;
    *) found node bad "Node $NODE is not ready" "Kubernetes reports this node as not ready. The kubelet or the node network is the usual cause." fix_restart yes ;;
  esac
  down=$(kc get nodes --no-headers | awk '$2 !~ /^Ready/ {print $1}' | tr '\n' ' ' | sed 's/ *$//')
  [ -n "$down" ] && found nodes warn "Nodes not ready: $down" "Run the doctor on each of these nodes. To remove a node that will not return, use Cluster > Remove a host in Homestead."
  broken=$(kc get pods -A --no-headers | awk '$4 ~ /CrashLoopBackOff|Error|ImagePullBackOff|ErrImagePull|CreateContainerConfigError/ {print $1"/"$2" ("$4")"}' | head -n 12)
  if [ -n "$broken" ]; then found pods warn "Failing pods: $(printf '%s\n' "$broken" | wc -l | tr -d ' ')" "The following pods are failing:
$broken

Each pod's log shows the cause (Homestead: Containers > Logs). The fix deletes these pods so that they are recreated." fix_restart_broken no; fi
  failed=$(kc get pods -A --field-selector=status.phase=Failed --no-headers | wc -l | tr -d ' ')
  [ "${failed:-0}" -gt 0 ] && found failed warn "Failed pods: $failed" "Pods that ended in failure, such as evicted pods, remain until deleted. They use no resources. The fix deletes them." fix_failed_pods yes
  stuck=$(kc get pods -A --no-headers | awk '$4=="Terminating" {print $1"/"$2}' | head -n 20)
  if [ -n "$stuck" ]; then found terminating warn "Pods stuck terminating" "The following pods have not stopped:
$stuck

This usually means their node is unavailable. The fix force-deletes them so that their workloads can start on other nodes." fix_terminating no; fi
  if kc get crd volumes.longhorn.io >/dev/null; then
    bad=$(kc -n longhorn-system get volumes.longhorn.io --no-headers -o custom-columns=N:.metadata.name,R:.status.robustness | awk '$2=="faulted" || $2=="degraded" {print $1" ("$2")"}')
    if [ -n "$bad" ]; then found longhorn warn "Longhorn volumes are not healthy" "$bad

Degraded volumes rebuild automatically when a node with capacity is available. Faulted volumes must be restored from a backup (Homestead: Data Protection)."
    else found longhorn ok "Longhorn volumes are healthy" "All volumes have their configured replicas."; fi
  fi
  if [ "$(kc -n kube-system get deploy coredns -o jsonpath='{.status.readyReplicas}')" = "" ] && kc -n kube-system get deploy coredns >/dev/null; then
    found dns bad "CoreDNS is not ready" "Pods cannot resolve service or external names. The fix restarts CoreDNS." fix_dns yes
  fi
  if kc -n lab get deploy homestead >/dev/null; then
    ready=$(kc -n lab get deploy homestead -o jsonpath='{.status.readyReplicas}')
    if [ -n "$ready" ] && [ "$ready" -gt 0 ]; then found homestead ok "Homestead is running" "The Homestead deployment has a ready replica."
    else found homestead bad "Homestead is not running" "The Homestead deployment has no ready replicas. The fix restarts it; if the problem persists, check the pod's events." fix_homestead yes; fi
  fi
  if [ "$KIND" = k3s-server ] && [ -d "$DATA/server/db/etcd" ]; then
    newest=$(ls -t "$DATA"/server/db/snapshots 2>/dev/null | head -n 1)
    if [ -z "$newest" ]; then found snapshot warn "No etcd snapshots found" "k3s takes an etcd snapshot every 12 hours by default. Without a snapshot, the cluster cannot be restored. The fix takes one now." fix_snapshot yes
    else
      age=$(( ($(date +%s) - $(stat -c %Y "$DATA/server/db/snapshots/$newest" 2>/dev/null || date +%s)) / 3600 ))
      if [ "$age" -gt 48 ]; then found snapshot warn "Newest etcd snapshot is $age hours old" "k3s normally takes a snapshot every 12 hours. The fix takes one now." fix_snapshot yes
      else found snapshot ok "Newest etcd snapshot is $age hours old" "$newest"; fi
    fi
  fi
}

run_checks() {
  : > "$FOUND"
  steps="check_service check_disk check_memory check_clock check_runtime check_certs check_iscsi check_cluster"
  if [ "$UI" = text ]; then
    for s in $steps; do
      [ "$DMODE" = menu ] && printf 'Checking %s...\n' "$(printf '%s' "${s#check_}" | tr _ ' ')" > "$TTY" 2>/dev/null
      $s
    done
    return
  fi
  { n=0; for s in $steps; do n=$((n+1)); printf 'XXX\n%d\nChecking %s\nXXX\n' $(( n * 100 / 9 )) "$(printf '%s' "${s#check_}" | tr _ ' ')"; $s; done; } \
    | "$BOX" --title "Node Health" --gauge "Starting checks" 8 70 0 > "$TTY" 2>&1
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
    *) msg "etcd Snapshot" "etcd snapshots can only be taken on a k3s or RKE2 server node." ;;
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
    # Keep the ten most recent etcd snapshots.
    ls -t "$DATA"/server/db/snapshots 2>/dev/null | tail -n +11 | while read -r old; do run rm -f "$DATA/server/db/snapshots/$old"; done
  fi
  after=$(df -Pm /var 2>/dev/null | awk 'NR==2 {print $4}')
  [ -n "$before" ] && [ -n "$after" ] && [ "$DRY" = 0 ] && msg "Clean-up Complete" "$(( after - before )) MB freed in /var."
}

apply_fix() { # id
  line=$(grep "^$1|" "$FOUND" | head -n 1)
  title=$(printf '%s' "$line" | cut -d'|' -f3); detail=$(printf '%s' "$line" | cut -d'|' -f4)
  fix=$(printf '%s' "$line" | cut -d'|' -f5)
  [ -n "$fix" ] || { msg "$title" "$detail

No automatic fix is available for this item."; return; }
  if confirm "$title" "$detail

Apply the fix now?"; then
    logline "fix: $1 ($fix)"
    if $fix; then msg "Fix Applied" "$title: the fix was applied. Run the checks again to confirm."
    else msg "Fix Failed" "The fix did not complete. See $DLOG for details."; fi
  fi
}

# ------------------------------------------------------------------ restore
restore() {
  [ "$KIND" = k3s-server ] || { msg "Restore from Snapshot" "Snapshot restore is available on k3s server nodes. On RKE2, run: rke2 server --cluster-reset --cluster-reset-restore-path=<snapshot>. On Harvester, follow the Harvester documentation."; return; }
  dir="$DATA/server/db/snapshots"
  set --
  for f in $(ls -t "$dir" 2>/dev/null | head -n 15); do set -- "$@" "$f" "$f   $(date -d "@$(stat -c %Y "$dir/$f")" '+%F %H:%M' 2>/dev/null)"; done
  [ $# -gt 0 ] || { msg "Restore from Snapshot" "No etcd snapshots were found on this node."; return; }
  snap=$(menu "Restore from Snapshot" "Select a snapshot. The cluster state (workloads, settings and secrets) is restored to that point. Volume data is not included; Longhorn manages volume data separately." "$@") || return
  servers=$(kc get nodes --no-headers | grep -c 'control-plane' || echo 1)
  typed "Confirm Restore" "k3s will be stopped, the cluster reset to $snap, and k3s started again. All changes made after the snapshot will be lost.$([ "${servers:-1}" -gt 1 ] && printf '\n\nThis cluster has %s server nodes. Before continuing, stop k3s on the other server nodes. Afterwards, delete %s/server/db on each of them and start k3s, so that they rejoin from this node.' "$servers" "$DATA")" RESTORE || { msg "Restore from Snapshot" "Restore cancelled."; return; }
  logline "restore: $snap"
  run systemctl stop k3s
  run k3s server --cluster-reset --cluster-reset-restore-path="$dir/$snap" || { msg "Restore Failed" "The cluster reset did not complete, and k3s is stopped. See journalctl -u k3s for details."; return; }
  run systemctl start k3s
  msg "Restore Complete" "The cluster has been restored to $snap. Run the health checks again once k3s has started."
}

cleanup_menu() {
  choice=$(menu "Clean Up" "Select what to remove:" \
    all "All of the following" \
    images "Unused container images" \
    journal "System journal entries beyond 200 MB" \
    failed "Failed and evicted pods" \
    snapshots "etcd snapshots older than the ten most recent") || return
  case "$choice" in
    all) what="unused container images, journal entries beyond 200 MB, failed pods and older etcd snapshots" ;;
    images) what="unused container images" ;;
    journal) what="journal entries beyond 200 MB" ;;
    failed) what="failed and evicted pods" ;;
    snapshots) what="etcd snapshots older than the ten most recent" ;;
  esac
  confirm "Clean Up" "Remove $what?" || return
  case "$choice" in
    all) fix_cleanup; fix_failed_pods ;;
    images) [ -n "$CRICTL" ] && run $CRICTL rmi --prune ;;
    journal) run journalctl --vacuum-size=200M ;;
    failed) fix_failed_pods ;;
    snapshots) ls -t "$DATA"/server/db/snapshots 2>/dev/null | tail -n +11 | while read -r old; do run rm -f "$DATA/server/db/snapshots/$old"; done ;;
  esac
  msg "Clean Up" "Clean-up complete."
}

mark() { case "$1" in bad) echo "[FAIL]" ;; warn) echo "[WARN]" ;; *) echo "[ OK ]" ;; esac; }
doctor_report() { # the results, most severe first, as text
  for level in bad warn ok; do
    grep "|$level|" "$FOUND" | while IFS='|' read -r id lvl title detail fix safe; do
      printf '%s %s%s\n' "$(mark "$lvl")" "$title" "$([ -n "$fix" ] && [ "$lvl" != ok ] && printf ' (fix available)')"
    done
  done
}
save_report() {
  out="/var/log/homestead-doctor-$(date +%Y%m%d-%H%M).txt"
  { printf 'Homestead node health report: %s (%s), %s\n\n' "$NODE" "$KIND" "$(date)"; doctor_report; } > "$out" \
    && msg "Report Saved" "The health report was saved to $out."
}

# ------------------------------------------------------------------ the doctor
doctor() { # menu | report | fix
  DMODE="$1"
  detect_node
  run_checks
  if [ "$DMODE" = report ]; then
    printf 'Homestead node health report: %s (%s)\n\n' "$NODE" "$KIND"; doctor_report
    grep -q '|bad|' "$FOUND" && exit 2; grep -q '|warn|' "$FOUND" && exit 1; exit 0
  fi
  if [ "$DMODE" = fix ]; then
    printf 'Applying safe fixes on %s (%s)\n' "$NODE" "$KIND"
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
        set -- "$@" "$id" "$(mark "$lvl") $title$([ -n "$fix" ] && [ "$lvl" != ok ] && printf ' (fix available)')"
      done <<EOF
$(grep "|$level|" "$FOUND")
EOF
    done
    bad=$(grep -c '|bad|' "$FOUND"); warn=$(grep -c '|warn|' "$FOUND")
    # A tag starting with "-" reads to whiptail as an option, so the
    # separator's tag is "~".
    set -- "$@" "~" "" \
      "@fix" "Apply all safe fixes" "@again" "Run checks again" "@save" "Save report"
    pick=$(menu "Node Health: $NODE" "$(kind_name): $bad failed, $warn warnings. Select an item for details." "$@") || return 0
    case "$pick" in
      "~") ;;
      @fix)
        if confirm "Apply Safe Fixes" "Apply all fixes marked as safe? These may restart stopped services, enable time synchronisation and iscsid, uncordon this node, delete failed pods, free disk space, and restart CoreDNS or Homestead."; then
          grep -E '\|(bad|warn)\|' "$FOUND" | while IFS='|' read -r id lvl title detail fix safe; do [ -n "$fix" ] && [ "$safe" = yes ] && $fix; done
          run_checks
        fi ;;
      @again) run_checks ;;
      @save) save_report ;;
      *) apply_fix "$pick" ;;
    esac
  done
}

# ------------------------------------------------------------------ installing
do_install() {
  if [ "$(given HS_ROLE)" = harvester ] || { [ -z "$(given HS_ROLE)" ] && is_harvester; }; then
    ROLE=harvester
    flow_harvester
  elif { [ -z "$(given HS_ROLE)" ] || [ "$(given HS_ROLE)" = addons ]; } && { active k3s || active rke2-server; }; then
    ROLE=addons
    active rke2-server && DIST=rke2
    prechecks addons
    add_to_cluster
  elif active k3s-agent || active rke2-agent; then
    msg "Worker Node" "This machine is already a worker node. Run the installer on a server node to install Homestead."
  else
    flow_new
  fi
}

# ------------------------------------------------------------------ start
what_is_here() {
  found="No Kubernetes installation"
  is_harvester && found="Harvester node"
  active k3s && found="k3s server node"
  active k3s-agent && found="k3s worker node"
  { active rke2-server || active rke2-agent; } && ! is_harvester && found="RKE2 node"
  active kubelet && [ "$found" = "No Kubernetes installation" ] && found="Kubernetes node"
  printf '%s' "$found"
}
kind_name() {
  case "$KIND" in
    k3s-server) echo "k3s server node" ;; k3s-agent) echo "k3s worker node" ;;
    rke2-server) echo "RKE2 server node" ;; rke2-agent) echo "RKE2 worker node" ;;
    harvester) echo "Harvester node" ;; kubernetes) echo "Kubernetes node" ;; *) echo "Node" ;;
  esac
}

[ "$(id -u)" = 0 ] || [ "$DRY" = 1 ] || fail "The installer must run as root: curl ... | sudo sh"
banner() { say "Homestead installer ($REF)$([ "$DRY" = 1 ] && printf ', dry run: no changes will be made')"; }
case "$ACTION" in
  install) banner; do_install; exit 0 ;;
  doctor) doctor menu; exit 0 ;;
  report) UI=text; doctor report ;;
  fix-safe) UI=text; doctor fix ;;
esac
# Without a terminal, or with the mode set in the environment: unattended.
if [ -n "$(given HS_ROLE)" ] || ! interactive; then
  banner; do_install; exit 0
fi

while :; do
  detect_node
  here=$(what_is_here)
  if [ "$KIND" != none ] && [ "$here" = "No Kubernetes installation" ]; then here="$(kind_name) (service stopped)"; fi
  cluster_overview
  prepare_overview
  if [ "$KIND" = none ]; then
    set -- install "Install Homestead" \
      checks "Run system checks"
  else
    set -- doctor "Check node health"
    [ "$HOMESTEAD_PRESENT" = no ] && set -- "$@" install "Install Homestead on this cluster"
    [ "$CLUSTER_PAGES" -le 1 ] || set -- "$@" addresses "More member/VIP addresses ($CLUSTER_PAGE/$CLUSTER_PAGES)"
    set -- "$@" details "Cluster details (members, VIPs and versions)"
    set -- "$@" clean "Clean up disk space"
    case "$KIND" in k3s-server|rke2-server) set -- "$@" snapshot "Take an etcd snapshot" restore "Restore from an etcd snapshot" ;; esac
    set -- "$@" report "Save a health report"
  fi
  BACK=Exit
  pick=$(menu "Homestead setup" "$(main_overview)

Select an option:" "$@") || exit 0
  BACK=Back
  case "$pick" in
    addresses) CLUSTER_PAGE=$((CLUSTER_PAGE % CLUSTER_PAGES + 1)) ;;
    details) cluster_details ;;
    install) do_install ;;
    checks) ( prechecks new ) ;;
    doctor) doctor menu ;;
    clean) cleanup_menu ;;
    snapshot) confirm "etcd Snapshot" "Take an etcd snapshot now?" && fix_snapshot && msg "etcd Snapshot" "Snapshot saved." ;;
    restore) restore ;;
    report) DMODE=menu; run_checks; save_report ;;
  esac
done
