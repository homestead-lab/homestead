"use strict";

/* CPU and memory in the units the rest of Homestead shows (#311): a number
   and a unit, typed amounts understood, slips refused, and the Kubernetes
   quantity the form sends kept as the form always read it. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const ui = fs.readFileSync("web/js/ui.js", "utf8");
const start = ui.indexOf("const MEMORY_UNITS");
const ctx = { text: s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])),
  document: { addEventListener() {} }, CSS: { escape: s => s } };
vm.createContext(ctx);
vm.runInContext(ui.slice(start, ui.indexOf("const quantityUpdate")) + "\nthis.parseMemory = parseMemory; this.parseCpu = parseCpu; this.quantity = quantity;", ctx);
const plain = value => JSON.parse(JSON.stringify(value));

test("memory: a number with MiB or GiB, and friendly spellings", () => {
  assert.deepEqual(plain(ctx.parseMemory("390", "Mi")), { value: "390Mi", mib: 390, said: "= 390 MiB" });
  assert.deepEqual(plain(ctx.parseMemory("1", "Gi")), { value: "1Gi", mib: 1024, said: "= 1 GiB" });
  assert.equal(ctx.parseMemory("1.5 GB", "Mi").value, "1536Mi");
  assert.equal(ctx.parseMemory("1.5 GB", "Mi").said, "= 1.5 GiB (1536 MiB)");
  assert.equal(ctx.parseMemory("512Mi", "Gi").value, "512Mi");
  assert.equal(ctx.parseMemory("", "Mi").value, "");
});

test("memory: the slips Kubernetes would take are refused, saying why", () => {
  assert.match(ctx.parseMemory("390m", "Mi").error, /thousandths of a byte/);
  assert.match(ctx.parseMemory("lots", "Mi").error, /Write a number/);
  assert.match(ctx.parseMemory("5 parsecs", "Mi").error, /not a memory unit/);
});

test("CPU: a share of one core, or cores, sent as millicores", () => {
  assert.deepEqual(plain(ctx.parseCpu("5", "%")), { value: "50m", millicores: 50, said: "= 5% of one core (50m)" });
  assert.equal(ctx.parseCpu("1.5", "cores").value, "1500m");
  assert.equal(ctx.parseCpu("250m", "%").value, "250m");
  assert.equal(ctx.parseCpu("2 cores", "%").said, "= 2 cores (2000m)");
  assert.match(ctx.parseCpu("0.01", "%").error, /at least 1m/);
});

test("a saved quantity opens in friendly units, its Kubernetes value kept for the form", () => {
  const memory = ctx.quantity("memory", "e_mem_0", "390Mi", { usage: "the app uses about 611 MB now" });
  assert.match(memory, /class="qty-text mono"[^>]*value="390"/);
  assert.match(memory, /<option value="Mi" selected>MiB<\/option>/);
  assert.match(memory, /<input type="hidden" id="e_mem_0" value="390Mi">/);
  assert.match(memory, /= 390 MiB<\/span><span class="qty-usage">the app uses about 611 MB now/);
  assert.match(ctx.quantity("memory", "m", "2Gi"), /value="2"[\s\S]*<option value="Gi" selected>/);
  const cpu = ctx.quantity("cpu", "d_cpu", "50m");
  assert.match(cpu, /value="5"[\s\S]*<option value="%" selected>% of a core/);
  assert.match(ctx.quantity("cpu", "c", "2"), /value="2"[\s\S]*<option value="cores" selected>/);
});

test("every form that sets these uses it, and a maximum is checked against its reservation", () => {
  for (const file of ["web/js/views-workloads.js", "web/js/views-lifecycle.js"]) {
    const source = fs.readFileSync(file, "utf8");
    assert.doesNotMatch(source, /placeholder="128Mi"|placeholder="50m"|e\.g\. 1Gi"/, file);
  }
  const deploy = fs.readFileSync("web/js/views-workloads.js", "utf8");
  assert.match(deploy, /UI\.quantity\("memory", "d_mem_limit", [^)]*reserved: "d_mem"/);
});
