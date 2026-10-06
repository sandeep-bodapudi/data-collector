/* Local storage for sheets and run history, in the browser's IndexedDB.
 * The server never stores sheets: it only runs the collection and hands the rows to the browser.
 * One database per signed-in user, so people sharing a computer never see each other's data. */
"use strict";

const Store = (() => {
  const VERSION = 1;
  let dbPromise = null;
  let userId = null;
  const state = { available: true, error: "" };

  function open() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      if (!("indexedDB" in window)) return reject(new Error("This browser does not support local storage (IndexedDB)."));
      let req;
      try { req = indexedDB.open(`onebridge-user-${userId}`, VERSION); }
      catch (e) { return reject(new Error("The browser blocked local storage. Turn off private browsing and try again.")); }
      req.onupgradeneeded = () => {
        const db = req.result;
        db.createObjectStore("sheets", { keyPath: "id" });      // metadata: listing never loads rows
        db.createObjectStore("sheet_data", { keyPath: "id" });  // {id, columns, rows}
        db.createObjectStore("runs", { keyPath: "id" });        // run history
      };
      req.onsuccess = () => {
        const db = req.result;
        db.onversionchange = () => { db.close(); dbPromise = null; };
        resolve(db);
      };
      req.onerror = () => reject(new Error("The browser blocked local storage. Turn off private browsing and try again."));
      req.onblocked = () => reject(new Error("Close other tabs of this app and try again."));
    }).catch((e) => { state.available = false; state.error = e.message; dbPromise = null; throw e; });
    return dbPromise;
  }

  // Runs fn(stores) inside one transaction and resolves with its result once the transaction has committed.
  async function tx(names, mode, fn) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction(names, mode);
      let result;
      t.oncomplete = () => resolve(result);
      t.onabort = t.onerror = () => {
        const err = t.error;
        reject(err && err.name === "QuotaExceededError"
          ? new Error("Your browser has no space left for sheets. Delete old sheets or free up disk space.")
          : new Error("Could not save to local storage" + (err ? ` (${err.name})` : ".")));
      };
      result = fn(Object.fromEntries(names.map((n) => [n, t.objectStore(n)])), t);
    });
  }
  const wrap = (req) => new Promise((res, rej) => { req.onsuccess = () => res(req.result); req.onerror = () => rej(req.error); });
  async function read(name, fn) {
    const db = await open();
    return wrap(fn(db.transaction(name, "readonly").objectStore(name)));
  }

  const approxBytes = (rows) => {
    if (!rows.length) return 0;
    const n = Math.min(200, rows.length);
    return Math.round(JSON.stringify(rows.slice(0, n)).length * rows.length / n);
  };

  const sheets = {
    async list() { return (await read("sheets", (s) => s.getAll())).sort((a, b) => b.created.localeCompare(a.created)); },
    get: (id) => read("sheets", (s) => s.get(id)),
    data: (id) => read("sheet_data", (s) => s.get(id)),
    /** Saves a new sheet. meta: {id, name, source, created, run_id?}; data: {columns, rows}. */
    async put(meta, data) {
      const full = { ...meta, rows: data.rows.length, columns: data.columns, size: approxBytes(data.rows) };
      await tx(["sheets", "sheet_data"], "readwrite", (st) => {
        st.sheets.put(full);
        st.sheet_data.put({ id: meta.id, columns: data.columns, rows: data.rows });
      });
      return full;
    },
    async rename(id, name) {
      const m = await sheets.get(id);
      if (m) await tx(["sheets"], "readwrite", (st) => st.sheets.put({ ...m, name }));
    },
    remove: (id) => tx(["sheets", "sheet_data"], "readwrite", (st) => { st.sheets.delete(id); st.sheet_data.delete(id); }),
  };

  const runs = {
    async list() { return (await read("runs", (s) => s.getAll())).sort((a, b) => b.started.localeCompare(a.started)); },
    get: (id) => read("runs", (s) => s.get(id)),
    put: (run) => tx(["runs"], "readwrite", (st) => { st.runs.put(run); }),
    remove: (id) => tx(["runs"], "readwrite", (st) => { st.runs.delete(id); }),
  };

  return {
    state,
    init(id) { userId = id; return open().then(() => true, () => false); },
    sheets, runs,
    /** Asks the browser not to evict our data when disk space runs low. */
    async persist() {
      try { return !!(navigator.storage && navigator.storage.persist && await navigator.storage.persist()); } catch { return false; }
    },
    async usage() {
      let est = {}, persisted = false;
      try { est = (navigator.storage && navigator.storage.estimate && await navigator.storage.estimate()) || {}; } catch {}
      try { persisted = !!(navigator.storage && navigator.storage.persisted && await navigator.storage.persisted()); } catch {}
      return { used: est.usage || 0, quota: est.quota || 0, persisted };
    },
    /** Everything in one object, for backup files. */
    async exportAll() {
      const [m, runList] = [await sheets.list(), await runs.list()];
      const data = [];
      for (const s of m) data.push(await sheets.data(s.id));
      return { app: "onebridge-data-collector", version: 1, exported: new Date().toISOString(), sheets: m, sheet_data: data, runs: runList };
    },
    async importAll(b) {
      if (!b || b.app !== "onebridge-data-collector" || !Array.isArray(b.sheets)) throw new Error("This is not an OneBridge backup file.");
      const byId = new Map((b.sheet_data || []).map((d) => [d.id, d]));
      let n = 0;
      for (const meta of b.sheets) {
        const d = byId.get(meta.id);
        if (!d) continue;
        await tx(["sheets", "sheet_data"], "readwrite", (st) => { st.sheets.put(meta); st.sheet_data.put(d); });
        n++;
      }
      for (const r of b.runs || []) await runs.put(r);
      return n;
    },
    async clearAll() { await tx(["sheets", "sheet_data", "runs"], "readwrite", (st) => { st.sheets.clear(); st.sheet_data.clear(); st.runs.clear(); }); },
  };
})();
