"""Grow an installer OS/Longhorn LV without shrinking or erasing any region.

Only a mounted ext4/XFS filesystem on an ordinary linear LV, in a local
single-PV VG, is eligible. A reviewed absolute extent target, pinned host
identity and a durable one-shot helper prevent blind retries after failure.
"""
import copy
import hashlib
import json
import re
import secrets
import shlex
import urllib.error
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import homestead_capacity_review as REVIEW
import homestead_hostrun as HOST

KIND = 'disk-allocation'
LABEL = 'homestead.io/disk-allocation'
GiB = 1024 ** 3
kget = ksend = platform = ops = read_log = None
NS = 'lab'


def bind(read, send, detect, operations, namespace, logs):
    global kget, ksend, platform, ops, NS, read_log
    kget, ksend, platform, ops, NS, read_log = read, send, detect, operations, namespace, logs


# Output sections are captured before printing: a failed LVM read cannot look
# like an empty list. The same exact metadata snapshot runs inside the helper.
SNAPSHOT = r'''
snapshot() {
  ROOT=$(findmnt -rn --mountpoint / -o SOURCE) || fail 'Cannot identify the OS filesystem'
  ROOT=$(readlink -f "$ROOT") || fail 'Cannot resolve the OS device'
  ROOT_ID=$(cat /sys/class/block/${ROOT##*/}/dm/uuid) || fail 'Cannot identify the OS logical volume'
  ROOT_NAME=$(cat /sys/class/block/${ROOT##*/}/dm/name) || fail 'Cannot read the OS logical volume name'
  case "$ROOT_NAME" in ''|*[!A-Za-z0-9+_.-]*) fail 'The OS logical volume name is invalid';; esac
  ROOT_LV="/dev/mapper/$ROOT_NAME"
  [ "$(readlink -f "$ROOT_LV")" = "$ROOT" ] || fail 'The OS logical volume name points to another device'
  ROOT_REPORT=$(lvs --noheadings --separator '|' -o vg_name,vg_uuid,lv_uuid "$ROOT_LV") || fail 'The OS is not on an accessible LVM volume'
  [ "$(printf '%s\n' "$ROOT_REPORT" | wc -l)" -eq 1 ] || fail 'The OS logical volume report is ambiguous'
  REPORTED_ID=$(printf '%s\n' "$ROOT_REPORT" | awk -F'|' '{gsub(/[ -]/,"",$2);gsub(/[ -]/,"",$3);print "LVM-" $2 $3}')
  [ "$REPORTED_ID" = "$ROOT_ID" ] || fail 'The OS LVM report identifies another device'
  [ "$(readlink -f "$ROOT_LV")" = "$ROOT" ] && [ "$(cat /sys/class/block/${ROOT##*/}/dm/uuid)" = "$ROOT_ID" ] || fail 'The OS logical volume changed during inspection'
  VG=$(printf '%s' "$ROOT_REPORT" | cut -d'|' -f1 | tr -d ' ')
  case "$VG" in ''|*[!A-Za-z0-9+_.-]*) fail 'Volume group name is invalid';; esac
  LVS=$(lvs -a --segments --reportformat json --units b --nosuffix -o lv_name,lv_uuid,lv_path,lv_size,vg_uuid,lv_attr,segtype,devices,lv_kernel_major,lv_kernel_minor "$VG") || fail 'Cannot inspect logical volumes'
  VGS=$(vgs --reportformat json --units b --nosuffix -o vg_name,vg_uuid,vg_extent_size,vg_extent_count,vg_free_count "$VG") || fail 'Cannot inspect free extents'
  PVS=$(pvs --reportformat json --units b --nosuffix -o pv_name,pv_uuid,vg_uuid,pv_pe_count,pv_pe_alloc_count --select "vg_name=$VG") || fail 'Cannot inspect physical allocation'
  printf 'LVS\n%s\nENDLVS\nVGS\n%s\nENDVGS\nPVS\n%s\nENDPVS\n' "$LVS" "$VGS" "$PVS"
}
'''

