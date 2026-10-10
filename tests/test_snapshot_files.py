"""Snapshot browsing never mounts the live claim or deletes its recovery point."""
import base64
import ast
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.parse
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import homestead_snapshot_files as S
import homestead_route_policy as POLICY


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.name = S.PREFIX + 'a' * 24
        self.paths = S._paths('lab', self.name)
        self.objects = {
            S.CSI.API: {},
            S.LH + '/volumes/live': {'metadata': {'uid': 'live-uid'}, 'spec': {'size': '2147483648', 'dataEngine': 'v1'}},
            S.LH + '/snapshots/daily': {'metadata': {'uid': 'snap-uid'}, 'spec': {'volume': 'live'}, 'status': {'readyToUse': True, 'restoreSize': 1073741824}},
            '/api/v1/persistentvolumes': {'items': [{'metadata': {'name': 'live-pv'}, 'spec': {'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'live', 'fsType': 'ext4'}, 'claimRef': {'name': 'data', 'namespace': 'lab', 'uid': 'claim-uid'}}}]},
            '/api/v1/namespaces/lab/persistentvolumeclaims/data': {'metadata': {'uid': 'claim-uid'}, 'spec': {'volumeName': 'live-pv', 'storageClassName': 'longhorn'}, 'status': {'phase': 'Bound'}},
            S.SC + 'longhorn': {'metadata': {'name': 'longhorn', 'uid': 'sc-uid'}, 'provisioner': 'driver.longhorn.io', 'parameters': {'numberOfReplicas': '3', 'recurringJobSelector': 'danger'}},
            S.LH + '/engines': {'items': []},
        }
        self.sent = []
        def get(path):
            if '?labelSelector=' in path:
                return {'items': [copy.deepcopy(v) for k, v in self.objects.items() if k.startswith(path.split('?')[0] + '/')]}
            if path not in self.objects:
                raise urllib.error.HTTPError(path, 404, 'missing', {}, None)
            return copy.deepcopy(self.objects[path])
        def send(method, path, body=None, **kwargs):
            self.sent.append((method, path, copy.deepcopy(body)))
            if method == 'POST':
                key = path + '/' + body['metadata']['name']
                self.assertNotIn(key, self.objects)
                obj = copy.deepcopy(body)
                obj['metadata'].update(uid='uid-' + obj['kind'].lower(), resourceVersion='1')
                self.objects[key] = obj
                return copy.deepcopy(obj)
            if method == 'PATCH':
                self.objects[path]['metadata']['annotations'].update(body['metadata']['annotations'])
                return get(path)
            if method == 'DELETE':
                self.assertEqual(self.objects[path]['metadata']['uid'], body['preconditions']['uid'])
                del self.objects[path]
                return {}
            raise AssertionError(method)
        self.get = get
        for patcher in (patch.multiple(S, kget=get, ksend=send, image=lambda:'ghcr.io/test/app@sha256:' + 'a'*64, system_namespaces={'kube-system'}),
                        patch.object(S.CSI, 'ksend', send), patch.object(S.CSI, 'support', return_value={'ready': True})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def v2(self):
        self.objects[S.LH + '/volumes/live']['spec']['dataEngine'] = 'v2'
        self.objects['/apis/apiextensions.k8s.io/v1/customresourcedefinitions/volumes.longhorn.io'] = {'spec': {'versions': [{'served': True, 'schema': {'openAPIV3Schema': {'properties': {'spec': {'properties': {'cloneMode': {'enum': ['full-copy','linked-clone']}}}}}}}]}}
        self.objects['/apis/apps/v1/namespaces/longhorn-system/daemonsets/longhorn-csi-plugin'] = {'spec': {'template': {'spec': {'containers': [{'name': 'longhorn-csi-plugin', 'image': 'longhornio/longhorn-manager:v1.10.0'}]}}}}

    def start(self):
        plan = S.review('live', 'daily')
        body = dict(volume='live', snapshot='daily', request_id='a'*24, review_token=plan['review_token'])
        S.start(body)
        return body

    def clone(self, complete=True):
        pvc = self.objects[self.paths[2]]
        pvc['status'] = {'phase': 'Bound'}
        pvc['spec']['volumeName'] = 'copy-pv'
        self.objects['/api/v1/persistentvolumes/copy-pv'] = {'spec': {'claimRef': {'uid': pvc['metadata']['uid']}, 'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'copy'}}}
        plan = json.loads(self.objects[self.paths[0]]['metadata']['annotations']['homestead.io/source'])
        self.objects[S.LH + '/volumes/copy'] = {'metadata': {'name': 'copy'}, 'spec': {'cloneMode': plan['method']}, 'status': {'cloneStatus': {'sourceVolume': 'live', 'snapshot': 'daily', 'state': 'completed' if complete else 'initiated'}}}
        self.objects[self.paths[1]]['status'] = {'readyToUse': True}
        self.objects[self.paths[3]]['status'] = {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}

    def test_v1_copies_only_the_selected_snapshot_and_preserves_source(self):
        original = copy.deepcopy(self.objects)
        body = self.start()
        self.assertEqual('full-copy', S.review('live', 'daily')['method'])
        self.assertEqual('1073741824', self.objects[self.paths[2]]['spec']['resources']['requests']['storage'])
        self.assertEqual('Retain', self.objects[self.paths[0]]['spec']['deletionPolicy'])
        self.assertEqual('snap://live/daily', self.objects[self.paths[0]]['spec']['source']['snapshotHandle'])
        self.assertEqual('1', self.objects[S.SC + self.name]['parameters']['numberOfReplicas'])
        self.assertNotIn('recurringJobSelector', self.objects[S.SC + self.name]['parameters'])
        for key, obj in original.items():
            self.assertEqual(obj, self.objects[key])
        count = len(self.sent)
        # Kubernetes serializes byte quantities using their canonical suffix.
        self.objects[self.paths[2]]['spec']['resources']['requests']['storage'] = '1Gi'
        S.start(body)
        self.assertEqual(count, len(self.sent), 'retry must reuse the same session')

    def test_v2_uses_a_dedicated_linked_class_when_schema_and_driver_support_it(self):
        self.v2()
        self.start()
        params = self.objects[S.SC + self.name]['parameters']
        self.assertEqual('linked-clone', params['cloneMode'])
        self.assertEqual('v2', params['dataEngine'])
        self.assertEqual('1', params['numberOfReplicas'])

    def test_v2_unknown_driver_reviews_full_copy_without_claiming_fast_clone(self):
        self.v2()
        del self.objects['/apis/apps/v1/namespaces/longhorn-system/daemonsets/longhorn-csi-plugin']
        self.assertEqual('full-copy', S.review('live', 'daily')['method'])

    def test_capability_change_requires_another_review(self):
        plan = S.review('live', 'daily')
        self.v2()
        with self.assertRaisesRegex(ValueError, 'changed'):
            S.start(dict(volume='live', snapshot='daily', request_id='a'*24, review_token=plan['review_token']))
        self.assertEqual([], self.sent)

    def test_live_pod_is_not_enough_to_call_copy_ready(self):
        self.start(); self.clone(False)
        self.objects[S.LH + '/engines']['items'] = [{'spec': {'volumeName': 'copy'}, 'status': {'cloneStatus': {'replica': {'snapshotName': 'daily', 'progress': 37, 'isCloning': True}}}}]
        state = S.status('lab', self.name)
        self.assertEqual(('preparing', 'clone', 37), (state['state'], state['stage'], state['percent']))
        self.clone()
        self.assertEqual('ready', S.status('lab', self.name)['state'])

    def test_percent_is_unknown_until_longhorn_reports_it(self):
        self.start(); self.clone(False)
        self.assertIsNone(S.status('lab', self.name)['percent'])

    def test_wrong_snapshot_or_live_volume_binding_is_never_browsable(self):
        self.start(); self.clone()
        self.objects[S.LH + '/volumes/copy']['status']['cloneStatus']['snapshot'] = 'other'
        with self.assertRaisesRegex(ValueError, 'selected snapshot'):
            S.read('lab', self.name, 'list')
        self.objects['/api/v1/persistentvolumes/copy-pv']['spec']['csi']['volumeHandle'] = 'live'
        with self.assertRaisesRegex(ValueError, 'live volume'):
            S.read('lab', self.name, 'list')

    def test_linked_clone_cannot_silently_become_full_copy(self):
        self.v2(); self.start(); self.clone()
        self.objects[S.LH + '/volumes/copy']['spec']['cloneMode'] = 'full-copy'
        with self.assertRaisesRegex(ValueError, 'linked-clone'):
            S.read('lab', self.name, 'list')
        # The rejected copy can still be cleaned up without touching live data.
        for _ in range(6):
            S.close('lab', self.name)
        self.assertNotIn(self.paths[2], self.objects)

    def test_cleanup_will_not_delete_a_claim_rebound_to_the_live_volume(self):
        self.start(); self.clone()
        self.objects['/api/v1/persistentvolumes/copy-pv']['spec']['csi']['volumeHandle'] = 'live'
        S.close('lab', self.name)
        with self.assertRaisesRegex(ValueError, 'live volume'):
            S.close('lab', self.name)
        self.assertIn(self.paths[2], self.objects)

    def test_read_only_mount_and_no_token_are_checked_before_exec(self):
        self.start(); self.clone()
        pod = self.objects[self.paths[3]]
        self.assertFalse(pod['spec']['automountServiceAccountToken'])
        pod['spec']['volumes'][0]['persistentVolumeClaim']['readOnly'] = False
        with patch.object(S.FILES, '_exec') as execute:
            with self.assertRaisesRegex(ValueError, 'mount'):
                S.read('lab', self.name, 'list')
            execute.assert_not_called()

    def test_reader_rejects_traversal_and_write_before_exec(self):
        self.start(); self.clone()
        with patch.object(S.FILES, '_exec') as execute:
            for path in ('../secret', 'a/../b', 'a//b', 'a\x00b'):
                with self.assertRaises(ValueError):
                    S.read('lab', self.name, 'stat', path)
            with self.assertRaises(ValueError):
                S.read('lab', self.name, 'write', 'file')
            execute.assert_not_called()

    def test_download_decodes_one_bounded_chunk(self):
        self.start(); self.clone()
        with patch.object(S.FILES, '_exec', return_value=(json.dumps({'size': 10, 'data': base64.b64encode(b'contents').decode()}), '')) as execute:
            self.assertEqual(b'contents', S.read('lab', self.name, 'chunk', 'config/file', 2)['data'])
            self.assertEqual(['/data', 'chunk', 'config/file', '2'], execute.call_args.args[2][-4:])

    def test_cleanup_orders_resources_and_never_deletes_source_snapshot(self):
        self.start()
        for _ in range(6):
            S.close('lab', self.name)
        deleted = [path for method, path, _ in self.sent if method == 'DELETE']
        self.assertEqual([self.paths[3], self.paths[2], self.paths[1], S.SC + self.name, self.paths[0]], deleted)
        self.assertIn(S.LH + '/snapshots/daily', self.objects)

    def test_expiry_recovers_cleanup_from_saved_records(self):
        self.start()
        self.objects[self.paths[0]]['metadata']['annotations']['homestead.io/expires'] = '1'
        with self.assertRaises(ValueError):
            S.read('lab', self.name, 'list')
        for _ in range(6):
            S.cleanup()
        self.assertEqual('closed', S.status('lab', self.name)['state'])

    def test_cleanup_refuses_an_unrelated_claim(self):
        self.start()
        S.close('lab', self.name)
        self.objects[self.paths[2]]['spec']['dataSource']['name'] = 'important'
        with self.assertRaisesRegex(ValueError, 'identity'):
            S.close('lab', self.name)
        self.assertIn(self.paths[2], self.objects)

    def test_live_file_api_cannot_write_a_snapshot_claim(self):
        with self.assertRaises(PermissionError):
            S.FILES.open_session('lab', self.name)

    def test_raw_block_claim_is_rejected_before_any_creation(self):
        self.objects['/api/v1/namespaces/lab/persistentvolumeclaims/data']['spec']['volumeMode'] = 'Block'
        with self.assertRaisesRegex(ValueError, 'filesystem'):
            self.start()
        self.assertEqual([], self.sent)

    def test_foreign_owner_prevents_cleanup_even_with_matching_labels(self):
        self.start(); S.close('lab', self.name)
        self.objects[self.paths[2]]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'unrelated'):
            S.close('lab', self.name)

    def test_cleanup_waits_for_pod_deletion_before_releasing_its_claim(self):
        self.start()
        with patch.object(S.CSI, '_delete') as delete:
            for _ in range(3):
                self.assertEqual('closing', S.close('lab', self.name)['state'])
            self.assertTrue(all(c.args[0] == self.paths[3] for c in delete.call_args_list))
        self.assertIn(self.paths[2], self.objects)

    def test_all_routes_require_admin(self):
        for method, action in [('GET', a) for a in ('plan','status','list','download')] + [('POST', a) for a in ('start','close')]:
            self.assertEqual('admin', POLICY.POLICY[method, '/api/snapshot-files/' + action])


class DownloadTests(unittest.TestCase):
    def run_download(self, chunks):
        # The module reads the copy a piece at a time (download); server.py
        # sends what it yields (_send_stream).
        import server
        reads = []
        def read(*args):
            reads.append(args)
            value = chunks.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        headers = {}
        h = SimpleNamespace(wfile=io.BytesIO(), close_connection=False, send_response=lambda _:None,
                            send_header=lambda k,v:headers.update({k:v}), _security_headers=lambda:None, end_headers=lambda:None)
        with mock.patch.object(server.SNAPSHOT_FILES, 'read', side_effect=read):
            stream = server.SNAPSHOT_FILES.download('lab', 'session', 'a\r\nX-Evil: yes.txt')
            server.H._send_stream(h, stream)
        return h, headers, reads

    def test_streams_each_chunk_and_escapes_the_attachment_filename(self):
        h, headers, reads = self.run_download([{'size':6}, {'size':6,'data':b'abc'}, {'size':6,'data':b'def'}])
        self.assertEqual(b'abcdef', h.wfile.getvalue())
        self.assertFalse(h.close_connection)
        self.assertEqual('6', headers['Content-Length'])
        self.assertNotIn('\r\n', headers['Content-Disposition'])
        self.assertEqual([0,3], [r[-1] for r in reads[1:]])

    def test_lost_helper_closes_a_partial_download_without_appending_json(self):
        h, _, _ = self.run_download([{'size':6}, {'size':6,'data':b'abc'}, ValueError('expired')])
        self.assertTrue(h.close_connection)
        self.assertEqual(b'abc', h.wfile.getvalue())

    def test_a_size_change_is_not_returned_as_a_successful_file(self):
        h, _, _ = self.run_download([{'size':6}, {'size':7,'data':b'abc'}])
        self.assertTrue(h.close_connection)
        self.assertEqual(b'', h.wfile.getvalue())


@unittest.skipUnless(os.name == 'posix', 'helper executes on Linux; dirfd safety is exercised in Linux CI')
class LinuxReaderTests(unittest.TestCase):
    def test_symlinks_and_special_files_cannot_escape_or_block_the_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'volume'; root.mkdir()
            (Path(tmp) / 'secret').write_text('private')
            (root / 'safe').write_bytes(b'hello')
            (root / 'escape').symlink_to(Path(tmp) / 'secret')
            (root / 'outside').symlink_to(Path(tmp), target_is_directory=True)
            os.mkfifo(root / 'pipe')
            def run(action, path):
                return subprocess.run([sys.executable, '-c', S.READER, str(root), action, path, '0'], capture_output=True, text=True, timeout=5)
            rows = json.loads(run('list', '').stdout)['entries']
            self.assertEqual(['safe'], [r['name'] for r in rows])
            self.assertEqual(b'hello', base64.b64decode(json.loads(run('chunk', 'safe').stdout)['data']))
            for path in ('escape', 'outside/secret', '../secret', 'pipe'):
                self.assertNotEqual(0, run('chunk', path).returncode, path)


if __name__ == '__main__':
    unittest.main()
