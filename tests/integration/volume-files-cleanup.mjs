// Exercise the real dialog dismissal paths and late file API responses.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
const base = process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173';
const browser = await chromium.launch({headless:true});
try {
  for (const width of [1440, 375]) {
    const page = await browser.newPage({viewport:{width,height:1000}}), errors=[];
    page.on('pageerror', error=>errors.push(error.message));
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(()=>typeof volumeFiles==='function' && ME && document.querySelector('#gate').classList.contains('hidden'));
    await page.evaluate(()=>{
      clearInterval(window.__loopTimer);
      window.filesRequests=[];
      const original=window.api;
      window.api=async(path, options)=>{
        if (!path.startsWith('/api/files/')) return original(path, options);
        filesRequests.push({path, options});
        if (path==='/api/files/close') {
          if(window.filesCloseDelay) return new Promise(resolve=>window.filesCloseFinish=()=>resolve({ok:true}));
          return {ok:true};
        }
        const listing={path:'',entries:[{name:'config',kind:'dir'}],truncated:false};
        if (window.filesDelay) return new Promise(resolve=>window.filesFinish=()=>resolve(listing));
        return listing;
      };
    });
    const open=async(name='data')=>{
      await page.evaluate(name=>volumeFiles('lab',name,false),name);
      await page.locator('.filelist').waitFor();
    };
    const closes=()=>page.evaluate(()=>filesRequests.filter(x=>x.path==='/api/files/close').length);
    for (const action of ['button','x','escape','navigation','replacement','back','pagehide']) {
      await open(); const before=await closes();
      if (action==='button') await page.getByRole('button',{name:'Close browser',exact:true}).click();
      if (action==='x') await page.locator('#mclose').click();
      if (action==='escape') await page.keyboard.press('Escape');
      if (action==='navigation') await page.evaluate(()=>go('nodes'));
      if (action==='replacement') await page.evaluate(()=>modal('Another dialog','Other content'));
      if (action==='back') await page.evaluate(()=>modalBack());
      if (action==='pagehide') await page.evaluate(()=>window.dispatchEvent(new Event('pagehide')));
      assert.equal(await closes(),before+1,`${action} releases one helper at ${width}px`);
      if(action==='pagehide') assert.equal(await page.evaluate(()=>filesRequests.at(-1).options.keepalive),true);
      await page.evaluate(()=>closeModal());
      assert.equal(await closes(),before+1,'closing again is idempotent');
    }
    // The dirty editor still asks before releasing its volume.
    await open(); const before=await closes();
    await page.evaluate(()=>{FILEVIEW.file='config.json';fileTouched();});
    await page.locator('#mclose').click();
    await page.locator('.askdlg [data-a="no"]').click();
    assert.equal(await closes(),before);
    assert.equal(await page.locator('#modal').evaluate(n=>n.classList.contains('hidden')),false);
    await page.locator('#mclose').click();
    await page.locator('.askdlg [data-a="yes"]').click();
    assert.equal(await closes(),before+1);

    // A pod can finish starting after the close request reached the server.
    await page.evaluate(()=>{window.filesDelay=true;volumeFiles('lab','late',false);});
    await page.waitForFunction(()=>typeof filesFinish==='function');
    await page.locator('#mclose').click();
    const late=await closes();
    await page.evaluate(()=>filesFinish());
    await page.waitForFunction(count=>filesRequests.filter(x=>x.path==='/api/files/close').length===count+1,late);
    assert.equal(await page.locator('#modal').evaluate(n=>n.classList.contains('hidden')),true);

    // A newer browser for the same claim owns the shared helper; stale replies
    // must not delete it or overwrite its new dialog.
    await page.evaluate(()=>{window.filesFinish=null;volumeFiles('lab','same',false);});
    await page.waitForFunction(()=>typeof filesFinish==='function');
    await page.evaluate(()=>{window.oldFilesFinish=filesFinish;window.filesDelay=false;});
    await open('same'); const reopened=await closes();
    await page.evaluate(()=>oldFilesFinish());
    await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,0)));
    assert.equal(await closes(),reopened);
    assert.equal(await page.locator('#mtitle').textContent(),'Files · same');
    await page.evaluate(()=>closeModal());

    // Finishing the old deletion before opening again avoids deleting a helper
    // just reused by the new browser for the same claim.
    await open('reopen');
    await page.evaluate(()=>{
      window.filesCloseDelay=true;
      window.filesReopening=volumeFiles('lab','reopen',false);
    });
    assert.equal(await page.evaluate(()=>filesRequests.at(-1).path),'/api/files/close');
    await page.evaluate(async()=>{
      window.filesCloseDelay=false;filesCloseFinish();await filesReopening;
    });
    await page.locator('.filelist').waitFor();
    await page.evaluate(()=>closeModal());

    // Cleanup stays on the original linked cluster even after its selection changes.
    await page.evaluate(()=>{FLEET.view={self:'here'};FLEET.target='there';});
    await open('remote-data');
    await page.evaluate(()=>{FLEET.target='elsewhere';closeModal();});
    assert.equal(await page.evaluate(()=>filesRequests.at(-1).options.headers['X-Homestead-Cluster']),'there');
    assert.deepEqual(errors,[]);
    console.log(`File browser cleanup passed at ${width}px`);
    await page.close();
  }
} finally { await browser.close(); }
