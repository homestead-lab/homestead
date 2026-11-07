"use strict";
// What a person reads in some HTML, with every notice opened: tags gone, the
// first line a closed notice previews left out (it is read again inside),
// entities and spaces tidied. For tests that pin words rather than markup,
// so a notice can change shape.
module.exports = html => String(html ?? "")
  .replace(/<span class="ui-notice-first">[\s\S]*?<\/span><\/span><span class="ui-notice-chev"/g, '</span><span class="ui-notice-chev"')
  .replace(/<[^>]*>/g, " ")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&")
  .replace(/\s+/g, " ")
  .replace(/ ([.,;:])/g, "$1")
  .trim();
