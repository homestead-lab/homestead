import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176',out=process.env.CUSTOM_WIDGET_SCREENSHOTS || 'release-assets/custom-widget';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
const attack=`<script>parent.__customEscaped=true;fetch('/api/security-test')</script><img src="/api/security-test" onerror="parent.__customEscaped=true"><svg><a href="javascript:alert(1)">bad</a></svg><math><mtext><img src="https://security-test.invalid/leak"></mtext></math><iframe src="/api/security-test" srcdoc="<script>parent.alert(1)</script>"></iframe><meta http-equiv="refresh" content="0;url=/api/security-test"><base href="https://security-test.invalid"><form action="/api/security-test"><input name="secret"><button>Submit</button></form><a href="javascript:parent.__customEscaped=true" target="_top" ping="/api/security-test">Link label</a><div id="app" onclick="parent.__customEscaped=true" style="position:fixed;background-image:url(/api/security-test);color:lime">Safe label</div><style>@import '/api/security-test';</style>`;
try{for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*security-test*',r=>{requests.push(r.request().url());return r.abort();});
 // Exercise srcdoc with Homestead's production parent policy too.
 await page.route(`${base}/?demo=1`,async route=>{const response=await route.fetch();await route.fulfill({response,headers:{...response.headers(),'content-security-policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; img-src 'self' data: blob: https:; connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'; object-src 'none'"}});});
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="compute"]').waitFor();
 await page.evaluate(theme=>{clearInterval(window.__loopTimer);SET.theme=theme;applySettings();},theme);
 await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
 await page.getByRole('button',{name:'Add Custom text / HTML',exact:true}).click();
 await page.getByLabel('Custom widget title',{exact:true}).fill('My service notes');
 await page.getByLabel('Custom widget content',{exact:true}).fill('<b>This is plain text</b>');
 await page.getByRole('button',{name:'Update preview',exact:true}).click();
 assert.equal(await page.locator('[data-widget="custom"] .dashboard-custom-text').textContent(),'<b>This is plain text</b>');
 assert.equal(await page.locator('[data-widget="custom"] .dashboard-custom-text b').count(),0);
 await page.evaluate(()=>{
  const probes=['<math><mtext><table><mglyph><style><!--</style><img title="--><img src=/api/security-test onerror=alert(1)>">','<svg><foreignObject><p onclick="alert(1)">x</p></foreignObject></svg>','<div>'.repeat(2000)+'end'+'</div>'.repeat(2000),'<p style="background:url(/api/security-test);color:var(--attack)">x</p>'];
  for(const source of probes){const output=DashboardCustom.sanitize(source);if(/<(?:img|svg|math|script|style)\b|onerror|onclick|url\(/i.test(output))throw new Error('Unsafe sanitizer result');}
 });
 await page.getByLabel('Custom widget format',{exact:true}).selectOption('html');
 await page.getByLabel('Custom widget content',{exact:true}).fill(attack);
 await page.getByRole('button',{name:'Update preview',exact:true}).click();
 const iframe=page.locator('[data-widget="custom"] iframe');
 assert.equal(await iframe.getAttribute('sandbox'),'');assert.equal(await iframe.getAttribute('referrerpolicy'),'no-referrer');
 let frame=await (await iframe.elementHandle()).contentFrame();await frame.getByText('Safe label',{exact:true}).waitFor();
 assert.equal(await frame.locator('script,img,svg,math,iframe,form,input,button,a,base,link,style:not(head style)').count(),0);
 assert.equal(await frame.locator('[onclick],[id],[src],[href]').count(),0);
 assert.equal(await frame.getByText('Safe label',{exact:true}).evaluate(el=>el.style.position),'');
 assert.equal(await frame.evaluate(()=>{try{parent.document;return false;}catch{return true;}}),true,'opaque origin prevents parent access');
 assert.equal(await frame.evaluate(()=>{try{localStorage.getItem('session');return false;}catch{return true;}}),true,'opaque origin prevents storage access');
 assert.equal(await page.evaluate(()=>window.__customEscaped),undefined);
 // Even if a future sanitizer regression lets an image/script through, the
 // frame policy still denies requests and the sandbox denies execution.
 await frame.evaluate(()=>{const img=document.createElement('img');img.src='/api/security-test';document.body.append(img);const script=document.createElement('script');script.textContent='window.__ran=true';document.body.append(script);});
 assert.equal(await frame.evaluate(()=>window.__ran),undefined);
 await page.getByLabel('Custom widget content',{exact:true}).fill('<h2 style="color:#63aef7">Home lab</h2><p>Backups run at <strong>02:00</strong>.</p><ul><li>Check failed jobs</li><li>Review disk health</li></ul><p><small>Maintenance: Sunday morning</small></p>');
 await page.getByRole('button',{name:'Update preview',exact:true}).click();
 await page.getByRole('button',{name:'Add Custom text / HTML',exact:true}).click();
 await page.getByLabel('Custom widget title',{exact:true}).fill('Shopping list');
 await page.getByLabel('Custom widget content',{exact:true}).fill('Replacement fan\nSpare Ethernet cable');
 await page.getByRole('button',{name:'Update preview',exact:true}).click();
 assert.equal(await page.locator('[data-widget="custom2"]').count(),1);
 await page.getByRole('button',{name:'Save layout',exact:true}).click();await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
 await page.reload();await page.locator('[data-widget="custom"] iframe').waitFor();
 const savedFrame=await (await page.locator('[data-widget="custom"] iframe').elementHandle()).contentFrame();await savedFrame.getByText('Home lab',{exact:true}).waitFor();assert.equal(await savedFrame.locator('h2').evaluate(el=>getComputedStyle(el).color),'rgb(99, 174, 247)');
 assert.equal(await page.locator('[data-widget="custom2"] .dashboard-custom-text').textContent(),'Replacement fan\nSpare Ethernet cable');
 for(const width of [1440,390,320]){
  await page.setViewportSize({width,height:1000});await page.addStyleTag({content:'*,*::before,*::after{transition:none!important}'});
  const card=page.locator('[data-widget="custom"]');await card.scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
  await card.screenshot({path:`${out}/html-${theme}-${width}.png`});
 }
 assert.deepEqual(requests,[],'custom content makes no network requests');assert.deepEqual(errors,[]);
 console.log(`Custom widget isolation, persistence and responsive rendering passed (${theme})`);await page.close();
}}finally{await browser.close();}
