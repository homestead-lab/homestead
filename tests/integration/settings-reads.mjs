// Settings reads what the open section shows (#705): opening it on one section
// does not read every other section's data, no path is read twice, and every
// section shown - by a repaint or without one - finishes loading its cards.
import assert from "node:assert/strict";
import {chromium} from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4176";
const browser = await chromium.launch({headless: true});

// What every section draws from, read on each paint.
const BASE = ["/api/settings", "/api/hardware/features", "/api/overview", "/api/auth/users", "/api/placement"];
// What each section's own cards read; none of it belongs to the Homestead section.
const OWN = {
  updates: ["/api/cluster/components", "/api/os-updates"],
  placement: ["/api/capacity/forecast"],
  hardware: ["/api/addons", "/api/namespaces/manage", "/api/storage/classes", "/api/longhorn/offline-rebuilding"],
  fleet: ["/api/fleet", "/api/move/moves"],
  connections: ["/api/mqtt", "/api/ipam"],
  access: ["/api/auth/keys", "/api/agents/requests"],
  troubleshooting: ["/api/diagnostics"],
};
const ELSEWHERE = Object.values(OWN).flat();
const HOMESTEAD = ["/api/self/health", "/api/self/replicas", "/api/homestead/data"];
// Settings' own paths; others (the setup guide's check) run on their own timers.
const settingsReads = reads => reads.filter(path => [...BASE, ...ELSEWHERE, ...HOMESTEAD].includes(path.split("@")[0]));
const twice = reads => [...new Set(reads.filter((path, i) => reads.indexOf(path) !== i))];

try {
  const context = await browser.newContext({viewport: {width: 1440, height: 900}});
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.addInitScript(() => localStorage.setItem("homestead.settings.tab", "homestead"));
  await page.goto(`${base}/?demo=1`, {waitUntil: "load"});
  await page.waitForSelector(".dashboard-widget");
  await page.evaluate(() => {
    window.__loopTimer?.stop();  // only Settings' own reads are counted
    window.__reads = [];
    window.__pending = [];  // reads not yet answered, with the page they belong to
    const real = window.api;
    window.api = (path, opts) => {
      if (!opts?.method || opts.method === "GET") window.__reads.push(String(path).split("?")[0] + (opts?.headers?.["X-Homestead-Cluster"] ? `@${opts.headers["X-Homestead-Cluster"]}` : ""));
      const read = {nav: window.NAV_TOKEN};
      window.__pending.push(read);
      // A read the page left behind is abandoned and never answers: only the
      // current page's reads are waited for.
      const done = () => { window.__pending = window.__pending.filter(other => other !== read); };
      const result = real(path, opts);
      Promise.resolve(result).then(done, done);
      return result;
    };
  });
  // Every read answered and no card on screen still spinning, then what was read.
  const settled = async what => {
    await page.waitForFunction(() => !window.__pending.some(read => read.nav === window.NAV_TOKEN)
      && ![...document.querySelectorAll(".settings-grid .spin2")].some(spin => spin.offsetParent !== null), null, {timeout: 20000})
      .catch(async () => {
        const spinning = await page.evaluate(() => [...document.querySelectorAll(".settings-grid .spin2")]
          .filter(spin => spin.offsetParent !== null).map(spin => spin.closest("[id]")?.id || "?"));
        assert.fail(`${what}: still loading after 20 s (pending reads, or spinners in ${spinning.join(", ") || "none"})`);
      });
    return page.evaluate(() => window.__reads.splice(0));
  };

  await page.evaluate(() => go("settings"));
  const opened = await settled("opening Settings");
  for (const path of HOMESTEAD) assert.ok(opened.includes(path), `the Homestead section reads ${path}`);
  for (const path of ELSEWHERE) assert.ok(!opened.includes(path), `opening Settings on Homestead does not read ${path}`);
  assert.deepEqual(twice(opened), [], "no path is read twice when Settings opens");

  // Shown without a repaint (a save error, a link while on Settings): the
  // section loads its cards once, and a section already loaded reads nothing.
  await page.evaluate(() => settingsTab("access"));
  const access = await settled("Users and access shown without a repaint");
  for (const path of OWN.access) assert.ok(access.includes(path), `showing access without a repaint reads ${path}`);
  assert.deepEqual(twice(access), [], "showing access without a repaint reads no path twice");
  await page.evaluate(() => settingsTab("access"));
  assert.deepEqual(settingsReads(await settled("access shown again")), [], "showing access again reads nothing");
  await page.evaluate(() => settingsTab("homestead"));
  assert.deepEqual(settingsReads(await settled("Homestead shown again")), [], "showing Homestead again reads nothing");

  // A link that names a section and then opens Settings, used while on it:
  // the repaint reads the section, once.
  await page.evaluate(() => { settingsTab("updates"); go("settings"); });
  const linked = await settled("Updates opened by a link while on Settings");
  for (const path of OWN.updates) assert.ok(linked.includes(path), `a link to Updates reads ${path}`);
  assert.deepEqual(twice(linked), [], "a link to Updates while on Settings reads no path twice");

  // Each section by its own button: it reads its own data and finishes loading.
  for (const [section, paths] of Object.entries(OWN)) {
    await page.evaluate(id => settingsGo(id), section);
    const shown = await settled(`opening ${section}`);
    for (const path of paths) assert.ok(shown.includes(path), `opening ${section} reads ${path}`);
  }
  await page.evaluate(() => settingsGo("monitoring"));
  const monitoring = await settled("opening monitoring");
  assert.deepEqual(monitoring.filter(path => !BASE.includes(path)), [], "Monitoring reads nothing beyond the base reads");
  for (const section of ["you", "homestead"]) {
    await page.evaluate(id => settingsGo(id), section);
    const shown = await settled(`opening ${section}`);
    for (const path of ELSEWHERE) assert.ok(!shown.includes(path), `opening ${section} does not read ${path}`);
    if (section === "homestead") for (const path of HOMESTEAD) assert.ok(shown.includes(path), `going back to Homestead reads ${path}`);
  }
  assert.deepEqual(errors, []);
  console.log("Settings reads only the open section's data, and every section finishes loading");
  await context.close();
} finally {
  await browser.close();
}