INSPECT = r'''set -eu
export LC_ALL=C
fail() { echo "ERROR $*"; exit 1; }
command -v unshare >/dev/null || fail 'Install util-linux unshare before adjusting storage'
__SNAPSHOT__
META=$(snapshot) || exit 1
printf '%s\n' "$META"
fs_facts() {
  ROLE=$1; P=$2
  SOURCE=$(findmnt -rn --mountpoint "$P" -o SOURCE) || fail 'Filesystem must be mounted'
  D=$(readlink -f "$SOURCE") || fail 'Cannot resolve filesystem device'
  [ -b "$D" ] || fail 'Filesystem is not on a block device'
  [ "$(findmnt -rn --mountpoint "$P" -o FSROOT)" = / ] || fail 'A bind mount cannot be resized'
  OPTIONS=$(findmnt -rn --mountpoint "$P" -o OPTIONS) || fail 'Cannot inspect mount options'
  case ",$OPTIONS," in *,rw,*) ;; *) fail 'Filesystem must be mounted read-write';; esac
  TYPE=$(findmnt -rn --mountpoint "$P" -o FSTYPE) || fail 'Cannot inspect filesystem type'
  U=$(blkid -s UUID -o value "$D") || fail 'Filesystem identity is missing'
  MM=$(lsblk -dnro MAJ:MIN "$D") || fail 'Cannot inspect device number'
  DM=$(cat /sys/class/block/${D##*/}/dm/uuid) || fail 'Filesystem is not on a logical volume'
  case "$DM" in LVM-*) ;; *) fail 'Only ordinary LVM volumes can be grown';; esac
  STABLE="/dev/disk/by-id/dm-uuid-$DM"
  [ "$(readlink -f "$STABLE")" = "$D" ] || fail 'Stable device identity is missing'
  case "$TYPE" in
    ext4)
      INFO=$(tune2fs -l "$D") || fail 'Cannot inspect ext4 geometry'
      BS=$(printf '%s\n' "$INFO" | awk '$1=="Block" && $2=="size:" {print $3}')
      BLOCKS=$(printf '%s\n' "$INFO" | awk '$1=="Block" && $2=="count:" {print $3}')
      command -v resize2fs >/dev/null || fail 'Install resize2fs before growing ext4';;
    xfs)
      INFO=$(xfs_info "$P") || fail 'Cannot inspect XFS geometry'
      printf '%s\n' "$INFO" | grep -Eq '^log[[:space:]]*=[[:space:]]*internal[[:space:]]' || fail 'External XFS logs need manual allocation'
      printf '%s\n' "$INFO" | grep -Eq '^realtime[[:space:]]*=[[:space:]]*none[[:space:]]' || fail 'XFS realtime devices need manual allocation'
      BS=$(printf '%s\n' "$INFO" | awk '/^data[[:space:]]/ {for(i=1;i<=NF;i++) if($i ~ /^bsize=/) {sub(/^bsize=/,"",$i); print $i}}')
      BLOCKS=$(printf '%s\n' "$INFO" | awk '/^data[[:space:]]/ {for(i=1;i<=NF;i++) if($i ~ /^blocks=/) {sub(/^blocks=/,"",$i); sub(/,$/,"",$i); print $i}}')
      command -v xfs_growfs >/dev/null || fail 'Install xfs_growfs before growing XFS';;
    *) fail 'Online growth supports mounted ext4 or XFS filesystems';;
  esac
  STAT=$(stat -f -c '%S|%b|%f|%a' "$P") || fail 'Cannot read filesystem usage'
  TOPO=$(lsblk -snbpP -o KNAME,TYPE,SIZE,SERIAL,WWN,PARTUUID,START,TRAN "$D") || fail 'Cannot inspect parent topology'
  ANCESTRY=$(lsblk -snrpo KNAME,TYPE "$D") || fail 'Cannot resolve physical parent'
  PD=$(printf '%s\n' "$ANCESTRY" | awk '$2=="disk" {print $1}')
  [ -n "$PD" ] || fail 'Physical parent is missing'
  PARENT_SYS=$(readlink -f /sys/class/block/${PD##*/}/device) || fail 'Cannot inspect physical transport'
  case "$PARENT_SYS" in ''|*/virtual/*) fail 'Virtual or network-backed parent disks need manual allocation';; esac
  printf 'FS %s|%s|%s|%s|%s|%s|%s|%s|%s|%s\nUSAGE %s|%s\nTOPO-%s\n%s\nENDTOPO\n' "$ROLE" "$P" "$D" "$U" "$TYPE" "$BS" "$BLOCKS" "$MM" "$STABLE" "$DM" "$ROLE" "$STAT" "$ROLE" "$TOPO"
}
fs_facts os /
MOUNT_POINTS=$(findmnt -rn -o TARGET) || fail 'Cannot inspect installed storage mounts'
for candidate in /mnt/longhorn-os /var/lib/longhorn; do
  if printf '%s\n' "$MOUNT_POINTS" | grep -Fxq "$candidate"; then
    FS_ROOT=$(findmnt -rn --mountpoint "$candidate" -o FSROOT) || fail 'Cannot inspect installer filesystem root'
    # Relocated runtime is an OS-backed bind mount, not another growable LV.
    # Do not let that hide the independent OS allocation or new data mount.
    if [ "$FS_ROOT" = / ]; then fs_facts longhorn "$candidate"; break; fi
  fi
done
echo END
'''.replace('__SNAPSHOT__', SNAPSHOT)


def _integer(value):
    try:
        number = Decimal(str(value).strip())
        if not number.is_finite() or number != int(number) or number < 0:
            raise ValueError()
        return int(number)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise ValueError('Storage sizes are incomplete; no change is available') from None


def _section(text, name, end=None):
    try:
        return text.split(name + '\n', 1)[1].split('\n' + (end or 'END' + name), 1)[0]
    except IndexError:
        raise ValueError('Storage inventory is incomplete') from None


