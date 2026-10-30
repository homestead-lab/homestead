"""Root-owned, boot-time Longhorn runtime maintenance worker.

This file is also copied verbatim to the host. It has no server dependencies.
It never formats a device or changes LVM metadata. The retained filesystem is
eligible for ordinary disk preparation only after a separate retirement.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

BASE = Path('/var/lib/homestead/longhorn-runtime')
SOURCE = Path('/var/lib/longhorn')
FSTAB = Path('/etc/fstab')
SYSTEMD = Path('/etc/systemd/system')
RUNTIME = {'engine-binaries', 'logs', 'instance-manager', 'unix-domain-socket', 'metadata'}
EMPTY = {'replicas', 'backing-images', 'backing-images-disk', 'lost+found'}
RESERVE = 5 * 1024 ** 3
PINNED = None
GUARD = '[Unit]\nRequires=homestead-longhorn-runtime.service\nAfter=homestead-longhorn-runtime.service\n'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def run(*args):
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    require(result.returncode == 0, 'Host inspection or maintenance failed: ' + args[0] + ': ' + result.stderr.strip()[:200])
    return result.stdout.strip()


def plain(path, directory=False):
    path = Path(path)
    for parent in [*reversed(path.parents), path]:
        if parent.exists() or parent.is_symlink():
            mode = parent.lstat().st_mode
            require(not stat.S_ISLNK(mode), 'A maintenance path is a symlink: ' + str(parent))
    if directory:
        require(path.is_dir(), 'Maintenance directory is missing: ' + str(path))
    return path


def administrative(path):
    path = plain(path)
    for parent in [*reversed(path.parents), path]:
        if parent.exists():
            info = parent.stat()
            require(info.st_uid == 0 and not info.st_mode & 0o022, 'Maintenance path is not exclusively root-managed: ' + str(parent))
    return path


# Extended attributes, kept by namespace (#973). The kernel maintains these
# itself: they are neither copied nor compared.
KERNEL_MANAGED = ('security.ima', 'security.evm')


def _label(name):
    """A Linux security module's label (SELinux, AppArmor, Smack)."""
    return name in ('security.selinux', 'security.apparmor') or name.startswith('security.SMACK64')


def read_attrs(path):
    """The attributes kept for path, base64 by name: all but the kernel-managed."""
    return {name: base64.b64encode(os.getxattr(path, name)).decode()
            for name in sorted(os.listxattr(path)) if name not in KERNEL_MANAGED}


def keep_attrs(path, attrs):
    """Give path the attributes read_attrs() returned for its source. Only what
    differs is written: on an SELinux host every file has a label, and a
    confined or unprivileged writer cannot set one, even to the same value.
    user.*, trusted.* and security.capability must be kept exactly; a refused
    write raises. A label the kernel refuses stops maintenance, naming both."""
    for name, value in attrs.items():
        if name in KERNEL_MANAGED:
            continue
        wanted = base64.b64decode(value)
        try:
            current = os.getxattr(path, name)
        except OSError:
            current = None
        if current == wanted:
            continue
        try:
            os.setxattr(path, name, wanted)
        except OSError as error:
            if not _label(name):
                raise
            have = 'absent or unreadable' if current is None else repr(current)
            require(False, f'Cannot keep {name} on {path}: it is {have}, the source has {wanted!r} '
                           f'({error.strerror}). Check for a context= mount or a policy that refuses the relabel.')


def metadata(path):
    info = path.stat()
    return {'mode':stat.S_IMODE(info.st_mode), 'uid':info.st_uid, 'gid':info.st_gid, 'attrs':read_attrs(path)}


def apply_metadata(path, values):
    os.chown(path, values['uid'], values['gid'])
    os.chmod(path, values['mode'])
    keep_attrs(path, values['attrs'])


def durable(path, content):
    path = plain(path)
    previous = metadata(path) if path.exists() else None
    if path.exists():require(path.is_file() and path.stat().st_nlink == 1, 'Maintenance file must be regular and unlinked')
    temp = path.with_name(path.name + '.next')
    require(not temp.is_symlink(), 'Maintenance temporary file is a symlink')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(content)
        stream.flush()
        if previous: apply_metadata(temp, previous)
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def save(folder, state):
    durable(folder / 'state.json', json.dumps(state, sort_keys=True))


