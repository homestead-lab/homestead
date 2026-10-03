// Render real shared components in isolated view tests, just as the browser does.
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../../web/js/ui.js"), "utf8");
module.exports = context => {
  context.document ??= {};
  vm.runInContext(source, context);
};
