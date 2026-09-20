/* Pure catalogue helpers kept separate so unusual third-party feed shapes are testable. */
function appCategoryLabel(value) {
  const visit = item => {
    if (Array.isArray(item)) {
      for (const child of item) { const found = visit(child); if (found) return found; }
      return "";
    }
    if (item && typeof item === "object") {
      for (const key of ["name", "label", "category", "Category"]) {
        const found = visit(item[key]); if (found) return found;
      }
      for (const child of Object.values(item)) { const found = visit(child); if (found) return found; }
      return "";
    }
    const text = String(item ?? "").trim();
    return text ? text.split(/[\s,|/]+/)[0] : "";
  };
  return visit(value);
}
window.appCategoryLabel = appCategoryLabel;
