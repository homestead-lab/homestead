// The node probe is read in the background (#705): a host shows at once, says
// it is waiting for its probe, keeps its last readings when a read is missed,
// and names a probe that has missed three in a row.
import assert from "node:assert/strict";
import {chromium} from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4176";
const browser = await chromium.launch({headless: true});
try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({viewport: {width, height: 1000}}), page = await context.newPage(), errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(`${base}/?demo=1`);
    await page.locator('[data-widget="nodes"]').waitFor();
    // These steps paint the pages themselves; the refresh tick would paint over them.
    await page.evaluate(() => window.__loopTimer?.stop());
    await page.evaluate(async () => {
      const original = window.api, seed = structuredClone(STATE.data.ov.nodes[0]);
      const host = (name, probe, temps) => ({...structuredClone(seed), name, probe, temps, disk_issues: []});
      const nodes = [
        host("node-a", {state: "waiting", misses: 0, last_ok: null, age_s: null}, null),
        host("node-b", {state: "stale", misses: 1, last_ok: 1, age_s: 45}, {...seed.temps, cpu_c: 44, max_c: 95, max_source: "CPU package", disks: []}),
        host("node-c", {state: "unreachable", misses: 3, last_ok: 1, age_s: 600}, {...seed.temps, cpu_c: 51, max_c: 96, max_source: "CPU package", disks: []}),
      ];
      window.__probeNodes = nodes;
      window.api = async (path, opts) => {
        const answer = await original(path, opts);
        if (path === "/api/nodes") return nodes;
        if (path === "/api/overview") return {...answer, nodes, nodes_total: 3};
        return answer;
      };
      await viewDash();
    });
    const card = name => page.locator(".node-compact-host", {has: page.getByRole("button", {name, exact: true})});
    assert.match(await card("node-a").innerText(), /CPU temp\s*waiting for the probe/);
    assert.match(await card("node-b").innerText(), /44°C · max 95°C, CPU package · last reading 45 s ago/);
    assert.match(await card("node-c").innerText(), /Probe not responding/);
    assert.match(await card("node-c").locator(".node-compact-warning").innerText(),
      /The probe on node-c has not responded the last 3 times. Its last reading is 10 min old./);
    assert.match(await card("node-c").innerText(), /51°C · max 96°C, CPU package · last reading 10 min ago/, "the last values stay, with their age");
    // The probe's warning is not a drive warning.
    assert.equal(await page.evaluate(() => nodeStorageLine(window.__probeNodes[2], 1, 0)), "1 drive");
    // Health: a stale reading still raises a temperature insight; an unreachable probe's old one does not.
    await page.evaluate(() => HealthInsights.open("health"));
    await page.waitForFunction(() => document.querySelector("#clusterHealth")?.textContent.includes("node-b: maximum 95°C"));
    assert.doesNotMatch(await page.locator("#clusterHealth").textContent(), /node-c: maximum/);
    assert.match(await page.locator("#clusterHealth").textContent(), /Highest temperature 95°C \(node-b/, "not node-c's old 96°C");
    await page.evaluate(() => closeModal());

    // Hosts as cards: the temperature line waits, and a PROBE line gives the age.
    await page.evaluate(async () => { localStorage.setItem("homestead.layout.nodes", "cards"); await viewNodes(); });
    await page.waitForFunction(() => document.querySelector(".phead h2")?.innerText.trim() === "Hosts");
    const hosts = await page.locator("#views").innerText();
    assert.match(hosts, /TEMP\s*waiting for the probe/);
    assert.match(hosts, /PROBE\s*last reading 45 s ago/);
    assert.match(hosts, /PROBE\s*not answering · last reading 10 min ago/);
    // Hosts compared in rows: the same words in the temperature row.
    await page.evaluate(async () => { localStorage.setItem("homestead.layout.nodes", "rows"); await viewNodes(); });
    await page.waitForFunction(() => document.querySelector(".node-comparison"));
    const rows = await page.locator("#views").innerText();
    assert.match(rows, /waiting for the probe/);
    // The table's notes are hidden on a phone, as all of its notes are.
    if (width > 600) assert.match(rows, /last reading 45 s ago/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    assert.deepEqual(errors, []);
    console.log(`Node probe states passed at ${width}px`);
    await context.close();
  }
} finally {
  await browser.close();
}
