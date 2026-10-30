// Leaving a page cancels the reads it started (#705). Each read is held for
// 3 s and at most six run at once, as the browser allows one origin over
// HTTP/1.1, so a page's reads otherwise queue behind the page you left.
import assert from "node:assert/strict";
import {chromium} from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4176";
const browser = await chromium.launch({headless: true});
// A hung step fails the check instead of holding its CI group for hours.
const watchdog = setTimeout(() => { console.error("navigation-aborts-reads: no result after 180 s"); process.exit(1); }, 180000);

// A slow cluster in front of the demo's answers: every request is held 3 s,
// six at a time; one whose signal is aborted is dropped at once, queued or running.
const slowCluster = () => {
  const answer = window.fetch, reads = window.__reads = [];
  let running = 0;
  const waiting = [];
  const next = () => { const go = waiting.shift(); if (go) go(); else running--; };
  window.fetch = (input, init) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.origin);
    if (!url.pathname.startsWith("/api/")) return answer(input, init);
    const signal = init?.signal;
    const read = {path: url.pathname, method: init?.method || "GET", signal: !!signal, at: performance.now(), state: "waiting"};
    reads.push(read);
    return new Promise((resolve, reject) => {
      let timer = null, holding = false, queued = null;
      const stop = () => {
        clearTimeout(timer);
        if (queued) { const i = waiting.indexOf(queued); if (i >= 0) waiting.splice(i, 1); }
        if (holding) next();
        read.state = "aborted";
        reject(new DOMException("The user aborted a request.", "AbortError"));
      };
      if (signal?.aborted) return stop();
      signal?.addEventListener("abort", stop, {once: true});
      const start = () => {
        queued = null; holding = true; read.state = "running"; read.running = performance.now();
        timer = setTimeout(() => {
          signal?.removeEventListener("abort", stop);
          holding = false; next(); read.state = "answered"; read.answered = performance.now();
          resolve(answer(input, init));
        }, 3000);
      };
      if (running < 6) { running++; start(); } else { queued = start; waiting.push(queued); }
    });
  };
};

const vmsShown = page => page.waitForFunction(() => document.querySelector(".phead h2")?.innerText.trim() === "Virtual machines"
  && !document.querySelector("#views.refreshing"), null, {timeout: 30000});

try {
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage(), errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(`${base}/?demo=1`);
  await page.waitForSelector(".dashboard-widget");
  // Only the reads this check makes: no refresh tick, jobs tray or alerts poll.
  await page.evaluate(() => { clearInterval(window.__loopTimer); clearTimeout(window.__operationTimer); window.stopAlertChecks?.(); });
  await page.evaluate(slowCluster);

  // Started before leaving, and not the page's: a read marked `keep` (the jobs
  // tray's), a read with its own signal, and a write.
  await page.evaluate(() => {
    window.__kept = api("/api/operations", {keep: true}).then(() => "answered", e => e.name);
    api("/api/nodes", {signal: new AbortController().signal});
    window.__write = api("/api/auth/preferences/dashboard", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({revision: null, layout: null})}).then(() => "answered", e => e.message);
  });
  const first = await page.evaluate(() => window.__reads.length);
  // Through Hosts and Volumes to Virtual machines, a moment on each.
  await page.evaluate(() => go("nodes"));
  await page.waitForTimeout(400);
  // Cluster, until its health card is reading: those reads are the page's
  // too, and leaving cancels them (#987).
  const health = await page.evaluate(() => { go("cluster"); return window.__reads.length; });
  await page.waitForFunction(at => window.__reads.slice(at).some(r => r.path === "/api/self/health"), health, {timeout: 15000});
  await page.evaluate(() => { window.__leftCluster = performance.now(); go("storage"); });
  await page.waitForTimeout(400);
  const left = await page.evaluate(() => { const before = window.__reads.length; window.__arrived = performance.now(); go("vms"); return before; });
  // The refresh tick (or coming back to the tab) during the load does not start
  // a second load beside it.
  await page.evaluate(() => refresh());
  await vmsShown(page);
  await page.waitForTimeout(3500);
  let reads = await page.evaluate(() => window.__reads);
  const earlier = reads.slice(first, left).filter(r => r.signal && r.method === "GET");
  // Cluster's own first read (its report) was answered before the card began.
  const leftCluster = await page.evaluate(() => window.__leftCluster);
  const stillRunning = earlier.filter(r => r.state !== "aborted" && !(r.answered < leftCluster));
  const vms = reads.slice(left).filter(r => r.path === "/api/vms");
  const arrived = await page.evaluate(() => window.__arrived);
  const queued = Math.round(vms[0].running - arrived);
  console.log(`/api/vms started ${queued} ms after leaving Volumes; ${earlier.length} reads of the pages left, `
    + `${stillRunning.length} not cancelled; ${vms.length} reads of /api/vms`);

  assert.deepEqual(stillRunning.map(r => `${r.path} ${r.state}`), [], "the pages left behind cancel their reads");
  const card = ["/api/lh/overview", "/api/self/health", "/api/settings", "/api/alerts"];
  const healthReads = reads.slice(health, left).filter(r => card.includes(r.path));
  assert.ok(healthReads.some(r => r.path === "/api/self/health"), "the health card was reading when Cluster was left");
  assert.deepEqual(healthReads.filter(r => r.state !== "aborted").map(r => `${r.path} ${r.state}`), [],
    "leaving Cluster cancels its health card's reads");
  assert.ok(queued < 500, `the page you went to does not queue behind the ones you left (${queued} ms)`);
  assert.equal(vms.length, 1, "a refresh during the load does not load the page a second time");
  assert.equal(await page.evaluate(() => window.__kept), "answered", "a kept read survives leaving the page");
  // Its late answer is still dropped like any read of a page left behind, so
  // the request itself is what shows it was not cancelled.
  assert.equal(reads.slice(0, first).find(r => r.path === "/api/nodes")?.state, "answered", "a read with its own signal is left alone");
  assert.equal(await page.evaluate(() => window.__write), "answered", "a write held across navigation completes and reports");
  assert.doesNotMatch(await page.locator("#views").innerText(), /could not (be )?read/i, "a cancelled read is not shown as a failed one");

  // A refresh asked for on purpose during the load (after a save, say) runs
  // once the load has finished.
  const mark = await page.evaluate(() => { const at = window.__reads.length; go("vms"); refresh(true); return at; });
  await vmsShown(page);
  await page.waitForFunction(at => window.__reads.slice(at).filter(r => r.path === "/api/vms" && r.state === "answered").length >= 2, mark, {timeout: 15000});
  reads = await page.evaluate(() => window.__reads);
  const [load, again] = reads.slice(mark).filter(r => r.path === "/api/vms");
  assert.ok(again.at >= load.answered, "the asked-for refresh waits for the load, then runs");

  // A load stuck for longer than 20 s no longer holds the refresh tick off.
  const stuck = await page.evaluate(async () => {
    STATE.loadingFor = window.NAV_TOKEN; STATE.loadingSince = Date.now() - 25000;
    const at = window.__reads.length;
    const ran = await refresh();
    STATE.loadingFor = null;
    return {ran, reads: window.__reads.slice(at).filter(r => r.path === "/api/vms").length};
  });
  assert.deepEqual(stuck, {ran: true, reads: 1}, "a load stuck past 20 s does not hold the tick off");

  assert.deepEqual(errors, []);
  await context.close();
  console.log("Navigation cancels the reads of the page left behind");
} finally {
  clearTimeout(watchdog);
  await browser.close();
}
