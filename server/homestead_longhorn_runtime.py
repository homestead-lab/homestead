"""Review, stage and follow offline relocation of installer Longhorn runtime."""
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import re
import secrets
import shlex
import urllib.error

import homestead_capacity_review as REVIEW
import homestead_disk_v2 as V2
import homestead_disk_region as REGION
import homestead_disk_allocation as ALLOCATION
import homestead_hostrun as HOST
import homestead_job_cadence as CADENCE
import homestead_storage_conflicts as CONFLICTS

KIND = 'longhorn-runtime'
ops = None
WORKER = Path(__file__).with_name('homestead_runtime_worker.py')
BASE = '/var/lib/homestead/longhorn-runtime'
SOURCE = '/var/lib/longhorn'


def bind(operations):
    global ops
    ops = operations


def _host(node, code, *, write=False, timeout=120):
    # Worker entrypoints are fixed server code. No request supplies code/paths.
    source = WORKER.read_text(encoding='utf-8')
    # exec needs one shared globals mapping for functions to see each other.
    script = "/usr/bin/python3 - <<'HOMESTEAD_RUNTIME'\nworker_source=" + repr(source) + "\nnamespace={'__name__':'runtime_worker'}\nexec(compile(worker_source, '<runtime-worker>', 'exec'), namespace)\n"
    script += "exec(" + repr(code) + ", namespace | {'worker_source': worker_source})\nHOMESTEAD_RUNTIME\n"
    out, err = HOST.run(node, script, timeout=timeout, **({'change': 'Longhorn runtime maintenance'} if write else {}))
    try:
        line = next(line[len('RESULT '):] for line in out.splitlines() if line.startswith('RESULT '))
        return json.loads(line)
    except (StopIteration, ValueError):
        raise ValueError('Runtime maintenance could not be verified: ' + str(err or out)[-600:]) from None


def _call(node, expression, *, write=False):
    return _host(node, 'print("RESULT " + json.dumps(' + expression + '))', write=write)


def _hardware(node):
    # The generic eraser still rejects runtime. This read only obtains the
    # same region identity, then the runtime worker applies its closed policy.
    script = V2.inspect_script(SOURCE).replace(V2.foreign_contents_message(), '')
    out, err = HOST.run(node, script, timeout=60)
    facts = V2.parse_host_facts(SOURCE, out, err)
    if facts.get('kind') != 'lvm':
        raise ValueError('Runtime relocation currently requires a dedicated installer logical volume')
    allocation = ALLOCATION.inspect(node)
    area = next((a for a in allocation['areas'] if a['role'] == 'longhorn'), None)
    if not area or area['stable_device'] != facts['by_id']:
        raise ValueError('The source must be the dedicated Longhorn LV in the OS single-PV volume group')
    return facts


def _inventory(node, disk, *, ref=None):
    V2._upgrade_idle()
    if V2.platform(True).get('harvester'): raise ValueError('Harvester owns its runtime storage')
    host = V2._get('/api/v1/nodes/' + V2._name(node))
    lh = V2._get(V2.LH + '/nodes/' + node)
    if not V2._ready(host) or not V2._ready(lh): raise ValueError('The host and Longhorn must be ready')
    for obj in (host, lh):
        if not obj.get('metadata', {}).get('uid') or not obj['metadata'].get('resourceVersion'):
            raise ValueError('Host identity/version is unavailable')
    spec = (lh.get('spec', {}).get('disks') or {}).get(disk)
    status = (lh.get('status', {}).get('diskStatus') or {}).get(disk) or {}
    if not spec or spec.get('diskType', 'filesystem') != 'filesystem' or not status.get('diskUUID'):
        raise ValueError('The original V1 disk registration must remain present')
    # Longhorn registrations may retain a trailing slash. Compare that spelling
    # only; do not resolve aliases or collapse parent/subdirectory components.
    if not ref and str(spec.get('path') or '').rstrip('/') != SOURCE:
        raise ValueError('Runtime move supports only the dedicated V1 store at /var/lib/longhorn. '
                         'Open that store under Host → Storage → Disk actions → Move Longhorn runtime. '
                         'Other storage layouts cannot be moved by this workflow.')
    if ref and (host['metadata']['uid'] != ref['node_uid'] or lh['metadata']['uid'] != ref['lh_uid']
                or status['diskUUID'] != ref['disk_uuid']):
        raise ValueError('Host or Longhorn disk identity changed')
    probe = {'node':node, 'status':status, 'spec':spec}
    if status.get('scheduledReplica') or status.get('scheduledBackingImage') or any(V2._on_disk(r, probe) for r in V2._items(V2.LH + '/replicas')):
        raise ValueError('Move or separately delete every replica and backing image on this store first')
    V2._residual_claims(node, status['diskUUID'])
    for volume in V2._items(V2.LH + '/volumes'):
        v, s = volume.get('spec') or {}, volume.get('status') or {}
        if v.get('migrationNodeID') or s.get('expansionRequired') or s.get('robustness') in ('degraded', 'faulted'):
            raise ValueError('Finish storage migrations, expansion and replica recovery before runtime maintenance')
    if host.get('metadata', {}).get('labels', {}).get('node.longhorn.io/create-default-disk') in ('true', 'config'):
        raise ValueError('Remove this host’s automatic Longhorn disk-creation label before runtime maintenance')
    return {'node':node, 'disk':disk, 'host':host, 'lh':lh, 'spec':spec, 'status':status}


