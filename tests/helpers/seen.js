"use strict";
// What a person reads in some HTML, with every More opened: tags gone, the
// More/Less toggle of a short notice left out, entities and spaces tidied.
// For tests that pin words rather than markup, so a notice can change shape.
module.exports = html => String(html ?? "")
  .replace(/<details class="ui-issue-toggle"[\s\S]*?<\/details>/g, "")
  .replace(/<[^>]*>/g, " ")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&")
  .replace(/\s+/g, " ")
  .replace(/ ([.,;:])/g, "$1")
  .trim();
