// Channel selection saves the preference and checks for a release, without
// installing anything. Check both settings and the dialog at phone width too.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";

const output = "release-assets/pages";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  for (const width of [1440, 375]) for (const theme of ["dark", "light"]) {
    const page = await browser.newPage({ viewport: { width, height: 1000 } });
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.addInitScript(theme => {
      localStorage.setItem("homestead.settings", JSON.stringify({ theme, motion: "off", refresh: 60 }));
    }, theme);
    await page.goto((process.env.HOMESTEAD_URL || "http://127.0.0.1:4173") + "/?demo=1", { waitUntil: "networkidle" });
    await page.evaluate(() => { settingsTab("updates"); go("settings"); });
    const picker = page.locator("#homesteadUpdateCard select[aria-label='Homestead release channel']");
    await picker.waitFor();
    assert.equal(await picker.inputValue(), "prod");
    await page.evaluate(() => {
      window.channelCalls = [];
      const realApi = api;
      window.api = async (path, opts) => { channelCalls.push(path); return realApi(path, opts); };
    });
    await picker.selectOption("dev");
    await page.waitForFunction(() => STATE.data.appSettings?.updates?.channel === "dev" && STATE.data.imageUpdates?.channel === "dev" && !HOMESTEAD_CHANNEL_SAVING);
    assert.match(await page.locator("#homesteadUpdateCard").innerText(), /2\.9\.0-dev\.1/);
    assert.equal(await picker.inputValue(), "dev");
    assert.ok(!(await page.evaluate(() => channelCalls)).some(path => /\/(apply|rollback)$/.test(path)));
    await page.addStyleTag({ content: "#jobTray,#toast{display:none!important}" });
    await picker.scrollIntoViewIfNeeded();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2);
    assert.equal(overflow, false, `settings overflow at ${width}/${theme}`);
    await page.screenshot({ path: `${output}/update-channel-${width}-${theme}.png`, fullPage: true });
    await picker.selectOption("prod");
    await page.waitForFunction(() => STATE.data.imageUpdates?.channel === "prod" && !HOMESTEAD_CHANNEL_SAVING);
    await page.evaluate(() => homesteadUpdateDialog());
    const modalPicker = page.locator("#hsUpdateBody select[aria-label='Homestead release channel']");
    await modalPicker.waitFor();
    // Linked clusters: one list, each tickbox beside its own cluster's name,
    // and the button saying which clusters it updates.
    await page.waitForFunction(() => document.querySelectorAll("#hsUpdateBody .hs-cluster").length >= 3);
    const rows = await page.locator("#hsUpdateBody .hs-cluster").evaluateAll(els => els.map(el => {
      const box = el.querySelector("input")?.getBoundingClientRect(), name = el.querySelector(".hs-cluster-name b").getBoundingClientRect();
      return { name: el.querySelector(".hs-cluster-name b").textContent, left: box && box.right <= name.left, level: box && Math.abs(box.top - name.top) < 14 };
    }));
    for (const row of rows) assert.ok(row.left && row.level, `the tickbox sits beside ${row.name} at ${width}px`);
    const button = () => page.locator("#hsUpdateBody .hs-actions .btn.pri").textContent();
    assert.match(await button(), /^Update to /);
    await page.locator("#hsUpdateBody .hs-cluster", { hasText: "Branch office" }).locator("input").check();
    assert.equal(await button(), "Update 2 clusters");
    await page.locator("#hsUpdateBody .hs-cluster", { hasText: "Branch office" }).locator("input").uncheck();
    assert.equal(await modalPicker.inputValue(), "prod");
    await modalPicker.selectOption("dev");
    await page.waitForFunction(() => STATE.data.imageUpdates?.channel === "dev" && !HOMESTEAD_CHANNEL_SAVING);
    assert.equal(await modalPicker.inputValue(), "dev");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    assert.deepEqual(errors, [], `page errors at ${width}/${theme}`);
    await page.close();
    console.log(`Update channels passed at ${width}px, ${theme}`);
  }
} finally { await browser.close(); }
