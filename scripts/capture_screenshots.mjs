// Release screenshots, from the demo data - never a live cluster.
//
// The README and the wiki link to these by name from the latest release
// (releases/latest/download/homestead-<name>.png), so every release brings
// them up to date. A shot that cannot be taken is reported and skipped:
// one missing picture is no reason to hold a release back.
import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";

const output = "release-assets";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
await page.addInitScript(() => {
  localStorage.setItem("homestead.settings", JSON.stringify({ theme: "dark", bg: "soft", blur: 26, motion: "off", refresh: 60 }));
});
await page.goto((process.env.HOMESTEAD_URL || "http://127.0.0.1:4173") + "/?demo=1", { waitUntil: "networkidle" });
await page.locator("#views .phead").waitFor();
await page.evaluate(() => document.fonts.ready);
// The demo keeps jobs running for its own pages; in a picture the tray only
// covers what the picture is of.
await page.addStyleTag({ content: "#jobTray{display:none!important}" });

const settle = () => page.waitForTimeout(900);
const missed = [];

// A page from the sidebar, whole.
const capture = async (view, name, before) => {
  try {
    if (view) {
      await page.evaluate(() => { if (!document.querySelector("#modal")?.classList.contains("hidden")) closeModal(); });
      await page.locator(`#nav a[data-view="${view}"]`).click();
      await page.locator("#views .phead").waitFor();
    }
    if (before) await page.evaluate(before);
    await settle();
    await page.screenshot({ path: `${output}/homestead-${name}.png`, fullPage: true });
  } catch (error) {
    missed.push(`${name}: ${error.message.split("\n")[0]}`);
  }
};

// A dialog, opened on a page the way someone would open it.
const dialog = async (view, name, open) => {
  try {
    await page.evaluate(() => { if (!document.querySelector("#modal")?.classList.contains("hidden")) closeModal(); });
    await page.locator(`#nav a[data-view="${view}"]`).click();
    await page.locator("#views .phead").waitFor();
    await settle();
    await page.evaluate(open);
    await page.locator("#modal:not(.hidden) #mbody").waitFor();
    await page.waitForTimeout(1400);
    await page.screenshot({ path: `${output}/homestead-${name}.png` });
    await page.evaluate(() => closeModal());
  } catch (error) {
    missed.push(`${name}: ${error.message.split("\n")[0]}`);
  }
};

await capture(null, "dashboard");
await capture("workloads", "containers");
await capture("flow", "architecture");
await capture("network", "networking", () => { try { localStorage.setItem("homestead.network.tab", "services"); } catch (e) {} });
await capture("nodes", "nodes");
await capture("storage", "volumes");
await capture("vms", "vms");
await capture("store", "app-store");
await capture("imports", "import");
await capture("shares", "shares");
await capture("protect", "data-protection");
await capture("helm", "helm");
await capture("resources", "resources");
await capture("cluster", "cluster");
await capture("settings", "settings-cluster", () => settingsTab("cluster"));
await capture("settings", "settings-health", () => settingsTab("about"));

await dialog("nodes", "node-detail", () => nodeDetail("harvester-node1"));
await dialog("storage", "disks", () => lhDisks());
await dialog("storage", "storage-class-change",
  () => volumeReclass(STATE.data.vols.find(v => v.pvc_name === "frigate-config") || STATE.data.vols[0]));
await dialog("storage", "storage-class-progress", () => reclassWatch("op4"));
await dialog("vms", "vm-new", () => vmNew());
await dialog("vms", "vm-edit", () => vmEdit("default", "home-assistant-os"));
await dialog("workloads", "image-updates", () => imageUpdateCenter());
await dialog("network", "vip-add", () => vipAdd());

await browser.close();
if (missed.length) console.log("screenshots skipped:\n  " + missed.join("\n  "));
