// A read that failed is not an empty answer (#620): a page says it could not
// read, and offers nothing that acts on a guess.
import assert from "node:assert/strict";
import {chromium} from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4176";
const browser = await chromium.launch({headless: true});

// Fail one API path the way a timeout does; every other read goes through.
// Each call wraps the page's own api(), so only the latest path fails.
const failing = path => {
  window.__realApi = window.__realApi || window.api;
  const read = window.__realApi;
  window.api = (asked, ...rest) => String(asked).split("?")[0] === path
    ? Promise.reject(new Error("timed out")) : read(asked, ...rest);
};
const settled = page => page.waitForFunction(() => !document.querySelector("#mbody .spin2, #mbody .loading"));
// The same for one exact URL, query included (a page that reads two views of one path).
const failingExact = url => {
  window.__realApi = window.__realApi || window.api;
  const read = window.__realApi;
  window.api = (asked, ...rest) => String(asked) === url ? Promise.reject(new Error("timed out")) : read(asked, ...rest);
};

try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({viewport: {width, height: 1000}});
    const page = await context.newPage(), errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(`${base}/?demo=1`);
    await page.waitForSelector(".dashboard-widget");
    // Most steps call a page's view directly, so the app still thinks it is on
    // the Dashboard: its refresh tick would repaint the Dashboard over the page
    // a step is reading. The check drives every paint itself.
    await page.evaluate(() => clearInterval(window.__loopTimer));

    // Remove a host: unread hosts are not "every host is Ready".
    await page.evaluate(failing, "/api/nodes");
    await page.evaluate(() => clusterRemovePick());
    await page.waitForFunction(() => !document.querySelector("#mbody .spin2"));
    const hosts = await page.locator("#mbody").innerText();
    assert.match(hosts, /Could not read the hosts/);
    assert.match(hosts, /timed out/);
    assert.doesNotMatch(hosts, /Every host is Ready/);
    await page.evaluate(() => closeModal());

    // Data protection: unread backup storage is unknown, and "Set up" is not offered.
    await page.evaluate(failing, "/api/objectstore");
    await page.evaluate(() => viewProtect());
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Data protection"));
    const protect = await page.locator("body").innerText();
    assert.match(protect, /Backup storage\s*unknown/i);
    assert.equal(await page.getByRole("button", {name: "Set up storage"}).count(), 0, "Set up is not offered on a guess");
    assert.doesNotMatch(protect, /No backup storage/);

    // Volumes: a part that could not be read is named, not silently left out.
    await page.evaluate(failing, "/api/volumes/old-copies");
    await page.evaluate(() => viewStorage());
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Volumes"));
    assert.match(await page.locator("body").innerText(), /could not be read[\s\S]*old copies[\s\S]*timed out/i);

    // Create volume: no form that would make it on whatever class the server picks.
    await page.evaluate(failing, "/api/storageclasses");
    await page.evaluate(() => volumeCreate());
    await settled(page);
    const create = await page.locator("#mbody").innerText();
    assert.match(create, /Could not read the storage classes[\s\S]*timed out/);
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Create volume"}).count(), 0);
    await page.evaluate(() => closeModal());

    // Ownership: unread is not "nothing here declares a user".
    await page.evaluate(failing, "/api/volumes/ownership");
    await page.evaluate(() => volumeChown("lab", "frigate-config"));
    await settled(page);
    const owner = await page.locator("#mbody").innerText();
    assert.match(owner, /Could not read what uses this volume[\s\S]*timed out/);
    assert.doesNotMatch(owner, /Nothing here declares a user/);
    await page.evaluate(() => closeModal());

    // New storage class: unread disks are not "no disk has a tag yet".
    await page.evaluate(failing, "/api/disks");
    await page.evaluate(() => { STATE.data.disks = null; storageClassCreate(); });
    await page.waitForFunction(() => !document.querySelector("#sc_disktags")?.innerText.includes("loading"));
    const tags = await page.locator("#sc_disktags").innerText();
    assert.match(tags, /Could not read the disks[\s\S]*timed out/);
    assert.doesNotMatch(tags, /No disk has a tag yet/);
    await page.evaluate(() => closeModal());

    // Settings: the storage classes card says it could not read them, rather than vanish.
    await page.evaluate(() => { window.api = window.__realApi; settingsTab("cluster"); go("settings"); });
    await page.waitForSelector("#storageClassesCard", {state: "attached"});
    await page.evaluate(failing, "/api/storage/classes");
    await page.evaluate(() => storageClassesPaint());
    const card = page.locator("#storageClassesCard");
    assert.equal(await card.isVisible(), true, "the card stays");
    assert.match(await card.innerText(), /Could not read the storage classes[\s\S]*timed out/);

    // VMs and imports (views-lifecycle.js).
    const body = () => page.locator("#mbody").innerText();
    const close = () => page.evaluate(() => closeModal());

    // New LAN network: unread options are not "Multus is needed first".
    await page.evaluate(() => { window.api = window.__realApi; window.__vmCreateOptions = undefined; });
    await page.evaluate(failing, "/api/vm/create-options");
    await page.evaluate(() => vmNetworkAdd());
    await settled(page);
    assert.match(await body(), /Could not read the network options[\s\S]*timed out/);
    assert.doesNotMatch(await body(), /Multus is needed first/);
    await close();

    // New VM: no form built on invented options.
    await page.evaluate(() => vmNew());
    await settled(page);
    assert.match(await body(), /Could not read what a new VM can use[\s\S]*timed out/);
    assert.equal(await page.locator("#v_steps").count(), 0);
    await close();

    // Edit VM: no hosts, images or networks made up when its options are unread.
    await page.evaluate(() => vmEdit("default", "home-assistant-os"));
    await settled(page);
    assert.match(await body(), /Could not read what this VM can use[\s\S]*timed out/);
    assert.equal(await page.locator("#ve_strategy, #ve_node").count(), 0);
    await close();

    // New k3s cluster: the same, and the failed read is not kept as the options.
    await page.evaluate(() => k3sCluster());
    await settled(page);
    assert.match(await body(), /Could not read what a new k3s cluster can use[\s\S]*timed out/);
    assert.equal(await page.locator("#k_name").count(), 0);
    assert.equal(await page.evaluate(() => window.__vmCreateOptions), undefined);
    await close();

    // Remove an import: no removal offered without knowing what it created.
    await page.evaluate(failing, "/api/imports/cleanup-plan");
    await page.evaluate(() => importRemove("media-server", "succeeded"));
    await settled(page);
    assert.match(await body(), /Could not check what this import created[\s\S]*timed out/);
    assert.equal(await page.locator("#imr_workload, .imr-volume").count(), 0);
    await close();

    // A source's Docker containers that could not be listed say so.
    await page.evaluate(failing, "/api/sources/containers");
    await page.evaluate(async () => { STATE.data.srcs = await window.__realApi("/api/sources"); await srcBrowse("unraid"); });
    await page.waitForFunction(() => !document.querySelector("#mbody .spin2"));
    assert.match(await body(), /Could not list the Docker containers on unraid[\s\S]*timed out/);
    await close();

    // Import: no invented storage class or namespace list.
    await page.evaluate(failing, "/api/storageclasses");
    await page.evaluate(() => viewImport());
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Import"));
    assert.match(await page.locator("body").innerText(), /could not be read[\s\S]*storage classes[\s\S]*timed out/i);
    // Virtual machines › Import, where Import VM disk is offered: the same.
    await page.evaluate(() => viewVmImport());
    // The Import page's subtitle also names "Virtual machines": wait for this page's own title.
    await page.waitForFunction(() => document.querySelector(".phead h2")?.innerText.trim() === "Virtual machines");
    assert.match(await page.locator("body").innerText(), /could not be read[\s\S]*storage classes[\s\S]*timed out/i);
    await page.evaluate(() => vmDiskImport());
    assert.match(await body(), /Could not read the storage classes/);
    assert.equal(await page.locator("#vd_name").count(), 0);
    await close();

    // A VM from an Unraid server: no invented options or class.
    await page.evaluate(failing, "/api/vm/create-options");
    await page.evaluate(() => {
      STATE.data.importStorageClasses = null;
      STATE.data.uvms = { unraid: { vms: [{ id: "v1", name: "vm1", slug: "vm1" }] } };
      return uvmImport("unraid", "v1");
    });
    await settled(page);
    assert.match(await body(), /Could not read what this import needs[\s\S]*timed out/);
    await close();

    // The import wizard's storage step: no form when the storage options are unread.
    await page.evaluate(failing, "/api/deploy/options");
    await page.evaluate(() => importSetup("unraid", "media-server"));
    await settled(page);
    assert.match(await body(), /Could not read the storage options[\s\S]*timed out/);
    assert.equal(await page.locator("#im_steps").count(), 0);
    await close();

    // Edit an app: its placement, storage, neighbours and address come from
    // these reads; saving a form built without one would change what it was.
    for (const [path, what] of [["/api/nodes", "the hosts"], ["/api/deploy/options", "the storage options"],
      ["/api/workloads", "the other apps"], ["/api/network", "the network"]]) {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, path);
      // In one step, so a refresh cannot refill the list of apps in between.
      await page.evaluate(() => { STATE.data.wl = null; wlEdit("lab", "frigate", true); });
      await settled(page);
      assert.match(await body(), new RegExp(`Could not read ${what}[\\s\\S]*timed out`));
      assert.equal(await page.locator("#e_steps").count(), 0, `no edit form without ${what}`);
      await close();
    }

    // Deploy: no namespace, host list, storage or addresses made up for the form.
    for (const [path, what] of [["/api/namespaces", "the namespaces"], ["/api/nodes", "the hosts"],
      ["/api/deploy/options", "the storage options"], ["/api/network", "the network"]]) {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, path);
      await page.evaluate(() => go("deploy"));
      await page.waitForFunction(() => /Could not read|Basics/.test(document.querySelector("#views")?.innerText || ""));
      const deploy = await page.locator("#views").innerText();
      assert.match(deploy, new RegExp(`Could not read ${what}[\\s\\S]*timed out`));
      assert.equal(await page.locator("#d_target_mode").count(), 0, `no deploy form without ${what}`);
    }
    // Retry after a failed read opens the template that was asked for, not an empty form.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/namespaces");
    await page.evaluate(() => { window.__deployPrefill = { name: "from-template" }; go("deploy"); });
    await page.waitForFunction(() => /Could not read the namespaces/.test(document.querySelector("#views")?.innerText || ""));
    await page.evaluate(() => { window.api = window.__realApi; go("deploy"); });
    await page.waitForSelector("#d_target_mode");
    assert.equal(await page.evaluate(() => DCFG.workload_name), "from-template");

    // Import Docker Compose and New VM: no made-up namespace list.
    for (const [path, what] of [["/api/namespaces", "the namespaces"], ["/api/network", "the network"]]) {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, path);
      await page.evaluate(() => composeImport());
      await settled(page);
      assert.match(await body(), new RegExp(`Could not read ${what}[\\s\\S]*timed out`));
      assert.equal(await page.locator("#composeNs").count(), 0, `no Compose form without ${what}`);
      await close();
    }
    // New VM refuses where a guess would build the wrong VM; where a failed read
    // only hides some choices, the form opens and says what it could not read.
    for (const [path, what, disk, refuses] of [["/api/network", "the network", "", true],
      ["/api/vm-disks", "the imported disks", "win-disk", true],
      ["/api/vm-disks", "the imported disks", "", false], ["/api/namespaces", "the other namespaces", "", false]]) {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, path);
      await page.evaluate(disk => vmNew(disk, disk ? "lab" : ""), disk);
      await settled(page);
      assert.match(await body(), new RegExp(`Could not read ${what}[\\s\\S]*timed out`));
      assert.equal(await page.locator("#v_steps").count(), refuses ? 0 : 1, `${path} ${disk ? "with" : "without"} a chosen disk`);
      await close();
    }

    // Promoting the second container: a failed read leaves the form as it was,
    // not half switched to the other container's settings.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(() => go("deploy"));
    // go() first shows the last Deploy form it painted; wait for this visit's own.
    await page.waitForFunction(() => document.querySelector("#d_target_mode") && !document.querySelector("#views.refreshing"));
    const before = await page.evaluate(() => { deployAddContainer(); collect(); return JSON.stringify(DCFG); });
    await page.evaluate(failing, "/api/deploy/options");
    await page.evaluate(() => deployRemovePrimary());
    await page.waitForFunction(() => /Could not read the storage options/.test(document.querySelector("#toast")?.innerText || ""));
    assert.equal(await page.evaluate(() => JSON.stringify(DCFG)), before, "the form's settings are unchanged");

    // Addresses: an unread network is not "no addresses to choose from".
    const noNetwork = async () => {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, "/api/network");
    };
    await noNetwork();
    await page.evaluate(() => importSetup("unraid", "media-server"));
    await settled(page);
    assert.match(await body(), /Could not read the network[\s\S]*timed out/);
    assert.equal(await page.locator("#im_steps").count(), 0, "no import wizard without the network");
    await close();

    // With ServiceLB there is no address to choose, so the import does not need the network.
    const platform = await page.evaluate(() => JSON.stringify(STATE.platform || null));
    await noNetwork();
    await page.evaluate(() => { STATE.platform = { ...(STATE.platform || {}), load_balancer: "servicelb" };
      return importSetup("unraid", "media-server"); });
    await settled(page);
    assert.equal(await page.locator("#im_steps").count(), 1, "the import opens under ServiceLB");
    await close();
    await page.evaluate(platform => { STATE.platform = JSON.parse(platform); }, platform);

    await noNetwork();
    await page.evaluate(() => { STATE.platform = { ...(STATE.platform || {}), load_balancer: "kube-vip" };
      STATE.data.sambaInstalled = false; return sambaSet(true); });
    await settled(page);
    assert.match(await body(), /Could not read the network[\s\S]*timed out/);
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Install", exact: true}).count(), 0, "no Install for Samba");
    await close();

    await noNetwork();
    await page.evaluate(() => { STATE.platform = { ...(STATE.platform || {}), load_balancer: "kube-vip" };
      STATE.data.nfs = { installed: false, exports: [{ name: "media" }] }; return nfsSet(true); });
    await settled(page);
    assert.match(await body(), /Could not read the network[\s\S]*timed out/);
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Install NFS server"}).count(), 0);
    await close();

    // New share, when it would also install Samba: its address picker says so.
    await noNetwork();
    await page.evaluate(() => {
      const read = window.api;
      window.api = (path, ...rest) => String(path).split("?")[0] === "/api/shares/options"
        ? read(path, ...rest).then(options => ({ ...options, samba_installed: false })) : read(path, ...rest);
      return newShare();
    });
    await page.waitForFunction(() => document.querySelector("#sh_smb") && !document.querySelector("#sh_smb .spin2"));
    assert.match(await page.locator("#sh_smb").innerText(), /Could not read the network[\s\S]*timed out/);
    await close();
    await page.evaluate(platform => { STATE.platform = JSON.parse(platform); }, platform);
    await page.evaluate(() => { window.api = window.__realApi; });

    // A read that fails after you have left its page is dropped, like one that
    // answers late; a write still says it failed.
    const late = await page.evaluate(async () => {
      const realFetch = window.fetch, failures = [];
      window.fetch = () => new Promise((_, reject) => failures.push(reject));
      const read = window.__realApi("/api/network").then(() => "answered", () => "rejected");
      const write = window.__realApi("/api/network", { method: "POST", body: "{}" }).then(() => "answered", () => "rejected");
      window.NAV_TOKEN++;
      failures.forEach(reject => reject(new TypeError("connection reset")));
      window.fetch = realFetch;
      const later = new Promise(resolve => setTimeout(() => resolve("dropped"), 300));
      return [await Promise.race([read, later]), await Promise.race([write, later])];
    });
    assert.deepEqual(late, ["dropped", "rejected"]);

    // Edit an app: a pinned host that is cordoned stays its host, rather than
    // the form falling back to "any host" and Save unpinning it.
    await page.evaluate(() => {
      const read = window.__realApi;
      window.api = (path, ...rest) => {
        const base = String(path).split("?")[0];
        if (base === "/api/workload") return read(path, ...rest).then(w => ({ ...w, node: "harvester-node2" }));
        if (base === "/api/nodes") return read(path, ...rest).then(nodes => nodes.map(n => n.name === "harvester-node2" ? { ...n, schedulable: false } : n));
        return read(path, ...rest);
      };
      STATE.data.wl = null;
      return wlEdit("lab", "frigate", true);
    });
    await page.waitForSelector("#e_node", {state: "attached"});
    assert.equal(await page.locator("#e_node").inputValue(), "harvester-node2", "the cordoned pinned host stays chosen");
    assert.match(await page.locator("#e_node option:checked").innerText(), /not schedulable/);
    await close();

    // Turning autostart off for Homestead itself asks first, even when the
    // dialog fetched the list of apps itself.
    const asked = await page.evaluate(async () => {
      const read = window.__realApi, questions = [];
      window.api = (path, ...rest) => String(path).split("?")[0] === "/api/workload"
        ? read(path, ...rest).then(w => ({ ...w, priority: "self" })) : read(path, ...rest);
      STATE.data.wl = null;
      await wlEdit("lab", "frigate", true);
      STATE.data.wl = null;
      const realAsk = window.ask;
      window.ask = async message => { questions.push(message); return false; };
      $("#e_autostart").checked = false;
      await editSave("lab", "frigate");
      window.ask = realAsk;
      return questions.join("\n");
    });
    assert.match(asked, /Turning autostart off stops Homestead/);
    await close();
    await page.evaluate(() => { window.api = window.__realApi; });

    // Overview: unread metrics are not "waiting for recorded metrics", unread
    // storage is not "not available yet", and unread Longhorn limits are named
    // rather than read as "no host is nearly full".
    // Each waits for the page it asked for, not the one still showing.
    const dashAfter = async (path, expected) => {
      await page.evaluate(failing, path);
      await page.evaluate(() => { go("dash"); });
      await page.waitForSelector(".dashboard-widget");
      await page.waitForFunction(source => !document.querySelector("#views.refreshing")
        && new RegExp(source).test(document.querySelector("#views")?.innerText || ""), expected.source);
      return page.locator("#views").innerText();
    };
    const metrics = await dashAfter("/api/history", /Could not read the recorded metrics/);
    assert.match(metrics, /Could not read the recorded metrics[\s\S]*timed out/);
    assert.doesNotMatch(metrics, /Waiting for recorded metrics/);
    const storage = await dashAfter("/api/storage", /Could not read storage/);
    assert.match(storage, /Could not read storage[\s\S]*timed out/);
    assert.doesNotMatch(storage, /Storage figures aren.t available yet/);
    const limits = await dashAfter("/api/longhorn/capacity", /Could not read Longhorn.s allocation limits/);
    assert.match(limits, /Could not read Longhorn.s allocation limits[\s\S]*timed out/);
    // Where it is not known that Longhorn is there, its limits are not reported unread.
    const knownPlatform = await page.evaluate(() => { const was = STATE.platform; STATE.platform = null; return was; });
    const unknown = await dashAfter("/api/longhorn/capacity", /Compute[\s\S]*Storage/);
    assert.doesNotMatch(unknown, /Could not read Longhorn.s allocation limits/);
    await page.evaluate(was => { STATE.platform = was; }, knownPlatform);

    // Hosts: one failed uptime read keeps the last good strips and says so,
    // in text a phone shows, not only in a tip.
    await page.evaluate(() => { window.api = window.__realApi; return viewNodes(); });
    await page.waitForFunction(() => document.querySelector(".node-upstrip .uptime-strip i[class^='uptime-']"));
    await page.evaluate(failing, "/api/nodes/uptime");
    await page.evaluate(() => viewNodes());
    await page.waitForFunction(() => /Could not read uptime/.test(document.querySelector("#views")?.innerText || ""));
    assert.ok(await page.locator(".node-upstrip .uptime-strip i[class^='uptime-']").count(), "the last good strips stay");
    assert.match(await page.locator(".node-upstrip").first().innerText(), /Could not read uptime[\s\S]*timed out/);

    // Hosts: unread uptime is not "no samples yet", on the list and on a host's page.
    await page.evaluate(failing, "/api/nodes/uptime");
    await page.evaluate(() => { STATE.data.nodeUptime = null; return viewNodes(); });
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Hosts"));
    const tips = await page.evaluate(() => [...document.querySelectorAll(".node-upstrip")].map(x => x.dataset.tip));
    assert.ok(tips.length, "the host list shows uptime strips");
    for (const tip of tips) {
      assert.match(tip, /Could not read uptime: timed out/);
      assert.doesNotMatch(tip, /No samples yet/);
    }
    await page.evaluate(async () => {
      document.querySelector("#views").insertAdjacentHTML("beforeend", '<div id="nodeUptime"></div>');
      await nodeUptimePaint({ name: "harvester-node1", status: "Ready" });
    });
    const hostUptime = await page.locator("#nodeUptime").innerText();
    assert.match(hostUptime, /Could not read uptime[\s\S]*timed out/);
    assert.doesNotMatch(hostUptime, /No samples yet/);

    // Hardware feature editor: unread hosts are named, not an empty host list
    // with "no probe data"; the paths can still be typed in.
    await page.evaluate(failing, "/api/nodes");
    await page.evaluate(() => { STATE.data.nodes = null; STATE.data.ov = null; return hardwareFeatureEdit(""); });
    await page.waitForSelector("#hf_id");
    const hardware = await page.locator("#mbody").innerText();
    assert.match(hardware, /Could not read the hosts[\s\S]*timed out/);
    assert.doesNotMatch(hardware, /No probe data from this host yet/);
    assert.equal(await page.locator("#hf_browse_node").count(), 0, "no host list built on nothing");
    assert.equal(await page.locator("#hf_host").count(), 1, "the path can still be typed");
    await page.evaluate(() => { window.api = window.__realApi; closeModal(); });

    // Data protection: unread backups are not "no backups on the target yet".
    await page.evaluate(failing, "/api/lh/backupvolumes");
    await page.evaluate(() => viewProtect());
    await page.waitForFunction(() => !document.querySelector("#views.refreshing")
      && /Could not read the backups/.test(document.querySelector("#views")?.innerText || ""));
    const backups = await page.locator("#views").innerText();
    assert.match(backups, /Could not read the backups[\s\S]*timed out/);
    assert.doesNotMatch(backups, /No backups on the target yet|No backup target, so no backups/);

    // A volume's snapshots: its unread backups are not "no backups".
    await page.evaluate(failing, "/api/lh/backups");
    await page.evaluate(() => lhSnaps("pvc-demo-frigate", "frigate"));
    await settled(page);
    const snaps = await page.locator("#mbody").innerText();
    assert.match(snaps, /Could not read this volume's backups[\s\S]*timed out/);
    assert.doesNotMatch(snaps, /no backups — needs a backup target/);
    await page.evaluate(() => closeModal());

    // Storage classes: a V2 class with Longhorn V2 unread says so, not nothing.
    await page.evaluate(failing, "/api/storage/v2");
    await page.evaluate(async () => {
      document.querySelector("#views").insertAdjacentHTML("beforeend", '<div id="storageClassesCard"></div>');
      await storageClassesPaint();
    });
    assert.match(await page.locator("#storageClassesCard").innerText(), /Longhorn V2[\s\S]*Could not read whether it is on[\s\S]*timed out/);
    // With no class on V2, an unread V2 answer is not worth a line.
    await page.evaluate(async () => {
      const read = window.__realApi;
      window.api = (asked, ...rest) => {
        const path = String(asked).split("?")[0];
        if (path === "/api/storage/v2") return Promise.reject(new Error("timed out"));
        if (path === "/api/storage/classes") return read(asked, ...rest).then(rows => rows.filter(row => row.engine !== "v2"));
        return read(asked, ...rest);
      };
      await storageClassesPaint();
    });
    assert.doesNotMatch(await page.locator("#storageClassesCard").innerText(), /Longhorn V2|Could not read whether it is on/);

    // Network shares: unread SMB users are not "no SMB users yet".
    await page.evaluate(failing, "/api/shares/users");
    await page.evaluate(() => viewShares());
    await page.waitForFunction(() => !document.querySelector("#views.refreshing")
      && document.querySelector(".phead")?.innerText.includes("Network shares"));
    const shares = await page.locator("#views").innerText();
    assert.match(shares, /Could not read the SMB users[\s\S]*timed out/);
    assert.doesNotMatch(shares, /No SMB users yet/);

    // Longhorn settings: the detached-volume rebuild setting, unread, is named, not left out.
    await page.evaluate(failing, "/api/longhorn/offline-rebuilding");
    await page.evaluate(async () => {
      document.querySelector("#views").insertAdjacentHTML("beforeend", '<div id="lhSettingsCard"></div>');
      await lhSettingsPaint();
    });
    assert.match(await page.locator("#lhSettingsCard").innerText(), /Could not read whether Longhorn rebuilds copies of detached volumes[\s\S]*timed out/);

    // Disk and node tags: the editor opens, and says the tags in use could not be read.
    await page.evaluate(failing, "/api/disks");
    for (const open of [() => diskTags("harvester-node1", "disk-1", ["ssd"]), () => nodeTags("harvester-node1", [])]) {
      await page.evaluate(() => { STATE.data.disks = null; });
      await page.evaluate(open);
      await page.waitForSelector("#tg_text");
      assert.match(await page.locator("#mbody").innerText(), /Could not read the tags already in use[\s\S]*timed out/);
      await page.evaluate(() => closeModal());
    }

    // Changing a storage class: a poll that fails says so and keeps trying.
    await page.evaluate(failing, "/api/operations");
    await page.evaluate(() => reclassWatch("op1"));
    await page.waitForFunction(() => /Could not read the job/.test(document.querySelector("#mbody")?.innerText || ""));
    assert.match(await page.locator("#mbody").innerText(), /Could not read the job[\s\S]*timed out[\s\S]*still trying/);
    // Nothing was shown yet, so the dialog offers a way out.
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Close"}).count(), 1, "a first failed read can be closed");
    await page.evaluate(() => { clearInterval(window.__reclassTimer); window.api = window.__realApi; closeModal(); });

    // A good poll, then a failed one: the steps shown stay, with the note under them.
    await page.evaluate(() => reclassWatch("op4"));
    await page.waitForSelector("#mbody .rc-steps");
    await page.evaluate(failing, "/api/operations");
    await page.waitForSelector("#rc_unread", {timeout: 10000});
    assert.ok(await page.locator("#mbody .rc-steps").count(), "the last view stays");
    assert.match(await page.locator("#rc_unread").innerText(), /Could not read the job[\s\S]*timed out[\s\S]*still trying/);
    // The next good poll takes the note away.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.waitForFunction(() => !document.querySelector("#rc_unread"), null, {timeout: 10000});
    await page.evaluate(() => { clearInterval(window.__reclassTimer); closeModal(); });

    // Apps (part 9). The image cache: unread VM images are said, not left out.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/images/vm");
    await page.evaluate(() => viewImages());
    await page.waitForFunction(() => /Could not read the VM images/.test(document.body.innerText), null, {timeout: 10000});
    assert.match(await page.locator("body").innerText(), /Could not read the VM images[\s\S]*timed out/);

    // Import: unread hosts and storage class details are named with the rest.
    for (const [setup, arg, what] of [[failing, "/api/nodes", "hosts"],
      [failingExact, "/api/storageclasses?facts=1", "storage class details"]]) {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(setup, arg);
      await page.evaluate(() => viewImport());
      await page.waitForFunction(w => document.body.innerText.includes(`${w}: timed out`), what, {timeout: 10000});
    }

    // Choosing another namespace: unread volumes there are not the old namespace's.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/deploy/options");
    const left = await page.evaluate(async () => {
      STATE.data.importStorage = {storage_classes: ["longhorn"], pvcs: [{name: "old-claim"}]};
      document.body.insertAdjacentHTML("beforeend", '<select id="im_ns"><option value="other" selected>other</option></select>'
        + '<datalist id="im_pvc_list"><option value="old-claim"></option></datalist>');
      await imNamespace("other");
      const found = {pvcs: STATE.data.importStorage.pvcs.length,
        options: document.querySelectorAll("datalist#im_pvc_list option").length};
      document.querySelector("#im_ns").remove(); document.querySelector("datalist#im_pvc_list").remove();
      return found;
    });
    assert.deepEqual(left, {pvcs: 0, options: 0}, "another namespace's volumes are not offered");
    await page.waitForFunction(() => /Could not read the volumes in other/.test(document.body.innerText), null, {timeout: 5000});

    // The import wizard: hosts not read just now are said; the hardware shown is from earlier.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/nodes");
    await page.evaluate(() => importSetup("unraid", "media-server"));
    await settled(page);
    assert.match(await body(), /Could not read the hosts just now[\s\S]*timed out/);
    await close();

    // If a host fails: Longhorn's policy unread is said, not left out.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/longhorn/capacity");
    await page.evaluate(() => wlFailover());
    await settled(page);
    assert.match(await body(), /Could not read Longhorn's node-failure policy[\s\S]*timed out/);
    await close();

    // If a host fails: an unread container list is said, and nothing is saved against an old one.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/workloads");
    await page.evaluate(() => wlFailover());
    await settled(page);
    assert.match(await body(), /Could not read the containers[\s\S]*timed out/);
    assert.equal(await page.locator("#mbody select[data-fo]").count(), 0, "no policy is offered against an old list");
    assert.equal(await page.locator("#mbody button", {hasText: "Save changes"}).count(), 0, "Save is not offered");
    await close();

    // A container's LAN address: unread networks are not "No LAN network yet", and the failure is not kept.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/vm/create-options");
    const lan = await page.evaluate(async () => {
      const kept = window.__vmCreateOptions; delete window.__vmCreateOptions;
      const html = await containerLanFields("t");
      const cached = window.__vmCreateOptions !== undefined;
      window.__vmCreateOptions = kept;
      return {html, cached};
    });
    assert.match(lan.html, /Could not read the networks[\s\S]*timed out/);
    assert.doesNotMatch(lan.html, /No LAN network/);
    assert.equal(lan.cached, false, "a failed read is not kept as the answer");

    // Checking images: a progress read that fails says so while the check goes on.
    await page.evaluate(() => { window.api = window.__realApi; go("workloads"); });
    await page.waitForSelector("#scanprogress", {state: "attached"});
    await page.evaluate(failing, "/api/image-updates/scan-progress");
    await page.evaluate(() => {
      window.__realLoadUpdates = window.loadImageUpdates;
      window.loadImageUpdates = () => new Promise(resolve => { window.__finishScan = resolve; });
      checkImageUpdates();
    });
    await page.waitForFunction(() => /could not read the progress/i.test(document.querySelector("#scanprogress")?.innerText || ""),
      null, {timeout: 10000});
    await page.evaluate(() => { window.loadImageUpdates = window.__realLoadUpdates; window.__finishScan(null); window.api = window.__realApi; });

    // Part 10. After removing a host: unread leftovers are not "the cluster is clean".
    await page.evaluate(failing, "/api/cluster/cleanup");
    const leftovers = await page.evaluate(async () => {
      document.body.insertAdjacentHTML("beforeend", '<div id="rm_left"></div>');
      await removalLeftovers();
      const text = document.querySelector("#rm_left").innerText;
      document.querySelector("#rm_left").remove();
      return text;
    });
    assert.match(leftovers, /Could not read what is left over[\s\S]*timed out/);
    assert.doesNotMatch(leftovers, /Nothing is left over/);

    // The Cluster page's cleanup card: unread is said, and the last card read is not drawn again as current.
    const cleanup = await page.evaluate(async () => {
      STATE.data.cleanupHtml = "<b>an old card</b>";
      document.body.insertAdjacentHTML("beforeend", '<div id="clusterCleanup"></div>');
      await clusterCleanupPaint();
      const text = document.querySelector("#clusterCleanup").innerText;
      document.querySelector("#clusterCleanup").remove();
      return {text, kept: STATE.data.cleanupHtml || ""};
    });
    assert.match(cleanup.text, /Could not read what hosts left behind[\s\S]*timed out/);
    assert.equal(cleanup.kept, "", "neither the old card nor the error is drawn first next time");

    // Adding a host: unread hosts are not "Waiting for a new host".
    await page.evaluate(failing, "/api/nodes");
    const watching = await page.evaluate(async () => {
      modal("Add a host", '<div id="guideWatch"></div>');
      ONBOARD.known = new Set();
      await guideWatch();
      clearTimeout(ONBOARD.timer);
      return document.querySelector("#guideWatch").innerText;
    });
    assert.match(watching, /Could not read the hosts[\s\S]*timed out/);
    assert.doesNotMatch(watching, /Waiting for a new host/);
    await close();

    // Files: unread volumes are said in the volume picker, not an old list.
    await page.evaluate(() => { window.api = window.__realApi; FM.volumes = [{namespace: "lab", pvc: "an-old-volume"}]; });
    await page.evaluate(failing, "/api/volumes");
    await page.evaluate(() => { fileManager("lab", "frigate-config"); });
    await page.waitForSelector("#fm_pane0 select");
    await page.waitForFunction(() => /Could not read the other volumes/.test(document.querySelector("#fm_pane0 select")?.innerHTML || ""),
      null, {timeout: 10000});
    const picker = await page.locator("#fm_pane0 select").innerHTML();
    assert.match(picker, /Could not read the other volumes[\s\S]*timed out/);
    assert.doesNotMatch(picker, /an-old-volume/);
    await page.evaluate(() => fmClose());

    // Portal: links not checked are not shown with an old answer.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/portal/status");
    await page.evaluate(async () => {
      const links = (await api("/api/portal")).links || [];
      STATE.data.portalStatus = Object.fromEntries(links.map(l => [l.id, {up: true, ms: 1}]));
      viewPortal();
    });
    await page.waitForFunction(() => /could not check/i.test(document.querySelector(".portal-dot")?.dataset.tip || ""), null, {timeout: 10000});
    assert.equal(await page.locator('.portal-dot[data-tip^="answering"]').count(), 0, "no old answer is shown");
    // Coming back: the earlier failure is not said while the new check is on its way.
    const revisit = await page.evaluate(() => {
      window.api = window.__realApi;
      const real = window.api; let answer;
      window.api = (asked, ...rest) => String(asked) === "/api/portal/status" ? new Promise(r => { answer = r; }) : real(asked, ...rest);
      return viewPortal().then(() => { const tip = document.querySelector(".portal-dot")?.dataset.tip || ""; answer({}); window.api = real; return tip; });
    });
    assert.doesNotMatch(revisit, /could not check/i);

    // Portal links editor: unread containers are said (their logos are not offered).
    await page.evaluate(() => { window.api = window.__realApi; STATE.data.wl = null; });
    await page.evaluate(failing, "/api/workloads");
    await page.evaluate(() => portalEdit());
    await settled(page);
    assert.match(await body(), /Could not read the containers[\s\S]*timed out/);
    await close();

    // Schedule: an unread last run is said, not left out (it may have been refused).
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(async () => { STATE.data.vms = await api("/api/vms"); });
    await page.evaluate(failing, "/api/power-schedules");
    await page.evaluate(() => vmSchedule("lab", "k3s-demo-server-1"));
    await page.waitForFunction(() => (document.querySelector("#sc_last")?.innerText || "").length, null, {timeout: 5000});
    assert.match(await page.locator("#sc_last").innerText(), /Could not read how the last stop or start went[\s\S]*timed out/);
    await close();

    // Schedule opened where the VMs were not read: its schedule is read, never the defaults offered in its place.
    await page.evaluate(() => { STATE.data.vms = null; });
    await page.evaluate(() => vmSchedule("lab", "k3s-demo-server-1"));
    await settled(page);
    assert.match(await body(), /Could not read this schedule[\s\S]*timed out/);
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Save"}).count(), 0, "nothing is saved over a schedule not read");
    await close();
    await page.evaluate(() => { window.api = window.__realApi; STATE.data.vms = null; });
    await page.evaluate(() => vmSchedule("lab", "k3s-demo-server-1"));
    await page.waitForSelector("#sc_stop");
    assert.equal(await page.locator("#sc_stop").inputValue(), "19:00", "its own schedule, read from the schedules");
    await close();
    // One the schedules read does not list has none: the defaults and Save are right there.
    await page.evaluate(() => { STATE.data.wl = null; });
    await page.evaluate(() => wlSchedule("lab", "home-assistant"));
    await page.waitForSelector("#sc_stop");
    assert.equal(await page.locator("#sc_stop").inputValue(), "01:00");
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Save"}).count(), 1);
    await close();
    // A schedule read that answers after its dialog was closed does not open it again.
    const reopened = await page.evaluate(async () => {
      const real = window.api; let answer;
      window.api = (asked, ...rest) => String(asked) === "/api/power-schedules" ? new Promise(r => { answer = r; }) : real(asked, ...rest);
      STATE.data.vms = null;
      vmSchedule("lab", "k3s-demo-server-1");
      closeModal();
      answer({items: []}); await new Promise(r => setTimeout(r, 50));
      window.api = real;
      return !$("#modal").classList.contains("hidden");
    });
    assert.equal(reopened, false, "a closed schedule dialog stays closed");

    // Resources: an object's unread events are said, not "loading" for ever.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(() => viewResources());
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Resources"));
    await page.evaluate(failing, "/api/resources/events");
    await page.evaluate(() => { const r = STATE.data.resList.rows[0]; return resOpen(r.namespace, r.name); });
    await page.waitForFunction(() => document.querySelector("#res_events")?.textContent !== "loading", null, {timeout: 5000});
    assert.match(await page.evaluate(() => document.querySelector("#res_events").textContent), /Could not read the events[\s\S]*timed out/);
    await close();

    // Resources: an unread list is not "None."
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/resources/list");
    await page.evaluate(() => viewResources());
    await page.waitForFunction(() => /timed out/.test(document.querySelector(".res-main")?.textContent || ""));
    assert.doesNotMatch(await page.locator(".res-main").innerText(), /None\./);
    assert.match(await page.locator(".res-main").innerText(), /Could not read them/);

    // Resources: unread namespaces are said, not kept as none, and Create does not offer an empty choice.
    await page.evaluate(() => { window.api = window.__realApi; STATE.data.namespacesAll = undefined; });
    await page.evaluate(failingExact, "/api/resources/list?group=&version=v1&resource=namespaces");
    await page.evaluate(() => viewResources());
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Resources"));
    assert.match(await page.locator(".res-main").textContent(), /Could not read the namespaces/);
    await page.evaluate(() => resCreate());
    assert.match(await body(), /Could not read the namespaces[\s\S]*timed out/);
    assert.equal(await page.locator("#rc_ns").evaluate(e => e.tagName), "INPUT", "the namespace is typed, not picked from an unread list");
    await close();
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(() => viewResources());
    assert.ok(await page.evaluate(() => (STATE.data.namespacesAll || []).length > 0), "the failure is not kept: the next visit reads them");

    // Monitoring: unread checks are said, not "First checks under way".
    await page.evaluate(() => { window.api = window.__realApi; STATE.data.uptime = undefined; });
    await page.evaluate(failing, "/api/uptime");
    await page.evaluate(() => viewMonitoring());
    await page.waitForFunction(() => document.querySelector(".phead")?.innerText.includes("Monitoring"));
    const monitor = await page.locator("body").innerText();
    assert.match(monitor, /Could not read the checks[\s\S]*timed out/);
    assert.doesNotMatch(monitor, /First checks under way/);
    assert.doesNotMatch(await page.locator(".phead").innerText(), /\b0 up\b/, "no count of checks not read");
    assert.match(await page.evaluate(() => monitorWidget()), /Could not read the checks/, "the widget does not spin for ever");
    await page.evaluate(() => wlMonitoring("lab", "paperless"));
    assert.match(await body(), /Could not read the checks[\s\S]*timed out/);
    assert.doesNotMatch(await body(), /First checks under way/);
    await close();
    // Checks read before are kept, and said to be from before.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(() => loadUptime());
    await page.evaluate(failing, "/api/uptime");
    await page.evaluate(() => viewMonitoring());
    await page.waitForFunction(() => /Could not read the checks just now/.test(document.body.innerText), null, {timeout: 5000});
    assert.match(await page.locator("body").innerText(), /Could not read the checks just now[\s\S]*timed out/);
    // The Containers page says so too: its Down and Slow marks are from the last read.
    await page.evaluate(() => viewWorkloads());
    await page.waitForFunction(() => /Could not read the checks just now/.test(document.querySelector(".containers-page")?.innerText || ""), null, {timeout: 5000});

    // Monitoring: unread VMs are said, not left out.
    await page.evaluate(() => { window.api = window.__realApi; STATE.data.vms = undefined; });
    await page.evaluate(failing, "/api/vms");
    await page.evaluate(() => viewMonitoring());
    await page.waitForFunction(() => /Could not read the VMs/.test(document.body.innerText), null, {timeout: 5000});
    assert.match(await page.locator("body").innerText(), /Could not read the VMs[\s\S]*timed out/);
    // The dashboard's widget reads them the same way: unread VMs are not stored as none.
    await page.evaluate(() => { STATE.data.vms = undefined; });
    // The widget is optional and off in the demo's layout, so its read is checked by its source and run directly.
    assert.doesNotMatch(await page.evaluate(() => Dashboard.loadMonitoring.toString()), /\.catch\(\(\) => \[\]\)/);
    await page.evaluate(() => loadMonitorLists());
    assert.equal(await page.evaluate(() => Array.isArray(STATE.data.vms)), false, "the dashboard keeps no empty list for unread VMs");
    assert.match(await page.evaluate(() => monitorWidget()), /Could not read the VMs/);
    await page.evaluate(() => vmMonitoring("lab", "k3s-demo-server-1"));
    assert.match(await body(), /Could not read k3s-demo-server-1's settings/);
    await close();

    // Monitoring: the apps' refresh failing marks the list as the one last read.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(async () => { STATE.data.wl = await api("/api/workloads"); });
    await page.evaluate(failing, "/api/workloads");
    // The refresh repaints only the Monitoring page, so the app is told it is there.
    await page.evaluate(() => { window.__view = STATE.view; STATE.view = "monitoring"; return viewMonitoring(); });
    await page.waitForFunction(() => /Could not read the apps just now/.test(document.body.innerText), null, {timeout: 5000});
    assert.match(await page.locator("body").innerText(), /Could not read the apps just now[\s\S]*timed out/);
    await page.evaluate(() => { STATE.view = window.__view; });

    // A monitoring dialog for one not in a read list: nothing is saved over settings not read.
    await page.evaluate(() => { window.api = window.__realApi; STATE.data.vms = null; });
    await page.evaluate(() => vmMonitoring("lab", "k3s-demo-server-1"));
    assert.match(await body(), /Could not read k3s-demo-server-1's settings/);
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Save"}).count(), 0);
    await close();
    // In a list that was read but lacks it, it is not found; still no Save.
    await page.evaluate(() => { STATE.data.vms = []; });
    await page.evaluate(() => vmMonitoring("lab", "k3s-demo-server-1"));
    assert.match(await body(), /k3s-demo-server-1 was not found[\s\S]*VMs last read/);
    assert.equal(await page.locator("#mbody").getByRole("button", {name: "Save"}).count(), 0);
    await close();
    await page.evaluate(() => { STATE.data.vms = null; });

    // Settings › Notifications: alerts that could not be refreshed are said, not shown as current.
    await page.evaluate(() => { window.api = window.__realApi; });
    const alerts = await page.evaluate(async () => {
      // The 20-second check is stopped and any read in flight is waited for, so the failing read is this step's own.
      clearInterval(PWA_ALERTS.timer); await PWA_ALERTS.request;
      document.body.insertAdjacentHTML("beforeend", '<div id="pwaAlerts"></div>');
      await refreshPwaAlerts(true);
      const real = window.api;
      window.api = (asked, ...rest) => String(asked).split("?")[0] === "/api/alerts" ? Promise.reject(new Error("timed out")) : real(asked, ...rest);
      await refreshPwaAlerts(true);
      const text = document.querySelector("#pwaAlerts").innerText;
      window.api = real; document.querySelector("#pwaAlerts").remove();
      startAlertChecks(); await PWA_ALERTS.request;
      return text;
    });
    assert.match(alerts, /Could not read the alerts just now[\s\S]*timed out/);

    // Platform pages, on a cluster that is not Harvester.
    await page.goto(`${base}/?demo=1&platform=k3s`);
    await page.waitForSelector(".dashboard-widget");
    await page.evaluate(() => clearInterval(window.__loopTimer));

    // Network overview: unread required components are said, not left out.
    await page.evaluate(failing, "/api/platform/baseline");
    await page.evaluate(() => { history.replaceState(null, "", "?demo=1&platform=k3s&tab=overview"); viewNetworking(); });
    await page.waitForFunction(() => /Could not check the required components/.test(document.body.innerText), null, {timeout: 10000});
    assert.match(await page.locator("body").innerText(), /Could not check the required components[\s\S]*timed out/);

    // LAN networks: unread is not "No LAN networks yet"; a list read before is kept, and said to be from before.
    await page.evaluate(() => { window.api = window.__realApi; });
    await page.evaluate(failing, "/api/vm/create-options");
    const nets = await page.evaluate(async () => {
      const kept = window.__vmCreateOptions;
      document.body.insertAdjacentHTML("beforeend", '<div id="netVmNets"></div>');
      window.__vmCreateOptions = undefined;
      await networkVmNetsPaint();
      const none = document.querySelector("#netVmNets").innerText;
      window.__vmCreateOptions = {network_details: [{name: "lan-old", lan: true, type: "macvlan"}]};
      await networkVmNetsPaint();
      const old = document.querySelector("#netVmNets").innerText;
      document.querySelector("#netVmNets").remove();
      window.__vmCreateOptions = kept;
      return {none, old};
    });
    assert.match(nets.none, /Could not read the LAN networks[\s\S]*timed out/);
    assert.doesNotMatch(nets.none, /No LAN networks yet/);
    assert.match(nets.old, /lan-old[\s\S]*Could not read the LAN networks just now[\s\S]*timed out/);

    // Add-ons (Settings › Cluster): each part that could not be read is said.
    const addons = async (path, words) => {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, path);
      const text = await page.evaluate(async () => {
        document.body.insertAdjacentHTML("beforeend", '<div id="addonsCard" hidden></div>');
        await addonsPaint();
        const card = document.querySelector("#addonsCard");
        const shown = card.hidden ? "" : card.innerText;
        card.remove();
        return shown;
      });
      assert.match(text, new RegExp(`${words}[\\s\\S]*timed out`), path);
      return text;
    };
    await addons("/api/addons", "Could not read the add-ons");
    const health = await addons("/api/self/health", "Could not read the node probe and the SMB server");
    assert.doesNotMatch(health, /Install node probe/, "nothing is installed on a guess");
    assert.doesNotMatch(health, /0 configured|address not assigned/, "the SMB server's shares and address are not read as none");
    await addons("/api/platform/baseline", "Could not check the required components");
    await addons("/api/host-console", "Could not read the host console");

    // On Harvester, known from the add-ons answer, no required components are checked for.
    await page.evaluate(() => { window.api = window.__realApi; });
    const harvester = await page.evaluate(async () => {
      const real = window.api, platform = STATE.platform;
      window.api = (asked, ...rest) => String(asked) === "/api/addons" ? Promise.resolve({harvester: true})
        : String(asked) === "/api/platform/baseline" ? Promise.reject(new Error("timed out")) : real(asked, ...rest);
      STATE.platform = null;
      document.body.insertAdjacentHTML("beforeend", '<div id="addonsCard" hidden></div>');
      await addonsPaint();
      const text = document.querySelector("#addonsCard").innerText;
      document.querySelector("#addonsCard").remove();
      window.api = real; STATE.platform = platform;
      return text;
    });
    assert.doesNotMatch(harvester, /Could not check the required components/);

    // Homestead's own address (Network overview): unread is said; what was read before is marked as such.
    await page.evaluate(() => { window.api = window.__realApi; });
    const self = await page.evaluate(async () => {
      document.querySelectorAll("#selfAddress").forEach(el => el.remove());
      document.body.insertAdjacentHTML("beforeend", '<div id="selfAddress"></div>');
      const kept = STATE.data.selfAddress; STATE.data.selfAddress = null;
      const real = window.api;
      window.api = (asked, ...rest) => String(asked).split("?")[0] === "/api/self/address" ? Promise.reject(new Error("timed out")) : real(asked, ...rest);
      await selfAddressPaint();
      const none = document.querySelector("#selfAddress").innerText;
      window.api = real; await selfAddressPaint();
      window.api = (asked, ...rest) => String(asked).split("?")[0] === "/api/self/address" ? Promise.reject(new Error("timed out")) : real(asked, ...rest);
      await selfAddressPaint();
      const old = document.querySelector("#selfAddress").innerText;
      document.querySelector("#selfAddress").remove(); window.api = real; STATE.data.selfAddress = kept;
      return {none, old};
    });
    assert.match(self.none, /Could not read where Homestead answers[\s\S]*timed out/);
    assert.match(self.old, /Could not read where Homestead answers just now[\s\S]*timed out/);

    // Installing KubeVirt or kube-vip: an unread host check is said in the question, not taken as "fine".
    for (const [key, words] of [["kubevirt", "Could not check whether the hosts have hardware virtualisation"],
      ["kube-vip", "Could not read which network interface the hosts use"]]) {
      await page.evaluate(() => { window.api = window.__realApi; });
      await page.evaluate(failing, "/api/addons");
      await page.evaluate(k => { addonInstall(k); }, key);
      await page.waitForSelector(".askdlg");
      assert.match(await page.locator(".askdlg .askmsg").innerText(), new RegExp(`${words}[\\s\\S]*timed out`));
      await page.locator('.askdlg [data-a="no"]').click();
    }
    await page.evaluate(() => { window.api = window.__realApi; });

    // Part 12 (#620): Settings, linked clusters, search, the Dashboard and the last silent reads.
    const real = () => page.evaluate(() => { window.api = window.__realApi || window.api; });
    const settingsCardText = title => page.evaluate(t => [...document.querySelectorAll("#views section.card")]
      .find(s => s.textContent.includes(t))?.textContent || "", title);
    const settingsWith = async path => {
      await real();
      await page.evaluate(failing, path);
      // Started, then waited for: awaiting viewSettings() inside evaluate now and then
      // reports "Execution context was destroyed" with no navigation (on dev too).
      await page.evaluate(() => { window.__view = STATE.view; STATE.view = "settings"; window.__settingsPainted = false;
        viewSettings().finally(() => { window.__settingsPainted = true; }); });
      await page.waitForFunction(() => window.__settingsPainted && document.querySelector(".phead")?.innerText.includes("Settings"));
      // The page stays painted for the checks; the view goes back to what it was.
      await page.evaluate(() => { STATE.view = window.__view; });
    };

    // Settings › Hardware features and About: unread hosts are not "no host detected", nor an older read as current.
    await page.evaluate(() => { window.__ov = STATE.data.ov; STATE.data.ov = null; STATE.data.nodes = null; });
    await settingsWith("/api/overview");
    const hardwareCard = await settingsCardText("Hardware features");
    assert.match(hardwareCard, /Could not read the hosts[\s\S]*timed out/);
    assert.doesNotMatch(hardwareCard, /no host detected/);
    assert.match(await settingsCardText("Hosts ready"), /Hosts ready\s*not read/);
    await page.evaluate(() => { STATE.data.ov = { nodes: [{ name: "host-old", hardware: Object.fromEntries((STATE.data.hardwareFeatures || []).map(f => [f.id, true])) }] }; });
    await settingsWith("/api/overview");
    assert.match(await settingsCardText("Hardware features"), /Could not read the hosts just now[\s\S]*timed out[\s\S]*from the last read/);
    await page.evaluate(() => { STATE.data.ov = window.__ov; });

    // Settings › Users and roles: unread users are said, not an empty table.
    await settingsWith("/api/auth/users");
    assert.match(await settingsCardText("Users and roles"), /Could not read the users[\s\S]*timed out/);

    // Settings › How hosts share the work: unread hosts are said, not "No hosts reported" or "Every app reserves something".
    await settingsWith("/api/placement");
    assert.match(await page.evaluate(() => document.querySelector("#set_standby_row").textContent), /Could not read the hosts[\s\S]*timed out/);
    const spread = await page.evaluate(() => placeModeText("spread", STATE.data.placement));
    assert.match(spread, /Could not read what the apps reserve[\s\S]*timed out/);
    assert.doesNotMatch(spread, /Every app reserves something/);

    // Settings › UniFi: unread is not "not connected" with Connect offered; an older read is marked.
    await page.evaluate(() => { window.__ipam = STATE.data.ipam; STATE.data.ipam = undefined; });
    await settingsWith("/api/ipam");
    // Its section reads it when shown (#757); read it here whichever section is open.
    await page.evaluate(() => unifiCardLoad());
    await page.waitForFunction(() => /Could not read/.test(document.querySelector("#unifiCard")?.textContent || ""));
    const unifi = await page.evaluate(() => document.querySelector("#unifiCard").textContent);
    assert.match(unifi, /Could not read the UniFi connection[\s\S]*timed out/);
    assert.doesNotMatch(unifi, /not connected|Connect UniFi/);
    await page.evaluate(() => { STATE.data.ipam = { unifi: { configured: true, url: "https://192.0.2.1", last_sync: Date.now() / 1000 } }; return unifiCardLoad(); });
    assert.match(await page.evaluate(() => document.querySelector("#unifiCard").textContent), /Could not read the UniFi connection just now[\s\S]*timed out[\s\S]*last read/);

    // The UniFi dialog: nothing is saved over a connection that was not read.
    await page.evaluate(() => { STATE.data.ipam = undefined; return ipamUnifi(); });
    await settled(page);
    const unifiDialog = await page.locator("#mbody").innerText();
    assert.match(unifiDialog, /Could not read the UniFi connection[\s\S]*timed out/);
    assert.equal(await page.locator("#mbody").getByRole("button", {name: /^Save/}).count(), 0, "no Save over an unread connection");
    await page.evaluate(() => { closeModal(); STATE.data.ipam = window.__ipam; });

    // Settings › Redundancy: unread copies are said, not an older answer.
    await real();
    await page.evaluate(failing, "/api/self/replicas");
    const copies = await page.evaluate(async () => {
      document.querySelectorAll("#replicaCard").forEach(el => el.remove());
      document.body.insertAdjacentHTML("beforeend", '<div id="replicaCard"></div>');
      STATE.data.replicaHtml = "old copies";
      await replicasPaint();
      const text = document.querySelector("#replicaCard").textContent;
      document.querySelector("#replicaCard").remove();
      return text;
    });
    assert.match(copies, /Could not read how many copies of Homestead run[\s\S]*timed out/);
    assert.doesNotMatch(copies, /old copies/);

    // Settings › AI agents: unread requests are not "none waiting".
    await real();
    await page.evaluate(failing, "/api/agents/requests");
    const agents = await page.evaluate(async () => {
      document.querySelectorAll("#mcpCard").forEach(el => el.remove());
      document.body.insertAdjacentHTML("beforeend", '<div id="mcpCard"></div>');
      await mcpCardPaint();
      const text = document.querySelector("#mcpCard").textContent;
      document.querySelector("#mcpCard").remove();
      return text;
    });
    assert.match(agents, /Could not read how many are waiting[\s\S]*timed out/);

    // Linked clusters: unread is said, with its error, and the address is not offered for saving over.
    const fleetCard = async path => {
      await real();
      await page.evaluate(failing, path);
      return page.evaluate(async () => {
        document.querySelectorAll("#fleetCard, #fleetMovesCard").forEach(el => el.remove());
        document.body.insertAdjacentHTML("beforeend", '<div id="fleetMovesCard"></div><div id="fleetCard"></div>');
        await fleetSettingsPaint();
        await new Promise(r => setTimeout(r, 300));
        const text = { card: document.querySelector("#fleetCard").textContent, address: !!document.querySelector("#fleetAddress"),
          moves: document.querySelector("#fleetMovesCard").textContent, movesHidden: document.querySelector("#fleetMovesCard").hidden };
        document.querySelectorAll("#fleetCard, #fleetMovesCard").forEach(el => el.remove());
        return text;
      });
    };
    const fleet = await fleetCard("/api/fleet");
    assert.match(fleet.card, /Could not read the linked clusters[\s\S]*timed out/);
    assert.equal(fleet.address, false, "no address to save over one not read");
    assert.match((await fleetCard("/api/fleet/legacy")).card, /Could not read the clusters added before linking[\s\S]*timed out/);
    const sources = await fleetCard("/api/move/clusters");
    assert.match(sources.moves, /Could not read which clusters apps can move from[\s\S]*timed out/);
    assert.equal(sources.movesHidden, false);

    // The cluster switcher with nothing read yet: "reading…" while the read is on its way, "not read" once it fails.
    await real();
    const first = await page.evaluate(async () => {
      window.__fleetView = FLEET.view; FLEET.view = null;
      const read = window.api; let fail;
      window.api = (asked, ...rest) => String(asked).split("?")[0] === "/api/fleet"
        ? new Promise((_, no) => { fail = no; }) : read(asked, ...rest);
      const button = document.createElement("button"); document.body.append(button);
      fleetMenu(button);
      const waiting = document.querySelector(".fleet-menu")?.textContent || "";
      fail(new Error("timed out"));
      await new Promise(r => setTimeout(r, 100));
      const failed = document.querySelector(".fleet-menu")?.textContent || "";
      fleetMenuClose(); button.remove(); window.api = read; FLEET.view = window.__fleetView;
      return {waiting, failed};
    });
    assert.match(first.waiting, /All clusters\s*reading…/);
    assert.doesNotMatch(first.waiting, /not read/);
    assert.match(first.failed, /Could not read the clusters: timed out[\s\S]*All clusters\s*not read/);

    // The cluster switcher: an older list is marked when the refresh fails.
    await real();
    await page.evaluate(failing, "/api/fleet");
    const menu = await page.evaluate(async () => {
      window.__fleetView = FLEET.view;
      FLEET.view = { members: [{ id: "x1", name: "old-one", self: true, version: "1", reachable: true }] };
      const button = document.createElement("button"); document.body.append(button);
      fleetMenu(button);
      await new Promise(r => setTimeout(r, 300));
      const text = document.querySelector(".fleet-menu")?.textContent || "";
      fleetMenuClose(); button.remove(); FLEET.view = window.__fleetView;
      return text;
    });
    assert.match(menu, /Could not read the clusters just now[\s\S]*timed out/);

    // Migration from a linked cluster: an unread reach check is said, not left out.
    await real();
    await page.evaluate(failing, "/api/move/clusters/readiness");
    await page.evaluate(async () => {
      window.__fleetView = FLEET.view; window.__transferCall = window.fleetTransferCall;
      FLEET.view = { members: [{ id: "x2", handle: "branch", name: "Branch", self: false, version: "1", reachable: true }] };
      window.fleetTransferCall = async () => ({ allowed: true, ready: true, reachable_off_cluster: true, endpoint: "http://192.0.2.9:9000", longhorn: {} });
      await fleetMigration("x2");
    });
    const migration = await page.locator("#mbody").innerText();
    assert.match(migration, /Could not check whether this cluster can reach it[\s\S]*timed out/);
    assert.match(migration, /Reachable from here\s*not checked/);
    await page.evaluate(() => { closeModal(); FLEET.view = window.__fleetView; window.fleetTransferCall = window.__transferCall; });

    // Search: a part that could not be searched is said, and is not kept as none.
    await real();
    await page.evaluate(failing, "/api/vms");
    const search = await page.evaluate(async () => {
      STATE.data.vms = null; STATE.q = "a";
      await globalSearch("a");
      const text = document.querySelector("#searchResults").textContent;
      searchClose?.(); STATE.q = "";
      return { text, kept: STATE.data.vms };
    });
    assert.match(search.text, /Could not search the VMs[\s\S]*timed out/);
    assert.equal(search.kept, null, "unread VMs are not stored as none");

    // Dashboard › Portal: links not checked are not shown with an old answer.
    await real();
    await page.evaluate(failing, "/api/portal/status");
    // The widget is in the demo's layout only when chosen, so it is run where it is, and its read checked by its source.
    const dots = await page.evaluate(async () => {
      STATE.data.portalStatus = { any: { up: true, ms: 1 } }; STATE.data.portalUnread = "";
      const shown = Dashboard.render().includes('id="dashboardPortal"');
      if (shown) await Dashboard.loadPortal(true);
      return { shown, status: STATE.data.portalStatus, unread: STATE.data.portalUnread, source: Dashboard.loadPortal.toString() };
    });
    assert.match(dots.source, /\/api\/portal\/status[^;]*\.catch\(e\s*=>[^;]*portalUnread\s*=\s*e\.message/, "the status read keeps its error for the dots");
    if (dots.shown) assert.deepEqual([dots.status, dots.unread], [null, "timed out"]);

    // Volumes › New storage class: an unread V2 status is said, not an older one as current.
    await real();
    await page.evaluate(() => { STATE.data.v2 = { enabled: true, ready_nodes: 3, total_nodes: 3, nodes: [] }; return storageClassCreate(); });
    await settled(page);
    await page.evaluate(failing, "/api/storage/v2");
    const engine = await page.evaluate(async () => {
      document.querySelector("#sc_engine").value = "v2";
      await storageClassEngine();
      return document.querySelector("#sc_engine_note").textContent;
    });
    assert.match(engine, /Could not read Longhorn's V2 status[\s\S]*timed out/);
    assert.doesNotMatch(engine, /3 of 3/);
    await page.evaluate(() => closeModal());

    // Longhorn V2 (for one who is not an admin): unread hosts are not an empty table; an older reading is marked.
    await page.evaluate(failing, "/api/storage/v2");
    const v2 = await page.evaluate(async () => {
      const can = window.can; window.can = role => role === "admin" ? false : can(role);
      STATE.data.v2 = null;
      await v2Details(true);
      const none = document.querySelector("#mbody").textContent;
      STATE.data.v2 = { enabled: true, ready_nodes: 1, total_nodes: 1, nodes: [{ name: "host-old", checks: { hugepages: true } }] };
      await v2Details(true);
      const old = document.querySelector("#mbody").textContent;
      window.can = can; closeModal();
      return { none, old };
    });
    assert.match(v2.none, /Could not read Longhorn's V2 engine[\s\S]*timed out/);
    assert.match(v2.old, /Could not read it just now[\s\S]*timed out[\s\S]*last reading/);

    // Signing in while a data move is paused: an answer that could not be read says so, with its status.
    const signIn = await page.evaluate(async () => {
      const fetcher = window.fetch;
      window.fetch = async () => ({ ok: false, status: 502, json: () => Promise.reject(new Error("not JSON")) });
      document.body.insertAdjacentHTML("beforeend", '<div id="rgTest"><input id="rg_user" value="admin"><input id="rg_pass" value="x"><p id="rg_err" hidden></p></div>');
      await recoverySignIn();
      const text = document.querySelector("#rg_err").textContent;
      document.querySelector("#rgTest").remove(); window.fetch = fetcher;
      return text;
    });
    assert.match(signIn, /could not be read[\s\S]*502/);

    // Files: a helper that could not be asked to stop is said, not counted as stopping.
    await real();
    await page.evaluate(failing, "/api/files/close");
    const closing = await page.evaluate(async () => {
      Object.assign(FM, { open: true, panes: [{ namespace: "lab", pvc: "old-volume" }], headers: {} });
      await fmRelease();
      return [...document.querySelectorAll("#toast .tst")].map(t => t.textContent).join(" | ");
    });
    assert.match(closing, /could not be asked to stop[\s\S]*timed out/);

    // Files opened again within one slow volumes read: the earlier opening's answer does not land in the new one.
    await real();
    const reopen = await page.evaluate(async () => {
      const read = window.api, waits = [];
      window.api = (asked, ...rest) => String(asked).split("?")[0] === "/api/volumes"
        ? new Promise((resolve, reject) => waits.push({ resolve, reject })) : read(asked, ...rest);
      modal("Files", '<div id="fmTestHost"></div>', true, "volume-files");
      const loads = window.fmLoad; window.fmLoad = async () => {};
      fmStart([fmPane("lab", "a"), fmPane("lab", "b")]);
      fmStart([fmPane("lab", "a"), fmPane("lab", "b")]);
      waits[1].resolve([{ namespace: "lab", pvc_name: "fresh" }]);
      await new Promise(r => setTimeout(r, 50));
      waits[0].reject(new Error("timed out"));
      await new Promise(r => setTimeout(r, 50));
      const out = { volumes: FM.volumes.map(v => v.pvc), unread: FM.volumesUnread };
      window.fmLoad = loads; window.api = read; FM.open = false; closeModal();
      return out;
    });
    assert.deepEqual(reopen, { volumes: ["fresh"], unread: "" });

    // A host's "if it goes down": unread room on the other hosts is said, not left out.
    await real();
    await page.evaluate(failing, "/api/capacity/forecast");
    const room = await page.evaluate(async () => {
      document.body.insertAdjacentHTML("beforeend", '<div id="ifDownRoom" data-node="harvester-node1"></div>');
      await ifDownRoom("harvester-node1");
      const text = document.querySelector("#ifDownRoom").textContent;
      document.querySelector("#ifDownRoom").remove();
      return text;
    });
    assert.match(room, /Could not work out the room on the other hosts[\s\S]*timed out/);

    // Helm › Install a chart: unread namespaces are said, so the one filled in is not taken for one that exists.
    await real();
    await page.evaluate(failing, "/api/namespaces");
    await page.evaluate(() => helmInstall());
    await page.waitForSelector("#hi_ns", {state: "attached"});
    assert.match(await page.evaluate(() => document.querySelector("#mbody").textContent), /Could not read the namespaces[\s\S]*timed out/);
    assert.equal(await page.evaluate(() => document.querySelector("#hi_ns").value), "", "no namespace is filled in on a guess");
    await page.evaluate(() => closeModal());

    // Hosts' uptime and Monitoring's checks are kept apart: one read does not drop the other's.
    const uptimeKeys = await page.evaluate(() => {
      const kept = STATE.data.uptime;
      STATE.data.uptime = { apps: { "lab/web": { state: "up" } }, every: 60, unread: "timed out" };
      noteNodeUptime({ nodes: { "host-a": { windows: {} } }, step: 60 });
      const out = { apps: !!STATE.data.uptime?.apps, unread: STATE.data.uptime?.unread, nodes: !!STATE.data.nodeUptime?.nodes?.["host-a"] };
      STATE.data.uptime = kept;
      return out;
    });
    assert.deepEqual(uptimeKeys, { apps: true, unread: "timed out", nodes: true });

    // The bell: alerts that could not be refreshed are said, not their last count as current.
    await real();
    await page.evaluate(failing, "/api/alerts");
    const bell = await page.evaluate(async () => {
      PWA_ALERTS.report = { active: [{ key: "k", title: "Old alert", severity: "warn", acknowledged: false }] };
      PWA_ALERTS.request = null;
      await refreshPwaAlerts(true);
      paintBell();
      return { pop: document.querySelector("#bellPop").textContent, label: document.querySelector("#bell summary, .bell summary")?.getAttribute("aria-label") || "" };
    });
    assert.match(bell.pop, /Could not read the alerts just now[\s\S]*timed out/);
    assert.match(bell.label, /alerts not read/);
    await real();
    await page.evaluate(() => refreshPwaAlerts(true));

    assert.deepEqual(errors, []);
    console.log(`Unread reads say so passed ${width}px`);
    await context.close();
  }
} finally {
  await browser.close();
}
