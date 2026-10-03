// Every page, opened against the demo data at a desktop and a phone width,
// captured whole and measured against the page rules in docs/design.md.
//
//   node scripts/audit_pages.mjs              all pages
//   node scripts/audit_pages.mjs storage      only those whose name has "storage"
//
// Writes release-assets/pages/<name>-<width>.png and report.json. It exits
// non-zero when a page can be measured breaking a rule: the page scrolls
// sideways, something runs off the side of the screen, text is smaller than
// 10px, or the page does not open. CI runs it, so a page cannot regress.
import { chromium } from "playwright";
import { mkdir, writeFile } from "node:fs/promises";

const output = process.env.PAGE_OUTPUT || "release-assets/pages";
const theme = process.env.HOMESTEAD_AUDIT_THEME === "light" ? "light" : "dark";
await mkdir(output, { recursive: true });
const base = (process.env.HOMESTEAD_URL || "http://127.0.0.1:4173") + "/?demo=1";
const only = process.argv[2] || "";

// name, the sidebar entry, then JavaScript to run once it is open (a tab).
const PAGES = [
  ["dashboard", "dash"],
  ["architecture", "flow"],
  ["nodes", "nodes"],
  ["nodes-cards", "nodes", "setViewLayout('nodes','viewNodes','cards')"],
  ["portal", "portal"],
  ["deploy", "deploy"],
  ["containers", "workloads"],
  ["containers-rows", "workloads", "setViewLayout('containers','renderWorkloads','rows')"],
  ["containers-cards", "workloads", "setViewLayout('containers','renderWorkloads','cards')"],
  ["containers-inline", "workloads", "setViewLayout('containers','renderWorkloads','rows');document.querySelector('.collection-disclosure')?.click()"],
  // Every linked cluster at once.
  ["containers-all", "workloads", "localStorage.setItem('homestead.fleet.mode','all');viewWorkloads()"],
  ["vms-all", "vms", "localStorage.setItem('homestead.fleet.mode','all');viewVMs()"],
  ["nodes-all", "nodes", "localStorage.removeItem('homestead.layout.nodes');localStorage.setItem('homestead.fleet.mode','all');viewNodes()"],
  ["volumes-all", "storage", "localStorage.setItem('homestead.fleet.mode','all');viewStorage()"],
  ["vms", "vms"],
  ["vm-import", "vmimport"],
  // The setup guide is a page now (it was the Welcome dialog).
  ["setup", "setup"],
  ["setup-phone", "setup", "setupOpen('phone')"],
  ["setup-appearance", "setup", "setupOpen('appearance')"],
  ["setup-disks", "setup", "setupOpen('disks')"],
  ["setup-lan", "setup", "setupOpen('lan')"],
  ["setup-smb", "setup", "setupOpen('smb')"],
  ["app-store", "store"],
  ["helm", "helm"],
  ["shares", "shares"],
  ["volumes", "storage"],
  ["image-cache", "images"],
  ["data-protection", "protect"],
  ["cluster", "cluster"],
  ["resources", "resources"],
  ["networking", "network", "networkTab('services')"],
  ["ip-addresses", "network", "networkTab('ip')"],
  ["firewall", "network", "networkTab('firewall')"],
  ["import", "imports"],
  ["schedules", "schedules"],
  ["events", "events"],
  ["events-signins", "events", "STATE.eventsTab='signins';viewEvents()"],
  ["node-page", "nodes", "nodeDetail('harvester-node1')"],
  ["node-storage", "nodes", "nodeDetail('harvester-node1');setTimeout(() => nodeSectionGo('storage'), 1200)"],
  ["node-devices", "nodes", "nodeDetail('harvester-node1');setTimeout(() => { nodeSectionGo('hardware'); nodeDevicesLook('harvester-node1'); }, 1200)"],
  ...["homestead", "updates", "monitoring", "hardware", "fleet", "connections", "access", "troubleshooting", "you"]
    .map((tab) => [`settings-${tab}`, "settings", `settingsTab('${tab}')`]),
];

const report = [];
const failures = [];
const browser = await chromium.launch({ headless: true });

