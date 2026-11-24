// Volumes reads what it draws (#1211): a load and every refresh read the
// volumes, the totals, the forecast, old copies and other volumes, and not the
// storage classes, the V2 engine or Longhorn's capacity, which only dialogs
// use. Each of those dialogs reads its own when it opens, once, and a failed
// read is said in the dialog (#620).
import assert from "node:assert/strict";
import {chromium} from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4176";
const browser = await chromium.launch({headless: true});

const PAGE = ["/api/volumes", "/api/storage", "/api/storage/forecast", "/api/volumes/old-copies", "/api/volumes/other"];
const DIALOGS_ONLY = ["/api/storage/classes", "/api/storage/v2", "/api/longhorn/capacity"];

try {
  const context = await browser.newContext({viewport: {width: 1440, height: 900}});
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(`${base}/?demo=1`, {waitUntil: "load"});
  await page.waitForSelector(".dashboard-widget");
  await page.evaluate(() => {
    window.__loopTimer?.stop();  // the check drives each refresh itself
    window.__reads = [];
    window.__every = [];
    window.__fail = "";
    const real = window.api;
    window.api = (path, opts) => {
      const bare = String(path).split("?")[0];
      // A poll passes its own signal: the demo's busy volumes start one that reads
      // /api/volumes every 4 s (volumeProgressWatch). Those are not the load's or a
      // tick's reads, so they are kept out of the counts, but not out of __every.
      if (!opts?.method || opts.method === "GET") { window.__every.push(bare); if (!opts?.signal) window.__reads.push(bare); }
      return bare === window.__fail ? Promise.reject(new Error("timed out")) : real(path, opts);
    };
  });
  const reads = () => page.evaluate(() => window.__reads.splice(0));
  const every = () => page.evaluate(() => window.__every.splice(0));
  const settled = () => page.waitForFunction(() => !document.querySelector("#mbody .spin2"));
  // Only Volumes' own reads: the bell and the jobs strip run on timers of their own.
  const own = list => list.filter(path => [...PAGE, ...DIALOGS_ONLY].includes(path));

  await page.evaluate(() => go("storage"));
  await page.waitForSelector("table[data-sort=volumes] tbody tr");
  await page.waitForFunction(() => !document.querySelector("#views .spin2"));
  const load = own(await reads());

  // Two ticks of the refresh loop: what each one reads.
  const ticks = [];
  for (let i = 0; i < 2; i++) {
    assert.equal(await page.evaluate(() => refresh()), true, "the refresh ran");
    ticks.push(own(await reads()));
  }
  // However they are made, the load and the ticks never read the three.
  const all = await every();
  for (const path of DIALOGS_ONLY) assert.ok(!all.includes(path), `the load and the ticks do not read ${path}, by any route`);
  console.log(`Volumes reads: load ${load.length}, ticks ${ticks.map(t => t.length).join(" and ")} (${ticks[0].join(", ")})`);
  for (const [label, list] of [["the load", load], ["tick 1", ticks[0]], ["tick 2", ticks[1]]]) {
    assert.deepEqual([...list].sort(), [...PAGE].sort(), `${label} reads what the page draws, each once`);
    for (const path of DIALOGS_ONLY) assert.ok(!list.includes(path), `${label} does not read ${path}`);
  }

  // Change storage class: reads the classes when it opens, once, and offers them.
  await page.evaluate(() => volumeReclass(STATE.data.vols.find(v => v.pvc_name === "frigate-config")));
  await page.waitForSelector("#rc_to option", {state: "attached"});
  const options = await page.locator("#rc_to option").allInnerTexts();
  assert.ok(options.length >= 2, `the other classes are offered: ${options}`);
  assert.deepEqual(own(await reads()), ["/api/storage/classes"]);
  await page.evaluate(() => closeModal());

  // Unread classes: said with Try again, not "no other class".
  await page.evaluate(() => { window.__fail = "/api/storage/classes"; });
  await page.evaluate(() => volumeReclass(STATE.data.vols.find(v => v.pvc_name === "frigate-config")));
  await settled();
  const unread = await page.locator("#mbody").innerText();
  assert.match(unread, /The storage classes could not be read[\s\S]*timed out/);
  assert.match(unread, /Try again/);
  await page.evaluate(() => { window.__fail = ""; });
  await page.getByRole("button", {name: "Try again"}).click();
  await page.waitForSelector("#rc_to option", {state: "attached"});
  await page.evaluate(() => closeModal());
  await reads();

  // Add a disk: reads whether the V2 engine is on when it opens, once.
  await page.evaluate(() => diskAdd("harvester-node1"));
  await page.waitForSelector("#da_engine");
  assert.equal(await page.locator("#da_engine option[value=v2]").count(), 1, "the demo's V2 engine is on, so V2 is offered");
  assert.deepEqual(own(await reads()), ["/api/storage/v2"]);
  await page.evaluate(() => closeModal());

  // Unread V2 engine: V1 only, and said; never "the V2 engine is off".
  await page.evaluate(() => { window.__fail = "/api/storage/v2"; });
  await page.evaluate(() => diskAdd("harvester-node1"));
  await page.waitForSelector("#da_engine");
  assert.equal(await page.locator("#da_engine option[value=v2]").count(), 0);
  const disk = await page.locator("#mbody").innerText();
  assert.match(disk, /could not read whether the V2 engine is on, so only V1 is offered \(timed out\)/);
  assert.doesNotMatch(disk, /The V2 engine is off/);
  await page.evaluate(() => { window.__fail = ""; closeModal(); });
  await reads();

  // Setting up a disk and the system drive's free space read it with their own check.
  await page.evaluate(() => diskSetup("harvester-node3", "/dev/sdc"));
  await page.waitForSelector("#ds_engine");
  assert.deepEqual(own(await reads()), ["/api/storage/v2"]);
  await page.evaluate(() => closeModal());
  await page.evaluate(() => diskOsSpace("harvester-node1"));
  await page.waitForSelector("#os_engine");
  assert.deepEqual(own(await reads()), ["/api/storage/v2"]);
  await page.evaluate(() => closeModal());

  // New storage class opened with no classes known (from a V2 disk, or the setup guide):
  // it reads them, once, and says which class Homestead puts new volumes on.
  await reads();
  await page.evaluate(() => { STATE.data.storageClasses = null; return storageClassCreate(); });
  await page.waitForSelector("#mbody [data-homestead-class]", {state: "attached"});
  assert.match(await page.locator("#mbody [data-homestead-class]").innerText(), /Homestead puts new volumes on longhorn-r2/);
  assert.deepEqual(own(await reads()), ["/api/storage/classes"]);
  await page.evaluate(() => closeModal());

  assert.deepEqual(errors, []);
  console.log("volumes page reads: ok");
} finally {
  await browser.close();
}