def _pending(node):
    return next((item for item in ops.stored() if item.get('kind') == KIND and item.get('ref', {}).get('node') == node
                 and item['ref'].get('phase') != 'retired'), None)


def _conflicts(node):
    for item in ops.stored():
        if item.get('kind') == KIND:
            if item.get('ref',{}).get('phase') != 'retired':raise ValueError('Finish the existing runtime maintenance job before starting on another host')
            continue
        ref = item.get('ref') or {}
        if (item.get('status') not in ops.TERMINAL or CONFLICTS.unresolved(item)) and (ref.get('node') == node or item.get('kind') in (
                'workload-copy', 'reclass', 'import-create', 'vm-import', 'restructure', 'cluster-shutdown')):
            raise ValueError('Finish other storage or host maintenance jobs before moving runtime')


def _plan(node, disk, request_id):
    inv = _inventory(node, disk)
    facts = _hardware(node)
    V2._other_stores(node, inv['lh'], facts, disk)
    if facts['longhorn_uuid'] != inv['status']['diskUUID']: raise ValueError('Mounted Longhorn disk identity changed')
    identity = 'set -eu\nfail() { echo "$*" >&2; exit 1; }\nD=$(readlink -f ' + shlex.quote(facts['by_id']) + ')\n' + REGION.verify(facts)
    plan = {'node':node, 'disk':disk, 'request_id':request_id, 'hardware':facts, 'identity_script':identity,
            'node_uid':inv['host']['metadata']['uid'], 'lh_uid':inv['lh']['metadata']['uid'],
            'boot_id':inv['host']['status']['nodeInfo']['bootID'], 'disk_uuid':inv['status']['diskUUID'],
            'original_disk':inv['spec'], 'retained':'/mnt/homestead-retired-' + request_id,
            'worker_sha256':hashlib.sha256(WORKER.read_text(encoding='utf-8').encode()).hexdigest()}
    return _call(node, 'inspect(' + repr(plan) + ')')


def _context(plan):
    return {k:v for k,v in plan.items() if k not in ('available_bytes','runtime_bytes')}


def review(body):
    node, disk = V2._name(body.get('node')), V2._name(body.get('disk'))
    with ops.transaction():
        existing = _pending(node)
        if existing: return {'operation':public(existing)}
        _conflicts(node)
    request_id = secrets.token_hex(12)
    plan = _plan(node, disk, request_id)
    cfg = {'node':node, 'disk':disk, 'request_id':request_id}
    return {**_display(plan), **cfg, 'capacity_token':REVIEW.issue(cfg, _context(plan))}


def _display(plan):
    facts = plan['hardware']
    return {'node':plan['node'], 'disk':plan['disk'], 'device':facts['device'], 'stable_device':facts['by_id'],
            'layout':facts.get('layout'), 'size_bytes':facts['size_bytes'], 'mountpoint':SOURCE,
            'runtime_bytes':plan['runtime_bytes'], 'available_bytes':plan['available_bytes'],
            'destination':BASE + '/' + plan['request_id'] + '/active', 'retained':plan['retained']}