def parse(text):
    if 'END' not in text.splitlines() or any(line.startswith('ERROR ') for line in text.splitlines()):
        raise ValueError(next((line[6:] for line in text.splitlines() if line.startswith('ERROR ')), 'Storage inventory is incomplete'))
    try:
        lvs = json.loads(_section(text, 'LVS'))['report'][0]['seg']
        vgs = json.loads(_section(text, 'VGS'))['report'][0]['vg']
        pvs = json.loads(_section(text, 'PVS'))['report'][0]['pv']
        if len(vgs) != 1 or len(pvs) != 1 or not lvs:
            raise ValueError('Automatic growth requires one local physical volume in the OS volume group')
        vg, pv = vgs[0], pvs[0]
        extent = _integer(vg['vg_extent_size']); total = _integer(vg['vg_extent_count']); free = _integer(vg['vg_free_count'])
        if not extent or free > total or _integer(pv['pv_pe_count']) != total or _integer(pv['pv_pe_alloc_count']) != total - free:
            raise ValueError('Physical and logical free-space inventories disagree')
        for name in (vg['vg_uuid'], pv['pv_uuid']):
            if not re.fullmatch(r'[A-Za-z0-9-]{8,64}', name): raise ValueError('Stable LVM identities are missing')
        if pv['vg_uuid'] != vg['vg_uuid'] or not re.fullmatch(r'/dev/[A-Za-z0-9_.-]+', pv['pv_name']):
            raise ValueError('The physical volume identity is ambiguous')
        volumes = {}
        for row in lvs:
            uid = row['lv_uuid']
            if (row['vg_uuid'] != vg['vg_uuid'] or row['segtype'] != 'linear' or not row['lv_attr'].startswith('-')
                    or not re.fullmatch(r'[A-Za-z0-9-]{8,64}', uid) or not re.fullmatch(r'[A-Za-z0-9+_.-]{1,64}', row['lv_name'])
                    or not re.fullmatch(re.escape(pv['pv_name']) + r'\([0-9]+\)', row['devices'])):
                raise ValueError('Thin, snapshot, RAID, encrypted or multi-device layouts need manual allocation')
            size = _integer(row['lv_size'])
            if not size or size % extent: raise ValueError('Logical volume size is not extent-aligned')
            if uid in volumes and volumes[uid] != {**row, 'size_bytes':size}:
                # Multiple linear segments are safe only with the same volume
                # identity and device; metadata remains in the snapshot hash.
                previous = volumes[uid]
                if any(previous[key] != row[key] for key in row if key != 'devices'):
                    raise ValueError('Logical volume identity is inconsistent')
            volumes[uid] = {**row, 'size_bytes':size}
        if sum(row['size_bytes'] for row in volumes.values()) != (total - free) * extent:
            raise ValueError('Hidden or unaccounted allocation prevents automatic growth')
        result = {'vg':vg['vg_name'], 'vg_uuid':vg['vg_uuid'], 'pv':pv['pv_name'], 'pv_uuid':pv['pv_uuid'],
                  'extent_bytes':extent, 'total_bytes':total*extent, 'free_bytes':free*extent, 'volumes':list(volumes.values()), 'areas':[]}
        for line in text.splitlines():
            if not line.startswith('FS '): continue
            role, point, device, uuid, fs, bs, blocks, mm, stable, dm = line[3:].split('|')
            matching = [row for row in volumes.values() if str(row['lv_kernel_major']) + ':' + str(row['lv_kernel_minor']) == mm]
            if len(matching) != 1 or role not in ('os', 'longhorn') or point not in ('/', '/var/lib/longhorn', '/mnt/longhorn-os'):
                raise ValueError('Filesystem ownership cannot be verified')
            lv = matching[0]
            expected_dm = 'LVM-' + vg['vg_uuid'].replace('-', '') + lv['lv_uuid'].replace('-', '')
            if (dm != expected_dm or stable != '/dev/disk/by-id/dm-uuid-' + dm or fs not in ('ext4', 'xfs')
                    or not re.fullmatch(r'/dev/[A-Za-z0-9_.-]+', device) or not re.fullmatch(r'[A-Za-z0-9-]{4,64}', uuid)
                    or role == 'longhorn' and (point == '/' or lv['lv_name'] != 'longhorn') or role == 'os' and point != '/'):
                raise ValueError('Only the existing installer OS and dedicated Longhorn logical volumes can be adjusted')
            topology = _section(text, 'TOPO-' + role, 'ENDTOPO')
            parents = [dict(field.split('=', 1) for field in shlex.split(row)) for row in topology.splitlines()]
            if (not parents or any(row['TYPE'] not in ('disk', 'part', 'lvm') for row in parents)
                    or sum(row['TYPE'] == 'disk' for row in parents) != 1 or sum(row['TYPE'] == 'lvm' for row in parents) != 1
                    or pv['pv_name'] not in [row['KNAME'] for row in parents]
                    or any(row['TYPE'] == 'disk' and (row['TRAN'] not in ('sata','ata','sas','nvme') or not (row['SERIAL'] or row['WWN'])) for row in parents)):
                raise ValueError('Only a local disk or partition below the logical volume is supported')
            bs, blocks = _integer(bs), _integer(blocks)
            usage = next(row[len('USAGE '+role+'|'):] for row in text.splitlines() if row.startswith('USAGE '+role+'|'))
            stat_bs, stat_blocks, available, usable = map(_integer, usage.split('|'))
            if not bs or not blocks or bs * blocks > lv['size_bytes'] or not stat_bs or not (usable <= available <= stat_blocks):
                raise ValueError('Filesystem geometry or usage is inconsistent')
            result['areas'].append({'role':role, 'name':'Host OS' if role == 'os' else 'Longhorn', 'mountpoint':point,
                'device':device, 'stable_device':stable, 'lv_uuid':lv['lv_uuid'], 'lv_name':lv['lv_name'], 'fs_uuid':uuid, 'filesystem':fs,
                'size_bytes':lv['size_bytes'], 'fs_bytes':bs*blocks, 'block_bytes':bs, 'used_bytes':(stat_blocks-available)*stat_bs,
                'reserved_bytes':(available-usable)*stat_bs, 'minimum_bytes':lv['size_bytes'], 'topology':topology})
        if len([a for a in result['areas'] if a['role'] == 'os']) != 1 or len({a['lv_uuid'] for a in result['areas']}) != len(result['areas']):
            raise ValueError('The OS and Longhorn allocation could not be distinguished')
        result['metadata_hash'] = hashlib.sha256((_section(text, 'LVS')+'\n'+_section(text, 'VGS')+'\n'+_section(text, 'PVS')).encode()).hexdigest()
        result['inspection_hash'] = hashlib.sha256(('\n'.join(line for line in text.splitlines() if not line.startswith('USAGE '))+'\n').encode()).hexdigest()
        return result
    except (KeyError, IndexError, TypeError, StopIteration, json.JSONDecodeError):
        raise ValueError('Storage inventory is incomplete') from None


