"""Evacuate a dedicated V1 disk, then separately approve preparing it for V2.

Longhorn owns replica replacement. Erasing is a one-shot, pinned Kubernetes
Job, with a host receipt written before wipefs. A lost Job never starts a
second erase. Unknown inventories and identities always stop the workflow.
"""
import copy
import hashlib
import functools
from contextlib import contextmanager
import json
import re
import secrets
import shlex
import urllib.error
from urllib.parse import quote

import homestead_capacity_review as REVIEW
import homestead_hostrun as HOST
import homestead_disk_setup as SETUP

KIND = 'disk-v2-convert'
LABEL = 'homestead.io/disk-v2-convert'
LH = '/apis/longhorn.io/v1beta2/namespaces/longhorn-system'
GiB = 1024 ** 3
kget = ksend = platform = ops = read_log = None
NS = 'lab'


def bind(read, send, detect, operations, namespace, logs):
    global kget, ksend, platform, ops, NS, read_log
    kget, ksend, platform, ops, NS, read_log = read, send, detect, operations, namespace, logs


def _get(path, optional=False):
    try:
        obj = kget(path)
    except urllib.error.HTTPError as error:
        if optional and error.code == 404:
            return None
        raise ValueError(f'Disk preparation inventory is unavailable (HTTP {error.code}); nothing is assumed empty') from error
    if not isinstance(obj, dict):
        raise ValueError('Disk preparation inventory is incomplete')
    return obj


def _items(path):
    obj = _get(path)
    if not isinstance(obj.get('items'), list):
        raise ValueError('Disk preparation inventory is incomplete')
    return obj['items']


def _name(value):
    value = str(value or '')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', value):
        raise ValueError('Choose a host and a Longhorn disk')
    return value


def _ready(obj, condition='Ready'):
    return not obj.get('metadata', {}).get('deletionTimestamp') and any(
        c.get('type') == condition and c.get('status') == 'True' for c in obj.get('status', {}).get('conditions', []))


def _setting(name):
    value = _get(LH + '/settings/' + name).get('value')
    if not isinstance(value, str) or not value:
        raise ValueError(f'Longhorn setting {name} could not be verified')
    return value


def _upgrade_idle():
    control = _get(LH + '/instancemanagerupgradecontrols/longhorn-instance-manager-upgrade-control', True)
    if control:
        status = control.get('status') or {}
        if status.get('currentNode') or any(n.get('state') != 'completed' for n in (status.get('nodes') or {}).values()):
            raise ValueError('Finish the V2 instance-manager upgrade before changing disks')


def _inventory(node, disk):
    node, disk = _name(node), _name(disk)
    if platform(True).get('harvester'):
        raise ValueError('Harvester owns disk preparation; use its disk management')
    _upgrade_idle()
    kube = {n['metadata']['name']: n for n in _items('/api/v1/nodes')}
    lhs = {n['metadata']['name']: n for n in _items(LH + '/nodes')}
    host, lh = kube.get(node), lhs.get(node)
    if not host or not lh or not _ready(host) or host.get('spec', {}).get('unschedulable') or not _ready(lh) or not lh.get('spec', {}).get('allowScheduling'):
        raise ValueError('The host must be ready and allow workload scheduling')
    if not host.get('metadata', {}).get('uid') or not host.get('status', {}).get('nodeInfo', {}).get('bootID'):
        raise ValueError('Host identity could not be verified')
    spec = (lh.get('spec', {}).get('disks') or {}).get(disk)
    status = (lh.get('status', {}).get('diskStatus') or {}).get(disk) or {}
    if not spec or spec.get('diskType', 'filesystem') != 'filesystem' or not _ready({'status': status}):
        raise ValueError('Choose a healthy V1 filesystem disk')
    if not status.get('diskUUID'):
        raise ValueError('Longhorn disk identity could not be verified')
    path = spec.get('path', '')
    if not re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+', path) or any(x in ('.', '..') for x in path.split('/')):
        raise ValueError('This mount path cannot be prepared automatically')
    managers = _items(LH + '/instancemanagers')
    if _setting('v2-data-engine') != 'true' or not any(
            m.get('spec', {}).get('nodeID') == node and m.get('spec', {}).get('dataEngine') == 'v2'
            and m.get('spec', {}).get('type') == 'aio' and m.get('status', {}).get('currentState') == 'running'
            and not m.get('metadata', {}).get('deletionTimestamp') for m in managers):
        raise ValueError('Set up V2 on this host and wait for its instance manager before evacuating the disk')
    replicas = _items(LH + '/replicas')
    volumes = {v['metadata']['name']: v for v in _items(LH + '/volumes')}
    engines = _items(LH + '/engines')
    return {'node': node, 'disk': disk, 'host': host, 'lh': lh, 'spec': spec, 'status': status,
            'kube': kube, 'lhs': lhs, 'replicas': replicas, 'volumes': volumes, 'engines': engines}


def _on_disk(replica, inv):
    spec = replica.get('spec') or {}
    return (replica.get('metadata', {}).get('name') in (inv['status'].get('scheduledReplica') or {})
            or spec.get('nodeID') == inv['node'] and
            (spec.get('diskID') == inv['status']['diskUUID'] or spec.get('diskPath') == inv['spec']['path']))


def _rw(inv, volume):
    modes = {name: mode for e in inv['engines'] if e.get('spec', {}).get('volumeName') == volume
             and e.get('status', {}).get('currentState') == 'running' and not e.get('metadata', {}).get('deletionTimestamp')
             for name, mode in (e.get('status', {}).get('replicaModeMap') or {}).items()}
    return [r for r in inv['replicas'] if r.get('spec', {}).get('volumeName') == volume
            and not r.get('metadata', {}).get('deletionTimestamp') and not r.get('spec', {}).get('failedAt')
            and r.get('status', {}).get('currentState') == 'running' and modes.get(r['metadata']['name']) == 'RW']


