#!/usr/bin/env bash
# The installer's and node doctor's screens, for the wiki - captured in CI.
#
# install.sh runs with --dry-run inside tmux, 100 columns by 32 rows, with
# stand-ins for the commands that describe the machine: first a fresh Ubuntu
# box (for the install screens), then a k3s server with a cordoned node and
# failing pods (for the doctor). Keys are sent as a person would press them,
# and each screen is saved with its colours as release-assets/tui-<name>.ans;
# scripts/render_tui.mjs turns those into PNGs. Needs tmux and whiptail.
set -u
cd "$(dirname "$0")/.."
OUT=release-assets
mkdir -p "$OUT"
BIN=$(mktemp -d)
MISSED=0
trap 'tmux kill-server 2>/dev/null; rm -rf "$BIN"' EXIT

stub() { printf '#!/bin/sh\n%s\n' "$2" > "$BIN/$1"; chmod +x "$BIN/$1"; }

# ---- a fresh machine
stub id '[ "$1" = -u ] && echo 0 || exec /usr/bin/id "$@"'
stub hostname 'echo node1'
stub timedatectl 'echo yes'
stub ip 'case "$*" in
  "-4 route get"*) echo "1.1.1.1 via 192.0.2.1 dev eth0 src 192.0.2.50 uid 0" ;;
  "-4 -o addr show scope global") echo "2: eth0    inet 192.0.2.50/24 brd 192.0.2.255 scope global eth0" ;;
  "-4 addr show") echo "    inet 192.0.2.50/24 brd 192.0.2.255 scope global eth0" ;;
esac'
stub df 'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/sda2 488245288 97649057 390596231 20% /"'

shot() { # name text-the-screen-shows
  # Wait for the screen to be the one expected - checks call out to the
  # internet, and take as long as they take - then a moment to settle.
  for _ in $(seq 60); do tmux capture-pane -p -t tui | grep -q "$2" && break; sleep 0.5; done
  sleep 0.7
  if tmux capture-pane -p -t tui | grep -q "$2"; then
    tmux capture-pane -p -e -t tui > "$OUT/tui-$1.ans"; echo "captured tui-$1"
  else
    echo "skipped tui-$1: the screen never showed \"$2\""; tmux capture-pane -p -t tui; MISSED=1
  fi
}
key() { tmux send-keys -t tui "$@"; }
start() { # PATH-prefix
  tmux kill-session -t tui 2>/dev/null
  tmux new-session -d -s tui -x 100 -y 32 \
    "env PATH=$1:$PATH TERM=xterm-256color NEWT_COLORS=root=white,black sh scripts/install.sh --dry-run; sleep 30"
  sleep 2
}

start "$BIN"
shot menu "What would you like to do"          # what it found, and what it can do
key Enter; shot role "What should this machine be"   # new cluster, or join
key Enter; shot kubernetes "Which Kubernetes"   # k3s or RKE2
key Enter; shot checks "This machine:"          # the checks, before anything changes
key Enter; shot longhorn "Install Longhorn"     # what the new cluster gets
key Enter; sleep 1; key Enter; shot ready "About to:"   # what it will do, asked once
key Escape

# ---- a k3s server with things to fix
stub systemctl 'case "$1" in
  is-active) [ "$3" = k3s ] || [ "$2" = k3s ] || [ "$3" = iscsid ] ;;
  list-unit-files) [ "$2" = k3s.service ] && echo "k3s.service enabled enabled" ;;
  *) exit 0 ;;
esac'
stub journalctl 'exit 0'
stub k3s '[ "$1" = kubectl ] && shift
case "$*" in
  "get --raw /readyz"|"get --raw /readyz/etcd") echo ok ;;
  "get node node1 -o jsonpath"*) echo "True true" ;;
  "get nodes --no-headers") echo "node1 Ready control-plane 1d v1.33"; echo "node2 NotReady <none> 1d v1.33" ;;
  "get pods -A --no-headers") echo "lab plex-1 0/1 CrashLoopBackOff 5 1h"; echo "lab web-1 1/1 Running 0 1h" ;;
  "get pods -A --field-selector=status.phase=Failed --no-headers") echo "lab old-1 0/1 Evicted 0 1d"; echo "lab old-2 0/1 Evicted 0 1d" ;;
  *"-n lab get deploy"*) case "$*" in *jsonpath*) echo 1 ;; *) echo found ;; esac ;;
  *"coredns"*) case "$*" in *jsonpath*) echo 1 ;; *) echo found ;; esac ;;
  *"get crd"*) exit 1 ;;
  *) exit 0 ;;
esac'
start "$BIN"
shot node-menu "Check this node"                 # a node: check, clean up, snapshots
key Enter; shot doctor "worth a look"            # what it found, worst first
key Enter; shot doctor-fix "Fix it now"          # one finding, and its fix
key Escape

# A screen that never showed is a menu that broke: say so.
exit "$MISSED"