def read(path):
    plain(path)
    return json.loads(Path(path).read_text(encoding='utf-8'))


def mount(path, field):
    return run('findmnt', '-rn', '--mountpoint', str(path), '-o', field)


def mounted(path):
    # os.path.ismount cannot recognize a bind mount on the same filesystem.
    # Read the complete mount table successfully rather than interpreting a
    # failed per-path query as an absent mount.
    return str(path) in run('findmnt', '-rn', '-o', 'TARGET').splitlines()


def root_identity():
    device = run('findmnt', '-rn', '--target', str(BASE.parent), '-o', 'SOURCE')
    root = run('findmnt', '-rn', '--mountpoint', '/', '-o', 'SOURCE')
    require(os.path.realpath(device) == os.path.realpath(root), 'Runtime destination must be on the OS root filesystem')
    return {'uuid': run('blkid', '-s', 'UUID', '-o', 'value', os.path.realpath(root)),
            'filesystem': run('findmnt', '-rn', '--mountpoint', '/', '-o', 'FSTYPE')}


def identity(plan):
    require(root_identity() == plan['root'], 'OS filesystem identity changed')
    facts = plan['hardware']
    device = os.path.realpath(facts['by_id'])
    require(stat.S_ISBLK(os.stat(device).st_mode), 'Retained device is missing')
    if PINNED is not None:
        require(os.fstat(PINNED).st_rdev == os.stat(device).st_rdev, 'Retained device no longer matches its pinned descriptor')
    require(run('blkid', '-s', 'UUID', '-o', 'value', device) == facts['uuid'], 'Retained filesystem identity changed')
    require(int(run('blockdev', '--getsize64', device)) == facts['size_bytes'], 'Retained device size changed')
    run('sh', '-c', plan['identity_script'])
    return device


def manifest(folder, *, source=False):
    """A closed inventory: no symlinks, mounts, hardlinks or unknown devices.

    Sockets in the two documented runtime directories are ephemeral and are
    intentionally not copied. All other special files stop maintenance.
    """
    folder = plain(folder, True)
    rootdev = folder.stat().st_dev
    output = {}
    def visit(path):
        rel = path.relative_to(folder).as_posix()
        entry = path.lstat()
        require(entry.st_dev == rootdev, 'Nested filesystem: ' + rel)
        require(not stat.S_ISLNK(entry.st_mode), 'Runtime symlink needs manual inspection: ' + rel)
        if source and stat.S_ISSOCK(entry.st_mode) and rel.split('/')[0] in ('unix-domain-socket', 'instance-manager'):
            return
        require(stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode), 'Unexpected runtime file: ' + rel)
        attrs = read_attrs(path)
        item = {'mode': stat.S_IMODE(entry.st_mode), 'uid': entry.st_uid, 'gid': entry.st_gid, 'attrs': attrs,
                'directory': stat.S_ISDIR(entry.st_mode)}
        if item['directory']:
            output[rel] = item
            for child in sorted(path.iterdir()): visit(child)
        else:
            require(entry.st_nlink == 1, 'Hard-linked runtime file needs manual inspection: ' + rel)
            with path.open('rb') as stream:
                item.update(size=entry.st_size, sha256=hashlib.file_digest(stream, 'sha256').hexdigest(), mtime=entry.st_mtime_ns)
            output[rel] = item
    for path in sorted(folder.iterdir()):
        name = path.name
        if source and name in EMPTY:
            require(path.is_dir() and not path.is_symlink() and path.stat().st_dev == rootdev and not list(path.iterdir()),
                    'Replica, backing-image, scratch or orphan data remains in ' + name)
        elif source and name == 'longhorn-disk.cfg':
            require(path.is_file() and not path.is_symlink(), 'Longhorn disk configuration is not a regular file')
        else:
            require(name in RUNTIME, 'Unrecognized file on the Longhorn filesystem: ' + name)
            require(path.is_dir(), 'Runtime entry is not a directory: ' + name)
            visit(path)
    return output


