"""Restoring from a snapshot replaces nothing the person did not review."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.kube import Kube
import homestead_kube as K
import homestead_leader as LEADER
import homestead_snapshot_files as S
import homestead_snapshot_restore as R
import homestead_route_policy as POLICY

NAME = S.PREFIX + 'a' * 24
LAUNCH = R._launch   # the real one: setUp stands a recorder in for it


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.state = {'state': 'ready', 'expires': time.time() + 1200, 'source': {'snapshot': 'daily'}}
        self.walk = [
            {'path': 'app.conf', 'kind': 'file', 'size': 10, 'modified': 100},
            {'path': 'conf.d', 'kind': 'dir', 'size': 0, 'modified': 100},
            {'path': 'conf.d/new.conf', 'kind': 'file', 'size': 5, 'modified': 100},
        ]
        self.live = {'app.conf': {'kind': 'file', 'size': 7, 'modified': 200}, 'conf.d': {'kind': 'dir', 'size': 0, 'modified': 50}}
        self.users = ([], ['ReadWriteOnce'])
        self.unanswered = set()
        self.launched = []
        patches = [
            mock.patch.object(R.SNAP, 'status', side_effect=lambda ns, name: dict(self.state)),
            mock.patch.object(R.SNAP, '_verified', return_value=None),
            mock.patch.object(R, '_walk', side_effect=lambda ns, session, folder, names: ([dict(r) for r in self.walk], False)),
            mock.patch.object(R, '_inspect', side_effect=lambda ns, pvc, folder, rels: {r: dict(self.live.get(r, {'kind': 'missing'})) for r in rels if r not in self.unanswered}),
            mock.patch.object(R, '_users', side_effect=lambda ns, pvc: self.users),
            mock.patch.object(R.FILES, '_session', side_effect=lambda ns, pvc: (ns, 'homestead-files-' + pvc)),
            mock.patch.object(R, '_launch', side_effect=lambda *args: self.launched.append(args)),
            # The restore claim is a Lease, read and written through the
            # module's bound client, here the fake API server.
            mock.patch.object(R.SNAP, 'kget', K.kget),
            mock.patch.object(R.SNAP, 'ksend', K.ksend),
            mock.patch.object(LEADER, 'IDENTITY', 'homestead-a'),
            mock.patch.object(R, 'RENEW_EVERY', 0.01, create=True),
        ]
        self.kube = Kube()
        self.kube.__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(R._RUNNING.clear)
        self.addCleanup(S.ACTIVE_RESTORES.clear)
        self.addCleanup(S.CLOSE_AFTER_RESTORE.clear)
        self.addCleanup(R.FILES.BUSY_CLAIMS.clear)
        self.other_copy = []   # holds as_another_copy set aside
        self.threads_before = set(threading.enumerate())
        self.addCleanup(self.stop_renewing)

    def stop_renewing(self):
        """Restores the tests leave running keep renewing their claim: stop
        them before the fake API server goes. A claim thread still alive
        after this would call the real API server once the fakes are gone."""
        for hold in list(R._HOLDERS.values()) + self.other_copy:
            hold['stop'].set()
            hold['thread'].join(2)
        R._HOLDERS.clear()
        alive = [t for t in set(threading.enumerate()) - self.threads_before if t.name == 'snapshot-restore-claim']
        self.assertEqual([], alive, 'a restore claim thread outlived the test: stop it in stop_renewing')

    def body(self, **extra):
        return {'namespace': 'lab', 'session': NAME, 'folder': 'config', 'paths': ['config/app.conf', 'config/conf.d'],
                'target': {'namespace': 'lab', 'pvc': 'data', 'folder': 'config'}, **extra}

    def test_plan_lists_adds_and_replaces_with_both_sizes(self):
        plan = R.restore_plan(self.body())
        self.assertTrue(plan['ready'], plan['blockers'])
        self.assertEqual(['conf.d/new.conf'], plan['adds'])
        self.assertEqual([{'path': 'app.conf', 'live': {'size': 7, 'modified': 200}, 'snapshot': {'size': 10, 'modified': 100}}],
                         plan['replaces'])
        self.assertEqual([], plan['skipped'])
        self.assertEqual(self.state['expires'], plan['session_ends_at'])
        self.assertRegex(plan['review_token'], r'^[0-9a-f]{20}$')

    def test_plan_warns_when_the_claim_is_in_use(self):
        self.users = (['grafana'], ['ReadWriteOnce'])
        plan = R.restore_plan(self.body())
        self.assertTrue(plan['ready'])
        self.assertEqual(['grafana'], plan['in_use'])
        self.assertEqual("grafana is running and using this volume, so it sees these files change while it runs. "
                         "For a database, stop it first.", plan['warning'])
        self.users = (['grafana'], ['ReadWriteOncePod'])
        plan = R.restore_plan(self.body())
        self.assertFalse(plan['ready'])
        self.assertIn('Stop the app first', plan['blockers'][0])

    def test_run_refuses_a_changed_review(self):
        token = R.restore_plan(self.body())['review_token']
        self.live['app.conf']['modified'] = 300
        reply = R.restore(self.body(review_token=token))
        self.assertEqual(409, reply.code)
        self.assertNotEqual(token, reply.body['plan']['review_token'])
        self.assertEqual([], self.launched)
        self.assertEqual({}, R._RUNNING)
        self.assertEqual('', self.claim()['spec']['holderIdentity'])   # the claim is given back
        self.assertEqual({}, R._HOLDERS)

    def test_the_review_is_checked_while_the_claim_is_held(self):
        token = R.restore_plan(self.body())['review_token']
        holders = []
        def walk(ns, session, folder, names):
            holders.append(self.claim()['spec']['holderIdentity'])
            return [dict(r) for r in self.walk], False
        with mock.patch.object(R, '_walk', side_effect=walk):
            self.assertEqual(202, R.restore(self.body(review_token=token)).code)
        self.assertTrue(holders[0].startswith('homestead-a/'), holders)

    def test_run_replaces_only_reviewed_paths_and_never_deletes(self):
        token = R.restore_plan(self.body())['review_token']
        reply = R.restore(self.body(review_token=token))
        self.assertEqual(202, reply.code)
        self.assertEqual('Restore 2 files from snapshot daily', reply.body['job']['title'])
        job, src_ns, src_pod, dst_ns, dst_pod, folder, reader = self.launched[0]
        self.assertEqual((src_ns, src_pod, dst_ns, dst_pod, folder), ('lab', NAME, 'lab', 'homestead-files-data', 'config'))
        self.assertEqual(['python3', '-c', S.READER, '/data', 'tarlist', 'config'], reader[:6])
        # The live folder conf.d is left as it is: only what was reviewed is sent.
        self.assertEqual(['app.conf', 'conf.d/new.conf'], json.loads(reader[6]))

    def test_symlink_escaping_the_claim_is_skipped(self):
        self.walk += [
            {'path': 'root', 'kind': 'link', 'size': 0, 'modified': 1, 'target': '/'},
            {'path': 'up', 'kind': 'link', 'size': 0, 'modified': 1, 'target': '../../x'},
            {'path': 'ok', 'kind': 'link', 'size': 0, 'modified': 1, 'target': 'app.conf'},
            {'path': 'fifo', 'kind': 'other', 'size': 0, 'modified': 1},
        ]
        self.live['conf.d'] = {'kind': 'via-link'}
        plan = R.restore_plan(self.body())
        skipped = {row['path']: row['why'] for row in plan['skipped']}
        self.assertEqual({'root', 'up', 'fifo', 'conf.d'}, set(skipped))
        self.assertIn('outside the volume', skipped['root'])
        self.assertIn('link', skipped['conf.d'])
        self.assertEqual(['ok'], plan['adds'])  # nothing under conf.d is offered either

    def test_an_entry_the_live_check_did_not_answer_is_skipped(self):
        self.unanswered = {'conf.d/new.conf'}
        plan = R.restore_plan(self.body())
        self.assertEqual([], plan['adds'])
        self.assertIn('conf.d/new.conf', [row['path'] for row in plan['skipped']])

    def test_expired_session_refuses(self):
        token = R.restore_plan(self.body())['review_token']
        for state in ('closing', 'closed'):
            self.state['state'] = state
            for call in (lambda: R.restore_plan(self.body()), lambda: R.restore(self.body(review_token=token))):
                with self.assertRaises(ValueError) as caught:
                    call()
                self.assertEqual("This snapshot's browsing session ended. Open it again.", str(caught.exception))
        self.assertEqual([], self.launched)

    def test_session_about_to_end_is_a_blocker(self):
        self.state['expires'] = time.time() + 120
        plan = R.restore_plan(self.body())
        self.assertFalse(plan['ready'])
        self.assertIn('Open it again', plan['blockers'][0])

    def test_second_restore_into_the_claim_is_409(self):
        token = R.restore_plan(self.body())['review_token']
        self.assertEqual(202, R.restore(self.body(review_token=token)).code)
        self.assertEqual(1, S.ACTIVE_RESTORES[('lab', NAME)])
        reply = R.restore(self.body(review_token=token))
        self.assertEqual(409, reply.code)
        self.assertIn('running', reply.body['error'])
        self.assertEqual(1, len(self.launched))

    def test_finishing_a_restore_frees_the_claim_and_session(self):
        token = R.restore_plan(self.body())['review_token']
        R.restore(self.body(review_token=token))
        job = self.launched[0][0]
        R._finished(job, 'lab', 'data', 'lab', NAME)
        self.assertEqual({}, R._RUNNING)
        self.assertEqual(0, S.ACTIVE_RESTORES[('lab', NAME)])

    def test_bad_input_is_refused_before_any_exec(self):
        for body in (self.body(paths=['other/app.conf']), self.body(paths=['config/../x']), self.body(paths=[]),
                     self.body(target={'namespace': 'lab', 'pvc': NAME, 'folder': ''})):
            with self.assertRaises((ValueError, PermissionError)):
                R.restore_plan(body)
        R._walk.assert_not_called()

    def test_two_restores_from_one_session_keep_it_until_both_end(self):
        token = R.restore_plan(self.body())['review_token']
        R.restore(self.body(review_token=token))
        other = self.body(target={'namespace': 'lab', 'pvc': 'other', 'folder': 'config'})
        R.restore(dict(other, review_token=R.restore_plan(other)['review_token']))
        first, second = self.launched[0][0], self.launched[1][0]
        R._finished(first, 'lab', 'data', 'lab', NAME)
        self.assertEqual(1, S.ACTIVE_RESTORES[('lab', NAME)])
        R._finished(second, 'lab', 'other', 'lab', NAME)
        self.assertEqual(0, S.ACTIVE_RESTORES[('lab', NAME)])

    def test_closing_during_a_restore_waits_for_it(self):
        token = R.restore_plan(self.body())['review_token']
        R.restore(self.body(review_token=token))
        self.assertIn(('lab', 'data'), R.FILES.BUSY_CLAIMS)
        with mock.patch.object(S, '_record', side_effect=AssertionError('nothing is removed yet')):
            self.assertEqual('closing', S.close('lab', NAME)['state'])
        with mock.patch.object(S, 'close') as close:
            R._finished(self.launched[0][0], 'lab', 'data', 'lab', NAME)
            close.assert_called_once_with('lab', NAME)
        self.assertNotIn(('lab', 'data'), R.FILES.BUSY_CLAIMS)

    def test_a_restore_that_could_not_start_frees_the_claim(self):
        token = R.restore_plan(self.body())['review_token']
        with mock.patch.object(R, '_launch', side_effect=RuntimeError('no thread')):
            with self.assertRaises(RuntimeError):
                R.restore(self.body(review_token=token))
        self.assertEqual({}, R._RUNNING)
        self.assertEqual(0, S.ACTIVE_RESTORES[('lab', NAME)])
        self.assertNotIn(('lab', 'data'), R.FILES.BUSY_CLAIMS)
        self.assertEqual('', self.claim()['spec']['holderIdentity'])

    def claim(self, pvc='data'):
        leases = K.kget(f'/apis/coordination.k8s.io/v1/namespaces/{LEADER.NS}/leases')['items']
        mine = [l for l in leases if l['metadata'].get('annotations', {}).get('homestead.io/restore-into') == f'lab/{pvc}']
        self.assertEqual(1, len(mine), leases)
        return mine[0]

    def as_another_copy(self):
        """From here on the calls come from a second copy of Homestead: its own
        process memory, which knows nothing of the first copy's restore. The
        first copy's claim threads are set aside, still renewing, and stopped
        at the end of the test."""
        with R._LOCK:
            self.other_copy += R._HOLDERS.values()
            R._HOLDERS.clear()
        R._RUNNING.clear()
        R.FILES.BUSY_CLAIMS.clear()
        S.ACTIVE_RESTORES.clear()
        LEADER.IDENTITY = 'homestead-b'

    def test_a_restore_running_in_another_copy_of_homestead_is_409(self):
        token = R.restore_plan(self.body())['review_token']
        self.assertEqual(202, R.restore(self.body(review_token=token)).code)
        self.as_another_copy()
        reply = R.restore(self.body(review_token=token))
        self.assertEqual(409, reply.code)
        self.assertEqual(R.ELSEWHERE, reply.body['error'])
        self.assertEqual('Another copy of Homestead is restoring files into this volume. Try again when it finishes. '
                         'If that copy stopped, the volume is free again within a minute.', R.ELSEWHERE)
        self.assertTrue(reply.body['plan']['ready'])   # the review is still shown with the refusal
        self.assertEqual(1, len(self.launched))
        self.assertEqual({}, R._RUNNING)
        self.assertNotIn(('lab', 'data'), R.FILES.BUSY_CLAIMS)

    def test_the_claim_is_short_and_frees_soon_after_a_restart(self):
        token = R.restore_plan(self.body())['review_token']
        R.restore(self.body(review_token=token))
        spec = self.claim()['spec']
        self.assertTrue(spec['holderIdentity'].startswith('homestead-a/'))
        self.assertEqual(60, spec['leaseDurationSeconds'])

    def run_copy(self, during):
        """Start a restore with the real launch; the copy calls during(job)
        and then ends as the transfer does. Returns the job once it ended."""
        token = R.restore_plan(self.body())['review_token']
        def transfer(job, *args, **kwargs):
            during(job)
            job.update(status='done', finished=time.time())
        with mock.patch.object(R, '_launch', LAUNCH), mock.patch.object(R.FILES, '_transfer', side_effect=transfer):
            reply = R.restore(self.body(review_token=token))
            self.assertEqual(202, reply.code)
            # The copy's thread ends after the claim is given back.
            self.wait(lambda: not any(t.name == 'snapshot-restore' for t in threading.enumerate()))
        return R.FILES._JOBS[reply.body['job']['id']]

    def wait(self, ready, seconds=3):
        end = time.time() + seconds
        while not ready():
            self.assertLess(time.time(), end, 'timed out')
            time.sleep(0.01)

    def test_the_claim_is_renewed_while_the_restore_copies(self):
        first = []
        def during(job):
            first.append(self.claim()['spec']['renewTime'])
            self.wait(lambda: self.claim()['spec']['renewTime'] != first[0])
        job = self.run_copy(during)
        self.assertEqual('done', job['status'])
        self.assertEqual('', self.claim()['spec']['holderIdentity'])
        writes = len(self.kube.writes)
        time.sleep(0.1)
        self.assertEqual(writes, len(self.kube.writes))   # no renewing after the restore ended

    def test_one_failed_renewal_does_not_lose_the_claim(self):
        path = f'/apis/coordination.k8s.io/v1/namespaces/{LEADER.NS}/leases/{R._claim_name("lab", "data")}'
        def during(job):
            self.kube.fail(path, 500, method='PUT')
            puts = lambda: sum(1 for w in self.kube.writes if w[0] == 'PUT')
            self.wait(lambda: puts() >= 2)
        job = self.run_copy(during)
        self.assertEqual('done', job['status'])
        self.assertEqual('', job['error'])

    def test_renewals_failing_for_60_seconds_lose_the_claim(self):
        path = f'/apis/coordination.k8s.io/v1/namespaces/{LEADER.NS}/leases/{R._claim_name("lab", "data")}'
        self.kube.fail(path, 500, times=10 ** 6, method='PUT')
        clock = iter(range(0, 10 ** 6, 10))   # each look at the clock is 10 s later
        with mock.patch.object(R, '_clock', side_effect=lambda: next(clock)):
            job = self.run_copy(lambda job: self.wait(lambda: R._HOLDERS[('lab', 'data')]['lost']))
        self.assertEqual('failed', job['status'])
        self.assertEqual(R.LOST, job['error'])

    def test_a_take_over_after_the_last_renewal_is_still_reported(self):
        # No renewal runs during this copy, and it ends long after the last
        # one: Homestead checks the claim once more before calling it done.
        now = [0]
        def during(job):
            self.take_over()
            now[0] = R.RENEW_EVERY + 60
        with mock.patch.object(R, 'RENEW_EVERY', 1000), mock.patch.object(R, '_clock', side_effect=lambda: now[0]):
            job = self.run_copy(during)
        self.assertEqual('failed', job['status'])
        self.assertEqual(R.LOST, job['error'])

    def take_over(self):
        """Another copy of Homestead takes the claim (this one's renewals did
        not reach the API server in time)."""
        lease = self.claim()
        lease['spec']['holderIdentity'] = 'homestead-b/0000'
        K.ksend('PUT', f"/apis/coordination.k8s.io/v1/namespaces/{LEADER.NS}/leases/{lease['metadata']['name']}", lease)

    def test_a_restore_that_loses_the_claim_while_copying_says_so(self):
        def during(job):
            self.take_over()
            self.wait(lambda: R._HOLDERS[('lab', 'data')]['lost'])
        job = self.run_copy(during)
        self.assertEqual('failed', job['status'])
        self.assertEqual(R.LOST, job['error'])
        self.assertEqual('Another restore may have written into this volume at the same time. Check the restored files.', R.LOST)
        self.assertEqual('homestead-b/0000', self.claim()['spec']['holderIdentity'])   # not taken from the other copy

    def test_a_claim_lost_before_the_copy_starts_stops_the_restore(self):
        token = R.restore_plan(self.body())['review_token']
        def walk(ns, session, folder, names):
            if ('lab', 'data') in R._HOLDERS:
                self.take_over()
                self.wait(lambda: R._HOLDERS[('lab', 'data')]['lost'])
            return [dict(r) for r in self.walk], False
        with mock.patch.object(R, '_walk', side_effect=walk):
            reply = R.restore(self.body(review_token=token))
        self.assertEqual(409, reply.code)
        self.assertEqual(R.NOT_HELD, reply.body['error'])
        self.assertEqual([], self.launched)
        self.assertEqual({}, R._RUNNING)
        self.assertEqual('homestead-b/0000', self.claim()['spec']['holderIdentity'])

    def test_a_restore_into_another_volume_is_not_held_up(self):
        token = R.restore_plan(self.body())['review_token']
        R.restore(self.body(review_token=token))
        self.as_another_copy()
        other = self.body(target={'namespace': 'lab', 'pvc': 'other', 'folder': 'config'})
        self.assertEqual(202, R.restore(dict(other, review_token=R.restore_plan(other)['review_token'])).code)

    def test_the_claim_is_given_back_when_the_restore_ends(self):
        token = R.restore_plan(self.body())['review_token']
        R.restore(self.body(review_token=token))
        R._finished(self.launched[0][0], 'lab', 'data', 'lab', NAME)
        self.assertEqual('', self.claim()['spec']['holderIdentity'])
        self.as_another_copy()
        self.assertEqual(202, R.restore(self.body(review_token=token)).code)

    def test_a_claim_that_ran_out_is_taken_over(self):
        token = R.restore_plan(self.body())['review_token']
        R.restore(self.body(review_token=token))
        self.as_another_copy()
        # The first copy stopped: no renewal of its comes after the claim ran out.
        for hold in self.other_copy:
            hold['stop'].set()
            hold['thread'].join(2)
        lease = self.claim()
        lease['spec'].update(renewTime='2020-01-01T00:00:00.000000Z', leaseDurationSeconds=60)
        K.ksend('PUT', f"/apis/coordination.k8s.io/v1/namespaces/{LEADER.NS}/leases/{lease['metadata']['name']}", lease)
        self.assertEqual(202, R.restore(self.body(review_token=token)).code)
        self.assertTrue(self.claim()['spec']['holderIdentity'].startswith('homestead-b/'))

    def test_two_copies_taking_the_claim_at_once_start_one_restore(self):
        token = R.restore_plan(self.body())['review_token']
        # The other copy created the Lease between this copy's read and its write.
        self.kube.fail(f'/apis/coordination.k8s.io/v1/namespaces/{LEADER.NS}/leases', 409, method='POST')
        reply = R.restore(self.body(review_token=token))
        self.assertEqual(409, reply.code)
        self.assertTrue(reply.body['error'].startswith('Another copy of Homestead is restoring files into this volume.'))
        self.assertEqual([], self.launched)
        self.assertEqual({}, R._RUNNING)

    def test_a_restore_whose_helper_pod_did_not_start_gives_the_claim_back(self):
        token = R.restore_plan(self.body())['review_token']
        with mock.patch.object(R.FILES, '_session', side_effect=RuntimeError('no helper pod')):
            with self.assertRaises(RuntimeError):
                R.restore(self.body(review_token=token))
        self.assertEqual('', self.claim()['spec']['holderIdentity'])
        self.assertEqual({}, R._RUNNING)

    def test_a_claim_that_cannot_be_written_stops_the_restore(self):
        token = R.restore_plan(self.body())['review_token']
        self.kube.fail(f'/apis/coordination.k8s.io/v1/namespaces/{LEADER.NS}/leases', 403, method='POST')
        with self.assertRaises(Exception):
            R.restore(self.body(review_token=token))
        self.assertEqual([], self.launched)
        self.assertEqual({}, R._RUNNING)

    def test_a_restore_too_large_for_the_time_left_is_a_blocker(self):
        self.state['expires'] = time.time() + 900
        self.walk[0]['size'] = 20 * 1024 ** 3
        plan = R.restore_plan(self.body())
        self.assertFalse(plan['ready'])
        self.assertIn('Open it again', plan['blockers'][0])

    def test_restore_routes_are_admin(self):
        import homestead_storage_server as STORAGE
        for path in ('/api/snapshot-files/restore/plan', '/api/snapshot-files/restore'):
            self.assertEqual('admin', STORAGE.ROUTES[('POST', path)][0])
            self.assertEqual('admin', POLICY.role(path, 'POST'))


class CleanupTests(unittest.TestCase):
    def test_cleanup_keeps_a_session_while_its_restore_runs(self):
        record = {'metadata': {'name': NAME, 'labels': {S.LABEL: 'true', 'app.kubernetes.io/managed-by': 'homestead'},
                               'annotations': {'homestead.io/expires': '1'}},
                  'spec': {'volumeSnapshotRef': {'namespace': 'lab'}}}
        with mock.patch.object(S.KUBE, 'get_optional', return_value={}), mock.patch.object(S, '_items', return_value=[record]), \
                mock.patch.object(S, 'close') as close:
            S.ACTIVE_RESTORES[('lab', NAME)] += 1
            try:
                S.cleanup()
                close.assert_not_called()
            finally:
                S.ACTIVE_RESTORES.clear()
            S.cleanup()
            close.assert_called_once_with('lab', NAME)


@unittest.skipUnless(os.name == 'posix', 'the reader and the helper script run on Linux')
class HelperTests(unittest.TestCase):
    def reader(self, root, action, folder, extra):
        return subprocess.run([sys.executable, '-c', S.READER, str(root), action, folder, extra], capture_output=True, timeout=10)

    def tree(self, tmp):
        snap = Path(tmp) / 'snap' / 'config'; (snap / 'conf.d').mkdir(parents=True)
        (Path(tmp) / 'secret').write_text('private')
        (snap / 'app.conf').write_text('from the snapshot')
        (snap / 'conf.d' / 'new.conf').write_text('new')
        (snap / 'ok').symlink_to('app.conf')
        (snap / 'escape').symlink_to(Path(tmp) / 'secret')
        return snap.parent

    def test_walk_reports_links_without_following_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            run = self.reader(root, 'walk', 'config', json.dumps(['app.conf', 'conf.d', 'ok', 'escape']))
            self.assertEqual(0, run.returncode, run.stderr)
            rows = {r['path']: r for r in json.loads(run.stdout)['entries']}
            self.assertEqual(['app.conf', 'conf.d', 'conf.d/new.conf', 'escape', 'ok'], sorted(rows))
            self.assertEqual(('link', 'app.conf'), (rows['ok']['kind'], rows['ok']['target']))
            self.assertEqual(('file', 17), (rows['app.conf']['kind'], rows['app.conf']['size']))

    def test_restored_tar_replaces_reviewed_files_and_keeps_extra_live_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            live = Path(tmp) / 'live'; (live / 'conf.d').mkdir(parents=True)
            (live / 'app.conf').write_text('changed live')
            (live / 'conf.d' / 'extra.conf').write_text('only live')
            (live / 'keep.txt').write_text('untouched')
            run = self.reader(root, 'tarlist', 'config', json.dumps(['app.conf', 'conf.d/new.conf', 'ok']))
            self.assertEqual(0, run.returncode, run.stderr)
            with tarfile.open(fileobj=io.BytesIO(run.stdout)) as tar:
                self.assertEqual(['app.conf', 'conf.d/new.conf', 'ok'], sorted(tar.getnames()))
            subprocess.run(['tar', '-xpf', '-'], input=run.stdout, cwd=live, check=True)
            self.assertEqual('from the snapshot', (live / 'app.conf').read_text())
            self.assertEqual('new', (live / 'conf.d' / 'new.conf').read_text())
            self.assertEqual('only live', (live / 'conf.d' / 'extra.conf').read_text())
            self.assertEqual('untouched', (live / 'keep.txt').read_text())
            self.assertEqual('app.conf', os.readlink(live / 'ok'))

    def test_tarlist_refuses_climbing_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            for bad in (['../secret'], ['conf.d/../../secret'], [''], ['/etc/passwd']):
                self.assertNotEqual(0, self.reader(root, 'tarlist', 'config', json.dumps(bad)).returncode, bad)
            (root / 'config' / 'linkdir').symlink_to(Path(tmp))
            self.assertNotEqual(0, self.reader(root, 'tarlist', 'config', json.dumps(['linkdir/secret'])).returncode)

    def test_inspection_records_split_only_on_newlines(self):
        rows = R._parse_inspect('regular file|3|5|a\u2028link|0|0|b\rc\n'.encode())
        self.assertEqual(['a\u2028link|0|0|b\rc'], list(rows))
        odd = b'missing|0|0|caf\xe9\n'
        self.assertEqual(['caf\udce9'], list(R._parse_inspect(odd)))

    def test_tarlist_fails_when_a_reviewed_entry_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            run = self.reader(root, 'tarlist', 'config', json.dumps(['app.conf', 'vanished.conf']))
            self.assertNotEqual(0, run.returncode)
            self.assertIn(b'vanished.conf', run.stderr)

    def test_live_inspection_flags_links_on_the_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / 'live'; (base / 'dir').mkdir(parents=True)
            (base / 'dir' / 'file').write_text('12345')
            (base / 'via').symlink_to(Path(tmp))
            (base / 'link').symlink_to('dir')
            rels = ['dir', 'dir/file', 'dir/missing/deeper', 'via/secret', 'link', 'link/file', 'gone']
            run = subprocess.run(['sh', '-c', R._inspect_script(str(base))], input='\n'.join(rels).encode() + b'\n',
                                 capture_output=True, timeout=10)
            self.assertEqual(0, run.returncode, run.stderr)
            kinds = {rel: row['kind'] for rel, row in R._parse_inspect(run.stdout).items()}
            self.assertEqual({'dir': 'dir', 'dir/file': 'file', 'dir/missing/deeper': 'missing', 'via/secret': 'via-link',
                              'link': 'link', 'link/file': 'via-link', 'gone': 'missing'}, kinds)
            self.assertEqual(5, R._parse_inspect(run.stdout)['dir/file']['size'])


if __name__ == '__main__':
    unittest.main()
