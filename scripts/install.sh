#!/bin/sh
# Homestead installer and node doctor.
#
#   curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/install.sh | sudo sh
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
#   HS_ROLE=new|server|agent|tiebreaker|addons|harvester   installation mode
#   --check: only the system checks, for HS_ROLE (default server); installs nothing
#   HS_DIST=k3s|rke2                            distribution for a new cluster
#   HS_NODE_IP=192.0.2.50                     this node's IP address
#   HS_SERVER=192.0.2.50  HS_TOKEN=...        the cluster to join
#   HS_LONGHORN=yes|no  HS_KUBEVIRT=yes|no      components for a new cluster
#   HS_KUBEVIP=yes|no  HS_MULTUS=yes|no         kube-vip and Multus, installed by Homestead (default yes)
#   HS_KUBEVIP_VERSION=0.11.1  HS_MULTUS_VERSION=v4.3.102   their chart versions (default: tested)
#   HS_NODEPROBE=yes|no                         the node probe, installed by Homestead (default yes;
#                                               installing again keeps this cluster's earlier choice)
#   HS_CONSOLE=yes|no                           local host status screen (default yes; native on Harvester)
#   HS_LONGHORN_VOLUME=200|0                    GB for Longhorn's own LVM volume where there is room (0: none)
#   HS_LONGHORN_DATA_PATH=/mnt/longhorn-os       joining: match the cluster's Longhorn default-data-path
#                                              older clusters commonly use /var/lib/longhorn
#   HS_K8S_VERSION=v1.33.4+k3s1                 k3s or RKE2 version
#   HS_LONGHORN_VERSION=v1.9.1  HS_KUBEVIRT_VERSION=v1.6.0  HS_CDI_VERSION=v1.62.0
#   HS_VERSION=2.8.184                          Homestead version
#   HS_VIP=192.0.2.242                        the VIP for Homestead and apps (k3s/RKE2: optional)
#   HS_CLASS=harvester-longhorn                 Harvester: storage class
#   HS_SETUP_CODE=ABCD-EFGH-JK                  the one-time code for the first admin (default: Homestead makes one)
#   HS_YES=1                                    install without the summary
# Versions not set are the current recommended releases.
#
# Options: --install / --doctor   open the installer or the node doctor directly
#          --console / --no-console   enable/disable the local host status screen only
#          --report      run the health checks, print the results and exit
#                        (0 healthy, 1 warnings, 2 failures)
#          --fix-safe    run the health checks and apply every safe fix
#          --uninstall   remove Homestead from this cluster, on a server node: lists
#                        every object by exact name, then asks. Apps are never touched.
#                        With --dry-run: only the list. Stays by default (--keep-data):
#                        the data volume, accounts and settings; --everything removes
#                        them too. HS_NAMESPACE and HS_APPS_NAMESPACE, if yours moved.
#          --dry-run     print the commands instead of running them
#          --text        use plain prompts instead of menus
#          --ref v2.8.176  use the scripts from this release
#          --skip-checks   continue the installation when system checks fail
set -u

RAW="https://raw.githubusercontent.com/homestead-lab/homestead"
REF="${HOMESTEAD_REF:-v2.8.335-dev.12}"
DRY=0
DIST=k3s
ROLE=""
ACTION=""
SKIP_CHECKS=0
EVERYTHING=0
KEEP_DATA=0
UI=""
TTY=/dev/tty
WIKI="https://github.com/homestead-lab/homestead/wiki"
FAILED=0
BACK=Back
MENU_DEFAULT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --install) ACTION=install; shift ;;
    --doctor) ACTION=doctor; shift ;;
    --console) ACTION=console; HS_CONSOLE=yes; shift ;;
    --no-console) ACTION=console; HS_CONSOLE=no; shift ;;
    --report) ACTION=report; UI=text; shift ;;
    --fix-safe) ACTION=fix-safe; UI=text; shift ;;
    --uninstall) ACTION=uninstall; UI=text; shift ;;
    --keep-data) KEEP_DATA=1; shift ;;
    --everything) EVERYTHING=1; shift ;;
    --skip-checks) SKIP_CHECKS=1; shift ;;
    --check) ACTION=check; UI=text; shift ;;
    --text) UI=text; shift ;;
    --ref) [ $# -ge 2 ] || { printf 'Option --ref needs a release, for example --ref %s\n' "$REF" >&2; exit 2; }
      REF="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -u/{ /^set -u/d; p; }' "$0" 2>/dev/null; exit 0 ;;
    *) printf 'Unknown option: %s (see --help)\n' "$1" >&2; exit 2 ;;
  esac
done
if [ "$KEEP_DATA$EVERYTHING" != 00 ] && [ "$ACTION" != uninstall ]; then
  printf 'Options --keep-data and --everything go with --uninstall (see --help)\n' >&2; exit 2
fi
if [ "$KEEP_DATA$EVERYTHING" = 11 ]; then
  printf 'Choose one of --keep-data and --everything (see --help)\n' >&2; exit 2
fi

# Every staged script, manifest and dialog file belongs to this invocation.
WORK_TMP=$(mktemp -d "${TMPDIR:-/tmp}/homestead.XXXXXXXX") || exit 1
chmod 700 "$WORK_TMP" || exit 1
trap '[ -n "$WORK_TMP" ] && [ -d "$WORK_TMP" ] && rm -rf -- "$WORK_TMP"' 0
# An install step runs in the background, where the shell ignores Ctrl+C, so
# it is stopped here; and a secret prompt may have turned echo off.
interrupted() {
  stty echo < "$TTY" 2>/dev/null
  if [ -n "${PROGRESS_JOB:-}" ] && kill -0 "$PROGRESS_JOB" 2>/dev/null; then
    stop_tree "$PROGRESS_JOB"
    printf '\nInstallation interrupted. Some components may already be installed; the log is in %s.\n' "$LOG" >&2
  fi
  exit "$1"
}
stop_tree() { # pid: the process and everything it started
  if command -v pgrep >/dev/null 2>&1; then
    for child in $(pgrep -P "$1" 2>/dev/null); do stop_tree "$child"; done
  fi
  kill -TERM "$1" 2>/dev/null
}
PROGRESS_JOB=""
LOG=/var/log/homestead-install.log
trap 'interrupted 130' INT
trap 'interrupted 143' HUP TERM

# ------------------------------------------------------------------ output
say() { printf '\n==> %s\n' "$*"; }
# The cluster token is an argument to the bootstrap script, so every command
# line that is logged or shown has it replaced.
SECRET=""
hide() {
  rest="$1"; shown=""
  if [ -n "$SECRET" ]; then
    while :; do
      case "$rest" in
        *"$SECRET"*) shown="$shown${rest%%"$SECRET"*}[token hidden]"; rest="${rest#*"$SECRET"}" ;;
        *) break ;;
      esac
    done
  fi
  printf '%s' "$shown$rest"
}
private_log() { # path: created, or kept, readable by root only
  [ -e "$1" ] || ( umask 077; : >> "$1" ) 2>/dev/null
  chmod 600 "$1" 2>/dev/null || true
}
fail() { printf '\nError: %s\n' "$*" >&2; exit 1; }
run() {
  # Every change goes through here, so --dry-run shows it instead.
  line=$(hide "$*")
  private_log /var/log/homestead-doctor.log
  { printf '%s run: %s\n' "$(date '+%F %T' 2>/dev/null)" "$line" >> /var/log/homestead-doctor.log; } 2>/dev/null || true
  if [ "$DRY" = 1 ]; then printf '+ %s\n' "$line"; else "$@"; fi
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
  case "$ACTION" in report|fix-safe|uninstall) return 0 ;; esac
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
    while :; do
      printf 'Select 1-%d [1]: ' $(( $# / 2 )) > "$TTY"
      read -r reply < "$TTY" || fail "No selection was made."
      [ -z "$reply" ] && reply=1
      i=0; for word in "$@"; do i=$((i+1)); if [ $((i % 2)) = 1 ] && [ $(( (i + 1) / 2 )) = "$reply" ]; then printf '%s' "$word"; return; fi; done
      printf '%s is not one of the choices.\n' "$reply" > "$TTY"
    done
  fi
  # Leaving one of these questions ends the installation: the button says so.
  BACK=Cancel
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
  case "$1" in server|tiebreaker) small_server_checks ;; esac
  if [ "$ACTION" = check ]; then
    msg "System Checks" "Results for $(hostname) as a $1:
$REPORT"
    exit "$FAILED"
  fi
  if [ "$FAILED" = 1 ] && [ "$SKIP_CHECKS" = 0 ]; then
    msg "System Checks Failed" "Resolve the following before installing:
$REPORT"
    fail "Installation stopped: system checks failed."
  fi
  msg "System Checks" "Results for $(hostname):
$REPORT"
}

