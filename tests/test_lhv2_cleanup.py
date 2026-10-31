import copy
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import homestead_lhv2_cleanup as C
import homestead_kube as K
from fakes.kube import Kube
import homestead_capacity_review as REVIEW
import homestead_storage_conflicts as CONFLICTS


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.setting={'metadata':{'uid':'setting-uid','resourceVersion':'3'},'value':'true'}
        self.node={'metadata':{'name':'host-1','uid':'node-uid','resourceVersion':'7'},'spec':{},
                   'status':{'nodeInfo':{'bootID':'boot-1'},'conditions':[{'type':'Ready','status':'True'}], 'capacity':{'hugepages-2Mi':'2Gi'}}}
        self.rows={C.CAP.LH+'/volumes':[],C.CAP.LH+'/nodes':[],C.CAP.LH+'/instancemanagers':[], '/api/v1/nodes':[self.node],'/api/v1/pods':[]}
        self.memory={'boot':'boot-1','pages':1024,'free':1024,'reserved':0,'owned_pages':512,'baseline':512,'phase':'prepared','proof':'proof','reason':''}
        self.saved=[]; self.writes=[]; self.host_writes=[]
        def start(kind,title,resource,href,ref,message):
            op={'id':'cleanup-1','kind':kind,'status':'running','ref':ref,'message':message,'progress':0}; self.saved.append(op); return op
        ops=SimpleNamespace(stored=lambda:self.saved,transaction=nullcontext,TERMINAL={'failed','succeeded','cancelled'},start=start,checkpoint=lambda item:None,_public=lambda item:{k:v for k,v in item.items() if k!='ref'})
        def send(method,path,body,**kwargs):
            self.writes.append((path,body));
            if path.endswith('v2-data-engine'): self.setting['value']='false'
            if path.endswith('host-1'): self.node['spec']['unschedulable']=True
        def host(row,action='inspect',**kwargs):
            if action=='stage':
                self.host_writes.append(kwargs); self.memory['phase']='restart-required'
            return copy.deepcopy(self.memory)
        for obj,name,value in [(C.SETUP,'ops',ops),(C.SETUP,'kget',lambda path:self.setting),(C.SETUP,'ksend',send),
                              (C.CAP,'ksend',send),(C.KUBE,'get_optional',lambda *a,**k:None),
                              (C,'items',lambda path:copy.deepcopy(self.rows[path])),(C,'host',host),(REVIEW,'_key',lambda:b'test-key')]:
            p=patch.object(obj,name,value);p.start();self.addCleanup(p.stop)

    def start(self):
        plan=C.review(); return C.start({k:plan[k] for k in ('request_id','capacity_token')}|{'confirm_capacity':True})

    def test_disable_waits_for_managers_then_cordons_and_stages_only_owned_pages(self):
        self.rows[C.CAP.LH+'/instancemanagers']=[{'metadata':{'name':'manager-1'},'spec':{'dataEngine':'v2'}}]
        self.start(); item=self.saved[0]
        C.progress(item); self.assertEqual('false',self.setting['value']); self.assertFalse(self.host_writes)
        C.progress(item); self.assertFalse(self.host_writes)
        self.rows[C.CAP.LH+'/instancemanagers']=[]
        self.rows['/api/v1/pods']=[{'metadata':{'name':'manager-1','namespace':'longhorn-system'},'spec':{}}]
        C.progress(item); self.assertFalse(self.host_writes)
        self.rows['/api/v1/pods']=[]; C.progress(item)
        self.assertTrue(self.node['spec']['unschedulable']); self.assertEqual(1,len(self.host_writes))
        self.assertEqual('restart-required',item['ref']['phase'])
        C.progress(item); self.assertEqual(1,len(self.host_writes))
        self.memory.update(phase='complete',boot='boot-2',pages=512)
        self.assertEqual('running',C.progress(item)[0])
        self.node['status']['capacity']['hugepages-2Mi']='1Gi'
        self.assertEqual('running',C.progress(item)[0])
        self.node['status']['nodeInfo']['bootID']='boot-2'
        self.assertEqual('succeeded',C.progress(item)[0])
        self.assertTrue(self.node['spec']['unschedulable'])

    def test_changed_ownership_or_confirmation_never_starts(self):
        plan=C.review(); body={k:plan[k] for k in ('request_id','capacity_token')}
        with self.assertRaises(ValueError):C.start(body)
        body['confirm_capacity']=True; self.memory['proof']='changed'
        with self.assertRaises(ValueError):C.start(body)
        self.assertFalse(self.saved); self.assertFalse(self.writes)

    def test_v2_data_disks_or_setup_task_blocks_disable(self):
        for kind in ('volume','disk','task'):
            with self.subTest(kind=kind):
                if kind=='volume':self.rows[C.CAP.LH+'/volumes']=[{'spec':{'dataEngine':'v2'}}]
                if kind=='disk':self.rows[C.CAP.LH+'/nodes']=[{'spec':{'disks':{'data':{'diskType':'block'}}}}]
                if kind=='task':self.saved.append({'kind':'longhorn-v2-prepare','status':'running'})
                with self.assertRaises(ValueError):C.review()
                self.rows[C.CAP.LH+'/volumes']=[];self.rows[C.CAP.LH+'/nodes']=[];self.saved.clear()
        self.assertFalse(self.writes)

    def test_unknown_legacy_reservations_disable_cpu_without_cordon_or_memory_writes(self):
        self.memory.update(phase='retained',owned_pages=0,reason='No ownership record')
        self.start();item=self.saved[0];C.progress(item)
        result=C.progress(item)
        self.assertEqual('succeeded',result[0]);self.assertIn('retained',result[2]);self.assertFalse(self.host_writes)
        self.assertNotIn('unschedulable',self.node['spec'])

    def test_other_hugepage_workloads_or_reenabled_engine_prevent_host_write(self):
        self.start();item=self.saved[0];C.progress(item)
        self.rows['/api/v1/pods']=[{'metadata':{'name':'other','namespace':'apps'},'spec':{'nodeName':'host-1','containers':[{'resources':{'requests':{'hugepages-2Mi':'2Gi'}}}]}}]
        state,_,message=C.progress(item)
        self.assertEqual('failed',state);self.assertIn('Other workloads',message)
        self.assertFalse(self.host_writes);self.assertNotIn('unschedulable',self.node['spec'])
        self.assertEqual('failed',C.progress(item)[0])   # a refused job keeps its first reason
        self.rows['/api/v1/pods']=[];self.setting['value']='true'
        self.saved.clear();self.start();item=self.saved[0];C.progress(item)   # a new job disables V2
        self.setting['value']='true'
        state,_,message=C.progress(item)
        self.assertEqual('failed',state);self.assertIn('re-enabled',message)

    def test_recovery_blocks_new_preparation_and_allows_only_reviewed_reboot(self):
        self.start();item=self.saved[0];C.progress(item);C.progress(item)
        with self.assertRaises(ValueError):C.require_idle()
        with self.assertRaises(ValueError):CONFLICTS.require_clear(self.saved,'host-os',{'node':'host-1'})
        CONFLICTS.require_clear(self.saved,'node-power',{'node':'host-1','action':'reboot'})
        with self.assertRaises(ValueError):CONFLICTS.require_clear(self.saved,'node-power',{'node':'host-1','action':'shutdown'})

    def test_new_node_identity_and_partial_inventory_refuse(self):
        self.start();item=self.saved[0];C.progress(item);self.node['metadata']['uid']='replaced'
        state,_,message=C.progress(item)
        self.assertEqual('failed',state);self.assertIn('Cluster hosts changed',message)
        self.assertFalse(self.host_writes)
        self.saved.clear()
        with patch.object(C,'items',side_effect=ValueError('Incomplete inventory')),self.assertRaises(ValueError):C.review()

    def test_failed_unresolved_disk_maintenance_still_blocks_cleanup(self):
        for kind in ('longhorn-runtime', 'disk-allocation', 'disk-v2-convert'):
            with self.subTest(kind=kind):
                self.saved[:]=[{'kind':kind,'status':'failed','ref':{'phase':'staged'}}]
                with self.assertRaisesRegex(ValueError,'maintenance'):C.review()
        self.assertFalse(self.writes)

    def test_offline_host_never_counts_as_resource_release(self):
        self.memory.update(phase='retained',owned_pages=0)
        self.start();item=self.saved[0];C.progress(item)
        self.node['status']['conditions'][0]['status']='False'
        result=C.progress(item)
        self.assertEqual('running',result[0]);self.assertIn('unavailable',result[2])

    def test_late_workload_after_cordon_prevents_boot_policy_write(self):
        self.start();item=self.saved[0];C.progress(item)
        original=C.SETUP.ksend
        def send(*args,**kwargs):
            result=original(*args,**kwargs)
            self.rows['/api/v1/pods']=[{'metadata':{'name':'late','namespace':'apps'},'spec':{'nodeName':'host-1','containers':[{'resources':{'requests':{'hugepages-2Mi':'2Gi'}}}]}}]
            return result
        with patch.object(C.SETUP,'ksend',side_effect=send):state,_,message=C.progress(item)
        self.assertEqual('failed',state);self.assertIn('claimed hugepages',message)
        self.assertFalse(self.host_writes)