def inspect_script(path):
    return r'''set -eu
P=__PATH__
fail() { echo "ERR $*"; exit 1; }
[ -d "$P" ] && [ ! -L "$P" ] || fail 'Longhorn mount is missing or is a symlink'
D=$(findmnt -rn --mountpoint "$P" -o SOURCE) || fail 'Longhorn must use the root of a dedicated mounted disk'
D=$(readlink -f "$D")
[ -b "$D" ] || fail 'Mount source is not a block device'
echo "DEVICE $D"
echo "SIZE $(blockdev --getsize64 "$D")"
echo "UUID $(blkid -s UUID -o value "$D")"
echo "FSTYPE $(blkid -s TYPE -o value "$D")"
echo "SERIAL $(lsblk -dnro SERIAL "$D")"
echo "WWN $(lsblk -dnro WWN "$D")"
echo "KIND $(lsblk -dnro TYPE "$D")"
[ "$(findmnt -rn --mountpoint "$P" -o FSROOT)" = / ] || fail 'A subdirectory or bind mount cannot be erased'
[ "$(lsblk -nrpo NAME "$D" | wc -l)" -eq 1 ] || fail 'Partitions or device-mapper children need manual preparation'
[ "$(findmnt -rn -S "$D" -o TARGET | wc -l)" -eq 1 ] || fail 'This filesystem has other mounts'
findmnt -rn -o TARGET | while read -r m; do case "$m" in "$P"/*) echo "ERR A nested mount uses the disk"; exit 1;; esac; done
B=${D##*/}
[ -z "$(ls /sys/class/block/$B/holders)" ] || fail 'Another device holds this disk'
for l in /dev/disk/by-id/*; do
  case "$l" in *-part*) continue;; esac
  [ "$(readlink -f "$l")" = "$D" ] && { echo "BYID $l"; break; }
done
[ -f "$P/longhorn-disk.cfg" ] && [ ! -L "$P/longhorn-disk.cfg" ] || fail 'Longhorn disk configuration is missing'
echo "CONFIG $(tr -d '\n' < "$P/longhorn-disk.cfg")"
# A dedicated Longhorn filesystem must not also hold unrelated user files.
find "$P" -mindepth 1 -maxdepth 1 ! -name replicas ! -name backing-images ! -name backing-images-disk ! -name longhorn-disk.cfg ! -name lost+found -print | grep -q . && fail 'Other files on this filesystem need to be moved first'
[ -f /etc/fstab ] && [ ! -L /etc/fstab ] || fail 'The host fstab is not a regular file'
awk -v p="$P" '$0 !~ /^[[:space:]]*#/ && $2==p && $4 ~ /x-systemd.automount/ {exit 1}' /etc/fstab || fail 'Disable the automatic mount before preparing this disk'
for c in wipefs flock findmnt blkid blockdev sha256sum; do command -v "$c" >/dev/null || fail "Host tool $c is missing"; done
echo END
'''.replace('__PATH__', shlex.quote(path))


def _host_facts(path, node):
    out, err = HOST.run(node, inspect_script(path), timeout=60)
    error = next((line[4:] for line in out.splitlines() if line.startswith('ERR ')), '')
    if error or 'END' not in out.splitlines():
        raise ValueError(error or 'Host disk inspection did not finish; ' + str(err or '')[:160])
    lines = {line.partition(' ')[0]: line.partition(' ')[2].strip() for line in out.splitlines()}
    device = SETUP._device(lines.get('DEVICE'))
    if lines.get('KIND') != 'disk' or lines.get('FSTYPE') not in ('ext4', 'xfs'):
        raise ValueError('Automatic conversion needs an ext4 or XFS filesystem on a dedicated whole disk')
    if path in SETUP.SYSTEM_POINTS or path.startswith(('/boot/', '/var/', '/usr/', '/home/', '/run/', '/etc/')):
        raise ValueError('System storage cannot be repurposed for V2')
    try:
        config = json.loads(lines['CONFIG'])
        size = int(lines['SIZE'])
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError('Physical disk or Longhorn identity could not be verified') from error
    by_id = lines.get('BYID', '')
    if not re.fullmatch(r'/dev/disk/by-id/[A-Za-z0-9_.:+@-]+', by_id) or not (lines.get('SERIAL') or lines.get('WWN')) or not lines.get('UUID') or size <= 0:
        raise ValueError('A stable device ID, hardware serial/WWN, size and filesystem UUID are required')
    return {'device': device, 'by_id': by_id, 'serial': lines.get('SERIAL', ''), 'wwn': lines.get('WWN', ''),
            'size_bytes': size, 'uuid': lines['UUID'], 'fstype': lines['FSTYPE'], 'mountpoint': path,
            'longhorn_uuid': config.get('diskUUID')}


def _soft(v, setting):
    option = v.get('spec', {}).get({'replica-soft-anti-affinity': 'replicaSoftAntiAffinity',
                                  'replica-disk-soft-anti-affinity': 'replicaDiskSoftAntiAffinity',
                                  'replica-zone-soft-anti-affinity': 'replicaZoneSoftAntiAffinity'}[setting], 'ignored')
    return option == 'enabled' if option in ('enabled', 'disabled') else _setting(setting) == 'true'


