/* Sheet operations that run in the browser: Merge & Dedupe and CSV/JSON export.
 * Pure functions with no DOM access, so they can be unit-tested in Node (see tests/sheetops.test.js). */
(function (root) {
  "use strict";

  /* ---- matching ------------------------------------------------------------------------- */
  // Compares values the way a person would: "+91 98480 12345" equals "9848012345", "WWW.A.com/" equals "a.com".
  function norm(value, column, match) {
    let v = value === null || value === undefined ? "" : String(value);
    if (match.trim !== false) v = v.replace(/\s+/g, " ").trim();
    if (match.ignore_case !== false) v = v.toLowerCase();
    if (match.smart !== false) {
      const col = column.toLowerCase();
      if (col.includes("phone") || col.includes("mobile")) {
        const set = new Set();
        for (const p of v.split(/[;,/]/)) {
          const digits = p.replace(/[^0-9]/g, "");
          if (digits) set.add(digits.slice(-10));
        }
        v = [...set].sort().join(";");
      } else if (col.includes("url") || col.includes("website") || col.includes("link") || col.includes("domain")) {
        v = v.replace(/^https?:\/\/(www\.)?/, "").replace(/\/+$/, "");
      } else if (col.includes("email")) {
        const set = new Set(v.split(/[;,\s]+/).map((e) => e.trim()).filter(Boolean));
        v = [...set].sort().join(";");
      }
    }
    // Unicode-aware: keeps letters, digits and combining marks (the vowel signs of Telugu, Hindi, Tamil...).
    if (match.ignore_punct) v = v.replace(/[^\p{L}\p{M}\p{N}_;]+/gu, "");
    return v;
  }

  function filledCount(row) {
    let n = 0;
    for (const k in row) {
      const s = String(row[k] ?? "").trim();
      if (s !== "" && s !== "None") n++;
    }
    return n;
  }

  /* ---- merge & dedupe -------------------------------------------------------------------- */
  // sheets: [{name, columns, rows}] in priority order. Returns the combined sheet plus what was removed.
  function merge(sheets, opts) {
    const keys = opts.keys || [], match = opts.match || {}, keep = opts.keep || "first", fillEmpty = !!opts.fillEmpty;
    const columns = [];
    const all = [];
    for (const s of sheets) {
      for (const c of s.columns) if (!columns.includes(c)) columns.push(c);
      const label = String(s.name).replace(/\.(xlsx|csv|json)$/i, "");
      for (const r of s.rows) all.push({ ...r, "Source Sheet": label });
    }
    if (sheets.length > 1) columns.push("Source Sheet");
    const missing = keys.filter((k) => !columns.includes(k));
    if (missing.length) throw new Error("Column not found: " + missing.join(", "));
    if (!keys.length) return { columns, rows: all, removedRows: [], reasons: [], rowsIn: all.length };

    const groups = new Map();
    const unique = [];
    all.forEach((row, i) => {
      const key = keys.map((k) => norm(row[k], k, match));
      if (!key.some(Boolean)) { unique.push(i); return; }  // rows with no value in the key columns are never duplicates
      const id = key.join("\u0001");
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(i);
    });

    const kept = new Map(), removedRows = [], reasons = [];
    for (const idxs of groups.values()) {
      let winner = idxs[0];
      if (keep === "last") winner = idxs[idxs.length - 1];
      else if (keep === "most_complete") {
        winner = idxs.reduce((best, i) => {
          const a = filledCount(all[i]), b = filledCount(all[best]);
          return a > b || (a === b && i < best) ? i : best;
        }, idxs[0]);
      }
      const row = { ...all[winner] };
      for (const i of idxs) {
        if (i === winner) continue;
        removedRows.push(all[i]);
        reasons.push("Same " + keys.join(", ") + " as a kept row");
        if (fillEmpty) {
          for (const c in all[i]) {
            if (String(row[c] ?? "").trim() === "" && String(all[i][c] ?? "").trim() !== "") row[c] = all[i][c];
          }
        }
      }
      kept.set(winner, row);
    }
    const order = [...kept.keys(), ...unique].sort((a, b) => a - b);
    return { columns, rows: order.map((i) => kept.get(i) || all[i]), removedRows, reasons, rowsIn: all.length };
  }

  function mergeSummary(r) {
    return {
      rows_in: r.rowsIn, removed: r.removedRows.length, rows_out: r.rows.length, columns: r.columns,
      sample_removed: r.removedRows.slice(0, 15).map((row, i) => ({ row, why: r.reasons[i] })),
    };
  }

  /* ---- export ---------------------------------------------------------------------------- */
  function csvCell(v) {
    let s = v === null || v === undefined ? "" : String(v);
    // Stop spreadsheet formula injection from scraped text. Phone-like values such as "+91 98480 12345" are left alone.
    if (/^[=@\t\r]/.test(s) || /^[+-](?![\d\s().-]*$)/.test(s)) s = "'" + s;
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function toCSV(columns, rows) {
    // A byte-order mark makes Excel open Telugu/Hindi text correctly.
    const lines = [columns.map(csvCell).join(",")];
    for (const r of rows) lines.push(columns.map((c) => csvCell(r[c])).join(","));
    return "﻿" + lines.join("\r\n");
  }
  function toJSON(columns, rows) {
    return JSON.stringify(rows.map((r) => Object.fromEntries(columns.map((c) => [c, r[c] ?? ""]))), null, 2);
  }

  const api = { norm, merge, mergeSummary, toCSV, toJSON, filledCount };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.SheetOps = api;
})(typeof window !== "undefined" ? window : globalThis);
