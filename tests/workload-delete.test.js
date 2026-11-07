"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

/* "Delete app" lists the Services it removes and the volumes it keeps. A
   failed read used to show as "none" and still offer the delete, which then
   removed the Services anyway. */
const source = require("./helpers/workloads-source")();
const wlDelete = between(source, "window.wlDelete = async", "\nwindow.wlDeleteNow");

async function review(answers) {
  const body = { innerHTML: "" };
  const ctx = { STATE: { data: { wl: [{ ns: "lab", name: "frigate", images: ["frigate"] }] } },
    modal() {}, $: () => body, esc: x => String(x), jsq: x => JSON.stringify(String(x)),
    UI: { typedConfirm: () => '<input id="wd_confirm">', actions: html => html, callout: (tone, title, body) => `<div class="ui-callout ${tone}">${typeof title === "object" ? title.html : title} ${body}</div>` },
    api: async url => { const a = answers[url]; if (a instanceof Error) throw a; return a; } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(wlDelete, ctx);
  await ctx.wlDelete("lab", "frigate");
  return body.innerHTML;
}
const network = { services: [{ name: "frigate-web", namespace: "lab", targets: ["frigate"], external_ips: [], ports: [] }] };
const vols = [{ name: "pv-1", pvc_name: "frigate-config", attached: ["frigate"] }];

test("a failed Services read says so and offers no delete", async () => {
  const html = await review({ "/api/network": new Error("HTTP 503"), "/api/volumes": vols });
  assert.match(html, /could not be read/);
  assert.doesNotMatch(html, /Services removed<\/span>\s*<b>none/);
  assert.doesNotMatch(html, /wlDeleteNow/);
  assert.match(html, /Retry/);
});

test("a failed volumes read says so and offers no delete", async () => {
  const html = await review({ "/api/network": network, "/api/volumes": new Error("HTTP 503") });
  assert.match(html, /could not be read/);
  assert.doesNotMatch(html, /none attached/);
  assert.doesNotMatch(html, /wlDeleteNow/);
});

test("when both reads work, the delete is offered with what it removes and keeps", async () => {
  const html = await review({ "/api/network": network, "/api/volumes": vols });
  assert.match(html, /frigate-web/);
  assert.match(html, /frigate-config/);
  assert.match(html, /wlDeleteNow/);
  const none = await review({ "/api/network": { services: [] }, "/api/volumes": [] });
  assert.match(none, /none attached/);
  assert.match(none, /wlDeleteNow/);
});
