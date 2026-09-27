import copy
import tempfile
import unittest
import urllib.error
from unittest import mock

from test_reclass import Cluster, OPS, CLASSES
import homestead_reclass as rc
import homestead_operations as ops
import homestead_capacity_review as review


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.cluster=Cluster();self.ops=OPS()
        for mapping in (self.cluster.pvcs,self.cluster.pvs,self.cluster.deps):
            for name,obj in mapping.items():obj['metadata'].update(uid=name+'-uid',resourceVersion='1')
        self.cluster.pvs['pv-old']['spec']['claimRef']['uid']='frigate-config-uid'
        self.body={'namespace':'lab','claim':'frigate-config','target':'longhorn-r3'}
        self.extra={}
        self.original=self.cluster.get
        def get(path):
            if path in self.extra:
                value=self.extra[path]
                if isinstance(value,Exception):raise value
                return copy.deepcopy(value)
            if '/storageclasses/' in path:
                return {'metadata':{'name':path.rsplit('/',1)[1],'uid':'class-uid','resourceVersion':'1'},'provisioner':'driver.longhorn.io'}
            result=copy.deepcopy(self.original(path))
            if path.endswith('/pods'):
                for pod in result['items']:pod['metadata'].update(uid=pod['metadata']['name']+'-uid',resourceVersion='1')
            return result
        for patch in (mock.patch.object(rc,'kget',side_effect=get),mock.patch.object(review,'_key',return_value=b'fixture-review-key')):
            patch.start();self.addCleanup(patch.stop)

    def approved(self):
        plan=rc.preview(self.body,'admin',self.ops)
        self.assertNotIn('_fences',plan)
        self.assertEqual([],self.cluster.sent)
        return {**self.body,'capacity_token':plan['capacity_token'],'confirm_capacity':True}

    def test_confirmation_binds_actor_source_volume_workload_and_target(self):
        body=self.approved()
        for change in ({'confirm_capacity':False},{'claim':'other'},{'target':'other'}):
            with self.assertRaises((ValueError,urllib.error.HTTPError)):rc.start_reviewed({**body,**change},'admin',self.ops)
        with self.assertRaises(ValueError):rc.start_reviewed(body,'other-admin',self.ops)
        self.cluster.deps['frigate']['metadata']['resourceVersion']='2'
        with self.assertRaises(ValueError):rc.start_reviewed(body,'admin',self.ops)
        self.assertEqual([],self.ops.started);self.assertEqual([],self.cluster.sent)

    def test_missing_and_expired_reviews_never_start(self):
        with self.assertRaises(ValueError):rc.start_reviewed(self.body,'admin',self.ops)
        body=self.approved()
        with mock.patch.object(review.time,'time',return_value=int(body['capacity_token'].split('.')[0])+1):
            with self.assertRaises(ValueError):rc.start_reviewed(body,'admin',self.ops)
        self.assertEqual([],self.ops.started)

    def test_rebound_volume_or_changed_storage_class_invalidates_review(self):
        body=self.approved()
        self.cluster.pvs['pv-old']['metadata']['uid']='replacement'
        with self.assertRaises(ValueError):rc.start_reviewed(body,'admin',self.ops)
        self.assertEqual([],self.cluster.sent)

    def test_queued_start_rechecks_before_stopping_anything(self):
        item=rc.start_reviewed(self.approved(),'admin',self.ops)
        self.assertIn('review_fences',item['ref'])
        self.cluster.deps['frigate']['metadata']['uid']='replacement'
        with self.assertRaises(ValueError):rc.resolve(item)
        self.assertEqual([],self.cluster.sent)
        self.assertIn('fresh review',rc.resumable(item))

    def test_inventory_failure_pagination_and_malformed_lists_fail_closed(self):
        for value in (urllib.error.HTTPError('',403,'forbidden',{},None),
                      urllib.error.HTTPError('',503,'unavailable',{},None),{},
                      {'items':[],'metadata':{'continue':'next-page'}},{'items':[None]}):
            self.extra['/api/v1/namespaces/lab/pods']=value
            with self.assertRaises((ValueError,urllib.error.HTTPError)):rc.preview(self.body,'admin',self.ops)
        self.assertEqual([],self.cluster.sent)

    def test_queued_inventory_404_never_enters_rollback(self):
        item=rc.start_reviewed(self.approved(),'admin',self.ops)
        self.extra['/api/v1/namespaces/lab/pods']=urllib.error.HTTPError('',404,'missing',{},None)
        with mock.patch.object(rc,'_rollback') as rollback:
            for _ in range(rc.MISS_LIMIT+1):
                with self.assertRaisesRegex(ValueError,'nothing was stopped'):rc.resolve(item)
            rollback.assert_not_called()
        self.assertEqual([],self.cluster.sent)

    def test_partial_stop_is_held_and_never_blindly_replayed(self):
        item=rc.start_reviewed(self.approved(),'admin',self.ops)
        with mock.patch.object(rc,'_stop',side_effect=RuntimeError('uncertain write')) as stop:
            with self.assertRaisesRegex(ValueError,'Nothing was restarted'):rc.resolve(item)
            self.assertTrue(item['ref']['retain_resources'])
            self.assertEqual('',rc.resumable(item))
            with self.assertRaisesRegex(ValueError,'Nothing was retried'):rc.resolve(item)
            self.assertEqual(1,stop.call_count)

    def test_optional_api_is_absent_only_when_discovery_confirms_it(self):
        path='/apis/kubevirt.io/v1/namespaces/lab/virtualmachines'
        self.extra[path]=urllib.error.HTTPError(path,404,'missing',{},None)
        self.extra['/apis']={'groups':[]}
        self.assertTrue(rc.preview(self.body,'admin',self.ops)['ok'])
        self.extra['/apis']={'groups':[{'name':'kubevirt.io'}]}
        with self.assertRaises(urllib.error.HTTPError):rc.preview(self.body,'admin',self.ops)
        self.extra['/apis']={}
        with self.assertRaises(ValueError):rc.preview(self.body,'admin',self.ops)

    def test_missing_id_and_mismatched_pv_claimref_block_start(self):
        self.cluster.pvs['pv-old']['spec']['claimRef']['uid']='other-claim'
        with self.assertRaises(ValueError):rc.preview(self.body,'admin',self.ops)
        self.cluster.pvs['pv-old']['spec']['claimRef']['uid']='frigate-config-uid'
        self.cluster.pvcs['frigate-config']['metadata'].pop('uid')
        with self.assertRaises(ValueError):rc.preview(self.body,'admin',self.ops)

    def test_new_pod_between_preview_and_start_requires_review(self):
        body=self.approved()
        self.extra['/api/v1/namespaces/lab/pods']={'items':[{'metadata':{'name':'new','uid':'new-uid','resourceVersion':'2'},
            'spec':{'volumes':[{'persistentVolumeClaim':{'claimName':'frigate-config'}}]},'status':{'phase':'Pending'}}]}
        with self.assertRaises(ValueError):rc.start_reviewed(body,'admin',self.ops)
        self.assertEqual([],self.ops.started)

    def test_capacity_failure_is_unknown_not_free(self):
        with mock.patch.object(rc,'capacity',side_effect=RuntimeError('offline')):
            plan=rc.preview(self.body,'admin',self.ops)
        self.assertIsNone(plan['space']['room_gb'])
        self.assertTrue(any('free space is unknown' in warning for warning in plan['warnings']))

    def test_source_activity_counters_do_not_expire_otherwise_valid_approval(self):
        body=self.approved()
        with mock.patch.object(rc,'_longhorn_used',return_value=7*1024**3):
            rc.start_reviewed(body,'admin',self.ops)
        self.assertEqual(1,len(self.ops.started))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        patch=mock.patch.object(ops,'DATA_DIR',directory.name);patch.start();self.addCleanup(patch.stop)

    def test_snapshot_never_advances_jobs(self):
        item=ops.start('fixture','Test',{},'/',{})
        with mock.patch.dict(ops.RESOLVERS,{'fixture':mock.Mock(side_effect=AssertionError('mutation'))}):
            self.assertEqual(item['id'],ops.snapshot()[0]['id'])
            ops.RESOLVERS['fixture'].assert_not_called()

    def test_queued_move_is_exclusive_under_history_lock(self):
        resource={'name':'data','namespace':'lab'}
        ops.start('reclass','First',resource,'/',{'phase':'stop'})
        with self.assertRaisesRegex(ValueError,'already has'):
            ops.start('reclass','Second',resource,'/',{'phase':'stop'})
        self.assertEqual(1,len(ops.snapshot()))

    def test_reclass_conflicts_with_existing_import_recovery_hold(self):
        held={'id':'held','kind':'import-create','status':'failed','ref':{'namespace':'lab','name':'app',
             'claims':{'data':{}},'retain_resources':True}}
        with mock.patch.object(ops,'_read',return_value=[held]),mock.patch.object(ops,'_write') as write:
            with self.assertRaisesRegex(ValueError,'recovery hold'):
                ops.start('reclass','Move',{'name':'data','namespace':'lab'},'/',{'namespace':'lab','claim':'data'})
            write.assert_not_called()

    def test_failed_reclass_can_only_be_replaced_when_not_resumable(self):
        item={'id':'first','kind':'reclass','status':'failed','resource':{'name':'data','namespace':'lab'},'ref':{'phase':'copy'}}
        with mock.patch.object(ops,'_read',return_value=[item]),mock.patch.dict(ops.RESUMABLE,{'reclass':lambda item:''}),mock.patch.object(ops,'_write') as write:
            with self.assertRaisesRegex(ValueError,'already has'):
                ops.start('reclass','Move',item['resource'],'/',{'phase':'stop'})
            write.assert_not_called()
