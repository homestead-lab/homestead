"use strict";

/* Settings shows a section's cards by CSS: a card names its topic
   (data-tab), and style.css lists the topics each section shows. The list in
   views-settings.js and the one in style.css must agree, and every card must
   belong to a section, or it is never seen. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const source = fs.readFileSync("web/js/views-settings.js", "utf8");
const css = fs.readFileSync("web/style.css", "utf8");
const block = source.slice(source.indexOf("const SETTINGS_SECTIONS = ["), source.indexOf("];", source.indexOf("const SETTINGS_SECTIONS = [")) + 2);
const SECTIONS = new Function(`${block}; return SETTINGS_SECTIONS;`)();

test("every section's topics are shown by the stylesheet", () => {
  for (const [id, , , topics] of SECTIONS) {
    for (const topic of topics) {
      assert.ok(css.includes(`.settings-grid[data-tab="${id}"]>section[data-tab="${topic}"]`), `${id} shows ${topic}`);
    }
  }
});

test("the stylesheet shows no topic a section does not hold", () => {
  const rules = [...css.matchAll(/\.settings-grid\[data-tab="([a-z]+)"\]>section\[data-tab="([a-z]+)"\]/g)];
  for (const [, id, topic] of rules) {
    const section = SECTIONS.find(([sid]) => sid === id);
    assert.ok(section, `${id} is a section`);
    assert.ok(section[3].includes(topic), `${id} holds ${topic}`);
  }
});

test("every Settings card belongs to a section", () => {
  const topics = new Set(SECTIONS.flatMap(([, , , t]) => t));
  const files = fs.readdirSync("web/js").filter(f => f.endsWith(".js"));
  for (const file of files) {
    const text = fs.readFileSync(`web/js/${file}`, "utf8");
    for (const [, topic] of text.matchAll(/<section class="card flat[^"]*"[^>]*data-tab="([a-z]+)"/g)) {
      assert.ok(topics.has(topic), `${file}: data-tab="${topic}" belongs to no section`);
    }
  }
});
