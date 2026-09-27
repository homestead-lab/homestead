"""Small shell preflights shared by the existing import and local-copy Jobs.

These are estimates, not storage reservations. Copy exit status remains the
completion check; a failure leaves both volumes available for inspection.
"""

SHELL = r'''
copy_path() {
    p="$1"
    while :; do
        if [ -L "$p" ]; then
            echo "==> error: copy path contains a symbolic link: $p. Choose a real folder; no data was removed."
            exit 4
        fi
        [ "$p" = "$2" ] && break
        [ "$p" != / ] || exit 4
        p=$(dirname "$p")
    done
}
copy_destination() {
    copy_path "$1" "$2"
    if [ -d "$1" ]; then
        if ! links=$(timeout 60 find "$1" -type l -print -quit); then
            echo '==> error: destination paths could not be checked. Inspect the destination and try again.'
            exit 4
        fi
        if [ -n "$links" ]; then
            echo '==> error: destination contains symbolic links. Choose an empty folder or inspect the links before copying.'
            exit 4
        fi
    fi
}
copy_space() {
    available=$(df -Pk "$1" 2>/dev/null | awk 'NR == 2 {print $4}')
    case "$available" in
        ''|*[!0-9]*) echo '==> warning: destination free space is unavailable; copy completion is not guaranteed.'; return ;;
    esac
    case "$2" in
        ''|*[!0-9]*) echo '==> warning: source size is unavailable; destination free space alone cannot prove this copy fits.'; return ;;
    esac
    echo "==> space: $2 KiB estimated copy; $available KiB free"
    if [ "$2" -gt "$available" ]; then
        echo '==> error: insufficient destination free space. Grow the volume or choose another destination. Both copies are retained.'
        exit 4
    fi
}
'''
