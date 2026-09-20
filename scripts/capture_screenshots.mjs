import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";

const output = "release-assets";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
await page.addInitScript(() => {
  localStorage.setItem("homestead.settings", JSON.stringify({ theme: "dark", bg: "soft", blur: 26, motion: "off", refresh: 60 }));
});
await page.goto("http://127.0.0.1:4173/?demo=1", { waitUntil: "networkidle" });
await page.locator("#views .phead").waitFor();
await page.evaluate(() => document.fonts.ready);

const capture = async (view, name) => {
  if (view) {
    await page.locator(`#nav a[data-view="${view}"]`).click();
    await page.locator("#views .phead").waitFor();
  }
  await page.screenshot({ path: `${output}/homestead-${name}.png`, fullPage: true });
};

await capture(null, "dashboard");
await capture("workloads", "containers");
await capture("flow", "architecture");
await capture("network", "networking");
await browser.close();