def _placements(inv):
    mine = [r for r in inv['replicas'] if _on_disk(r, inv)]
    listed = set(inv['status'].get('scheduledReplica') or {})
    if listed - {r['metadata']['name'] for r in mine}:
        raise ValueError('Longhorn lists replicas that cannot be inspected; wait before evacuating')
    if inv['status'].get('scheduledBackingImage'):
        raise ValueError('Remove or relocate backing-image copies from this disk in Longhorn first')
    rows, blockers, budget, scheduled, planned = [], [], {}, {}, {}
    empty_nodes = _setting('allow-empty-node-selector-volume') == 'true'
    empty_disks = _setting('allow-empty-disk-selector-volume') == 'true'
    minimum = int(_setting('storage-minimal-available-percentage'))
    over = int(_setting('storage-over-provisioning-percentage'))
    if not 0 <= minimum <= 100 or over <= 0:
        raise ValueError('Longhorn disk capacity policy could not be verified')
    for replica in sorted(mine, key=lambda r: (-int(r.get('spec', {}).get('volumeSize') or 0), r['metadata']['name'])):
        name = replica['spec'].get('volumeName', '')
        volume = inv['volumes'].get(name)
        if not volume:
            blockers.append(f'{name}: its volume cannot be inspected')
            continue
        vs, st = volume.get('spec') or {}, volume.get('status') or {}
        size = int(vs.get('size') or 0)
        if size <= 0 or vs.get('dataEngine', 'v1') != 'v1':
            blockers.append(f'{name}: its V1 volume size could not be verified')
        if vs.get('topologyRequirement') or vs.get('cloneMode') == 'linked-clone':
            blockers.append(f'{name}: topology-constrained or linked-clone volumes need separate disk planning')
        if st.get('state') != 'attached' or st.get('robustness') != 'healthy' or vs.get('migrationNodeID') or st.get('expansionRequired'):
            blockers.append(f'{name}: attach it and wait for healthy replicas, with no migration or expansion')
        rw = _rw(inv, name)
        if len(rw) < int(vs.get('numberOfReplicas') or 1):
            blockers.append(f'{name}: not all requested replicas are confirmed RW')
        others = [r for r in rw if not _on_disk(r, inv)]
        if not others:
            blockers.append(f'{name}: this disk holds its only healthy copy; add a healthy replica elsewhere first')
        held_nodes = {r['spec'].get('nodeID') for r in others}
        held_disks = {(r['spec'].get('nodeID'), r['spec'].get('diskID')) for r in others}
        held_nodes.update(n for n, d in planned.get(name, []))
        held_disks.update(planned.get(name, []))
        held_zones = {inv['kube'].get(n, {}).get('metadata', {}).get('labels', {}).get('topology.kubernetes.io/zone', '') for n in held_nodes}
        choices = []
        for node, lh in sorted(inv['lhs'].items()):
            kube = inv['kube'].get(node, {})
            ns = lh.get('spec') or {}
            if not _ready(kube) or kube.get('spec', {}).get('unschedulable') or not _ready(lh) or not ns.get('allowScheduling'):
                continue
            if not set(vs.get('nodeSelector') or []) <= set(ns.get('tags') or []) or not vs.get('nodeSelector') and ns.get('tags') and not empty_nodes:
                continue
            if vs.get('dataLocality') == 'strict-local' and node != st.get('currentNodeID'):
                continue
            if node in held_nodes and not _soft(volume, 'replica-soft-anti-affinity'):
                continue
            zone = kube.get('metadata', {}).get('labels', {}).get('topology.kubernetes.io/zone', '')
            if zone in held_zones and not _soft(volume, 'replica-zone-soft-anti-affinity'):
                continue
            for disk, ds in sorted((ns.get('disks') or {}).items()):
                status = (lh.get('status', {}).get('diskStatus') or {}).get(disk) or {}
                key = (node, disk)
                if key == (inv['node'], inv['disk']) or ds.get('diskType', 'filesystem') != 'filesystem' or not ds.get('allowScheduling') or ds.get('evictionRequested'):
                    continue
                if not _ready({'status': status}) or not _ready({'status': status}, 'Schedulable') or not status.get('diskUUID'):
                    continue
                if not set(vs.get('diskSelector') or []) <= set(ds.get('tags') or []) or not vs.get('diskSelector') and ds.get('tags') and not empty_disks:
                    continue
                if (node, status['diskUUID']) in held_disks and not _soft(volume, 'replica-disk-soft-anti-affinity'):
                    continue
                maximum, available = status.get('storageMaximum'), status.get('storageAvailable')
                allocated, reserved = status.get('storageScheduled'), ds.get('storageReserved', 0)
                if not all(isinstance(n, int) and n >= 0 for n in (maximum, available, allocated, reserved)) or maximum <= 0:
                    continue
                budget.setdefault(key, available - reserved - maximum * minimum // 100)
                scheduled.setdefault(key, (maximum - reserved) * over // 100 - allocated)
                if size > 0 and budget[key] >= size and scheduled[key] >= size:
                    choices.append(key)
        target = choices[0] if choices else None
        if target:
            budget[target] -= size
            scheduled[target] -= size
            target_uuid = inv['lhs'][target[0]]['status']['diskStatus'][target[1]]['diskUUID']
            planned.setdefault(name, []).append((target[0], target_uuid))
        else:
            blockers.append(f'{name}: no other eligible V1 disk has room for its full {round(size / GiB, 1)} GiB replica and placement rules')
        rows.append({'volume': name, 'uid': volume['metadata'].get('uid'), 'replica': replica['metadata']['name'],
                     'size_bytes': size, 'replicas': int(vs.get('numberOfReplicas') or 1),
                     'destination': ' / '.join(target) if target else 'No eligible destination'})
    return rows, list(dict.fromkeys(blockers))


def _context(inv, facts, rows):
    return {'host_uid': inv['host']['metadata']['uid'], 'boot_id': inv['host']['status']['nodeInfo']['bootID'],
            'lh_uid': inv['lh']['metadata'].get('uid'), 'disk': inv['spec'], 'disk_uuid': inv['status']['diskUUID'],
            'hardware': facts, 'placements': rows}


def review(body):
    node, disk = _name(body.get('node')), _name(body.get('disk'))
    with ops._lock:
        existing = _active_for(node, disk)
        if existing:
            return {'operation': public(existing)}
    inv = _inventory(node, disk)
    facts = _host_facts(inv['spec']['path'], node)
    if facts['longhorn_uuid'] != inv['status']['diskUUID']:
        raise ValueError('The mounted filesystem does not match this Longhorn disk')
    rows, blockers = _placements(inv)
    request_id = secrets.token_hex(12)
    cfg = {'action': 'evacuate', 'node': node, 'disk': disk, 'request_id': request_id}
    return {'node': node, 'disk': disk, 'device': facts['device'], 'size_bytes': facts['size_bytes'],
            'volumes': rows, 'blockers': blockers, 'request_id': request_id,
            'capacity_token': REVIEW.issue(cfg, _context(inv, facts, rows)),
            'detail': 'Move V1 replicas to other V1 disks. The device stays mounted and is not erased.'}


def _active_for(node, disk):
    return next((i for i in ops._read() if i.get('kind') == KIND and i.get('ref', {}).get('node') == node
                 and i.get('ref', {}).get('disk') == disk and i.get('ref', {}).get('phase') not in ('complete', 'cancelled')), None)


def _patch_disk(inv, value):
    meta = inv['lh'].get('metadata') or {}
    if not meta.get('uid') or not meta.get('resourceVersion'):
        raise ValueError('Longhorn host identity/version could not be verified')
    path = '/spec/disks/' + inv['disk'].replace('~', '~0').replace('/', '~1')
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': meta['uid']},
             {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']},
             {'op': 'test', 'path': path, 'value': inv['spec']},
             {'op': 'remove', 'path': path} if value is None else {'op': 'replace', 'path': path, 'value': value}]
    return ksend('PATCH', LH + '/nodes/' + inv['node'], patch, ctype='application/json-patch+json')


def start(body):
    node, disk, request_id = _name(body.get('node')), _name(body.get('disk')), str(body.get('request_id', ''))
    if body.get('confirm_capacity') is not True or not re.fullmatch(r'[a-f0-9]{24}', request_id):
        raise ValueError('Review and confirm disk evacuation first')
    with ops._lock:
        saved = ops._read()
        prior = next((i for i in saved if i.get('kind') == KIND and i.get('ref', {}).get('request_id') == request_id), None)
        if prior:
            if prior['ref'].get('approval') != body.get('capacity_token') or (prior['ref']['node'], prior['ref']['disk']) != (node, disk):
                raise ValueError('This request belongs to another disk review')
            return public(prior)
        if _active_for(node, disk) or any(i.get('status') not in ops.TERMINAL and i.get('ref', {}).get('node') == node
                and i.get('kind') in (KIND, 'node-power', 'host-os', 'longhorn-v2-prepare', 'disk-retire') for i in saved):
            raise ValueError('A disk or host maintenance task is already active; open it first')
        inv = _inventory(node, disk)
        facts = _host_facts(inv['spec']['path'], node)
        rows, blockers = _placements(inv)
        cfg = {'action': 'evacuate', 'node': node, 'disk': disk, 'request_id': request_id, 'capacity_token': body.get('capacity_token')}
        if facts['longhorn_uuid'] != inv['status']['diskUUID'] or not REVIEW.valid(cfg, _context(inv, facts, rows)):
            raise ValueError('Disk identity, placement or host state changed; review evacuation again')
        if blockers:
            raise ValueError(' '.join(blockers))
        ref = {'node': node, 'disk': disk, 'phase': 'evacuating', 'request_id': request_id, 'approval': body['capacity_token'],
               'node_uid': inv['host']['metadata']['uid'], 'boot_id': inv['host']['status']['nodeInfo']['bootID'],
               'lh_uid': inv['lh']['metadata']['uid'], 'disk_uuid': inv['status']['diskUUID'],
               'original_disk': copy.deepcopy(inv['spec']), 'hardware': facts, 'volumes': rows}
        op = ops.start(KIND, 'Prepare ' + facts['device'] + ' for Longhorn V2', {'kind': 'Node', 'name': node},
                       '/nodes?node=' + quote(node, safe='') + '&section=storage', ref, 'Queued replica evacuation; no device erase is authorized')
        return public(next(i for i in ops._read() if i['id'] == op['id']))


def _saved(operation_id):
    item = next((i for i in ops._read() if i.get('id') == operation_id and i.get('kind') == KIND), None)
    if not item:
        raise ValueError('Disk preparation task not found')
    return item


def public(item):
    ref = item['ref']
    return {**ops._public(item), 'node': ref['node'], 'disk': ref['disk'], 'device': ref['hardware']['device'],
            'phase': ref['phase'], 'volumes': ref['volumes'], 'remaining': ref.get('remaining', len(ref['volumes'])),
            'needs_erase_review': ref['phase'] == 'awaiting-erase', 'namespace': ref.get('namespace', ''), 'job': ref.get('job', '')}


def status(operation_id):
    ops.list_operations()
    with ops._lock:
        return public(_saved(operation_id))


def _verify_original(inv, ref):
    if inv['host']['metadata']['uid'] != ref['node_uid'] or inv['lh']['metadata']['uid'] != ref['lh_uid']:
        raise ValueError('Host identity changed; stop and review the disk again')
    if inv['status']['diskUUID'] != ref['disk_uuid']:
        raise ValueError('Longhorn disk identity changed; nothing further is changed')
    expected = copy.deepcopy(ref['original_disk'])
    expected.update(allowScheduling=False, evictionRequested=True)
    if inv['spec'] not in (ref['original_disk'], expected) or ref.get('eviction_started') and inv['spec'] != expected:
        raise ValueError('The disk configuration changed outside this task; stop and review it again')


def _evacuated(inv, ref):
    mine = [r for r in inv['replicas'] if _on_disk(r, inv)]
    ref['remaining'] = max(len(mine), len(inv['status'].get('scheduledReplica') or {}))
    if ref['remaining'] or inv['status'].get('scheduledBackingImage'):
        return False, 'Waiting for replica evacuation; ' + str(ref['remaining']) + ' replicas still on this disk'
    for row in ref['volumes']:
        volume = inv['volumes'].get(row['volume'])
        if not volume:
            continue  # a deliberately deleted volume no longer needs replicas
        if not _same_volume(volume, row):
            return False, row['volume'] + ': volume identity, size or replica target changed, or migration/expansion is active; stop and review again'
        status = volume.get('status') or {}
        if status.get('state') != 'attached' or status.get('robustness') != 'healthy' or len(_rw(inv, row['volume'])) < row['replicas']:
            return False, row['volume'] + ': waiting for healthy RW replacement replicas'
    return True, 'Replicas are healthy elsewhere. Review erasing and preparing this device to continue.'


def _same_volume(volume, row):
    spec, status = volume.get('spec') or {}, volume.get('status') or {}
    return (volume.get('metadata', {}).get('uid') == row['uid']
            and int(spec.get('numberOfReplicas') or 1) == row['replicas']
            and int(spec.get('size') or 0) == row['size_bytes'] and spec.get('dataEngine', 'v1') == 'v1'
            and not spec.get('migrationNodeID') and not status.get('expansionRequired'))


def prepare_review(operation_id):
    with ops._lock:
        item = _saved(operation_id)
        ref = item['ref']
        if item.get('status') in ops.TERMINAL or ref['phase'] != 'awaiting-erase':
            raise ValueError('Wait for evacuation to finish before reviewing device preparation')
        inv = _inventory(ref['node'], ref['disk'])
        _verify_original(inv, ref)
        ready, reason = _evacuated(inv, ref)
        if not ready:
            raise ValueError(reason)
        facts = _host_facts(inv['spec']['path'], ref['node'])
        if facts != ref['hardware']:
            raise ValueError('Physical disk or filesystem identity changed; no erase is authorized')
        request_id = secrets.token_hex(12)
        cfg = {'action': 'prepare', 'operation_id': item['id'], 'request_id': request_id}
        context = {'hardware': facts, 'host_uid': ref['node_uid'], 'boot_id': inv['host']['status']['nodeInfo']['bootID'], 'disk_uuid': ref['disk_uuid'],
                   'volumes': ref['volumes'], 'disk': inv['spec']}
        return {'operation_id': item['id'], 'node': ref['node'], 'device': facts['device'], 'size_bytes': facts['size_bytes'],
                'request_id': request_id, 'capacity_token': REVIEW.issue(cfg, context),
                'detail': 'Remove the V1 disk entry, unmount its filesystem, remove its fstab entry and erase its signatures. Add the blank device to V2.'}


def prepare(body):
    request_id = str(body.get('request_id', ''))
    if body.get('confirm_capacity') is not True or not re.fullmatch(r'[a-f0-9]{24}', request_id):
        raise ValueError('Review and confirm device preparation first')
    with ops._lock:
        item = _saved(body.get('operation_id'))
        ref = item['ref']
        if ref.get('prepare_request_id') == request_id:
            if ref.get('prepare_approval') != body.get('capacity_token') or body.get('confirm_device') != ref['hardware']['device']:
                raise ValueError('This request belongs to another preparation review')
            return public(item)
        if ref['phase'] != 'awaiting-erase' or item.get('status') in ops.TERMINAL:
            raise ValueError('This task is not waiting for an erase review')
        inv = _inventory(ref['node'], ref['disk'])
        _verify_original(inv, ref)
        ready, reason = _evacuated(inv, ref)
        if not ready:
            raise ValueError(reason)
        facts = _host_facts(inv['spec']['path'], ref['node'])
        context = {'hardware': facts, 'host_uid': ref['node_uid'], 'boot_id': inv['host']['status']['nodeInfo']['bootID'], 'disk_uuid': ref['disk_uuid'],
                   'volumes': ref['volumes'], 'disk': inv['spec']}
        cfg = {'action': 'prepare', 'operation_id': item['id'], 'request_id': request_id, 'capacity_token': body.get('capacity_token')}
        if facts != ref['hardware'] or body.get('confirm_device') != facts['device'] or not REVIEW.valid(cfg, context):
            raise ValueError('A fresh preparation review and the exact device name are required')
        ref.update(phase='removing', boot_id=inv['host']['status']['nodeInfo']['bootID'], prepare_request_id=request_id, prepare_approval=body['capacity_token'],
                   job='homestead-disk-v2-' + request_id, namespace=NS)
        ops.checkpoint(item)  # durable approval before removing any entry or dispatching an erase
        return public(item)


def _host_script(ref):
    facts = ref['hardware']
    values = {'__BYID__': facts['by_id'], '__UUID__': facts['uuid'], '__SERIAL__': facts['serial'],
              '__WWN__': facts['wwn'], '__SIZE__': str(facts['size_bytes']), '__PATH__': facts['mountpoint'],
              '__BOOT__': ref['boot_id'], '__RECEIPT__': ref['prepare_request_id'], '__DISKUUID__': ref['disk_uuid']}
    script = r'''set -eu
fail() { echo "ERROR $*"; exit 1; }
B=__BYID__; U=__UUID__; P=__PATH__; SERIAL=__SERIAL__; WWN=__WWN__; SIZE=__SIZE__
[ "$(cat /proc/sys/kernel/random/boot_id)" = __BOOT__ ] || fail 'Host restarted after review'
D=$(readlink -f "$B")
[ -b "$D" ] && [ "$(lsblk -dnro TYPE "$D")" = disk ] || fail 'Reviewed device is not a whole disk'
[ "$(blockdev --getsize64 "$D")" = "$SIZE" ] || fail 'Device size changed'
[ "$(lsblk -dnro SERIAL "$D" | sed 's/^ *//;s/ *$//')" = "$SERIAL" ] || fail 'Device serial changed'
[ "$(lsblk -dnro WWN "$D" | sed 's/^ *//;s/ *$//')" = "$WWN" ] || fail 'Device WWN changed'
[ "$(blkid -s UUID -o value "$D")" = "$U" ] || fail 'Filesystem identity changed'
[ "$(lsblk -nrpo NAME "$D" | wc -l)" -eq 1 ] || fail 'Partitions or holders appeared'
[ -z "$(ls /sys/class/block/${D##*/}/holders)" ] || fail 'Another device holds this disk'
[ "$(readlink -f "$(findmnt -rn --mountpoint "$P" -o SOURCE)")" = "$D" ] || fail 'Mount source changed'
[ "$(findmnt -rn --mountpoint "$P" -o FSROOT)" = / ] || fail 'Mount root changed'
[ "$(findmnt -rn -S "$D" -o TARGET | wc -l)" -eq 1 ] || fail 'Other mounts use the filesystem'
findmnt -rn -o TARGET | while read -r m; do case "$m" in "$P"/*) echo 'ERROR Nested mount uses the disk'; exit 1;; esac; done
[ -f "$P/longhorn-disk.cfg" ] && [ ! -L "$P/longhorn-disk.cfg" ] || fail 'Longhorn configuration changed'
grep -Fq __DISKUUID__ "$P/longhorn-disk.cfg" || fail 'Longhorn disk UUID changed'
find "$P" -mindepth 1 -maxdepth 1 ! -name replicas ! -name backing-images ! -name backing-images-disk ! -name longhorn-disk.cfg ! -name lost+found -print | grep -q . && fail 'Other files appeared on the disk'
for p in / /boot /boot/efi /usr /var /home /var/lib/kubelet /var/lib/rancher; do
  S=$(findmnt -rn --target "$p" -o SOURCE 2>/dev/null || true)
  [ "$(readlink -f "$S" 2>/dev/null || true)" != "$D" ] || fail 'Device now holds system storage'
done
[ -f /etc/fstab ] && [ ! -L /etc/fstab ] || fail 'fstab is not a regular file'
awk -v p="$P" -v u="UUID=$U" -v d="$D" -v b="$B" '$0 !~ /^[[:space:]]*#/ && $2==p && (($1!=u && $1!=d && $1!=b) || $4 ~ /x-systemd.automount/) {exit 1}' /etc/fstab || fail 'fstab entry changed or enables an automatic mount'
# Lock this physical device and record a one-shot receipt on the host.
R=/var/lib/homestead/disk-v2
[ ! -L /var/lib/homestead ] && [ ! -L "$R" ] || fail 'Receipt directory is a symlink'
mkdir -p "$R"; chmod 700 "$R"
exec 9>"$R/lock-$(printf '%s' "$B" | sha256sum | cut -d' ' -f1)"
flock -n 9 || fail 'Another preparation holds this device'
J="$R/"__RECEIPT__
[ ! -e "$J" ] && [ ! -L "$J" ] || fail 'An earlier erase attempt exists; it will not be repeated'
echo 'Unmounting the evacuated filesystem'
umount "$P" || fail 'Filesystem is busy; no force or lazy unmount is used'
cp -p /etc/fstab "$R/fstab-"__RECEIPT__
T=$(mktemp /etc/.fstab-homestead.XXXXXX)
trap 'rm -f "$T"' EXIT
cp -p /etc/fstab "$T"
awk -v p="$P" '$0 ~ /^[[:space:]]*#/ || $2!=p' /etc/fstab > "$T"
mv "$T" /etc/fstab
systemctl daemon-reload 2>/dev/null || true
[ -z "$(lsblk -nrpo MOUNTPOINT "$D" | grep . || true)" ] || fail 'The device was mounted again'
# Persist intent before wipefs. No restart/retry may run this erase again.
(set -C; printf 'started\n' > "$J") || fail 'Erase receipt already exists'
sync "$J"
echo 'Clearing filesystem signatures for Longhorn V2'
wipefs -a "$D"
[ -z "$(blkid -p -s TYPE -o value "$D" 2>/dev/null || true)" ] || fail 'Filesystem signature remains'
[ "$(lsblk -nrpo NAME "$D" | wc -l)" -eq 1 ] || fail 'Device is not blank'
printf 'complete\n' > "$J.done"; sync "$J.done"; mv "$J.done" "$J"; sync "$R"
echo 'Device is blank; waiting for Homestead to register it with Longhorn V2'
'''
    for key, value in values.items():
        script = script.replace(key, shlex.quote(value))
    return script


def _job_body(ref):
    script = _host_script(ref)
    return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': ref['job'], 'namespace': NS,
            'labels': {LABEL: 'true'}, 'annotations': {LABEL: hashlib.sha256(script.encode()).hexdigest()}},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 600, 'ttlSecondsAfterFinished': 604800, 'template': {'metadata': {'labels': {LABEL: 'true'}},
                     'spec': {'nodeName': ref['node'], 'hostPID': True, 'hostIPC': True, 'hostNetwork': True,
                              'automountServiceAccountToken': False, 'restartPolicy': 'Never',
                              'tolerations': [{'operator': 'Exists'}], 'containers': [{'name': 'prepare', 'image': HOST.IMAGE,
                              'command': HOST.HOST + [script], 'securityContext': {'privileged': True},
                              'resources': {'requests': {'cpu': '10m', 'memory': '64Mi'}, 'limits': {'memory': HOST.MEMORY_LIMIT}}}]}}}}