// Each width's list is shared between a few tabs, all at once: one tab
// working through every item in turn took most of CI's time.
const WORKERS = Number(process.env.AUDIT_WORKERS || 4);
const WIDTHS = [["desktop", 1440, 900, false], ["mobile", Number(process.env.HOMESTEAD_MOBILE_WIDTH || 390), 844, true]];
const todo = PAGES.filter(([name]) => !only || name.includes(only));
const shares = Array.from({ length: WORKERS }, (_, i) => todo.filter((_, j) => j % WORKERS === i)).filter((share) => share.length);
await Promise.all(WIDTHS.flatMap((width) => shares.map((share) => audit(width, share))));
await browser.close();
// Back in list order, whichever tab finished first.
const order = (r) => todo.findIndex(([name]) => name === r.name) * 2 + (r.label === "mobile");
report.sort((a, b) => order(a) - order(b));

async function audit([label, width, height, mobile], items) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: mobile ? 2 : 1,
    isMobile: mobile, hasTouch: mobile, colorScheme: theme });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(theme => {
    localStorage.setItem("homestead.settings", JSON.stringify({ theme, bg: "soft", blur: 26, motion: "off", refresh: 60 }));
  }, theme);
  await page.goto(base, { waitUntil: "networkidle" });
  await page.locator("#views .phead").waitFor({state:"attached"});
  await page.locator("#views").waitFor({state:"visible"});
  await page.evaluate(() => document.fonts.ready);
  // Running jobs and passing notices belong to a moment, not the page.
  await page.addStyleTag({ content: "#jobTray,#toast{display:none!important}" });

  for (const [name, view, after] of items) {
    try {
      await page.evaluate(() => { try { closeModal(); } catch (e) { /* none open */ } localStorage.removeItem("homestead.fleet.mode"); });
      await page.evaluate((v) => go(v), view);
      await page.locator("#views .phead").waitFor({state:"attached"});
      await page.locator("#views").waitFor({state:"visible"});
      await page.waitForTimeout(1200);
      if (after) { await page.evaluate(after); await page.waitForTimeout(900); }
      await page.evaluate(() => window.scrollTo(0, 0));
      const metrics = await page.evaluate(() => {
        const root = document.querySelector("#views");
        const vw = window.innerWidth;
        // Inside a container that scrolls sideways on purpose, running wide is fine.
        const scrolls = (e) => { for (let p = e.parentElement; p && p !== root; p = p.parentElement) {
          const o = getComputedStyle(p).overflowX; if (o === "auto" || o === "scroll" || o === "hidden") return true; } return false; };
        const visible = [...root.querySelectorAll("*")].filter((e) => e.offsetParent && e.getBoundingClientRect().width > 0);
        const wide = visible.filter((e) => e.getBoundingClientRect().right > vw + 1 && !scrolls(e))
          .map((e) => `${e.tagName.toLowerCase()}.${[...e.classList].slice(0, 2).join(".")}`);
        const texts = visible.filter((e) => [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()));
        const tiny = texts.filter((e) => parseFloat(getComputedStyle(e).fontSize) < 10)
          .map((e) => `${e.tagName.toLowerCase()}.${[...e.classList].slice(0, 2).join(".")} ${getComputedStyle(e).fontSize}`);
        return {
          title: document.querySelector("#views .phead h1, #views .phead h2, #views .phead")?.textContent.trim().split("\n")[0].slice(0, 40),
          height: document.documentElement.scrollHeight,
          sideways: document.documentElement.scrollWidth > vw + 1,
          tables: root.querySelectorAll("table").length,
          words: root.innerText.split(/\s+/).filter(Boolean).length,
          overflow: [...new Set(wide)].slice(0, 6),
          tiny: [...new Set(tiny)].slice(0, 8),
        };
      });
      await page.screenshot({ path: `${output}/${name}-${label}.png`, fullPage: true });
      report.push({ name, label, ...metrics });
      if (metrics.sideways) failures.push(`${name} (${label}): the page scrolls sideways`);
      if (metrics.overflow.length) failures.push(`${name} (${label}): runs off the screen: ${metrics.overflow.join(", ")}`);
      if (metrics.tiny.length) failures.push(`${name} (${label}): text under 10px: ${metrics.tiny.join(", ")}`);
    } catch (error) {
      failures.push(`${name} (${label}): could not open: ${error.message.split("\n")[0]}${errors.length ? ` - error: ${errors.at(-1)}` : ""}`);
    }
  }
  await context.close();
}

await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2));
for (const r of report) {
  console.log(`${r.name.padEnd(20)} ${r.label.padEnd(8)} ${String(r.height).padStart(6)}px ${String(r.words).padStart(5)} words ${r.tables} tables`);
}
if (failures.length) {
  console.log(`\n${failures.length} problem(s):\n  ${failures.join("\n  ")}`);
  process.exitCode = 1;
}