def _host(node):
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,251}', str(node or '')):
        raise ValueError('Choose a host')
    if platform(True).get('harvester'):
        raise ValueError('Harvester owns host allocation; use its disk management')
    obj = kget('/api/v1/nodes/' + node)
    if (not isinstance(obj, dict) or obj.get('metadata', {}).get('deletionTimestamp')
            or not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in obj.get('status', {}).get('conditions', []))):
        raise ValueError('The host must be ready before adjusting storage')
    uid, boot = obj.get('metadata', {}).get('uid'), obj.get('status', {}).get('nodeInfo', {}).get('bootID')
    if not uid or not boot: raise ValueError('Host identity is unavailable')
    return {'node_uid':uid, 'boot_id':boot}


def inspect(node):
    host = _host(node)
    out, _ = HOST.run(node, INSPECT, timeout=90)
    facts = parse(out)
    if _host(node) != host: raise ValueError('The host restarted during inspection; check again')
    return {**facts, **host, 'node':node}


def _context(facts):
    clean = copy.deepcopy(facts)
    for area in clean['areas']:
        for key in ('used_bytes', 'reserved_bytes'): area.pop(key)
    return clean


def _saved(operation_id):
    item = next((i for i in ops.stored() if i.get('id') == operation_id and i.get('kind') == KIND), None)
    if not item: raise ValueError('Storage allocation job not found')
    return item


def _pending(node, exclude=''):
    return next((i for i in ops.stored() if i.get('kind') == KIND and i.get('id') != exclude and i.get('ref', {}).get('node') == node
                 and i.get('ref', {}).get('phase') not in ('complete', 'cancelled')), None)


