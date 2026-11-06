// The host cards' 30-day uptime strip has its own row, the figures a row under
// it, so a card with maintenance lines up with one without. The comparison view
// reads the same. Measured in a browser: the fault was only ever a matter of widths.
import assert from "node:assert/strict";
import fs from "node:fs";
import {chromium} from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4176";
const shots = process.env.UPTIME_STRIP_SCREENSHOTS || "";
if (shots) fs.mkdirSync(shots, {recursive: true});
const browser = await chromium.launch({headless: true});

const measure = (root = "") => [...document.querySelectorAll(`${root} .node-upstrip`)].map(row => {
  const box = el => el.getBoundingClientRect();
  const strip = row.querySelector(".uptime-strip.mini"), figure = row.querySelector("b");
  const text = row.querySelector("b + .dim");
  return {
    words: row.innerText, rowLeft: box(row).left,
    width: box(strip).width, left: box(strip).left, top: box(strip).top, bottom: box(strip).bottom,
    bars: strip.children.length, bar: box(strip.children[0]).width,
    figureTop: box(figure).top, figureLeft: box(figure).left, textTop: box(text).top, textLeft: box(text).left,
  };
});

try {
  for (const width of [1440, 700, 360]) {
    const context = await browser.newContext({viewport: {width, height: 1000}});
    const page = await context.newPage(), errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(`${base}/?demo=1`);
    await page.waitForSelector(".dashboard-widget");
    await page.evaluate(() => clearInterval(window.__loopTimer));

    // The demo's six minutes of maintenance are older than 30 days, so the words
    // this is about would not show. One host gets five minutes inside the window.
    await page.evaluate(() => {
      const read = window.api;
      window.api = async (asked, ...rest) => {
        const answer = await read(asked, ...rest);
        if (String(asked).split("?")[0] !== "/api/nodes/uptime") return answer;
        const copy = structuredClone(answer);
        copy.nodes["harvester-node1"].maintenance_s["30d"] = 300;
        return copy;
      };
    });

    // Cards: same strip on every card, with or without maintenance.
    await page.evaluate(async () => { localStorage.setItem("homestead.layout.nodes", "cards"); await viewNodes(); });
    await page.waitForFunction(() => /5m maintenance/.test(document.querySelector("#views")?.innerText || ""));
    const cards = await page.evaluate(measure);
    assert.ok(cards.length > 1, `${width}: several cards`);
    assert.ok(cards.some(c => /maintenance/.test(c.words)), `${width}: the demo has a card with maintenance`);
    assert.ok(cards.some(c => !/maintenance/.test(c.words)), `${width}: and one without`);
    for (const c of cards) {
      assert.equal(c.bars, 30);
      assert.ok(Math.abs(c.width - cards[0].width) <= 1, `${width}: strips ${cards[0].width} and ${c.width} px wide`);
      assert.ok(Math.abs((c.left - c.rowLeft) - (cards[0].left - cards[0].rowLeft)) <= 1, `${width}: strips start at the same place on the card`);
      assert.ok(c.bar >= 3, `${width}: a bar is ${c.bar} px wide`);
      assert.ok(c.figureTop >= c.bottom, `${width}: the percentage is under the strip`);
      assert.ok(c.textTop >= c.bottom, `${width}: the text is under the strip`);
      assert.ok(Math.abs(c.textLeft - c.left) <= 1, `${width}: the text starts where the strip does`);
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${width}: no sideways page scroll on cards`);
    if (shots) await page.screenshot({path: `${shots}/cards-${width}.png`, animations: "disabled"});

    // Comparison: the strip above the figures, at every width. Its label stays hidden.
    await page.evaluate(async () => { localStorage.setItem("homestead.layout.nodes", "rows"); await viewNodes(); });
    await page.waitForFunction(() => document.querySelector(".comparison-table .node-upstrip"));
    const rows = await page.evaluate(measure, ".comparison-table");
    assert.ok(rows.length > 1, `${width}: comparison has strips`);
    const tops = rows.map(c => c.top);
    assert.ok(Math.max(...tops) - Math.min(...tops) <= 1, `${width}: comparison's strips line up, tops ${tops.join(", ")}`);
    assert.deepEqual(await page.evaluate(() => [...document.querySelectorAll(".comparison-table .node-upstrip>.dim:first-child")]
      .map(label => getComputedStyle(label).display)), rows.map(() => "none"), `${width}: comparison hides the UPTIME label`);
    for (const c of rows) {
      assert.ok(c.figureTop >= c.bottom, `${width}: comparison's percentage is under the strip`);
      assert.ok(c.textTop >= c.bottom, `${width}: comparison's text is under the strip`);
      assert.ok(Math.abs(c.left - c.rowLeft) <= 1, `${width}: comparison's strip starts at the left of its cell`);
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${width}: no sideways page scroll on the comparison`);
    if (shots) {
      await page.locator(".comparison-table .node-upstrip").first().scrollIntoViewIfNeeded();
      await page.screenshot({path: `${shots}/comparison-${width}.png`, animations: "disabled"});
    }

    assert.deepEqual(errors, [], `${width}: page errors`);
    await context.close();
  }
  console.log("host uptime strip: own row at every width, cards and comparison");
} finally {
  await browser.close();
}
