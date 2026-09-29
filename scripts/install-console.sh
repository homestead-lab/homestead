#!/bin/sh
# Install/remove only Homestead's tty1 override. Never interrupt a logged-in user.
set -eu
ACTION=${1:-enable}
DROPIN=/etc/systemd/system/getty@tty1.service.d/50-homestead-console.conf
DEST=/usr/local/lib/homestead
case "$ACTION" in enable|disable) ;; *) echo 'Expected enable or disable' >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || { echo 'Run as root.' >&2; exit 1; }
command -v systemctl >/dev/null || { echo 'Host console needs systemd.' >&2; exit 1; }
if [ "$ACTION" = disable ]; then
  rm -f "$DROPIN"
  systemctl daemon-reload
  echo 'Status screen disabled. Normal login returns on next boot or logout.'
  exit 0
fi
[ -r "${2:-}" ] || { echo 'Pass the downloaded host-console.py path.' >&2; exit 1; }
AGETTY=$(command -v agetty) || { echo 'agetty is required.' >&2; exit 1; }
# Harvester owns its console and immutable host configuration.
[ ! -f /etc/harvester-release ] || { echo 'Keeping the native Harvester console.'; exit 0; }
if ! python3 -c 'import curses' 2>/dev/null; then
  command -v timeout >/dev/null || { echo 'Install Python 3 with curses first.' >&2; exit 1; }
  if command -v apt-get >/dev/null; then
    timeout -k 10 120 sh -c 'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3' </dev/null
  elif command -v dnf >/dev/null; then timeout -k 10 120 dnf install -y python3 </dev/null
  elif command -v yum >/dev/null; then timeout -k 10 120 yum install -y python3 </dev/null
  elif command -v zypper >/dev/null; then timeout -k 10 120 zypper --non-interactive install python3 python3-curses </dev/null
  else echo 'Install Python 3 with curses first.' >&2; exit 1; fi
fi
PYTHON=$(command -v python3)
"$PYTHON" -c 'import curses' || exit 1
install -d -m 755 "$DEST" "$(dirname "$DROPIN")"
install -m 644 "$2" "$DEST/host-console.py"
# Type=idle marks this process started immediately: displaying the console must
# not hold up getty.target or multi-user.target as an ExecStartPre would.
cat > "$DEST/console-getty" <<EOF
#!/bin/sh
"$PYTHON" -I "$DEST/host-console.py"
exec "$AGETTY" --noclear - linux
EOF
chmod 755 "$DEST/console-getty"
cat > "$DROPIN" <<'EOF'
[Service]
Type=idle
ExecStart=
ExecStart=-/bin/sh /usr/local/lib/homestead/console-getty
EOF
systemctl daemon-reload
systemctl enable getty@tty1.service
echo 'Status screen enabled on tty1 for the next boot or logout. Enter/Q/Esc opens login.'