def start(body):
    cfg = {k:body.get(k) for k in ('node','disk','request_id','capacity_token')}
    if body.get('confirm_capacity') is not True or not re.fullmatch('[a-f0-9]{24}', str(cfg['request_id'])):
        raise ValueError('Review and confirm runtime maintenance first')
    with ops.transaction():
        existing = _pending(cfg['node'])
        if existing:
            if existing['ref'].get('approval') != cfg['capacity_token']: raise ValueError('Open the saved runtime maintenance job')
            return public(existing)
        _conflicts(cfg['node'])
        plan = _plan(cfg['node'], cfg['disk'], cfg['request_id'])
        if not REVIEW.valid(cfg, _context(plan)): raise ValueError('Host or storage changed, or the review expired. Review again.')
        item = ops.start(KIND, 'Move Longhorn runtime on ' + cfg['node'], {'kind':'Node','name':cfg['node']},
                         '/nodes?node=' + cfg['node'] + '&section=storage',
                         {**plan, 'phase':'queued', 'approval':cfg['capacity_token']}, 'Runtime relocation approved; source files retained')
        return public(_saved(item['id']))


def _saved(operation_id):
    item = next((i for i in ops.stored() if i.get('kind') == KIND and i.get('id') == operation_id), None)
    if not item: raise ValueError('Runtime maintenance job not found')
    return item


def public(item):
    return {**ops._public(item), **_display(item['ref']), 'phase':item['ref']['phase']}


def _folder(ref):
    return 'Path(' + repr(BASE + '/' + ref['request_id']) + ')'


def _disabled(ref, retained=False):
    return {**ref['original_disk'], 'allowScheduling':False, 'evictionRequested':False,
            'path':ref['retained'] if retained else SOURCE}


def _recheck(ref):
    inv = _inventory(ref['node'], ref['disk'], ref=ref)
    if inv['spec'] not in (ref['original_disk'], _disabled(ref), _disabled(ref, True)):
        raise ValueError('Disk configuration changed outside this maintenance job')
    V2._other_stores(ref['node'], inv['lh'], ref['hardware'], ref['disk'])
    return inv


def _arm(ref):
    inv = _recheck(ref)
    if inv['spec'] != _disabled(ref, True):
        V2._patch_disk(inv, _disabled(ref, True))
    inv = _recheck(ref)
    if inv['spec'] != _disabled(ref, True): raise ValueError('The retained disk registration could not be verified')
    folder = _folder(ref)
    return _host(ref['node'], 'folder=' + folder + '\nplan=read(folder / "plan.json")\n'
        + 'require(plan == ' + repr({k:v for k,v in ref.items() if k not in ('phase','approval')}) + ', "Saved host plan differs")\n'
        + 'state=read(folder / "state.json")\nrequire(state["phase"] == "unarmed", "Maintenance was already armed")\n'
        + 'verify_staging(plan)\nstate["phase"]="staged"\nsave(folder,state)\nprint("RESULT "+json.dumps(state))', write=True)


def progress(item):
    ref = item['ref']
    try:
        if ref['phase'] == 'queued':
            inv = _recheck(ref)
            V2._patch_disk(inv, _disabled(ref))
            ref['phase'] = 'staging'; ops.checkpoint(item)
            plan = {k:v for k,v in ref.items() if k not in ('phase','approval')}
            _call(ref['node'], 'stage(' + repr(plan) + ', worker_source)', write=True)
            _arm(ref)
            ref['phase'] = 'staged'; ops.checkpoint(item)
        elif ref['phase'] == 'staging':
            raise ValueError('Staging was interrupted. Recheck this job; no helper is repeated automatically.')
        if not V2._ready(V2._get('/api/v1/nodes/' + ref['node'])):
            return 'running', item.get('progress', 20), 'Waiting for the host to return from runtime maintenance; the original LV remains retained'
        def on_host():
            state = _call(ref['node'], 'read(' + _folder(ref) + ' / "state.json")')
            phase = state['phase']
            if phase in ('relocated','retired'):
                inv = _recheck(ref)
                if inv['spec'] != _disabled(ref, True): raise ValueError('Retained store registration changed')
                _call(ref['node'], '(runtime_check(read(' + _folder(ref) + ' / "plan.json"), ' + _folder(ref) + '), True)[1]')
            ref['phase'] = phase
            if phase == 'retired': return 'succeeded', 100, 'Runtime moved and its archived originals retired. Review Prepare for V2 separately.'
            if phase == 'relocated': return 'running', 70, 'Runtime moved. Original LV and archive retained; review retirement to continue.'
            return 'running', 20 if phase == 'staged' else 85, 'Reboot through host maintenance to ' + ('move runtime' if phase == 'staged' else 'retire the archived original runtime')
        # Waiting for a reboot can take days: the host is looked at every CADENCE interval (#975).
        return CADENCE.look(item, 'longhorn-runtime', on_host)
    except (ValueError, urllib.error.HTTPError) as error:
        return 'failed', item.get('progress', 0), str(error)