def verified_partial(source, target, expected):
    if target.exists():
        # Only a never-activated staging copy can be resumed. Its existing
        # files must be exact prefixes of the verified original; unrelated
        # content is never overwritten or recursively removed.
        require(not any(p == str(target) or p.startswith(str(target) + '/') for p in run('findmnt', '-rn', '-o', 'TARGET').splitlines()),
                'A staging copy is mounted or contains mounts')
        partial = manifest(target)
        require(set(partial) <= set(expected), 'Unrecognized data appeared in a staging copy')
        for rel, entry in partial.items():
            require(entry['directory'] == expected[rel]['directory'], 'A staging entry changed type')
            if not entry['directory']:
                require(entry['size'] <= expected[rel]['size'], 'A staging file exceeds its reviewed source')
                with (source / rel).open('rb') as inp, (target / rel).open('rb') as out:
                    while chunk := out.read(1024 * 1024):
                        require(inp.read(len(chunk)) == chunk, 'A staging file is not a prefix of its verified source')
        return sum(entry.get('size',0) for entry in partial.values())
    return 0


def copy_runtime(source, target, expected):
    verified_partial(source,target,expected)
    if not target.exists():target.mkdir(mode=0o700)
    for rel, entry in sorted(expected.items(), key=lambda pair: (pair[0].count('/'), pair[0])):
        src, dst = source / rel, target / rel
        if entry['directory']: dst.mkdir(mode=0o700, exist_ok=True)
        else:
            offset = dst.stat().st_size if dst.exists() else 0
            with src.open('rb') as inp, dst.open('ab' if dst.exists() else 'xb') as out:
                inp.seek(offset)
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush(); os.fsync(out.fileno())
        os.chown(dst, entry['uid'], entry['gid'])
        os.chmod(dst, entry['mode'])
        keep_attrs(dst, entry['attrs'])
        if not entry['directory']: os.utime(dst, ns=(entry['mtime'], entry['mtime']))
    require(manifest(target) == expected, 'Runtime copy verification failed; source remains intact')
    apply_metadata(target, metadata(source))
    run('sync', '-f', str(target))


def offline(plan):
    require(Path('/proc/sys/kernel/random/boot_id').read_text().strip() != plan['boot_id'],
            'Maintenance runs only after reboot, before Kubernetes starts')
    forbidden = {'k3s', 'k3s-agent', 'kubelet', 'containerd', 'longhorn'}
    own_ns = os.stat('/proc/1/ns/mnt').st_ino
    device = os.stat(os.path.realpath(plan['hardware']['by_id'])).st_rdev
    number = f'{os.major(device)}:{os.minor(device)}'
    for proc in Path('/proc').iterdir():
        if proc.name.isdecimal(): process_use(proc, device, own_ns, number, forbidden)


def process_use(proc, device, own_ns, number, forbidden):
    try:
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        started, kernel_thread = fields[19], bool(int(fields[6]) & 0x200000)
        comm = (proc / 'comm').read_text().strip()
        require(comm not in forbidden and not comm.startswith('longhorn-'), 'Kubernetes or Longhorn is still running')
        if not kernel_thread and os.stat(proc / 'ns/mnt').st_ino != own_ns:
            require(all(line.split()[2] != number for line in (proc / 'mountinfo').read_text().splitlines()),
                    'A private mount namespace still uses the retained filesystem')
        if int(proc.name) != os.getpid() and not kernel_thread:
            for link in ('cwd', 'root'):
                require((proc / link).stat().st_dev != device, 'A process working directory or root still uses the retained filesystem')
            for line in (proc / 'maps').read_text().splitlines():
                major, minor = (int(part, 16) for part in line.split()[3].split(':'))
                require(os.makedev(major, minor) != device, 'A process still maps a file from the retained filesystem')
            for descriptor in (proc / 'fd').iterdir():
                try: opened = descriptor.stat()
                except FileNotFoundError:
                    if not descriptor.is_symlink() and not descriptor.exists(): continue  # Proven closed handle.
                    raise
                require(opened.st_dev != device and not (stat.S_ISBLK(opened.st_mode) and opened.st_rdev == device),
                        'A process still has the retained filesystem open')
        require((proc / 'stat').read_text().rsplit(')', 1)[1].split()[19] == started,
                'Process identity changed during inspection; review the stopped host again')
    except FileNotFoundError:
        if proc.exists(): raise
        # A proven exited process cannot retain a mount or open file. Access
        # failures for a PID that still exists never count as absence.


