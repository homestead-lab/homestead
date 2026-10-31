// Every dialog, opened against the demo data at a desktop and a phone width,
// captured whole and measured against the design rules in docs/design.md.
//
//   node scripts/audit_dialogs.mjs            all dialogs
//   node scripts/audit_dialogs.mjs vm         only those whose name has "vm"
//
// Writes release-assets/dialogs/<name>-<width>.png, and report.json with each
// dialog's measurements. It exits non-zero when a dialog breaks a rule that
// can be measured: content wider than the dialog, text too small to read, a
// dialog that could not be opened. CI runs it, so a dialog cannot regress.
import { chromium } from "playwright";
import { mkdir, writeFile } from "node:fs/promises";
import { captureDialog, openAuditPage } from "./audit_ready.mjs";

const output = process.env.DIALOG_OUTPUT || "release-assets/dialogs";
await mkdir(output, { recursive: true });
const base = (process.env.HOMESTEAD_URL || "http://127.0.0.1:4173") + "/?demo=1&demo-scenario=incidents";
const only = process.argv[2] || "";
// design.md, Verbosity budget. Visible words count names and rows too, so the
// budgets sit above today's longest dialogs: they stop growth, not content.
const DIALOG_WORDS = 300, REVIEW_WORDS = 200;
const REVIEW = /review|start|update|reboot|shutdown|power|delete|remove|confirm/;
const theme = process.env.HOMESTEAD_AUDIT_THEME === "light" ? "light" : "dark";

