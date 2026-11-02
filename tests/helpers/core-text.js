// core-format.js's wording helpers (plural, stateWords), for view tests that build their own context.
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const between = require("./between");
const core = require("./core-source")();
const words = between(core, "/* A count in words", "const stateWords") + between(core, "const stateWords", "\n\n");
const source = `${words}\nthis.plural = plural; this.stateWords = stateWords;`;
module.exports = context => { vm.runInContext(source, context); return context; };
