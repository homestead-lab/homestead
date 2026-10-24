// A ready page with a live request must be auditable; an unfinished page must
// still fail. No cluster or external service is contacted.
import assert from "node:assert/strict";
import http from "node:http";
import { chromium } from "playwright";
import { openAuditPage } from "../../scripts/audit_ready.mjs";

let pending = 0;
const server = http.createServer((req, res) => {
  if (req.url === "/live") {
    pending++; res.on("close", () => pending--);
    res.writeHead(200, { "Content-Type": "text/event-stream" }); res.write(": connected\n\n");
    return;
  }
  res.writeHead(200, { "Content-Type": "text/html" });
  res.end(`<!doctype html><div id="gate"></div><div id="views"></div><script>
    window.NAV_TOKEN = 1; let loading = true;
    function pageLoading() { return loading; }
    fetch('/live');
    ${req.url === "/ready" ? `setTimeout(() => {
      document.querySelector('#gate').classList.add('hidden');
      document.querySelector('#views').innerHTML = '<header class="phead">Dashboard</header>';
      setTimeout(() => { loading = false; }, 100);
    }, 100);` : ""}
  </script>`);
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await openAuditPage(page, `${base}/ready`);
  assert.equal(pending, 1, "readiness does not require the live connection to end");
  assert.equal(await page.evaluate(() => loading), false, "initial data finishes before auditing");
  await page.close();
  const unfinished = await browser.newPage(); unfinished.setDefaultTimeout(750);
  await assert.rejects(openAuditPage(unfinished, `${base}/unfinished`), /Timeout/,
    "a page that never becomes usable is not accepted");
  await unfinished.close();
  console.log("Audit readiness: live requests do not block a ready page; unfinished pages fail.");
} finally {
  await browser?.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
}
