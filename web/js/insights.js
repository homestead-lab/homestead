/* Read-only, shared health advice for the dashboard and Cluster Health.
   Missing observations stay unknown; advice never changes cluster configuration. */
const HealthInsights = (() => {
  const titles = {health:"Health suggestions", workloads:"Workload health", backups:"Backup freshness", updates:"Updates awaiting review", jobs:"Running and failed jobs"};
  const sources = {cluster:"/api/cluster", protection:"/api/lh/overview", nodes:"/api/nodes", self:"/api/self/health", settings:"/api/settings", workloads:"/api/workloads", vms:"/api/vms", updates:"/api/image-updates", jobs:"/api/operations", components:"/api/cluster/components", hosts:"/api/os-updates"};
  const needs = {health:["cluster","protection","nodes","self","settings"], workloads:["workloads","vms"], backups:["protection"], updates:["updates","components","hosts"], jobs:["jobs"]};
  const rank = {critical:0,medium:1,low:2};
  const tone = severity => ({critical:"bad",medium:"warn",low:"info",healthy:"ok"}[severity] || "info");
  const item = (id,severity,title,detail,route) => ({id,severity,title,detail,route});
  const known = n => typeof n === "number" && Number.isFinite(n);
  function advice(data) {
    const rows=[], cp=data.cluster?.control_plane;
    if (cp?.etcd_total > 0) {
      if (known(cp.etcd_ready) && cp.etcd_ready < Math.floor(cp.etcd_total/2)+1)
        rows.push(item("quorum","critical","etcd quorum is unavailable","Restore unavailable etcd hosts before making cluster changes.","cluster"));
      else if (cp.etcd_total === 2)
        rows.push(item("quorum","medium","Two etcd members have no failure margin","Add a third etcd server. Both current members are required for quorum.","cluster"));
      else if (cp.quorum_margin === 0)
        rows.push(item("quorum","medium","No etcd failure margin","Review member readiness and plan three healthy etcd servers for resilience.","cluster"));
    }
    if (data.cluster?.unavailable?.length) rows.push(item("cluster-partial","low","Some platform checks are unavailable","Open Cluster to review missing observations.","cluster"));
    for (const n of data.cluster?.nodes || []) {
      if (n.ready === false) rows.push(item("node:"+n.name,"critical",n.name+" is not ready","Check this host before restarting or moving workloads.","nodes"));
      else if (n.pressure?.length) rows.push(item("pressure:"+n.name,"medium",n.name+" reports resource pressure",n.pressure.join(", ")+". Free capacity or move workloads.","nodes"));
    }
    const p=data.protection;
    if (p) {
      const unprotected=(p.volumes || []).filter(v=>v.backed_up === false);
      if (unprotected.length) rows.push(item("backup-coverage","medium",`${unprotected.length} volume${unprotected.length===1?"":"s"} without scheduled backups`,"Assign a recurring external backup. Replicas and local snapshots do not replace a backup.","protect"));
      if (p.total > 0 && p.target?.configured === false) rows.push(item("backup-target","medium","Configure a backup destination","Choose a separate backup destination, then schedule backups.","protect"));
      else if (p.target?.configured && p.target.available === false) rows.push(item("backup-target","critical","Backup destination is unavailable",p.target.reason || "Restore access so backups can resume.","protect"));
      const failed=(p.jobs || []).filter(j=>j.task?.startsWith("backup") && j.last_failed);
      if (failed.length) rows.push(item("backup-failed","critical",`${failed.length} backup job${failed.length===1?"":"s"} failed`,"Check the job logs and confirm a successful backup after recovery.","protect"));
      const never=(p.volumes || []).filter(v=>v.backed_up && !v.last_backup_at);
      if (never.length) rows.push(item("backup-never","medium",`${never.length} scheduled volume${never.length===1?" has":"s have"} no recorded backup`,"Run a backup and check that it completes.","protect"));
    }
    const temp=data.settings?.thresholds?.temperature, diskLimit=data.settings?.thresholds?.disk;
    for (const n of data.nodes || []) {
      if (diskLimit && known(n.fs_pct) && n.fs_pct >= diskLimit.warning) rows.push(item("disk-space:"+n.name,n.fs_pct>=diskLimit.critical?"critical":"medium",`${n.name}: disk ${Math.round(n.fs_pct)}% full`,"Free space or expand capacity before storage fills.","nodes"));
      if (temp && known(n.temps?.max_c) && n.temps.max_c >= temp.warning) rows.push(item("temperature:"+n.name,n.temps.max_c>=temp.critical?"critical":"medium",`${n.name}: maximum ${n.temps.max_c}°C`,"Inspect the sensor and cooling. Drive temperatures use their own thresholds.","nodes"));
      for (const d of n.temps?.disks || []) {
        if (["critical","attention","degraded"].includes(d.health?.state)) rows.push(item("disk:"+n.name+":"+d.name,d.health.state==="critical"?"critical":"medium",`${n.name} / ${d.name}: drive needs attention`,d.health.summary || "Check SMART results and verify backups before replacing the drive.","nodes"));
        else if (!d.health || d.health.stale_probe || ["unknown","unsupported"].includes(d.health.state)) rows.push(item("disk:"+n.name+":"+d.name,"low",`${n.name} / ${d.name}: drive health unknown`,"Check the node probe and SMART support.","nodes"));
      }
    }
    const h=data.self;
    if (h) {
      if (h.api?.ok === false) rows.push(item("self-api","critical","Homestead cannot reach Kubernetes",h.api.error || "Check cluster connectivity.","about"));
      if (h.replicas?.pods && h.replicas.pods.filter(p=>p.ready).length < h.replicas.desired) rows.push(item("self-replicas","medium","Homestead has unavailable replicas","Inspect its pods and restart errors.","about"));
      const loops=(h.loops || []).filter(l=>["failing","stale","error"].includes(l.state));
      if (loops.length) rows.push(item("self-loops","medium",`${loops.length} Homestead background check${loops.length===1?" needs":"s need"} attention`,loops.map(l=>l.label || l.name).join(", ")+". Review Homestead health.","about"));
      if (h.permissions?.state === "error") rows.push(item("self-permissions","critical","Homestead permissions need repair",h.permissions.detail || "Review the installation permissions.","about"));
      if (h.probe && (!h.probe.installed || h.probe.reporting < h.probe.desired)) rows.push(item("probe","low","Host monitoring is incomplete","Check node probes to restore temperature and drive observations.","about"));
      if (h.addresses?.problem || h.addresses?.clashes?.length) rows.push(item("addresses","critical","A service address conflicts with the cluster","Review service addresses before changing hosts.","network"));
    }
    for (const key of needs.health) if (data[key] === null) rows.push(item("unknown:"+key,"low",`${({cluster:"Platform",protection:"Backup",nodes:"Host",self:"Homestead",settings:"Threshold"})[key]} checks unavailable`,"Retry or check access. Health cannot be confirmed from missing data.","retry"));
    return rows.sort((a,b)=>rank[a.severity]-rank[b.severity] || a.id.localeCompare(b.id));
  }
  function backupRows(p, now=Date.now()) {
    return [...(p?.volumes || [])].sort((a,b)=>Number(!!a.last_backup_at)-Number(!!b.last_backup_at) || String(a.last_backup_at || "").localeCompare(String(b.last_backup_at || ""))).map(v=> {
      const stamp=Date.parse(v.last_backup_at), age=Number.isFinite(stamp)&&stamp<=now ? Math.floor((now-stamp)/86400000) : null;
      return item(v.name,!v.last_backup_at?"medium":"low",v.pvc || v.name,
        !v.last_backup_at?"No recorded backup":age===null?"Backup timestamp unavailable":`Last backup ${age===0?"today":age+"d ago"}`,"protect");
    });
  }

  function workloadSummary(workloads=[],vms=[]) {
    const active=workloads.filter(w=>w.desired>0), unhealthy=active.filter(w=>w.ready<w.desired);
    const vmIssues=vms.filter(v=>v.run_strategy!=="Halted" && v.status!=="Stopped" && v.status!=="Running");
    return {ready:active.length-unhealthy.length,active:active.length,stopped:workloads.filter(w=>w.desired===0).length,
      rows:[...unhealthy.map(w=>item(w.ns+"/"+w.name,"medium",w.name,`${w.ready || 0}/${w.desired} pods ready · ${w.ns}`,"workloads")),
        ...vmIssues.map(v=>item(v.name,"medium",v.name,v.status || "VM not ready","vms"))]};
  }
  let data={}, request=null, generation=0, checkedAt=0;
  const open = route => { if(route==="retry")return load(true);if(route==="about"){settingsTab("about");return go("settings");}if(route==="updates"){settingsTab("updates");return go("settings");}if(route==="jobs")return jobsDialog();return go(route); };
  const list = (rows,limit=Infinity) => UI.insightList(rows.slice(0,limit).map(r=>({...r,tone:tone(r.severity),label:r.severity,action:"Review",onclick:`HealthInsights.open(${jsArg(r.route)})`})));
  function healthBody(compact=false) {
    const rows=advice(data), max=Math.max(...(data.nodes || []).map(n=>n.temps?.max_c).filter(known));
    const counts=["critical","medium","low"].map(level=>`${rows.filter(r=>r.severity===level).length} ${level}`).join(" · ");
    return `<div class="insight-summary">${esc(counts)}${Number.isFinite(max)?`<span>Max observed temperature ${esc(max)}°C</span>`:""}</div>`+
      (rows.length?list(rows,compact?3:Infinity):'<div class="empty small">No suggestions from the available checks.</div>')+
      (compact && rows.length>3?`<div class="ui-help">${rows.length-3} more in Cluster Health</div>`:"");
  }
  function body(id) {
    if (!needs[id].every(key=>key in data))return '<div class="empty small">Loading checks…</div>';
    if(id==="health")return healthBody(true);
    if(id!=="updates" && needs[id].some(key=>data[key]===null))return UI.callout("warn","Checks unavailable","Retry to refresh this widget.")+UI.button("Retry","HealthInsights.load(true)");
    if(id==="backups")return `<div class="ui-help">Recorded external backups, oldest first. Check schedules in Protection.</div>`+list(backupRows(data.protection),4);
    if(id==="workloads") {const s=workloadSummary(data.workloads,data.vms);return `<div class="insight-summary">${s.ready}/${s.active} apps ready<span>${s.stopped} stopped</span></div>`+(s.rows.length?list(s.rows,4):'<div class="empty small">No active workloads need attention.</div>');}
    if(id==="updates") {
      const r=data.updates, rows=(r?.workloads || []).filter(w=>w.available || w.images?.some(i=>i.error)).map(w=>item(w.ns+"/"+w.name,w.available?"low":"medium",w.name,w.available?"Image update available":"Registry check failed","updates"));
      for(const c of data.components?.components || [])if(c.next)rows.push(item("component:"+c.id,"low",c.name,`${c.installed} → ${c.next}`,"updates"));
      for(const [name,h] of Object.entries(data.hosts?.hosts || {}))if(h.updates?.length || h.reboot)rows.push(item("host:"+name,h.security?"medium":"low",name,`${h.updates?.length || 0} package updates${h.reboot?" · restart needed":""}`,"updates"));
      const incomplete=needs.updates.some(k=>data[k]===null) || r?.partial || (Array.isArray(r?.errors)?r.errors.length:r?.errors) || (data.hosts?.applies && !Object.keys(data.hosts.hosts || {}).length);
      return `<div class="insight-summary">${rows.length} item${rows.length===1?"":"s"} to review</div>`+list(rows,4)+(rows.length?"":'<div class="empty small">No updates reported by the available checks.</div>')+(incomplete?'<div class="ui-help">Some update checks are unavailable or incomplete. Review Updates.</div>':"");
    }
    if(id==="jobs") {const jobs=data.jobs, active=jobs.filter(j=>!["succeeded","failed","cancelled"].includes(j.status)), failed=jobs.filter(j=>j.status==="failed");return `<div class="insight-summary">${active.length} running<span>${failed.length} failed</span></div>`+list([...failed,...active].map(j=>item(j.id,j.status==="failed"?"critical":"low",j.title,j.message || j.status,"jobs")),4);}
    return "";
  }
  function widget(id) {const route={health:"cluster",workloads:"workloads",backups:"protect",updates:"updates",jobs:"jobs"}[id];return `<div class="card flat insight-widget">${UI.moduleHeader(titles[id],"",UI.button("View all",`HealthInsights.open('${route}')`,{attrs:`aria-label="View all ${titles[id].toLowerCase()}"`}))}<div data-insight="${id}">${body(id)}</div></div>`;}
  function paint() {for(const el of document.querySelectorAll("[data-insight]"))el.innerHTML=el.dataset.insight==="full"?healthBody():body(el.dataset.insight);window.applyRole?.();}
  async function load(force=false) {
    if(request){await request;return load(force);}
    const ids=[...document.querySelectorAll("[data-insight]")].map(el=>el.dataset.insight==="full"?"health":el.dataset.insight);
    const keys=[...new Set(ids.flatMap(id=>needs[id] || []))];if(!keys.length)return;
    if(!force && checkedAt>Date.now()-30000 && keys.every(key=>key in data)){paint();return;}
    const token=window.NAV_TOKEN, epoch=generation;
    request=(async()=>{
      const results=await Promise.all(keys.map(async key=>{
        if(key==="protection" && STATE.platform?.longhorn===false)return [key,{volumes:[]}];
        if(key==="vms" && STATE.platform?.kubevirt===false)return [key,[]];
        try{
          const value=await api(sources[key],{keep:true});
          const arrays=["nodes","workloads","vms","jobs"], fields={cluster:"control_plane",protection:"volumes",self:"api",settings:"thresholds",updates:"workloads",components:"components"};
          const valid=arrays.includes(key)?Array.isArray(value):value && typeof value==="object" && !value.error && (!fields[key] || value[fields[key]]!==undefined);
          return [key,valid?value:null];
        }catch{return [key,null];}
      }));
      if(token!==window.NAV_TOKEN || epoch!==generation)return;
      data={...data,...Object.fromEntries(results)};checkedAt=Date.now();paint();
    })().finally(()=>{request=null;});return request;
  }
  function liveJobs(jobs,stale=false) {data.jobs=stale?null:jobs;for(const el of document.querySelectorAll('[data-insight="jobs"]'))el.innerHTML=body("jobs");}
  return {titles,advice,backupRows,workloadSummary,widget,load,open,liveJobs,reset(){generation++;data={};checkedAt=0;},healthBody};
})();
if(typeof window!=="undefined")window.HealthInsights=HealthInsights;
if(typeof module!=="undefined")module.exports=HealthInsights;