def review(body):
    node, role = body.get('node'), body.get('role', 'os')
    facts = inspect(node)
    with ops.transaction():
        pending = _pending(node)
        if pending: return {'operation':public(pending)}
    area = next((a for a in facts['areas'] if a['role'] == role), None)
    if not area: raise ValueError('No separate installer Longhorn filesystem was found on this host')
    size_gib = body.get('size_gib')
    if size_gib is None: return {**facts, 'role':role, 'size_gib':None}
    if isinstance(size_gib, bool) or not isinstance(size_gib, int) or size_gib <= 0:
        raise ValueError('Enter the new total size as whole GiB')
    target = ((size_gib * GiB + facts['extent_bytes'] - 1) // facts['extent_bytes']) * facts['extent_bytes']
    if target <= area['size_bytes']:
        raise ValueError('Online adjustment only grows a filesystem. Reducing the OS needs offline maintenance; reclaiming Longhorn space needs evacuation and recreation.')
    if target - area['size_bytes'] > facts['free_bytes']:
        raise ValueError('There is not enough unallocated space in this volume group. Existing OS or Longhorn space is not reclaimed automatically.')
    request_id = secrets.token_hex(12)
    cfg = {'node':node, 'role':role, 'size_gib':size_gib, 'request_id':request_id}
    return {**facts, **cfg, 'target_bytes':target, 'remaining_bytes':facts['free_bytes'] - target + area['size_bytes'],
            'capacity_token':REVIEW.issue(cfg, _context(facts))}


def public(item):
    ref = item['ref']
    return {**ops._public(item), 'node':ref['node'], 'phase':ref['phase'], 'role':ref['role'],
            'target_bytes':ref['target_bytes'], 'mountpoint':ref['area']['mountpoint'], 'device':ref['area']['device']}


def start(body):
    cfg = {key:body.get(key) for key in ('node', 'role', 'size_gib', 'request_id', 'capacity_token')}
    if body.get('confirm_capacity') is not True or not re.fullmatch(r'[a-f0-9]{24}', str(cfg['request_id'])):
        raise ValueError('Review and confirm the allocation first')
    with ops.transaction():
        for item in ops.stored():
            if item.get('kind') == KIND and item.get('ref', {}).get('request_id') == cfg['request_id']:
                if item['ref'].get('approval') != cfg['capacity_token']: raise ValueError('This request belongs to another review')
                return public(item)
        if _pending(cfg['node']): raise ValueError('Inspect the existing storage allocation job first')
        facts = inspect(cfg['node'])
        if not REVIEW.valid(cfg, _context(facts)): raise ValueError('Storage or host identity changed, or the review expired. Review again.')
        area = next(a for a in facts['areas'] if a['role'] == cfg['role'])
        target = ((cfg['size_gib'] * GiB + facts['extent_bytes'] - 1) // facts['extent_bytes']) * facts['extent_bytes']
        if not area['size_bytes'] < target <= area['size_bytes'] + facts['free_bytes']:
            raise ValueError('The reviewed growth no longer fits')
        ref = {**facts, 'area':area, 'role':cfg['role'], 'target_bytes':target, 'phase':'queued', 'request_id':cfg['request_id'],
               'approval':cfg['capacity_token'], 'job':'homestead-allocation-' + cfg['request_id'], 'namespace':NS}
        item = ops.start(KIND, 'Grow ' + area['name'] + ' on ' + cfg['node'], {'kind':'Node', 'name':cfg['node']},
                         '/nodes?node=' + cfg['node'] + '&section=storage', ref, 'Allocation approved; no filesystem has changed yet')
        return public(_saved(item['id']))


def _script(ref):
    area = ref['area']
    target = ref['target_bytes']
    if target % area['block_bytes']:
        raise ValueError('The target is not aligned to filesystem blocks')
    script = r'''set -eu
export LC_ALL=C
fail() { echo "ERROR $*"; exit 1; }
inspect_all() {
__INSPECT__
}
[ "$(cat /proc/sys/kernel/random/boot_id)" = __BOOT__ ] || fail 'Host restarted after review'
B=__STABLE__; P=__POINT__; U=__FSUUID__; TARGET=__TARGET__; PV=__PV__
D=$(readlink -f "$B") || fail 'Cannot resolve reviewed logical volume'
[ -b "$D" ] || fail 'Reviewed logical volume is missing'
LV_NAME=$(cat /sys/class/block/${D##*/}/dm/name) || fail 'Cannot read the reviewed logical volume name'
case "$LV_NAME" in ''|*[!A-Za-z0-9+_.-]*) fail 'Logical volume name is invalid';; esac
L="/dev/mapper/$LV_NAME"
exec 8<"$D" || fail 'Cannot pin the reviewed logical volume'
exec 7<"$P" || fail 'Cannot pin the mounted filesystem'
R=/var/lib/homestead/allocation
[ ! -L /var/lib/homestead ] && [ ! -L "$R" ] || fail 'Receipt directory is a symlink'
mkdir -p "$R"; chmod 700 "$R"
exec 9>"$R/vg-"__VGUUID__
flock -n 9 || fail 'Another allocation change holds this volume group'
J="$R/"__REQUEST__
[ ! -e "$J" ] && [ ! -L "$J" ] || fail 'This attempt already ran; inspect its saved outcome'
FRESH=$(inspect_all) || fail 'Fresh storage inspection failed'
HASH=$(printf '%s\n' "$FRESH" | sed '/^USAGE /d' | sha256sum | cut -d' ' -f1)
[ "$HASH" = __HASH__ ] || fail 'Allocation, topology or filesystem identity changed after review'
mount_identity() {
  CURRENT=$(readlink -f "$B") || fail 'Cannot resolve stable volume identity'
  [ "$CURRENT" = "$D" ] || fail 'Logical volume device changed'
  [ "$(readlink -f "$L")" = "$D" ] || fail 'Logical volume name points to another device'
  [ "$(cat /sys/class/block/${D##*/}/dm/uuid)" = __DM__ ] || fail 'Logical volume UUID changed'
  [ "$(blkid -s UUID -o value "$D")" = "$U" ] || fail 'Filesystem UUID changed'
  SOURCE=$(findmnt -rn --mountpoint "$P" -o SOURCE) || fail 'The filesystem was unmounted'
  SOURCE=$(readlink -f "$SOURCE") || fail 'Cannot resolve mounted source'
  [ "$SOURCE" = "$D" ] || fail 'Mount source changed'
  [ "$(findmnt -rn --mountpoint "$P" -o FSROOT)" = / ] || fail 'Mount root changed'
  [ "$(findmnt -rn --mountpoint "$P" -o FSTYPE)" = __TYPE__ ] || fail 'Filesystem type changed'
  OPTIONS=$(findmnt -rn --mountpoint "$P" -o OPTIONS) || fail 'Cannot inspect mount options'
  case ",$OPTIONS," in *,rw,*) ;; *) fail 'Filesystem became read-only';; esac
}
mount_identity
(set -C; printf 'started\n' > "$J") || fail 'A previous attempt exists'
sync "$J"
echo 'Growing the logical volume to the approved total size'
__EXTEND__
[ "$(blockdev --getsize64 /proc/self/fd/8)" = "$TARGET" ] || fail 'Logical volume did not reach the approved size; inspect it before continuing'
printf 'volume-grown\n' > "$J.next"; sync "$J.next"; mv "$J.next" "$J"; sync "$R"
mount_identity
echo 'Growing the mounted filesystem'
__GROW__
echo 'Verifying the filesystem and protected allocations'
AFTER=$(inspect_all) || fail 'Growth ran but final inspection failed; inspect the saved job'
LINE=$(printf '%s\n' "$AFTER" | grep '^FS '__ROLE__'|') || fail 'The resized filesystem is absent'
BLOCKS=$(printf '%s\n' "$LINE" | cut -d'|' -f7)
[ "$BLOCKS" = __BLOCKS__ ] || fail 'Filesystem did not reach the approved block count; logical volume may already be larger'
printf 'complete\n' > "$J.next"; sync "$J.next"; mv "$J.next" "$J"; sync "$R"
echo 'Logical volume and filesystem reached the approved size'
'''
    extend = ('[ "$(blockdev --getsize64 /proc/self/fd/8)" = "$TARGET" ]' if ref.get('filesystem_only') else
              'lvextend --autobackup y --extents ' + str(target // ref['extent_bytes']) + ' "$L" "$PV"')
    grow = ('resize2fs /proc/self/fd/8 ' + shlex.quote(str(target // 1024) + 'K') if area['filesystem'] == 'ext4' else
            'xfs_growfs -D ' + str(target // area['block_bytes']) + ' /proc/self/fd/7')
    values = {'__BOOT__':ref['boot_id'], '__STABLE__':area['stable_device'], '__POINT__':area['mountpoint'], '__FSUUID__':area['fs_uuid'],
              '__TARGET__':str(target), '__PV__':ref['pv'], '__VGUUID__':ref['vg_uuid'], '__REQUEST__':ref['request_id'],
              '__HASH__':ref['inspection_hash'], '__DM__':area['stable_device'].rsplit('dm-uuid-', 1)[1], '__TYPE__':area['filesystem'],
              '__ROLE__':ref['role'], '__BLOCKS__':str(target // area['block_bytes'])}
    script = script.replace('__INSPECT__', INSPECT).replace('__EXTEND__', extend).replace('__GROW__', grow)
    for key, value in values.items(): script = script.replace(key, shlex.quote(value))
    # Both tools can resolve mount-table paths internally even when given an
    # fd path. Keep the reviewed mounts in our own namespace, unaffected by
    # later host mount propagation, and pin their device/directory as well.
    # https://github.com/tytso/e2fsprogs/blob/master/resize/online.c
    return 'exec unshare --mount --propagation private -- sh -c ' + shlex.quote(script)


def _job_body(ref):
    script = _script(ref)
    return {'apiVersion':'batch/v1', 'kind':'Job', 'metadata':{'name':ref['job'], 'namespace':NS, 'labels':{LABEL:'true'},
            'annotations':{LABEL:hashlib.sha256(script.encode()).hexdigest()}}, 'spec':{'backoffLimit':0, 'activeDeadlineSeconds':900,
            'ttlSecondsAfterFinished':604800, 'template':{'metadata':{'labels':{LABEL:'true'}}, 'spec':{
            'nodeName':ref['node'], 'hostPID':True, 'hostIPC':True, 'hostNetwork':True, 'automountServiceAccountToken':False,
            'restartPolicy':'Never', 'tolerations':[{'operator':'Exists'}], 'containers':[{'name':'grow', 'image':HOST.IMAGE,
            'command':HOST.HOST + [script], 'securityContext':{'privileged':True},
            'resources':{'requests':{'cpu':'10m','memory':'64Mi'},'limits':{'memory':HOST.MEMORY_LIMIT}}}]}}}}


class HelperMissing(ValueError):
    pass


def _job(ref, create=False):
    path = f'/apis/batch/v1/namespaces/{NS}/jobs/{ref["job"]}'
    try: obj = kget(path)
    except urllib.error.HTTPError as error:
        if error.code != 404: raise
        obj = None
    expected = _job_body(ref)
    if obj is None:
        if not create: raise HelperMissing('The helper is missing. Growth is not repeated automatically; recheck its saved outcome.')
        try: obj = ksend('POST', path.rsplit('/',1)[0], expected)
        except urllib.error.HTTPError as error:
            if error.code != 409: raise
            obj = kget(path)
    meta, spec = obj.get('metadata', {}), obj.get('spec', {})
    actual, target = spec.get('template', {}).get('spec', {}), expected['spec']['template']['spec']
    if (not meta.get('uid') or ref.get('job_uid') and meta['uid'] != ref['job_uid']
            or meta.get('annotations', {}).get(LABEL) != expected['metadata']['annotations'][LABEL]
            or spec.get('backoffLimit') != 0 or spec.get('activeDeadlineSeconds') != 900
            or any(actual.get(key) != target[key] for key in ('nodeName','hostPID','hostIPC','hostNetwork','automountServiceAccountToken','restartPolicy'))
            or len(actual.get('containers', [])) != 1 or actual.get('initContainers') or actual.get('volumes')
            or any(actual['containers'][0].get(key) != target['containers'][0][key] for key in ('name','image','command','securityContext'))
            or any(actual['containers'][0].get(key) for key in ('args','env','envFrom','volumeMounts'))):
        raise ValueError('Helper identity or configuration changed; no work is repeated')
    ref['job_uid'] = meta['uid']
    return obj


def _ended(ref):
    """How the helper ended, or None while it may still run. Kubernetes
    removes a finished helper after 7 days (ttlSecondsAfterFinished): then
    the end this job saw and kept (progress) stands in for it, for that same
    helper only. A helper gone with no end seen may still be running."""
    try:
        status = _job(ref).get('status', {})
    except HelperMissing:
        seen = ref.get('helper_end') or {}
        if seen.get('outcome') and seen.get('job') == ref['job'] and seen.get('job_uid') and seen['job_uid'] == ref.get('job_uid'):
            return seen['outcome']
        raise
    return 'failed' if status.get('failed') else 'succeeded' if status.get('succeeded') else None


def _verify_result(ref, facts):
    if facts['node_uid'] != ref['node_uid'] or facts['vg_uuid'] != ref['vg_uuid'] or facts['pv_uuid'] != ref['pv_uuid']:
        raise ValueError('Host or volume group identity changed; inspect before continuing')
    area = next((a for a in facts['areas'] if a['lv_uuid'] == ref['area']['lv_uuid']), None)
    if not area or any(area[key] != ref['area'][key] for key in ('fs_uuid','filesystem','mountpoint','role','stable_device','block_bytes')):
        raise ValueError('The resized filesystem identity changed')
    old = {v['lv_uuid']:v for v in ref['volumes']}
    new = {v['lv_uuid']:v for v in facts['volumes']}
    if old.keys() != new.keys() or any(new[uid] != before for uid, before in old.items() if uid != area['lv_uuid']):
        raise ValueError('Other logical volumes changed; inspect the allocation before continuing')
    if area['size_bytes'] not in (ref['area']['size_bytes'], ref['target_bytes']):
        raise ValueError('The logical volume has an unexpected size; no automatic recovery is available')
    return area


def progress(item):
    ref = item['ref']
    try:
        if ref['phase'] == 'complete': return 'succeeded', 100, 'Logical volume and filesystem reached the approved size'
        if ref['phase'] == 'queued':
            if _context(inspect(ref['node'])) != _context(inspect_keys(ref)):
                raise ValueError('Storage changed since review; no growth was dispatched')
            ref['phase'] = 'dispatching'; ops.checkpoint(item)
        if ref['phase'] == 'dispatching':
            # Save intent first; a lost helper is never recreated by progress.
            ref['phase'] = 'growing'; ops.checkpoint(item)
            job = _job(ref, create=True); ops.checkpoint(item)
        else: job = _job(ref)
        status = job.get('status', {})
        if status.get('failed') or status.get('succeeded'):
            # Kept with the job: recheck still knows the helper stopped once
            # Kubernetes has removed it.
            ref['helper_end'] = {'job':ref['job'], 'job_uid':ref['job_uid'],
                                 'outcome':'failed' if status.get('failed') else 'succeeded'}
        if status.get('failed'): raise ValueError('Growth stopped. The logical volume may already be larger; recheck this job before any further allocation.')
        if not status.get('succeeded'): return 'running', 50, 'Growing the logical volume and mounted filesystem; existing files are retained'
        facts = inspect(ref['node']); area = _verify_result(ref, facts)
        if area['size_bytes'] != ref['target_bytes'] or area['fs_bytes'] != ref['target_bytes']:
            raise ValueError('Growth is incomplete. Recheck this job to inspect the logical volume and filesystem separately.')
        ref['phase'] = 'complete'; ops.checkpoint(item)
        return 'succeeded', 100, 'Logical volume and filesystem reached the approved size; no restart was required'
    except (ValueError, urllib.error.HTTPError) as error:
        return 'failed', item.get('progress', 0), str(error)


def inspect_keys(ref):
    """The immutable inspection portion persisted alongside job state."""
    return {key:ref[key] for key in ('vg','vg_uuid','pv','pv_uuid','extent_bytes','total_bytes','free_bytes','volumes','areas',
                                    'metadata_hash','inspection_hash','node_uid','boot_id','node')}


def status(operation_id):
    ops.advance_soon()
    with ops.transaction(): return public(_saved(operation_id))


def recheck(operation_id):
    with ops.transaction():
        item = _saved(operation_id); ref = item['ref']
        if item.get('status') not in ops.TERMINAL: raise ValueError('Wait for the current helper to finish before rechecking')
        if ref['phase'] == 'queued':
            ref['phase'] = 'cancelled'
            item.update(status='cancelled', progress=0, message='No helper was dispatched. Create a fresh allocation review.')
            ops.checkpoint(item)
            return {'operation':public(item), 'detail':'No helper was dispatched. Create a fresh allocation review.'}
        try: ended = _ended(ref)
        except ValueError:
            # An absent or altered helper cannot be assumed stopped.
            raise ValueError('The original helper cannot be verified. Inspect it and the host receipt before recovery; automatic recovery is blocked.') from None
        if not ended:
            raise ValueError('The original helper may still be running')
        facts = inspect(ref['node']); area = _verify_result(ref, facts)
        if area['size_bytes'] == ref['target_bytes'] and area['fs_bytes'] == ref['target_bytes']:
            ref['phase'] = 'complete'; item.update(status='succeeded', progress=100, message='Recheck confirmed the logical volume and filesystem reached the approved size')
            ops.checkpoint(item)
            return {'operation':public(item)}
        if area['size_bytes'] != ref['target_bytes']:
            ref['phase'] = 'cancelled'
            item.update(status='cancelled', progress=0, message='No logical-volume growth was observed. Create a fresh allocation review.')
            ops.checkpoint(item)
            return {'operation':public(item), 'detail':'No LV growth was observed. Close this job and create a fresh allocation review.'}
        request_id = secrets.token_hex(12)
        cfg = {'operation_id':operation_id, 'request_id':request_id, 'action':'finish-filesystem'}
        return {**facts, **cfg, 'area':area, 'target_bytes':ref['target_bytes'], 'capacity_token':REVIEW.issue(cfg, _context(facts))}


def finish(body):
    cfg = {key:body.get(key) for key in ('operation_id','request_id','action','capacity_token')}
    if body.get('confirm_capacity') is not True or cfg['action'] != 'finish-filesystem' or not re.fullmatch(r'[a-f0-9]{24}', str(cfg['request_id'])):
        raise ValueError('Review finishing the filesystem first')
    with ops.transaction():
        item = _saved(cfg['operation_id']); ref = item['ref']
        if ref['request_id'] == cfg['request_id']:
            if ref['approval'] != cfg['capacity_token']: raise ValueError('This is another recovery review')
            return public(item)
        if item.get('status') not in ops.TERMINAL: raise ValueError('The current allocation helper is still active')
        if not _ended(ref): raise ValueError('The original helper may still be running')
        facts = inspect(ref['node']); area = _verify_result(ref, facts)
        if not REVIEW.valid(cfg, _context(facts)) or area['size_bytes'] != ref['target_bytes'] or area['fs_bytes'] >= ref['target_bytes']:
            raise ValueError('Recovery needs a fresh review of the same incomplete filesystem')
        ref.setdefault('attempts', []).append({'job':ref['job'], 'job_uid':ref.get('job_uid'), 'message':item.get('message')})
        ref.update(facts, area=area, request_id=cfg['request_id'], approval=cfg['capacity_token'], filesystem_only=True,
                   job='homestead-allocation-' + cfg['request_id'], phase='queued')
        ref.pop('job_uid', None)
        item.update(status='queued', progress=0, message='Finishing filesystem growth; no additional extents will be allocated')
        ops.checkpoint(item)
        return public(item)


def protect_mutations(disks):
    previous = disks.mutation_scope
    @contextmanager
    def scope(node, disk, device):
        with ops.transaction():
            if _pending(node): raise ValueError('This host has a storage allocation job. Finish or recheck it before changing disks.')
            with previous(node, disk, device): yield
    disks.mutation_scope = scope


def logs(item):
    ref = item['ref']
    obj = kget(f'/api/v1/namespaces/{NS}/pods?labelSelector=job-name%3D' + ref['job'])
    if not isinstance(obj.get('items'), list) or (obj.get('metadata') or {}).get('continue'): raise ValueError('Helper log inventory is incomplete')
    return [{'title':'Storage allocation', 'text':read_log(f'/api/v1/namespaces/{NS}/pods/{p["metadata"]["name"]}/log?tailLines=100')}
            for p in obj['items'] if any(o.get('uid') == ref.get('job_uid') for o in p.get('metadata', {}).get('ownerReferences', []))]


def register(operations):
    operations.RESOLVERS[KIND] = progress
    operations.LOGGERS[KIND] = logs
    operations.CANCEL_BLOCKS[KIND] = lambda item: 'Storage growth cannot be interrupted safely. Let the helper finish, then recheck its saved outcome.'
    operations.VIEWS[KIND] = lambda item, out: out.update(cancellable=False, cleanable=False, allocation=True)


ROUTES = {
    ('POST','/api/disks/allocation/review'):('admin', lambda request: review(request.body)),
    ('POST','/api/disks/allocation/start'):('admin', lambda request: start(request.body)),
    ('POST','/api/disks/allocation/recheck'):('admin', lambda request: recheck(request.body.get('id'))),
    ('POST','/api/disks/allocation/finish'):('admin', lambda request: finish(request.body)),
    ('GET','/api/disks/allocation/status'):('admin', lambda request: status(request.query.get('id',[''])[0])),
}