// name, the page to open first, then the steps: JavaScript to run, or
// "click:<fn>" (the first control whose onclick calls fn), or "text:<label>"
// (the first button in the dialog with that label).
const DIALOGS = [
  ["host-join-storage", "nodes", "platformJoinGuide()", "document.querySelector('#mbody details').open=true"],
  ["allocation-edit", "nodes", "diskAllocationOpen('node-1')"],
  ["allocation-review", "nodes", "diskAllocationOpen('node-1')", "diskAllocationReview()"],
  ["allocation-identity", "nodes", "diskAllocationOpen('host-with-a-long-descriptive-storage-name-01')", "diskAllocationReview()", "[...document.querySelectorAll('#mbody details')].find(d=>d.textContent.includes('Device identity')).open=true"],
  ["allocation-no-free", "nodes", "window.__demoAllocationNoFree=true;diskAllocationOpen('node-1')"],
  ["allocation-failed", "nodes", "window.__demoAllocationState='failed';diskAllocationWatch('demo-allocation')"],
  ["allocation-recovery", "nodes", "window.__demoAllocationState='failed';diskAllocationWatch('demo-allocation')", "diskAllocationRecheck('demo-allocation')"],
  ["volume-copy-move", "storage", "volumeCopyMove({name:'pvc-demo',pvc_name:'example-data'})"],
  ["volume-copy-move-review", "storage", "volumeCopyMove({name:'pvc-demo',pvc_name:'example-data'},{disk_tags:['nvme']})"],
  ["volume-copy-move-blocked", "storage", "volumeCopyMove({name:'pvc-demo',pvc_name:'example-data'},{disk_tags:['missing']})"],
  ["one-host-copies-review", "dash", "window.__demoOneHostCopies='lower';oneHostCopiesReview()"],
  ["one-host-copies-raise-review", "dash", "window.__demoOneHostCopies='raise';oneHostCopiesReview()"],
  ["one-host-copies-none", "dash", "window.__demoOneHostCopies='';oneHostCopiesReview()"],
  ["container-diagnostics", "workloads", "wlDiagnostics('lab','example-server')"],
  ["container-command-review", "workloads", "wlDiagnostics('lab','example-server')", "document.querySelector('#diagnosticCommand').value='uname -a';diagnosticReview({disabled:false})"],
  ["firewall-create", "network", "firewallEdit()"],
  ["firewall-review", "network", "firewallEdit()", "firewallReview()"],
  ["active-alerts", "dash", "pwaAlertsDialog()"],
  ["notifications", "dash", "notificationsDialog()"],
  // The jobs entries own their jobs (#934): the demo answers every jobs read from window.__demoOps, so the
  // poll cannot swap its own in before the screenshot. Each entry starts with it reset (see audit()).
  ["jobs-running", "workloads", "STATE.data.operations=window.__demoOps=[{id:'run',kind:'image-update',title:'Update Immich',status:'running',message:'Waiting for the new pod',progress:45}];jobsDialog()"],
  ["jobs-recovery", "workloads", "STATE.data.operations=window.__demoOps=[{id:'held',kind:'self-data-handoff',title:'Move Homestead data',status:'failed',message:'Copy stopped. Both volumes are retained.',dismissible:false,storage_recovery:true}];jobsDialog()"],
  ["jobs-empty", "workloads", "STATE.data.operations=window.__demoOps=[];jobsDialog()"],
  ["jobs-disconnected", "workloads", "STATE.data.operations=window.__demoOps=[{id:'run',title:'Update Immich',status:'running',message:'Waiting for the new pod',progress:null}];STATE.operationsStale=true;jobsDialog()"],
  ["container-update-progress", "workloads", "modal('Updating containers',batchUpdateMarkup([{ns:'lab',name:'immich'},{ns:'lab',name:'plex'}],{[rolloutKey({ns:'lab',name:'immich'})]:{phase:'updating',ready:0,desired:1},[rolloutKey({ns:'lab',name:'plex'})]:{phase:'ready',ready:1,desired:1}},[],false,true))"],

  ["longhorn-v2-upgrade-ready", "settings", "window.__demoV2UpgradeState='ready';lhV2Upgrade('v1.13.0')"],
  ["longhorn-v2-upgrade-blocked", "settings", "window.__demoV2UpgradeState='blocked';lhV2Upgrade('v1.13.0')"],
  ["longhorn-v2-upgrade-running", "settings", "window.__demoV2UpgradeState='running';lhV2Upgrade()"],
  ["longhorn-v2-upgrade-failed", "settings", "window.__demoV2UpgradeState='failed';lhV2Upgrade()"],
  ["longhorn-v2-upgrade-settings", "settings", "window.__demoV2UpgradeState='running';lhV2Upgrade()", "lhV2UpgradeSettings(false)"],
  ["component-upgrade-check-blocked", "settings", "window.__demoUpgradeCheck=true;clusterComponentsPaint().then(()=>componentUpgrade('cluster'))"],
  ["component-enable-upgrades", "settings", "clusterComponentsPaint().then(()=>{const c=STATE.data.components.components.find(c=>c.id==='kubevirt');c.can_manage=true;componentManageOperator('kubevirt')})"],
  ["component-upgrade-check-longhorn", "settings", "window.__demoUpgradeCheck=true;window.__demoV2UpgradeState='ready';clusterComponentsPaint().then(()=>componentUpgrade('longhorn'))"],
  ["runtime-move-review", "nodes", "runtimeMoveOpen('host-1','disk-data')"],
  ["runtime-move-retirement", "nodes", "runtimeRetireReview('demo-runtime')"],
  ["runtime-move-retained", "nodes", "window.__demoRuntimePhase='relocated';runtimeMoveWatch('demo-runtime')"],
  ["disk-v2-region-identity", "nodes", "window.__demoDiskV2Region=true;window.__demoDiskV2State='awaiting-erase';diskV2Watch('demo-disk-v2')", "diskV2EraseReview('demo-disk-v2')", "[...document.querySelectorAll('#mbody details')].find(d=>d.textContent.includes('Device identity')).open=true"],
  ["disk-v2-region-review", "nodes", "window.__demoDiskV2Region=true;window.__demoDiskV2State='ready';diskV2Open('node-1','disk-data')"],
  ["disk-v2-region-erase", "nodes", "window.__demoDiskV2Region=true;window.__demoDiskV2State='awaiting-erase';diskV2Watch('demo-disk-v2')", "diskV2EraseReview('demo-disk-v2')"],
  ["disk-v2-review", "nodes", "window.__demoDiskV2State='ready';diskV2Open('node-1','disk-data')"],
  ["disk-v2-blocked", "nodes", "window.__demoDiskV2State='blocked';diskV2Open('node-1','disk-data')"],
  ["disk-v2-evacuating", "nodes", "window.__demoDiskV2State='evacuating';diskV2Watch('demo-disk-v2')"],
  ["disk-v2-awaiting", "nodes", "window.__demoDiskV2State='awaiting-erase';diskV2Watch('demo-disk-v2')"],
  ["disk-v2-erase", "nodes", "window.__demoDiskV2State='awaiting-erase';diskV2Watch('demo-disk-v2')", "diskV2EraseReview('demo-disk-v2')"],
  ["disk-v2-preparing", "nodes", "window.__demoDiskV2State='preparing';diskV2Watch('demo-disk-v2')"],
  ["disk-v2-failed", "nodes", "window.__demoDiskV2State='failed';diskV2Watch('demo-disk-v2')"],
  ["disk-v2-complete", "nodes", "window.__demoDiskV2State='complete';diskV2Watch('demo-disk-v2')"],
  ["longhorn-v2-missing", "settings", "window.__demoV2State='missing';lhV2Setup()"],
  ["longhorn-v2-cleanup-review", "settings", "lhV2CleanupOpen()"],
  ["longhorn-v2-cleanup-restart", "settings", "lhV2CleanupWatch('demo-v2-cleanup')"],
  ["longhorn-v2-prepare", "settings", "window.__demoV2State='missing';lhV2Setup()", "lhV2ReviewHost('k3s-test')"],
  ["longhorn-v2-running", "settings", "window.__demoV2State='running';lhV2Setup()"],
  ["longhorn-v2-reboot", "settings", "window.__demoV2State='reboot';lhV2Setup()"],
  ["longhorn-v2-failed", "settings", "window.__demoV2State='failed';lhV2Setup()"],
  ["longhorn-v2-enable", "settings", "window.__demoV2State='ready';lhV2Setup()", "lhV2ReviewEnable()"],
  ["longhorn-v2-waiting", "settings", "window.__demoV2State='enabled';lhV2Setup()"],
  ["longhorn-v2-complete", "settings", "window.__demoV2State='complete';lhV2Setup()"],
  ["longhorn-v2-harvester", "settings", "window.__demoV2State='harvester';lhV2Setup()", "lhV2ReviewEnable()"],
  ["cluster-shutdown-review", "cluster", "window.__demoShutdown=null;window.__demoShutdownBlocked=false;clusterShutdown()"],
  ["cluster-shutdown-blocked", "cluster", "window.__demoShutdown=null;window.__demoShutdownBlocked=true;clusterShutdown()"],
  ["cluster-shutdown-progress", "cluster", "api('/api/cluster/shutdown',{method:'POST'}).then(r=>clusterShutdownProgress(r.state))"],
  ["cluster-shutdown-disconnected", "cluster", "api('/api/cluster/shutdown',{method:'POST'}).then(r=>{clusterShutdownProgress(r.state);clearTimeout(shutdownTimer);shutdownPaint(r.state,true)})"],
  ["bug-record", "settings", "bugStart()"],
  ["bug-comment", "settings", "bugDescribe('0123456789abcdef0123456789abcdef')"],
  ["bug-review", "settings", "bugReview('0123456789abcdef0123456789abcdef')"],
  ["bug-review-full", "settings", "bugReview('0123456789abcdef0123456789abcdef','full')"],
  ["bug-issue", "settings", "bugReview('0123456789abcdef0123456789abcdef')", "bugIssue()"],
  ["bug-package", "settings", "bugPackage()"],
  ["bug-package-full", "settings", "bugPackage()", "document.querySelector('#bugPackageFormat').value='full';bugPackageWarning('full')"],
  ["bug-delete", "settings", "bugDelete('0123456789abcdef0123456789abcdef')"],
  ["self-data-prepare", "settings", "window.__demoDataPrepared=false;replicasMoveData()", "selfDataPrepareReview()"],
  ["self-data-blocked-k3s", "settings", "window.__demoOps=(window.__demoOps||[]).filter(x=>x.id!=='data-batch-recovery');window.__demoDataBatchRecovery=true;replicasMoveData()"],
  ["self-data-batch-recovery", "settings", "window.__demoOps=(window.__demoOps||[]).filter(x=>x.id!=='data-batch-recovery');window.__demoDataBatchRecovery=true;replicasMoveData()", "selfDataOpenJob('data-batch-recovery')"],
  ["self-data-archive-review", "settings", "window.__demoDataPrepared=true;replicasMoveData()", "selfDataArchiveReview('demo-data-prepare')"],
  ["self-data-final-review", "settings", "window.__demoDataPrepared=true;replicasMoveData('demo-data-prepare')", "selfDataFinalReview()"],
  ["change-password", "dash", "pwChange()"],
  ["storage-recovery-ready", "storage", "window.__demoStorageState='ready';storageRecoveryReview('op4')"],
  ["storage-recovery-running", "storage", "window.__demoStorageState='running';storageRecoveryReview('op4')"],
  ["storage-recovery-uncertain", "storage", "window.__demoStorageState='uncertain';storageRecoveryReview('op4')"],
  ["compose-import", "workloads", "composeImport()"],
  ["move-workload", "workloads", "moveWorkload('frigate','lab')"],
  ["add-harvester-host", "cluster", "clusterOnboarding()"],
  ["add-harvester-host-2", "cluster", "clusterOnboarding()", "stepGo('host_join',1)"],
  ["add-harvester-host-3", "cluster", "clusterOnboarding()", "stepGo('host_join',2)"],
  ["add-harvester-host-4", "cluster", "clusterOnboarding()", "stepGo('host_join',3)"],
  ["remove-host", "cluster", "clusterRemovePick()"],
  ["add-host", "cluster", "platformJoinGuide()"],
  ["add-host-control-plane", "cluster", "platformJoinGuide('control-plane')"],
  ["add-host-tiebreaker", "cluster", "platformJoinGuide('tiebreaker')"],
  ["helm-release", "helm", "click:helmRelease"],
  ["helm-install", "helm", "helmInstall()"],
  // The addresses load only on the network page's IP tab. Each runner takes
  // a share of this list, so this may be the first address dialog a tab
  // opens: it loads them itself rather than rely on what ran before it.
  ["ip-edit", "network", "api('/api/ipam').then(d => { STATE.data.ipam = d; })", "ipamEdit()"],
  ["ip-subnets", "network", "ipamSubnets()"],
  ["ip-unifi", "network", "ipamUnifi()"],
  ["ip-import", "network", "ipamImport()"],
  ["gitops-reconcile-review", "workloads", "gitopsLoad().then(()=>gitopsReconcile('lab','paperless'))"],
  ["gitops-reconcile-result", "workloads", "gitopsLoad().then(()=>gitopsReconcile('lab','paperless'))", "text:Reconcile now"],
  ["workload-edit", "workloads", "wlEdit('lab','frigate')"],
  ["workload-reservations", "workloads", "reservationsReview()"],
  ["workload-priority", "workloads", "priorityReview()"],
  ["workload-group-tier", "workloads", "groupTier(groupKeyIndex('Home'))"],
  ["workload-edit-placement", "workloads", "wlEdit('lab','home-assistant')", "stepGo('e_steps',4)"],
  // Starts after (#746): a second app picked, one with two ports; a Flux app's lines; a loop refused.
  // Named "waits-for": "start" would hold the Edit form to a review's word budget.
  ["workload-edit-waits-for", "workloads", "wlEdit('lab','paperless')", "(()=>{stepGo('e_steps',4);startsAfterAdd();const s=[...document.querySelectorAll('#e_starts_rows .sa-app')].pop();s.value='lab/frigate';startsAfterAppPicked(s)})()"],
  ["workload-edit-waits-for-managed", "workloads", "wlEdit('lab','doublecommander')", "(async()=>{stepGo('e_steps',4);await startsAfterLines()})()"],
  ["workload-edit-waits-for-cycle", "workloads", "wlEdit('lab','postgres')", "(async()=>{startsAfterAdd();const s=document.querySelector('#e_starts_rows .sa-app');s.value='lab/home-assistant';startsAfterAppPicked(s);await editSave('lab','postgres')})()"],
  ["workload-edit-add-container", "workloads", "wlEdit('lab','frigate')", "containerAdd('edit')"],
  ["workload-edit-hardware", "workloads", "wlEdit('lab','frigate')", "stepGo('e_steps',1)"],
  ["workload-edit-environment", "workloads", "wlEdit('lab','home-assistant')", "stepGo('e_steps',2)"],
  ["workload-edit-storage", "workloads", "wlEdit('lab','frigate')", "stepGo('e_steps',3)"],
  ["workload-edit-review", "workloads", "wlEdit('lab','frigate')", "text:Review changes"],
  ["workload-storage-copy", "workloads", "wlEdit('lab','frigate')", "editReview({ns:'lab',name:'frigate',containers:[{name:'frigate',volumes:[{path:'/media/frigate',source:'camera-data',sub_path:'recordings',copy_from:{claim:'frigate-recordings'}}]}]})"],
  ["vm-migrate", "vms", "vmMove('default','home-assistant-os')"],
  ["vm-new", "vms", "vmNew()"],
  ["vm-new-passthrough", "vms", "vmNew()", "stepGo('v_steps',5);vmAddHostDevice()"],
  ["vm-image-delete", "images", "click:vmImageDelete"],
  ["schedule-new", "schedules", "jobEdit()"],
  ["vm-disk-import", "vmimport", "vmDiskImport()"],
  ["vm-import-unraid", "vmimport", "setTimeout(() => uvmImport('unraid','win11-desk'), 1500)"],
  ["vm-import-job-log", "vmimport", "window.__demoOps=[{id:'import-log',kind:'unraid-vm-import',title:'Import desktop',status:'failed',progress:100,message:'Upload refused'}];STATE.data.operations=window.__demoOps;uvmCopiesPaint();document.querySelector('#uvmCopies button').click()"],
  ["import-source-add", "imports", "srcAdd()"],
  ["import-source-verify", "imports", "window.__demoSourceKeyChanged=false;srcVerify('unraid')"],
  ["import-source-key-changed", "imports", "window.__demoSourceKeyChanged=true;srcVerify('unraid')"],
  ["import-configure", "imports", "inspectImport('unraid','media-server')"],
  ["import-copy-review", "imports", "importReview({name:'media-server',image:'example/media:1',source_consistency:'stopped',volumes:[{name:'media-config',create:false,access_mode:'ReadWriteOnce',storage_class:'longhorn'}],mappings:[{remote_path:'/mnt/user/appdata/media-server',mount_path:'/config'}]})", "document.querySelector('#mbody .ui-more').open=true"],
  ["import-recovery", "imports", "window.__demoOps=[...(window.__demoOps||[]).filter(x=>x.id!=='import-recovery'),{id:'import-recovery',kind:'import-create',mutation_recovery:true,resource:{namespace:'lab',name:'photos'}}];powerRecoveryReview('import-recovery','import')"],
  ["import-recovery-active", "imports", "window.__demoOps=[...(window.__demoOps||[]).filter(x=>x.id!=='import-active'),{id:'import-active',kind:'import-create',mutation_recovery:true,demo_dispatching:true,resource:{namespace:'lab',name:'photos'}}];powerRecoveryReview('import-active','import')"],
  ["import-job-remove", "imports", "importRemove('homestead-import-photos','done')"],
  ["cluster-browse", "settings", "settingsTab('fleet')", "clusterBrowse('branch')"],
  ["mqtt-configure", "settings", "settingsTab('monitoring')", "mqttConfigure()"],
  ["update-window", "settings", "settingsTab('updates')", "updateWindowEdit()"],
  ["catalog-edit", "settings", "settingsTab('connections')", "catalogEdit()"],
  ["alert-webhook-configure", "settings", "settingsTab('connections')", "alertHookConfigure()"],
  ["cluster-storage", "settings", "settingsTab('fleet')", "clusterReady('staging')", "clusterStorage('staging')"],
  ["move-review", "settings", "settingsTab('fleet')", "moveReview('branch','container','frigate')"],
  ["vm-cluster-copy", "settings", "settingsTab('fleet')", "moveReview('branch','vm','home-assistant-os','copy','default')"],
  ["vm-cluster-copy-devices", "settings", "settingsTab('fleet')", "moveReview('branch','vm','gpu-desktop','copy','default')", "const select = document.querySelector('[aria-label=\"Destination device for display\"]'); select.value = 'homestead.io/pci-10de-1e87'; select.dispatchEvent(new Event('change', { bubbles: true }));"],
  ["container-cluster-copy", "settings", "settingsTab('fleet')", "moveReview('branch','container','frigate','copy','lab')"],
  ["vm-cluster-copy-picker", "vms", "[...document.querySelectorAll('[onclick]')].find(e => e.getAttribute('onclick').includes('moveToCluster(') && e.getAttribute('onclick').includes(\"'copy'\")).click()"],
  ["container-cluster-copy-picker", "workloads", "[...document.querySelectorAll('[onclick]')].find(e => e.getAttribute('onclick').includes('moveToCluster(') && e.getAttribute('onclick').includes(\"'copy'\")).click()"],
  ["vip-add", "network", "vipAdd()"],
  ["host-bond", "network", "hostBondDialog('harvester-node1')"],
  ["smart-disk", "nodes", "smartDisk('harvester-node1','nvme0n1')"],
  ["probe-install", "nodes", "probeInstallConfirm()"],
  ["vm-placement-checks", "nodes", "allocationProbeSettings()"],
  ["hardware-features", "nodes", "hardwareFeatureSettings()"],
  ["os-updates", "nodes", "osUpdates()"],
  ["settings-host-updates", "settings", "settingsTab('updates')", "click:osUpdates()"],
  ["self-address", "network", "selfAddressMove()"],
  ["vip-change", "network", "vipChange('192.0.2.242')"],
  ["os-space", "nodes", "diskOsSpace('harvester-node1')"],
  ["portal-edit", "portal", "portalEdit()"],
  ["backup-storage-setup", "protect", "objectStoreSetup()"],
  ["protect-job-new", "protect", "lhJob()"],
  ["protect-job-edit", "protect", "click:lhJob({"],
  ["protection-plans", "protect", "lhPlans()"],
  ["protect-group", "protect", "lhGroup()"],
  ["protect-covered", "protect", "click:lhCovered"],
  ["protect-assign", "protect", "click:lhAssign"],
  ["snapshots", "storage", "lhSnaps('pvc-demo-frigate','frigate-config')"],
  ["snapshot-files-v1", "storage", "window.__demoSnapshotV2=false;snapshotFiles('pvc-demo-frigate','daily-snapshot')"],
  ["snapshot-files-v2", "storage", "window.__demoSnapshotV2=true;snapshotFiles('pvc-demo-frigate','daily-snapshot')"],
  ["snapshot-files-progress", "storage", "window.__demoSnapshotV2=false;window.__demoSnapshotWaiting=true;snapshotFiles('pvc-demo-frigate','daily-snapshot')", "snapshotFilesStart()"],
  ["snapshot-files-ready", "storage", "window.__demoSnapshotV2=true;window.__demoSnapshotWaiting=false;snapshotFiles('pvc-demo-frigate','daily-snapshot')", "snapshotFilesStart().then(()=>snapshotFilesPoll())"],
  // A ready snapshot opens in the file manager; restoring a file shows its review.
  ["snapshot-restore-in-manager", "storage", "window.__demoSnapshotV2=true;window.__demoSnapshotWaiting=false;snapshotFiles('pvc-demo-frigate','daily-snapshot')", "snapshotFilesStart().then(()=>snapshotFilesPoll())",
    "new Promise(done=>{const t=setInterval(()=>{if(FM.panes[0]?.listing){clearInterval(t);done();}},100);}).then(()=>{fmClick({},0,'config.yml');return fmRestoreReview();})"],
  ["backups", "storage", "lhBackupList('pvc-demo-frigate','frigate-config')"],
  ["backup-restore", "storage", "lhBackupList('pvc-demo-frigate','frigate-config')", "click:lhRestore"],
  ["restore-into-app", "protect", "lhRestoreInto('backup-demo-frigate-20260919')"],
  ["restore-into-app-gitops", "protect", "window.__demoRestoreGitops=true;lhRestoreInto('backup-demo-frigate-20260919')"],
  ["previous-volume-swap-back", "protect", "loadPreviousVolumes().then(()=>previousSwapBack('lab','frigate-config'))"],
  ["fs-freeze-on", "protect", "loadFsFreeze().then(()=>fsFreezeReview(true))"],
  ["fs-freeze-on-old-kernel", "protect", "window.__demoFsFreezeOldKernel=true;loadFsFreeze().then(()=>fsFreezeReview(true))"],
  ["fs-freeze-off", "protect", "loadFsFreeze().then(()=>fsFreezeReview(false))"],
  ["backup-target", "protect", "lhTarget()"],
  ["resource-open", "resources", "click:resOpen"],
  ["resource-create", "resources", "resCreate()"],
  ["samba-remove", "settings", "sambaRemove()"],
  ["nfs-remove", "settings", "nfsRemove()"],
  ["volume-create", "storage", "volumeCreate()"],
  ["volume-edit", "storage", "click:volumeEdit"],
  ["volume-class-review", "storage", "volumeReclass({name:'frigate-config',pvc_name:'frigate-config',namespace:'lab',storage_class:'longhorn-r2'})"],
  ["volume-delete", "storage", "click:volumeDelete"],
  ["storage-class-new", "storage", "storageClassCreate()"],
  ["volume-files", "storage", "click:fileManager"],
  ["volume-ownership", "storage", "volumeChown('lab','frigate-config')"],
  ["share-new", "shares", "newShare()"],
  ["share-new-user", "shares", "newShare()", "document.querySelector('#sh_identity').value='';shareAccountHint()"],
  ["share-users", "shares", "smbUsers()"],
  ["share-user-add", "shares", "smbUsers()", "smbUserEdit()"],
  ["share-user-password", "shares", "smbUsers()", "smbUserEdit('lab')"],
  ["share-user-remove", "shares", "smbUserRemove('backup')"],
  ["disks", "storage", "lhDisks()"],
  ["disk-add", "storage", "diskAdd('harvester-node1')"],
  ["vm-image-store", "vms", "vmStore()"],
  ["vm-open", "vms", "vmOpen('default','home-assistant-os')"],
  ["vm-edit", "vms", "vmEdit('default','home-assistant-os')"],
  ["vm-edit-hardware", "vms", "vmEdit('default','home-assistant-os')", "text:Hardware"],
  ["vm-edit-passthrough", "vms", "vmEdit('default','home-assistant-os')", "text:Passthrough", "vmAddHostDevice()"],
  ["vm-iso-library", "vms", "vmIsoLibrary()"],
  ["vm-iso-folder", "vms", "vmIsoLibrary()", "text:＋ Folder"],
  ["vm-delete", "vms", "vmDelete('default','home-assistant-os')"],
  ["k3s-cluster", "vms", "k3sCluster()"],
  ["image-updates", "workloads", "imageUpdateCenter()"],
  ["homestead-updates", "workloads", "homesteadUpdateDialog()"],
  ["image-update-review", "workloads", "imageUpdateReview('lab','frigate')"],
  ["image-update-cordoned", "workloads", "window.__demoImageReviewCordoned=true;imageUpdateReview('lab','homestead')"],
  ["image-update-cordoned-details", "workloads", "window.__demoImageReviewCordoned=true;imageUpdateReview('lab','homestead')", "document.querySelector('#mbody > .update-review > details').open=true"],
  ["image-update-batch", "workloads", "imageUpdateCenter()", "text:Review selected"],
  ["workload-group", "workloads", "wlGroup('lab','frigate')"],
  ["workload-groups", "workloads", "manageWorkloadGroups()"],
  ["workload-groups-filtered", "workloads", "manageWorkloadGroups();document.querySelector('#wg_q').value='f';wgFilter();wgTickShown(true)"],
  ["workload-groups-rename", "workloads", "manageWorkloadGroups();wgRename(0)"],
  ["workload-monitoring-port", "workloads", "wlMonitoring('lab','frigate')"],
  ["workload-outage-actions", "workloads", "wlMonitoring('lab','frigate');document.querySelector('#oa_restart').click();document.querySelector('#oa_hook').click()"],
  ["vm-outage-actions", "vms", "vmMonitoring('default','home-assistant-os');document.querySelector('#oa_restart').click();document.querySelector('#oa_hook').click()"],
  ["workload-logo", "workloads", "wlLogo('lab','frigate')"],
  ["workload-logo-fixup", "workloads", "logoFixup()"],
  ["vm-logo", "vms", "vmLogo('lab','ubuntu-test')"],
  ["vm-monitoring", "vms", "vmMonitoring('default','home-assistant-os')"],
  ["workload-monitoring", "workloads", "wlMonitoring('lab','paperless')"],
  ["workload-history", "workloads", "wlHistory('lab','frigate')"],
  ["change-history", "workloads", "wlHistory()"],
  ["log-search", "workloads", "logSearch()"],
  ["roles-compare", "settings", "rolesCompare()"],
  ["user-add", "settings", "userAdd()"],
  ["workload-schedule", "workloads", "wlSchedule('lab','paperless')"],
  ["workload-schedule-new", "workloads", "wlSchedule('lab','frigate')"],
  ["vm-schedule", "vms", "vmSchedule('lab','k3s-demo-server-1')"],
  ["schedules-overview", "workloads", "schedulesOverview()"],
  ["log-search-results", "workloads", "logSearch();document.querySelector('#ls_q').value='error';logSearchRun()"],
  ["log-alerts", "workloads", "logAlerts()"],
  ["log-alert-new", "workloads", "logSearch('lab','frigate');document.querySelector('#ls_q').value='Unable to read';logAlertFromSearch()"],
  ["log-alert-edit", "workloads", "logAlerts()", "logAlertEdit('la-0d3f5a9b2c71')"],
  ["history-undo", "workloads", "historyUndo('lab','paperless','c2')"],
  ["node-if-down", "nodes", "nodeIfDown('harvester-node2')"],
  ["set-restart-review", "nodes", "STATE.platform.harvester=false", "setRestartReview('data','postgres','harvester-node3')"],
  ["set-restart-blocked", "nodes", "STATE.platform.harvester=false;window.__demoSetRestartBlocked=true", "setRestartReview('data','postgres','harvester-node3')"],
  ["workload-update-mode", "workloads", "wlUpdateMode('lab','frigate')"],
  ["workload-start-capacity", "workloads", "wlScale('lab','doublecommander',1)"],
  ["workload-start-waits", "workloads", "wlScale('lab','paperless',1)"],
  ["stop-homestead", "workloads", "wlStopSelf('lab','homestead')"],
  ["failover", "workloads", "wlFailover()"],
  ["workload-delete", "workloads", "wlDelete('lab','frigate')"],
  ["workload-logs", "workloads", "click:wlLogs"],
  ["workload-console", "workloads", "wlConsole('lab','frigate')"],
  ["workload-main-port", "workloads", "wlPrimaryPort('lab','frigate')"],
  ["balance-containers", "nodes", "balanceHosts('containers')"],
  ["balance-volumes", "nodes", "balanceHosts('volumes')"],
  ["balance-review", "nodes", "balanceHosts('review')"],
  ["move-several", "workloads", "moveSeveral()"],
  ["move-several-review", "workloads", "moveSeveral(['lab/frigate','lab/home-assistant'],'harvester-node3')"],
  ["node-single-host-reboot", "nodes", "window.__demoSingleHostOutage=true;nodePowerReview('harvester-node1','reboot')"],
  ["node-power-disconnected", "nodes", "modal('Host maintenance',nodePowerProgressMarkup({id:'demo-power',status:'running',progress:8,message:'Waiting for apps to stop; power has not been sent',power:{phase:'holding',planned_outage:true,direct:true,holds:true,action:'reboot'}},true),true)"],
  ["node-power-handoff", "nodes", "modal('Host maintenance',nodePowerProgressMarkup({id:'demo-power',status:'running',progress:20,message:'Homestead is stopping so its data volume detaches',power:{phase:'observing',planned_outage:true,handoff:true,direct:true,holds:true,action:'reboot'}},true),true)"],
  ["node-power-disconnected-long", "nodes", "modal('Host maintenance',nodePowerProgressMarkup({status:'running',power:{phase:'observing',planned_outage:true,direct:true,action:'poweroff'}},true,16*60*1000),true)"],
  ["node-power-disconnected-details", "nodes", "modal('Host maintenance',nodePowerProgressMarkup({status:'running',progress:8,message:'Waiting for apps to stop; power has not been sent',power:{phase:'holding',planned_outage:true,direct:true,holds:true,action:'reboot'}},true),true)", "document.querySelector('#mbody .ui-more').open=true"],
  ["node-single-host-shutdown", "nodes", "window.__demoSingleHostOutage=true;nodePowerReview('harvester-node1','poweroff')"],
  ["node-reboot", "nodes", "nodePowerReview('harvester-node1','reboot')"],
  ["node-promote", "nodes", "nodePromoteReview('harvester-node1')"],
  ["node-shutdown", "nodes", "nodePowerReview('harvester-node1','poweroff')", "document.querySelector('#mbody .ui-more').open=true"],
  ["app-store-app", "store", "click:storeDetails"],
  ["deploy-preview", "deploy", "UI.selectSection('containerDeploy','summary')", "click:previewYaml"],
  ["fleet-link", "workloads", "fleetLink()"],
  ["fleet-migration-on", "settings", "settingsTab('fleet')", "fleetMigration('b2c0de')"],
  ["fleet-migration-here", "settings", "settingsTab('fleet')", "fleetMigration('a1f00d')"],
  ["fleet-migration-new", "settings", "settingsTab('fleet')", "fleetMigration('c3beef')"],
  ["config-backup", "settings", "configBackup()"],
  ["config-restore", "settings", "configRestore()"],
  ["config-restore-parts", "settings", "configRestore()", "configRestoreParts({homestead:'2.8.209',site:'Main site',created:'2026-09-26T21:40:00Z',parts:[{id:'settings',label:'Settings',detail:'Site name, health thresholds',state:'same',restorable:true,default:true},{id:'users',label:'Users and roles',detail:'Every account, its role and password',caution:'Replaces every account and password with those in the backup, and signs everyone out.',state:'differs',restorable:true,default:false},{id:'ipam',label:'IP addresses',detail:'Subnets and documented addresses',state:'differs',restorable:true,default:true},{id:'vmstore',label:'VM image store',detail:'The cloud images kept',state:'empty',restorable:false,default:true}]})"],
  ["move-to-cluster", "workloads", "moveToCluster('container','frigate')"],
  ["jobs-completed-data-moves", "dash", "STATE.data.operations=window.__demoOps=[{id:'data-done',kind:'self-data-handoff',title:'Move Homestead data',resource:{namespace:'lab',kind:'PersistentVolumeClaim'},status:'succeeded',progress:100,message:'Homestead is ready on the new volume. The original volume is retained.',dismissible:true},{id:'data-recovered',kind:'self-data-handoff',title:'Move Homestead data',resource:{namespace:'lab',kind:'PersistentVolumeClaim'},status:'cancelled',progress:100,message:'Recovered Homestead on its original volume. Both data volumes and the recovery audit are retained.',dismissible:true}];renderOperations();jobsDialog()"],
];

