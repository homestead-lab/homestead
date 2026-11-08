"use strict";
// Colour means state (docs/design.md): green healthy, amber needs a person,
// red failed, blue opens something. Progress and labels stay neutral, so a
// healthy page is calm (#490).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");
const between = require("./helpers/between");
const read = f => fs.readFileSync(f, "utf8");
// A top-level `const NAME = {...};` object literal, evaluated.
const map = (file, name) => {
  return Function(`${between(read(file), `const ${name} = {`, "};", { through: true })} return ${name};`)();
};

test("Image Cache: only active is green; rollback, stopped and scheduled are neutral", () => {
  const reasons = map("web/js/views-lifecycle-images.js", "IMAGE_REASONS");
  assert.equal(reasons.active.tone, "ok");
  for (const r of ["rollback", "stopped", "scheduled"]) assert.equal(reasons[r].tone, "neutral", r);
});

test("Image Cache: an image kept because a record could not be read is amber and says why", () => {
  const unreadable = map("web/js/views-lifecycle-images.js", "IMAGE_REASONS").unreadable;
  assert.equal(unreadable?.tone, "warn");
  assert.equal(unreadable.tip, "Kept, because a record of which images are in use could not be read.");
});

test("Schedules and Import show a running or waiting job as neutral, not amber", () => {
  const src = require("./helpers/lifecycle-source")();
  // Schedules' State (#919): running and paused neutral; amber only for a run that did not happen or an unknown.
  assert.match(src, /running: \["neutral", "running"\], paused: \["neutral", "paused"\]/);
  assert.doesNotMatch(src, /UI\.chip\("running", "warn"\)/);
  assert.match(src, /j\.state === "failed" \? "bad" : "neutral"/);
});

test("App Store and Helm labels are neutral: guidance, official, verified, installed here", () => {
  const store = require("./helpers/workloads-source")();
  assert.doesNotMatch(store, /level === "dependency" \? "warn" : "info"/);
  const helm = read("web/js/views-helm.js");
  assert.match(helm, /UI\.chip\("official", "neutral"\)/);
  assert.match(helm, /UI\.chip\("verified", "neutral"\)/);
  assert.doesNotMatch(helm, /UI\.chip\("(official|verified)", "(ok|info)"/);
  for (const [, [, tone]] of Object.entries(map("web/js/views-helm.js", "HELM_MANAGED"))) assert.equal(tone, "neutral");
});

test("a VM in progress or paused is neutral; Running is green", () => {
  const tones = map("web/js/views-vms.js", "VM_TONE");
  assert.equal(tones.Running, "ok");
  for (const s of ["Stopped", "Paused", "Migrating", "Starting", "Stopping", "Deleting", "Provisioning", "WaitingForVolumeBinding"])
    assert.equal(tones[s], "neutral", s);
});

test("a k3s VM's cluster and role tags are the shared neutral cluster tag", () => {
  const src = read("web/js/views-vms.js");
  assert.doesNotMatch(src, /vmClusterTag = v => v\.cluster \? `<span class="tag info"/);
  assert.doesNotMatch(src, /<span class="tag info">server<\/span>/);
  assert.doesNotMatch(src, /UI\.chip\("server", "info"\)/, "the k3s review's role chips are neutral too");
});

test("an Unraid VM that is shut off has a neutral chip, as Stopped does", () => {
  assert.match(read("web/js/views-vms.js"), /UI\.chip\(vm\.state \|\| "unknown", off \? "neutral" : "ok"\)/);
});

test("Dashboard widgets: low severity and acknowledged findings are neutral, running jobs too", () => {
  const src = read("web/js/insights.js");
  assert.match(src, /\{critical:"bad",medium:"warn",low:"",healthy:"ok"\}\[severity\] \?\? ""/);
  assert.doesNotMatch(src, /tone:acked\?"info"/);
});

test("the bell: a waiting agent request is amber, alerts take their worst severity", () => {
  const src = read("web/js/homestead-update.js");
  assert.match(src, /AI agent request"\)\} waiting for approval`, "warn"\)/);
  assert.doesNotMatch(src, /plural\(alertCount, "active alert"\), "danger"\)/);
});

test("AI agent requests: pending waits for a person, so amber; running and denied are neutral", () => {
  const status = map("web/js/views-agents.js", "AGENT_STATUS");
  assert.equal(status.pending[1], "warn");
  assert.equal(status.running[1], "");
  assert.equal(status.denied[1], "");
  const outcome = map("web/js/views-agents.js", "AGENT_OUTCOME");
  assert.equal(outcome.pending, "warn");
  assert.equal(outcome.denied, "");
  assert.equal(outcome.refused, "warn");
});

test("other progress states are neutral too: installing, starting, rebuilding, copying, a container starting", () => {
  const platform = read("web/js/platform.js");
  assert.match(platform, /UI\.chip\("installing", "neutral"\)/);
  assert.match(platform, /UI\.chip\("starting", "neutral"\)/);
  assert.doesNotMatch(platform, /UI\.chip\("(installing|starting)", "(warn|med)"/);
  assert.match(platform, /UI\.chip\(`\$\{probe\.ready[^`]*`, "neutral"\)/);
  assert.match(platform, /UI\.chip\(`removing ·[^`]*`, "neutral"\)/);
  assert.match(platform, /UI\.chip\(`\$\{c\.current\} of[^`]*`, "neutral"\)/);
  assert.match(platform, /UI\.chip\("partly serving", "warn"\)/, "a partly served share still needs a person");
  assert.match(require("./helpers/storage-source")(), /UI\.chip\(`rebuilding \$\{rebuild\.pct\}%`, "neutral"/);
  assert.doesNotMatch(require("./helpers/storage-source")(), /UI\.chip\(`rebuilding[^`]*`, "(warn|med)"/);
  assert.match(read("web/js/vm-hardware.js"), /UI\.chip\("copying", "neutral"\)/);
  assert.doesNotMatch(require("./helpers/workloads-source")(), /c\.state === "running" \? "med"/);
});