def fstab_lines(plan):
    original = plan['fstab']
    lines = original.splitlines(keepends=True)
    selected = [line for line in lines if line.strip() and not line.lstrip().startswith('#') and line.split()[1] == str(SOURCE)]
    require(len(selected) == 1, 'Exactly one persistent Longhorn filesystem mount is required')
    fields = selected[0].split()
    require(fields[0] == 'UUID=' + plan['hardware']['uuid'] and fields[2] == plan['hardware']['fstype']
            and 'x-systemd.automount' not in fields[3], 'The source fstab entry is not the reviewed filesystem UUID')
    folder = BASE / plan['request_id']
    retained = plan['retained']
    return ''.join(line for line in lines if line != selected[0]).rstrip() + '\n' + (
        f'{folder}/active {SOURCE} none bind 0 0\n'
        f'UUID={plan["hardware"]["uuid"]} {retained} {plan["hardware"]["fstype"]} defaults,ro,nofail 0 0\n')


def source_check(plan, point):
    device = identity(plan)
    require(os.path.realpath(mount(point, 'SOURCE')) == device and mount(point, 'FSROOT') == '/', 'Source mount changed')
    targets = run('findmnt', '-rn', '-S', device, '-o', 'TARGET').splitlines()
    require(targets == [str(point)], 'Other mounts use this filesystem')
    require(not any(target.startswith(str(point) + '/') for target in run('findmnt', '-rn', '-o', 'TARGET').splitlines()),
            'A nested mount uses the source')
    require(read(point / 'longhorn-disk.cfg').get('diskUUID') == plan['disk_uuid'], 'Longhorn disk identity changed')
    return manifest(point, source=True)


def inspect(plan):
    require(sys.version_info >= (3, 11), 'Python 3.11 or newer is required on the host')
    plain(BASE.parent, True); administrative(BASE); administrative(FSTAB); administrative(SYSTEMD); administrative(Path('/mnt'))
    require(not BASE.exists(), 'A saved runtime relocation exists; open its maintenance job')
    require(root_identity()['filesystem'] in ('ext4', 'xfs'), 'OS runtime destination needs ext4 or XFS')
    plan['root'] = root_identity()
    data = source_check(plan, SOURCE)
    require(bool(data), 'There are no supported runtime directories to relocate')
    usage = os.statvfs(BASE.parent)
    size = sum(item.get('size', 0) for item in data.values())
    free = usage.f_bavail * usage.f_frsize
    require(free >= 2 * size + RESERVE, 'OS filesystem needs two runtime copies plus 5 GiB free')
    plain(FSTAB)
    plan['fstab'] = FSTAB.read_text()
    plan['fstab_metadata'] = metadata(FSTAB)
    fstab_lines(plan)
    services = [name for name in ('k3s', 'k3s-agent') if subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode == 0]
    require(len(services) == 1, 'This workflow requires exactly one active K3s service')
    plan['service'] = services[0]
    require(not (SYSTEMD / (services[0] + '.service.d/homestead-runtime.conf')).exists(), 'Runtime startup guard already exists')
    require(not (SYSTEMD / 'homestead-longhorn-runtime.service').exists()
            and not (SYSTEMD / 'homestead-longhorn-runtime.service.d').exists(), 'A runtime maintenance unit already exists')
    return {**plan, 'runtime_bytes': size, 'available_bytes': free}


