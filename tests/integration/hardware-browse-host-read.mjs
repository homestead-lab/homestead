// The hardware feature editor browses a host's devices from that host's own
// read (/api/node), as the host lists no longer carry them (#1211): the
// devices show, a failed read says so and is tried again, and the list reads
// are not asked for them.
import assert from "node:assert/strict";
import {chromium} from "playwright";
import {openAuditPage} from "../../scripts/audit_ready.mjs";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const browser = await chromium.launch({headless: true});

// The demo's hosts have no device list: each host's own read gets one, and
// the list rows lose theirs, as the server sends them. Every read is counted.
const devicesOnlyInTheHostsRead = () => {
  const read = window.__realApi = window.__realApi || window.api;
  window.__reads = [];
  window.__failNode = 0;
  const devices = name => ({
    path_entries: [{path: `/dev/hailo-${name}`, type: "CharDevice"}, {path: "/dev/dri/renderD128", type: "CharDevice"}],
    usb: [{vid: "1a6e", pid: "089a", name: `Coral USB on ${name}`, path: "/dev/bus/usb/002/003"}]});
  const listRow = n => { const {labels, ...row} = n; const {devices, mounts, iscsi, ...temps} = n.temps || {}; return {...row, temps}; };
  window.api = async (asked, ...rest) => {
    const url = String(asked);
    window.__reads.push(url);
    if (url.startsWith("/api/node?")) {
      if (window.__failNode > 0) { window.__failNode--; throw new Error("timed out"); }
      const name = new URL(url, location.origin).searchParams.get("name");
      if (window.__hold === name) await new Promise(go => { window.__release = go; });
      const n = await read(asked, ...rest);
      return {...n, temps: {...(n.temps || {}), devices: devices(n.name)}};
    }
    const answer = await read(asked, ...rest);
    if (url === "/api/nodes") return answer.map(listRow);
    if (url === "/api/overview") return {...answer, nodes: (answer.nodes || []).map(listRow)};
    return answer;
  };
};
const results = page => page.locator("#hf_browse_results");

try {
  for (const width of [1440, 390]) {
    const page = await browser.newPage({viewport: {width, height: 1000}}), errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await openAuditPage(page, `${base}/?demo=1`);
    await page.evaluate(devicesOnlyInTheHostsRead);
    await page.evaluate(async () => { STATE.data.nodes = await api("/api/nodes"); STATE.data.ov = null; return hardwareFeatureEdit(""); });
    await page.waitForSelector("#hf_browse_node");
    assert.equal(await page.evaluate(() => STATE.data.nodes.some(n => n.temps?.devices || n.labels)), false,
      "the host list rows carry no devices and no labels");

    // The first host's devices, from its own read.
    await results(page).getByText("/dev/hailo-harvester-node1").waitFor();
    assert.match(await results(page).innerText(), /Coral USB on harvester-node1/);
    assert.deepEqual(await page.evaluate(() => window.__reads.filter(u => u.startsWith("/api/node?"))),
      ["/api/node?name=harvester-node1"]);

    // Filtering reads nothing again; choosing a device fills the form.
    await page.fill("#hf_browse_q", "hailo");
    await page.waitForFunction(() => !/Coral USB/.test(document.querySelector("#hf_browse_results").innerText));
    await results(page).getByRole("button", {name: /hailo-harvester-node1/}).click();
    assert.equal(await page.inputValue("#hf_host"), "/dev/hailo-harvester-node1");
    await page.fill("#hf_browse_q", "");

    // Another host: its own devices, read once.
    await page.selectOption("#hf_browse_node", "harvester-node2");
    await results(page).getByText("/dev/hailo-harvester-node2").waitFor();
    assert.doesNotMatch(await results(page).innerText(), /harvester-node1/);
    await page.selectOption("#hf_browse_node", "harvester-node1");
    await results(page).getByText("/dev/hailo-harvester-node1").waitFor();
    assert.equal(await page.evaluate(() => window.__reads.filter(u => u.startsWith("/api/node?")).length), 2,
      "a host already read is not read again while the editor is open");

    // A read that fails says so, and the next look tries again.
    await page.evaluate(() => { window.__failNode = 1; });
    await page.selectOption("#hf_browse_node", "harvester-node3");
    await results(page).getByText("The devices on this host could not be read").waitFor();
    assert.match(await results(page).innerText(), /timed out/);
    assert.doesNotMatch(await results(page).innerText(), /No matching device paths|has not reported/);
    await page.fill("#hf_browse_q", "dev");
    await results(page).getByText("/dev/hailo-harvester-node3").waitFor();

    // A late answer for a host no longer chosen does not paint over the newer choice.
    await page.evaluate(() => { HW_BROWSE.clear(); window.__hold = "harvester-node2"; });
    await page.selectOption("#hf_browse_node", "harvester-node2");
    await page.waitForFunction(() => typeof window.__release === "function");
    await page.selectOption("#hf_browse_node", "harvester-node1");
    await results(page).getByText("/dev/hailo-harvester-node1").waitFor();
    await page.evaluate(() => { window.__hold = null; window.__release(); window.__release = null; });
    await page.evaluate(() => HW_BROWSE.get("harvester-node2").then(() => new Promise(go => setTimeout(go, 50))));
    assert.match(await results(page).innerText(), /hailo-harvester-node1/);
    assert.doesNotMatch(await results(page).innerText(), /harvester-node2/, "the late answer is dropped");
    await page.evaluate(() => closeModal());

    // With every linked cluster's hosts in the list, the browser offers this
    // cluster's hosts only, and asks this cluster only about them.
    await page.evaluate(async () => {
      const own = await api("/api/nodes");
      STATE.data.nodes = [...own.map(n => ({...n, site: {id: "me", name: "Here", self: true}})),
        {...own[0], name: "shed-node1", site: {id: "shed1", name: "Shed", self: false}},
        {...own[1], site: {id: "shed1", name: "Shed", self: false}}];
      window.__reads = [];
      return hardwareFeatureEdit("");
    });
    await results(page).getByText("/dev/hailo-harvester-node1").waitFor();
    const offered = await page.locator("#hf_browse_node option").evaluateAll(o => o.map(x => x.value));
    assert.deepEqual(offered, ["harvester-node1", "harvester-node2", "harvester-node3"], "no linked cluster's host");
    assert.deepEqual(await page.evaluate(() => window.__reads.filter(u => u.startsWith("/api/node?"))),
      ["/api/node?name=harvester-node1"]);

    assert.deepEqual(errors, []);
    await page.evaluate(() => { window.api = window.__realApi; closeModal(); });
    await page.close();
    console.log(`Hardware browser reads each host's own devices at ${width}px`);
  }
} finally {
  await browser.close();
}
