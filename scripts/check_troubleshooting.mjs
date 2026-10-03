// Exercise the recorder itself, including privacy boundaries and interrupted saves.
import { chromium } from "playwright";
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const browser = await chromium.launch({ headless: true });
await mkdir("release-assets/troubleshooting", { recursive: true });
try {
  for (const width of [1280, 375]) for (const theme of ["dark", "light"]) {
    const page = await browser.newPage({ viewport: { width, height: 960 }, acceptDownloads: true });
    const errors = [];
    page.on("pageerror", error => { if (!error.message.includes("diagnostic test failure")) errors.push(error.message); });
    await page.addInitScript(theme => localStorage.setItem("homestead.settings", JSON.stringify({ theme, motion: "off", refresh: 60 })), theme);
    await page.goto(`${base}/settings?demo=1&tab=troubleshooting`, { waitUntil: "networkidle" });
    await page.locator("#diagnosticReports table").waitFor();
    const button = name => page.getByRole("button", { name, exact: true });
    await button("Record a bug").click(); await button("Start recording").click();
    await page.locator("#diagnosticRecorder:not(.hidden)").waitFor();
    await page.evaluate(() => go("workloads"));
    await page.locator("#views .phead").waitFor({state:"attached"});
    await page.evaluate(() => {
      window.diagnosticNoop = () => {};
      const fixture = document.createElement("div"); fixture.id = "diagnosticFixture";
      fixture.innerHTML = '<button onclick="diagnosticNoop()">UI only</button><input aria-label="Private value"><input type="password" aria-label="Secret password"><details><summary>UI disclosure</summary>Details</details><select aria-label="Private option"><option value="private-option">Private option</option><option value="private-other">Private other</option></select>';
      document.querySelector("#views").appendChild(fixture);
    });
    await button("UI only").click();
    await page.getByLabel("Private value", { exact: true }).fill("DO-NOT-RECORD-TEXT");
    await page.getByLabel("Secret password").fill("DO-NOT-RECORD-PASSWORD");
    await page.getByLabel("Private option", { exact: true }).selectOption("private-other");
    await page.getByText("UI disclosure", { exact: true }).click();
    await page.evaluate(() => {
      window.dispatchEvent(new ErrorEvent("error", { message: "diagnostic test failure", lineno: 22 }));
      const original = window.fetch;
      window.fetch = (url, init) => {
        if (String(url).includes("/api/diagnostics/events")) { window.fetch = original; return Promise.resolve(new Response(JSON.stringify({ error: "offline test" }), { status: 503, headers: { "Content-Type": "application/json" } })); }
        return original(url, init);
      };
    });
    await button("Stop recording").click();
    await page.getByText(/Could not save all events/).waitFor();
    await button("Save and review").click();
    await page.locator("#bugTitle").waitFor();
    const id = await page.evaluate(async () => (await api("/api/diagnostics")).find(row => row.title === "Bug report").id);
    const record = await page.evaluate(id => api(`/api/diagnostics/report?id=${id}&format=full`), id);
    assert(record.events.some(row => row.kind === "click" && row.action === "diagnosticNoop"), "UI-only click retained");
    assert(record.events.some(row => row.kind === "navigation" && row.action === "workloads"));
    assert(record.events.some(row => row.kind === "error" && row.message === "diagnostic test failure"));
    assert(record.events.some(row => row.kind === "change"));
    assert(record.events.some(row => row.kind === "toggle"));
    assert(!JSON.stringify(record).includes("DO-NOT-RECORD"));
    assert(!JSON.stringify(record).includes("private-other"));
    await page.locator("#bugTitle").fill("Dialog does not close");
    await page.locator("#bugComment").fill("Expected the dialog to close after clicking Save.");
    await button("Keep draft").click();
    const draft = await page.evaluate(id => api(`/api/diagnostics/report?id=${id}&format=full`), id);
    assert.equal(draft.comment, "Expected the dialog to close after clicking Save.");
    await page.evaluate(id => bugDescribe(id), id);
    await button("Prepare report").click(); await page.locator("#bugFormat").waitFor();
    await page.locator("#bugFormat").selectOption("full");
    await page.getByText("Full logs may contain sensitive information", { exact: true }).waitFor();
    const downloadWait = page.waitForEvent("download"); await button("Download log").click();
    assert((await downloadWait).suggestedFilename().endsWith("-full.log"));
    await button("GitHub issue…").click();
    assert(await button("Continue on GitHub").isDisabled());
    await page.locator("#bugShareAck").check();
    assert(!(await button("Continue on GitHub").isDisabled()));
    assert((await button("Continue on GitHub").getAttribute("onclick")).includes("https://github.com/wjcloudy/homestead/issues/new?"));
    await page.screenshot({ path: `release-assets/troubleshooting/issue-${theme}-${width}.png`, fullPage: true });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.evaluate(() => closeModal()); await page.evaluate(() => bugPackage());
    await page.locator("#bugPackageFormat").selectOption("full");
    await button("Prepare package").click(); await page.locator("#bugFormat").waitFor();
    assert.equal(await page.locator("#bugFormat").inputValue(), "full");
    await page.screenshot({ path: `release-assets/troubleshooting/full-${theme}-${width}.png`, fullPage: true });
    await page.evaluate(() => closeModal()); await page.evaluate(() => bugStart()); await button("Start recording").click();
    await page.locator("#diagnosticRecorder:not(.hidden)").waitFor();
    await page.waitForFunction(() => !!sessionStorage.getItem("homestead.diagnostic.recording"));
    await page.reload({ waitUntil: "networkidle" }); await page.getByText("Recording interrupted", { exact: true }).waitFor();
    // A real server retains the interrupted report; the demo resets on reload.
    assert.equal(await page.evaluate(() => document.querySelectorAll("#diagnosticRecorder button").length), 1);
    assert.deepEqual(errors, []);
    console.log(`Recorder passed: ${width}px ${theme}; UI-only actions, excluded values, retry, draft, formats, public review, reload`);
    await page.close();
  }
} finally { await browser.close(); }
