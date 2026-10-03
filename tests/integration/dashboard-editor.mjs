import assert from "node:assert/strict";
import {chromium} from "playwright";
import {mkdir} from "node:fs/promises";
const base=process.env.HOMESTEAD_URL || "http://127.0.0.1:4176";
const output=process.env.DASHBOARD_EDITOR_SCREENSHOTS || "release-assets/dashboard-editor";
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true});
try{
 for(const width of [1440,390,320]) for(const theme of ["dark","light"]){
  const context=await browser.newContext({viewport:{width,height:1000}}),page=await context.newPage(),errors=[];
  page.on("pageerror",e=>errors.push(e.message));
  await page.goto(`${base}/?demo=1`);
  await page.locator("#historyCard .spark").first().waitFor();
  await page.evaluate(theme=>{clearInterval(window.__loopTimer);SET.theme=theme;applySettings();},theme);
  const order=()=>page.locator('.dashboard-widget').evaluateAll(els=>els.map(el=>el.dataset.widget));
  const initial=await order();assert.equal(initial.length,7);
  if(width<=900){
    assert.equal(await page.getByRole('button',{name:'Edit dashboard',exact:true}).isVisible(),false);
    await page.evaluate(()=>Dashboard.start());
    assert.equal(await page.evaluate(()=>Dashboard.editing()),false,'phone cannot enter the desktop editor');
    assert.ok((await page.locator('.dashboard-widget').first().boundingBox()).y<160,'widgets start near the top');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    await page.screenshot({path:`${output}/saved-${theme}-${width}.png`,fullPage:true});
    // A desktop draft survives resizing without exposing desktop layout controls.
    await page.setViewportSize({width:1440,height:1000});
    await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
    await page.getByRole('button',{name:'Add Portal links',exact:true}).click();
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.locator('.dashboard-edit-layout').isVisible(),false);
    assert.equal(await page.getByRole('button',{name:'Phone preview',exact:true}).isVisible(),false);
    assert.equal(await page.evaluate(()=>Dashboard.dirty()),true);
    await page.setViewportSize({width:1440,height:1000});
    assert.equal(await page.locator('[data-widget="portal"]').count(),1);
    await page.getByRole('button',{name:'Cancel',exact:true}).click();await page.locator('.askdlg [data-a="yes"]').click();
    assert.deepEqual(errors,[]);console.log(`Mobile dashboard passed: ${width}px ${theme}`);await context.close();continue;
  }
  await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
  if(width<=1100)await page.locator('.dashboard-library-toggle').click();
  await page.getByRole('button',{name:'Add Portal links',exact:true}).click();
  await page.locator('#dashboardPortal .portal-tile').first().waitFor();
  await page.locator('[data-dash-drag="portal"]').focus();await page.keyboard.press('Home');
  assert.equal((await order())[0],'portal');
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.dashDrag),'portal');
  await page.getByLabel('Widget width',{exact:true}).selectOption('12');
  await page.getByLabel('Widget height',{exact:true}).selectOption('520');
  await page.getByRole('button',{name:'Undo',exact:true}).click();
  assert.equal(await page.getByLabel('Widget height',{exact:true}).inputValue(),'0');
  await page.getByRole('button',{name:'Redo',exact:true}).click();
  assert.equal(await page.getByLabel('Widget height',{exact:true}).inputValue(),'520');
  // Background updates must not replace controls or lose the draft.
  await page.evaluate(()=>{window.savedEditor=document.querySelector('.dashboard-edit-layout');go('dash');return viewDash();});
  assert.equal(await page.evaluate(()=>savedEditor===document.querySelector('.dashboard-edit-layout')),true);
  if(width===1440){
    // Pointer drag moves a card to another grid position, including backwards.
    const grip=page.locator('[data-dash-drag="compute"]');await grip.scrollIntoViewIfNeeded();
    const from=await grip.boundingBox(),target=await page.locator('[data-dash-drag="throughput"]').boundingBox();
    const sourceBox=await page.locator('[data-widget="compute"]').boundingBox();
    await page.mouse.move(from.x+15,from.y+15);await page.mouse.down();
    assert.equal(await page.locator('.dashboard-drag-preview').count(),0,'a click does not create a preview');
    await page.mouse.move(target.x+20,target.y+15,{steps:10});
    const preview=page.locator('.dashboard-drag-preview'), previewCard=preview.locator('[data-widget="compute"]');
    assert.equal(await preview.getAttribute('aria-hidden'),'true');
    assert.equal(await preview.evaluate(el=>el.inert),true);
    assert.ok(Number(await preview.evaluate(el=>getComputedStyle(el).opacity))<1);
    const previewBox=await previewCard.boundingBox();
    assert.ok(Math.abs(previewBox.width-sourceBox.width)<1,'preview preserves card width');
    assert.ok(Math.abs(previewBox.x-(sourceBox.x+target.x+20-from.x-15))<1,'preview follows pointer with grab offset');
    assert.equal(await page.locator('.dashboard-widget.drop-target').count(),1,'preview does not intercept the target');
    assert.equal(await preview.locator('[id]').evaluateAll(els=>els.every(el=>document.querySelectorAll(`[id="${el.id}"]`).length===1)),true,'preview IDs are unique');
    await page.screenshot({path:`${output}/drag-preview-${theme}.png`});
    await page.mouse.up();
    assert.equal(await preview.count(),0,'drop removes the preview');
    assert.ok((await order()).indexOf('compute')>(await order()).indexOf('throughput'));
    const afterDrop=await order(),cancelGrip=page.locator('[data-dash-drag="compute"]');
    await cancelGrip.scrollIntoViewIfNeeded();const cancelBox=await cancelGrip.boundingBox();
    await page.mouse.move(cancelBox.x+15,cancelBox.y+15);await page.mouse.down();
    await page.mouse.move(cancelBox.x+80,cancelBox.y+55,{steps:5});
    assert.equal(await preview.count(),1);
    await page.keyboard.press('Escape');await page.mouse.up();
    assert.equal(await preview.count(),0,'Escape removes the preview');assert.deepEqual(await order(),afterDrop,'cancel preserves order');
    await page.mouse.move(cancelBox.x+15,cancelBox.y+15);await page.mouse.down();
    await page.mouse.move(cancelBox.x+80,cancelBox.y+55,{steps:5});
    await page.evaluate(()=>window.dispatchEvent(new Event('blur')));await page.mouse.up();
    assert.equal(await preview.count(),0,'losing window focus removes the preview');assert.deepEqual(await order(),afterDrop);
    await page.locator('[data-dash-resize="compute"]').focus();await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('aria-label')),'Widget width');
    const handle=page.locator('[data-dash-resize="compute"]');await handle.scrollIntoViewIfNeeded();const box=await handle.boundingBox();
    await page.mouse.move(box.x+16,box.y+16);await page.mouse.down();await page.mouse.move(box.x+210,box.y+100,{steps:12});await page.mouse.up();
    assert.notEqual(await page.locator('[data-widget="compute"]').evaluate(el=>el.style.getPropertyValue('--widget-span')),'4');
    await page.getByRole('button',{name:'Phone preview',exact:true}).click();
    await page.screenshot({path:`${output}/phone-preview-${theme}.png`,fullPage:true});
    assert.ok((await page.locator('.dashboard-phone').boundingBox()).width<=390);
    await page.getByRole('button',{name:'Desktop canvas',exact:true}).click();
  }
  if(width<=1100)await page.locator('.dashboard-library-toggle').click();
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:`${output}/editor-${theme}-${width}.png`,fullPage:true});
  await page.screenshot({path:`${output}/editor-screen-${theme}-${width}.png`});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'editor fits viewport');
  const expected=await order();await page.getByRole('button',{name:'Save layout',exact:true}).click();
  await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
  assert.deepEqual(await order(),expected);
  await page.reload();await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();assert.deepEqual(await order(),expected);
  await page.screenshot({path:`${output}/saved-${theme}-${width}.png`,fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'saved layout fits viewport');
  await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
  await page.getByRole('button',{name:'Remove Portal links',exact:true}).click();
  await page.evaluate(()=>go('portal'));await page.locator('.askdlg [data-a="no"]').click();assert.equal(await page.evaluate(()=>STATE.view),'dash');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();await page.locator('.askdlg [data-a="yes"]').click();
  await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();assert.deepEqual(await order(),expected);
  // A conflicting session save is not silently overwritten, and server failure retains the draft.
  await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
  await page.evaluate(()=>{localStorage.setItem(`homestead.demo.dashboard.${ME}`,JSON.stringify({revision:"another-session",layout:{version:1,items:[]}}));});
  await page.getByRole('button',{name:'Save layout',exact:true}).click();
  assert.equal(await page.evaluate(()=>Dashboard.editing()),true);
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();assert.deepEqual(await order(),[]);
  await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();await page.getByRole('button',{name:'Reset layout',exact:true}).click();
  assert.deepEqual(await order(),initial);
  await page.getByRole('button',{name:'Save layout',exact:true}).click();await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
  if(width===1440 && theme==='dark'){
    await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
    await page.getByRole('button',{name:'Add Portal links',exact:true}).click();
    await page.evaluate(()=>{window.originalDashboardApi=window.api;window.api=async function(path,opts){if(path==='/api/auth/preferences/dashboard' && opts?.method==='POST')throw new Error('Server unavailable');return originalDashboardApi(path,opts);};});
    await page.getByRole('button',{name:'Save layout',exact:true}).click();assert.equal(await page.evaluate(()=>Dashboard.dirty()),true);
    await page.evaluate(()=>{window.api=originalDashboardApi;});
    await page.getByRole('button',{name:'Save layout',exact:true}).click();await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
    assert.equal((await order()).includes('portal'),true);
    await page.evaluate(async()=>{window.originalUser=ME;Dashboard.invalidate();ME='another-user';resetPaint();await viewDash();});
    assert.deepEqual(await order(),initial,'another account gets its own layout');
    await page.evaluate(async()=>{ME=originalUser;resetPaint();await viewDash();});assert.equal((await order()).includes('portal'),true);
    await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
    await page.evaluate(()=>Dashboard.invalidate());assert.equal(await page.evaluate(()=>Dashboard.editing()),false,'sign-out clears editing state');
  }
  assert.deepEqual(errors,[]);console.log(`Dashboard editor passed: ${width}px ${theme}`);await context.close();
 }
}finally{await browser.close();}
