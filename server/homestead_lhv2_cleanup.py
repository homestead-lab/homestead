"""Disable unused V2 and restore only recorded host memory reservations."""
import json
from pathlib import Path
import secrets

import homestead_capacity_review as REVIEW
import homestead_hostrun as HOST
import homestead_kube as KUBE
import homestead_lhcapacity as CAP
import homestead_lhv2_setup as SETUP
import homestead_pod_resources as RESOURCES
import homestead_routes
import homestead_storage_conflicts as CONFLICTS

KIND = 'longhorn-v2-cleanup'
WORKER = Path(__file__).with_name('homestead_v2_memory_worker.py')


def pending():
    return next((i for i in SETUP.ops.stored() if i.get('kind') == KIND and i.get('ref', {}).get('phase') != 'complete'), None)


def require_idle():
    if pending(): raise ValueError('Finish the saved V2 resource cleanup before preparing or enabling V2')


def items(path):
    return KUBE.items(path, read=SETUP.kget)


def inventory(*, cleaning=False):
    if KUBE.get_optional(CAP.HARVESTER_V2, read=SETUP.kget) is not None:
        raise ValueError('Harvester manages host memory; use its V2 settings and maintenance workflow')
    setting = SETUP.kget(CAP.LH + '/settings/v2-data-engine')
    require = setting.get('metadata') or {}
    if not require.get('uid') or not require.get('resourceVersion') or setting.get('value') not in ('true', 'false'):
        raise ValueError('V2 setting identity or state is unavailable')
    lh_nodes = items(CAP.LH + '/nodes')
    if any(v.get('spec', {}).get('dataEngine') == 'v2' for v in items(CAP.LH + '/volumes')):
        raise ValueError('Move V2 volumes to V1 before disabling V2')
    if any(d.get('diskType') == 'block' for n in lh_nodes for d in (n.get('spec', {}).get('disks') or {}).values()):
        raise ValueError('Remove empty V2 disk registrations before disabling V2')
    if any(i.get('kind') in ('longhorn-v2-prepare', 'disk-v2-convert', 'longhorn-runtime', 'disk-allocation', 'host-os', 'platform-upgrade') + (() if cleaning else ('node-power',))
           and (i.get('status') not in SETUP.ops.TERMINAL or CONFLICTS.unresolved(i)) for i in SETUP.ops.stored()):
        raise ValueError('Finish active host, disk and component maintenance before disabling V2')
    nodes = items('/api/v1/nodes'); pods = items('/api/v1/pods')
    managers = items(CAP.LH + '/instancemanagers')
    names = [m['metadata']['name'] for m in managers if m.get('spec', {}).get('dataEngine') == 'v2']
    active = names + [p['metadata']['name'] for p in pods if p.get('metadata', {}).get('labels', {}).get('longhorn.io/data-engine') == 'v2']
    return setting, nodes, pods, active


def host(row, action='inspect', *, proof='', boot=''):
    source = WORKER.read_text(encoding='utf-8')
    expression = 'inspect(' + repr(row['uid']) + ')' if action == 'inspect' else 'stage(' + ','.join(map(repr, (row['uid'], proof, boot))) + ')'
    script = "python3 - <<'HOMESTEAD_MEMORY'\n" + source + '\nprint("RESULT " + json.dumps(' + expression + '))\nHOMESTEAD_MEMORY\n'
    out, err = HOST.run(row['node'], script, timeout=90, **({'change': 'Release V2 memory after restart'} if action == 'stage' else {}))
    try:
        return json.loads(next(line[7:] for line in out.splitlines() if line.startswith('RESULT ')))
    except (StopIteration, ValueError):
        raise ValueError('Host memory inspection did not finish: ' + (err or out)[-350:]) from None


