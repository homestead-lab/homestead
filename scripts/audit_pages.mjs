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
import { openAuditPage } from "./audit_ready.mjs";

const output = process.env.PAGE_OUTPUT || "release-assets/pages";
const theme = process.env.HOMESTEAD_AUDIT_THEME === "light" ? "light" : "dark";
await mkdir(output, { recursive: true });
const base = (process.env.HOMESTEAD_URL || "http://127.0.0.1:4173") + "/?demo=1";
const only = process.argv[2] || "";

// name, the sidebar entry, then JavaScript to run once it is open (a tab).
const PAGES = [
  ["dashboard", "dash"],
  ["architecture", "flow"],
  ["architecture-jobs", "flow", "document.querySelector('.a2jobs > summary').click()"],
  ["nodes", "nodes"],
  ["nodes-cards", "nodes", "setViewLayout('nodes','viewNodes','cards')"],
  ["portal", "portal"],
  ["deploy", "deploy"],
  // Deploy's Placement: Edit's section, set before the first rollout (#844).
  ["deploy-placement", "deploy", "UI.selectSection('containerDeploy','placement')"],
  ["containers", "workloads"],
  ["containers-rows", "workloads", "setViewLayout('containers','renderWorkloads','rows')"],
  ["containers-cards", "workloads", "setViewLayout('containers','renderWorkloads','cards')"],
  ["containers-inline", "workloads", "setViewLayout('containers','renderWorkloads','rows');document.querySelector('.collection-disclosure')?.click()"],
  // Every card's pod list open (#873): an app with a pod on each host.
  ["containers-card-pods", "workloads", "setViewLayout('containers','renderWorkloads','cards');document.querySelectorAll('.wcard .workloadtree').forEach((d) => { d.open = true; })"],
  ["containers-inline-pods", "workloads", "setViewLayout('containers','renderWorkloads','rows');document.querySelector('.wl-row[data-row-key*=\"edge-tunnel\"] .collection-disclosure')?.click();document.querySelectorAll('.wl-detail-row:not([hidden]) .workloadtree').forEach((d) => { d.open = true; })"],
  // Every linked cluster at once.
  ["containers-all", "workloads", "localStorage.setItem('homestead.fleet.mode','all');viewWorkloads()"],
  ["vms-all", "vms", "localStorage.setItem('homestead.fleet.mode','all');viewVMs()"],
  ["nodes-all", "nodes", "localStorage.removeItem('homestead.layout.nodes');localStorage.setItem('homestead.fleet.mode','all');viewNodes()"],
  ["volumes-all", "storage", "localStorage.setItem('homestead.fleet.mode','all');viewStorage()"],
  ["vms", "vms"],
  ["vm-import", "vmimport"],
  // The setup guide is a page now (it was the Welcome dialog).
  ["setup", "setup"],
  ["setup-placement", "setup", "setupOpen('placement')"],
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
  ["data-protection-filtered", "protect", "protectFilter({show:'never'},true)"],
  ["cluster", "cluster"],
  ["resources", "resources"],
  ["agents", "agents"],
  ["networking", "network", "networkTab('overview')"],
  ["network-services", "network", "networkTab('services')"],
  ["network-vips", "network", "networkTab('vips')"],
  ["network-addresses", "network", "networkTab('addresses')"],
  ["network-lan", "network", "networkTab('lan')"],
  ["network-ports", "network", "networkTab('ports')"],
  ["ip-addresses", "network", "networkTab('ip')"],
  ["firewall", "network", "networkTab('firewall')"],
  ["certificates-and-keys", "network", "networkTab('expiry')"],
  ["import", "imports"],
  ["schedules", "schedules"],
  ["schedules-week", "schedule-week"],
  ["monitoring", "monitoring"],
  ["timeline", "timeline"],
  ["timeline-30-days", "timeline", "timelineRange('month')"],
  ["timeline-host", "timeline", "timelineFilter('host', 'harvester-node1')"],
  ["monitoring-30-days", "monitoring", "monitorRange('month')"],
  ["events", "events"],
  ["events-signins", "events", "STATE.eventsTab='signins';viewEvents()"],
  ["node-page", "nodes", "nodeDetail('harvester-node1')"],
  ["node-storage", "nodes", "nodeDetail('harvester-node1');setTimeout(() => nodeSectionGo('storage'), 1200)"],
  ["node-devices", "nodes", "nodeDetail('harvester-node1');setTimeout(() => { nodeSectionGo('hardware'); nodeDevicesLook('harvester-node1'); }, 1200)"],
  ...["homestead", "updates", "placement", "monitoring", "hardware", "fleet", "connections", "access", "troubleshooting", "you"]
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
  // What reloads the tab under a check, when one does: the reload is rare
  // and its cause not yet known, so it is named where it happens.
  let checking = "";
  page.on("load", () => { if (checking) console.log(`the tab reloaded during ${checking} (${label}): ${page.url()}`); });
  await page.addInitScript(theme => {
    localStorage.setItem("homestead.settings", JSON.stringify({ theme, bg: "soft", blur: 26, motion: "off", refresh: 60 }));
  }, theme);
  await openAuditPage(page, base);
  // Running jobs and passing notices belong to a moment, not the page.
  await page.addStyleTag({ content: "#jobTray,#toast{display:none!important}" });

  for (const [name, view, after] of items) for (let attempt = 1; ; attempt++) {
    checking = name;
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
        // Text squeezed into a narrow column beside a row's pill or buttons:
        // under 90px and three or more lines (the phone Image updates rows).
        const squeezed = texts.filter((e) => {
          if (e.closest("pre, code, table, svg, .monaco-editor, .xterm")) return false;
          const r = e.getBoundingClientRect(), range = document.createRange();
          range.selectNodeContents(e);
          const lines = new Set([...range.getClientRects()].map((x) => Math.round(x.top))).size;
          return r.width < 90 && lines >= 3 && e.textContent.trim().length > 12;
        }).map((e) => `${e.tagName.toLowerCase()}.${[...e.classList].slice(0, 2).join(".")} "${e.textContent.trim().slice(0, 30)}"`);
        // A field with no name: a screen reader says "edit text" and nothing more.
        const unnamed = [...root.querySelectorAll("input:not([type=hidden]), select, textarea")].filter((e) => e.checkVisibility()
          && !(e.getAttribute("aria-label") || "").trim() && !(e.getAttribute("title") || "").trim()
          && ![...(e.labels || [])].some((l) => l.textContent.trim())
          && !(e.getAttribute("aria-labelledby") || "").split(/\s+/).some((id) => document.getElementById(id)?.textContent.trim()))
          .map((e) => e.id ? `#${e.id}` : `${e.tagName.toLowerCase()}${e.name ? `[name=${e.name}]` : ""}.${[...e.classList].slice(0, 2).join(".")}`);
        // Where each pod runs, and an image's name and tag, are never cut (#873).
        const cut = (e) => e.scrollWidth > e.clientWidth + 1 || e.getBoundingClientRect().right > e.parentElement.getBoundingClientRect().right + 1;
        const hostEls = [...root.querySelectorAll(".tree-pod-head .tree-node")].filter((e) => e.checkVisibility());
        const clipped = [...hostEls, ...[...root.querySelectorAll(".tree-image-name")].filter((e) => e.checkVisibility())]
          .filter(cut).map((e) => `${e.className} "${e.textContent.trim().slice(0, 40)}"`);
        return {
          // The most hosts one pod list spans: one app with pods on several hosts.
          hosts: Math.max(0, ...[...root.querySelectorAll(".workloadtree[open]")].map((tree) => new Set([...tree.querySelectorAll(".tree-pod-head .tree-node")].map((e) => e.textContent.trim())).size)),
          clipped: [...new Set(clipped)].slice(0, 6),
          unnamed: [...new Set(unnamed)].slice(0, 8),
          title: document.querySelector("#views .phead h1, #views .phead h2, #views .phead")?.textContent.trim().split("\n")[0].slice(0, 40),
          height: document.documentElement.scrollHeight,
          sideways: document.documentElement.scrollWidth > vw + 1,
          tables: root.querySelectorAll("table").length,
          words: root.innerText.split(/\s+/).filter(Boolean).length,
          overflow: [...new Set(wide)].slice(0, 6),
          tiny: [...new Set(tiny)].slice(0, 8),
          squeezed: [...new Set(squeezed)].slice(0, 6),
        };
      });
      await page.screenshot({ path: `${output}/${name}-${label}.png`, fullPage: true });
      report.push({ name, label, ...metrics });
      if (metrics.sideways) failures.push(`${name} (${label}): the page scrolls sideways`);
      if (metrics.overflow.length) failures.push(`${name} (${label}): runs off the screen: ${metrics.overflow.join(", ")}`);
      if (metrics.tiny.length) failures.push(`${name} (${label}): text under 10px: ${metrics.tiny.join(", ")}`);
      if (metrics.squeezed.length) failures.push(`${name} (${label}): text squeezed into a narrow column: ${metrics.squeezed.join(", ")}`);
      if (metrics.clipped.length) failures.push(`${name} (${label}): a pod's host or an image's tag is cut: ${metrics.clipped.join(", ")}`);
      if (name.endsWith("-pods") && metrics.hosts < 3) failures.push(`${name} (${label}): no open pod list with pods on three hosts (at most ${metrics.hosts})`);
      if (metrics.unnamed.length) failures.push(`${name} (${label}): fields with no name: ${metrics.unnamed.join(", ")}`);
    } catch (error) {
      // The tab reloaded under the check ("Execution context was destroyed"),
      // now and then on any of the later pages: open the app again and look
      // at this page once more. A page that navigates away itself fails twice.
      if (attempt === 1 && /Execution context was destroyed/.test(error.message)) {
        checking = "";
        await openAuditPage(page, base);
        await page.addStyleTag({ content: "#jobTray,#toast{display:none!important}" });
        continue;
      }
      failures.push(`${name} (${label}): could not open: ${error.message.split("\n")[0]}${errors.length ? ` - error: ${errors.at(-1)}` : ""}`);
    }
    break;
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
