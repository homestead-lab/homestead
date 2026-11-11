// A failed app icon is asked for once, not on every refresh (#906), keyed rows keep their node and
// focus when a list reorders, and an action menu open while its app changes is brought up to date
// once it closes. Real Chromium, on the demo's icons: one answers (an SVG), one 404s.
import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const browser = await chromium.launch({ headless: true });
try {
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  // The demo answers in the page (no network request to watch), and notes each icon it is asked for.
  await page.goto(`${base}/containers?demo=1`, { waitUntil: "networkidle" });
  await page.locator(".wl-row, .wcard").first().waitFor();
  await page.evaluate(() => { window.__loopTimer?.stop(); });
  let icons = [];
  const read = async () => { icons = await page.evaluate(() => window.__iconRequests || []); };
  const count = kind => icons.filter(path => path.includes(kind === "ok" ? "a".repeat(64) : "b".repeat(64))).length;
  const missing = () => page.evaluate(() => {
    const rows = [...document.querySelectorAll("#views .appav")].filter(av => av.closest(".wl-row, .wcard")?.textContent.includes("home-assistant"));
    return rows.map(av => ({ img: !!av.querySelector("img"), initials: av.querySelector(".avfallback")?.hidden === false }));
  });
  await page.waitForTimeout(300);
  for (let tick = 0; tick < 3; tick++) { await page.evaluate(() => refresh(true)); await page.waitForTimeout(150); }
  await read();
  assert.equal(count("missing"), 1, `a missing icon is asked for once over 3 refreshes, not on each: ${JSON.stringify(icons)}`);
  assert.equal(count("ok"), 1, "an icon that answers is fetched once too");
  const shown = await missing();
  assert.ok(shown.length && shown.every(x => !x.img && x.initials), `the app shows its initials: ${JSON.stringify(shown)}`);
  const kept = await page.evaluate(async () => {
    const av = [...document.querySelectorAll("#views .appav")].find(a => a.closest(".wl-row, .wcard")?.textContent.includes("frigate"));
    const img = av.querySelector("img"); const before = { src: img.src, hidden: av.querySelector(".avfallback").hidden };
    await refresh(true); await refresh(true);
    return { same: av.querySelector("img") === img, src: img.src === before.src, hidden: av.querySelector(".avfallback").hidden === before.hidden && before.hidden };
  });
  assert.deepEqual(kept, { same: true, src: true, hidden: true }, "an icon that loaded keeps its picture across refreshes");
  assert.ok(await page.locator("[data-mkey]").count() > 0, "the workloads list carries data-mkey");

  // A keyed list in the real browser: the same row nodes after a reorder, and focus where the reader was.
  const result = await page.evaluate(() => {
    const make = (...keys) => { const ul = document.createElement("ul");
      ul.innerHTML = keys.map(k => `<li data-mkey="${k}"><button type="button">${k}</button></li>`).join(""); return ul; };
    const host = make("a", "b", "c"); document.body.append(host);
    const [a, b, c] = [...host.children], button = b.firstElementChild; button.focus();
    morph(host, make("c", "a", "b"));
    const out = { order: [...host.children].map(x => x.dataset.mkey).join(""), a: host.children[1] === a, b: host.children[2] === b, c: host.children[0] === c,
      focus: document.activeElement === button };
    host.remove(); return out;
  });
  assert.deepEqual(result, { order: "cab", a: true, b: true, c: true, focus: true }, "keyed rows keep their nodes and focus");

  // The same data painted again does no DOM work, on every refresh, not every other one: the page
  // watcher (a real MutationObserver, which node tests do not have) must not count paint's own marks.
  // paint() is counted too: no morph is only a skip when each refresh did paint.
  const morphs = await page.evaluate(async () => {
    await refresh(true); await new Promise(r => setTimeout(r, 50));   // let any change since settle once
    const real = window.morph, realPaint = window.paint; let calls = 0;
    const paints = [];
    const views = document.querySelector("#views");   // the page; the bell and widgets morph their own parts
    window.morph = (...args) => { if (args[0] === views) calls++; return real(...args); };
    window.paint = (...args) => { paints[paints.length - 1]++; return realPaint(...args); };
    try { for (let tick = 0; tick < 3; tick++) { paints.push(0); await refresh(true); await new Promise(r => setTimeout(r, 50)); } }
    finally { window.morph = real; window.paint = realPaint; }
    return { paints, calls, hash: !!document.querySelector("#views").dataset.hash };
  });
  assert.ok(morphs.paints.every(n => n > 0), `each of the three refreshes paints the page: ${JSON.stringify(morphs.paints)}`);
  assert.deepEqual({ calls: morphs.calls, hash: morphs.hash }, { calls: 0, hash: true }, "three refreshes of the same data morph the page not once");

  // An app that stops while its "…" menu is open: the open menu is left alone, and once it closes
  // the next refresh brings it up to date, though the data has not changed again since.
  const MENU = `#views .wl-row[data-mkey*='"frigate"'] details.actionmenu`;
  const items = () => page.evaluate(sel => {
    const d = document.querySelector(sel);
    return d ? [...(d.shell || d).querySelectorAll(".actionmenu-pop button")].map(b => b.textContent.trim()) : null;
  }, MENU);
  const before = await items();
  assert.ok(before?.includes("Stop") && !before.includes("Start"), `frigate's menu offers Stop: ${JSON.stringify(before)}`);
  await page.locator(`${MENU} > summary`).click();
  await page.locator("details.actionmenu-portal[open]").waitFor({ state: "attached" });
  await page.evaluate(async () => {
    Object.assign(window.__demoWorkloads.find(w => w.name === "frigate"), { desired: 0, ready: 0 });
    await refresh(true);
  });
  assert.ok((await items()).includes("Stop"), "the open menu is left as it was");
  // Closing the menu puts it back in its row, a change the page watcher would see and so make the
  // next paint morph by itself. The watcher is stopped across the close (paint() starts a new one),
  // so only paint()'s own note of the part it left behind (report.skipped) can bring the menu up to date.
  assert.ok(await page.evaluate(() => {
    const views = document.querySelector("#views"), watcher = views.paintWatcher;
    if (!watcher) return false;
    watcher.disconnect(); delete views.paintWatcher; return true;
  }), "the page has a paint watcher to stop");
  await page.keyboard.press("Escape");
  await page.locator("details.actionmenu-portal").waitFor({ state: "detached" });
  for (let tick = 0; tick < 2; tick++) await page.evaluate(() => refresh(true));
  const after = await items();
  assert.ok(after?.includes("Start") && !after.includes("Stop"), `once closed, frigate's menu offers Start, not Stop: ${JSON.stringify(after)}`);
  assert.deepEqual(errors, []);
  console.log("failed icon asked for once over 3 refreshes; loaded icon kept; keyed rows keep node and focus");
} finally { await browser.close(); }
