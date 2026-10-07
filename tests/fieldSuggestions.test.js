// Run with: node tests/fieldSuggestions.test.js   (no dependencies)
const assert = require("assert");
const F = require("../static/fieldSuggestions.js");

let passed = 0;
const test = (name, fn) => { fn(); passed++; console.log("PASS " + name); };

// ---- the real bug this exists to catch: a more specific keyword losing to a shorter, generic one just because
// the generic category happens to be listed first in the array -------------------------------------------------
test("'dental clinic' matches Dentists, not the generic Clinics & doctors ('clinic' is a substring of it)", () => {
  assert.strictEqual(F.matchByText("dental clinics in Hyderabad contact").label, "Dentists");
});
test("'dental hospital' matches Dentists, not the generic Hospitals ('hospital' is a substring of it)", () => {
  assert.strictEqual(F.matchByText("best dental hospital in Delhi").label, "Dentists");
});
test("'driving school' matches Driving schools, not the generic Schools ('school' is a substring of it)", () => {
  assert.strictEqual(F.matchByText("driving schools in Pune").label, "Driving schools");
});
test("'play school' matches Daycare & playschools, not the generic Schools", () => {
  assert.strictEqual(F.matchByText("best play school for kids in Chennai").label, "Daycare & playschools");
});
test("'pet clinic' matches Veterinary & pet care, not the generic Clinics & doctors", () => {
  assert.strictEqual(F.matchByText("pet clinic near me").label, "Veterinary & pet care");
});
test("the generic categories still match on their own when nothing more specific is present", () => {
  assert.strictEqual(F.matchByText("hospitals in Hyderabad").label, "Hospitals");
  assert.strictEqual(F.matchByText("clinics in Hyderabad").label, "Clinics & doctors");
  assert.strictEqual(F.matchByText("schools in Vijayawada").label, "Schools");
});

// ---- word-boundary keywords (" atm ") must match at the very start or end of the text, not just in the middle ---
test("a boundary keyword matches when it's the very first word typed", () => {
  assert.strictEqual(F.matchByText("atm near Ameerpet").label, "Banks & ATMs");
});
test("a boundary keyword matches when it's the very last word typed", () => {
  assert.strictEqual(F.matchByText("find an atm").label, "Banks & ATMs");
});
test("a boundary keyword does not match as a substring of an unrelated word", () => {
  assert.strictEqual(F.matchByText("automated systems near me"), null);
});

// ---- no false positives --------------------------------------------------------------------------------------
test("ordinary text with no category keyword at all matches nothing", () => {
  assert.strictEqual(F.matchByText("best companies for freelancers"), null);
});
test("empty or missing text matches nothing, and never throws", () => {
  assert.strictEqual(F.matchByText(""), null);
  assert.strictEqual(F.matchByText(undefined), null);
});

// ---- directory-source category fallback (used only when nothing in the typed text matched) --------------------
test("a source category is used only once the text itself gives no match", () => {
  assert.strictEqual(F.matchByCategory(["Hospitals"]).label, "Hospitals");
});
test("a source with no category set (empty string) never matches every entry", () => {
  assert.strictEqual(F.matchByCategory([""]), null);
  assert.strictEqual(F.matchByCategory([]), null);
  assert.strictEqual(F.matchByCategory(undefined), null);
});

// ---- Places mode: matched by its own fixed category, not by free text --------------------------------------
test("a Places category with a matching entry returns it", () => {
  assert.strictEqual(F.matchByPlacesCategory("Hindu temples").label, "Temples");
  assert.strictEqual(F.matchByPlacesCategory("Veterinary clinics").label, "Veterinary & pet care");
});
test("a Places category with no matching entry, or none chosen yet, returns null rather than throwing", () => {
  assert.strictEqual(F.matchByPlacesCategory("Parks"), null);  // a real category - just none of ours suggests fields for it
  assert.strictEqual(F.matchByPlacesCategory(""), null);
  assert.strictEqual(F.matchByPlacesCategory(undefined), null);
});

// ---- every placesCategory actually points at something real (catches a future typo immediately) ---------------
test("every placesCategory used here is unique (no two entries silently fight over the same Places category)", () => {
  const used = F.FIELD_SUGGESTIONS.map((fs) => fs.placesCategory).filter(Boolean);
  assert.strictEqual(new Set(used).size, used.length, JSON.stringify(used));
});
test("every label is unique", () => {
  const labels = F.FIELD_SUGGESTIONS.map((fs) => fs.label);
  assert.strictEqual(new Set(labels).size, labels.length);
});

console.log(`\n${passed} tests passed`);