# ------------------------------------------------------------------ a small server
# A server - a tiebreaker most of all - keeps etcd's data, which writes and
# syncs to disk on every change: a slow card makes the whole cluster slow or
# unstable. These run for the server and tiebreaker roles (#364).
fio_p99_ms() { # fio's normal output on stdin -> the 99th percentile of its syncs, in whole ms
  awk '/sync percentiles/ { unit = $3; on = 1; next }
       on && /99\.00th=/ { v = $0; sub(/.*99\.00th=\[ */, "", v); sub(/\].*/, "", v)
         d = unit ~ /nsec/ ? 1000000 : unit ~ /usec/ ? 1000 : 1; printf "%d\n", v / d; exit }'
}
dd_avg_ms() { # dd's stderr on stdin, writes -> the average time a synced write took, in whole ms
  awk -v n="$1" '/copied/ { for (i = 1; i <= NF; i++) if ($i ~ /^s,?$/) { printf "%d\n", $(i - 1) * 1000 / n; exit } }'
}
etcd_sync_ms() {
  # Where k3s will keep its data if it is there already; never made here, so a
  # dry run changes nothing. The test file is removed afterwards.
  dir=/var/lib/rancher; [ -d "$dir" ] && [ -w "$dir" ] || dir=/var/tmp
  if have fio; then
    # The C locale: a decimal comma or translated words would misread the time.
    LC_ALL=C fio --name=homestead-etcd-check --directory="$dir" --rw=write --ioengine=sync --fdatasync=1 \
      --size=22m --bs=2300 2>/dev/null | fio_p99_ms
  else
    LC_ALL=C dd if=/dev/zero of="$dir/homestead-etcd-check" bs=4k count=200 oflag=dsync 2>&1 >/dev/null | dd_avg_ms 200
  fi
  rm -f "$dir"/homestead-etcd-check*
}
cgroup_memory() {
  grep -qw memory /sys/fs/cgroup/cgroup.controllers 2>/dev/null \
    || awk '$1 == "memory" && $4 == 1 { found = 1 } END { exit !found }' /proc/cgroups 2>/dev/null
}
vxlan_available() { [ -d /sys/module/vxlan ] || modprobe -n vxlan >/dev/null 2>&1; }
swap_on() { [ -n "$(swapon --show --noheadings 2>/dev/null)" ] || awk 'NR > 1 { found = 1 } END { exit !found }' /proc/swaps 2>/dev/null; }
is_pi() { grep -q 'Raspberry Pi' /proc/device-tree/model 2>/dev/null; }
pi_throttled() { have vcgencmd && vcgencmd get_throttled 2>/dev/null | sed -n 's/^throttled=//p'; }

small_server_checks() {
  ms=$(etcd_sync_ms)
  if [ -z "$ms" ]; then check warn "Disk sync time could not be measured. etcd needs a disk that syncs in under 10 ms: an SSD, not a microSD card."
  elif [ "$ms" -gt 10 ]; then check warn "Disk syncs in $ms ms. etcd needs under 10 ms; on this disk the cluster may be slow or unstable. Use an SSD."
  else check pass "Disk syncs in $ms ms, fast enough for etcd"; fi
  if cgroup_memory; then check pass "The memory cgroup controller is on"
  elif is_pi; then check fail "The memory cgroup controller is off; k3s will not run. Add cgroup_enable=memory cgroup_memory=1 to /boot/firmware/cmdline.txt and reboot."
  else check fail "The memory cgroup controller is off; k3s will not run. Enable it on the kernel command line and reboot."; fi
  vxlan_available && check pass "The vxlan kernel module is available" \
    || check fail "The vxlan kernel module is missing; the cluster network (flannel) needs it. Install your kernel's extra modules."
  swap_on && check warn "Swap is on. Kubernetes runs best with swap off: swapoff -a, and remove it from /etc/fstab." \
    || check pass "Swap is off"
  if is_pi; then
    t=$(pi_throttled)
    case "$t" in
      ""|0x0) check pass "No under-voltage or throttling reported" ;;
      *) words=$(throttle_words "$t")
        case "$words" in
          *under-voltage*) advice="Use the official power supply" ;;
          *temperature*|*capped*) advice="Give it better cooling" ;;
          *) advice="Check its power and cooling" ;;
        esac
        check warn "This Raspberry Pi reports $t: ${words:-unknown flags}. $advice; a vote that drops out puts the cluster at risk." ;;
    esac
  fi
}
throttle_words() { # vcgencmd get_throttled bits, in words
  v=$(printf '%d' "$1" 2>/dev/null || echo 0); words=""
  [ $((v & 0x1)) -ne 0 ] && words="$words, under-voltage now"
  [ $((v & 0x2)) -ne 0 ] && words="$words, frequency capped now"
  [ $((v & 0x4)) -ne 0 ] && words="$words, throttled now"
  [ $((v & 0x8)) -ne 0 ] && words="$words, soft temperature limit now"
  [ $((v & 0x10000)) -ne 0 ] && words="$words, under-voltage since boot"
  [ $((v & 0x20000)) -ne 0 ] && words="$words, frequency capped since boot"
  [ $((v & 0x40000)) -ne 0 ] && words="$words, throttled since boot"
  [ $((v & 0x80000)) -ne 0 ] && words="$words, soft temperature limit since boot"
  printf '%s' "${words#, }"
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
VCACHE="$WORK_TMP/releases"

fetch() { curl -sfL --max-time 15 "$1" 2>/dev/null; }
newest_first() { sed 's/^v//' | sort -t. -k1,1nr -k2,2nr -k3,3nr | sed 's/^/v/'; }
cached() { # key command... -> the command's output, retrieved once per run
  mkdir -p "$VCACHE" 2>/dev/null
  f="$VCACHE/$1"; shift
  [ -s "$f" ] || "$@" > "$f" 2>/dev/null
  cat "$f" 2>/dev/null
}
channels() { # k3s|rke2 -> "channel version" lines, releases only
  # Asked a few times first: its outages are brief, and the fallback below
  # can name a different "stable" than a host joining later would get.
  for try in 1 2 3 4; do
    body=$(curl -sfL --connect-timeout 5 --max-time 15 "https://update.$1.io/v1-release/channels" 2>/dev/null); code=$?
    found=$(printf '%s' "$body" | tr -d '\n' \
      | grep -o '"name":"[^"]*","latest":"[^"]*"' \
      | sed 's/"name":"\([^"]*\)","latest":"\([^"]*\)"/\1 \2/' \
      | grep -v '^testing ' | grep -Ev ' v[0-9.]+-')
    if [ -n "$found" ]; then printf '%s\n' "$found"; return; fi
    # Not reached at all - since October 2026 it takes no connections - so
    # GitHub at once, rather than a minute and a half waiting on it.
    case "$code" in 6|7|28) break ;; esac
    [ "$try" = 4 ] || sleep 10
  done
  # The update server is down (update.rke2.io has answered 404, then taken no
  # connections): the project's own releases stand in - the newest as stable,
  # and the newest of each minor.
  repo=k3s-io/k3s; [ "$1" = rke2 ] && repo=rancher/rke2
  fetch "https://api.github.com/repos/$repo/releases?per_page=60" | grep -o '"tag_name": *"[^"]*"' \
    | sed 's/.*"\([^"]*\)"$/\1/' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+\+(k3s|rke2r)[0-9]+$' \
    | sort -t. -k1,1V -k2,2nr -k3,3nr | awk -F. '
      NR == 1 { print "stable " $0 }
      !seen[$1 "." $2]++ { print $1 "." $2 " " $0 }'
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
    homestead) cached homestead releases homestead-lab/homestead | head -n 1 | sed 's/^v//' ;;
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
    homestead) list=$(cached homestead releases homestead-lab/homestead); rec="v$rec" ;;
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
  while :; do
    VIP=$(ask HS_VIP "Homestead and Apps Address" "Enter an unused address on your LAN, outside your router's DHCP range, for Homestead and your apps (for example, 192.0.2.200). It is a VIP: it moves to another node if one goes down.

Homestead will be at http://VIP:8088, as well as on each node's address, and apps share the VIP on their own ports. Its backup storage and network shares go there too.

Leave it empty to use the nodes' own addresses for now; add a VIP later under Networking." "") || exit
    [ -n "$VIP" ] || return 0
    problem=""
    printf '%s' "$VIP" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || problem="'$VIP' is not a valid IPv4 address."
    [ -z "$problem" ] && [ "$VIP" = "${NODE_IP:-}" ] && problem="$VIP is this machine's own address; a VIP must be an unused one."
    [ -n "$problem" ] || break
    [ -z "$(given HS_VIP)" ] || fail "$problem"
    msg "Homestead and Apps Address" "$problem"
  done
  if [ "$DRY" = 0 ] && ping -c 1 -W 1 "$VIP" >/dev/null 2>&1; then
    yesno HS_TAKEN "Address In Use" "A device already responds at $VIP. Use this address anyway?" no || fail "Installation cancelled. Choose an unused address."
  fi
}
vip_args() { [ -n "${VIP:-}" ] && printf -- ' --vip %s' "$VIP"; true; }
homestead_url() { if [ -n "${VIP:-}" ]; then echo "http://$VIP:8088 (and http://$1:8088)"; else echo "http://$1:8088"; fi; }

network_args() {
  case "$(given HS_KUBEVIP)" in n*|N*|0|false) printf ' --no-kube-vip' ;; esac
  case "$(given HS_MULTUS)" in n*|N*|0|false) printf ' --no-multus' ;; esac
  case "${NODEPROBE:-$(given HS_NODEPROBE)}" in n*|N*|0|false) printf ' --no-node-probe' ;; ?*) printf ' --node-probe' ;; esac
  v=$(given HS_KUBEVIP_VERSION); [ -n "$v" ] && printf ' --kube-vip-version %s' "$v"
  v=$(given HS_MULTUS_VERSION); [ -n "$v" ] && printf ' --multus-version %s' "$v"
  true
}

# The node probe (#1279). homestead-install outlives a reinstall that keeps
# Homestead's data, so on a running cluster its node-probe is this cluster's
# earlier choice: offered as the default, and kept when nobody says otherwise
# (NODEPROBE stays empty; bootstrap-k3s.sh keeps the ConfigMap's value).
pick_node_probe() { # [kubectl...] for a running cluster
  NODEPROBE=""; PROBE_KEPT=""
  if [ $# -gt 0 ]; then
    # A failed read is never taken as "no earlier choice": that would offer Yes.
    if kept=$("$@" -n lab get configmap homestead-install --ignore-not-found -o jsonpath='{.data.node-probe}' 2>/dev/null); then
      case "$(printf '%s' "$kept" | tr '[:upper:]' '[:lower:]')" in
        no|false|0|skip) PROBE_KEPT=no ;;
        ?*) PROBE_KEPT=yes ;;
      esac
    elif [ -z "$(given HS_NODEPROBE)" ]; then
      fail "Could not read homestead-install to keep this cluster's node probe choice. Run again, or set HS_NODEPROBE=yes or HS_NODEPROBE=no."
    fi
  fi
  if [ -n "$(given HS_NODEPROBE)" ] || interactive; then
    if yesno HS_NODEPROBE "Node Probe" "Install the node probe on every node?

