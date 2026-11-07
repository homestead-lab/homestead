// views-workloads.js and its parts (#911), as index.html loads them. Each part is one
// contiguous piece of the old file with a one-line header and a Node-only export at the end;
// text() puts the old file back together (header and export taken off), so a test that reads
// or cuts the file reads the same text it did before the split. files() is the order of the tags.
const fs = require("node:fs"), path = require("node:path");
const WEB = path.join(__dirname, "..", "..", "web");

const files = () => [...fs.readFileSync(path.join(WEB, "index.html"), "utf8")
  .matchAll(/<script src="\/js\/(views-workloads(?:-[a-z]+)?\.js)\?v=/g)].map(m => m[1]);
const body = file => {
  const text = fs.readFileSync(path.join(WEB, "js", file), "utf8");
  return text.replace(/^[^\n]*\n/, "").replace(/\n\/\/ Node only \(tests\)[\s\S]*$/, "");
};

module.exports = () => files().map(body).join("");
module.exports.files = files;
module.exports.text = body;
// "web/js/…" paths and bare names, for a test that lists the files it loads one by one.
module.exports.paths = () => files().map(f => `web/js/${f}`);
module.exports.names = files;
