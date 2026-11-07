// The OS updates dialog in a real browser (#1174): the real DOM, focus, dialog
// stack and role switch that tests/os-updates-hosts-view.test.js can't have.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {chromium} from 'playwright';

const base = process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173';
const screenshots = process.env.OS_UPDATES_SCREENSHOTS;
if (screenshots) await fs.mkdir(screenshots, {recursive:true});
const demo = await fs.readFile(new URL('../../web/js/demo.js', import.meta.url), 'utf8');
const browser = await chromium.launch({headless:true});
const row = (page, name) => page.locator('#osu_hosts tbody tr', {has:page.locator(`text=${name}`)});
const noSideways = page => page.evaluate(() => [document.documentElement.scrollWidth, innerWidth,
  document.querySelector('.modalbox').scrollWidth, document.querySelector('.modalbox').clientWidth]);
try {
  for (const width of [1280, 400]) {
    const context = await browser.newContext({viewport:{width, height:900}}), page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => { window.__osuNativeFetch = window.fetch.bind(window); });
    await page.route('**/js/demo.js*', route => route.fulfill({contentType:'application/javascript', body:demo + `
      (()=>{const demoFetch=window.fetch;window.fetch=(url,opts)=>
        window.__osuRealFetch && typeof url==='string' && url.startsWith('/api/os-updates/start')
          ? window.__osuNativeFetch(url,opts) : demoFetch(url,opts);})();
    `}));
    const open = async () => {
      await page.evaluate(() => { window.__demoOsRun = null; closeModal(); osUpdates(); });
      await page.waitForSelector('#osu_hosts tbody tr');
    };
    const shot = async name => {
      if (!screenshots) return;
      await page.waitForTimeout(400);   // the dialog's fade
      await page.screenshot({path:`${screenshots}/${width}-${name}.png`});
    };
    const sideways = async what => {
      const [page_, inner, box, client] = await noSideways(page);
      assert.ok(page_ <= inner, `${what}: the page scrolls sideways at ${width}`);
      assert.ok(box <= client, `${what}: the dialog scrolls sideways at ${width}`);
    };
    try {
      await page.goto(`${base}/?demo=1`, {waitUntil:'load'});
      await page.waitForSelector('.dashboard-widget');
      await page.evaluate(() => clearInterval(window.__loopTimer));

      await open();
      await shot('rows');
      await sideways('rows');
      assert.equal(await row(page, 'harvester-node3').getByRole('button', {name:'Update…'}).count(), 1);
      assert.equal(await row(page, 'harvester-node2').getByRole('button', {name:'Restart…'}).count(), 1);
      assert.equal(await row(page, 'harvester-node2').locator('[data-osu-pick]').count(), 0, 'a restart alone is not ticked');
      assert.equal(await page.locator('#osu_picked').isVisible(), false, 'nothing ticked');

      // Several ticked, one review; Back keeps the ticks.
      await row(page, 'harvester-node1').locator('[data-osu-pick]').check();
      await row(page, 'harvester-node3').locator('[data-osu-pick]').check();
      assert.equal(await page.locator('#osu_picked').innerText(), 'Update 2 selected hosts…');
      await page.locator('#osu_picked').click();
      await page.waitForSelector('#osu_review_go');
      await shot('review-several');
      await sideways('the combined review');
      const text = await page.locator('#mbody').innerText();
      assert.ok(text.indexOf('harvester-node3') < text.indexOf('harvester-node1'), "Homestead's own host goes last");
      assert.match(text, /goes last/);
      assert.equal(await page.locator('#mbody').getByRole('button', {name:/^Review|Reboot|Restart/}).count(), 0, 'no restart from the review');
      await page.locator('#mbody').getByRole('button', {name:'Cancel'}).click();
      await page.waitForSelector('#osu_hosts tbody tr');
      assert.equal(await row(page, 'harvester-node3').locator('[data-osu-pick]').isChecked(), true, 'Back keeps the ticks');
      assert.equal(await page.locator('#osu_picked').isVisible(), true);

      // "Do not restart" takes the restart checks off the screen.
      await page.locator('#osu_picked').click();
      await page.waitForSelector('#osu_review_go');
      await page.locator('#osu_review_restart').selectOption('never');
      assert.equal(await page.locator('.osu-verdict').first().isVisible(), false);
      await page.locator('#osu_review_restart').selectOption('when-needed');
      assert.equal(await page.locator('.osu-verdict').first().isVisible(), true);

      // A 409 over real fetch, through the real api() and reviewFlow: the review
      // redraws in place and keeps the choice. The demo transport hands this one
      // call to the browser's own fetch; page.route answers it as the route would.
      await open();
      await page.evaluate(() => { window.__osuRealFetch = true; });
      await page.route('**/api/os-updates/start', route => route.fulfill({status:409, contentType:'application/json',
        body:JSON.stringify({error:'Something changed since this was reviewed; review it again', preview:{}})}));
      await row(page, 'harvester-node3').getByRole('button', {name:'Update…'}).click();
      await page.waitForSelector('#osu_review_go');
      await page.locator('#osu_review_restart').selectOption('never');
      const depth = await page.locator('#mback').innerText();
      const [reply] = await Promise.all([page.waitForResponse('**/api/os-updates/start'), page.locator('#osu_review_go').click()]);
      assert.equal(reply.status(), 409);
      await page.waitForFunction(() => !document.querySelector('#osuReviewLoading') && document.querySelector('#osu_review_go')?.textContent === 'Update harvester-node3');
      assert.equal(await page.locator('#osu_review_restart').inputValue(), 'never', 'the choice survives the redraw');
      assert.equal(await page.locator('#mback').innerText(), depth, 'the same dialog, not another on top');
      await page.unroute('**/api/os-updates/start');
      await page.evaluate(() => { window.__osuRealFetch = false; });

      // One host's row: reviewed, started, and then the rows follow the run.
      await open();
      await row(page, 'harvester-node3').getByRole('button', {name:'Update…'}).click();
      await page.waitForSelector('#osu_review_go');
      await shot('review-one');
      await sideways('the review');
      await page.locator('#osu_review_go').click();
      await page.waitForFunction(() => /updating now/.test(document.querySelector('#osu_hosts')?.innerText || ''));
      await shot('running');
      assert.match(await row(page, 'harvester-node3').innerText(), /updating now/);
      assert.equal(await page.locator('#osu_hosts [data-osu-pick]').count(), 0);
      assert.equal(await page.locator('#osu_hosts button.btn').count(), 0, 'no row offers anything during a run');
      assert.equal(await page.locator('#osu_run').getByRole('button').count(), 0, 'a run of one has nothing to stop after');
      assert.match(await page.locator('.ui-progress-head').first().innerText(), /Updating harvester-node3/);

      // A row's Restart… is the Host actions review, and Back comes home.
      await open();
      await row(page, 'harvester-node2').getByRole('button', {name:'Restart…'}).click();
      await page.waitForSelector('#pw_confirm');
      // The title reads "Reboot · harvester-node2"; the host is drawn large in the header under it.
      assert.match(await page.locator('#mtitle').textContent(), /harvester-node2/);
      assert.match(await page.locator('#msubject').innerText(), /harvester-node2/);
      await page.locator('#mback').click();
      await page.waitForSelector('#osu_hosts tbody tr');

      // The name goes to the host's page.
      await row(page, 'harvester-node3').locator('button.linkish').click();
      await page.waitForFunction(() => new URLSearchParams(location.search).get('node') === 'harvester-node3');
      assert.equal(await page.locator('#modal').isVisible(), false);
      await page.waitForSelector('#nodeHostOs', {state:'attached'});

      // A viewer keeps the table and the links, and none of the controls.
      await open();
      await page.evaluate(() => { ROLE = 'viewer'; applyRole(); });
      assert.equal(await row(page, 'harvester-node3').locator('button.btn:visible').count(), 0);
      assert.equal(await row(page, 'harvester-node3').locator('[data-osu-pick]').isVisible(), false);
      assert.equal(await row(page, 'harvester-node3').locator('button.linkish').isVisible(), true);
      await shot('viewer');
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  }
  console.log('OS updates dialog: rows, ticks, review, run, Restart…, link and viewer checked at 1280 and 400 px');
} finally { await browser.close(); }