It reports temperatures, disk SMART health and each node's network interfaces to Homestead, and keeps kube-vip announcing on the right interface as nodes join. It runs a privileged container on each node to read them.$([ "$PROBE_KEPT" = no ] && printf '\n\nThis cluster was installed without it, so No is the default.')" "${PROBE_KEPT:-yes}"
    then NODEPROBE=yes; else NODEPROBE=no; fi
  fi
}
node_probe_word() { printf '%s' "${NODEPROBE:-${PROBE_KEPT:-yes}}"; }
node_probe_line() {
  if [ "$(node_probe_word)" = yes ]; then printf 'Installed by Homestead'; else printf 'Not installed'; fi
  if [ -n "${PROBE_KEPT:-}" ] && [ -z "$(given HS_NODEPROBE)" ] && [ "$(node_probe_word)" = "$PROBE_KEPT" ]; then printf ' (kept from this cluster)'; fi
  true
}

# ------------------------------------------------------------------ progress

# A long step, run with a progress bar that follows its "==> " stages. The
# full output goes to $LOG and is shown if the step fails. Without menus the
# output is printed as it runs.
progress() { # title stages command...
  title="$1"; stages="$2"; shift 2
  if [ "$UI" = text ] || [ "$DRY" = 1 ]; then run "$@"; return; fi
  private_log "$LOG"
  : > "$LOG" 2>/dev/null || LOG="$WORK_TMP/install.log"
  status="$WORK_TMP/install.status"
  rm -f "$status"
  { "$@" > "$LOG" 2>&1 < /dev/null; echo $? > "$status"; } &
  job=$!; PROGRESS_JOB=$job
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
  PROGRESS_JOB=""
  code=$(cat "$status" 2>/dev/null || echo 1)
  if [ "$code" != 0 ]; then
    tail -n 40 "$LOG" > "$WORK_TMP/install.tail" 2>/dev/null
    "$BOX" --title "Installation Failed" --scrolltext --textbox "$WORK_TMP/install.tail" 24 100 < "$TTY" > "$TTY" 2>&1
    fail "Installation failed. The full log is in $LOG."
  fi
}

# ------------------------------------------------------------------ bootstrap
bootstrap() { # stages, then args for bootstrap-k3s.sh
  stages="$1"; shift
  script="$WORK_TMP/bootstrap-k3s.sh"
  run curl -sfL --connect-timeout 10 --max-time 120 "$RAW/$REF/scripts/bootstrap-k3s.sh" -o "$script" || fail "Could not download bootstrap-k3s.sh."
  progress "Installing" "$stages" sh "$script" "$@" || fail "Installation failed. See the output above."
}

# ------------------------------------------------------------------ flows
pick_console() {
  CONSOLE=yes
  [ -n "$(given HS_CONSOLE)" ] || interactive || return 0
  yesno HS_CONSOLE "Host Console" "Show a live status screen on this host's first local console?

It shows cluster status, node addresses, CPU, RAM and disk usage. Enter, Q or Escape opens the normal authenticated login prompt. SSH and other consoles stay available. Enabled by default; takes effect on the next boot or logout." yes || CONSOLE=no
}

console_line() { [ "${CONSOLE:-yes}" = yes ] && echo "Status screen on tty1 (exit to login)" || echo "Normal login prompt"; }

install_console() {
  # Use a private directory: the installer runs as root on multi-user hosts.
  if [ "$DRY" = 1 ]; then console_tmp="$WORK_TMP/console-dry-run"
  else console_tmp=$(mktemp -d) || return 1; fi
  console_action=enable
  [ "${CONSOLE:-yes}" = yes ] || console_action=disable
  console_ok=yes
  run curl -sfL --connect-timeout 10 --max-time 60 "$RAW/$REF/scripts/install-console.sh" -o "$console_tmp/install-console.sh" || console_ok=no
  if [ "$console_action" = enable ]; then
    run curl -sfL --connect-timeout 10 --max-time 60 "$RAW/$REF/scripts/host-console.py" -o "$console_tmp/host-console.py" || console_ok=no
  fi
  if [ "$console_ok" = yes ]; then
    run sh "$console_tmp/install-console.sh" "$console_action" "$console_tmp/host-console.py" || console_ok=no
  fi
  [ "$DRY" = 1 ] || rm -rf "$console_tmp"
  if [ "$console_ok" != yes ]; then
    msg "Host Console" "The host console could not be configured. Retry with: sudo sh install.sh --console"
    return 1
  fi
}

configure_console() {
  if is_harvester; then msg "Host Console" "Harvester provides its own host status console."; return; fi
  pick_console
  install_console
}

flow_new() {
  ROLE=$(choose HS_ROLE "Installation Mode" "Select how to install this machine:" \
    new "Create a new cluster" \
    server "Join an existing cluster as a server node" \
    tiebreaker "Join an existing cluster as a tiebreaker (an etcd vote only)" \
    agent "Join an existing cluster as a worker node") || exit
  [ "$ROLE" = harvester ] && fail "This machine is not a Harvester node."
  case "$ROLE" in
    new) pick_dist ;;
    server|agent|tiebreaker) find_cluster ;;
  esac
  pick_ip
  prechecks "$ROLE"
  case "$ROLE" in
    new) new_cluster ;;
    server|agent|tiebreaker) join_cluster "$ROLE" ;;
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
  NODE_IP=$(choose HS_NODE_IP "Node IP Address" "Select the IP address that other nodes use to reach this machine. The node is registered with this address." "$@") || exit
}

# The distribution for a new cluster. Unattended, k3s unless HS_DIST is set.
pick_dist() {
  if [ -z "$(given HS_DIST)" ] && ! interactive; then DIST=k3s; return; fi
  DIST=$(choose HS_DIST "Kubernetes Distribution" "Select the Kubernetes distribution for the new cluster. Homestead supports both." \
    k3s "k3s    Lightweight Kubernetes; suited to small machines" \
    rke2 "RKE2   Hardened upstream Kubernetes, as used by Harvester") || exit
  case "$DIST" in k3s|rke2) ;; *) fail "Unsupported distribution: $DIST. Set HS_DIST to k3s or rke2." ;; esac
}

# The cluster to join: its address and distribution. An RKE2 server answers
# on port 9345, a k3s server on port 6443.
find_cluster() {
  server=$(ask HS_SERVER "Join Cluster" "Enter the IP address or hostname of an existing server node - the machine's own address, not the VIP Homestead and apps use (that one carries apps, not the cluster):" "") || exit
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
  mountpoint -q /mnt/longhorn-os 2>/dev/null && return 0
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
  while :; do
    size=$(ask HS_LONGHORN_VOLUME "Longhorn Volume" "This machine's system is on LVM, and $1 has $2 GB free beyond $3 GB kept for the system.

Longhorn replica data can have a volume of its own at ${LH_DATA_PATH:-/mnt/longhorn-os}, so replicas cannot fill the system's filesystem. New clusters keep runtime files on the OS at /var/lib/longhorn. Space left out stays free for the system or Longhorn's V2 engine (Hosts > Storage > Use free space).

Size in GB (0 keeps Longhorn on the system's filesystem):" "$2") || exit
    case "$size" in ''|*[!0-9]*) ;; *) break ;; esac
    [ -z "$given_size" ] || fail "The Longhorn volume size must be a number of GB."
    msg "Longhorn Volume" "The size must be a number of GB."
  done
  if [ "$size" = 0 ]; then LH_VOLUME=none; LH_LINE="on the system's filesystem"
  else [ "$size" -gt "$2" ] && size=$2; LH_VOLUME=$size; LH_LINE="$1/longhorn, $size GB of its own"; fi
}
volume_args() {
  [ -n "${LH_VOLUME:-}" ] && printf -- ' --longhorn-volume %s' "$LH_VOLUME"
  [ -n "${LH_DATA_PATH:-}" ] && printf -- ' --longhorn-data-path %s' "$LH_DATA_PATH"
  true
}

pick_longhorn_path() {
  if [ "$DIST" = rke2 ]; then path_command='/var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml'
  else path_command='k3s kubectl'; fi
  while :; do
    LH_DATA_PATH=$(ask HS_LONGHORN_DATA_PATH "Match the Cluster's Storage" "This host must use the storage folder already set by your cluster. Choosing a different folder could put app and VM data on the OS filesystem instead of the space reserved for storage.

Find it in Homestead: Cluster > Add a host > Storage setting for the installer.

Or run this on an existing server:
sudo $path_command -n longhorn-system get settings.longhorn.io default-data-path -o jsonpath='{.value}'

Paste the exact folder below. Do not choose a new one:" "") || exit
    case "$LH_DATA_PATH" in /mnt/longhorn-os|/var/lib/longhorn) break ;; esac
    [ -z "$(given HS_LONGHORN_DATA_PATH)" ] || fail "Storage setting not supported. Copy the cluster's default-data-path into HS_LONGHORN_DATA_PATH. Custom storage folders need manual setup; no storage has been changed."
    msg "Check the Storage Folder" "Copy only the folder shown in Homestead or by the command, without quotes or spaces. Custom storage folders need manual setup: choose Cancel on the next screen to leave the installer. No storage has been changed."
  done
}

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
  [ "$longhorn" = yes ] && pick_longhorn_volume
  # Unattended, it is installed unless HS_NODEPROBE says no, as kube-vip and
  # Multus are. No cluster to ask yet: bootstrap-k3s.sh asks once it is up.
  # shellcheck disable=SC2046
  if $(kube_cmd) get nodes >/dev/null 2>&1; then pick_node_probe $(kube_cmd); else pick_node_probe; fi
  yesno HS_KUBEVIRT "Virtual Machines" "Install KubeVirt and CDI to run virtual machines alongside containers?$(kvm || printf '\n\nHardware virtualisation is not available on this machine. Virtual machines would run in emulation mode, with reduced performance.')" "$kubevirt" && kubevirt=yes || kubevirt=no
  pick_vip
  pick_console
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
  Node probe           $(node_probe_line)
  Host console         $(console_line)
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
  install_console
  finish_new
}