def status(operation_id):
    ops.advance_soon()
    with ops.transaction(): return public(_saved(operation_id))


def recheck(operation_id):
    with ops.transaction():
        item = _saved(operation_id); ref = item['ref']
        _recheck(ref)
        if ref['phase'] in ('staging','unarmed'):
            plan={k:v for k,v in ref.items() if k not in ('phase','approval')}
            state=_call(ref['node'],'restore_staging('+repr(plan)+', worker_source) if not ('+_folder(ref)+' / "state.json").exists() or read('+_folder(ref)+' / "state.json")["phase"] == "unarmed" else read('+_folder(ref)+' / "state.json")',write=True)
        else:state = _call(ref['node'], 'read(' + _folder(ref) + ' / "state.json")')
        if state['phase'] == 'unarmed': state = _arm(ref)
        ref['phase'] = state['phase']
        item.update(status='running', message='Saved maintenance state rechecked; source has not been erased')
        ops.checkpoint(item)
        return public(item)


def retirement_review(operation_id):
    with ops.transaction():
        item = _saved(operation_id); ref = item['ref']; _recheck(ref)
        state = _call(ref['node'], 'retirement(' + _folder(ref) + ')')
        cfg = {'id':operation_id, 'action':'retire-runtime'}
        return {**_display(ref), **cfg, 'capacity_token':REVIEW.issue(cfg, {'plan':_context(ref),'state':state})}


def retire(body):
    cfg = {k:body.get(k) for k in ('id','action','capacity_token')}
    with ops.transaction():
        item = _saved(cfg['id']); ref = item['ref']; _recheck(ref)
        if body.get('confirm_capacity') is not True or body.get('confirm_device') != ref['hardware']['device'] or cfg['action'] != 'retire-runtime':
            raise ValueError('Type the reviewed device name and confirm retirement of its archived runtime files')
        state = _call(ref['node'], 'retirement(' + _folder(ref) + ')')
        if not REVIEW.valid(cfg, {'plan':_context(ref),'state':state}): raise ValueError('Retirement review changed or expired; review again')
        _call(ref['node'], 'arm_retirement(' + _folder(ref) + ')', write=True)
        ref['phase'] = 'retire-staged'; item.update(status='running')
        ops.checkpoint(item)
        return public(item)


def protect_mutations(disks):
    previous = disks.mutation_scope
    @contextmanager
    def scope(node, disk, device):
        with ops.transaction():
            if _pending(node): raise ValueError('Finish the saved Longhorn runtime maintenance before changing this host’s disks')
            with previous(node, disk, device): yield
    disks.mutation_scope = scope


def register(operations):
    operations.RESOLVERS[KIND] = progress
    operations.CANCEL_BLOCKS[KIND] = lambda item: 'Runtime maintenance keeps a boot gate and retained source. Recheck its saved state; do not discard it.'
    operations.VIEWS[KIND] = lambda item, out: out.update(cancellable=False, cleanable=False, runtime_maintenance=True)


ROUTES = {
    ('POST','/api/disks/runtime/review'):('admin', lambda r: review(r.body)),
    ('POST','/api/disks/runtime/start'):('admin', lambda r: start(r.body)),
    ('GET','/api/disks/runtime/status'):('admin', lambda r: status(r.query.get('id',[''])[0])),
    ('POST','/api/disks/runtime/recheck'):('admin', lambda r: recheck(r.body.get('id'))),
    ('POST','/api/disks/runtime/retirement-review'):('admin', lambda r: retirement_review(r.body.get('id'))),
    ('POST','/api/disks/runtime/retire'):('admin', lambda r: retire(r.body)),
}