class RefusalEndsTests(unittest.TestCase):
    """A refusal that waiting can't clear ends the job (#TBD-lhv2-cleanup-ends): it fails with the
    reason, lets go of V2 prepare and enable, and uncordons exactly the hosts it cordoned."""
    def setUp(self):
        setting={'apiVersion':'longhorn.io/v1beta2','kind':'Setting','metadata':{'name':'v2-data-engine','namespace':'longhorn-system'},'value':'true'}
        self.kube=self.enterContext(Kube(setting,self.node('node-a'),self.node('node-b'),groups=('longhorn.io',),absent_groups=('harvesterhci.io',)))
        self.memory={n:{'boot':'boot-1','pages':1024,'free':1024,'reserved':0,'owned_pages':512,'baseline':512,'phase':'prepared','proof':'proof','reason':''} for n in ('node-a','node-b')}
        self.saved=[]; self.staged=[]
        def start(kind,title,resource,href,ref,message):
            op={'id':'cleanup-1','kind':kind,'status':'running','ref':ref,'message':message,'progress':0}; self.saved.append(op); return op
        ops=SimpleNamespace(stored=lambda:self.saved,transaction=nullcontext,TERMINAL={'failed','succeeded','cancelled'},start=start,
                            checkpoint=lambda item:None,_public=lambda item:{k:v for k,v in item.items() if k!='ref'})
        self.inspect_error=None; self.stage_error=None
        def host(row,action='inspect',**kwargs):
            if action=='stage':
                self.staged.append(row['node']); self.memory[row['node']]['phase']='restart-required'
                if self.stage_error: raise self.stage_error   # staged on the host, but the answer is lost
            elif self.inspect_error: raise self.inspect_error
            return copy.deepcopy(self.memory[row['node']])
        for obj,name,value in [(C.SETUP,'ops',ops),(C.SETUP,'kget',K.kget),(C.SETUP,'ksend',K.ksend),(C.CAP,'ksend',K.ksend),
                               (C,'host',host),(REVIEW,'_key',lambda:b'test-key')]:
            p=patch.object(obj,name,value);p.start();self.addCleanup(p.stop)

    @staticmethod
    def node(name):
        return {'apiVersion':'v1','kind':'Node','metadata':{'name':name},'spec':{},
                'status':{'nodeInfo':{'bootID':'boot-1'},'conditions':[{'type':'Ready','status':'True'}],'capacity':{'hugepages-2Mi':'2Gi'}}}

    def annotations(self,name):
        return self.kube.object('/api/v1/nodes/'+name)['metadata'].get('annotations') or {}

    def cordoned(self,name):
        return bool(self.kube.object('/api/v1/nodes/'+name)['spec'].get('unschedulable'))

    def start_and_stop_v2(self):
        plan=C.review(); C.start({k:plan[k] for k in ('request_id','capacity_token')}|{'confirm_capacity':True})
        item=self.saved[0]; self.assertEqual('running',C.progress(item)[0])   # V2 disabled
        return item

    def hugepage_pod(self,node):
        return {'apiVersion':'v1','kind':'Pod','metadata':{'name':'late-'+node,'namespace':'apps'},
                'spec':{'nodeName':node,'containers':[{'name':'c','resources':{'requests':{'hugepages-2Mi':'2Gi'}}}]},'status':{'phase':'Running'}}

    def test_refusal_after_cordon_fails_with_reason_and_uncordons_its_host(self):
        item=self.start_and_stop_v2()
        # A workload claims node-a's hugepages between the job's cordon and its boot-policy write.
        self.kube.react('PATCH','/api/v1/nodes/node-a',lambda m,p,b:self.kube.add(self.hugepage_pod('node-a')) and None)
        state,_,message=C.progress(item)
        self.assertEqual('failed',state); self.assertIn('claimed hugepages',message)
        self.assertFalse(self.cordoned('node-a')); self.assertFalse(self.staged)
        self.assertNotIn(C.CORDONED_BY,self.annotations('node-a'))   # our mark goes with our cordon
        self.assertIsNone(C.pending()); C.require_idle()   # V2 prepare and enable are allowed again

    def test_the_cordon_carries_the_jobs_mark(self):
        item=self.start_and_stop_v2(); C.progress(item)
        self.assertTrue(self.cordoned('node-a')); self.assertEqual('cleanup-1',self.annotations('node-a')[C.CORDONED_BY])

    def test_a_host_another_job_now_holds_stays_cordoned_until_that_job_ends(self):
        item=self.start_and_stop_v2()
        power={'id':'power-1','kind':'node-power','status':'running','ref':{'node':'node-a','action':'drain'}}
        def cordon(m,p,b):
            self.kube.add(self.hugepage_pod('node-a')); self.saved.append(power)
        self.kube.react('PATCH','/api/v1/nodes/node-a',cordon)
        state,_,message=C.progress(item)
        self.assertEqual('running',state); self.assertIn('claimed hugepages',message)
        self.assertTrue(self.cordoned('node-a')); self.assertIn('node-a',message); self.assertIn('maintenance',message)
        self.assertIsNotNone(C.pending())
        power['status']='succeeded'
        self.assertEqual('failed',C.progress(item)[0]); self.assertFalse(self.cordoned('node-a')); self.assertIsNone(C.pending())

    def test_a_conflicting_uncordon_write_is_retried_not_given_up(self):
        item=self.start_and_stop_v2()
        self.kube.react('PATCH','/api/v1/nodes/node-a',lambda m,p,b:self.kube.add(self.hugepage_pod('node-a')) and None)
        self.kube.fail('/api/v1/nodes/node-a',409,method='PATCH',skip=1)   # the cordon goes through, the uncordon meets a 409
        state,_,message=C.progress(item)
        self.assertEqual('running',state); self.assertTrue(self.cordoned('node-a'))
        self.assertEqual('cleanup-1',self.annotations('node-a')[C.CORDONED_BY]); self.assertIsNotNone(C.pending())
        self.assertEqual('failed',C.progress(item)[0]); self.assertFalse(self.cordoned('node-a'))
        self.assertNotIn(C.CORDONED_BY,self.annotations('node-a'))

    def test_the_cordon_is_recorded_before_its_write(self):
        item=self.start_and_stop_v2()
        self.kube.fail('/api/v1/nodes/node-a',OSError('reply lost'),method='PATCH',applied=True)
        with self.assertRaises(OSError): C.progress(item)
        self.assertTrue(self.cordoned('node-a')); self.assertTrue(item['ref']['nodes'][0]['cordoned'])

    def test_a_clean_run_keeps_the_cordons_and_removes_the_mark(self):
        item=self.start_and_stop_v2()
        C.progress(item); C.progress(item)   # node-a, then node-b, cordoned and staged
        self.assertEqual(['node-a','node-b'],self.staged)
        self.restarted('node-a'); self.restarted('node-b')
        self.assertEqual('succeeded',C.progress(item)[0])
        for name in ('node-a','node-b'):
            self.assertTrue(self.cordoned(name)); self.assertNotIn(C.CORDONED_BY,self.annotations(name))

    def test_a_staged_host_removed_from_the_cluster_counts_as_released(self):
        item=self.start_and_stop_v2(); C.progress(item)   # node-a staged
        self.kube.react('PATCH','/api/v1/nodes/node-b',lambda m,p,b:self.kube.add(self.hugepage_pod('node-b')) and None)
        self.assertEqual('running',C.progress(item)[0])
        self.kube.discard('/api/v1/nodes/node-a'); self.inspect_error=OSError('host unreachable')
        state,_,message=C.progress(item)
        self.assertEqual('failed',state); self.assertIn('node-a',message); self.assertIn('no longer',message)
        self.assertIsNone(C.pending())

    def test_a_staged_host_back_with_a_different_pool_says_so(self):
        item=self.start_and_stop_v2(); C.progress(item)
        self.kube.react('PATCH','/api/v1/nodes/node-b',lambda m,p,b:self.kube.add(self.hugepage_pod('node-b')) and None)
        C.progress(item)
        self.memory['node-a'].update(phase='complete',boot='boot-2')
        obj=self.kube.object('/api/v1/nodes/node-a'); obj['status']['nodeInfo']['bootID']='boot-2'; self.kube.add(obj)   # pool unchanged
        state,_,message=C.progress(item)
        self.assertEqual('running',state); self.assertIn('different hugepage pool',message); self.assertTrue(self.cordoned('node-a'))

    def test_a_staging_host_may_be_rebooted_through_host_maintenance(self):
        item=self.start_and_stop_v2()
        self.stage_error=OSError('host helper went away')
        with self.assertRaises(OSError): C.progress(item)
        self.assertEqual('staging',item['ref']['nodes'][0]['phase'])
        CONFLICTS.require_clear(self.saved,'node-power',{'node':'node-a','action':'reboot'})
        with self.assertRaises(ValueError): CONFLICTS.require_clear(self.saved,'node-power',{'node':'node-a','action':'shutdown'})

    def test_a_host_someone_else_marked_since_stays_cordoned(self):
        item=self.start_and_stop_v2()
        self.stage_error=OSError('host helper went away')
        with self.assertRaises(OSError): C.progress(item)
        self.stage_error=None; self.memory['node-a']['phase']='prepared'
        obj=self.kube.object('/api/v1/nodes/node-a'); obj['metadata']['annotations'][C.CORDONED_BY]='someone-else'; self.kube.add(obj)
        refusal=self.kube.object(C.CAP.LH+'/settings/v2-data-engine'); refusal['value']='true'; self.kube.add(refusal)
        state,_,message=C.progress(item)
        self.assertEqual('failed',state); self.assertTrue(self.cordoned('node-a')); self.assertIn('node-a',message)

    def test_a_host_cordoned_before_the_job_stays_cordoned(self):
        path='/api/v1/nodes/node-a'; obj=self.kube.object(path); obj['spec']['unschedulable']=True
        self.kube.add(obj)
        item=self.start_and_stop_v2()
        self.kube.add(self.hugepage_pod('node-a'))   # someone else's hugepage workload on the host cordoned beforehand
        state,_,message=C.progress(item)
        self.assertEqual('failed',state); self.assertIn('Other workloads',message)
        self.assertTrue(self.cordoned('node-a')); self.assertFalse(self.cordoned('node-b'))
        self.assertFalse([w for w in self.kube.writes if w[1]==path])

    def restarted(self,name):
        """The host came back from its restart with the reviewed pool released."""
        self.memory[name].update(phase='complete',boot='boot-2',pages=512)
        obj=self.kube.object('/api/v1/nodes/'+name); obj['status']['nodeInfo']['bootID']='boot-2'
        obj['status']['capacity']['hugepages-2Mi']='1Gi'; self.kube.add(obj)

    def test_a_refusal_while_a_host_is_staged_waits_for_its_restart_then_fails_and_lets_go(self):
        item=self.start_and_stop_v2()
        self.assertEqual('running',C.progress(item)[0])   # node-a cordoned and its release staged
        self.assertEqual(['node-a'],self.staged); self.assertTrue(self.cordoned('node-a'))
        self.kube.react('PATCH','/api/v1/nodes/node-b',lambda m,p,b:self.kube.add(self.hugepage_pod('node-b')) and None)
        state,_,message=C.progress(item)
        # node-a's RAM release waits for its restart: it stays cordoned and the job keeps holding V2.
        self.assertEqual('running',state); self.assertIn('claimed hugepages',message); self.assertIn('node-a',message)
        self.assertFalse(self.cordoned('node-b')); self.assertTrue(self.cordoned('node-a'))
        with self.assertRaises(ValueError): C.require_idle()
        self.assertEqual('running',C.progress(item)[0]); self.assertTrue(self.cordoned('node-a'))   # not restarted yet
        self.restarted('node-a')
        state,_,message=C.progress(item)
        self.assertEqual('failed',state); self.assertIn('claimed hugepages',message)
        self.assertFalse(self.cordoned('node-a')); self.assertEqual('complete',item['ref']['nodes'][0]['phase'])
        self.assertIsNone(C.pending()); C.require_idle()

    def test_busy_hosts_and_failed_reads_still_retry_and_undo_nothing(self):
        item=self.start_and_stop_v2()
        self.kube.fail('/api/v1/pods',500)
        with self.assertRaises(OSError): C.progress(item)
        self.stage_error=C.HOST.Busy('another change is running on node-a')
        with self.assertRaises(C.HOST.Busy): C.progress(item)
        self.assertTrue(self.cordoned('node-a')); self.assertIsNotNone(C.pending())
        self.assertEqual('staging',item['ref']['nodes'][0]['phase'])

    def test_a_lost_stage_answer_keeps_the_host_cordoned_until_an_inspection_shows_otherwise(self):
        item=self.start_and_stop_v2()
        self.stage_error=OSError('host helper went away')
        with self.assertRaises(OSError): C.progress(item)
        self.stage_error=None
        self.assertEqual(['node-a'],self.staged); self.assertEqual('staging',item['ref']['nodes'][0]['phase'])
        refusal=self.kube.object(C.CAP.LH+'/settings/v2-data-engine'); refusal['value']='true'; self.kube.add(refusal)
        self.inspect_error=OSError('host unreachable')
        state,_,message=C.progress(item)
        self.assertEqual('running',state); self.assertIn('re-enabled',message); self.assertIn('node-a: host unreachable',message)
        self.assertTrue(self.cordoned('node-a')); self.assertIsNotNone(C.pending())
        self.inspect_error=None
        self.assertEqual('running',C.progress(item)[0])   # the receipt says restart-required
        self.assertTrue(self.cordoned('node-a')); self.assertEqual('restart-required',item['ref']['nodes'][0]['phase'])
        self.restarted('node-a')
        self.assertEqual('failed',C.progress(item)[0]); self.assertFalse(self.cordoned('node-a')); self.assertIsNone(C.pending())

    def test_a_staging_host_the_inspection_shows_unstaged_is_uncordoned(self):
        item=self.start_and_stop_v2()
        self.stage_error=OSError('host helper went away')
        with self.assertRaises(OSError): C.progress(item)
        self.stage_error=None; self.memory['node-a']['phase']='prepared'   # the stage never reached the host
        refusal=self.kube.object(C.CAP.LH+'/settings/v2-data-engine'); refusal['value']='true'; self.kube.add(refusal)
        state,_,message=C.progress(item)
        self.assertEqual('failed',state); self.assertIn('re-enabled',message)
        self.assertFalse(self.cordoned('node-a')); self.assertIsNone(C.pending())

    def test_a_failed_job_is_not_resolved_again(self):
        item=self.start_and_stop_v2()
        self.kube.add(self.hugepage_pod('node-a'))
        state,percent,message=C.progress(item); item.update(status=state,progress=percent,message=message)
        writes=len(self.kube.writes)
        self.assertEqual('failed',C.recheck('cleanup-1')['status']); self.assertEqual(writes,len(self.kube.writes))


if __name__ == '__main__':
    unittest.main()