join_cluster() { # server|agent|tiebreaker
  token=$(ask HS_TOKEN "Cluster Token" "Enter the cluster token. To display it, run this command on an existing server node:

  sudo cat $(token_file)" "" secret) || exit
  [ -n "$token" ] || fail "No cluster token was entered."
  SECRET="$token"
  case "$1" in server|tiebreaker) mode=join ;; *) mode=agent ;; esac
  # A tiebreaker holds an etcd vote and no Longhorn data (#364).
  if [ "$1" = tiebreaker ]; then LH_VOLUME=""; LH_LINE=""; LH_DATA_PATH=""
  else pick_longhorn_path; pick_longhorn_volume; fi
  pick_console
  summary "Review the settings below. Select a component to change its version, or select Install to begin.

  Installation mode    Join an existing cluster as a $(case "$1" in server) echo "server node" ;; tiebreaker) echo "tiebreaker (an etcd vote only)" ;; *) echo "worker node" ;; esac)
  Distribution         $(dist_name)
  Cluster              $url
  Host console         $(console_line)
  Node name            $(hostname)
  Node IP address      $NODE_IP${LH_LINE:+
  Longhorn data        $LH_LINE}
  Storage path         ${LH_DATA_PATH:-Not used}

Longhorn host packages (open-iscsi, NFS client) are installed first." k8s
  # shellcheck disable=SC2046
  bootstrap 3 "$mode" "$url" "$token" --node-ip "$NODE_IP" $([ "$1" = tiebreaker ] && echo --tiebreaker) $(dist_flag) $(version_args k8s)$(volume_args)
  install_console
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
  # shellcheck disable=SC2086
  pick_node_probe $kcmd
  pick_vip
  pick_console
  # shellcheck disable=SC2086
  summary "Review the settings below. Select a component to change its version, or select Install to begin.

  Installation mode    Install on this $(dist_name) cluster
  Host console         $(console_line)
  Storage              $storage
  Networking           $(network_line)
  Node probe           $(node_probe_line)
  Apps VIP             $([ -n "${VIP:-}" ] && echo "$VIP - Homestead, its storage and shares, and apps" || echo "None yet - the nodes' own addresses")
  Homestead URL        $(homestead_url "$(default_ip)")" $comps
  # shellcheck disable=SC2086,SC2046
  bootstrap 5 addons $(dist_flag) $(version_args $comps) $(network_args)$(vip_args)
  install_console
  finish_new
}

finish_new() {
  ip="${NODE_IP:-$(default_ip)}"
  # shellcheck disable=SC2046
  apply_setup_code $(kube_cmd)
  # shellcheck disable=SC2046
  msg "Installation Complete" "Homestead is available at:

  http://$ip:8088$([ -n "${VIP:-}" ] && printf '\n  http://%s:8088 - once kube-vip is up, a minute or two after Homestead' "$VIP")

Open this address to create the administrator account. It asks for the one-time setup code.
$(setup_code_line $(kube_cmd))

To add a node, run the installer on the new machine and select Join an existing cluster. Give it this server's own address ($ip)$([ -n "${VIP:-}" ] && printf ', not the apps VIP (%s): the VIP carries apps, not the cluster' "$VIP") - and the cluster token, which this command displays:

  sudo cat $(token_file)

kube-vip, Multus$([ "$(node_probe_word)" = no ] || printf ' and the node probe') are installed by Homestead after it starts; their progress is shown in its job tray. Anything left out can be added under Settings > Cluster > Add-ons."
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
  while :; do
    vip=$(ask HS_VIP "Homestead IP Address" "Harvester detected. Enter an unused IP address for Homestead on your LAN, outside the DHCP range (for example, 192.0.2.242):" "") || exit
    printf '%s' "$vip" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' && break
    [ -z "$(given HS_VIP)" ] || fail "'$vip' is not a valid IPv4 address."
    msg "Homestead IP Address" "'$vip' is not a valid IPv4 address."
  done
  if [ "$DRY" = 0 ] && ping -c 1 -W 1 "$vip" >/dev/null 2>&1; then
    yesno HS_TAKEN "Address In Use" "A device already responds at $vip. Use this address anyway?" no || fail "Installation cancelled. Choose an unused address."
  fi
  set --
  for c in $($KC get storageclass -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do set -- "$@" "$c" "$c"; done
  [ $# -gt 0 ] || set -- harvester-longhorn "harvester-longhorn"
  class=$(choose HS_CLASS "Storage Class" "Select the storage class for Homestead's data. harvester-longhorn is the Harvester default." "$@") || exit
  summary "Review the settings below. Select a component to change its version, or select Install to begin.

  Installation mode    Install on this Harvester cluster
  Homestead URL        http://$vip:8088
  Storage class        $class
  Namespace            lab" homestead
  say "Installing Homestead"
  manifest="$WORK_TMP/deploy.yaml"
  ref="$REF"; [ -n "$HS_RELEASE" ] && ref="v$HS_RELEASE"
  run curl -sfL --connect-timeout 10 --max-time 120 "$RAW/$ref/deploy/deploy.yaml" -o "$manifest" || fail "Could not download the Homestead manifest."
  # The class only on its two lines, for this manifest's longhorn and an older
  # release's longhorn-r2 (#731); the same expressions as bootstrap-k3s.sh.
  run sed -i -e "s/192\\.0\\.2\\.242/$vip/g" \
    -e "s/storageClassName: longhorn\(-r2\)\{0,1\}$/storageClassName: $class/" \
    -e "s/value: \"longhorn\(-r2\)\{0,1\}\"$/value: \"$class\"/" \
    -e "s/accessModes: \\[ReadWriteMany\\]/accessModes: [ReadWriteOnce]/" "$manifest"
  # shellcheck disable=SC2086
  run $KC apply -f "$manifest" || fail "kubectl could not apply the Homestead manifest."
  # shellcheck disable=SC2086
  progress "Installing" 1 sh -c "echo '==> Waiting for Homestead to start'; $KC -n lab rollout status deployment/homestead --timeout=10m" \
    || fail "Homestead did not start. Run: $KC -n lab get pods"
  # shellcheck disable=SC2086
  apply_setup_code $KC
  # shellcheck disable=SC2086
  msg "Installation Complete" "Homestead is available at:

  http://$vip:8088

Open this address to create the administrator account. It asks for the one-time setup code.
$(setup_code_line $KC)

Next steps: reserve addresses for applications under Networking > Your VIPs, and install the node probe from Settings > Cluster > Add-ons. See $WIKI/Installing-on-Harvester"
}

# ------------------------------------------------------------------ doctor prompts
DLOG=/var/log/homestead-doctor.log
DMODE=menu
FOUND="$WORK_TMP/doctor"
: > "$FOUND"
logline() { private_log "$DLOG"; { printf '%s %s\n' "$(date '+%F %T' 2>/dev/null)" "$*" >> "$DLOG"; } 2>/dev/null || true; }
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
unit_exists() { systemctl list-unit-files "$1" 2>/dev/null | grep -q "^$1"; }
detect_node() {
  KIND=none; SERVICE=""; DATA=""; KC=""
  if grep -qi harvester /etc/os-release 2>/dev/null || [ -f /etc/rancher/rancherd/config.yaml ]; then KIND=harvester; fi
  # RKE2 installs both rke2-server and rke2-agent units on every host and
  # enables one: the running unit decides, then the enabled one, then any.
  for test in "systemctl is-active --quiet" "systemctl is-enabled --quiet" unit_exists; do
    for s in k3s k3s-agent rke2-server rke2-agent kubelet; do
      if $test "$s.service" 2>/dev/null; then SERVICE=$s; break 2; fi
    done
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
  # RKE2's crictl (and a plain one on a k3s host) needs the distribution's
  # config to find containerd at /run/k3s/containerd; without it every
  # check called the runtime dead, and its fix restarted the node's service.
  for f in "$DATA/agent/etc/crictl.yaml" /var/lib/rancher/rke2/agent/etc/crictl.yaml; do
    [ -n "$CRICTL" ] && [ -z "${CRI_CONFIG_FILE:-}" ] && [ -r "$f" ] && export CRI_CONFIG_FILE="$f"
  done
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
    # On one host, a volume Longhorn cannot place more copies of reads
    # degraded for good: it is short of hosts, not rebuilding (#841). One
    # host is one Kubernetes node and one Longhorn node record, as Homestead counts it.
    one=0
    [ "$(kc get nodes --no-headers | wc -l | tr -d ' ')" = 1 ] &&
      [ "$(kc -n longhorn-system get nodes.longhorn.io --no-headers | wc -l | tr -d ' ')" = 1 ] && one=1
    bad=$(kc -n longhorn-system get volumes.longhorn.io --no-headers -o 'custom-columns=N:.metadata.name,R:.status.robustness,S:.status.conditions[?(@.type=="Scheduled")].reason' |
      awk -v one="$one" '$2=="faulted" {print $1" (faulted)"} $2=="degraded" {print $1 (one==1 && $3=="ReplicaSchedulingFailure" ? " (degraded: more copies than hosts)" : " (degraded)")}')
    if [ -n "$bad" ]; then
      why=""
      case "$bad" in *"more copies than hosts"*) why="This cluster has one host, so Longhorn keeps one copy of each volume, and a volume that asks for more reads degraded. In Homestead, an admin can match the copies to the host count: Cluster > Health > Match copies to hosts." ;; esac
      case "$bad" in *"(degraded)"*) why="${why:+$why }Degraded volumes rebuild automatically when a node with capacity is available." ;; esac
      case "$bad" in *"(faulted)"*) why="${why:+$why }Faulted volumes must be restored from a backup (Homestead: Data Protection)." ;; esac
      title="Longhorn volumes ask for more copies than this host can hold"
      printf '%s\n' "$bad" | grep -qv "more copies than hosts" && title="Longhorn volumes are not healthy"
      found longhorn warn "$title" "$bad