const report = [];
const failures = [];
const browser = await chromium.launch({ headless: true });

// Each width's list is shared between a few tabs, all at once: one tab
// working through every item in turn took most of CI's time.
const WORKERS = Number(process.env.AUDIT_WORKERS || 4);
const WIDTHS = [["desktop", 1440, 900, false], ["mobile", 390, 844, true]];
// CI splits the list across runners: AUDIT_SHARD of AUDIT_SHARDS, each every Nth dialog.
const SHARDS = Math.max(1, Number(process.env.AUDIT_SHARDS || 1));
const SHARD = Number(process.env.AUDIT_SHARD || 0) % SHARDS;
const todo = DIALOGS.filter(([name]) => !only || name.includes(only)).filter((_, j) => j % SHARDS === SHARD);
const shares = Array.from({ length: WORKERS }, (_, i) => todo.filter((_, j) => j % WORKERS === i)).filter((share) => share.length);
await Promise.all(WIDTHS.flatMap((width) => shares.map((share) => audit(width, share))));
await browser.close();
// Back in list order, whichever tab finished first.
const order = (r) => todo.findIndex(([name]) => name === r.name) * 2 + (r.label === "mobile");
report.sort((a, b) => order(a) - order(b));

async function audit([label, width, height, mobile], items) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: mobile ? 2 : 1,
    isMobile: mobile, hasTouch: mobile, colorScheme: "dark" });
  const page = await context.newPage();
  // A dialog may ask the browser to confirm first (a volume in use, say): yes.
  page.on("dialog", (dialog) => dialog.accept());
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(theme => {
    localStorage.setItem("homestead.settings", JSON.stringify({ theme, bg: "soft", blur: 26, motion: "off", refresh: 60 }));
  }, theme);
  await openAuditPage(page, base);
  await page.addStyleTag({ content: "#jobTray{display:none!important}" });

  for (const [name, view, ...steps] of items) {
    await page.evaluate(() => { window.__demoDataBatchRecovery = false; window.__demoSingleHostOutage = false; window.__demoSetRestartBlocked = false; window.__demoUpgradeCheck = false; STATE.operationsStale = false; delete window.__demoOps; });
    try {
      await page.evaluate(() => { try { closeModal(); } catch (e) { /* none open */ } });
      await page.evaluate((v) => go(v), view);
      await page.locator("#views .phead").waitFor({state:"attached"});
      await page.waitForTimeout(700);
      for (const step of steps) {
        if (step.startsWith("click:")) {
          const fn = step.slice(6);
          const found = await page.evaluate((fn) => {
            const el = [...document.querySelectorAll("[onclick]")].find((e) => e.getAttribute("onclick").includes(fn.includes("(") ? fn : `${fn}(`));
            if (!el) return false;
            el.click(); return true;
          }, fn);
          if (!found) throw new Error(`no control calls ${fn}`);
        } else if (step.startsWith("text:")) {
          const text = step.slice(5);
          const found = await page.evaluate((text) => {
            const el = [...document.querySelectorAll("#modal button")].find((b) => b.textContent.trim().startsWith(text));
            if (!el) return false;
            el.click(); return true;
          }, text);
          if (!found) throw new Error(`no "${text}" button`);
        } else {
          await page.evaluate(step);
        }
        await page.waitForTimeout(900);
      }
      await page.locator("#modal:not(.hidden) #mbody").waitFor({ timeout: 4000 });
      await page.waitForTimeout(600);
      const metrics = await page.evaluate(() => {
        const box = document.querySelector(".modalbox"), body = document.querySelector("#mbody");
        const boxRect = box.getBoundingClientRect();
        const wide = [...body.querySelectorAll("*")].filter((e) => {
          const r = e.getBoundingClientRect();
          return r.width > 0 && r.right > boxRect.right + 1 && getComputedStyle(e).position !== "fixed"
            && !e.closest("pre, code, .monaco-editor, .xterm, table, .tablewrap, [data-scroll-x]");
        }).map((e) => `${e.tagName.toLowerCase()}.${[...e.classList].join(".")}`);
        const texts = [...body.querySelectorAll("*")].filter((e) => [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()));
        const tiny = texts.filter((e) => parseFloat(getComputedStyle(e).fontSize) < 10 && e.offsetParent)
          .map((e) => `${e.tagName.toLowerCase()}.${[...e.classList].join(".")} ${getComputedStyle(e).fontSize}`);
        // Text squeezed into a narrow column beside a row's pill or buttons:
        // under 90px and three or more lines (the phone Image updates rows).
        const squeezed = texts.filter((e) => {
          if (!e.offsetParent) return false;
          if (e.closest("pre, code, table, svg, .monaco-editor, .xterm")) return false;
          const r = e.getBoundingClientRect(), range = document.createRange();
          range.selectNodeContents(e);
          const lines = new Set([...range.getClientRects()].map((x) => Math.round(x.top))).size;
          return r.width < 90 && lines >= 3 && e.textContent.trim().length > 12;
        }).map((e) => `${e.tagName.toLowerCase()}.${[...e.classList].slice(0, 2).join(".")} "${e.textContent.trim().slice(0, 30)}"`);
        // A field with no name: a screen reader says "edit text" and nothing more.
        const unnamed = [...body.querySelectorAll("input:not([type=hidden]), select, textarea")].filter((e) => e.checkVisibility()
          && !(e.getAttribute("aria-label") || "").trim() && !(e.getAttribute("title") || "").trim()
          && ![...(e.labels || [])].some((l) => l.textContent.trim())
          && !(e.getAttribute("aria-labelledby") || "").split(/\s+/).some((id) => document.getElementById(id)?.textContent.trim()))
          .map((e) => e.id ? `#${e.id}` : `${e.tagName.toLowerCase()}${e.name ? `[name=${e.name}]` : ""}.${[...e.classList].slice(0, 2).join(".")}`);
        // The footer (design.md): Cancel, Close or Back on the left, the result on the right, and a
        // danger button there only when the dialog is about that action ("Delete VM · x").
        const end = [...body.querySelectorAll(".ui-actions-end > button, .ui-actions-end > .btn, .ui-actions-end .actionbar > button")].filter((b) => b.checkVisibility());
        const dialogTitle = document.querySelector("#mtitle").textContent.trim();
        const last = end.at(-1), verb = (last?.textContent.trim().split(/\s+/)[0] || "").toLowerCase();
        const footer = [
          ...end.filter((b) => /^(back( to .+)?|cancel|close)$/i.test(b.textContent.trim().replace(/^‹\s*/, ""))).map((b) => `"${b.textContent.trim()}" sits on the right`),
          // Cancelling an operation that is running is that dialog's main action.
          ...(last?.classList.contains("danger") && verb && verb !== "cancel" && !dialogTitle.toLowerCase().startsWith(verb) ? [`the main slot is a danger "${last.textContent.trim()}"`] : []),
        ];
        return {
          unnamed: [...new Set(unnamed)].slice(0, 8),
          footer,
          title: document.querySelector("#mtitle").textContent,
          copy: body.innerText,
          explanations: [...body.querySelectorAll("p, .ui-lead, .ui-help, .note, .ui-callout-body")].filter(e => e.offsetParent && !e.querySelector("p, .ui-lead, .ui-help, .note, .ui-callout-body")).map(e=>e.innerText.trim()).filter(Boolean),
          width: Math.round(boxRect.width), height: Math.round(box.scrollHeight),
          notes: body.querySelectorAll(".note").length,
          buttons: body.querySelectorAll("button, .btn").length,
          checkboxes: body.querySelectorAll("input[type=checkbox]").length,
          words: body.innerText.split(/\s+/).filter(Boolean).length,
          // Visible notices: design.md asks for one, combining related warnings.
          attention: [...body.querySelectorAll(".ui-callout, .note.bad, .note.warn")].filter(e => e.checkVisibility()).length,
          // The same sentence said twice in one dialog.
          repeated: (() => {
            const seen = new Map();
            for (const e of body.querySelectorAll("p, li, .ui-lead, .ui-help, .ui-callout-body, .note")) {
              if (!e.checkVisibility() || e.querySelector("p, li, .ui-lead, .ui-help, .ui-callout-body, .note")) continue;
              const text = e.innerText.trim().replace(/\s+/g, " ");
              if (text.length >= 40) seen.set(text, (seen.get(text) || 0) + 1);
            }
            return [...seen].filter(([, n]) => n > 1).map(([text]) => text.slice(0, 80));
          })(),
          overflow: [...new Set(wide)].slice(0, 6),
          tiny: [...new Set(tiny)].slice(0, 6),
          squeezed: [...new Set(squeezed)].slice(0, 6),
        };
      });
      await captureDialog(page, `${output}/${name}-${label}.png`);
      report.push({ name, label, ...metrics });
      if (metrics.overflow.length) failures.push(`${name} (${label}): content wider than the dialog: ${metrics.overflow.join(", ")}`);
      if (metrics.tiny.length) failures.push(`${name} (${label}): text under 10px: ${metrics.tiny.join(", ")}`);
      if (metrics.squeezed.length) failures.push(`${name} (${label}): text squeezed into a narrow column: ${metrics.squeezed.join(", ")}`);
      if (metrics.unnamed.length) failures.push(`${name} (${label}): fields with no name: ${metrics.unnamed.join(", ")}`);
      if (metrics.footer.length) failures.push(`${name} (${label}): footer: ${metrics.footer.join("; ")}`);
      // design.md, Verbosity budget: what a dialog shows before Details is opened.
      const limit = REVIEW.test(name) ? REVIEW_WORDS : DIALOG_WORDS;
      if (metrics.words > limit) failures.push(`${name} (${label}): ${metrics.words} visible words, over ${limit}: move explanations, identifiers and calculations into UI.more`);
      if (metrics.attention > 1) failures.push(`${name} (${label}): ${metrics.attention} visible notices: combine them into one UI.callout`);
      if (metrics.repeated.length) failures.push(`${name} (${label}): says the same thing twice: "${metrics.repeated[0]}"`);
    } catch (error) {
      const toastText = await page.evaluate(() => [...document.querySelectorAll("#toast .tst")].map((t) => t.textContent.trim()).join(" | ")).catch(() => "");
      failures.push(`${name} (${label}): could not open: ${error.message.split("\n")[0]}${toastText ? ` - toast: ${toastText}` : ""}${errors.length ? ` - error: ${errors.at(-1)}` : ""}`);
    }
  }
  await context.close();
}

await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2));
for (const r of report) {
  console.log(`${r.name.padEnd(26)} ${r.label.padEnd(8)} ${String(r.height).padStart(5)}px ${String(r.words).padStart(4)} words ${r.notes} notes ${r.buttons} buttons ${r.checkboxes} checks`);
}
if (failures.length) {
  console.log(`\n${failures.length} problem(s):\n  ${failures.join("\n  ")}`);
  process.exitCode = 1;
}