def _job(ref, create=False):
    path = f'/apis/batch/v1/namespaces/{NS}/jobs/{ref["job"]}'
    obj = _get(path, True)
    expected = _job_body(ref)
    if not obj:
        if not create:
            raise ValueError('Preparation helper is missing. Its erase will not be repeated; inspect the host receipt and disk')
        try:
            obj = ksend('POST', path.rsplit('/', 1)[0], expected)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            obj = _get(path)
    meta, spec = obj.get('metadata') or {}, obj.get('spec') or {}
    actual, target = spec.get('template', {}).get('spec', {}), expected['spec']['template']['spec']
    if (not meta.get('uid') or meta.get('annotations', {}).get(LABEL) != expected['metadata']['annotations'][LABEL]
            or ref.get('job_uid') and meta['uid'] != ref['job_uid'] or spec.get('backoffLimit') != 0
            or spec.get('activeDeadlineSeconds') != 600 or actual.get('initContainers')
            or any(actual.get(k) != target[k] for k in ('nodeName', 'hostPID', 'hostIPC', 'hostNetwork', 'automountServiceAccountToken', 'restartPolicy'))
            or len(actual.get('containers', [])) != 1
            or any(actual['containers'][0].get(k) != target['containers'][0][k] for k in ('name', 'image', 'command', 'securityContext'))
            or any(actual['containers'][0].get(k) for k in ('env', 'envFrom', 'args', 'volumeMounts')) or actual.get('volumes')):
        raise ValueError('Preparation helper identity or configuration changed; nothing is repeated or deleted')
    ref['job_uid'] = meta['uid']
    return obj