def stage(plan, worker_source):
    current = inspect(dict(plan))
    require(all(current[key] == plan[key] for key in ('root', 'fstab', 'fstab_metadata', 'service')), 'Host configuration changed since review')
    plain(BASE).mkdir(mode=0o700)
    folder = BASE / plan['request_id']; folder.mkdir(mode=0o700)
    durable(folder / 'plan.json', json.dumps(plan, sort_keys=True))
    durable(folder / 'worker.py', worker_source)
    save(folder, {'phase': 'unarmed', 'message': 'Registration must be verified before boot maintenance is armed'})
    retained = plain(plan['retained'])
    require(not retained.exists(), 'The retained mount directory already exists')
    retained.mkdir(mode=0o700)
    run('chattr', '+i', str(retained))
    require('i' in run('lsattr', '-d', str(retained)).split()[0], 'Cannot protect retained mount from root-backed writes')
    durable(SYSTEMD / 'homestead-longhorn-runtime.service', unit_text(plan))
    dropin = plain(SYSTEMD / (plan['service'] + '.service.d'))
    dropin.mkdir(exist_ok=True)
    durable(dropin / 'homestead-runtime.conf', GUARD)
    durable(folder / 'RECOVERY.txt', recovery_text(plan))
    run('systemctl', 'daemon-reload')
    return read(folder / 'state.json')


def recovery_text(plan):
    folder = BASE / plan['request_id']
    return ('Inspect state.json, failure.txt and journalctl -u homestead-longhorn-runtime.\n'
            'Do not erase the source or switch to an old runtime copy after activation.\n'
            'If the saved phase is unarmed and K3s is stopped, verified pre-cutover recovery is:\n'
            f'/usr/bin/python3 {folder}/worker.py recover-staging {folder}\n'
            'This only releases our exact startup dependency after checking the unchanged original mount.\n'
            'It preserves the disabled disk registration, protected retained placeholder and all files.\n'
            f'Only after that command succeeds: systemctl start {plan["service"]}\n'
            'Then open the saved Homestead job and choose Recheck outcome to restore and verify staging.\n'
            'For copying, cutover, relocated or retirement phases, recovery never removes the guard.\n'
            'Resolve the recorded inspection failure, then reboot; the worker resumes only its recorded phase.\n')


def staging_source(plan):
    administrative(BASE / plan['request_id']); administrative(FSTAB); administrative(SYSTEMD)
    require(FSTAB.read_text() == plan['fstab'] and metadata(FSTAB) == plan['fstab_metadata'], 'Original mount configuration changed')
    require(root_identity() == plan['root'], 'OS filesystem identity changed')
    source_check(plan, SOURCE)
    require('rw' in mount(SOURCE,'OPTIONS').split(','), 'Original source must still be the unchanged writable mount')
    require(not (SYSTEMD / 'homestead-longhorn-runtime.service.d').exists(), 'Unexpected maintenance unit overrides exist')


def restore_staging(plan, worker_source):
    """Explicit repair of interrupted staging, never a boot-time retry."""
    staging_source(plan)
    folder = BASE / plan['request_id']
    administrative(BASE).mkdir(mode=0o700,exist_ok=True)
    require(set(p.name for p in BASE.iterdir()) <= {plan['request_id']}, 'Maintenance directory changed')
    folder.mkdir(mode=0o700,exist_ok=True)
    allowed = {'plan.json','worker.py','state.json','RECOVERY.txt','failure.txt'}
    require(all(p.name.removesuffix('.next') in allowed and p.is_file() and not p.is_symlink() and p.stat().st_nlink == 1 for p in folder.iterdir()),
            'Unexpected data exists in the staging receipt')
    require(hashlib.sha256(worker_source.encode()).hexdigest() == plan['worker_sha256'], 'Current worker differs from reviewed staging')
    for name, content in [('plan.json',json.dumps(plan,sort_keys=True)),('worker.py',worker_source)]:
        path=folder/name
        if path.exists():require(path.read_text() == content, 'Saved staging file changed: '+name)
        else:durable(path,content)
    state = read(folder/'state.json') if (folder/'state.json').exists() else {'phase':'unarmed'}
    require(state['phase']=='unarmed', 'Only unarmed pre-cutover staging can be restored')
    if not (folder/'state.json').exists():save(folder,state)
    retained=administrative(plan['retained'])
    require(not mounted(retained), 'The retained placeholder is unexpectedly mounted')
    if not retained.exists():retained.mkdir(mode=0o700)
    require(retained.is_dir() and not list(retained.iterdir()), 'Retained placeholder contains data')
    run('chattr','+i',str(retained))
    require('i' in run('lsattr','-d',str(retained)).split()[0], 'Retained placeholder is not protected')
    dropin=administrative(SYSTEMD/(plan['service']+'.service.d'));dropin.mkdir(exist_ok=True)
    for path,content in [(SYSTEMD/'homestead-longhorn-runtime.service',unit_text(plan)),(dropin/'homestead-runtime.conf',GUARD)]:
        administrative(path)
        if path.exists():require(path.read_text()==content,'Startup dependency changed outside maintenance')
        else:durable(path,content)
    durable(folder/'RECOVERY.txt',recovery_text(plan))
    verify_staging(plan)
    return state


