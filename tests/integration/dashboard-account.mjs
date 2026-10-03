// Isolated browser contexts share only the account API fixture, not local storage.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176';
const endpoint='/api/auth/preferences/dashboard', records=new Map();let revision=0, unavailable=false;
const browser=await chromium.launch({headless:true});const contexts=[],errors=[];
const legacy={version:1,items:[{id:'portal',width:6,height:0}]};
async function open(user,old=null){
 const context=await browser.newContext({viewport:{width:1440,height:1000}});contexts.push(context);
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.exposeFunction('accountRequest',async body=>{
   if(unavailable)return {error:'Preferences service unavailable'};
   const current=records.get(user)||{revision:null,layout:null};
   if(body){
     assert.deepEqual(Object.keys(body).sort(),['layout','revision']);
     if(body.revision!==current.revision)return {error:'Your dashboard changed in another session. Cancel and reopen the editor.'};
     records.set(user,{revision:String(++revision),layout:body.layout});
   }
   return structuredClone(records.get(user)||current);
 });
 await page.goto(`${base}/?demo=1`);await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
 await page.evaluate(async({user,old,endpoint})=>{
   clearInterval(window.__loopTimer);ME=user;Dashboard.invalidate();
   if(old)localStorage.setItem(`homestead.dashboard.v1.${encodeURIComponent(user)}`,JSON.stringify(old));
   const original=window.api;
   window.api=async(path,opts={})=>{
     if(path!==endpoint)return original(path,opts);
     const answer=await accountRequest(opts.method==='POST'?JSON.parse(opts.body):null);
     if(answer.error)throw new Error(answer.error);return answer;
   };
   resetPaint();await viewDash();
 },{user,old,endpoint});return page;
}
const order=page=>page.locator('.dashboard-widget').evaluateAll(nodes=>nodes.map(n=>n.dataset.widget));
try{
 const a=await open('alice',legacy);assert.deepEqual(await order(a),['portal']);
 assert.deepEqual(records.get('alice').layout,legacy,'legacy browser layout imported once');
 assert.equal(await a.evaluate(()=>localStorage.getItem('homestead.dashboard.v1.alice')),null);
 const b=await open('alice');assert.deepEqual(await order(b),['portal'],'new browser receives the account layout');
 await a.getByRole('button',{name:'Edit dashboard',exact:true}).click();await b.getByRole('button',{name:'Edit dashboard',exact:true}).click();
 await a.getByRole('button',{name:'Add Compute',exact:true}).click();await a.getByRole('button',{name:'Save layout',exact:true}).click();await a.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
 await b.getByRole('button',{name:'Add Storage',exact:true}).click();await b.getByRole('button',{name:'Save layout',exact:true}).click();
 await b.getByRole('button',{name:'Save layout',exact:true}).waitFor();assert.equal(await b.evaluate(()=>Dashboard.dirty()),true,'stale editor keeps its draft');
 assert.deepEqual(records.get('alice').layout.items.map(x=>x.id),['portal','compute'],'stale session cannot overwrite the winner');
 await b.getByRole('button',{name:'Cancel',exact:true}).click();await b.locator('.askdlg [data-a="yes"]').click();await b.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();assert.deepEqual(await order(b),['portal','compute']);
 const c=await open('bob');assert.equal((await order(c)).includes('portal'),false,'accounts remain separate');
 const d=await open('alice',{version:1,items:[]});assert.deepEqual(await order(d),['portal','compute'],'old browser cannot replace an existing account layout');
 unavailable=true;await d.evaluate(()=>viewDash());await d.getByRole('button',{name:'Edit dashboard',exact:true}).click();assert.equal(await d.evaluate(()=>Dashboard.editing()),false,'unavailable reads cannot create a stale edit baseline');
 unavailable=false;await d.getByRole('button',{name:'Edit dashboard',exact:true}).click();await d.getByRole('button',{name:'Save layout',exact:true}).waitFor();
 assert.equal(await d.evaluate(()=>Dashboard.editing()),true);
 assert.deepEqual(errors,[]);console.log('Dashboard account sync passed: independent browsers, migration, account isolation, concurrent edits and reconnect.');
}finally{for(const context of contexts)await context.close();await browser.close();}