def progress(item):
    ref = item['ref']
    try:
        if ref['phase'] == 'complete':
            _cleanup(ref)
            return 'succeeded', 100, 'Disk is ready for V2 replicas; existing volumes retain their engine'
        if ref['phase'] in ('evacuating', 'awaiting-erase'):
            inv = _inventory(ref['node'], ref['disk'])
            _verify_original(inv, ref)
            if not ref.get('eviction_started'):
                wanted = copy.deepcopy(ref['original_disk'])
                wanted.update(allowScheduling=False, evictionRequested=True)
                if inv['spec'] != wanted:
                    _patch_disk(inv, wanted)
                ref['eviction_started'] = True
                return 'running', 10, 'Longhorn is moving replicas to other V1 disks; no erase is authorized'
            ready, reason = _evacuated(inv, ref)
            ref['phase'] = 'awaiting-erase' if ready else 'evacuating'
            count = len(ref['volumes'])
            return 'running', 70 if ready else 10 + int(55 * max(0, count - ref['remaining']) / max(1, count)), reason
        if ref['phase'] == 'removing':
            host = _get('/api/v1/nodes/' + ref['node'])
            lh = _get(LH + '/nodes/' + ref['node'])
            if host['metadata'].get('uid') != ref['node_uid'] or host['status']['nodeInfo'].get('bootID') != ref['boot_id'] or lh['metadata'].get('uid') != ref['lh_uid']:
                raise ValueError('Host identity or boot changed; no preparation is dispatched')
            if ref['disk'] in (lh.get('spec', {}).get('disks') or {}):
                inv = _inventory(ref['node'], ref['disk'])
                _verify_original(inv, ref)
                ready, reason = _evacuated(inv, ref)
                if not ready:
                    return 'running', 70, reason + '; the reviewed erase has not started'
                _patch_disk(inv, None)
            # Removing the entry prevents new Longhorn replicas landing there.
            # An independently scheduled/unobserved replica still stops dispatch.
            _no_replicas(ref)
            ref['phase'] = 'dispatching'
            ops.checkpoint(item)
            return 'running', 75, 'V1 entry removed; dispatching the reviewed one-shot preparation helper'
        if ref['phase'] == 'dispatching':
            if _get(f'/apis/batch/v1/namespaces/{NS}/jobs/{ref["job"]}', True):
                # Dispatch may have succeeded before its response/checkpoint
                # was lost. Adopt only the exact helper and follow it; never
                # mistake a later health change for an undispatched erase.
                _job(ref)
                ref['phase'] = 'preparing'
                ops.checkpoint(item)
                return 'running', 80, 'Following the saved preparation helper; its erase is not dispatched again'
            host = _get('/api/v1/nodes/' + ref['node'])
            if (host['metadata'].get('uid') != ref['node_uid']
                    or host.get('status', {}).get('nodeInfo', {}).get('bootID') != ref['boot_id'] or not _ready(host)):
                raise ValueError('Host identity, boot or readiness changed; no preparation helper is dispatched')
            _no_replicas(ref)
            _replacement_health(ref)
            _job(ref, create=True)
            ref['phase'] = 'preparing'
            ops.checkpoint(item)
            return 'running', 80, 'Unmounting and preparing the evacuated device; open the task log'
        if ref['phase'] == 'preparing':
            job = _job(ref)
            if job.get('status', {}).get('failed'):
                ref['preparation_log'] = '\n'.join(s.get('text', '') for s in logs(item))[-8000:]
                ops.checkpoint(item)
                return 'failed', 80, 'Device preparation failed. Inspect its log; partial changes are retained and erasing is not retried automatically.'
            if not job.get('status', {}).get('succeeded'):
                return 'running', 80, 'Preparing the device; helper continues independently of Homestead'
            ref['phase'] = 'registering'
            ref['preparation_log'] = '\n'.join(s.get('text', '') for s in logs(item))[-8000:]
            ops.checkpoint(item)
        if ref['phase'] == 'registering':
            _register(ref)
            ref['phase'] = 'verifying'
            ops.checkpoint(item)
        if ref['phase'] == 'verifying':
            lh = _get(LH + '/nodes/' + ref['node'])
            entry = (lh.get('spec', {}).get('disks') or {}).get(ref['v2_disk'])
            if lh['metadata'].get('uid') != ref['lh_uid'] or entry != _new_disk(ref):
                raise ValueError('The registered V2 disk configuration changed; inspect it before continuing')
            ds = (lh.get('status', {}).get('diskStatus') or {}).get(ref['v2_disk']) or {}
            if not _ready({'status': ds}) or not _ready({'status': ds}, 'Schedulable') or not ds.get('diskUUID'):
                return 'running', 95, 'Waiting for Longhorn to report the new V2 block disk ready and schedulable'
            ref['phase'] = 'complete'
            ops.checkpoint(item)
            _cleanup(ref)
            return 'succeeded', 100, 'Disk is ready for V2 replicas. Existing volumes keep their data engine; choose a V2 storage class to move a volume.'
        return 'running', item.get('progress', 0), 'Inspect the saved disk preparation task'
    except ValueError as error:
        # Before erase, a blocked check is recoverable without abandoning the
        # workflow. During preparation, preserve the task and never retry wipe.
        return ('running' if ref['phase'] in ('evacuating', 'awaiting-erase', 'removing', 'registering', 'verifying') else 'failed'), item.get('progress', 0), str(error)