def node_rows(nodes):
    result = []
    for n in nodes:
        meta, status = n.get('metadata', {}), n.get('status', {})
        if not meta.get('uid') or not status.get('nodeInfo', {}).get('bootID'):
            raise ValueError('Host identity is unavailable')
        result.append({'node': meta['name'], 'uid': meta['uid'], 'boot': status['nodeInfo']['bootID'],
                       'ready': any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', []))})
    return result


def review():
    existing = pending()
    if existing: return {'operation': public(existing)}
    setting, nodes, pods, active = inventory()
    rows = []
    for row in node_rows(nodes):
        try:
            if not row['ready']: raise ValueError('Host is unavailable; its reservation is retained')
            memory = host(row)
            if memory['boot'] != row['boot']: raise ValueError('Host restarted during review')
            rows.append({**row, **memory})
        except (ValueError, OSError) as error:
            rows.append({**row, 'owned_pages': 0, 'phase': 'retained', 'reason': str(error)})
    context = {'setting_uid': setting['metadata']['uid'], 'enabled': setting['value'] == 'true', 'nodes': rows,
               'managers': active}
    cfg = {'request_id': secrets.token_hex(12)}
    # Live free counters change as V2 works. Ownership, identity and policy are
    # pinned; live use is rechecked before any host policy write.
    stable = _context(context)
    return {**context, **cfg, 'capacity_token': REVIEW.issue(cfg, stable)}


def _context(plan):
    return {'setting_uid': plan['setting_uid'], 'nodes': [{k: r.get(k) for k in ('node', 'uid', 'boot', 'proof', 'owned_pages', 'baseline', 'phase')} for r in plan['nodes']]}


def start(body):
    if body.get('confirm_capacity') is not True: raise ValueError('Review and confirm V2 resource cleanup first')
    with SETUP.ops.transaction():
        existing = pending()
        if existing:
            if existing['ref'].get('approval') != body.get('capacity_token'): raise ValueError('Open the saved V2 resource cleanup job')
            return public(existing)
        plan = review()
        cfg = {'request_id': body.get('request_id'), 'capacity_token': body.get('capacity_token')}
        if not REVIEW.valid(cfg, _context(plan)): raise ValueError('Host memory ownership changed or review expired; review again')
        op = SETUP.ops.start(KIND, 'Disable V2 and release owned resources', {'kind': 'Cluster', 'name': 'longhorn'},
                            '/settings?tab=hardware', {**_context(plan), 'nodes': plan['nodes'], 'managers': plan['managers'],
                            'approval': body['capacity_token'], 'phase': 'stopping'}, 'Waiting for V2 to stop; host memory is unchanged')
        return status(op['id'])


def public(item):
    return {**SETUP.ops._public(item), 'phase': item['ref']['phase'], 'nodes': item['ref']['nodes']}


def status(identity):
    item = next((i for i in SETUP.ops.stored() if i.get('id') == identity and i.get('kind') == KIND), None)
    if not item: raise ValueError('V2 cleanup job not found')
    return public(item)


def progress(item):
    ref = item['ref']
    setting, nodes, pods, active = inventory(cleaning=True)
    if setting['metadata']['uid'] != ref['setting_uid']: raise ValueError('V2 setting identity changed')
    if {(r['node'],r['uid']) for r in node_rows(nodes)} != {(r['node'],r['uid']) for r in ref['nodes']}:
        raise ValueError('Cluster hosts changed; cleanup requires a new inspection')
    if setting['value'] == 'true':
        if ref['phase'] != 'stopping': raise ValueError('V2 was re-enabled; host cleanup is stopped')
        ref['phase'] = 'waiting'; SETUP.ops.checkpoint(item)
        CAP.ksend('PATCH', CAP.LH + '/settings/v2-data-engine', [
            {'op': 'test', 'path': '/metadata/uid', 'value': setting['metadata']['uid']},
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': setting['metadata']['resourceVersion']},
            {'op': 'replace', 'path': '/value', 'value': 'false'}], ctype='application/json-patch+json')
        homestead_routes.forget('lhcap')
        return 'running', 10, 'V2 disabled; waiting for its instance managers to stop'
    live_pods = {p['metadata']['name'] for p in pods if p['metadata'].get('namespace') == 'longhorn-system'}
    if active or live_pods.intersection(ref['managers']):
        return 'running', 15, 'Waiting for V2 instance managers to stop; CPU and memory release is not yet verified'
    if any(not r['ready'] for r in node_rows(nodes)):
        return 'running', 20, 'Waiting for hosts to return; resource release on an unavailable host is not assumed'
    ref['phase'] = 'cleanup'
    current = {r['node']: r for r in node_rows(nodes)}
    waiting = False
    for row in ref['nodes']:
        if row.get('phase') in ('retained', 'complete') or not row.get('owned_pages'): continue
        node = current.get(row['node'])
        if not node or node['uid'] != row['uid']: raise ValueError('Host identity changed; memory is retained')
        if not node['ready']:
            waiting = True; continue
        memory = host(row)
        if memory['phase'] == 'complete':
            observed = next(n for n in nodes if n['metadata']['name'] == row['node'])
            capacity = RESOURCES.quantity(observed.get('status', {}).get('capacity', {}).get('hugepages-2Mi'), 'storage')
            if node['boot'] != memory['boot'] or capacity != memory['baseline'] * 2097152:
                waiting = True; continue
            row.update(memory); continue
        if memory['phase'] == 'restart-required':
            row.update(memory); waiting = True; continue
        if memory.get('proof') != row.get('proof') or memory['boot'] != row['boot']:
            raise ValueError('Host memory changed since review; reservation is retained')
        requested = sum(RESOURCES.pod_request(p.get('spec', {}), 'hugepages-2Mi') for p in pods
                        if p.get('spec', {}).get('nodeName') == row['node'] and p.get('status', {}).get('phase') not in ('Succeeded', 'Failed'))
        if requested > row['baseline'] * 2097152:
            raise ValueError('Other workloads request this host’s hugepages; its reservation is retained')
        obj = next(n for n in nodes if n['metadata']['name'] == row['node'])
        # Prevent new placements while kubelet still advertises the old pool.
        if not obj.get('spec', {}).get('unschedulable'):
            SETUP.ksend('PATCH', '/api/v1/nodes/' + row['node'], [
                {'op': 'test', 'path': '/metadata/uid', 'value': row['uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': obj['metadata']['resourceVersion']},
                {'op': 'add', 'path': '/spec/unschedulable', 'value': True}], ctype='application/json-patch+json')
        # A workload can bind while the initial inventory is being read. Take
        # another complete read after cordoning, before changing boot policy.
        fresh = items('/api/v1/pods')
        if sum(RESOURCES.pod_request(p.get('spec', {}), 'hugepages-2Mi') for p in fresh
               if p.get('spec', {}).get('nodeName') == row['node'] and p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')) > row['baseline'] * 2097152:
            raise ValueError('A workload claimed hugepages during review; memory is unchanged and the host remains cordoned')
        row.update(host(row, 'stage', proof=row['proof'], boot=row['boot'])); waiting = True
        SETUP.ops.checkpoint(item)
        break
    if waiting:
        ref['phase'] = 'restart-required'; SETUP.ops.checkpoint(item)
        return 'running', 60, 'V2 stopped. Review a restart for each prepared host to release RAM; hosts remain cordoned until you allow scheduling'
    ref['phase'] = 'complete'; SETUP.ops.checkpoint(item)
    retained = any(r.get('phase') == 'retained' or not r.get('owned_pages') for r in ref['nodes'])
    return 'succeeded', 100, 'V2 stopped; owned RAM released' + ('; unowned reservations retained (see host details)' if retained else '')


def register(ops):
    # The hosts have their hugepages back once the job ends: the V2 engine on
    # Volumes shows it on its next refresh (#978).
    ops.RESOLVERS[KIND] = homestead_routes.drops_when_ended(progress, 'lhv2')
    ops.CANCEL_BLOCKS[KIND] = lambda item: 'Inspect saved V2 cleanup. A host may have memory changes awaiting restart.'


def recheck(identity):
    with SETUP.ops.transaction():
        item = next((i for i in SETUP.ops.stored() if i.get('id') == identity and i.get('kind') == KIND), None)
        if not item: raise ValueError('V2 cleanup job not found')
        state, percent, message = progress(item)
        if state in homestead_routes.ENDED:
            homestead_routes.forget('lhv2')
        item.update(status=state, progress=percent, message=message)
        SETUP.ops.checkpoint(item)
        return public(item)


ROUTES = {
    ('GET', '/api/longhorn/v2/cleanup'): ('admin', lambda request: review()),
    ('POST', '/api/longhorn/v2/cleanup'): ('admin', lambda request: start(request.body)),
    ('POST', '/api/longhorn/v2/cleanup/recheck'): ('admin', lambda request: recheck(request.body.get('id'))),
    ('GET', '/api/longhorn/v2/cleanup/status'): ('admin', lambda request: status(request.query.get('id', [''])[0])),
}