$why"
    else found longhorn ok "Longhorn volumes are healthy" "All volumes have their configured replicas."; fi
  fi
  for dns in coredns rke2-coredns-rke2-coredns; do
    kc -n kube-system get deploy "$dns" >/dev/null || continue
    if [ "$(kc -n kube-system get deploy "$dns" -o jsonpath='{.status.readyReplicas}')" = "" ]; then
      found dns bad "CoreDNS is not ready" "Pods cannot resolve service or external names. The fix restarts CoreDNS." "fix_dns $dns" yes
    fi
    break
  done
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
  total=$(printf '%s\n' $steps | wc -l)
  { n=0; for s in $steps; do printf 'XXX\n%d\nChecking %s\nXXX\n' $(( n * 100 / total )) "$(printf '%s' "${s#check_}" | tr _ ' ')"; $s; n=$((n+1)); done; } \
    | "$BOX" --title "Node Health" --gauge "Starting checks" 8 70 0 > "$TTY" 2>&1
}

# ------------------------------------------------------------------ fixes
fix_restart() { run systemctl restart "$SERVICE" && sleep 5; }
fix_iscsid() { run systemctl enable --now iscsid; }
fix_clock() {
  # Ubuntu names chrony's unit chrony.service, with chronyd.service only an
  # alias, which systemctl refuses to enable; Red Hat names it chronyd.
  for unit in chrony chronyd; do
    if systemctl list-unit-files "$unit.service" 2>/dev/null | grep -Eq "^$unit\.service +(enabled|disabled|static|indirect)"; then
      run systemctl enable --now "$unit"; return
    fi
  done
  run timedatectl set-ntp true
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
fix_dns() { run $KC -n kube-system rollout restart "deployment/${1:-coredns}"; }
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
  # The API is often down when a restore is needed: then the count is unknown,
  # and the warning about other server nodes is given anyway.
  if nodes=$(kc get nodes --no-headers); then
    servers=$(printf '%s\n' "$nodes" | grep -c 'control-plane'); servers_known=yes
  else servers=0; servers_known=no; fi
  others=""
  if [ "$servers_known" = no ]; then
    others="The cluster could not be asked how many server nodes it has. If it has others, stop k3s on them before continuing. Afterwards, delete $DATA/server/db on each of them and start k3s, so that they rejoin from this node."
  elif [ "$servers" -gt 1 ]; then
    others="This cluster has $servers server nodes. Before continuing, stop k3s on the other server nodes. Afterwards, delete $DATA/server/db on each of them and start k3s, so that they rejoin from this node."
  fi
  typed "Confirm Restore" "k3s will be stopped, the cluster reset to $snap, and k3s started again. All changes made after the snapshot will be lost.${others:+

$others}" RESTORE || { msg "Restore from Snapshot" "Restore cancelled."; return; }
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

safe_fixes() { # [verbose]: every safe fix found, each run once
  applied=" "
  grep -E '\|(bad|warn)\|' "$FOUND" | while IFS='|' read -r id lvl title detail fix safe; do
    [ -n "$fix" ] && [ "$safe" = yes ] || continue
    [ "${1:-}" = verbose ] && printf '  %s\n' "$title"
    case "$applied" in *" $fix "*) continue ;; esac
    applied="$applied$fix "
    $fix
  done
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
    safe_fixes verbose
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
          safe_fixes
          run_checks
        fi ;;
      @again) run_checks ;;
      @save) save_report ;;
      *) apply_fix "$pick" ;;
    esac
  done
}

# ------------------------------------------------------------------ setup code
# The first admin's one-time setup code (#737) is in the Secret
# lab/homestead-setup until that account exists. HS_SETUP_CODE sets it ahead.
SETUP_ALPHABET=0123456789ABCDEFGHJKMNPQRSTVWXYZ
check_setup_code() {
  [ -n "${HS_SETUP_CODE:-}" ] || return 0
  plain=$(printf '%s' "$HS_SETUP_CODE" | tr -d ' -' | tr 'a-z' 'A-Z')
  case "$plain" in *[!$SETUP_ALPHABET]*) plain="" ;; esac
  [ "${#plain}" = 10 ] || fail "HS_SETUP_CODE must be 10 characters from 0-9 and A-Z without I, L, O and U, such as ABCD-EFGH-JK."
}
apply_setup_code() { # kubectl... : the code set ahead, from stdin, never on a command line or on disk
  [ -n "${HS_SETUP_CODE:-}" ] || return 0
  if [ "$DRY" = 1 ]; then printf '+ %s apply -f - (apply the Secret lab/homestead-setup with HS_SETUP_CODE)\n' "$*"; return 0; fi
  printf 'apiVersion: v1\nkind: Secret\ntype: Opaque\nmetadata:\n  name: homestead-setup\n  namespace: lab\nstringData:\n  code: "%s"\n' \
    "$(printf '%s' "$HS_SETUP_CODE" | tr 'a-z' 'A-Z')" | "$@" apply -f - >/dev/null \
    || fail "kubectl could not save HS_SETUP_CODE as the Secret lab/homestead-setup."
}
setup_code_line() { # kubectl... -> where the first admin's setup code is, or the code itself
  code=""
  if [ "$DRY" = 0 ]; then
    i=0; while [ $i -lt 30 ]; do
      code=$("$@" -n lab get secret homestead-setup -o jsonpath='{.data.code}' 2>/dev/null | base64 -d 2>/dev/null)
      [ -n "$code" ] && break; i=$((i+1)); sleep 2
    done
  fi
  if [ -n "$code" ]; then printf 'Setup code: %s' "$code"
  else printf "Setup code: this command shows it\n  %s -n lab get secret homestead-setup -o jsonpath='{.data.code}' | base64 -d" "$*"; fi
}
kube_cmd() { if [ "$DIST" = rke2 ]; then echo "/var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml"; else echo "k3s kubectl"; fi; }

# ------------------------------------------------------------------ installing
do_install() {
  check_setup_code
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

# ------------------------------------------------------------------ uninstall
# What --uninstall removes, and the only things it removes: exact names, never a
# label, because apps deployed through Homestead carry its labels too.
# tests/test_what_homestead_changes.py holds this list against the wiki page
# "What Homestead changes": each name under "Safe to delete", none under "do not
# delete". self is Homestead itself, safe its helpers, data goes only with
# --everything. own is Homestead's namespace, apps the apps' namespace and lab.
# Not here on purpose, and on the page under "do not delete":
# homestead-data-handoff and homestead-cluster-shutdown (the journals of a data
# move and a cluster shutdown), homestead-move-credentials-* (a backup target
# may read them, and each name is generated).
UNINSTALL_OBJECTS='
self own deployment homestead
self own service homestead
self own serviceaccount homestead
self cluster clusterrolebinding homestead
self cluster clusterrole homestead
safe apps daemonset homestead-nodeprobe
safe apps configmap homestead-nodeprobe
safe apps secret homestead-smart-key
safe apps secret homestead-allocation-auth
safe apps configmap homestead-shell-sessions
safe apps role homestead-console
safe apps rolebinding homestead-console
safe own secret homestead-setup
safe own role homestead-setup
safe own rolebinding homestead-setup
safe longhorn-system recurringjobs.longhorn.io homestead-weekly-trim
safe longhorn-system networkpolicies.networking.k8s.io homestead-backend-access
data own service homestead-vip
data own lease homestead-leader
data apps secret homestead-auth
data apps configmap homestead-install
data apps configmap homestead-settings
data apps configmap homestead-outage-carry
data apps configmap homestead-portal
data apps configmap homestead-ipam
data apps configmap homestead-group-tiers
data apps configmap homestead-mqtt
data apps configmap homestead-hardware
data apps configmap homestead-sources
data apps configmap homestead-vm-store
data apps configmap homestead-vips
data apps configmap homestead-fleet
data apps configmap homestead-fleet-replay
data apps configmap homestead-clusters
data apps configmap homestead-iso-library
data apps secret homestead-unifi
data apps secret homestead-mqtt
data apps secret homestead-fleet-key
data own persistentvolumeclaim homestead-data
'
UNINSTALL_TAINT=homestead.io/standby
UNINSTALL_HOLD=/etc/apt/apt.conf.d/99-homestead-hold
UNINSTALL_HOLD_MARK="Homestead installs updates on this host itself"
UNINSTALL_PAGE="$WIKI/What-Homestead-changes#what-removing-homestead-leaves-behind"
UNINSTALL_WAIT_TRIES=60 # three seconds apart: three minutes for Homestead's pods to stop
UNINSTALL_AGAIN="curl -sfL $RAW/main/scripts/install.sh | sudo sh -s -- --uninstall"
UN_OWN=""; UN_APPS=""; UN_RELEASE=""; UN_COUNT=0; UN_LOOKED=""; UN_FAILED=""; UN_CLAIM=""
UN_RUNNING=0 # Homestead's Deployment was there when this run started
UN_PROVEN=0 # a Homestead Deployment or a homestead-data claim says Homestead was here
UN_GUESSED_APPS=0; UN_FOREIGN=0; UN_BINDING_FOREIGN=0
UN_SO_FAR="Nothing has been removed."

# Asks the cluster and keeps its words: "not there" and "could not ask" are not
# the same, and only the first may read as nothing to remove.
uk() {
  if have timeout; then
    timeout 60 $KC "$@" 2>"$WORK_TMP/uk.err"; uk_rc=$?
    [ "$uk_rc" != 124 ] || echo "no answer in 60 seconds" > "$WORK_TMP/uk.err"
    return "$uk_rc"
  fi
  $KC "$@" 2>"$WORK_TMP/uk.err"
}
uk_absent() { grep -Eq "NotFound|doesn't have a resource type" "$WORK_TMP/uk.err"; }
uk_refuse() { # what could not be read
  fail "Could not read $1 from the cluster: $(head -n 1 "$WORK_TMP/uk.err"). $UN_SO_FAR"
}

uninstall_namespaces() {
  named=${HS_NAMESPACE:-}; UN_APPS=${HS_APPS_NAMESPACE:-}
  rows=$(uk get deployment -A --field-selector metadata.name=homestead -o jsonpath='{range .items[*]}{.metadata.namespace}{"|"}{.metadata.labels.app}{"|"}{.metadata.annotations.meta\.helm\.sh/release-name}{"|"}{.spec.template.spec.containers[0].env[?(@.name=="DEFAULT_NS")].value}{"|"}{.spec.template.spec.volumes[?(@.name=="data")].persistentVolumeClaim.claimName}{"\n"}{end}') \
    || uk_refuse "the Deployments named homestead"
  rows=$(printf '%s\n' "$rows" | awk -F'|' '$2 == "homestead"')
  # Copies in two namespaces share one ClusterRole: naming one does not make it
  # safe to take the role from the other.
  if [ "$(printf '%s\n' "$rows" | grep -c .)" -gt 1 ]; then
    fail "Homestead is running in more than one namespace: $(printf '%s\n' "$rows" | cut -d'|' -f1 | tr '\n' ' ')
They share a ClusterRole, so removing one this way would break the other. Remove all but one by hand: $UNINSTALL_PAGE
Nothing has been removed."
  fi
  if [ -n "$rows" ]; then
    UN_OWN=$(printf '%s' "$rows" | cut -d'|' -f1)
    if [ -n "$named" ] && [ "$named" != "$UN_OWN" ]; then
      fail "HS_NAMESPACE says $named, but Homestead is running in $UN_OWN. Nothing has been removed."
    fi
    UN_RELEASE=$(printf '%s' "$rows" | cut -d'|' -f3)
    says=$(printf '%s' "$rows" | cut -d'|' -f4)
    if [ -n "$UN_APPS" ] && [ -n "$says" ] && [ "$UN_APPS" != "$says" ]; then
      fail "HS_APPS_NAMESPACE says $UN_APPS, but Homestead's Deployment says $says. Nothing has been removed."
    fi
    [ -n "$UN_APPS" ] || UN_APPS=$says
    UN_CLAIM=$(printf '%s' "$rows" | cut -d'|' -f5)
    UN_RUNNING=1; UN_PROVEN=1
  else
    # Homestead has gone already: its data volume says where it lived.
    claims=$(uk get persistentvolumeclaim -A --field-selector metadata.name=homestead-data -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}') \
      || uk_refuse "the claims named homestead-data"
    if [ -n "$named" ]; then
      UN_OWN=$named
      if printf '%s\n' "$claims" | grep -qxF -- "$named"; then UN_PROVEN=1
      elif [ -n "$claims" ]; then
        fail "HS_NAMESPACE says $named, but Homestead's data volume is in: $(printf '%s\n' "$claims" | tr '\n' ' ')
Nothing has been removed."
      fi
    elif [ "$(printf '%s\n' "$claims" | grep -c .)" -gt 1 ]; then
      fail "Found Homestead's data volume in more than one namespace: $(printf '%s\n' "$claims" | tr '\n' ' ')
Set HS_NAMESPACE to the one to remove. Nothing has been removed."
    elif [ -n "$claims" ]; then
      UN_OWN=$claims; UN_PROVEN=1
    else
      UN_OWN=lab
    fi
    [ -n "$UN_APPS" ] || UN_GUESSED_APPS=1
  fi
  [ -n "$UN_APPS" ] || UN_APPS=lab
  UN_LOOKED=$UN_OWN
  for ns in "$UN_APPS" lab; do
    case " $UN_LOOKED " in *" $ns "*) ;; *) UN_LOOKED="$UN_LOOKED $ns" ;; esac
  done
}

