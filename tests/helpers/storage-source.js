// The views-storage-*.js files (#911: views-storage.js split by page), as
// index.html loads them. The default export is their text joined, for the tests
// that cut a function out at its markers or run the old file whole; files() and
// names() are the parts, for the tests that run each file of a list in turn, as
// the browser does. A part index.html does not load is missing here too.
const fs = require("node:fs"), path = require("node:path");
const WEB = path.join(__dirname, "..", "..", "web");

const names = () => [...fs.readFileSync(path.join(WEB, "index.html"), "utf8")
  .matchAll(/<script src="\/js\/(views-storage-[a-z-]+\.js)\?v=/g)].map(m => m[1]);
const files = () => names().map(name => `web/js/${name}`);

module.exports = () => names().map(name => fs.readFileSync(path.join(WEB, "js", name), "utf8")).join("\n");
module.exports.names = names;
module.exports.files = files;
