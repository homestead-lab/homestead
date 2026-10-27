// Exercise the real dialog dismissal paths of the file manager: every way out
// stops the helper it opened, once, and late file API answers never reopen it.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {openAuditPage} from '../../scripts/audit_ready.mjs';
const base = process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173';
const browser = await chromium.launch({headless:true});
try {
  for (const width of [1440, 375]) {
    const page = await browser.newPage({viewport:{width,height:1000}}), errors=[];
    page.on('pageerror', error=>errors.push(error.message));
    await openAuditPage(page, `${base}/?demo=1`);
    await page.evaluate(()=>{
      clearInterval(window.__loopTimer);
      window.filesRequests=[];
      const original=window.api;
      window.api=async(path, options)=>{
        if (!path.startsWith('/api/files/')) return original(path, options);
        filesRequests.push({path, options});
        if (path==='/api/files/close') return {ok:true, stopped:true};
        if (path==='/api/files/keepalive') return {ok:true, running:true};
        const listing={path:'',entries:[{name:'config',kind:'dir',size:0,mode:'755',user:'app',group:'app',modified:0}],truncated:false};
        if (window.filesDelay) return new Promise(resolve=>window.filesFinish=()=>resolve(listing));
        return listing;
      };
    });
    const open=async(name='data')=>{
      await page.evaluate(name=>fileManager('lab',name,false),name);
      await page.locator('#fm_pane0 .fm-row[data-name="config"]').waitFor();
    };
    // One helper per claim: both panes start on the same volume.
    const closes=()=>page.evaluate(()=>filesRequests.filter(x=>x.path==='/api/files/close').length);
    for (const action of ['button','x','escape','navigation','replacement','back','pagehide']) {
      await open(); const before=await closes();
      if (action==='button') await page.locator('#fm .fm-foot button',{hasText:'Close'}).click();
      if (action==='x') await page.locator('#mclose').click();
      if (action==='escape') await page.keyboard.press('Escape');
      if (action==='navigation') await page.evaluate(()=>go('nodes'));
      if (action==='replacement') await page.evaluate(()=>modal('Another dialog','Other content'));
      if (action==='back') await page.evaluate(()=>modalBack());
      if (action==='pagehide') await page.evaluate(()=>window.dispatchEvent(new Event('pagehide')));
      await page.waitForFunction(count=>filesRequests.filter(x=>x.path==='/api/files/close').length===count+1,before);
      if(action==='pagehide') assert.equal(await page.evaluate(()=>filesRequests.at(-1).options.keepalive),true);
      await page.evaluate(()=>closeModal());
      assert.equal(await closes(),before+1,`${action} releases one helper at ${width}px, once`);
    }

    // A listing that answers after the manager closed does not reopen it.
    await page.evaluate(()=>{window.filesDelay=true;fileManager('lab','late',false);});
    await page.waitForFunction(()=>typeof filesFinish==='function');
    await page.locator('#mclose').click();
    const late=await closes();
    await page.evaluate(()=>{window.filesDelay=false;filesFinish();});
    await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,50)));
    assert.equal(await closes(),late);
    assert.equal(await page.locator('#modal').evaluate(n=>n.classList.contains('hidden')),true);

    // Cleanup stays on the original linked cluster even after its selection changes.
    await page.evaluate(()=>{FLEET.view={self:'here'};FLEET.target='there';});
    await open('remote-data');
    await page.evaluate(()=>{FLEET.target='elsewhere';closeModal();});
    await page.waitForFunction(()=>filesRequests.at(-1).path==='/api/files/close');
    assert.equal(await page.evaluate(()=>filesRequests.at(-1).options.headers['X-Homestead-Cluster']),'there');
    assert.deepEqual(errors,[]);
    console.log(`File manager cleanup passed at ${width}px`);
    await page.close();
  }
} finally { await browser.close(); }
