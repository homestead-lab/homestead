/* Read-only, shared health advice for the dashboard and Cluster Health.
   Missing observations stay unknown; advice never changes cluster configuration. */
const HealthInsights = (() => {
  const titles = {health:"Health suggestions", workloads:"Workload health", containers:"Containers", vms:"Virtual machines", backups:"Backup freshness", updates:"Updates awaiting review", jobs:"Running and failed jobs"};
  const sources = {alerts:"/api/alerts",overview:"/api/overview",cluster:"/api/cluster", protection:"/api/lh/overview", nodes:"/api/nodes", self:"/api/self/health", settings:"/api/settings", workloads:"/api/workloads", vms:"/api/vms", updates:"/api/image-updates", jobs:"/api/operations", components:"/api/cluster/components", hosts:"/api/os-updates"};
  const needs = {health:["cluster","protection","nodes","self","settings","overview","alerts"], workloads:["workloads","vms"], containers:["workloads"], vms:["vms"], backups:["protection"], updates:["updates","components","hosts"], jobs:["jobs"]};
  const rank = {critical:0,medium:1,low:2};
  const tone = severity => ({critical:"bad",medium:"warn",low:"info",healthy:"ok"}[severity] || "info");
  const item = (id,severity,title,detail,route) => ({id,severity,title,detail,route});
  const known = n => typeof n === "number" && Number.isFinite(n);
  function advice(data) {
    const rows=[], cp=data.cluster?.control_plane;
    if (cp?.etcd_total > 0) {
      if (known(cp.etcd_ready) && cp.etcd_ready < Math.floor(cp.etcd_total/2)+1)
        rows.push(item("quorum","critical","etcd quorum is unavailable","Restore unavailable etcd hosts before making cluster changes.","cluster"));
      else if (cp.etcd_total === 1)
        rows.push(item("quorum","low","Single control-plane host","This cluster's state is kept on one host, so it has no failover: if that host fails, the cluster comes back only from an etcd snapshot. That is normal for a one-node cluster. Keep snapshots (k3s takes one every 12 hours; the node doctor can take one now) and copy them to another machine. Three server nodes would survive the loss of one.","cluster"));
      else if (cp.etcd_total === 2)
        rows.push(item("quorum","medium","Two etcd members have no failure margin","Add a third etcd server. Both current members are required for quorum.","cluster"));
      else if (cp.quorum_margin === 0)
        rows.push(item("quorum","medium","No etcd failure margin",`${cp.etcd_ready} of ${cp.etcd_total} etcd members are ready, the fewest that keep quorum: losing one more stops the control plane. Bring the others back before changing hosts.`,"cluster"));
    }
    if (data.cluster?.unavailable?.length) rows.push(item("cluster-partial","low","Some platform checks are unavailable","Open Cluster to review missing observations.","cluster"));
    for (const n of data.cluster?.nodes || []) {
      if (n.ready === false) rows.push(item("node:"+n.name,"critical",n.name+" is not ready","Check this host before restarting or moving workloads.","node:"+n.name));
      else if (n.pressure?.length) rows.push(item("pressure:"+n.name,"medium",n.name+" reports resource pressure",n.pressure.join(", ")+". Free capacity or move workloads.","node:"+n.name));
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
      if (diskLimit && known(n.fs_pct) && n.fs_pct >= diskLimit.warning) rows.push(item("disk-space:"+n.name,n.fs_pct>=diskLimit.critical?"critical":"medium",`${n.name}: disk ${Math.round(n.fs_pct)}% full`,"Free space or expand capacity before storage fills.","node:"+n.name));
      if (temp && known(n.temps?.max_c) && n.temps.max_c >= temp.warning) rows.push(item("temperature:"+n.name,n.temps.max_c>=temp.critical?"critical":"medium",`${n.name}: maximum ${n.temps.max_c}°C`,"Inspect the sensor and cooling. Drive temperatures use their own thresholds.","node:"+n.name));
      for (const d of n.temps?.disks || []) {
        if (["critical","attention","degraded"].includes(d.health?.state)) rows.push(item("disk:"+n.name+":"+d.name,d.health.state==="critical"?"critical":"medium",`${n.name} / ${d.name}: drive needs attention`,d.health.summary || "Check SMART results and verify backups before replacing the drive.","disk:"+n.name+":"+d.name));
        else if (!d.health || d.health.stale_probe || ["unknown","unsupported"].includes(d.health.state)) rows.push(item("disk:"+n.name+":"+d.name,"low",`${n.name} / ${d.name}: drive health unknown`,"Check the node probe and SMART support.","disk:"+n.name+":"+d.name));
      }
    }
    // Keep the exact reasons behind the dashboard banner visible here too.
    for(const issue of data.overview?.health_issues || []) {
      const id=issue.kind==="Disk"?"disk:"+issue.name.replace("/",":"):issue.kind==="Node"?"node:"+issue.name:"condition:"+issue.kind+":"+issue.name;
      const existing=rows.findIndex(row=>row.id===id), severity=issue.severity==="critical"?"critical":"medium";
      const route=["Disk","Node"].includes(issue.kind)?id:({Volume:"storage",Backup:"protect",Workload:"workloads"})[issue.kind] || "cluster";
      const finding=item(id,severity,`${issue.kind} ${issue.name}`,issue.reason || "Review this condition.",route);
      if(existing<0)rows.push(finding);
      else if(rank[severity]<rank[rows[existing].severity])rows[existing]=finding;
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
    for (const key of needs.health) if (key !== "alerts" && data[key] === null) rows.push(item("unknown:"+key,"low",`${({cluster:"Platform",protection:"Backup",nodes:"Host",self:"Homestead",settings:"Threshold",overview:"Cluster health"})[key]} checks unavailable`,"Retry or check access. Health cannot be confirmed from missing data.","retry"));
    // A finding the cluster tracks as an alert can be acknowledged by this
    // account; it returns by itself when the condition gets worse.
    const alerts=new Map((data.alerts?.active || []).map(a=>[a.key,a]));
    for (const row of rows) {
      const alert=alerts.get(alertKey(row.id));
      if (alert?.version) row.alert={key:alert.key,version:alert.version,acknowledged:!!alert.acknowledged};
    }
    return rows.sort((a,b)=>Number(!!a.alert?.acknowledged)-Number(!!b.alert?.acknowledged) || rank[a.severity]-rank[b.severity] || a.id.localeCompare(b.id));
  }
  // The alert behind a finding: health:<kind>:<name>, as the server names it.
  function alertKey(id) {
    if (id.startsWith("disk:")) { const [node,...disk]=id.slice(5).split(":"); return `health:Disk:${node}/${disk.join(":")}`; }
    if (id.startsWith("node:")) return "health:Node:"+id.slice(5);
    if (id.startsWith("condition:")) return "health:"+id.slice(10);
    return "";
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
  // Both list widgets share row semantics and formatting with their collection pages.
  function resourceRows(id, records=[]) {
    return records.filter(r=>id!=="containers" || !r.platform).map(r=>{
      const vm=id==="vms", stopped=vm?r.status==="Stopped":r.desired===0;
      const ready=vm?r.status==="Running":r.desired>0 && r.ready>=r.desired;
      const status=vm?r.status || "Unknown":stopped?"Stopped":ready?"Running":known(r.ready)&&known(r.desired)?`${r.ready}/${r.desired} ready`:"Unknown";
      return {record:r,name:r.name,ns:r.ns,status,tone:stopped?"neutral":ready?"ok":vm && /Error|Fail|Crash|BackOff/i.test(status)?"bad":"warn",
        cpu:stopped?null:vm?r.usage?.cpu_pct:known(r.cpu)?r.cpu*100:null,
        memory:stopped?null:vm?r.usage?.mem:known(r.mem_mb)?r.mem_mb*1024**2:null};
    }).sort((a,b)=>a.name.localeCompare(b.name) || String(a.ns).localeCompare(String(b.ns)));
  }
  function filteredRows(id, records, settings={}) {
    return resourceRows(id,records).filter(r=>(!settings.groups || settings.groups.includes(r.record.group || "")) &&
      (!settings.status || settings.status==="all" || (settings.status==="running"?r.status==="Running":settings.status==="stopped"?r.status==="Stopped":!["Running","Stopped"].includes(r.status))));
  }
  function resourceList(id, records, settings={}) {
    const rows=filteredRows(id,records,settings), running=rows.filter(r=>r.status==="Running").length;
    const header=`<thead><tr><th>Name</th><th>State</th><th title="${id==="vms"?"Launcher CPU usage as a percentage of assigned cores":"100% equals one fully used CPU core"}">CPU</th><th>Memory</th></tr></thead>`;
    const row=r=>`<tr${clusterAttr(r.record)}><td><div class="dashboard-resource-name">${UI.statusDot(r.tone)}<span title="${esc([r.ns,r.name].filter(Boolean).join("/"))}">${esc(r.name)}</span></div></td><td><span class="dashboard-resource-state ${r.tone}" title="${esc(r.record.problem || r.status)}">${esc(r.status)}</span></td><td class="mono">${known(r.cpu)?esc(Math.round(r.cpu*10)/10)+"%":"—"}</td><td class="mono">${known(r.memory)?esc(vmBytes(r.memory)):"—"}</td></tr>`;
    const groups=Array.from({length:Math.ceil(rows.length/4)},(_,i)=>rows.slice(i*4,i*4+4));
    const table=rows.length?`<div class="dashboard-resource-scroll" tabindex="0" role="region" aria-label="${titles[id]} list"><div class="dashboard-resource-columns">${groups.map(group=>`<table class="dashboard-resource-table">${header}<tbody>${group.map(row).join("")}</tbody></table>`).join("")}</div></div>`:'<div class="empty small">No '+(id==="vms"?"virtual machines":"app containers")+' match this view.</div>';
    return table+`<div class="dashboard-resource-footer">${rows.length} ${id==="vms"?"VMs":"containers"}<span>${running} running</span></div>`;
  }
  let data={}, request=null, generation=0, checkedAt=0;
  // A finding about one host or one drive opens that host's page, at the drive.
  const open = route => { if(route==="health")return go("cluster",{params:{section:"health"}});
    if(route.startsWith("node:"))return go("nodes",{params:{node:route.slice(5)}});
    if(route.startsWith("disk:")){const [node,...disk]=route.slice(5).split(":");return go("nodes",{params:{node,disk:disk.join(":")}});} if(route==="retry")return load(true);if(route==="about"){settingsTab("about");return go("settings");}if(route==="updates"){settingsTab("updates");return go("settings");}if(route==="jobs")return jobsDialog();return go(route); };
  const list = (rows,limit=Infinity) => UI.insightList(rows.slice(0,limit).map(r=>{
    const acked=r.alert?.acknowledged, review=`HealthInsights.open(${jsArg(r.route)})`;
    const actionsHtml=r.alert ? UI.button("Review",review)+UI.button(acked?"Undo":"Acknowledge",`HealthInsights.acknowledge(${jsArg(r.alert.key)},${jsArg(r.alert.version)},${acked})`,
      {attrs:`data-tip="${acked?"Show this finding as active again":"Mark as reviewed until it gets worse"}"`}) : "";
    return {...r,tone:acked?"info":r.tone || tone(r.severity),label:acked?"Acknowledged":r.label || r.severity,action:"Review",onclick:review,actionsHtml};
  }));
  async function acknowledge(key,version,undo=false) {
    try{await api("/api/alerts/acknowledge",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,version,undo})});
      toast(undo?"Acknowledgement removed":"Acknowledged until this condition worsens","ok");}
    catch(e){toast(e.message,"bad");}
    await load(true);
    window.refreshPwaAlerts?.(true);
  }
  function healthBody(compact=false) {
    const rows=advice(data), max=Math.max(...(data.nodes || []).map(n=>n.temps?.max_c).filter(known));
    const open=rows.filter(r=>!r.alert?.acknowledged), acked=rows.length-open.length;
    const counts=["critical","medium","low"].map(level=>`${open.filter(r=>r.severity===level).length} ${level}`).join(" · ")+(acked?` · ${acked} acknowledged`:"");
    return `<div class="insight-summary">${esc(counts)}${Number.isFinite(max)?`<span>Max observed temperature ${esc(max)}°C</span>`:""}</div>`+
      (rows.length?list(rows,compact?3:Infinity):'<div class="empty small">No suggestions from the available checks.</div>')+
      (compact && rows.length>3?`<div class="ui-help">${rows.length-3} more in Cluster Health</div>`:"");
  }
  function body(id, settings={}) {
    if (!needs[id].every(key=>key in data))return '<div class="empty small">Loading checks…</div>';
    if(id==="health")return healthBody(true);
    if(id!=="updates" && needs[id].some(key=>data[key]===null))return UI.callout("warn","Checks unavailable","Retry to refresh this widget.")+UI.button("Retry","HealthInsights.load(true)");
    if(id==="containers" || id==="vms")return resourceList(id,data[id==="containers"?"workloads":"vms"],settings);
    if(id==="backups")return `<div class="ui-help">Recorded external backups, oldest first. Check schedules in Protection.</div>`+(data.protection.volumes.length ? list(backupRows(data.protection).map(r=>({...r,label:r.severity==="medium"?"Missing":"Recorded"})),4) : '<div class="empty small">No Longhorn volumes reported.</div>');
    if(id==="workloads") {const s=workloadSummary(data.workloads,data.vms);return `<div class="insight-summary">${s.ready}/${s.active} apps ready<span>${s.stopped} stopped</span></div>`+(s.rows.length?list(s.rows,4):'<div class="empty small">No active workloads need attention.</div>');}
    if(id==="updates") {
      const r=data.updates, rows=(r?.workloads || []).filter(w=>w.available || w.images?.some(i=>i.error)).map(w=>item(w.ns+"/"+w.name,w.available?"low":"medium",w.name,w.available?"Image update available":"Registry check failed","updates"));
      for(const c of data.components?.components || [])if(c.next)rows.push(item("component:"+c.id,"low",c.name,`${c.installed} → ${c.next}`,"updates"));
      for(const [name,h] of Object.entries(data.hosts?.hosts || {}))if(h.updates?.length || h.reboot)rows.push(item("host:"+name,h.security?"medium":"low",name,`${h.updates?.length || 0} package updates${h.reboot?" · restart needed":""}`,"updates"));
      const incomplete=needs.updates.some(k=>data[k]===null) || r?.partial || (Array.isArray(r?.errors)?r.errors.length:r?.errors) || (data.hosts?.applies && !Object.keys(data.hosts.hosts || {}).length);
      return `<div class="insight-summary">${rows.length} item${rows.length===1?"":"s"} to review</div>`+list(rows,4)+(rows.length?"":'<div class="empty small">No updates reported by the available checks.</div>')+(incomplete?'<div class="ui-help">Some update checks are unavailable or incomplete. Review Updates.</div>':"");
    }
    if(id==="jobs") {const jobs=data.jobs, active=jobs.filter(j=>!["succeeded","failed","cancelled"].includes(j.status)), failed=jobs.filter(j=>j.status==="failed");return `<div class="insight-summary">${active.length} running<span>${failed.length} failed</span></div>`+list([...failed,...active].map(j=>({...item(j.id,j.status==="failed"?"critical":"low",j.title,j.message || j.status,"jobs"),label:j.status,tone:j.status==="failed"?"bad":"warn"})),4);}
    return "";
  }
  function widget(id, settings={}) {const resource=["containers","vms"].includes(id);const route={containers:"workloads",vms:"vms",health:"health",workloads:"workloads",backups:"protect",updates:"updates",jobs:"jobs"}[id];return `<div class="card flat insight-widget${resource?" dashboard-resource-widget":""}"${resource?` style="--resource-list-height:${settings.height || 360}px"`:""}>${UI.moduleHeader(titles[id],"",UI.button("View all",`HealthInsights.open('${route}')`,{attrs:`aria-label="View all ${titles[id].toLowerCase()}"`}))}<div data-insight="${id}">${body(id,settings)}</div></div>`;}
  // The dashboard banner: once this account has acknowledged every issue in
  // it, it says so quietly instead of warning.
  function paintBanner() {
    const banner=document.querySelector(".clusteralert"), issues=data.overview?.health_issues || [];
    if(!banner || !data.alerts || !issues.length)return;
    const alerts=new Map((data.alerts.active || []).map(a=>[a.key,a]));
    const done=issues.every(i=>alerts.get(`health:${i.kind}:${i.name}`)?.acknowledged), title=banner.querySelector("b");
    title.dataset.text ||= title.textContent;
    banner.classList.toggle("acknowledged",done);
    title.textContent=done?"Acknowledged until it gets worse":title.dataset.text;
  }
  function paint() {paintBanner();for(const el of document.querySelectorAll("[data-insight]")){const next=el.cloneNode(false);next.innerHTML=el.dataset.insight==="full"?healthBody():body(el.dataset.insight,window.Dashboard?.settings(el.dataset.insight));morph(el,next);}window.Dashboard?.refreshOptions();window.applyRole?.();}
  async function load(force=false) {
    if(request){await request;return load(force);}
    const ids=[...document.querySelectorAll("[data-insight]")].map(el=>el.dataset.insight==="full"?"health":el.dataset.insight);
    const keys=[...new Set([...ids.flatMap(id=>needs[id] || []),...(document.querySelector(".clusteralert")?["overview","alerts"]:[])])];if(!keys.length)return;
    if(!force && checkedAt>Date.now()-30000 && keys.every(key=>key in data)){paint();return;}
    const token=window.NAV_TOKEN, epoch=generation;
    request=(async()=>{
      const results=await Promise.all(keys.map(async key=>{
        if(key==="protection" && STATE.platform?.longhorn===false)return [key,{volumes:[]}];
        if(key==="vms" && STATE.platform?.kubevirt===false)return [key,[]];
        try{
          const value=await api(sources[key],{keep:true});
          const arrays=["nodes","workloads","vms","jobs"], fields={alerts:"active",overview:"health_issues",cluster:"control_plane",protection:"volumes",self:"api",settings:"thresholds",updates:"workloads",components:"components"};
          const valid=arrays.includes(key)?Array.isArray(value):value && typeof value==="object" && !value.error && (!fields[key] || value[fields[key]]!==undefined);
          return [key,valid?value:null];
        }catch{return [key,null];}
      }));
      if(token!==window.NAV_TOKEN || epoch!==generation)return;
      data={...data,...Object.fromEntries(results)};checkedAt=Date.now();paint();
    })().finally(()=>{request=null;});return request;
  }
  function liveJobs(jobs,stale=false) {data.jobs=stale?null:jobs;for(const el of document.querySelectorAll('[data-insight="jobs"]'))el.innerHTML=body("jobs");}
  return {titles,advice,acknowledge,alertKey,backupRows,workloadSummary,resourceRows,filteredRows,groups:()=>data.workloads?.filter(w=>!w.platform).map(w=>w.group || ""),widget,load,open,liveJobs,reset(){generation++;data={};checkedAt=0;},healthBody};
})();
if(typeof window!=="undefined")window.HealthInsights=HealthInsights;
if(typeof module!=="undefined")module.exports=HealthInsights;