def recover_staging(folder):
    """Local opt-in availability recovery; no rollback of an activated copy."""
    plan,state=read(folder/'plan.json'),read(folder/'state.json')
    require(state['phase']=='unarmed','Only unarmed pre-cutover staging can release the startup dependency')
    staging_source(plan)
    require(not (folder/'active').exists() and not (folder/'archive').exists(),'Runtime copying has already started')
    require(hashlib.sha256(administrative(folder/'worker.py').read_bytes()).hexdigest()==plan['worker_sha256'],'Saved worker changed')
    require(run('systemctl','show',plan['service'],'-p','ActiveState','--value') in ('inactive','failed'),'Stop Kubernetes before local recovery')
    retained=administrative(plan['retained'])
    require(not mounted(retained) and retained.is_dir() and not list(retained.iterdir()) and 'i' in run('lsattr','-d',str(retained)).split()[0],
            'Retained placeholder must remain empty and protected')
    unit=administrative(SYSTEMD/'homestead-longhorn-runtime.service')
    require(unit.read_text()==unit_text(plan),'Maintenance unit changed')
    dropin=administrative(SYSTEMD/(plan['service']+'.service.d/homestead-runtime.conf'))
    if dropin.exists():require(dropin.read_text()==GUARD,'Startup dependency changed')
    state['local_staging_recovery']=True;save(folder,state)
    if dropin.exists():dropin.unlink()
    run('sync','-f',str(dropin.parent));run('systemctl','daemon-reload')
    require('homestead-longhorn-runtime.service' not in run('systemctl','show',plan['service'],'-p','Requires','--value').split(),
            'Another required runtime dependency remains; Kubernetes must stay stopped')
    return {'phase':'unarmed','message':'Original source retained. Start '+plan['service']+' explicitly, then recheck the Homestead job to restore staging.'}


def unit_text(plan):
    folder = BASE / plan['request_id']
    return ('[Unit]\nDescription=Verified Longhorn runtime maintenance\nAfter=local-fs.target\n'
            'RequiresMountsFor=/var/lib/homestead\nBefore=k3s.service k3s-agent.service\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\nTimeoutStartSec=infinity\n'
            f'ExecStart=/usr/bin/python3 {folder}/worker.py boot {folder}\n')