def _no_replicas(ref):
    lh = _get(LH + '/nodes/' + ref['node'])
    if lh['metadata'].get('uid') != ref['lh_uid'] or ref['disk'] in (lh.get('spec', {}).get('disks') or {}):
        raise ValueError('The old disk entry is present or the host identity changed; no erase is dispatched')
    probe = {'node': ref['node'], 'status': {'diskUUID': ref['disk_uuid']}, 'spec': ref['original_disk']}
    if any(_on_disk(r, probe) for r in _items(LH + '/replicas')):
        raise ValueError('A replica still refers to the evacuated disk; no erase is dispatched')
    _upgrade_idle()


def _replacement_health(ref):
    inv = {'replicas': _items(LH + '/replicas'), 'engines': _items(LH + '/engines')}
    volumes = {v['metadata']['name']: v for v in _items(LH + '/volumes')}
    for row in ref['volumes']:
        volume = volumes.get(row['volume'])
        if not volume:
            continue
        if (not _same_volume(volume, row) or volume.get('status', {}).get('state') != 'attached'
                or volume.get('status', {}).get('robustness') != 'healthy'
                or len(_rw(inv, row['volume'])) < row['replicas']):
            raise ValueError(row['volume'] + ': replacement replica health changed; no erase is dispatched')


def _new_disk(ref):
    return {'path': ref['hardware']['by_id'], 'diskType': 'block', 'diskDriver': 'auto', 'allowScheduling': True,
            'evictionRequested': False, 'storageReserved': 0, 'tags': ref['original_disk'].get('tags') or []}


