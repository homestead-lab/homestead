// core.js and its core-*.js parts (#911), as index.html loads them: the text the
// tests that slice core.js by its markers read. files() is the order of the tags,
// so a part index.html does not load is missing here too, and a slice that needs
// it fails on its marker.
const fs = require("node:fs"), path = require("node:path");
const WEB = path.join(__dirname, "..", "..", "web");

const files = () => [...fs.readFileSync(path.join(WEB, "index.html"), "utf8")
  .matchAll(/<script src="\/js\/(core(?:-[a-z-]+)?\.js)\?v=/g)].map(m => m[1]);
const text = file => fs.readFileSync(path.join(WEB, "js", file), "utf8");

module.exports = () => files().map(text).join("\n");
module.exports.files = files;
module.exports.text = text;