uninstall_foreign() { # a Deployment named homestead in Homestead's namespace that is someone else's
  UN_FOREIGN=0
  label=$(uk -n "$UN_OWN" get deployment homestead -o 'jsonpath={.metadata.labels.app}') \
    || { uk_absent && return 0; uk_refuse "deployment $UN_OWN/homestead"; }
  # It may be what mounts homestead-data, so that claim proves nothing either.
  [ "$label" = homestead ] || { UN_FOREIGN=1; UN_PROVEN=0; }
}

uninstall_moved() { [ "$UN_RUNNING" = 1 ] && [ "$UN_CLAIM" != homestead-data ]; }

# What a person can do next, once Homestead's Deployment has gone. A moved claim
# leaves a later run nothing to know this install by.
uninstall_next() { # what to say when a rerun will work
  if uninstall_moved; then
    printf 'A later run cannot recognise this install, so it may refuse. Finish by hand: %s' "$UNINSTALL_PAGE"
  else
    printf '%s' "$1"
  fi
}

uninstall_kept_tickets() {
  [ -s "$WORK_TMP/tickets-kept" ] || return 0
  printf '\nStaying: a rollback may be running while Homestead runs, so these stay. Once\nHomestead has gone, wait at least 5 minutes after any rollback, then run this again\nto remove them. The app on each may be stopped; start it yourself:\n'
  while read -r volume; do printf '  attachment ticket homestead-revert on %s\n' "$volume"; done < "$WORK_TMP/tickets-kept"
}

uninstall_is_homesteads() { # kind namespace name: what marks it, for the kinds that have a mark
  case "$1" in
    service) path='{.spec.selector.app}'; want=homestead ;;
    clusterrolebinding)
      path='{range .subjects[*]}{.kind}{"|"}{.name}{"|"}{.namespace}{"\n"}{end}'
      want="ServiceAccount|homestead|$UN_OWN" ;;
    *) return 0 ;;
  esac
  if [ "$2" = - ]; then whose=$(uk get "$1" "$3" -o "jsonpath=$path") || uk_refuse "$1 $3"
  else whose=$(uk -n "$2" get "$1" "$3" -o "jsonpath=$path") || uk_refuse "$1 $2/$3"; fi
  [ "$whose" = "$want" ]
}

uninstall_leaving() { # kind namespace|- name
  if [ "$2" = - ]; then shown="$1 $3"; else shown="$1 $2/$3"; fi
  printf "Leaving %s: it has that name, but it is not Homestead's.\n" "$shown"
}

# One line per object that exists: tier|namespace|kind|name.
uninstall_list() {
  : > "$WORK_TMP/remove"
  UN_BINDING_FOREIGN=0
  uninstall_foreign
  while read -r tier where kind name; do
    [ -n "$tier" ] || continue
    [ "$tier" != data ] || [ "$EVERYTHING" = 1 ] || continue
    # With the data on another claim, what is on this one is not known to be spare.
    if [ "$name" = homestead-data ] && [ "$UN_RUNNING" = 1 ] && [ "$UN_CLAIM" != homestead-data ]; then continue; fi
    case "$where" in
      own) places=$UN_OWN ;;
      apps) places=$UN_APPS; [ "$UN_APPS" = lab ] || places="$UN_APPS lab" ;;
      cluster) places=- ;;
      *) places=$where ;;
    esac
    for ns in $places; do
      if [ "$ns" = - ]; then uk get "$kind" "$name" -o name >/dev/null
      else uk -n "$ns" get "$kind" "$name" -o name >/dev/null; fi
      if [ $? != 0 ]; then
        uk_absent && continue
        uk_refuse "$kind $name"
      fi
      if [ "$tier" = self ] && { [ "$UN_FOREIGN" = 1 ] || [ "$UN_BINDING_FOREIGN" = 1 ]; }; then
        uninstall_leaving "$kind" "$ns" "$name"
        continue
      fi
      if ! uninstall_is_homesteads "$kind" "$ns" "$name"; then
        [ "$kind" != clusterrolebinding ] || UN_BINDING_FOREIGN=1
        uninstall_leaving "$kind" "$ns" "$name"
        continue
      fi
      printf '%s|%s|%s|%s\n' "$tier" "$ns" "$kind" "$name" >> "$WORK_TMP/remove"
    done
  done <<LIST
$UNINSTALL_OBJECTS
LIST
}

