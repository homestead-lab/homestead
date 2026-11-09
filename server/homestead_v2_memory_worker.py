"""Host-side ownership of V2 hugepages. Cleanup changes boot policy, never live RAM.

The pre-existing pool is a floor, not an estimate of what other users need.
Legacy/changed ownership is retained. Modules, CPU isolation and disks are untouched.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

CONFIG = Path('/etc/sysctl.d/99-homestead-longhorn-v2.conf')
RECEIPT = Path('/var/lib/homestead/longhorn-v2-memory.json')
POOL = Path('/sys/kernel/mm/hugepages/hugepages-2048kB')
BOOT = Path('/proc/sys/kernel/random/boot_id')
MACHINE = Path('/etc/machine-id')
NUMA = Path('/sys/devices/system/node/online')
PROC = Path('/proc')
POLICY_DIRS = [Path(p) for p in ('/etc/sysctl.d', '/run/sysctl.d', '/usr/local/lib/sysctl.d', '/usr/lib/sysctl.d', '/lib/sysctl.d')]
POLICY_FILES = [Path(p) for p in ('/etc/sysctl.conf', '/proc/cmdline', '/etc/default/grub', '/etc/kernel/cmdline')]


def require(value, message):
    if not value:
        raise ValueError(message)


def secure(path):
    for item in [*reversed(path.parents), path]:
        if item.exists() or item.is_symlink():
            info = item.lstat()
            require(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                    'Memory ownership path is not exclusively root-managed')
    if path.exists():
        require(path.is_file() and path.stat().st_nlink == 1, 'Memory ownership file is not a regular private file')


def atomic(path, text):
    secure(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    secure(path)
    fd, name = tempfile.mkstemp(prefix='.homestead-v2-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(name): os.unlink(name)


def policy(*, shared=False):
    files = set(POLICY_FILES)
    for folder in POLICY_DIRS:
        if folder.exists():
            require(folder.is_dir(), 'Cannot inspect host memory policy')
            files.update(folder.glob('*.conf'))
    rows = {}
    for path in sorted(files):
        if path == CONFIG: continue
        if not path.exists() and not path.is_symlink(): continue
        # Vendor symlinks (including /dev/null masks) are read, never changed.
        data = path.read_bytes()
        if shared and any(any(word in line for word in ('nr_hugepages', 'hugepages=', 'hugepagesz='))
                          for line in data.decode('utf-8', 'replace').splitlines() if not line.lstrip().startswith('#')):
            return True
        rows[str(path)] = hashlib.sha256(data).hexdigest()
    if shared: return False
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def counts():
    values = {key: int((POOL / key).read_text()) for key in ('nr_hugepages', 'free_hugepages', 'resv_hugepages')}
    require(all(v >= 0 for v in values.values()) and values['free_hugepages'] <= values['nr_hugepages'], 'Invalid hugepage counters')
    return values


def config(pages):
    return f'vm.nr_hugepages={pages}\n'


def receipt(node_uid):
    secure(RECEIPT); secure(CONFIG)
    if not RECEIPT.exists():
        return None
    row = json.loads(RECEIPT.read_text())
    require(row.get('version') == 1 and row.get('node_uid') == node_uid and row.get('machine') == MACHINE.read_text().strip(),
            'Memory ownership belongs to another host')
    require(type(row.get('baseline')) is int and type(row.get('target')) is int
            and 0 <= row['baseline'] <= row['target'] <= 1048576, 'Invalid memory ownership record')
    require(row.get('policy') == policy(), 'The host memory or start-up policy changed. Keep its reservation.')
    return row


def reserve(node_uid, boot, requested, v2_pages):
    require(BOOT.read_text().strip() == boot, 'The host restarted. Review the preparation again.')
    require(type(requested) is int and type(v2_pages) is int and 0 <= requested <= 1048576 and 0 <= v2_pages <= 524288,
            'Invalid memory request')
    if not requested: return
    now = counts(); old = receipt(node_uid)
    baseline = now['nr_hugepages']
    if old:
        completed = old.get('phase') == 'restart-required' and BOOT.read_text().strip() != old.get('cleanup_boot')
        require(old.get('phase') == 'prepared' or completed, 'Finish the saved memory cleanup before preparing V2')
        expected = old['baseline'] if completed else old['target']
        require(CONFIG.read_text() == config(expected) and now['nr_hugepages'] == expected,
                'The recorded V2 reservation changed, so it needs a manual review')
        baseline = old['baseline']
    elif CONFIG.exists():
        # Old setup wrote an aggregate total without recording its previous pool.
        # Keep the entire existing reservation as the unowned baseline.
        text = CONFIG.read_text()
        try: prior = int(text.removeprefix('vm.nr_hugepages=').strip())
        except ValueError: raise ValueError('Legacy memory policy needs manual review') from None
        require(0 <= prior <= 1048576 and text == config(prior), 'Legacy memory policy needs manual review')
        baseline = max(baseline, prior)
    target = max(requested, baseline, now['nr_hugepages'], v2_pages + now['nr_hugepages'] - now['free_hugepages'] + now['resv_hugepages'])
    require(target <= 1048576, 'Memory pool exceeds the supported limit')
    memory = dict(line.split(':', 1) for line in (PROC / 'meminfo').read_text().splitlines() if ':' in line)
    require((target - now['nr_hugepages']) * 2048 <= max(0, int(memory['MemAvailable'].split()[0]) - 1048576),
            'Leave at least 1 GiB available after preparing V2')
    row = {'version': 1, 'node_uid': node_uid, 'machine': MACHINE.read_text().strip(), 'baseline': baseline,
           'target': target, 'policy': policy(), 'phase': 'preparing', 'boot': boot}
    # Receipt first: any interrupted preparation stays untrusted until inspected.
    atomic(RECEIPT, json.dumps(row)); atomic(CONFIG, config(target))
    (POOL / 'nr_hugepages').write_text(str(target))
    actual = counts()['nr_hugepages']
    require(now['nr_hugepages'] <= actual <= target, 'Unexpected hugepage allocation. It is kept for a manual review.')
    row['phase'] = 'prepared'; atomic(RECEIPT, json.dumps(row))
    if actual < target: print('V2 setup: only part of the memory was set aside. A reviewed host restart is needed.')


def inspect(node_uid):
    now = counts(); row = receipt(node_uid)
    base = {'boot': BOOT.read_text().strip(), 'pages': now['nr_hugepages'], 'free': now['free_hugepages'],
            'reserved': now['resv_hugepages'], 'owned_pages': 0, 'baseline': now['nr_hugepages'], 'phase': 'retained'}
    if not row:
        return {**base, 'reason': 'No ownership record. Existing hugepages are retained because their pre-V2 allocation is unknown.'}
    expected = row['baseline'] if row.get('phase') == 'restart-required' else row['target']
    require(CONFIG.read_text() == config(expected), 'The managed memory policy changed. Keep its reservation.')
    require(row.get('phase') in ('prepared', 'restart-required'), 'An interrupted preparation needs a manual review. Keep its reservation.')
    require(not policy(shared=True), 'Another host policy sets up hugepages, so its shared reservation is kept')
    require(NUMA.read_text().strip() == '0', 'More than one NUMA node needs a manual memory review, so their reservations are kept')
    if row['phase'] == 'restart-required' and base['boot'] != row['cleanup_boot']:
        require(base['pages'] == row['baseline'], 'The restarted host has a different hugepage pool. Check its memory policy.')
        return {**base, 'phase': 'complete', 'baseline': row['baseline'], 'owned_pages': row['target'] - row['baseline']}
    require(base['pages'] == row['target'], 'The current hugepage allocation changed. Keep its reservation.')
    return {**base, 'baseline': row['baseline'], 'owned_pages': row['target'] - row['baseline'],
            'phase': row['phase'], 'proof': hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
            'reason': '' if row['target'] > row['baseline'] else 'The existing pool was not added by this preparation, so it is kept.'}


def stage(node_uid, proof, boot):
    view = inspect(node_uid)
    if view['phase'] in ('restart-required', 'complete'): return view
    require(view['phase'] == 'prepared' and view['proof'] == proof and view['boot'] == boot, 'Memory cleanup review changed')
    require(view['owned_pages'] > 0, 'No owned pages can be released')
    require(view['pages'] - view['free'] + view['reserved'] <= view['baseline'],
            'Hugepages are still in use beyond the original pool. Wait for V2 and whatever else uses them to stop.')
    # Also catch SPDK not yet reflected in the controller's pod inventory.
    for process in PROC.iterdir():
        if not process.name.isdecimal(): continue
        try: name = (process / 'comm').read_text().strip()
        except FileNotFoundError: continue
        require(not name.startswith(('spdk', 'reactor_', 'vhost')), 'A storage polling process is still running')
    row = receipt(node_uid)
    row.update(phase='restart-required', cleanup_boot=boot)
    # Do not shrink a live pool: kubelet reports hugepage capacity from startup.
    # An interruption between these writes refuses further automatic changes.
    atomic(RECEIPT, json.dumps(row)); atomic(CONFIG, config(row['baseline']))
    return inspect(node_uid)