def verify_staging(plan):
    folder = administrative(BASE / plan['request_id'])
    require(read(folder / 'plan.json') == plan, 'Saved maintenance plan changed')
    worker = administrative(folder / 'worker.py')
    require(hashlib.sha256(worker.read_bytes()).hexdigest() == plan['worker_sha256'], 'Saved maintenance worker changed')
    unit = administrative(SYSTEMD / 'homestead-longhorn-runtime.service')
    require(unit.read_text() == unit_text(plan), 'Boot maintenance unit is missing or changed')
    dropin = administrative(SYSTEMD / (plan['service'] + '.service.d/homestead-runtime.conf'))
    require(dropin.read_text() == GUARD,
            'Kubernetes startup guard is missing or changed')
    retained = administrative(plan['retained'])
    require(not mounted(retained) and not list(retained.iterdir()) and 'i' in run('lsattr','-d',str(retained)).split()[0],
            'Retained mount placeholder is not protected against root-backed writes')
    require(FSTAB.read_text() == plan['fstab'] and metadata(FSTAB) == plan['fstab_metadata'], 'Original mount configuration changed')
    source_check(plan, SOURCE)
    run('systemctl', 'daemon-reload')
    for property_name in ('Requires', 'After'):
        require('homestead-longhorn-runtime.service' in run('systemctl', 'show', plan['service'], '-p', property_name, '--value').split(),
                'The effective Kubernetes startup dependencies do not include runtime maintenance')
    require(not (SYSTEMD / 'homestead-longhorn-runtime.service.d').exists(), 'Unexpected maintenance unit overrides exist')


def runtime_check(plan, folder):
    require(root_identity() == plan['root'], 'OS filesystem identity changed')
    require(SOURCE.stat().st_dev == (folder / 'active').stat().st_dev and SOURCE.stat().st_ino == (folder / 'active').stat().st_ino,
            'The fixed runtime path is not the verified destination bind mount')
    require(mount(SOURCE, 'FSTYPE') == plan['root']['filesystem'], 'Runtime filesystem type changed')
    require('rw' in mount(SOURCE, 'OPTIONS').split(','), 'Runtime destination is read-only')
    require(not (SOURCE / 'longhorn-disk.cfg').exists(), 'Runtime destination became a replica store')