uninstall_tainted() { # the hosts carrying the hot-spare taint
  nodes=$(uk get nodes -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{.spec.taints[*].key}{"\n"}{end}') || uk_refuse "the hosts"
  printf '%s\n' "$nodes" | awk -v taint="$UNINSTALL_TAINT" '{for (i = 2; i <= NF; i++) if ($i == taint) print $1}'
}

# volume ticket, for each of Homestead's attachment tickets it may take; no Longhorn
# is none. While Homestead runs a revert ticket may be a live rollback, not a leftover.
uninstall_tickets() {
  : > "$WORK_TMP/tickets-kept"
  rows=$(uk -n longhorn-system get volumeattachments.longhorn.io -o 'jsonpath={range .items[*]}{.metadata.name}{"\t"}{.spec.attachmentTickets.homestead-rebuild}{"\t"}{.spec.attachmentTickets.homestead-revert}{"\n"}{end}') \
    || { uk_absent && return 0; uk_refuse "Longhorn's attachment tickets"; }
  printf '%s\n' "$rows" | awk -F'\t' '$2 != "" {print $1, "homestead-rebuild"}'
  if [ "$UN_RUNNING" = 1 ] || [ "$UN_FOREIGN" = 1 ]; then
    printf '%s\n' "$rows" | awk -F'\t' '$3 != "" {print $1}' > "$WORK_TMP/tickets-kept"
  else
    printf '%s\n' "$rows" | awk -F'\t' '$3 != "" {print $1, "homestead-revert"}'
  fi
}

uninstall_hold_is_homesteads() { [ -f "$UNINSTALL_HOLD" ] && grep -qF "$UNINSTALL_HOLD_MARK" "$UNINSTALL_HOLD"; }

# An app Homestead stopped stays stopped, and only the annotation says how many
# copies it had. What a job holds is named whatever state it is in: never refused.
uninstall_stopped() {
  rows=$(uk get deployments,statefulsets,cronjobs -A -o 'jsonpath={range .items[*]}{.kind}{"|"}{.metadata.namespace}{"|"}{.metadata.name}{"|"}{.spec.replicas}{"|"}{.spec.suspend}{"|"}{.metadata.annotations.homestead\.io/held-for-power}{"|"}{.metadata.annotations.homestead\.io/held-as}{"|"}{.metadata.annotations.homestead\.io/schedule-copies}{"|"}{.metadata.annotations.homestead\.io/autostart-replicas}{"|"}{.metadata.annotations.homestead\.io/restructure-replicas}{"\n"}{end}') \
    || uk_refuse "the apps"
  printf '%s\n' "$rows" | awk -F'|' '
    $6 != "" {printf "  %s %s/%s (a job holds this)\n", $1, $2, $3; next}
    ($1 == "CronJob" ? $5 == "true" : $4 == "0") && ($7 != "" || $8 != "" || $9 != "" || $10 != "") {printf "  %s %s/%s\n", $1, $2, $3}'
  rows=$(uk get virtualmachines.kubevirt.io -A -o 'jsonpath={range .items[*]}{.metadata.namespace}{"|"}{.metadata.name}{"|"}{.spec.running}{"|"}{.spec.runStrategy}{"|"}{.metadata.annotations.homestead\.io/held-for-power}{"|"}{.metadata.annotations.homestead\.io/held-as}{"|"}{.metadata.annotations.homestead\.io/schedule-copies}{"|"}{.metadata.annotations.homestead\.io/autostart-replicas}{"\n"}{end}') \
    || { uk_absent && return 0; uk_refuse "the VMs"; }
  printf '%s\n' "$rows" | awk -F'|' '
    $5 != "" {printf "  VirtualMachine %s/%s (a job holds this)\n", $1, $2; next}
    ($3 == "false" || $4 == "Halted") && ($6 != "" || $7 != "" || $8 != "") {printf "  VirtualMachine %s/%s\n", $1, $2}'
}

uninstall_stays() {
  printf '\nWhat stays:\n'
  cat <<STAYS
  - Your apps, VMs and volumes, and what Homestead made for them. They keep
    running. Nothing is removed by label.
  - The homestead.io/* annotations on your apps. They are harmless, and they are
    the only record of how many copies a stopped app had.
  - Longhorn, kube-vip, Multus, KubeVirt and CDI (their HelmCharts), the backup
    target's Secrets, the object store, the share servers, and the storage and
    priority classes Homestead made: your apps and data still use them.
  - The other taints and labels on hosts, and what Homestead set on the hosts
    (the multipath setting, inotify limits, Longhorn's disk and its mount, GPU
    passthrough, network configuration).
  - /etc/apt/apt.conf.d/99-homestead-hold on each other host, if Homestead
    managed OS updates: this removes it only on this host.
STAYS
  if [ "$EVERYTHING" = 1 ]; then
    printf '  - A namespace that still holds anything else.\n'
  else
    printf '  - Homestead'"'"'s data volume, its accounts, its settings and its own address (the\n'
    printf '    Service homestead-vip, if it has one), so a reinstall picks them up.'
    if uninstall_moved; then printf '\n'; else printf ' To remove\n    those too, run this with --everything.\n'; fi
    printf '    A reinstall keeping this data puts the node probe back, unless you removed\n'
    printf '    it earlier, or Homestead here is a release without this fix. If it\n'
    printf '    stays away, install it from Settings > Cluster > Add-ons. A Helm reinstall\n'
    printf '    brings it back in any case, unless nodeprobe.enabled is false.\n'
  fi
  printf 'The whole list: %s\n' "$UNINSTALL_PAGE"
}

uninstall_review() {
  say "Removing Homestead ($REF)$([ "$DRY" = 1 ] && printf ', dry run: no changes will be made')"
  UN_COUNT=$(grep -c . "$WORK_TMP/remove")
  # Into files, not $(...): a refusal in a subshell would only end the subshell,
  # and "not allowed to look" would read as "none".
  uninstall_stopped > "$WORK_TMP/stopped"
  if [ -s "$WORK_TMP/stopped" ]; then
    printf '\nWarning: Homestead stopped these, and they stay stopped once it is gone. Each\nhas an annotation saying how many copies it had; scale it back up yourself. One\nthat a job holds was stopped, or is about to be, by a job that will not finish:\n'
    cat "$WORK_TMP/stopped"
  fi
  uninstall_tainted > "$WORK_TMP/tainted"; uninstall_tickets > "$WORK_TMP/tickets"
  tainted=$(cat "$WORK_TMP/tainted"); tickets=$(cat "$WORK_TMP/tickets")
  [ -z "$tainted" ] || UN_COUNT=$((UN_COUNT + 1))
  [ -z "$tickets" ] || UN_COUNT=$((UN_COUNT + 1))
  uninstall_hold_is_homesteads && UN_COUNT=$((UN_COUNT + 1))
  if [ "$UN_COUNT" = 0 ]; then
    printf "\nNothing of Homestead's is left to remove. Looked in namespaces %s and longhorn-system, and at the hosts. Set HS_NAMESPACE or HS_APPS_NAMESPACE if yours are elsewhere.\n" "$UN_LOOKED"
    uninstall_stays
    return 0
  fi
  for tier in self safe data; do
    grep -q "^$tier|" "$WORK_TMP/remove" || continue
    case "$tier" in
      self) printf '\nHomestead itself:\n' ;;
      safe) printf '\nIts helpers:\n' ;;
      data) printf '\nIts data, accounts and records, for good:\n' ;;
    esac
    awk -F'|' -v tier="$tier" '$1 == tier { if ($2 == "-") printf "  %s %s\n", $3, $4; else printf "  %s %s/%s\n", $3, $2, $4 }' "$WORK_TMP/remove"
  done
  if [ -n "$tainted" ] || [ -n "$tickets" ] || uninstall_hold_is_homesteads; then
    printf '\nOn the hosts:\n'
    for node in $tainted; do printf '  taint %s on %s\n' "$UNINSTALL_TAINT" "$node"; done
    printf '%s\n' "$tickets" | while read -r volume ticket; do
      [ -n "$volume" ] && printf '  attachment ticket %s on Longhorn volume %s\n' "$ticket" "$volume"
    done
    if uninstall_hold_is_homesteads; then
      printf '  %s on this host. It gives Ubuntu its own automatic updates back: if\n  those reboot by themselves, hosts reboot without being drained first.\n' "$UNINSTALL_HOLD"
    fi
  fi
  if [ "$EVERYTHING" = 1 ] && [ "$UN_PROVEN" = 1 ]; then
    printf '\nThen namespace %s, only if nothing else is left in it.\n' "$UN_OWN"
  fi
  if [ -n "$UN_RELEASE" ]; then
    printf '\nHomestead was installed with Helm, so that part goes through: helm uninstall %s -n %s\n' "$UN_RELEASE" "$UN_OWN"
  fi
  uninstall_kept_tickets
  if uninstall_moved; then
    printf '\nStaying: Homestead has its data on a claim it did not start with, so this will not\ndelete it, nor any claim named homestead-data. Delete them yourself if you are sure:\n'
    [ -z "$UN_CLAIM" ] || printf '  %s/%s holds the data\n' "$UN_OWN" "$UN_CLAIM"
    printf "A later run cannot recognise this: with Homestead's Deployment gone, it may offer a\nclaim named homestead-data as Homestead's data volume, or refuse if there is none.\n"
    [ "$EVERYTHING" = 1 ] || printf 'To remove the accounts and records too, choose --everything on this run.\n'
  fi
  if [ "$UN_GUESSED_APPS" = 1 ]; then
    printf "\nHomestead's Deployment has gone, so it cannot say where your apps' namespace is:\nthis looked in %s. If yours is elsewhere, set HS_APPS_NAMESPACE and run this again.\n" "$UN_LOOKED"
  fi
  printf '\nMake sure no OS update, host reboot, cluster shutdown or other Homestead job (a\ncopy, an import, a restore, a rollback) is running: Homestead cannot finish one\nonce it is gone. If Flux, Argo CD or a k3s HelmChart manages Homestead, stop that\nfirst: it would put Homestead back after this has checked it is gone.\n'
  uninstall_stays
}

uninstall_delete() { # namespace|- kind name
  if [ "$1" = - ]; then set -- "$2" "$3"; run $KC delete "$1" "$2" --ignore-not-found --timeout=120s
  else ns=$1; shift; run $KC -n "$ns" delete "$1" "$2" --ignore-not-found --timeout=120s; fi \
    || UN_FAILED="$UN_FAILED $1/$2"
}

uninstall_remove_tier() { # tier
  while IFS='|' read -r tier ns kind name; do
    [ "$tier" = "$1" ] || continue
    # The data volume is what lets a later run know this install, so it goes last
    # and only when all the rest went.
    if [ "$kind" = persistentvolumeclaim ] && [ -n "$UN_FAILED" ]; then
      printf 'Keeping the data volume %s/%s: something above could not be removed, and a later run needs it to recognise this install.\n' "$ns" "$name"
      continue
    fi
    uninstall_delete "$ns" "$kind" "$name"
  done < "$WORK_TMP/remove"
}

uninstall_kubeconfig() {
  case "$SERVICE" in
    k3s) echo /etc/rancher/k3s/k3s.yaml ;;
    rke2-server) echo /etc/rancher/rke2/rke2.yaml ;;
    *) echo "${KUBECONFIG:-}" ;;
  esac
}

# Homestead first, and seen gone before anything else goes: while it runs, it
# puts back what it looks after.
uninstall_stop_homestead() {
  if grep -q "^self|$UN_OWN|deployment|homestead$" "$WORK_TMP/remove"; then
    if [ -n "$UN_RELEASE" ]; then
      if ! run env KUBECONFIG="$(uninstall_kubeconfig)" helm uninstall "$UN_RELEASE" -n "$UN_OWN" 2>"$WORK_TMP/helm.err"; then
        cat "$WORK_TMP/helm.err" >&2
        # The Deployment names a release Helm has lost: asking again would only fail again.
        grep -q "release: not found" "$WORK_TMP/helm.err" \
          || fail "helm uninstall did not finish. Nothing else has been removed; run this again once it has."
        printf 'Helm has no record of that release, so its objects go by name instead.\n'
        run $KC -n "$UN_OWN" delete deployment homestead --ignore-not-found --timeout=120s \
          || fail "Could not remove Homestead's Deployment. Nothing else has been removed."
      fi
    else
      run $KC -n "$UN_OWN" delete deployment homestead --ignore-not-found --timeout=120s \
        || fail "Could not remove Homestead's Deployment. Nothing else has been removed."
    fi
  fi
  i=0
  while :; do
    pods=$(uk -n "$UN_OWN" get pods -l app=homestead -o name) \
      || fail "Could not check that Homestead has stopped: $(head -n 1 "$WORK_TMP/uk.err"). Nothing else has been removed. $(uninstall_next "Run this again.")"
    [ -n "$pods" ] || return 0
    i=$((i + 1))
    if [ "$i" -ge "$UNINSTALL_WAIT_TRIES" ]; then
      printf "\nHomestead's pods are still there after three minutes:\n%s\n" "$pods" >&2
      printf 'A pod on a host that is down stays until that host returns. Force it, only if the host is really off:\n' >&2
      for pod in $pods; do printf '  %s -n %s delete %s --force --grace-period=0\n' "$KC" "$UN_OWN" "$pod" >&2; done
      fail "Nothing else has been removed. $(uninstall_next "Run this again once they have gone.")"
    fi
    sleep 3
  done
}

uninstall_namespace() {
  ns=$UN_OWN
  case "$ns" in
    default|kube-*|longhorn-system)
      printf 'Namespace %s stays: it is not Homestead'"'"'s alone.\n' "$ns"; return 0 ;;
  esac
  kinds=$(uk api-resources --namespaced --verbs=list -o name) \
    || { printf 'Namespace %s stays: could not check what is in it (%s).\n' "$ns" "$(head -n 1 "$WORK_TMP/uk.err")"; return 0; }
  held=""
  for kind in $kinds; do
    case "$kind" in events|events.events.k8s.io) continue ;; esac
    found=$(uk -n "$ns" get "$kind" -o name) \
      || { printf 'Namespace %s stays: could not check its %s (%s).\n' "$ns" "$kind" "$(head -n 1 "$WORK_TMP/uk.err")"; return 0; }
    for item in $found; do
      case "${item##*/}" in kube-root-ca.crt) continue ;; esac
      case "$item" in serviceaccount/default) continue ;; esac
      held="$held $item"
    done
  done
  if [ -n "$held" ]; then
    printf 'Namespace %s stays. It still holds:%s\n' "$ns" "$(printf '%s\n' $held | head -n 10 | sed 's/^/ /' | tr -d '\n')"
    return 0
  fi
  uninstall_delete - namespace "$ns"
}

