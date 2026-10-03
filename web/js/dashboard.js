/* Personal dashboard composition. Widget data/renderers stay owned by their features.
   Order is also the reading/tab order; the grid never backfills visual gaps. */
const Dashboard = (() => {
  const widgets = Object.freeze({
    compute: {title:"Compute", description:"Live CPU and memory", width:4, widths:[4,6,8,12]},
    throughput: {title:"Throughput", description:"Network traffic and local disk", width:4, widths:[4,6,8,12]},
    storage: {title:"Storage", description:"Capacity and replica health", width:4, widths:[4,6,8,12]},
    nodes: {title:"Node health", description:"Host metrics and drive warnings", width:12, widths:[4,6,8,12]},
    cpu: {title:"Top CPU", description:"The busiest workloads", width:6, widths:[4,6,8,12]},
    memory: {title:"Top memory", description:"Workloads using the most RAM", width:6, widths:[4,6,8,12]},
    history: {title:"Over time", description:"Recorded cluster metrics", width:12, widths:[8,12]},
    ...Object.fromEntries([
      ["health","Health suggestions","Prioritized checks and next steps"],
      ["workloads","Workload health","Readiness and stopped workloads"],
      ["containers","Containers","Compact app status and live usage"],
      ["vms","Virtual machines","Compact VM status and live usage"],
      ["backups","Backup freshness","Oldest external backups and missing copies"],
      ["updates","Updates awaiting review","Container, platform and host updates"],
      ["jobs","Running and failed jobs","Active work and outcomes needing attention"],
    ].map(([id,title,description])=>[id,{title,description,width:6,widths:[4,6,8,12],optional:true}])),
    portal: {title:"Portal links", description:"Your apps and devices", width:6, widths:[4,6,8,12]},
  });
  const widths = {4:"One third",6:"Half",8:"Two thirds",12:"Full width"};
  const defaults = () => Object.entries(widgets).filter(([id,w])=>id!=="portal" && !w.optional).map(([id,w])=>({id,width:w.width,height:0}));
  const normalize = value => {
    if (value?.version !== 1 || !Array.isArray(value.items)) return defaults();
    const seen = new Set();
    return value.items.filter(item=>item && Object.hasOwn(widgets,item.id) && !seen.has(item.id) && seen.add(item.id)).map(item=>({
      id:item.id, width:widgets[item.id].widths.includes(item.width)?item.width:widgets[item.id].width,
      height:[0,240,360,520].includes(item.height)?item.height:0,
      ...(item.id==="nodes" && item.display==="detailed"?{display:"detailed"}:{}),
    }));
  };
  const endpoint = "/api/auth/preferences/dashboard";
  const legacyKey = () => `homestead.dashboard.v1.${encodeURIComponent(ME || "guest")}`;
  const legacy = () => { try { const value=JSON.parse(localStorage.getItem(legacyKey()));return value?.version===1 && Array.isArray(value.items)?{version:1,items:normalize(value)}:null; } catch { return null; } };
  const clearLegacy = () => { try {localStorage.removeItem(legacyKey());}catch{} };
  let layout = null, revision = null, loadedUser = null, loadError = "", layoutRequest = null, saving = false;
  const read = () => clone(loadedUser===ME && layout!==null ? layout : legacy()?.items || defaults());
  let draft = null, initial = "", baseline = null, undo = [], redo = [], selected = "", phone = false, panelOpen = false, gesture = null, portalRequest = null, session = 0;
  function clearGesture() {
    const g=gesture;gesture=null;
    if(!g)return;
    g.preview?.remove();
    g.el.classList.remove("dragging","drag-source");
    document.querySelectorAll(".dashboard-widget.drop-target").forEach(el=>el.classList.remove("drop-target"));
    if(g.handle.hasPointerCapture(g.pointerId))g.handle.releasePointerCapture(g.pointerId);
  }
  function dragPreview(g) {
    const preview=document.createElement("div");
    preview.className="dashboard-drag-preview";preview.setAttribute("aria-hidden","true");preview.inert=true;
    // Keep the original canvas width so its container queries render identically.
    const canvas=document.createElement("div");canvas.className="dashboard-canvas";canvas.dataset.editing="true";
    canvas.style.width=`${g.el.closest(".dashboard-canvas").getBoundingClientRect().width}px`;
    const card=g.el.cloneNode(true);card.classList.remove("dragging","drag-source","selected","drop-target");
    card.style.width=`${g.rect.width}px`;card.style.height=`${g.rect.height}px`;
    // Copies of SVG gradients need their own IDs, as do the widget's other nodes.
    const ids=new Map();
    for(const el of [card,...card.querySelectorAll("[id]")])if(el.id){const id=el.id;el.id=`dashboard-drag-${id}`;ids.set(id,el.id);}
    for(const el of [card,...card.querySelectorAll("*")])for(const attr of [...el.attributes]){
      let value=attr.value;
      for(const [id,replacement] of ids){value=value.replaceAll(`url(#${id})`,`url(#${replacement})`);if(value===`#${id}`)value=`#${replacement}`;}
      if(value!==attr.value)el.setAttribute(attr.name,value);
    }
    canvas.append(card);preview.append(canvas);document.body.append(preview);
    g.preview=preview;g.previewCard=card;g.el.classList.add("drag-source");
  }
  async function load() {
    if(draft!==null)return true;
    if(layoutRequest)return layoutRequest;
    const requestedSession=session, user=ME;
    if(loadedUser!==user){layout=null;revision=null;loadError="";loadedUser=user;}
    layoutRequest=(async()=>{
      try{
        let answer=await api(endpoint,{keep:true});
        if(requestedSession!==session || user!==ME)return false;
        if(!answer || !("revision" in answer) || !("layout" in answer))throw new Error("Dashboard preferences are unavailable.");
        const old=legacy();
        if(answer.layout===null && old){
          try{answer=await api(endpoint,{method:"POST",body:JSON.stringify({revision:answer.revision,layout:old})});}
          catch(error){
            // A first-save race has a winner. Never replace that account layout
            // with another browser's legacy data; leave the legacy copy on failure.
            const latest=await api(endpoint,{keep:true});
            if(latest.layout===null)throw error;
            answer=latest;
          }
        }
        if(requestedSession!==session || user!==ME)return false;
        layout=normalize(answer.layout);revision=answer.revision;loadError="";
        if(answer.layout!==null)clearLegacy();
        return true;
      }catch(error){if(requestedSession===session && user===ME)loadError=error.message || "Could not load your dashboard layout.";return false;}
      finally{if(requestedSession===session)layoutRequest=null;}
    })();
    return layoutRequest;
  }
  const clone = value => JSON.parse(JSON.stringify(value));
  const dirty = () => draft !== null && JSON.stringify(draft) !== initial;
  const announce = text => { const host=document.getElementById("dashboardStatus"); if(host) host.textContent=text; };
  function widgetContent(id, item={}) {
    if (id === "nodes") return dashboardNodes(STATE.data.ov?.nodes || [], item.display || "compact");
    if (widgets[id]?.optional) return HealthInsights.widget(id,item);
    if (id === "portal") return `<div class="card flat dashboard-portal">${UI.moduleHeader("Portal links", "", '<button class="btn sm" onclick="go(\'portal\')">Open Portal</button>')}
      <div id="dashboardPortal">${portalBody()}</div></div>`;
    if (id === "history") return `<section class="card flat history-card" id="historyCard">${STATE.data.historyHtml || '<div class="empty small">History is loading…</div>'}</section>`;
    return apiObject.content[id] || '<div class="card flat empty">Data unavailable</div>';
  }
  function portalBody() {
    if (STATE.data.dashboardPortalError) return '<div class="empty small">Portal links could not be loaded. <button class="btn sm" onclick="Dashboard.loadPortal(true)">Retry</button></div>';
    const data = STATE.data.portal;
    if (!data) return '<div class="empty small">Loading links…</div>';
    return data.links?.length ? portalTiles(data.links, {compact:true}) : '<div class="empty small">No links yet. Add apps and devices in Portal.</div>';
  }
  async function loadPortal(force=false) {
    if (!(draft || read()).some(item=>item.id==="portal")) return;
    if(portalRequest) return portalRequest;
    const requestedSession=session;
    portalRequest=(async()=>{
      try {
        const data=await api("/api/portal");if(requestedSession!==session)return;STATE.data.portal=data;STATE.data.dashboardPortalError=false;
        const host=document.getElementById("dashboardPortal");if(host)host.innerHTML=portalBody();
        const status=await api(`/api/portal/status${force?"?force=1":""}`).catch(()=>({}));
        if(requestedSession!==session)return;STATE.data.portalStatus=status;
        portalDots();
      } catch {if(requestedSession!==session)return;STATE.data.dashboardPortalError=true;const host=document.getElementById("dashboardPortal");if(host)host.innerHTML=portalBody();}
      finally {if(requestedSession===session)portalRequest=null;}
    })();
    return portalRequest;
  }
  function controls(item) {
    const w=widgets[item.id];
    return `<div class="dashboard-widget-tools">
      <button type="button" class="dashboard-grip" data-dash-drag="${item.id}" aria-label="Move ${w.title}" title="Drag to move. Use arrow keys to reorder.">⠿ <span>${w.title}</span></button>
      <button type="button" class="btn sm" aria-label="Settings for ${w.title}" onclick="Dashboard.select(${jsq(item.id)})">Settings</button>
      <button type="button" class="iconbtn" aria-label="Remove ${w.title}" onclick="Dashboard.remove(${jsq(item.id)})">×</button>
    </div>`;
  }
  function render(edit=false) {
    const items=draft || read();
    return `<div class="dashboard-canvas${phone && edit?" dashboard-phone":""}"${edit?' data-editing="true"':''}>
      <div class="dashboard-grid consumer-grid" aria-label="Dashboard widgets">${items.map(item=>`<section class="dashboard-widget${edit && item.id===selected?" selected":""}" data-widget="${item.id}" style="--widget-span:${item.width};--widget-height:${item.height}px" aria-label="${widgets[item.id].title}">
        ${edit?controls(item):""}<div class="dashboard-widget-content"${edit?' inert':''}>${widgetContent(item.id,item)}</div>
        ${edit?`<button type="button" class="dashboard-select" aria-label="Configure ${widgets[item.id].title}" onclick="Dashboard.select(${jsq(item.id)})"></button>
          <button type="button" class="dashboard-resize" data-dash-resize="${item.id}" aria-label="Resize ${widgets[item.id].title}" title="Drag to resize, or use Widget settings">↘</button>`:""}
      </section>`).join("")}</div>
      ${items.length?"":`<div class="dashboard-empty card flat"><b>Your dashboard, your way</b><p class="dim">${edit?"Choose a widget from the library to get started.":"Add the information you use most."}</p>${edit?"":'<button class="btn hide-sm" onclick="Dashboard.start()">Add widgets</button><span class="only-sm">Open this dashboard on a larger screen to add widgets.</span>'}</div>`}
    </div>`;
  }
  function inspector() {
    const item=draft.find(row=>row.id===selected), index=draft.indexOf(item);
    return `<aside class="dashboard-library card flat" data-panel-open="${panelOpen}" aria-label="Dashboard editor controls">
      <button class="dashboard-library-toggle" aria-expanded="${panelOpen}" onclick="Dashboard.togglePanel()"><b>Widgets &amp; settings</b><span>${panelOpen?"Close":"Open"} ${panelOpen?"−":"＋"}</span></button>
      <div class="dashboard-library-body">
      ${UI.moduleHeader("Widgets", "Add what you use. Remove what you don’t.")}
      <div class="dashboard-catalog">${Object.entries(widgets).map(([id,w])=>{
        const added=draft.some(item=>item.id===id);
        return `<button type="button" class="dashboard-catalog-item${id===selected?" on":""}" onclick="Dashboard.${added?"select":"add"}(${jsq(id)})" aria-label="${added?"Configure":"Add"} ${w.title}"><span><b>${w.title}</b><small>${w.description}</small></span><span aria-hidden="true">${added?"✓":"＋"}</span></button>`;
      }).join("")}</div>
      <div class="dashboard-inspector">${UI.moduleHeader("Widget settings",item?widgets[item.id].title:"Select a widget to adjust it.")}
      ${item?`<label>Width<select aria-label="Widget width" onchange="Dashboard.size('width',+this.value)">${widgets[item.id].widths.map(width=>`<option value="${width}" ${width===item.width?"selected":""}>${widths[width]}</option>`).join("")}</select></label>
        <label>Height<select aria-label="Widget height" onchange="Dashboard.size('height',+this.value)">${[[0,"Fit content"],[240,"Short"],[360,"Medium"],[520,"Tall"]].map(([height,label])=>`<option value="${height}" ${height===item.height?"selected":""}>${label}</option>`).join("")}</select></label>
        ${item.id==="nodes"?`<label>Display<select aria-label="Node health display" onchange="Dashboard.nodeDisplay(this.value)"><option value="compact" ${item.display!=="detailed"?"selected":""}>Compact summaries</option><option value="detailed" ${item.display==="detailed"?"selected":""}>Detailed comparison</option></select></label>`:""}
        <div class="dashboard-order"><button class="btn sm" ${index===0?"disabled":""} onclick="Dashboard.move(${jsq(item.id)},${index-1})">↑ Earlier</button><button class="btn sm" ${index===draft.length-1?"disabled":""} onclick="Dashboard.move(${jsq(item.id)},${index+1})">↓ Later</button></div>
        <button class="btn sm danger" onclick="Dashboard.remove(${jsq(item.id)})">Remove widget</button>`:""}</div>
      ${UI.guide("How the grid works", "Drag a widget’s handle to change its position. Drag its bottom corner to resize, or use Widget settings. Cards snap to columns and grow to fit their content. On phones, cards stack in the same order with automatic height. Arrow keys on a move handle reorder cards. Metrics pause while editing. Your layout follows your account across browsers and devices.")}
    </div></aside>`;
  }
  function editor(message="") {
    if(draft===null)return;
    const focus=document.activeElement, focusId=focus?.dataset.dashDrag;
    paint(`${UI.pageHeader("Edit dashboard", "Arrange your widgets. Your layout follows your account.")}
      <div class="dashboard-mobile-notice only-sm" role="status">Use a larger screen to continue arranging widgets. Your unsaved changes are kept.</div>
      <div class="dashboard-edit-toolbar"><div class="row"><span class="tag">Editing</span><span class="dim small">${draft.length} widgets</span><span class="dim small">${dirty()?"Unsaved changes":"Layout saved"}</span></div>
        <div class="row"><button class="btn sm" onclick="Dashboard.history(-1)" ${undo.length?"":"disabled"}>Undo</button><button class="btn sm" onclick="Dashboard.history(1)" ${redo.length?"":"disabled"}>Redo</button><button class="btn sm" onclick="Dashboard.reset()">Reset layout</button>
        <button class="btn sm" aria-pressed="${phone}" onclick="Dashboard.preview()">${phone?"Desktop canvas":"Phone preview"}</button>${UI.button("Cancel", "Dashboard.cancel()", {disabled:saving})}${UI.button(saving?"Saving…":"Save layout", "Dashboard.save()", {kind:"pri",disabled:saving})}</div></div>
      ${(STATE.data.ov?.health_issues || []).length ? UI.callout("warn", "Cluster needs attention", esc(STATE.data.ov.health_summary || "Review cluster health after editing.")) : ""}
      <div class="dashboard-edit-layout"><div class="dashboard-workbench"><div class="dashboard-canvas-label"><span>${phone?"Phone · stacked layout":"Grid · drag to arrange"}</span><span>${phone?"Order is shared with desktop":"12 columns · automatic rows"}</span></div>${render(true)}</div>${inspector()}</div>
      <div id="dashboardStatus" class="sr-only" role="status" aria-live="polite">${esc(message)}</div>`);
    const current=draft.find(item=>item.id===selected);
    for(const field of ["width","height"]){const input=document.querySelector(`[aria-label="Widget ${field}"]`);if(input && current)input.value=String(current[field]);}
    // Editing never runs the widget's actions. Apply role restrictions to the preview too.
    if(focusId)document.querySelector(`[data-dash-drag="${focusId}"]`)?.focus({preventScroll:true});
  }
  function change(fn,message) {
    if(draft===null || saving)return;
    const before=clone(draft);fn();
    draft=normalize({version:1,items:draft});
    if(JSON.stringify(before)!==JSON.stringify(draft)){undo.push(before);if(undo.length>50)undo.shift();redo=[];}
    editor(message);
  }
  function select(id){if(draft?.some(item=>item.id===id)){
    selected=id;panelOpen=true;editor();
    if(innerWidth<=1100)document.querySelector('.dashboard-inspector')?.scrollIntoView({block:"nearest"});
  }}
  function move(id,to) {
    const from=draft?.findIndex(item=>item.id===id);if(from==null || from<0)return;
    to=Math.max(0,Math.min(draft.length-1,to));
    selected=id;change(()=>{const [item]=draft.splice(from,1);draft.splice(to,0,item);},`${widgets[id].title} moved to position ${to+1}.`);
  }
  async function leave() {
    if(saving){toast("Wait for the layout to finish saving.","warn");return false;}
    if(dirty() && !(await ask("Discard dashboard changes?")))return false;
    clearGesture();draft=null;return true;
  }
  async function cancel(){if(await leave()){resetPaint();await viewDash();document.querySelector('[onclick="Dashboard.start()"]')?.focus();}}
  async function start() {
    if(matchMedia("(max-width:900px)").matches){toast("Use a larger screen to edit the dashboard layout.","warn");return;}
    if(draft!==null)return;
    if(!(await load())){toast(loadError,"bad");return;}
    if(draft!==null || matchMedia("(max-width:900px)").matches)return;
    baseline=revision;
    draft=read();initial=JSON.stringify(draft);undo=[];redo=[];phone=false;panelOpen=false;selected=draft[0]?.id || "";
    resetPaint();editor();loadPortal();document.querySelector('[onclick="Dashboard.save()"]')?.focus();
  }
  async function save() {
    if(draft===null || saving)return;
    const requestedSession=session,user=ME;
    clearGesture();saving=true;editor();
    try {
      const answer=await api(endpoint,{method:"POST",body:JSON.stringify({revision:baseline,layout:{version:1,items:draft}})});
      if(requestedSession!==session || user!==ME)return;
      layout=normalize(answer.layout);revision=answer.revision;loadedUser=user;clearLegacy();
    }catch(e){if(requestedSession===session && user===ME){saving=false;editor();toast(e.message || "Your layout could not be saved. Try again.","bad");}return;}
    if(requestedSession!==session || user!==ME)return;
    saving=false;clearGesture();draft=null;resetPaint();await viewDash();toast("Dashboard layout saved to your account","ok");
    document.querySelector('[onclick="Dashboard.start()"]')?.focus();
  }
  const apiObject={widgets, defaults, normalize, content:{}, render, loadPortal, load, notice:()=>loadError?UI.callout("warn","Dashboard layout unavailable","Your saved layout could not be refreshed. Editing is unavailable until it reconnects."):"", editing:()=>draft!==null, dirty, start, save, cancel, leave, select, move,
    invalidate(){window.HealthInsights?.reset();session++;layout=null;revision=null;loadedUser=null;loadError="";layoutRequest=null;saving=false;clearGesture();draft=null;undo=[];redo=[];portalRequest=null;apiObject.content={};},
    add(id){if(!Object.hasOwn(widgets,id)||draft===null||draft.some(item=>item.id===id))return;selected=id;change(()=>draft.push({id,width:widgets[id].width,height:0}),`${widgets[id].title} added.`);if(id==="portal")loadPortal();if(widgets[id]?.optional)HealthInsights.load();},
    remove(id){if(!Object.hasOwn(widgets,id))return;change(()=>{draft=draft.filter(item=>item.id!==id);if(selected===id)selected=draft[0]?.id || "";},`${widgets[id].title} removed. Use Undo to restore it.`);},
    size(field,value){if(!["width","height"].includes(field))return;change(()=>{const item=draft.find(item=>item.id===selected);if(item)item[field]=value;},"Widget size updated.");},
    nodeDisplay(value){if(!["compact","detailed"].includes(value))return;change(()=>{const item=draft.find(row=>row.id==="nodes");if(item)item.display=value;},"Node display updated.");},
    history(direction){const from=direction<0?undo:redo,to=direction<0?redo:undo;if(!from.length || draft===null || saving)return;to.push(clone(draft));draft=from.pop();if(!draft.some(item=>item.id===selected))selected=draft[0]?.id || "";editor(direction<0?"Change undone.":"Change restored.");},
    reset(){change(()=>{draft=defaults();selected=draft[0].id;},"Default layout restored. Save to keep it, or Undo to go back.");},
    preview(){phone=!phone;editor();},
    togglePanel(){panelOpen=!panelOpen;editor();},
  };
  if(typeof document!=="undefined") {
    document.addEventListener("keydown",event=>{
      const resize=event.target.closest?.("[data-dash-resize]");
      if(resize && ["Enter"," "].includes(event.key)){
        event.preventDefault();select(resize.dataset.dashResize);document.querySelector('[aria-label="Widget width"]')?.focus();return;
      }
      const handle=event.target.closest?.("[data-dash-drag]");
      if(handle && ["ArrowUp","ArrowDown","ArrowLeft","ArrowRight","Home","End"].includes(event.key)){
        event.preventDefault();const index=draft.findIndex(item=>item.id===handle.dataset.dashDrag);
        move(handle.dataset.dashDrag,event.key==="Home"?0:event.key==="End"?draft.length-1:index+(["ArrowUp","ArrowLeft"].includes(event.key)?-1:1));
      }
      if(event.key==="Escape" && gesture){gesture.cancel=true;endGesture();}
    });
    document.addEventListener("pointerdown",event=>{
      const handle=event.target.closest?.("[data-dash-drag],[data-dash-resize]");
      if(!handle || draft===null || saving || gesture || event.button!==0)return;
      const id=handle.dataset.dashDrag || handle.dataset.dashResize;
      handle.focus({preventScroll:true});
      const item=draft.find(item=>item.id===id),el=handle.closest(".dashboard-widget");
      gesture={id,el,handle,rect:el.getBoundingClientRect(),item:clone(item),resize:!!handle.dataset.dashResize,x:event.clientX,y:event.clientY,to:draft.findIndex(row=>row.id===id),moved:false,pointerId:event.pointerId};
      handle.setPointerCapture(event.pointerId);event.preventDefault();selected=id;el.classList.add("selected");
    });
    document.addEventListener("pointermove",event=>{
      if(!gesture || event.pointerId!==gesture.pointerId)return;
      const g=gesture,dx=event.clientX-g.x,dy=event.clientY-g.y;
      if(Math.abs(dx)+Math.abs(dy)<6 && !g.moved)return;
      g.moved=true;g.el.classList.add("dragging");
      if(g.resize){
        const grid=g.el.closest(".dashboard-grid").getBoundingClientRect();
        const desired=g.item.width+dx/(grid.width/12);
        g.width=widgets[g.id].widths.reduce((best,n)=>Math.abs(n-desired)<Math.abs(best-desired)?n:best,g.item.width);
        const desiredHeight=(g.item.height || 280)+dy;
        g.height=desiredHeight<200?0:desiredHeight<300?240:desiredHeight<440?360:520;
        g.el.style.setProperty("--widget-span",g.width);g.el.style.setProperty("--widget-height",`${g.height}px`);
        announce(`${widths[g.width]}, ${g.height?g.height+" pixels":"fit content"}.`);
      }else{
        if(!g.preview)dragPreview(g);
        g.previewCard.style.transform=`translate3d(${g.rect.left+dx}px,${g.rect.top+dy}px,0)`;
        document.querySelectorAll(".dashboard-widget.drop-target").forEach(el=>el.classList.remove("drop-target"));
        const target=document.elementFromPoint(event.clientX,event.clientY)?.closest(".dashboard-widget");
        if(target && target!==g.el){g.to=draft.findIndex(row=>row.id===target.dataset.widget);target.classList.add("drop-target");announce(`Move to position ${g.to+1}.`);}
        // The page scrolls while a handle is held near its top/bottom edge.
        if(event.clientY>innerHeight-70 || event.clientY<90){const delta=event.clientY>innerHeight-70?18:-18;document.querySelector(".main")?.scrollBy(0,delta);window.scrollBy(0,delta);}
      }
    });
    function endGesture(){
      if(!gesture)return;const g=gesture;clearGesture();
      if(g.cancel || !g.moved){editor();return;}
      if(g.resize)change(()=>{const item=draft.find(item=>item.id===g.id);item.width=g.width;item.height=g.height;},"Widget resized.");
      else move(g.id,g.to);
      document.querySelector(`[data-dash-drag="${g.id}"]`)?.focus({preventScroll:true});
    }
    document.addEventListener("pointerup",endGesture);
    const cancelGesture=()=>{if(gesture)gesture.cancel=true;endGesture();};
    document.addEventListener("pointercancel",cancelGesture);
    document.addEventListener("lostpointercapture",event=>{if(gesture?.pointerId===event.pointerId)cancelGesture();});
    window.addEventListener("blur",cancelGesture);
    window.addEventListener("resize",()=>{if(matchMedia("(max-width:900px)").matches)cancelGesture();});
    window.addEventListener("beforeunload",event=>{if(dirty()){event.preventDefault();event.returnValue="";}});
  }
  return apiObject;
})();
if (typeof window !== "undefined") window.Dashboard = Dashboard;
if (typeof module !== "undefined") module.exports = Dashboard;