def boot(folder):
    global PINNED
    plan, state = read(folder / 'plan.json'), read(folder / 'state.json')
    if state['phase'] == 'retired':
        return _boot(folder)
    device = identity(plan)
    descriptor = os.open(device, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        PINNED = descriptor
        return _boot(folder)
    finally:
        PINNED = None
        os.close(descriptor)


def _boot(folder):
    plain(folder, True)
    plan, state = read(folder / 'plan.json'), read(folder / 'state.json')
    phase = state['phase']
    require(phase != 'unarmed', 'Registration staging is incomplete. Recheck the saved job before reboot recovery.')
    if phase == 'retired':
        runtime_check(plan, folder)
        return state
    offline(plan)
    identity(plan)
    retained = Path(plan['retained'])
    expected_fstab = fstab_lines(plan)
    require(FSTAB.read_text() in (plan['fstab'], expected_fstab), 'fstab changed outside runtime maintenance')
    require(metadata(FSTAB) == plan['fstab_metadata'], 'fstab ownership or permissions changed')
    if phase in ('staged', 'copying'):
        source_check(plan, SOURCE)
        run('mount', '-o', 'remount,ro', str(SOURCE))
        require('ro' in mount(SOURCE, 'OPTIONS').split(','), 'Source did not become read-only')
        expected = source_check(plan, SOURCE)
        usage = os.statvfs(BASE)
        completed = sum(verified_partial(SOURCE,folder/name,expected) for name in ('active','archive'))
        remaining = 2 * sum(i.get('size', 0) for i in expected.values()) - completed
        require(usage.f_bavail * usage.f_frsize >= remaining + RESERVE,
                'OS free space changed; original is retained read-only')
        if phase == 'staged':
            save(folder, {'phase': 'copying', 'manifest': expected})
        else:
            require(expected == state['manifest'], 'Original runtime changed during interrupted copying')
        for name in ('active', 'archive'):
            copy_runtime(SOURCE, folder / name, expected)
        require(source_check(plan, SOURCE) == expected, 'Source changed during copying')
        state = {'phase': 'cutover', 'manifest': expected}
        save(folder, state)
        phase = 'cutover'
    if phase == 'cutover':
        offline(plan); identity(plan)
        require(manifest(folder / 'archive') == state['manifest'] and manifest(folder / 'active') == state['manifest'], 'Saved copies changed')
        plain(retained).mkdir(mode=0o700, exist_ok=True)
        durable(FSTAB, expected_fstab)
        run('systemctl', 'daemon-reload')
        # A restart may have mounted the new fstab already. Inspect every step;
        # never use a force/lazy unmount or replace an unexpected mount.
        if SOURCE.stat().st_dev != (folder / 'active').stat().st_dev:
            require(source_check(plan, SOURCE) == state['manifest'], 'Original runtime changed before cutover')
            run('umount', str(SOURCE))
        if not mounted(retained):
            require(not list(retained.iterdir()), 'Retained mount directory is not empty')
            run('mount', str(retained))
        require(source_check(plan, retained) == state['manifest'], 'Retained source differs from archive')
        require('ro' in mount(retained, 'OPTIONS').split(','), 'Retained source is not read-only')
        if not mounted(SOURCE): run('mount', str(SOURCE))
        runtime_check(plan, folder)
        state['phase'] = 'relocated'
        save(folder, state)
    elif phase in ('relocated', 'retire-staged', 'retiring'):
        runtime_check(plan, folder)
        current = source_check(plan, retained)
        require(manifest(folder / 'archive') == state['manifest'], 'Retained archive changed')
        if phase == 'relocated':
            require(current == state['manifest'], 'Retained source changed')
        else:
            require(Path('/proc/sys/kernel/random/boot_id').read_text().strip() != state['armed_boot'],
                    'Retirement requires a new reboot through host maintenance')
            require(all(rel in state['manifest'] and entry == state['manifest'][rel] for rel, entry in current.items()),
                    'Unexpected contents appeared on the retained filesystem')
            if phase == 'retire-staged': require(current == state['manifest'], 'Source changed after retirement review')
            state['phase'] = 'retiring'; save(folder, state)
            run('mount', '-o', 'remount,rw', str(retained))
            identity(plan); offline(plan)
            current = source_check(plan, retained)
            require(all(state['manifest'].get(rel) == entry for rel, entry in current.items()), 'Source changed at retirement boundary')
            # Socket endpoints cannot be archived. They are known ephemeral
            # runtime entries, and are removed only in this approved offline
            # retirement after all process/namespace consumers were excluded.
            for name in ('unix-domain-socket', 'instance-manager'):
                root = retained / name
                if root.exists():
                    for path in root.rglob('*'):
                        if stat.S_ISSOCK(path.lstat().st_mode): path.unlink()
            for rel, entry in sorted(current.items(), key=lambda pair: pair[0].count('/'), reverse=True):
                path = retained / rel
                # Rechecking each entry is intentional: no recursive delete.
                if entry['directory']:
                    # Only known ephemeral sockets can remain; never erase them.
                    path.rmdir()
                else: path.unlink()
            run('sync', '-f', str(retained))
            run('mount', '-o', 'remount,ro', str(retained))
            require(not source_check(plan, retained), 'Runtime retirement did not leave an empty store')
            state['phase'] = 'retired'; save(folder, state)
    else:
        require(phase == 'cutover', 'Unknown maintenance phase')
    return state


def retirement(folder):
    plan, state = read(folder / 'plan.json'), read(folder / 'state.json')
    require(state['phase'] == 'relocated', 'Relocation must finish before retirement')
    runtime_check(plan, folder)
    require(source_check(plan, Path(plan['retained'])) == state['manifest'], 'Retained runtime changed')
    require(manifest(folder / 'archive') == state['manifest'], 'Runtime archive could not be verified')
    return state


def arm_retirement(folder):
    state = retirement(folder)
    state['phase'] = 'retire-staged'
    state['armed_boot'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    save(folder, state)
    return state


def main():
    folder = plain(Path(sys.argv[2]), True)
    require(folder.parent == BASE, 'Unexpected maintenance directory')
    try:
        action={'boot':boot,'retirement':retirement,'recover-staging':recover_staging}.get(sys.argv[1])
        require(action is not None,'Unknown maintenance action')
        result = action(folder)
        print(json.dumps(result))
    except Exception as error:
        # The last durable phase is retained, so failure never grants startup
        # or silently retries an unrelated mutation.
        durable(folder / 'failure.txt', str(error) + '\n')
        print('Longhorn runtime maintenance blocked: ' + str(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
