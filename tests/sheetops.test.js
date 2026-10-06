// Run with: node tests/sheetops.test.js   (no dependencies)
const assert = require("assert");
const S = require("../static/sheetops.js");

let passed = 0;
const test = (name, fn) => { fn(); passed++; console.log("PASS " + name); };

const cols = ["Name", "Emails", "Phone Numbers", "Address", "Website"];
const sheetA = { name: "one.xlsx", columns: cols, rows: [
  { Name: "A School", Emails: "info@a.com", "Phone Numbers": "+91 98480 12345", Address: "", Website: "" },
  { Name: "B School", Emails: "", "Phone Numbers": "040-2345678", Address: "B road", Website: "" },
] };
const sheetB = { name: "two.xlsx", columns: [...cols], rows: [
  { Name: "A School (dup)", Emails: "INFO@A.COM ", "Phone Numbers": "9848012345", Address: "A road", Website: "https://www.a.com/" },
  { Name: "C School", Emails: "c@c.com", "Phone Numbers": "", Address: "", Website: "" },
] };

test("smart match: email case/space duplicates are removed (4 in, 1 removed)", () => {
  const r = S.merge([sheetA, sheetB], { keys: ["Emails"], keep: "first" });
  assert.strictEqual(r.rowsIn, 4); assert.strictEqual(r.removedRows.length, 1); assert.strictEqual(r.rows.length, 3);
});
test("rows with an empty key are never treated as duplicates", () => {
  const r = S.merge([sheetA, sheetA], { keys: ["Emails"] });
  assert.strictEqual(r.removedRows.length, 1);                                   // only A School repeats
  assert.strictEqual(r.rows.filter((x) => x.Name === "B School").length, 2);     // B School has no email: both kept
});
test("phones match across formats (+91 98480 12345 == 9848012345)", () => {
  const r = S.merge([sheetA, sheetB], { keys: ["Phone Numbers"] });
  assert.strictEqual(r.removedRows.length, 1);
});
test("websites ignore https://, www. and trailing slash", () => {
  assert.strictEqual(S.norm("https://www.A.com/", "Website", {}), "a.com");
});
test("keep most complete + fill empty cells from the removed duplicate", () => {
  const r = S.merge([sheetA, sheetB], { keys: ["Emails"], keep: "most_complete", fillEmpty: true });
  const a = r.rows.find((x) => x.Emails.toLowerCase().trim() === "info@a.com");
  assert.strictEqual(a.Name, "A School (dup)");          // the dup row has more filled cells
  assert.strictEqual(a.Address, "A road");
});
test("keep last", () => {
  const r = S.merge([sheetA, sheetB], { keys: ["Emails"], keep: "last" });
  assert.ok(r.rows.some((x) => x.Name === "A School (dup)") && !r.rows.some((x) => x.Name === "A School"));
});
test("fill_empty keeps the kept row's own values and fills blanks", () => {
  const r = S.merge([sheetA, sheetB], { keys: ["Emails"], keep: "first", fillEmpty: true });
  const a = r.rows.find((x) => x.Name === "A School");
  assert.strictEqual(a.Website, "https://www.a.com/"); assert.strictEqual(a.Name, "A School");
});
test("source sheet column added when merging several sheets", () => {
  const r = S.merge([sheetA, sheetB], { keys: [] });
  assert.ok(r.columns.includes("Source Sheet")); assert.strictEqual(r.rows[0]["Source Sheet"], "one");
  assert.strictEqual(r.rows.length, 4);
});
test("single sheet does not get a Source Sheet column", () => {
  assert.ok(!S.merge([sheetA], { keys: ["Emails"] }).columns.includes("Source Sheet"));
});
test("unknown key column is rejected", () => {
  assert.throws(() => S.merge([sheetA], { keys: ["Nope"] }), /Column not found/);
});
test("ignore punctuation keeps Telugu and Hindi letters", () => {
  assert.strictEqual(S.norm("శ్రీ. వేంకటేశ్వర!", "Name", { ignore_punct: true, ignore_case: true }), "శ్రీవేంకటేశ్వర");
  assert.strictEqual(S.norm("A.B.C. School", "Name", { ignore_punct: true }), "abcschool");
});
test("summary has counts and reasons", () => {
  const sm = S.mergeSummary(S.merge([sheetA, sheetB], { keys: ["Emails"] }));
  assert.strictEqual(sm.rows_in, 4); assert.strictEqual(sm.removed, 1); assert.strictEqual(sm.rows_out, 3);
  assert.match(sm.sample_removed[0].why, /Same Emails/);
});
test("CSV: BOM, quoting, escaped quotes, newlines", () => {
  const csv = S.toCSV(["a", "b"], [{ a: 'x,"y"', b: "l1\nl2" }]);
  assert.ok(csv.startsWith("﻿"));
  assert.ok(csv.includes('"x,""y"""') && csv.includes('"l1\nl2"'));
});
test("CSV: formulas neutralised but phone numbers untouched", () => {
  const csv = S.toCSV(["v"], [{ v: "=HYPERLINK(1)" }, { v: "+91 98480 12345" }, { v: "-5" }, { v: "@SUM(1)" }, { v: "+cmd|' /C calc'!A0" }]);
  const lines = csv.slice(1).split("\r\n");
  assert.strictEqual(lines[1], "'=HYPERLINK(1)");
  assert.strictEqual(lines[2], "+91 98480 12345");
  assert.strictEqual(lines[3], "-5");
  assert.strictEqual(lines[4], "'@SUM(1)");
  assert.ok(lines[5].startsWith("'+cmd"));
});
test("JSON export keeps column order and blanks", () => {
  const out = JSON.parse(S.toJSON(["a", "b"], [{ a: 1 }]));
  assert.deepStrictEqual(out, [{ a: 1, b: "" }]);
});
console.log(`\n${passed} tests passed`);