def _register(ref):
    _no_replicas(ref)
    lh = _get(LH + '/nodes/' + ref['node'])
    ref['v2_disk'] = 'v2-' + ref['prepare_request_id']
    disks = lh.get('spec', {}).get('disks') or {}
    desired = _new_disk(ref)
    if ref['v2_disk'] in disks:
        if disks[ref['v2_disk']] != desired:
            raise ValueError('V2 disk name is already used by a different device')
        return
    if any(d.get('path') == desired['path'] for d in disks.values()):
        raise ValueError('The prepared block device has another Longhorn entry; inspect it before registering')
    _verify_blank(ref)
    host = _get('/api/v1/nodes/' + ref['node'])
    if host['metadata'].get('uid') != ref['node_uid']:
        raise ValueError('Host identity changed after preparation; no V2 entry was added')
    fresh = _get(LH + '/nodes/' + ref['node'])
    if fresh.get('spec', {}).get('disks', {}) != disks:
        raise ValueError('Longhorn disk entries changed during verification; checking again')
    meta = fresh['metadata']
    ksend('PATCH', LH + '/nodes/' + ref['node'], [
        {'op': 'test', 'path': '/metadata/uid', 'value': ref['lh_uid']},
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']},
        {'op': 'test', 'path': '/spec/disks', 'value': disks},
        {'op': 'add', 'path': '/spec/disks/' + ref['v2_disk'], 'value': desired}], ctype='application/json-patch+json')


