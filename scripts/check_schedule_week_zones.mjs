// Demo-only browser check (#919): This week in a browser on another side of the
// world. In Pacific/Auckland, on the evening before New Zealand's clocks go
// forward (02:00 NZST on Sunday 27 September 2026 is 03:00 NZDT), each run's
// When is its time on Auckland's clock, an hour later after the change, and
// Today ends at Auckland's midnight, not the server's or UTC's. The expected
// times are worked out here from the fixed offsets (+12 h, +13 h after
// 2026-09-26 14:00 UTC), not with the browser's time zone data the page uses.
import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";

const DST = Date.parse("2026-09-26T14:00:00Z");
const auckland = ms => new Date(ms + (ms < DST ? 12 : 13) * 3600000);   // its wall clock, read with getUTC*
const two = n => String(n).padStart(2, "0");

async function rows(page) {
  return page.$$eval('table[data-sort="schedule-week"] tbody tr', trs => trs.map(tr => {
    const when = tr.querySelector('td[data-label="When"]');
    return { at: Number(when.dataset.sort), text: when.innerText.trim() };
  }).filter(r => r.at > 0));
}

async function look(browser, now, label) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, timezoneId: "Pacific/Auckland", locale: "en-GB" });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.clock.setFixedTime(new Date(now));
  await page.goto(`${base}/?demo=1`, { waitUntil: "networkidle" });
  await page.evaluate(() => go("schedule-week"));
  await page.locator('table[data-sort="schedule-week"]').waitFor();
  const week = await rows(page);
  assert.ok(week.length > 5, `${label}: the demo's week has runs`);
  assert.ok(week.some(r => r.at >= DST), `${label}: runs after the clock change`);
  for (const r of week) {
    const wall = auckland(r.at);
    // "Sat 26 Sept, 14:00": the time after the last comma, the date before it,
    // whose only number is the day (this year's dates carry no year).
    const cut = r.text.lastIndexOf(","), date = r.text.slice(0, cut), time = r.text.slice(cut + 1).trim();
    assert.equal(time, `${two(wall.getUTCHours())}:${two(wall.getUTCMinutes())}`, `${label}: ${r.text} is ${wall.toISOString()} on Auckland's clock`);
    assert.deepEqual(date.match(/\d+/g), [String(wall.getUTCDate())], `${label}: ${r.text} is on Auckland's date`);
  }
  // Today: the runs before Auckland's next midnight, and only those.
  const local = auckland(now);
  const midnight = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate() + 1) - (now < DST ? 12 : 13) * 3600000;
  await page.evaluate(() => weekRange("today"));
  await page.waitForFunction(() => document.querySelector('[aria-label="Range"] button.on')?.textContent === "Today");
  const today = await page.$$eval('table[data-sort="schedule-week"] tbody tr td[data-label="When"]', tds => tds.map(td => Number(td.dataset.sort)).filter(Boolean))
    .catch(() => []);
  assert.deepEqual(today.sort(), week.map(r => r.at).filter(at => at < midnight).sort(), `${label}: Today ends at Auckland's midnight`);
  assert.match(page.url(), /[?&]range=today\b/, `${label}: the range is kept in the address`);
  assert.deepEqual(errors, [], `${label}: no page errors`);
  await context.close();
  const utcMidnight = Date.UTC(new Date(now).getUTCFullYear(), new Date(now).getUTCMonth(), new Date(now).getUTCDate() + 1);
  return { runs: week.length, today: today.length, after: week.filter(r => r.at >= midnight && r.at < utcMidnight).length };
}

const browser = await chromium.launch({ headless: true });
try {
  // Saturday 13:00 NZST: the nightly 02:00 UTC runs fall at 14:00 today, and at 15:00 NZDT after the change.
  const evening = await look(browser, Date.parse("2026-09-26T01:00:00Z"), "Saturday afternoon");
  assert.ok(evening.today > 0, "Saturday afternoon: Today has runs, so its end is tested");
  // Wednesday 18:50 NZST: a VM stops at 19:00 UTC, after Auckland's midnight but before UTC's, so not today.
  const weekday = await look(browser, Date.parse("2026-09-23T06:50:00Z"), "Wednesday evening");
  assert.ok(weekday.today > 0 && weekday.after > 0, "Wednesday evening: runs on both sides of Auckland's midnight, before UTC's");
  // 23:50 NZST: ten minutes of today left.
  const late = await look(browser, Date.parse("2026-09-26T11:50:00Z"), "ten to midnight");
  console.log(`Schedule week zones passed: ${evening.runs} runs (${evening.today} today), ${weekday.today} today and ${weekday.after} after midnight on Wednesday, ${late.today} today at ten to midnight in Pacific/Auckland`);
} finally {
  await browser.close();
}
