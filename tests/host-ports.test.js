const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

const ctx = { console };
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("web/js/diagrams.js", "utf8"), ctx);

const nic = (name, extra = {}) => ({ name, kind: "nic", link: "up", speed_mbps: 2500, master: "", carries: [], ...extra });
const bonded = {
  uplink: "br0",
  ports: [
    nic("enp1s0", { master: "bond0", carries: ["host address"], uplink: true, bond_member: { state: "active" } }),
    nic("enp2s0", { master: "bond0", carries: ["host address"], uplink: true, speed_mbps: 1000, was_mbps: 2500, bond_member: { state: "backup" } }),
    nic("enp3s0", { link: "down", speed_mbps: null }),
    { name: "bond0", kind: "bond", link: "up", master: "br0", carries: ["host address"], uplink: true, bond: { mode: "active-backup" } },
    { name: "br0", kind: "bridge", link: "up", carries: ["host address", "lab/iot"], uplink: true },
  ],
  conditions: [{ iface: "enp2s0", title: "enp2s0 on host-2 runs slower than it did" }],
};

test("a bonded host draws its NICs, the bond, the bridge and what they carry", () => {
  const out = ctx.Diagram.ports(bonded, { address: "192.0.2.12", vips: ["192.0.2.50"] });
  for (const word of ["enp1s0", "enp2s0", "enp3s0", "bond0", "br0", "192.0.2.12 · this host", "192.0.2.50", "lab/iot", "no link", "was 2.5 Gb/s"])
    assert.ok(out.includes(word), word);
  // The active member solid, the backup dashed, the dead spare red.
  assert.match(out, /class="dg-active"/);
  assert.match(out, /class="dg-standby"/);
  assert.match(out, /class="dg-down"/);
  assert.match(out, /dg-led-warn/);
  assert.match(out, /aria-label="3 network ports: enp1s0 2.5 Gb\/s in bond0, enp2s0 1 Gb\/s in bond0, enp3s0 down\. enp2s0 on host-2 runs slower than it did"/);
});

test("a plain host with one NIC draws without a bond or bridge", () => {
  const out = ctx.Diagram.ports({ uplink: "eth0", ports: [nic("eth0", { carries: ["host address"], uplink: true })], conditions: [] });
  assert.ok(out.includes("eth0"));
  assert.ok(!out.includes("dg-bond"));
  assert.ok(!out.includes("dg-bridge"));
  assert.ok(out.includes("this host's address"));
});

test("an LACP bond draws both members as active", () => {
  const out = ctx.Diagram.ports({ uplink: "bond0", conditions: [], ports: [
    nic("ens5", { master: "bond0", carries: ["host address"], bond_member: { state: "active" } }),
    nic("ens6", { master: "bond0", carries: ["host address"], bond_member: { state: "active" } }),
    { name: "bond0", kind: "bond", link: "up", carries: ["host address"], bond: { mode: "802.3ad" } }] });
  assert.strictEqual((out.match(/class="dg-active"/g) || []).length, 2);
  assert.ok(!out.includes("dg-standby"));
});