do_uninstall() {
  detect_node
  [ -n "$KC" ] || fail "This host has no administrator access to the cluster. On a server node, run: $UNINSTALL_AGAIN
Nothing has been removed."
  uninstall_namespaces
  uninstall_list
  # Beside a Deployment that is not Homestead's, the marks on the hosts prove nothing
  # either. With no Deployment at all they stand on their own: each carries its mark.
  : > "$WORK_TMP/marks"
  if [ "$UN_FOREIGN" = 1 ]; then
    uninstall_tainted > "$WORK_TMP/tainted"; uninstall_tickets > "$WORK_TMP/tickets"
    while read -r node; do printf '  taint %s on %s\n' "$UNINSTALL_TAINT" "$node"; done < "$WORK_TMP/tainted" >> "$WORK_TMP/marks"
    while read -r volume ticket; do printf '  attachment ticket %s on Longhorn volume %s\n' "$ticket" "$volume"; done < "$WORK_TMP/tickets" >> "$WORK_TMP/marks"
    while read -r volume; do printf '  attachment ticket homestead-revert on Longhorn volume %s\n' "$volume"; done < "$WORK_TMP/tickets-kept" >> "$WORK_TMP/marks"
    if uninstall_hold_is_homesteads; then printf '  %s on this host\n' "$UNINSTALL_HOLD" >> "$WORK_TMP/marks"; fi
  fi
  if [ "$UN_PROVEN" = 0 ] && { [ -s "$WORK_TMP/remove" ] || [ -s "$WORK_TMP/marks" ]; }; then
    if [ "$UN_FOREIGN" = 1 ]; then why="the Deployment $UN_OWN/homestead is not Homestead's, and it may be what uses them"
    else why="there is no Homestead Deployment and no homestead-data claim"; fi
    fail "These have Homestead's names, but $why, so nothing shows they are Homestead's:
$(awk -F'|' '{ if ($2 == "-") printf "  %s %s\n", $3, $4; else printf "  %s %s/%s\n", $3, $2, $4 }' "$WORK_TMP/remove"; cat "$WORK_TMP/marks")
Nothing has been removed. If they are Homestead's leftovers, remove them by hand: $UNINSTALL_PAGE"
  fi
  uninstall_review
  [ "$UN_COUNT" != 0 ] || return 0
  if [ "$DRY" = 1 ]; then printf '\nDry run: nothing was changed.\n'; return 0; fi
  interactive || fail "Removing Homestead asks first, and there is no terminal to ask on. Nothing was removed. Run it from a terminal: $UNINSTALL_AGAIN
Add --dry-run to see the list without one."
  if [ -n "$UN_RELEASE" ] && ! have helm; then
    fail "helm is not on this host. Run this, then $UNINSTALL_AGAIN for the rest:
  helm uninstall $UN_RELEASE -n $UN_OWN
Nothing was removed."
  fi
  word=yes
  [ "$EVERYTHING" != 1 ] || word=everything
  question="This removes everything listed above."
  volume=$(awk -F'|' '$1 == "data" && $3 == "persistentvolumeclaim" {print $2 "/" $4}' "$WORK_TMP/remove")
  [ -z "$volume" ] || question="$question That includes Homestead's data volume, $volume, for good."
  if ! typed "Remove Homestead" "$question" "$word"; then
    printf '\nNothing was removed.\n'
    return 1
  fi
  uninstall_stop_homestead
  UN_SO_FAR="Homestead itself may have gone, and the rest is still there. $(uninstall_next "Run this again.")"
  uninstall_list > /dev/null # what Helm or the first delete took is off the list
  uninstall_remove_tier self
  uninstall_remove_tier safe
  uninstall_tainted > "$WORK_TMP/tainted"
  for node in $(cat "$WORK_TMP/tainted"); do
    run $KC taint nodes "$node" "$UNINSTALL_TAINT-" || UN_FAILED="$UN_FAILED taint/$node"
  done
  uninstall_tickets > "$WORK_TMP/tickets"
  while read -r volume ticket; do
    [ -n "$volume" ] || continue
    run $KC -n longhorn-system patch volumeattachments.longhorn.io "$volume" --type=merge \
      -p "{\"spec\":{\"attachmentTickets\":{\"$ticket\":null}}}" || UN_FAILED="$UN_FAILED ticket/$volume"
  done < "$WORK_TMP/tickets"
  if uninstall_hold_is_homesteads; then
    run rm -f -- "$UNINSTALL_HOLD" || UN_FAILED="$UN_FAILED $UNINSTALL_HOLD"
  fi
  uninstall_remove_tier data
  if [ "$EVERYTHING" = 1 ] && [ "$UN_PROVEN" = 1 ]; then uninstall_namespace; fi
  uninstall_stays
  uninstall_kept_tickets
  printf '\nOn each other host, if Homestead managed its OS updates: rm -f %s\n' "$UNINSTALL_HOLD"
  if [ -n "$UN_FAILED" ]; then
    printf '\nCould not remove:%s\n%s\n' "$UN_FAILED" "$(uninstall_next "Run this again; it only looks for what is left.")"
    return 1
  fi
  if [ "$UN_RUNNING" = 1 ]; then say "Homestead has been removed."
  else say "What was left of Homestead has been removed."; fi
  [ "$UN_GUESSED_APPS" = 0 ] || printf "This looked in %s for it. If your apps' namespace is elsewhere, set HS_APPS_NAMESPACE and run this again.\n" "$UN_LOOKED"
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

# The checks alone only read, and say themselves when they are not run as root.
[ "$(id -u)" = 0 ] || [ "$DRY" = 1 ] || [ "$ACTION" = check ] || {
  [ "$ACTION" != uninstall ] || fail "This must run as root: $UNINSTALL_AGAIN"
  fail "The installer must run as root: curl ... | sudo sh"
}
banner() { say "Homestead installer ($REF)$([ "$DRY" = 1 ] && printf ', dry run: no changes will be made')"; }
case "$ACTION" in
  check) ROLE=$(given HS_ROLE); prechecks "${ROLE:-server}" ;;
  console) banner; configure_console; exit $? ;;
  install) banner; do_install; exit 0 ;;
  doctor) doctor menu; exit 0 ;;
  report) UI=text; doctor report ;;
  fix-safe) UI=text; doctor fix ;;
  uninstall) do_uninstall; exit $? ;;
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
    [ "$KIND" = harvester ] || set -- "$@" console "Configure host status console"
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
    console) configure_console ;;
    install) do_install ;;
    checks) ( prechecks new ) ;;
    doctor) doctor menu ;;
    clean) cleanup_menu ;;
    snapshot) confirm "etcd Snapshot" "Take an etcd snapshot now?" && fix_snapshot && msg "etcd Snapshot" "Snapshot saved." ;;
    restore) restore ;;
    report) DMODE=menu; run_checks; save_report ;;
  esac
done