def _verify_blank(ref):
    facts = ref['hardware']
    script = '\n'.join(['set -eu', 'D=$(readlink -f ' + shlex.quote(facts['by_id']) + ')',
        '[ -b "$D" ]', '[ "$(blockdev --getsize64 "$D")" = ' + shlex.quote(str(facts['size_bytes'])) + ' ]',
        '[ "$(lsblk -dnro SERIAL "$D" | sed \'s/^ *//;s/ *$//\')" = ' + shlex.quote(facts['serial']) + ' ]',
        '[ "$(lsblk -dnro WWN "$D" | sed \'s/^ *//;s/ *$//\')" = ' + shlex.quote(facts['wwn']) + ' ]',
        '[ "$(lsblk -nrpo NAME "$D" | wc -l)" -eq 1 ]',
        '[ -z "$(lsblk -nrpo MOUNTPOINT "$D" | grep . || true)" ]',
        '[ -z "$(blkid -p -s TYPE -o value "$D" 2>/dev/null || true)" ]',
        '[ "$(cat /var/lib/homestead/disk-v2/' + ref['prepare_request_id'] + ')" = complete ]', 'echo VERIFIED'])
    out, err = HOST.run(ref['node'], script, timeout=60)
    if 'VERIFIED' not in out.splitlines():
        raise ValueError('The blank device or its completion receipt could not be verified; no V2 entry was added')


def _cleanup(ref):
    try:
        job = _get(f'/apis/batch/v1/namespaces/{NS}/jobs/{ref["job"]}', True)
    except ValueError:
        return  # cleanup availability cannot invalidate completed disk preparation
    if job and job.get('metadata', {}).get('uid') == ref.get('job_uid'):
        try:
            ksend('DELETE', f'/apis/batch/v1/namespaces/{NS}/jobs/{ref["job"]}',
                  {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'propagationPolicy': 'Background', 'preconditions': {'uid': ref['job_uid']}})
        except urllib.error.HTTPError:
            pass  # do not turn a successfully prepared disk into a failed erase


def cancel_plan(item):
    can = item['ref']['phase'] in ('evacuating', 'awaiting-erase', 'cancelled')
    return {'can': can, 'why_not': '' if can else 'Device preparation is already approved; its erase cannot be safely interrupted or forgotten',
            'mode': 'stop', 'needs': 'admin', 'undo': ['Stop requesting further replica evacuation'],
            'keeps': ['Replacement replicas remain. The original disk stays disabled for new replicas. No device is erased.'], 'severity': 'low'}


def cancel_run(item, options):
    if not cancel_plan(item)['can']:
        raise ValueError(cancel_plan(item)['why_not'])
    ref = item['ref']
    if ref['phase'] == 'cancelled':
        return 'Evacuation stopped; disk remains mounted and disabled for new replicas'
    host = _get('/api/v1/nodes/' + ref['node'])
    lh = _get(LH + '/nodes/' + ref['node'])
    inv = {'host': host, 'lh': lh, 'node': ref['node'], 'disk': ref['disk'],
           'spec': lh.get('spec', {}).get('disks', {}).get(ref['disk']),
           'status': lh.get('status', {}).get('diskStatus', {}).get(ref['disk'], {})}
    _verify_original(inv, ref)
    desired = copy.deepcopy(inv['spec'])
    desired.update(allowScheduling=False, evictionRequested=False)
    _patch_disk(inv, desired)
    ref['phase'] = 'cancelled'
    # The generic canceller persists its original record separately.
    with ops._lock:
        saved = _saved(item['id'])
        saved['ref']['phase'] = 'cancelled'
        ops.checkpoint(saved)
    return 'Evacuation stopped; disk remains mounted and disabled for new replicas'


def mutation_guard(node, disk=None, device=None):
    with ops._lock:
        for item in ops._read():
            ref = item.get('ref') or {}
            if (item.get('kind') == KIND and ref.get('node') == node and ref.get('phase') not in ('complete', 'cancelled')
                    and (disk is None and device is None or disk is not None and disk in (ref.get('disk'), ref.get('v2_disk'))
                         or device is not None and device in (ref.get('hardware', {}).get('device'), ref.get('hardware', {}).get('by_id'), ref.get('hardware', {}).get('mountpoint')))):
                raise ValueError('This disk has a saved V2 preparation task; open that task before changing it')


def tasks():
    with ops._lock:
        return [{'id': i['id'], 'node': i['ref']['node'], 'disk': i['ref']['disk'], 'device': i['ref']['hardware']['device'],
                 'phase': i['ref']['phase']} for i in ops._read() if i.get('kind') == KIND and i.get('ref', {}).get('phase') not in ('complete', 'cancelled')]


def logs(item):
    ref = item['ref']
    if not ref.get('job'):
        return []
    if ref.get('preparation_log'):
        return [{'title': 'Device preparation', 'text': ref['preparation_log']}]
    pods = _items('/api/v1/namespaces/' + NS + '/pods?labelSelector=' + quote('job-name=' + ref['job'], safe=''))
    return [{'title': 'Device preparation', 'text': read_log('/api/v1/namespaces/' + NS + '/pods/' + p['metadata']['name'] + '/log?tailLines=100')}
            for p in pods if any(o.get('uid') == ref.get('job_uid') for o in p.get('metadata', {}).get('ownerReferences', []))]


def protect_mutations(disks):
    """Keep the saved approval and ordinary disk writes mutually exclusive."""
    @contextmanager
    def scope(node, disk, device):
        with ops._lock:
            mutation_guard(node, disk, device)
            yield
    disks.mutation_scope = scope
    def protect(fn, mode):
        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            if mode == 'config':
                cfg = args[0] if args else kwargs.get('cfg', {})
                node, disk, device = cfg.get('node'), cfg.get('disk'), cfg.get('device') or cfg.get('path')
            else:
                node = args[0] if args else kwargs.get('node')
                disk = (args[1] if len(args) > 1 else kwargs.get('disk_id')) if mode == 'disk' else None
                device = None
            current_scope = disks.mutation_scope
            if current_scope is None:
                return fn(*args, **kwargs)
            with current_scope(node, disk, device):
                return fn(*args, **kwargs)
        return guarded
    for name in ('set_disk_tags', 'set_scheduling', 'evict', 'remove'):
        setattr(disks, name, protect(getattr(disks, name), 'disk'))
    disks.set_node_tags = protect(disks.set_node_tags, 'node')
    for name in ('add', 'set_up', 'use_os_space', 'retire_start'):
        setattr(disks, name, protect(getattr(disks, name), 'config'))
    disks.v2_tasks = tasks


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/disks/v2/status"): ("admin", lambda request: status(request.query.get("id", [""])[0])),
    ("POST", "/api/disks/v2/plan"): ("admin", lambda request: review(request.body)),
    ("POST", "/api/disks/v2/start"): ("admin", lambda request: start(request.body)),
    ("POST", "/api/disks/v2/prepare-review"): ("admin", lambda request: prepare_review(request.body.get("id", ""))),
    ("POST", "/api/disks/v2/prepare"): ("admin", lambda request: prepare(request.body)),
}
