/* OneBridge Data Collector - single-page front end (hash routing, no build step). */
"use strict";

const CFG = JSON.parse(document.getElementById("config").textContent);
const IS_VIEWER = CFG.user.role === "viewer";
const IS_ADMIN = CFG.user.role === "admin";
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (id, size = 16) => `<svg width="${size}" height="${size}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const fmtNum = (n) => Number(n || 0).toLocaleString();
const rowsText = (n) => `${fmtNum(n)} row${n === 1 ? "" : "s"}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fmtDuration(s) {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return `${m}m ${String(r).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
function fmtDate(iso, withTime = true) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, withTime ? { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" }
                                              : { day: "numeric", month: "short", year: "numeric" });
}
function timeAgo(iso) {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)} d ago`;
  return fmtDate(iso, false);
}
const STATUS_LABEL = { queued: "Queued", running: "Running", done: "Succeeded", cancelled: "Cancelled", error: "Failed" };
const badge = (status) => `<span class="badge ${esc(status)}">${STATUS_LABEL[status] || esc(status)}</span>`;
const SOURCE_LABEL = { run: "Run", merge: "Merged", import: "Imported", shared: "Shared copy" };

/* ---------------------------------------------------------------- API */
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  let r;
  try { r = await fetch(path, init); }
  catch { throw new Error("Can't reach the server. Check your connection and try again."); }
  if (r.status === 401) { location.href = "/login"; throw new Error("Signed out"); }
  const isJson = (r.headers.get("Content-Type") || "").includes("json");
  const data = isJson ? await r.json() : null;
  if (!r.ok) throw Object.assign(new Error((data && data.error) || `Something went wrong (${r.status}).`), { status: r.status });
  return data;
}

/* ---------------------------------------------------------------- local data helpers (sheets and runs live in this browser) */
const nowIso = () => new Date().toISOString();
const newId = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
const fileSafe = (n) => String(n || "").replace(/[\\/:*?"<>|]+/g, "").replace(/\s+/g, " ").trim().slice(0, 80) || "sheet";
const fmtSize = (b) => b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(b < 10240 ? 1 : 0)} KB` : b < 1073741824 ? `${(b / 1048576).toFixed(1)} MB` : `${(b / 1073741824).toFixed(2)} GB`;
function stampName(base) {
  const d = new Date();
  return `${base} · ${d.toLocaleDateString(undefined, { day: "numeric", month: "short" })} ${d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
async function exportSheet(sheetId, format) {
  let meta, data;
  try { [meta, data] = await Promise.all([Store.sheets.get(sheetId), Store.sheets.data(sheetId)]); } catch (e) { return toast(e.message, "err"); }
  if (!meta || !data) return toast("This sheet could not be found on this device.", "err");
  return exportData(meta.name, data.columns, data.rows, format);
}
async function exportData(name, columns, rows, format) {
  const base = fileSafe(name);
  if (format === "csv") return saveBlob(new Blob([SheetOps.toCSV(columns, rows)], { type: "text/csv;charset=utf-8" }), base + ".csv");
  if (format === "json") return saveBlob(new Blob([SheetOps.toJSON(columns, rows)], { type: "application/json" }), base + ".json");
  toast("Preparing your Excel file…");  // the server only formats it; nothing is stored there
  try {
    const r = await fetch("/api/export/xlsx", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, columns, rows }) });
    if (r.status === 401) { location.href = "/login"; return; }
    if (!r.ok) throw new Error(((await r.json().catch(() => ({}))).error) || "Could not create the Excel file.");
    saveBlob(await r.blob(), base + ".xlsx");
  } catch (e) {
    // fetch() only throws a TypeError when the server can't be reached at all
    toast(e instanceof TypeError ? "Can't reach the server for the Excel file. CSV and JSON downloads work without it." : e.message, "err");
  }
}

/* ---------------------------------------------------------------- run tracker
 * The server runs the collection. This watches every active run (even while you are on another page),
 * and when one finishes it saves the rows as a sheet in this browser and lets the server forget them. */
const Tracker = (() => {
  const ACTIVE = ["queued", "running"], FINISHED = ["done", "cancelled", "error"];
  const live = new Map(), watching = new Set(), saving = new Set(), listeners = new Set();
  let timer = null, busy = false;

  const updateNav = () => { const el = $("#nav-running"); if (el) { el.hidden = !watching.size; el.textContent = watching.size; } };
  const emit = (id, final) => { listeners.forEach((fn) => { try { fn(id, final); } catch (e) { console.error(e); } }); updateNav(); };

  async function save(run, job) {
    saving.add(run.id); emit(run.id);
    try {
      let sheetId = null;
      if (job.count > 0) {
        const res = await api(`/api/jobs/${run.id}/result`);
        sheetId = run.id;
        await Store.sheets.put({ id: sheetId, name: stampName(run.name), source: "run", created: nowIso(), run_id: run.id }, res);
        Store.persist();
        api(`/api/jobs/${run.id}`, { method: "DELETE" }).catch(() => {});  // saved: the server can forget the rows
      }
      await Store.runs.put({ ...run, status: job.status, rows: job.count, with_email: job.with_email, with_phone: job.with_phone,
        total: job.total, done: job.done, finished: nowIso(), duration: job.elapsed, error: job.error || "", sheet_id: sheetId, log: job.log, unsaved: false });
    } catch (e) {
      // The rows stay on the server for a while, so the person can free some space and try again.
      await Store.runs.put({ ...run, status: "error", rows: job.count, finished: nowIso(), duration: job.elapsed, log: job.log, unsaved: true,
        error: `The rows were collected but could not be saved on this device. ${e.message}` }).catch(() => {});
    } finally { saving.delete(run.id); live.delete(run.id); emit(run.id, true); }
  }

  async function tick() {
    if (busy) return;
    busy = true;
    try {
      for (const id of [...watching]) {
        let job;
        try { job = await api(`/api/jobs/${id}`); }
        catch (e) {
          if (e.status === 404) {  // the server restarted or already handed the rows over
            watching.delete(id); live.delete(id);
            const run = await Store.runs.get(id);
            if (run && ACTIVE.includes(run.status)) {
              await Store.runs.put({ ...run, status: "error", finished: nowIso(), error: "The server restarted while this run was going, so it was lost. Please start it again." });
            }
            emit(id, true);
          }
          continue;  // network hiccup: try again on the next tick
        }
        live.set(id, job);
        if (FINISHED.includes(job.status)) {
          watching.delete(id);
          // With two tabs open both see the finished run; only one may save it. The lock makes the second wait,
          // and the re-check inside finds the run already saved by the first.
          const saveOnce = async () => {
            const run = await Store.runs.get(id);
            if (run && ACTIVE.includes(run.status)) await save(run, job); else { live.delete(id); emit(id, true); }
          };
          if (navigator.locks) await navigator.locks.request("onebridge-save-" + id, saveOnce); else await saveOnce();
        } else emit(id);
      }
    } finally {
      busy = false;
      if (!watching.size && timer) { clearInterval(timer); timer = null; }
    }
  }
  const start = () => { if (!timer) timer = setInterval(tick, 1500); tick(); };

  return {
    live, saving,
    on(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    watch(id) { watching.add(id); start(); updateNav(); },
    /** On page load: pick up runs that were still going when the page was closed. */
    async resume() {
      try { for (const r of await Store.runs.list()) if (ACTIVE.includes(r.status)) watching.add(r.id); } catch { return; }
      if (watching.size) start();
      updateNav();
    },
    async retry(id) {
      const run = await Store.runs.get(id);
      const job = await api(`/api/jobs/${id}`);
      await save({ ...run, status: "running" }, job);
    },
  };
})();

/* ---------------------------------------------------------------- installable app (PWA) */
let deferredInstall = null;
const isStandalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
function syncInstallButton() { const b = $("#install-btn"); if (b) b.hidden = !deferredInstall || isStandalone(); }
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferredInstall = e; syncInstallButton(); window.dispatchEvent(new Event("install-state")); });
window.addEventListener("appinstalled", () => { deferredInstall = null; syncInstallButton(); toast("Installed. Open Data Collector from your apps.", "ok"); window.dispatchEvent(new Event("install-state")); });
async function promptInstall() {
  if (!deferredInstall) return false;
  deferredInstall.prompt();
  const { outcome } = await deferredInstall.userChoice;
  deferredInstall = null; syncInstallButton(); window.dispatchEvent(new Event("install-state"));
  return outcome === "accepted";
}
function syncOffline() { const c = $("#offline-chip"); if (c) c.hidden = navigator.onLine; }
window.addEventListener("online", () => { syncOffline(); toast("You're back online", "ok"); });
window.addEventListener("offline", () => { syncOffline(); toast("You're offline. Your saved sheets still work."); });
function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  const hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("Service worker not registered:", e));
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (hadController) toast("A new version is ready.", "", { label: "Reload", run: () => location.reload() });
  });
}

/* ---------------------------------------------------------------- toasts, dialogs, menus */
function toast(msg, kind = "", action) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `${kind === "err" ? icon("alert") : kind === "ok" ? icon("check") : icon("info")}<span>${esc(msg)}</span>`;
  if (action) {
    const b = document.createElement("button");
    b.textContent = action.label;
    b.onclick = () => { action.run(); el.remove(); };
    el.appendChild(b);
  }
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), action ? 8000 : 4500);
}

function modal({ title, text = "", body = "", confirm = "OK", cancel = "Cancel", danger = false, wide = false, onOpen, onConfirm }) {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "overlay";
    ov.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="m-title" style="${wide ? "max-width:640px" : ""}">
      <div class="modal-head"><h3 id="m-title">${esc(title)}</h3>${text ? `<p>${esc(text)}</p>` : ""}</div>
      ${body ? `<div class="modal-body">${body}</div>` : '<div style="height:8px"></div>'}
      <div class="modal-foot">${cancel ? `<button class="btn" data-x>${esc(cancel)}</button>` : ""}
        <button class="btn ${danger ? "btn-danger-solid" : "btn-primary"}" data-ok>${esc(confirm)}</button></div></div>`;
    document.body.appendChild(ov);
    const close = (v) => { ov.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") close(false); };
    document.addEventListener("keydown", onKey);
    ov.addEventListener("mousedown", (e) => { if (e.target === ov) close(false); });
    $("[data-x]", ov)?.addEventListener("click", () => close(false));
    const ok = $("[data-ok]", ov);
    ok.addEventListener("click", async () => {
      if (!onConfirm) return close(true);
      ok.disabled = true;
      try { const v = await onConfirm(ov); if (v !== false) close(v ?? true); }
      catch (e) { toast(e.message, "err"); }
      finally { ok.disabled = false; }
    });
    onOpen?.(ov);
    ($("input,select,textarea", ov) || ok).focus();
  });
}
const confirmDialog = (title, text, confirm = "Delete") => modal({ title, text, confirm, danger: true });

function openMenu(anchor, items) {
  closeMenus();
  const wrap = anchor.closest(".menu-wrap") || anchor.parentElement;
  const m = document.createElement("div");
  m.className = "menu";
  m.innerHTML = items.map((it, i) => it === "-" ? "<hr>" :
    `<button data-i="${i}" class="${it.danger ? "danger" : ""}">${it.icon ? icon(it.icon) : ""}${esc(it.label)}</button>`).join("");
  wrap.appendChild(m);
  m.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-i]");
    if (b) { closeMenus(); items[+b.dataset.i].run(); }
  });
  setTimeout(() => document.addEventListener("click", closeMenus, { once: true }), 0);
}
function closeMenus() { $$(".menu").forEach((m) => m.remove()); }

function emptyState(ico, title, text, actionHtml = "") {
  return `<div class="empty"><div class="e-ico">${icon(ico, 22)}</div><b>${esc(title)}</b><p>${esc(text)}</p>${actionHtml}</div>`;
}
function skeletonRows(n = 5, cols = 5) {
  return `<table class="tbl"><tbody>${Array.from({ length: n }, () =>
    `<tr>${Array.from({ length: cols }, () => '<td><div class="skel"></div></td>').join("")}</tr>`).join("")}</tbody></table>`;
}
function exportDataMenu(name, columns, rows) {
  return [
    { label: "Excel (.xlsx)", icon: "download", run: () => exportData(name, columns, rows, "xlsx") },
    { label: "CSV (.csv)", icon: "download", run: () => exportData(name, columns, rows, "csv") },
    { label: "JSON (.json)", icon: "download", run: () => exportData(name, columns, rows, "json") },
  ];
}
function exportMenu(sheetId) {
  return [
    { label: "Excel (.xlsx)", icon: "download", run: () => exportSheet(sheetId, "xlsx") },
    { label: "CSV (.csv)", icon: "download", run: () => exportSheet(sheetId, "csv") },
    { label: "JSON (.json)", icon: "download", run: () => exportSheet(sheetId, "json") },
  ];
}

/* ---------------------------------------------------------------- tag input */
function TagInput(box, { placeholder = "", disabled = false, onChange = () => {} } = {}) {
  const items = [];
  const input = document.createElement("input");
  input.disabled = disabled;
  input.setAttribute("aria-label", placeholder);
  box.classList.toggle("disabled", disabled);
  box.appendChild(input);
  const render = (silent) => {
    $$(".tag", box).forEach((t) => t.remove());
    items.forEach((v, i) => {
      const t = document.createElement("span");
      t.className = "tag";
      t.innerHTML = `<span title="Click to edit">${esc(v)}</span><button type="button" aria-label="Remove ${esc(v)}">${icon("x", 12)}</button>`;
      t.querySelector("button").onclick = (e) => { e.stopPropagation(); items.splice(i, 1); render(); };
      t.querySelector("span").onclick = (e) => { e.stopPropagation(); input.value = items.splice(i, 1)[0]; render(); input.focus(); };
      box.insertBefore(t, input);
    });
    input.placeholder = items.length ? "Add another…" : placeholder;
    if (!silent) onChange(items);
  };
  // One render for the whole batch - adding a pasted or generated list of hundreds one-by-one would otherwise
  // rebuild the entire tag list after each single item (quadratic, and visibly janky at "search 100+ areas" scale).
  const addMany = (list) => {
    let changed = false;
    for (let v of list) { v = v.trim(); if (v && !items.includes(v)) { items.push(v); changed = true; } }
    if (changed) render();
  };
  const add = (v) => addMany([v]);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); add(input.value); input.value = ""; }
    else if (e.key === "Backspace" && !input.value && items.length) { items.pop(); render(); }
  });
  input.addEventListener("blur", () => { if (input.value.trim()) { add(input.value); input.value = ""; } });
  input.addEventListener("paste", (e) => {
    const text = e.clipboardData.getData("text");
    if (/\r?\n/.test(text)) { e.preventDefault(); addMany(text.split(/\r?\n/)); }
  });
  box.addEventListener("click", () => input.focus());
  render(true);
  return { items, add, addMany, input, flush() { if (input.value.trim()) { add(input.value); input.value = ""; } } };
}

/* ---------------------------------------------------------------- shell: theme, nav, router */
const themeBtn = $("#theme-toggle");
function currentTheme() {
  return document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function setTheme(t) {
  if (t === "system") { delete document.documentElement.dataset.theme; try { localStorage.removeItem("theme"); } catch {} }
  else { document.documentElement.dataset.theme = t; try { localStorage.setItem("theme", t); } catch {} }
  themeBtn.innerHTML = icon(currentTheme() === "dark" ? "sun" : "moon", 18);
}
themeBtn.onclick = () => setTheme(currentTheme() === "dark" ? "light" : "dark");
setTheme(document.documentElement.dataset.theme || "system");
$("#menu-toggle").onclick = () => document.body.classList.toggle("nav-open");
document.addEventListener("click", (e) => {  // tap outside the mobile menu to close it
  if (document.body.classList.contains("nav-open") && !e.target.closest(".sidebar, #menu-toggle")) document.body.classList.remove("nav-open");
});

function setCrumbs(...parts) {
  // A part can be a plain string (a leaf title) or {label, href}; a saved run/sheet with a missing name (e.g. an
  // older run saved before a bug fix) falls back to "Untitled" here instead of crashing the whole page on it.
  const label = (p) => (p && p.label) || p || "Untitled";
  $("#crumbs").innerHTML = parts.map((p, i) => i === parts.length - 1 ? `<b>${esc(label(p))}</b>`
    : `<a href="${p.href}">${esc(label(p))}</a><span>/</span>`).join("");
  document.title = `${label(parts[parts.length - 1])} · OneBridge Data Collector`;
}

async function refreshNavCounts() {
  try {
    const sheets = await Store.sheets.list();
    const sh = $("#nav-sheets");
    sh.hidden = !sheets.length; sh.textContent = sheets.length;
  } catch { /* local storage unavailable: the page itself explains it */ }
}

const ROUTES = [
  [/^#\/home$/, () => viewHome()],
  [/^#\/new\/(web|places)$/, (m) => viewNewRun(m[1])],
  [/^#\/runs$/, () => viewRuns()],
  [/^#\/runs\/([\w-]+)$/, (m) => viewRunDetail(m[1])],
  [/^#\/sheets$/, () => viewSheets()],
  [/^#\/sheets\/([\w-]+)$/, (m) => viewSheet(m[1])],
  [/^#\/shared$/, () => viewShared()],
  [/^#\/shared\/([\w-]+)$/, (m) => viewSharedSheet(m[1])],
  [/^#\/merge(?:\?ids=([\w,-]*))?$/, (m) => viewMerge(m[1] ? m[1].split(",") : [])],
  [/^#\/settings$/, () => viewSettings()],
  [/^#\/admin$/, () => IS_ADMIN ? viewAdmin() : viewNotFound()],
];
let cleanup = null;
let ROUTE_SEQ = 0;
// Every view grabs its sequence number first; if the person navigates away while it is still
// loading, its late results are dropped instead of overwriting the new page.
const routeToken = () => ROUTE_SEQ;
const stale = (t) => t !== ROUTE_SEQ;
async function route() {
  ROUTE_SEQ++;
  if (cleanup) { cleanup(); cleanup = null; }
  closeMenus();
  document.body.classList.remove("nav-open");
  const hash = location.hash || "#/home";
  const key = hash.split("/")[1]?.split("?")[0] || "home";
  $$(".nav a[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === key));
  const view = $("#view");
  view.classList.toggle("full", /^#\/sheets\/[\w-]+$/.test(hash));
  view.innerHTML = `<div class="card card-body"><div class="skel" style="width:35%;height:22px"></div><div class="skel mt-16"></div><div class="skel mt-8" style="width:80%"></div><div class="skel mt-8" style="width:60%"></div></div>`;
  for (const [re, fn] of ROUTES) {
    const m = hash.match(re);
    if (m) {
      try { cleanup = (await fn(m)) || null; }
      catch (e) { view.innerHTML = `<div class="card">${emptyState("alert", "This page could not be loaded", e.message, '<a class="btn" href="#/home">Go home</a>')}</div>`; }
      view.focus({ preventScroll: true });
      window.scrollTo(0, 0);
      refreshNavCounts();
      return;
    }
  }
  viewNotFound();
}
function viewNotFound() {
  setCrumbs("Not found");
  $("#view").innerHTML = `<div class="card">${emptyState("alert", "Page not found", "This page doesn't exist or you don't have access.", '<a class="btn btn-primary" href="#/home">Go home</a>')}</div>`;
}
window.addEventListener("hashchange", route);

/* ================================================================ HOME */
async function viewHome() {
  const tk = routeToken();
  setCrumbs("Home");
  const v = $("#view");
  const hour = new Date().getHours();
  const hello = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  v.innerHTML = `
    <div class="page-head"><div><h1>${hello}, ${esc(CFG.user.username)}</h1>
      <p>Collect public data from the web into clean, ready-to-use spreadsheets.</p></div>
      ${IS_VIEWER ? "" : `<div class="actions"><a class="btn btn-primary" href="#/new/web">${icon("plus")}New run</a></div>`}</div>
    <div id="home-ai"></div>
    <div class="grid grid-4" id="kpis">${Array.from({ length: 4 }, () => '<div class="card kpi"><div class="skel" style="width:60%"></div><div class="skel mt-16" style="height:26px;width:40%"></div></div>').join("")}</div>
    ${IS_VIEWER ? "" : `<h2 style="font-size:15px;margin:28px 0 12px">Quick start</h2>
    <div class="grid grid-4 quick-grid">
      <a class="card quick" href="#/new/web"><div class="q-ico">${icon("globe", 20)}</div><div><b>Search the web</b><span>Emails, phones and details from websites</span></div></a>
      <a class="card quick" href="#/new/places"><div class="q-ico">${icon("pin", 20)}</div><div><b>Find places</b><span>Temples, hospitals, schools… in any city</span></div></a>
      <a class="card quick" href="#/sheets" data-import><div class="q-ico">${icon("upload", 20)}</div><div><b>Import a file</b><span>Bring in Excel, CSV or JSON</span></div></a>
      <a class="card quick" href="#/merge"><div class="q-ico">${icon("merge", 20)}</div><div><b>Merge &amp; dedupe</b><span>Combine sheets, remove duplicates</span></div></a>
    </div>`}
    <div id="home-lists" class="mt-24"></div>`;
  $("[data-import]", v)?.addEventListener("click", (e) => { e.preventDefault(); location.hash = "#/sheets"; setTimeout(pickImport, 150); });

  const [runs, sheets] = await Promise.all([Store.runs.list(), Store.sheets.list()]);
  if (stale(tk)) return;
  const week = Date.now() - 7 * 864e5;
  const d = { runs_total: runs.length, runs_week: runs.filter((r) => new Date(r.started).getTime() >= week).length,
    running: runs.filter((r) => RUN_ACTIVE.includes(r.status)).length, sheets_total: sheets.length,
    rows_total: sheets.reduce((a, x) => a + (x.rows || 0), 0) };
  $("#kpis").innerHTML = [
    ["runs", "Runs this week", fmtNum(d.runs_week), `${fmtNum(d.runs_total)} in total`],
    ["play", "Running now", fmtNum(d.running), d.running ? "Live progress in Runs" : "Nothing running"],
    ["sheet", "Sheets", fmtNum(d.sheets_total), "Saved on this device"],
    ["rows", "Rows collected", fmtNum(d.rows_total), "Across all your sheets"],
  ].map(([ic, label, val, sub]) => `<div class="card kpi"><small><span class="kpi-ico">${icon(ic)}</span>${label}</small><b>${val}</b><div class="sub">${sub}</div></div>`).join("");
  let aiReady = true;
  if (!IS_VIEWER) {
    try { aiReady = !!(await api("/api/settings")).ai_key_mask; } catch { /* offline: skip the hint */ }
    if (stale(tk)) return;
  }
  if (!aiReady && !IS_VIEWER) {
    $("#home-ai").innerHTML = `<div class="callout info" style="margin-bottom:16px">${icon("sparkle")}<div class="grow"><b>Collect any detail with AI.</b> Add your own AI key (OpenAI, Gemini, Claude, OpenRouter, Groq…) to fill custom columns like “opening hours” or “services offered”.</div><a class="btn btn-sm" href="#/settings">Add AI key</a></div>`;
  }
  const lists = $("#home-lists");
  if (!runs.length && !sheets.length) {
    lists.innerHTML = IS_VIEWER ? `<div class="card">${emptyState("sheet", "Nothing here yet", "Sheets shared with you will appear here.")}</div>` : `
      <div class="card"><div class="card-head"><div><h3>Get started in 3 steps</h3><p>Your first spreadsheet takes about two minutes.</p></div></div>
      <div class="card-body"><div class="walk">
        <div class="card w"><div class="n">1</div><b>Start a run</b><span>Type what you need, such as “CBSE schools in Vijayawada contact”, or pick a place type and a city.</span></div>
        <div class="card w"><div class="n">2</div><b>Watch it collect</b><span>See live progress, rows and contacts found. Stop anytime and keep what was found.</span></div>
        <div class="card w"><div class="n">3</div><b>Use your sheet</b><span>Open, search, merge and download as Excel, CSV or JSON.</span></div>
      </div><div class="mt-16"><a class="btn btn-primary" href="#/new/web">${icon("plus")}Start your first run</a></div></div></div>`;
    return;
  }
  lists.innerHTML = `<div class="grid grid-2">
    <div class="card"><div class="card-head"><h3>Recent runs</h3><a href="#/runs" class="small">View all</a></div>
      <div class="table-wrap">${runs.length ? `<table class="tbl compact"><tbody>${runs.slice(0, 6).map((r) => `
        <tr class="clickable" data-href="#/runs/${r.id}"><td><div class="name-cell"><div><b>${esc(r.name || "Untitled")}</b><small>${r.mode === "places" ? "Places" : "Web search"} · ${timeAgo(r.started)}</small></div></div></td>
        <td>${badge(r.status)}</td><td class="num">${fmtNum(r.rows)} rows</td></tr>`).join("")}</tbody></table>`
        : emptyState("runs", "No runs yet", "Start a run to collect data.")}</div></div>
    <div class="card"><div class="card-head"><h3>Recent sheets</h3><a href="#/sheets" class="small">View all</a></div>
      <div class="table-wrap">${sheets.length ? `<table class="tbl compact"><tbody>${sheets.slice(0, 6).map((s) => `
        <tr class="clickable" data-href="#/sheets/${s.id}"><td><div class="name-cell"><div class="file-ico">${icon("sheet")}</div><div><b>${esc(s.name)}</b><small>${SOURCE_LABEL[s.source] || "Run"} · ${timeAgo(s.created)}</small></div></div></td>
        <td class="num">${fmtNum(s.rows)} rows</td></tr>`).join("")}</tbody></table>`
        : emptyState("sheet", "No sheets yet", "Finished runs save a sheet automatically.")}</div></div></div>`;
  lists.addEventListener("click", (e) => { const tr = e.target.closest("tr[data-href]"); if (tr) location.hash = tr.dataset.href; });
}

/* ================================================================ NEW RUN */
// "title" isn't offered here: every row already gets a "Name" column (the site's own name, read off the page -
// see extract.clean_name / _site_name), which is what people actually want instead of a raw <title> tag.
const FIELD_INFO = {
  emails: ["mail", "Email addresses"], phones: ["phone", "Phone & mobile numbers"],
  address: ["pin", "Postal address"], description: ["text", "Short description of the site"],
  social: ["link", "Facebook, Instagram, LinkedIn links"], contact_page: ["contact", "Link to the Contact page"],
  text_snippet: ["text", "First 500 characters of the page"],
};
const DEFAULT_FIELDS = ["emails", "phones", "address"];
const PLATFORMS = [["web", "Websites", "globe"], ["linkedin.com", "LinkedIn", "link"], ["facebook.com", "Facebook", "link"], ["instagram.com", "Instagram", "link"], ["twitter.com", "X / Twitter", "link"]];
// Plain Emails/Phone/Address/Website are generic - they fit any business, but miss what actually matters for a
// temple, a hospital, a hotel... These are suggested AI details (see the "Other details with AI" box below),
// matched against the typed searches and any ticked directory source's category, not forced on anyone: they
// still cost an AI key to actually run, same as any other custom detail.
const FIELD_SUGGESTIONS = [
  { label: "Temples", keywords: ["temple", "mandir", "devasthanam", "swamy temple"], fields: ["Deity", "Darshan timings", "Major festivals", "Managed by (trust/devasthanam)"] },
  { label: "Hospitals", keywords: ["hospital", "clinic", "nursing home", "medical cent"], fields: ["Specialities", "Emergency services available", "Visiting hours", "Number of beds"] },
  { label: "Schools", keywords: ["school"], fields: ["Board (CBSE/ICSE/State)", "Grades offered", "Admission process", "Medium of instruction"] },
  { label: "Colleges", keywords: ["college", "university", "engineering college", "polytechnic"], fields: ["Courses offered", "Affiliated university", "Established year", "Accreditation"] },
  { label: "Restaurants", keywords: ["restaurant", "cafe", "dhaba", "eatery", "bakery"], fields: ["Cuisine", "Price range", "Opening hours", "Home delivery available"] },
  { label: "Hotels", keywords: ["hotel", "resort", "lodge", "guest house", "homestay"], fields: ["Star rating", "Room types", "Check-in / check-out time", "Amenities"] },
  { label: "Real estate", keywords: ["real estate", "property", "builders", "apartments for sale", "plots for sale"], fields: ["Property types", "Price range", "RERA number"] },
  { label: "Gyms & fitness", keywords: ["gym", "fitness cent", "yoga studio", "crossfit"], fields: ["Membership plans", "Trainers available", "Timings"] },
  { label: "Salons & spas", keywords: ["salon", " spa", "parlour", "parlor"], fields: ["Services offered", "Price range", "Timings"] },
];
const CAT_GROUPS = [
  ["Religious", ["Hindu temples", "Churches", "Mosques", "Gurudwaras", "Buddhist / Jain temples", "All places of worship"]],
  ["Health", ["Hospitals", "Clinics & doctors", "Pharmacies"]],
  ["Education", ["Schools", "Colleges & universities", "Engineering colleges"]],
  ["Food & stay", ["Restaurants", "Cafes", "Hotels"]],
  ["Business", ["Offices / companies", "IT companies", "Factories / industrial", "Supermarkets & shops", "Banks", "ATMs", "Petrol pumps"]],
  ["Public", ["Government offices", "Police stations", "Tourist attractions"]],
];

async function viewNewRun(mode) {
  if (IS_VIEWER) return viewNotFound();
  const tk = routeToken();
  // The person's own directory sources (Settings -> Directory sources), loaded fresh each time the form opens.
  const SOURCES = (await api("/api/sources").catch(() => ({ sources: [] }))).sources || [];
  const seedLabels = Object.fromEntries(SOURCES.map((x) => [String(x.id), x.name]));
  setCrumbs({ label: "Runs", href: "#/runs" }, "New run");
  const v = $("#view");
  let settings = { ai_key_mask: "", ai_provider: "anthropic" };
  try { settings = await api("/api/settings"); } catch {}
  if (stale(tk)) return;
  const aiReady = !!settings.ai_key_mask || (settings.ai_provider === "custom" && settings.ai_base_url);
  const providerLabel = (CFG.providers[settings.ai_provider] || {}).label || "your AI provider";

  const aiBlock = (id, suggest) => `
    <div class="field" style="margin-top:20px">
      <label class="label">${icon("sparkle")} Other details with AI <span class="opt">(optional)</span></label>
      <div class="tags" id="${id}"></div>
      <div class="hint">${aiReady ? `Type any detail in your own words, such as <i>opening hours</i> or <i>services offered</i>. Uses your ${esc(providerLabel)} key.`
        : `Add your own AI key in <a href="#/settings">Settings</a> to fill any custom detail.`}</div>
      ${suggest ? `<div class="hint" id="field-suggest" hidden></div>` : ""}
    </div>`;

  const webForm = `
    <div class="form-section">
      <div class="section-title"><div class="step-num">1</div><div><h3>What should we search for?</h3><p>Write it like a Google search. Add as many as you like.</p></div></div>
      <div class="section-body">
        <div class="tags" id="queries"></div>
        <div class="hint">Press <kbd>Enter</kbd> after each search. Paste a list to add many at once.</div>
        <div class="chips"><small>Try</small>
          ${["CBSE schools in Vijayawada contact", "real estate agents in Pune email", "textile exporters in Tiruppur", "IT companies in Hyderabad contact"].map((x) => `<button type="button" class="chip" data-q="${esc(x)}">${esc(x)}</button>`).join("")}
        </div>
        <details class="more mt-16" id="batch-gen">
          <summary><b class="small">${icon("plus", 14)} Generate many searches at once</b><span class="chev">${icon("chev", 16)}</span></summary>
          <div class="mt-8">
            <p class="hint" style="margin-top:0">For broad coverage (e.g. "100+ websites"), list the areas you want and one search pattern. Each area becomes its own search.</p>
            <div class="grid grid-2">
              <div class="field"><label class="label" for="batch-areas">Areas / items <span class="opt">(one per line)</span></label>
                <textarea class="input" id="batch-areas" rows="5" placeholder="Bachupally, Hyderabad&#10;Kukatpally, Hyderabad&#10;Ameerpet, Hyderabad&#10;Miyapur, Hyderabad"></textarea></div>
              <div class="field"><label class="label" for="batch-template">Search pattern <span class="opt">(use <code>{area}</code>)</span></label>
                <textarea class="input" id="batch-template" rows="5">engineering colleges in {area} contact email phone</textarea></div>
            </div>
            <div class="row-flex"><button type="button" class="btn btn-primary btn-sm" id="batch-add">Add these searches</button><span class="muted small" id="batch-count"></span></div>
          </div>
        </details>
        <div class="field mt-24"><label class="label">Search on</label>
          <div class="option-grid">${PLATFORMS.map(([val, label, ic]) => `
            <label class="option"><input type="checkbox" name="platform" value="${val}" ${val === "web" ? "checked" : ""}><span class="o-ico">${icon(ic)}</span><span><b>${label}</b><small>${val === "web" ? "All public websites" : "Public search results"}</small></span><span class="box"></span></label>`).join("")}
          </div>
          <div class="hint">${CFG.restricted ? "Logged-in collection for LinkedIn/Facebook is enabled by your admin and uses the cookies in your Settings."
            : "For social sites we collect only what appears in public search results (name, link, snippet). Their pages are not opened."}</div>
        </div>
        <div class="field mt-24"><label class="label">Your directory sources <span class="opt">(no search engine needed - pick as many as you like)</span></label>
          ${SOURCES.length ? `<div class="option-grid">${SOURCES.map((x) => `
            <label class="option"><input type="checkbox" name="sources" value="${x.id}"><span class="o-ico">${icon("globe")}</span><span><b>${esc(x.name)}</b><small>${esc(x.category || "")}</small></span><span class="box"></span></label>`).join("")}
          </div>
          <div class="hint">Read directly, page by page - not affected by free search engines refusing requests. Picking more than one combines them into the same sheet, with duplicate names removed automatically - useful since any one directory can be missing a few entries the others have. Adds to whatever searches you listed above; you can also leave the searches empty and use this alone.</div>`
            : `<div class="hint">You haven't added a directory source yet. Add one in <a href="#/settings">Settings → Directory sources</a>, or start from a preset there.</div>`}
        </div>
      </div>
    </div>
    <div class="form-section">
      <div class="section-title"><div class="step-num">2</div><div><h3>Which details do you need?</h3><p>Each one becomes a column in your sheet.</p></div></div>
      <div class="section-body">
        <div class="option-grid">${Object.keys(FIELD_INFO).filter((k) => k in CFG.fields).map((k) => `
          <label class="option"><input type="checkbox" name="field" value="${k}" ${DEFAULT_FIELDS.includes(k) ? "checked" : ""}><span class="o-ico">${icon(FIELD_INFO[k][0])}</span><span><b>${esc(CFG.fields[k])}</b><small>${esc(FIELD_INFO[k][1])}</small></span><span class="box"></span></label>`).join("")}
        </div>
        ${aiBlock("custom", true)}
      </div>
    </div>
    <div class="form-section">
      <div class="section-title"><div class="step-num muted">3</div><div><h3>Options</h3><p>The defaults work well for most searches.</p></div></div>
      <div class="section-body">
        <div class="grid grid-2">
          <div class="field"><label class="label">Websites per search</label>
            <div class="range-row"><input type="range" id="max_results" min="5" max="500" step="5" value="30" aria-label="Websites per search"><output id="max_out">30</output></div>
            <div class="hint">More websites means more rows, but the run takes longer.</div></div>
          <div class="field"><label class="label" for="region">Country</label>
            <select class="input" id="region">${Object.entries(CFG.regions).map(([k, val]) => `<option value="${k}" ${k === "in-en" ? "selected" : ""}>${esc(val)}</option>`).join("")}</select>
            <div class="hint">Search results from this country come first.</div></div>
        </div>
        <div class="field"><label class="label">Keep only rows that have</label>
          <div class="seg" id="require">${[["", "Everything"], ["emails", "An email"], ["phones", "A phone"], ["any_contact", "Email or phone"]].map(([val, l], i) => `<button type="button" data-v="${val}" class="${i ? "" : "active"}">${l}</button>`).join("")}</div></div>
        <label class="switch"><input type="checkbox" id="follow_contact" checked><span class="sw"></span><span><b>Check “Contact us” and “About” pages</b><small>Finds many more emails and phone numbers.</small></span></label>
        <label class="switch mt-8"><input type="checkbox" id="one_per_site" checked><span class="sw"></span><span><b>One row per website</b><small>Skips duplicate pages from the same site.</small></span></label>
      </div>
    </div>`;

  const placesForm = `
    <div class="form-section">
      <div class="section-title"><div class="step-num">1</div><div><h3>What kind of place?</h3><p>Pick one category.</p></div></div>
      <div class="section-body">
        <div class="search" style="max-width:340px;margin-bottom:14px">${icon("search")}<input id="cat-filter" placeholder="Search categories, e.g. hospital" aria-label="Search categories"></div>
        <div id="cats"></div>
      </div>
    </div>
    <div class="form-section">
      <div class="section-title"><div class="step-num">2</div><div><h3>Where?</h3><p>A city, district, state or country. Add several if you like.</p></div></div>
      <div class="section-body">
        <div class="tags" id="locations"></div>
        <div class="hint">Press <kbd>Enter</kbd> after each place. Adding the country gives better matches.</div>
        <div class="chips"><small>Try</small>${["Bachupally, Hyderabad", "Hyderabad, India", "Vijayawada, India", "Tirupati, India"].map((x) => `<button type="button" class="chip" data-l="${esc(x)}">${esc(x)}</button>`).join("")}</div>
      </div>
    </div>
    <div class="form-section">
      <div class="section-title"><div class="step-num muted">3</div><div><h3>Options</h3><p>Filter by name and add contact details from websites.</p></div></div>
      <div class="section-body">
        <div class="grid grid-2">
          <div class="field"><label class="label" for="name_filter">Name contains <span class="opt">(optional)</span></label><input class="input" id="name_filter" placeholder="e.g. Venkateswara, Apollo"></div>
          <div class="field"><label class="label">Max places per location</label><div class="range-row"><input type="range" id="max_places" min="10" max="500" step="10" value="300" aria-label="Max places per location"><output id="places_out">300</output></div></div>
        </div>
        <label class="switch"><input type="checkbox" id="enrich" checked><span class="sw"></span><span><b>Find emails &amp; phone numbers</b><small>Reads each place's website for contact details. The map itself rarely has them, so leave this on if you need contacts. Slower.</small></span></label>
        <label class="switch mt-8" id="find-sites-row"><input type="checkbox" id="find_sites" checked><span class="sw"></span><span><b>Look up websites the map doesn't list</b><small>Searches the web for each place's own site first (about 2 seconds per place, up to 80 per run). Without this, only places with a website on the map get contacts.</small></span></label>
        ${aiBlock("custom-places")}
        <div class="callout info mt-16">${icon("info")}<div>Places come from OpenStreetMap, a free public map. Well-known places are almost always listed; very small ones may be missing.</div></div>
      </div>
    </div>`;

  v.innerHTML = `
    <div class="page-head"><div><h1>New run</h1><p>Tell us what to collect. You'll get a sheet you can open, merge and download.</p></div>
      <div class="seg" role="tablist" aria-label="Run type">
        <button type="button" class="${mode === "web" ? "active" : ""}" data-mode="web">${icon("globe")} Web search</button>
        <button type="button" class="${mode === "places" ? "active" : ""}" data-mode="places">${icon("pin")} Places</button>
      </div></div>
    <div class="split">
      <div class="card">${mode === "web" ? webForm : placesForm}</div>
      <aside class="card summary-panel">
        <div class="card-head"><h3>Run summary</h3></div>
        <div class="card-body">
          <ul class="summary-list" id="summary"></ul>
          <ul class="checklist" id="checks"></ul>
          <div class="field mt-16"><label class="label" for="file_name">Sheet name <span class="opt">(optional)</span></label><input class="input" id="file_name" placeholder="e.g. Hyderabad IT leads" maxlength="80"></div>
          <button class="btn btn-primary btn-lg btn-block" id="start">${icon("play")}Start run</button>
          <p class="hint" style="text-align:center">Respects each site's robots.txt and waits between visits.</p>
        </div>
      </aside>
    </div>`;
  $$("[data-mode]", v).forEach((b) => b.onclick = () => { location.hash = `#/new/${b.dataset.mode}`; });

  const state = { require: "", category: "" };
  let queries, locations, custom, dismissedSuggestion = "";
  // Which of FIELD_SUGGESTIONS, if any, matches what's typed so far - the searches themselves, and any ticked
  // directory source's own category (set when that source was added in Settings), not guessed from nothing.
  const matchedSuggestion = () => {
    const sourceCats = $$("input[name=sources]:checked", v).map((c) => ((SOURCES.find((x) => String(x.id) === c.value) || {}).category || "").toLowerCase());
    const text = (queries?.items || []).join(" ").toLowerCase();
    return FIELD_SUGGESTIONS.find((fs) => fs.keywords.some((k) => text.includes(k)) || sourceCats.some((c) => c.includes(fs.label.toLowerCase()) || fs.label.toLowerCase().includes(c)));
  };
  const renderSuggestion = () => {
    const box = $("#field-suggest");
    if (!box) return;
    const match = matchedSuggestion();
    if (!match || match.label === dismissedSuggestion) { box.hidden = true; return; }
    const missing = match.fields.filter((f) => !custom.items.includes(f));
    if (!missing.length) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = `<b>${esc(match.label)} search detected.</b> Suggested details${aiReady ? "" : " (needs your AI key in Settings)"}: `
      + missing.map((f) => `<button type="button" class="chip" data-sf="${esc(f)}">+ ${esc(f)}</button>`).join(" ")
      + ` <button type="button" class="btn btn-sm" id="sf-all">Add all</button> <button type="button" class="btn btn-sm" id="sf-dismiss">Not this</button>`;
    $$("[data-sf]", box).forEach((b) => b.onclick = () => custom.add(b.dataset.sf));
    $("#sf-all").onclick = () => custom.addMany(missing);
    $("#sf-dismiss").onclick = () => { dismissedSuggestion = match.label; renderSuggestion(); };
  };
  const update = () => {
    const rows = [], checks = [];
    if (mode === "web") {
      const fields = $$("input[name=field]:checked", v).map((c) => CFG.fields[c.value]).concat(custom.items);
      const plats = $$("input[name=platform]:checked", v).map((c) => PLATFORMS.find((p) => p[0] === c.value)[1]);
      const sourceEls = $$("input[name=sources]:checked", v);
      const seedOn = sourceEls.length > 0;
      rows.push(["Searches", queries.items.length || "—"], ["Search on", plats.join(", ") || "—"],
        ["Websites", queries.items.length ? `up to ${fmtNum(queries.items.length * plats.length * +$("#max_results").value)}` : "—"],
        ["Country", CFG.regions[$("#region").value]], ["Columns", fields.length ? `${fields.length} details` : "—"]);
      if (seedOn) rows.splice(1, 0, ["Directory source" + (sourceEls.length > 1 ? "s" : ""), sourceEls.map((c) => seedLabels[c.value]).join(", ")]);
      checks.push([queries.items.length > 0 || seedOn, "At least one search, or a directory source"], [fields.length > 0, "At least one detail"], [plats.length > 0, "A place to search"]);
      renderSuggestion();
    } else {
      rows.push(["Category", state.category], ["Locations", locations.items.length ? locations.items.slice(0, 2).join("; ") + (locations.items.length > 2 ? ` +${locations.items.length - 2}` : "") : "—"],
        ["Max per location", fmtNum($("#max_places").value)], ["Website check", $("#enrich").checked || custom.items.length ? "Yes" : "No"]);
      checks.push([!!state.category, "A category"], [locations.items.length > 0, "At least one location"]);
    }
    if (custom.items.length) { rows.push(["AI details", custom.items.length]); checks.push([aiReady, "Your AI key is set"]); }
    $("#summary").innerHTML = rows.map(([k, val]) => `<li><span>${esc(k)}</span><b>${esc(val)}</b></li>`).join("");
    $("#checks").innerHTML = checks.map(([ok, l]) => `<li class="${ok ? "ok" : "todo"}">${esc(l)}</li>`).join("");
  };

  if (mode === "web") {
    queries = TagInput($("#queries"), { placeholder: "e.g. software companies in Hyderabad contact email", onChange: update });
    $$("[data-q]", v).forEach((b) => b.onclick = () => queries.add(b.dataset.q));
    const batchExpand = () => {
      const areas = $("#batch-areas").value.split("\n").map((x) => x.trim()).filter(Boolean);
      const tpl = $("#batch-template").value.trim();
      const made = tpl.includes("{area}") ? areas.map((a) => tpl.replace(/\{area\}/g, a)) : [];
      return { areas, made };
    };
    const batchSync = () => {
      const { areas, made } = batchExpand();
      $("#batch-count").textContent = !areas.length ? "" : !$("#batch-template").value.includes("{area}")
        ? "Add {area} to the pattern so each line becomes its own search." : `= ${fmtNum(made.length)} search${made.length === 1 ? "" : "es"}`;
    };
    $("#batch-areas").oninput = batchSync; $("#batch-template").oninput = batchSync;
    $("#batch-add").onclick = () => {
      const { made } = batchExpand();
      if (!made.length) return toast("Add at least one area, and keep {area} in the pattern.", "err");
      queries.addMany(made);
      $("#batch-areas").value = ""; $("#batch-count").textContent = "";
      toast(`Added ${fmtNum(made.length)} search${made.length === 1 ? "" : "es"}`, "ok");
    };
    custom = TagInput($("#custom"), { placeholder: aiReady ? "e.g. founder name, services offered" : "Add your AI key in Settings first", disabled: !aiReady, onChange: update });
    $("#max_results").oninput = () => { $("#max_out").textContent = $("#max_results").value; update(); };
    $$("#require button", v).forEach((b) => b.onclick = () => { $$("#require button", v).forEach((x) => x.classList.remove("active")); b.classList.add("active"); state.require = b.dataset.v; });
    $$("input[name=field], input[name=platform], input[name=sources], #region", v).forEach((c) => c.addEventListener("change", update));
    setTimeout(() => queries.input.focus(), 50);
  } else {
    locations = TagInput($("#locations"), { placeholder: "e.g. Bachupally, Hyderabad", onChange: update });
    $$("[data-l]", v).forEach((b) => b.onclick = () => locations.add(b.dataset.l));
    custom = TagInput($("#custom-places"), { placeholder: aiReady ? "e.g. main deity, temple timings" : "Add your AI key in Settings first", disabled: !aiReady, onChange: update });
    const renderCats = () => {
      const q = $("#cat-filter").value.trim().toLowerCase();
      const known = new Set(CAT_GROUPS.flatMap((g) => g[1]));
      const groups = [...CAT_GROUPS, ["Other", CFG.categories.filter((c) => !known.has(c))]];
      $("#cats").innerHTML = groups.map(([name, cats]) => {
        const list = cats.filter((c) => CFG.categories.includes(c) && (!q || c.toLowerCase().includes(q) || name.toLowerCase().includes(q)));
        return list.length ? `<div class="cat-group"><h4>${esc(name)}</h4><div class="cats">${list.map((c) =>
          `<button type="button" class="cat ${c === state.category ? "active" : ""}" data-c="${esc(c)}" aria-pressed="${c === state.category}">${esc(c)}</button>`).join("")}</div></div>` : "";
      }).join("") || '<p class="muted">No category matches. Try another word.</p>';
      $$(".cat", v).forEach((b) => b.onclick = () => { state.category = b.dataset.c; renderCats(); update(); });
    };
    $("#cat-filter").oninput = renderCats;
    renderCats();
    $("#max_places").oninput = () => { $("#places_out").textContent = $("#max_places").value; update(); };
    const syncSites = () => { $("#find-sites-row").hidden = !$("#enrich").checked; };
    $("#enrich").onchange = () => { syncSites(); update(); };
    syncSites();
  }
  update();

  $("#start").onclick = async () => {
    custom.flush();
    const body = { mode, file_name: $("#file_name").value, custom_fields: custom.items.join(",") };
    if (mode === "web") {
      queries.flush();
      const sourceIds = $$("input[name=sources]:checked", v).map((c) => +c.value);
      Object.assign(body, {
        queries: queries.items.join("\n"), max_results: $("#max_results").value, region: $("#region").value,
        require: state.require, follow_contact: $("#follow_contact").checked, one_per_site: $("#one_per_site").checked,
        sources: sourceIds,
        fields: $$("input[name=field]:checked", v).map((c) => c.value), platforms: $$("input[name=platform]:checked", v).map((c) => c.value),
      });
      if (!queries.items.length && !body.sources.length) { toast("Add at least one search in step 1, or pick one or more of your directory sources.", "err"); return queries.input.focus(); }
      if (!body.platforms.length) return toast("Choose at least one place to search.", "err");
      if (!body.fields.length && !custom.items.length) return toast("Pick at least one detail in step 2.", "err");
    } else {
      locations.flush();
      if (!state.category) { toast("Choose a category in step 1.", "err"); return $("#cat-filter").focus(); }
      Object.assign(body, { category: state.category, locations: locations.items.join("\n"), name_filter: $("#name_filter").value,
        max_results: $("#max_places").value, enrich: $("#enrich").checked, find_websites: $("#find_sites").checked });
      if (!locations.items.length) { toast("Add at least one location in step 2.", "err"); return locations.input.focus(); }
    }
    if (!navigator.onLine) return toast("You're offline. Connect to the internet to start a run.", "err");
    if (!Store.state.available) return toast(Store.state.error || "Local storage is not available, so results could not be kept.", "err");
    const btn = $("#start");
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Starting…';
    try {
      const r = await api("/api/jobs", { method: "POST", body });
      const lines = (t) => String(t || "").split("\n").map((x) => x.trim()).filter(Boolean);
      const sourceNames = (body.sources || []).map((id) => seedLabels[id]).filter(Boolean);
      const spec = { ...body, queries: lines(body.queries), locations: lines(body.locations), sourceNames,
        custom_fields: String(body.custom_fields || "").split(",").map((x) => x.trim()).filter(Boolean) };
      const name = body.file_name || (mode === "places" ? `${body.category} in ${spec.locations.slice(0, 2).join(", ")}`
        : spec.queries[0] || sourceNames.join(", ") || "Web search");
      await Store.runs.put({ id: r.id, name, mode, spec, status: "running", started: nowIso(), rows: 0, with_email: 0, with_phone: 0,
        duration: 0, error: "", sheet_id: null });
      Tracker.watch(r.id);
      location.hash = `#/runs/${r.id}`;
    } catch (e) {
      toast(e.message, "err");
      btn.disabled = false; btn.innerHTML = `${icon("play")}Start run`;
    }
  };
}

/* ================================================================ RUNS */
const RUN_ACTIVE = ["queued", "running"];
const RUN_FINISHED = ["done", "cancelled", "error"];
// A run that is still going shows the live numbers from the server instead of the saved record.
function effectiveRun(r) {
  const j = Tracker.live.get(r.id);
  if (j && RUN_ACTIVE.includes(r.status)) {
    return { ...r, status: RUN_FINISHED.includes(j.status) ? "running" : j.status, rows: j.count, duration: j.elapsed };
  }
  return r;
}

async function viewRuns() {
  const tk = routeToken();
  setCrumbs("Runs");
  const v = $("#view");
  const state = { q: "", status: "all" };
  v.innerHTML = `
    <div class="page-head"><div><h1>Runs</h1><p>Your collections. The history is kept on this device.</p></div>
      ${IS_VIEWER ? "" : `<div class="actions"><a class="btn btn-primary" href="#/new/web">${icon("plus")}New run</a></div>`}</div>
    <div class="card">
      <div class="toolbar">
        <div class="search grow" style="max-width:360px">${icon("search")}<input id="q" placeholder="Search runs" aria-label="Search runs"></div>
        <div class="seg" id="status">${[["all", "All"], ["running", "Running"], ["done", "Succeeded"], ["cancelled", "Cancelled"], ["error", "Failed"]].map(([k, l], i) => `<button data-s="${k}" class="${i ? "" : "active"}">${l}</button>`).join("")}</div>
      </div>
      <div class="table-wrap" id="list">${skeletonRows(6, 6)}</div>
    </div>`;
  let runs = [];
  const load = async () => { const r = await Store.runs.list(); if (stale(tk)) return; runs = r.map(effectiveRun); render(); };
  const render = () => {
    const list = runs.filter((r) => (state.status === "all" || r.status === state.status || (state.status === "running" && r.status === "queued"))
      && (!state.q || (r.name || "").toLowerCase().includes(state.q)));
    $("#list").innerHTML = !runs.length ? emptyState("runs", "No runs yet", "Start a run to collect data from the web.", IS_VIEWER ? "" : `<a class="btn btn-primary" href="#/new/web">${icon("plus")}New run</a>`)
      : !list.length ? emptyState("search", "No matching runs", "Try a different search or filter.")
      : `<table class="tbl"><thead><tr><th>Run</th><th>Status</th><th>Started</th><th class="num">Duration</th><th class="num">Rows</th><th></th></tr></thead><tbody>
        ${list.map((r) => `<tr class="clickable" data-id="${r.id}">
          <td><div class="name-cell"><div class="kpi-ico">${icon(r.mode === "places" ? "pin" : "globe")}</div><div><b>${esc(r.name || "Untitled")}</b><small>${runSubtitle(r)}</small></div></div></td>
          <td>${badge(r.status)}</td>
          <td class="nowrap" title="${esc(fmtDate(r.started))}">${timeAgo(r.started)}</td>
          <td class="num">${fmtDuration(r.duration)}</td><td class="num">${fmtNum(r.rows)}</td>
          <td class="actions"><span class="menu-wrap">${r.sheet_id ? `<a class="btn btn-sm" href="#/sheets/${r.sheet_id}" data-stop>Open sheet</a>` : ""}
            <button class="btn btn-ghost btn-sm btn-icon" data-menu="${r.id}" aria-label="More actions">${icon("dots")}</button></span></td></tr>`).join("")}
        </tbody></table>`;
  };
  $("#q").oninput = (e) => { state.q = e.target.value.trim().toLowerCase(); render(); };
  $$("#status button").forEach((b) => b.onclick = () => { $$("#status button").forEach((x) => x.classList.remove("active")); b.classList.add("active"); state.status = b.dataset.s; render(); });
  $("#list").addEventListener("click", (e) => {
    if (e.target.closest("[data-stop]")) return;
    const m = e.target.closest("[data-menu]");
    if (m) {
      e.stopPropagation();
      const r = runs.find((x) => x.id === m.dataset.menu);
      return openMenu(m, [
        { label: "View details", icon: "eye", run: () => location.hash = `#/runs/${r.id}` },
        ...(r.sheet_id ? exportMenu(r.sheet_id) : []),
        "-",
        { label: "Delete from history", icon: "trash", danger: true, run: async () => {
          if (RUN_ACTIVE.includes(r.status)) return toast("Stop the run before deleting it.", "err");
          if (!await confirmDialog("Delete this run?", "The run is removed from your history. Its sheet stays in Sheets.")) return;
          try { await Store.runs.remove(r.id); toast("Run deleted", "ok"); load(); } catch (err) { toast(err.message, "err"); }
        } },
      ]);
    }
    const tr = e.target.closest("tr[data-id]");
    if (tr) location.hash = `#/runs/${tr.dataset.id}`;
  });
  await load();
  const off = Tracker.on(() => load().catch(() => {}));
  return off;
}
function runSubtitle(r) {
  const s = r.spec || {};
  if (r.mode === "places") return `Places · ${esc(s.category || "")}${s.locations && s.locations.length ? ` · ${esc(s.locations.slice(0, 2).join(", "))}` : ""}`;
  if (r.mode === "enrich") return `Fill missing details${s.source_sheet ? ` · ${esc(s.source_sheet)}` : ""}`;
  return `Web search${s.queries && s.queries.length ? ` · ${s.queries.length} search${s.queries.length > 1 ? "es" : ""}` : ""}${s.region ? ` · ${esc(CFG.regions[s.region] || "")}` : ""}`;
}

/* ================================================================ RUN DETAIL */
async function viewRunDetail(id) {
  const tk = routeToken();
  setCrumbs({ label: "Runs", href: "#/runs" }, "Run");
  const v = $("#view");
  let run = await Store.runs.get(id);
  if (stale(tk)) return;
  if (!run) return viewNotFound();
  setCrumbs({ label: "Runs", href: "#/runs" }, run.name);
  const isPlaces = run.mode === "places";
  v.innerHTML = `
    <div class="page-head"><div><div class="row-flex"><h1>${esc(run.name || "Untitled")}</h1><span id="badge"></span></div>
      <p>${runSubtitle(run)} · started ${esc(fmtDate(run.started))}</p></div>
      <div class="actions" id="run-actions"></div></div>
    <div class="card"><div class="card-body">
      <div class="steps" id="steps"></div>
      <div class="row-flex" style="margin-bottom:10px"><span class="spinner" id="spin" style="color:var(--accent)"></span><span id="activity" class="muted"></span></div>
      <div class="progress" id="bar"><div></div></div>
      <div class="progress-meta"><span id="meta-l"></span><span id="meta-r"></span></div>
      <div class="grid grid-4 mt-16">
        ${[["rows", "Rows collected", "s-rows"], ["mail", "With email", "s-email"], ["phone", "With phone", "s-phone"], ["clock", "Elapsed", "s-time"]].map(([ic, l, idv]) =>
          `<div class="card kpi" style="box-shadow:none;background:var(--surface-2)"><small><span class="kpi-ico">${icon(ic)}</span>${l}</small><b id="${idv}">—</b></div>`).join("")}
      </div>
      <div id="result"></div>
    </div></div>
    <div class="card mt-16">
      <div class="tabs" role="tablist"><button class="active" data-tab="results">Results</button><button data-tab="log">Log</button><button data-tab="input">Input</button>
        <button class="btn btn-sm" id="copy-log" style="margin-left:auto" hidden>${icon("copy", 14)}Copy log</button></div>
      <div id="tab-results"></div>
      <pre class="log" id="tab-log" hidden></pre>
      <div id="tab-input" class="card-body" hidden></div>
    </div>`;
  $$(".tabs button[data-tab]", v).forEach((b) => b.onclick = () => {
    $$(".tabs button[data-tab]", v).forEach((x) => x.classList.toggle("active", x === b));
    ["results", "log", "input"].forEach((t) => $(`#tab-${t}`).hidden = t !== b.dataset.tab);
    $("#copy-log").hidden = b.dataset.tab !== "log";
  });
  $("#copy-log").onclick = async () => {
    const ok = await copyText($("#tab-log").textContent);
    toast(ok ? "Log copied" : "Couldn't copy - select the text and copy it manually", ok ? "ok" : "err");
  };
  const INPUT_LABELS = { queries: "Searches", category: "Category", locations: "Locations", region: "Country", fields: "Details", custom_fields: "AI details",
    platforms: "Search on", max_results: "Max results", require: "Keep only rows with", follow_contact: "Check contact pages", one_per_site: "One row per website",
    enrich: "Check websites", name_filter: "Name contains", file_name: "Sheet name", source_sheet: "From sheet",
    sourceNames: "Directory sources" };
  const spec = run.spec || {};
  $("#tab-input").innerHTML = `<ul class="summary-list">${Object.entries(spec).filter(([k, val]) => INPUT_LABELS[k] && val !== "" && val !== null && !(Array.isArray(val) && !val.length))
    .map(([k, val]) => `<li><span>${INPUT_LABELS[k]}</span><b>${esc(k === "region" ? CFG.regions[val] || val : Array.isArray(val) ? val.join("; ") : typeof val === "boolean" ? (val ? "Yes" : "No") : val)}</b></li>`).join("")}</ul>`;

  let sheetData = null;  // rows of the saved sheet, shown in Results once the run has finished
  const paint = () => {
    const job = Tracker.live.get(id) || null;
    const finished = !RUN_ACTIVE.includes(run.status);
    const jobDone = !!job && RUN_FINISHED.includes(job.status);
    const savingNow = Tracker.saving.has(id) || (!finished && jobDone);
    const status = finished ? run.status : (job && !jobDone ? job.status : "running");
    $("#badge").innerHTML = badge(status);
    const steps = isPlaces ? [["search", "Find places"], ["visit", "Check websites"], ["save", "Save sheet"], ["done", "Done"]]
                           : [["search", "Search"], ["visit", "Read websites"], ["save", "Save sheet"], ["done", "Done"]];
    const phase = finished ? "done" : savingNow ? "save" : job ? job.phase : "search";
    const cur = steps.findIndex((s) => s[0] === phase);
    $("#steps").innerHTML = steps.map(([, l], i) => {
      const cls = finished ? "did" : i < cur ? "did" : i === cur ? "doing" : "";
      return `<div class="s ${cls}"><span class="d">${cls === "did" ? "✓" : i + 1}</span><span class="l">${l}</span></div>`;
    }).join("");
    $("#spin").hidden = finished;
    $("#activity").textContent = finished ? "This run has finished." : savingNow ? "Saving the sheet on this device…" : job ? job.activity : "Waiting for the server…";
    const pct = finished ? 100 : job && job.total ? Math.round(100 * job.done / job.total) : 0;
    $("#bar").classList.toggle("indeterminate", !finished && !savingNow && (!job || !job.total || job.phase === "search"));
    $("#bar > div").style.width = (savingNow ? 100 : pct) + "%";
    if (!finished && !savingNow && job && job.total && job.phase !== "search") {
      $("#meta-l").textContent = `${fmtNum(job.done)} of ${fmtNum(job.total)} ${isPlaces && job.phase !== "visit" ? "locations" : "websites"} · ${pct}%`;
      const left = job.done > 2 ? job.elapsed / job.done * (job.total - job.done) : null;
      $("#meta-r").textContent = left == null ? "Estimating time left…" : left < 60 ? "Less than a minute left" : `About ${Math.round(left / 60)} min left`;
    } else { $("#meta-l").textContent = finished || savingNow ? "" : "Working…"; $("#meta-r").textContent = ""; }
    const src = job && !finished ? job : null;
    $("#s-rows").textContent = fmtNum(src ? src.count : run.rows);
    $("#s-email").textContent = fmtNum(src ? src.with_email : run.with_email);
    $("#s-phone").textContent = fmtNum(src ? src.with_phone : run.with_phone);
    $("#s-time").textContent = fmtDuration(src ? src.elapsed : run.duration);
    const sheetId = run.sheet_id;
    $("#run-actions").innerHTML = (!finished && job && !jobDone ? `<button class="btn btn-danger" id="stop">${icon("stop")}Stop &amp; keep results</button>` : "")
      + (run.unsaved ? `<button class="btn btn-primary" id="retry">Try saving again</button>` : "")
      + (sheetId ? `<span class="menu-wrap"><button class="btn" id="dl">${icon("download")}Download</button></span><a class="btn btn-primary" href="#/sheets/${sheetId}">${icon("sheet")}Open sheet</a>` : "")
      + (finished && !IS_VIEWER ? `<a class="btn" href="#/new/${isPlaces ? "places" : "web"}">${icon("plus")}New run</a>` : "");
    $("#stop")?.addEventListener("click", async () => {
      $("#stop").disabled = true; $("#activity").textContent = "Stopping… finishing the pages already open.";
      try { await api(`/api/jobs/${id}/stop`, { method: "POST" }); } catch (e) { toast(e.message, "err"); }
    });
    $("#retry")?.addEventListener("click", async () => {
      $("#retry").disabled = true;
      try { await Tracker.retry(id); } catch (e) { toast(e.status === 404 ? "The rows are no longer on the server. Please run it again." : e.message, "err"); $("#retry") && ($("#retry").disabled = false); }
    });
    $("#dl")?.addEventListener("click", (e) => { e.stopPropagation(); openMenu($("#dl"), exportMenu(sheetId)); });
    if (finished) {
      const rows = run.rows;
      $("#result").innerHTML = status === "error" ? `<div class="callout err mt-16">${icon("alert")}<div><b>The run stopped because of an error.</b> ${esc(run.error || "")} ${rows && !run.unsaved ? "The rows collected before the error were saved." : ""}</div></div>`
        : rows ? `<div class="callout ok mt-16">${icon("check")}<div class="grow"><b>${fmtNum(rows)} rows saved on this device.</b> Open the sheet to search, merge or download it.</div></div>`
        : `<div class="callout warn mt-16">${icon("info")}<div><b>No rows this time.</b> Try broader words, add the city or country, or choose “Everything” under Keep only rows that have.</div></div>`;
    } else $("#result").innerHTML = "";
    $("#tab-log").textContent = ((job && job.log) || run.log || []).join("\n") || "No log yet.";
    if (job && !finished) renderPreviewGrid($("#tab-results"), job.columns, job.preview.slice().reverse(), job.count, null, "latest");
    else if (sheetData) renderPreviewGrid($("#tab-results"), sheetData.columns, sheetData.rows.slice(0, 50), run.rows, sheetId, "first");
    else $("#tab-results").innerHTML = `<div class="card-body">${emptyState("sheet", finished ? "No results" : "No rows yet", finished ? "This run didn't save any rows." : "Rows appear here as they are collected.")}</div>`;
  };
  const refresh = async () => {
    const r = await Store.runs.get(id);
    if (stale(tk)) return;
    if (r) run = r;
    if (!RUN_ACTIVE.includes(run.status) && run.sheet_id && !sheetData) {
      try { sheetData = await Store.sheets.data(run.sheet_id); } catch { /* the sheet may have been deleted */ }
      if (stale(tk)) return;
    }
    paint();
  };
  const off = Tracker.on((rid) => { if (rid === id) refresh(); });
  await refresh();
  return off;
}

function renderPreviewGrid(el, columns, rows, total, sheetId, kind) {
  if (!rows.length) {
    el.innerHTML = `<div class="card-body">${emptyState("rows", "No rows yet", "Rows appear here as they are collected.")}</div>`;
    return;
  }
  el.innerHTML = `<div class="grid-scroll" style="max-height:440px"><table class="tbl grid-tbl"><thead><tr><th>#</th>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r, i) => `<tr><td>${i + 1}</td>${columns.map((c) => cellHtml(r[c])).join("")}</tr>`).join("")}</tbody></table></div>
    <div class="pager"><span>Showing ${kind === "first" ? "the first" : "the latest"} ${fmtNum(rows.length)} of ${fmtNum(total)} rows</span>${sheetId ? `<a href="#/sheets/${sheetId}">Open full sheet →</a>` : "<span>The sheet is saved when the run finishes.</span>"}</div>`;
}
function cellHtml(val, q = "") {
  const s = val === null || val === undefined ? "" : String(val);
  if (!s) return '<td class="empty">—</td>';
  let inner = esc(s);
  if (/^https?:\/\/\S+$/.test(s)) inner = `<a href="${esc(s)}" target="_blank" rel="noopener noreferrer">${esc(s.replace(/^https?:\/\/(www\.)?/, ""))}</a>`;
  else if (q) inner = inner.replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`);
  return `<td title="${esc(s)}">${inner}</td>`;
}

/* ================================================================ SHEETS */
let pickImport = () => {};
async function viewSheets() {
  const tk = routeToken();
  setCrumbs("Sheets");
  const v = $("#view");
  const state = { q: "", source: "all", selected: new Set() };
  v.innerHTML = `
    <div class="page-head"><div><h1>Sheets</h1><p>Results from your runs, merges and imports, saved on this device.</p></div>
      ${IS_VIEWER ? "" : `<div class="actions"><button class="btn" id="import">${icon("upload")}Import file</button><a class="btn btn-primary" href="#/new/web">${icon("plus")}New run</a></div>`}</div>
    <input type="file" id="import-file" accept=".xlsx,.csv,.tsv,.json" hidden>
    ${deviceNote()}
    <div class="card">
      <div class="toolbar">
        <div class="search grow" style="max-width:360px">${icon("search")}<input id="q" placeholder="Search sheets" aria-label="Search sheets"></div>
        <div class="seg" id="source">${[["all", "All"], ["run", "Runs"], ["merge", "Merged"], ["import", "Imported"]].map(([k, l], i) => `<button data-s="${k}" class="${i ? "" : "active"}">${l}</button>`).join("")}</div>
        <div class="row-flex" id="bulk" hidden style="margin-left:auto"><span class="muted small" id="bulk-n"></span>
          <button class="btn btn-sm" id="bulk-merge">${icon("merge")}Merge &amp; dedupe</button>
          <button class="btn btn-sm btn-danger" id="bulk-del">${icon("trash")}Delete</button></div>
      </div>
      <div class="table-wrap" id="list">${skeletonRows(6, 6)}</div>
    </div>`;
  let sheets = [];
  const load = async () => { const r = await Store.sheets.list(); if (stale(tk)) return; sheets = r; state.selected = new Set([...state.selected].filter((id) => sheets.some((s) => s.id === id))); render(); };
  const render = () => {
    const list = sheets.filter((s) => (state.source === "all" || s.source === state.source) && (!state.q || s.name.toLowerCase().includes(state.q)));
    const allSel = list.length && list.every((s) => state.selected.has(s.id));
    $("#list").innerHTML = !sheets.length ? emptyState("sheet", "No sheets yet", "Finished runs save a sheet here automatically. You can also import Excel, CSV or JSON files.",
        IS_VIEWER ? "" : `<div class="row-flex" style="justify-content:center"><button class="btn" data-import>${icon("upload")}Import file</button><a class="btn btn-primary" href="#/new/web">${icon("plus")}New run</a></div>`)
      : !list.length ? emptyState("search", "No matching sheets", "Try a different search or filter.")
      : `<table class="tbl"><thead><tr>${IS_VIEWER ? "" : `<th style="width:36px"><input type="checkbox" id="sel-all" ${allSel ? "checked" : ""} aria-label="Select all"></th>`}
          <th>Name</th><th>Source</th><th class="num">Rows</th><th class="num">Columns</th><th class="num">Size</th><th>Created</th><th></th></tr></thead><tbody>
        ${list.map((s) => `<tr class="clickable" data-id="${s.id}">${IS_VIEWER ? "" : `<td data-stop><input type="checkbox" data-sel="${s.id}" ${state.selected.has(s.id) ? "checked" : ""} aria-label="Select ${esc(s.name)}"></td>`}
          <td><div class="name-cell"><div class="file-ico">${icon("sheet")}</div><div><b>${esc(s.name)}</b><small>${s.columns.slice(0, 4).map(esc).join(" · ")}${s.columns.length > 4 ? " …" : ""}</small></div></div></td>
          <td><span class="badge plain ${s.source === "merge" ? "accent" : ""}">${SOURCE_LABEL[s.source] || "Run"}</span></td>
          <td class="num">${fmtNum(s.rows)}</td><td class="num">${s.columns.length || "—"}</td><td class="num">${fmtSize(s.size || 0)}</td>
          <td class="nowrap" title="${esc(fmtDate(s.created))}">${timeAgo(s.created)}</td>
          <td class="actions" data-stop><span class="menu-wrap"><button class="btn btn-sm" data-dl="${s.id}">${icon("download")}Download</button>
            ${IS_VIEWER ? "" : `<button class="btn btn-ghost btn-sm btn-icon" data-menu="${s.id}" aria-label="More actions">${icon("dots")}</button>`}</span></td></tr>`).join("")}
      </tbody></table>`;
    $("#bulk").hidden = !state.selected.size;
    $("#bulk-n").textContent = `${state.selected.size} selected`;
  };
  pickImport = () => $("#import-file")?.click();
  $("#import")?.addEventListener("click", pickImport);
  $("#import-file").onchange = async (e) => {
    const f = e.target.files[0]; e.target.value = "";
    if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    toast(`Reading ${f.name}…`);
    try {
      const r = await api("/api/parse", { method: "POST", body: fd });
      await Store.sheets.put({ id: newId(), name: r.name, source: "import", created: nowIso() }, { columns: r.columns, rows: r.rows });
      Store.persist();
      toast(`Imported ${fmtNum(r.rows.length)} rows`, "ok"); await load();
    } catch (err) { toast(navigator.onLine ? err.message : "Importing needs a connection to read the file.", "err"); }
  };
  $("#q").oninput = (e) => { state.q = e.target.value.trim().toLowerCase(); render(); };
  $$("#source button").forEach((b) => b.onclick = () => { $$("#source button").forEach((x) => x.classList.remove("active")); b.classList.add("active"); state.source = b.dataset.s; render(); });
  $("#bulk-merge").onclick = () => { location.hash = `#/merge?ids=${[...state.selected].join(",")}`; };
  $("#bulk-del").onclick = async () => {
    const n = state.selected.size;
    if (!await confirmDialog(`Delete ${n} sheet${n > 1 ? "s" : ""}?`, "This can't be undone. Download anything you want to keep first.")) return;
    for (const id of state.selected) { try { await Store.sheets.remove(id); } catch (err) { toast(err.message, "err"); } }
    state.selected.clear(); toast(`Deleted ${n} sheet${n > 1 ? "s" : ""}`, "ok"); load();
  };
  $("#list").addEventListener("click", async (e) => {
    if (e.target.closest("[data-import]")) return pickImport();
    if (e.target.id === "sel-all") {
      const list = sheets.filter((s) => (state.source === "all" || s.source === state.source) && (!state.q || s.name.toLowerCase().includes(state.q)));
      list.forEach((s) => e.target.checked ? state.selected.add(s.id) : state.selected.delete(s.id));
      return render();
    }
    const sel = e.target.closest("[data-sel]");
    if (sel) { sel.checked ? state.selected.add(sel.dataset.sel) : state.selected.delete(sel.dataset.sel); return render(); }
    const dl = e.target.closest("[data-dl]");
    if (dl) { e.stopPropagation(); return openMenu(dl, exportMenu(dl.dataset.dl)); }
    const m = e.target.closest("[data-menu]");
    if (m) {
      e.stopPropagation();
      const s = sheets.find((x) => x.id === m.dataset.menu);
      return openMenu(m, [
        { label: "Open", icon: "eye", run: () => location.hash = `#/sheets/${s.id}` },
        { label: "Rename", icon: "edit", run: () => renameSheet(s, load) },
        { label: "Share…", icon: "share", run: () => shareDialog(s) },
        { label: "Merge & dedupe", icon: "merge", run: () => location.hash = `#/merge?ids=${s.id}` },
        "-",
        { label: "Delete", icon: "trash", danger: true, run: async () => {
          if (!await confirmDialog("Delete this sheet?", `“${s.name}” will be removed. This can't be undone.`)) return;
          try { await Store.sheets.remove(s.id); toast("Sheet deleted", "ok"); load(); } catch (err) { toast(err.message, "err"); }
        } },
      ]);
    }
    if (e.target.closest("[data-stop]")) return;
    const tr = e.target.closest("tr[data-id]");
    if (tr) location.hash = `#/sheets/${tr.dataset.id}`;
  });
  await load();
}
function renameSheet(s, after) {
  return modal({
    title: "Rename sheet", confirm: "Save",
    body: `<label class="label" for="rn">Name</label><input class="input" id="rn" maxlength="120" value="${esc(s.name)}">`,
    onConfirm: async (ov) => {
      const name = $("#rn", ov).value.trim();
      if (!name) throw new Error("Enter a name.");
      await Store.sheets.rename(s.id, name); toast("Sheet renamed", "ok"); after?.();
    },
  });
}
// A reminder that sheets are kept in this browser. Dismissible, remembered on this device.
function deviceNote() {
  try { if (localStorage.getItem("deviceNoteDismissed")) return ""; } catch {}
  return `<div class="callout info" id="device-note" style="margin-bottom:16px">${icon("info")}<div class="grow"><b>Your sheets live on this device.</b> They aren't uploaded to the server, so they won't show up on other computers or if you clear your browser data. <a href="#/settings">Back them up in Settings</a>.</div>
    <button class="btn btn-ghost btn-sm" onclick="try{localStorage.setItem('deviceNoteDismissed','1')}catch(e){};this.closest('#device-note').remove()">Got it</button></div>`;
}

/* ================================================================ SHEET VIEW (data grid) */
const GRID_CARD = `
    <div class="card">
      <div class="toolbar"><div class="search grow" style="max-width:420px">${icon("search")}<input id="q" placeholder="Search all columns" aria-label="Search all columns"></div>
        <span class="muted small" id="count"></span></div>
      <div class="grid-scroll" id="grid"></div>
      <div class="pager"><span id="range"></span><div class="row-flex"><button class="btn btn-sm" id="prev">Previous</button><button class="btn btn-sm" id="next">Next</button></div></div>
    </div>`;

// A searchable, paged, read-only grid for any sheet's data (my own or a shared one). Returns a cleanup function.
function mountGrid(data) {
  const PAGE = 100;
  const state = { q: "", offset: 0, rows: data.rows };
  const draw = () => {
    const slice = state.rows.slice(state.offset, state.offset + PAGE);
    $("#grid").innerHTML = !slice.length ? emptyState("search", state.q ? "No rows match" : "This sheet is empty", state.q ? "Try a different search." : "")
      : `<table class="tbl grid-tbl"><thead><tr><th>#</th>${data.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>
        ${slice.map((r, i) => `<tr class="clickable" data-i="${i}"><td>${state.offset + i + 1}</td>${data.columns.map((c) => cellHtml(r[c], state.q)).join("")}</tr>`).join("")}</tbody></table>`;
    $("#count").textContent = state.q ? `${fmtNum(state.rows.length)} matching rows` : "";
    $("#range").textContent = state.rows.length ? `Rows ${fmtNum(state.offset + 1)}–${fmtNum(Math.min(state.offset + PAGE, state.rows.length))} of ${fmtNum(state.rows.length)}` : "";
    $("#prev").disabled = state.offset === 0;
    $("#next").disabled = state.offset + PAGE >= state.rows.length;
  };
  let deb;
  $("#q").oninput = (e) => {
    clearTimeout(deb);
    deb = setTimeout(() => {
      state.q = e.target.value.trim();
      const q = state.q.toLowerCase();
      state.rows = q ? data.rows.filter((r) => data.columns.some((c) => String(r[c] ?? "").toLowerCase().includes(q))) : data.rows;
      state.offset = 0; draw();
    }, 200);
  };
  $("#prev").onclick = () => { state.offset = Math.max(0, state.offset - PAGE); draw(); $("#grid").scrollTop = 0; };
  $("#next").onclick = () => { state.offset += PAGE; draw(); $("#grid").scrollTop = 0; };
  $("#grid").addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    const tr = e.target.closest("tr[data-i]");
    if (tr) rowDrawer(data.columns, state.rows[state.offset + +tr.dataset.i], state.offset + +tr.dataset.i + 1);
  });
  draw();
  return () => clearTimeout(deb);
}

// Whether "Fill missing details" makes sense for this sheet: it needs a Name to look up by, an Email/Phone-ish
// column to know what's missing, and at least one row that's actually missing one.
function sheetNeedsFilling(meta, data) {
  if (!meta.columns.includes("Name")) return false;
  const emailCol = ["Emails", "Email"].find((c) => meta.columns.includes(c));
  const phoneCol = ["Phone Numbers", "Phone"].find((c) => meta.columns.includes(c));
  if (!emailCol && !phoneCol) return false;
  return data.rows.some((r) => r.Name && !(emailCol && r[emailCol]) && !(phoneCol && r[phoneCol]));
}

async function viewSheet(id) {
  const tk = routeToken();
  setCrumbs({ label: "Sheets", href: "#/sheets" }, "Sheet");
  const v = $("#view");
  const meta = await Store.sheets.get(id);
  if (stale(tk)) return;
  if (!meta) return viewNotFound();
  setCrumbs({ label: "Sheets", href: "#/sheets" }, meta.name);
  const data = await Store.sheets.data(id);
  if (stale(tk)) return;
  if (!data) return viewNotFound();
  const canFill = !IS_VIEWER && sheetNeedsFilling(meta, data);
  v.innerHTML = `
    <div class="page-head"><div><h1>${esc(meta.name)}</h1><p>${fmtNum(meta.rows)} rows · ${meta.columns.length} columns · ${SOURCE_LABEL[meta.source] || "Run"} · ${esc(fmtDate(meta.created))} · saved on this device</p></div>
      <div class="actions">${IS_VIEWER ? "" : `<button class="btn" id="rename">${icon("edit")}Rename</button><a class="btn" href="#/merge?ids=${id}">${icon("merge")}Dedupe / merge</a><button class="btn" id="share">${icon("share")}Share</button>`}
        ${canFill ? `<button class="btn" id="fill-missing">${icon("search")}Fill missing details</button>` : ""}
        <span class="menu-wrap"><button class="btn btn-primary" id="export">${icon("download")}Export</button></span></div></div>
    ${GRID_CARD}`;
  $("#export").onclick = (e) => { e.stopPropagation(); openMenu($("#export"), exportMenu(id)); };
  $("#rename")?.addEventListener("click", () => renameSheet(meta, () => route()));
  $("#share")?.addEventListener("click", () => shareDialog(meta));
  $("#fill-missing")?.addEventListener("click", async () => {
    const btn = $("#fill-missing");
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Starting…';
    try {
      const r = await api("/api/jobs", { method: "POST", body: { mode: "enrich", columns: data.columns, rows: data.rows, custom_fields: "" } });
      await Store.runs.put({ id: r.id, name: meta.name, mode: "enrich", spec: { mode: "enrich", columns: data.columns, source_sheet: meta.name },
        status: "running", started: nowIso(), rows: 0, with_email: 0, with_phone: 0, duration: 0, error: "", sheet_id: null });
      Tracker.watch(r.id);
      location.hash = `#/runs/${r.id}`;
    } catch (e) {
      toast(e.message, "err");
      btn.disabled = false; btn.innerHTML = `${icon("search")}Fill missing details`;
    }
  });
  return mountGrid(data);
}

/* ================================================================ SHARING (an explicit copy on the server that expires) */
const expiresIn = (iso) => {
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "expired";
  const h = ms / 36e5;
  return h < 1 ? "in under an hour" : h < 24 ? `in ${Math.round(h)} hour${Math.round(h) === 1 ? "" : "s"}` : `in ${Math.round(h / 24)} day${Math.round(h / 24) === 1 ? "" : "s"}`;
};
const shareUrl = (id) => `${location.origin}/#/shared/${id}`;
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch {
    const t = document.createElement("textarea");
    t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
    document.body.appendChild(t); t.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch {}
    t.remove(); return ok;
  }
}

// What goes out when a link is sent by mail or chat: the sheet's name, its size and the link. Never the data.
// A customer message is written for someone outside the company; a colleague message assumes they have an account.
const customerUrl = (id) => `${location.origin}/s/${id}`;
const linkFor = (r) => (r.kind === "external" ? customerUrl(r.id) : shareUrl(r.id));
function shareMessage(r) {
  const url = linkFor(r), until = fmtDate(r.expires, false), company = CFG.company;
  if (r.kind === "external") {
    const intro = `Hello, here is the spreadsheet you asked for from ${company}: “${r.name}” (${rowsText(r.rows)}).`;
    const note = `${r.has_passcode ? "It is protected, and I will send you the passcode separately. " : ""}The link works until ${until}, and you can download the file as Excel or CSV.`;
    return { subject: `Your data from ${company}: ${r.name}`, intro, url, note,
      full: `Hello,\n\nHere is the spreadsheet you asked for from ${company}: “${r.name}” (${rowsText(r.rows)}).\n\nOpen it here: ${url}\n\n${note}\n\nRegards,\n${company}` };
  }
  const intro = `Hi, I've shared the sheet “${r.name}” (${rowsText(r.rows)}) with you on ${company} Data Collector.`;
  const note = `You'll need to sign in to open it. The link stops working on ${until}.`;
  return { subject: `Shared sheet: ${r.name}`, intro, url, note, full: `${intro}\n\nOpen it here: ${url}\n\n${note}` };
}
// The ways to send a link. Gmail, WhatsApp and Telegram open their own pages with the message filled in;
// "More apps" opens the device's own share menu (phones, tablets and the installed app) when it exists.
function shareChannels(r, opts = {}) {
  const m = shareMessage(r), enc = encodeURIComponent;
  const to = /^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$/.test(opts.to || "") ? opts.to : "";
  const openPage = (url) => window.open(url, "_blank", "noopener");
  const items = [
    { id: "gmail", label: "Gmail", icon: "mail", run: () => openPage(`https://mail.google.com/mail/?view=cm&fs=1${to ? `&to=${enc(to)}` : ""}&su=${enc(m.subject)}&body=${enc(m.full)}`) },
    { id: "mail", label: "Email app", icon: "mail", run: () => { const a = document.createElement("a"); a.href = `mailto:${to}?subject=${enc(m.subject)}&body=${enc(m.full)}`; document.body.appendChild(a); a.click(); a.remove(); } },
    { id: "whatsapp", label: "WhatsApp", icon: "chat", run: () => openPage(`https://wa.me/?text=${enc(m.full)}`) },
    { id: "telegram", label: "Telegram", icon: "send", run: () => openPage(`https://t.me/share/url?url=${enc(m.url)}&text=${enc(m.intro + " " + m.note)}`) },
  ];
  if (navigator.share) {
    items.push({ id: "more", label: "More apps…", icon: "share", run: async () => {
      try { await navigator.share({ title: m.subject, text: `${m.intro} ${m.note}`, url: m.url }); }
      catch (e) { if (e.name !== "AbortError") toast("Your device couldn't open its share menu.", "err"); }
    } });
  }
  return items;
}
function renderSendRow(el, r, opts) {
  const channels = shareChannels(r, opts);
  el.innerHTML = channels.map((c, i) => `<button type="button" class="btn send-${c.id}" data-i="${i}">${icon(c.icon)}${esc(c.label)}</button>`).join("");
  el.onclick = (e) => { const b = e.target.closest("[data-i]"); if (b) channels[+b.dataset.i].run(); };
}

const PASSCODE_CHARS = "abcdefghjkmnpqrstuvwxyz23456789";  // no look-alikes (0/o, 1/l)
function genPasscode(n = 7) {
  const a = new Uint32Array(n); crypto.getRandomValues(a);
  return [...a].map((x) => PASSCODE_CHARS[x % PASSCODE_CHARS.length]).join("");
}
const XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
async function buildFile(name, columns, rows) {
  // Prefer the real Excel file; if the server can't be reached, fall back to CSV.
  const base = fileSafe(name);
  try {
    const r = await fetch("/api/export/xlsx", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, columns, rows }) });
    if (r.ok) return new File([await r.blob()], base + ".xlsx", { type: XLSX_MIME });
  } catch { /* offline */ }
  return new File([SheetOps.toCSV(columns, rows)], base + ".csv", { type: "text/csv" });
}

async function shareDialog(meta) {
  let data, people;
  try { [data, people] = await Promise.all([Store.sheets.data(meta.id), api("/api/users/directory")]); }
  catch (e) { return toast(navigator.onLine ? e.message : "Sharing needs a connection.", "err"); }
  if (!data) return toast("This sheet could not be found on this device.", "err");
  const picked = new Set();
  let mode = "customer";
  try { mode = localStorage.getItem("shareMode") === "colleagues" ? "colleagues" : "customer"; } catch {}
  const canFiles = !!(navigator.canShare && navigator.canShare({ files: [new File(["x"], "x.csv", { type: "text/csv" })] }));
  let created = null, createdOpts = {};
  const body = `
    <div class="seg" id="sh-mode" style="margin-bottom:18px">
      <button type="button" data-m="customer">Customer outside the company</button>
      <button type="button" data-m="colleagues">Colleagues</button>
    </div>

    <div id="sh-customer">
      <div class="field"><label class="label" for="sh-cname">Customer or company name</label>
        <input class="input" id="sh-cname" maxlength="160" placeholder="e.g. Global Education Services" autocomplete="off"></div>
      <div class="field"><label class="label" for="sh-cmail">Customer's email <span class="opt">(optional)</span></label>
        <input class="input" id="sh-cmail" type="email" placeholder="name@company.com" autocomplete="off">
        <div class="hint">Only used to fill in the "To" box when you send the email. It isn't saved.</div></div>
      <div class="field"><label class="label" for="sh-cmsg">Message for the customer <span class="opt">(optional)</span></label>
        <textarea class="input" id="sh-cmsg" maxlength="1000" rows="2" placeholder="Shown at the top of their page, e.g. “Student leads for the September intake.”"></textarea></div>
      <div class="field">
        <label class="switch"><input type="checkbox" id="sh-pass-on" checked><span class="sw"></span><span><b>Protect with a passcode</b><small>Recommended. Send the passcode in a separate message, for example on WhatsApp or by phone.</small></span></label>
        <div class="input-row mt-8" id="sh-pass-row"><input class="input mono" id="sh-pass" value="${genPasscode()}" autocomplete="off" spellcheck="false" aria-label="Passcode"><button class="btn" type="button" id="sh-pass-new">New</button></div>
      </div>
      <div class="field"><label class="label" for="sh-cdays">Link works for</label>
        <select class="input" id="sh-cdays"><option value="1">1 day</option><option value="7" selected>7 days</option><option value="30">30 days</option></select></div>
      <label class="switch"><input type="checkbox" id="sh-ack"><span class="sw"></span><span><b>I confirm we are allowed to share this data with this customer</b><small>This sheet may contain personal details. Check your agreement and the privacy rules that apply (including for people abroad) before sending.</small></span></label>
    </div>

    <div id="sh-colleagues" hidden>
      <div class="field">
        <label class="label">Share with</label>
        ${people.length ? `<div class="search" style="margin-bottom:8px">${icon("search")}<input id="sh-find" placeholder="Find a person" aria-label="Find a person"></div>
          <div class="people" id="sh-people">${people.map((p) => `<label class="person" data-name="${esc(p.username.toLowerCase())}"><input type="checkbox" value="${p.id}"><span class="avatar" style="width:26px;height:26px;font-size:11px">${esc(p.username.slice(0, 2))}</span><span>${esc(p.username)}</span></label>`).join("")}</div>`
          : `<div class="callout info">${icon("info")}<div>No one else has an account yet. Ask an admin to add people, or share with the link below.</div></div>`}
        <label class="switch mt-8"><input type="checkbox" id="sh-link"><span class="sw"></span><span><b>Anyone in the company with the link</b><small>They still have to sign in first.</small></span></label>
      </div>
      <div class="grid grid-2">
        <div class="field"><label class="label" for="sh-days">Stops working</label>
          <select class="input" id="sh-days"><option value="1">After 1 day</option><option value="7" selected>After 7 days</option><option value="30">After 30 days</option></select></div>
        <div class="field"><span class="label">Downloads</span>
          <label class="switch"><input type="checkbox" id="sh-export" checked><span class="sw"></span><span><b>Allow downloads</b><small>Hides the download buttons. It can't stop someone copying what they see.</small></span></label></div>
      </div>
    </div>

    <div class="callout info mt-16">${icon("info")}<div>This shares a <b>copy as it is now</b> (${rowsText(data.rows.length)}). Later changes to your sheet aren't included. The copy is kept on the server until it expires or you stop sharing, then it is deleted.</div></div>
    ${canFiles ? `<div class="mt-16"><button class="btn" id="sh-file" type="button">${icon("share")}Send the file itself instead…</button></div>` : ""}`;

  return modal({
    title: `Share “${meta.name}”`, confirm: "Create link", wide: true, body,
    onOpen: (ov) => {
      const setMode = (m) => {
        mode = m;
        try { localStorage.setItem("shareMode", m); } catch {}
        $$("#sh-mode button", ov).forEach((b) => b.classList.toggle("active", b.dataset.m === m));
        $("#sh-customer", ov).hidden = m !== "customer";
        $("#sh-colleagues", ov).hidden = m !== "colleagues";
        $("[data-ok]", ov).textContent = m === "customer" ? "Create customer link" : "Share with colleagues";
      };
      $$("#sh-mode button", ov).forEach((b) => b.onclick = () => setMode(b.dataset.m));
      setMode(mode);
      $("#sh-pass-new", ov).onclick = () => { $("#sh-pass", ov).value = genPasscode(); };
      $("#sh-pass-on", ov).onchange = (e) => { $("#sh-pass-row", ov).hidden = !e.target.checked; };
      $$("#sh-people input", ov).forEach((c) => c.onchange = () => c.checked ? picked.add(+c.value) : picked.delete(+c.value));
      $("#sh-find", ov)?.addEventListener("input", (e) => {
        const q = e.target.value.trim().toLowerCase();
        $$(".person", ov).forEach((p) => p.hidden = !!q && !p.dataset.name.includes(q));
      });
      $("#sh-file", ov)?.addEventListener("click", async () => {
        try {
          const file = await buildFile(meta.name, data.columns, data.rows);
          await navigator.share({ files: [file], title: meta.name });
        } catch (e) { if (e.name !== "AbortError") toast("Your device couldn't share the file.", "err"); }
      });
      setTimeout(() => (mode === "customer" ? $("#sh-cname", ov) : $("#sh-find", ov))?.focus(), 50);
    },
    onConfirm: async (ov) => {
      if (created) return true;  // after creating, the button reads "Done"
      let payload;
      if (mode === "customer") {
        const customer = $("#sh-cname", ov).value.trim();
        const passOn = $("#sh-pass-on", ov).checked, pass = $("#sh-pass", ov).value.trim();
        if (!customer) { toast("Enter the customer's name.", "err"); $("#sh-cname", ov).focus(); return false; }
        if (passOn && (pass.length < 4 || pass.length > 40)) { toast("A passcode needs 4 to 40 characters.", "err"); $("#sh-pass", ov).focus(); return false; }
        if (!$("#sh-ack", ov).checked) { toast("Please confirm that you're allowed to share this data with the customer.", "err"); return false; }
        payload = { kind: "external", customer, message: $("#sh-cmsg", ov).value, passcode: passOn ? pass : "", acknowledged: true, expires_days: +$("#sh-cdays", ov).value };
        createdOpts = { to: $("#sh-cmail", ov).value.trim(), passcode: passOn ? pass : "" };
      } else {
        const link = $("#sh-link", ov).checked;
        if (!picked.size && !link) { toast("Pick at least one person, or allow anyone with the link.", "err"); return false; }
        payload = { kind: "internal", recipients: [...picked], link_access: link, expires_days: +$("#sh-days", ov).value, allow_export: $("#sh-export", ov).checked };
        createdOpts = {};
      }
      const r = await api("/api/shares", { method: "POST", body: { name: meta.name, columns: data.columns, rows: data.rows, ...payload } });
      created = r;
      const url = linkFor(r);
      const who = r.kind === "external" ? esc(r.customer)
        : esc([...(r.recipients.length ? [r.recipients.join(", ")] : []), ...(r.link_access ? ["anyone signed in with the link"] : [])].join(" and "));
      $(".modal-body", ov).innerHTML = `
        <div class="callout ok">${icon("check")}<div><b>${r.kind === "external" ? "Ready to send to" : "Shared with"} ${who}.</b> The link stops working ${esc(expiresIn(r.expires))}.</div></div>
        <div class="field mt-16"><label class="label" for="sh-url">Link</label>
          <div class="input-row"><input class="input mono" id="sh-url" readonly value="${esc(url)}"><button class="btn btn-primary" type="button" id="sh-copy">Copy link</button></div></div>
        ${r.kind === "external" && createdOpts.passcode ? `<div class="field"><label class="label" for="sh-code">Passcode</label>
          <div class="input-row"><input class="input mono" id="sh-code" readonly value="${esc(createdOpts.passcode)}"><button class="btn" type="button" id="sh-copy-code">Copy passcode</button></div>
          <div class="hint">Send this in a <b>separate message</b>, for example on WhatsApp or by phone. You can see it again any time under <b>Shared → Shared by me → Passcode</b>.</div></div>` : ""}
        <div class="field"><span class="label">Send the link</span><div class="send-row" id="sh-send"></div>
          <div class="hint">Only the sheet's name, size and link go in the message${r.kind === "external" ? ". The passcode is not included" : ""}.</div></div>
        ${r.kind === "external" ? `<div class="field"><span class="label">Or send the file</span>
          <div class="send-row"><button class="btn" type="button" id="sh-dl">${icon("download")}Download Excel to attach yourself</button>${canFiles ? `<button class="btn" type="button" id="sh-sendfile">${icon("share")}Send the file itself…</button>` : ""}</div></div>
          <div class="callout info">${icon("info")}<div>You'll see when the customer opens or downloads it under <b>Shared → Shared by me</b>.</div></div>` : `<div class="hint">People you chose will also find it under <b>Shared</b>. You can stop sharing any time from there.</div>`}`;
      renderSendRow($("#sh-send", ov), r, createdOpts);
      $("#sh-url", ov).onfocus = (e) => e.target.select();
      $("#sh-copy", ov).onclick = async () => toast((await copyText(url)) ? "Link copied" : "Copy the link by hand: select it and press Ctrl+C.", "ok");
      $("#sh-copy-code", ov)?.addEventListener("click", async () => toast((await copyText(createdOpts.passcode)) ? "Passcode copied" : "Couldn't copy the passcode.", "ok"));
      $("#sh-dl", ov)?.addEventListener("click", () => exportData(meta.name, data.columns, data.rows, "xlsx"));
      $("#sh-sendfile", ov)?.addEventListener("click", async () => {
        try { await navigator.share({ files: [await buildFile(meta.name, data.columns, data.rows)], title: meta.name }); }
        catch (e) { if (e.name !== "AbortError") toast("Your device couldn't share the file.", "err"); }
      });
      $("[data-ok]", ov).textContent = "Done";
      $("[data-x]", ov)?.remove();
      return false;
    },
  });
}

// Shows the passcode of a customer link to the person who created it (the server only gives it to them).
async function showPasscode(share) {
  let r;
  try { r = await api(`/api/shares/${share.id}/passcode`); } catch (e) { return toast(e.message, "err"); }
  if (!r.passcode) {
    return modal({ title: "Passcode", confirm: "OK", cancel: "",
      body: `<div class="callout warn">${icon("alert")}<div>This link has a passcode, but it was created before passcodes could be viewed, so it can't be shown. Stop sharing and create a new link to set one you can see.</div></div>` });
  }
  return modal({
    title: `Passcode for ${share.customer}`, confirm: "Done", cancel: "",
    body: `<div class="input-row"><input class="input mono" id="pc-val" readonly value="${esc(r.passcode)}" aria-label="Passcode"><button class="btn btn-primary" type="button" id="pc-copy">Copy passcode</button></div>
      <div class="hint">Send it to the customer in a <b>separate message</b>, not together with the link. Only you can see this. Not even admins can.</div>`,
    onOpen: (ov) => {
      $("#pc-val", ov).onfocus = (e) => e.target.select();
      $("#pc-copy", ov).onclick = async () => toast((await copyText(r.passcode)) ? "Passcode copied" : "Couldn't copy the passcode.", "ok");
    },
  });
}

async function viewShared() {
  const tk = routeToken();
  setCrumbs("Shared");
  const v = $("#view");
  const state = { box: "with-me" };
  v.innerHTML = `
    <div class="page-head"><div><h1>Shared</h1><p>Sheets you've sent to customers or colleagues, and ones colleagues shared with you. Each one expires automatically.</p></div></div>
    <div class="card">
      <div class="toolbar"><div class="seg" id="box"><button data-b="with-me" class="active">Shared with me</button><button data-b="by-me">Shared by me</button></div></div>
      <div class="table-wrap" id="list">${skeletonRows(4, 5)}</div>
    </div>`;
  let items = [];
  const activity = (x) => x.kind !== "external" ? '<span class="muted">—</span>'
    : !x.views && !x.downloads ? '<span class="muted">Not opened yet</span>'
    : `<div>${[x.views ? `Opened ${x.views}×` : "", x.downloads ? `downloaded ${x.downloads}×` : ""].filter(Boolean).join(" · ")}</div><div class="muted small">last ${timeAgo(x.last_opened)}</div>`;
  const kindNote = (x) => x.kind === "external" ? `Customer link${x.has_passcode ? " · passcode" : ""}` : x.allow_export ? "Downloads allowed" : "Downloads off";
  const render = () => {
    const mine = state.box === "by-me";
    $("#list").innerHTML = !items.length
      ? emptyState("share", mine ? "You haven't shared anything" : "Nothing has been shared with you", mine ? "Open a sheet and press Share to send it to a customer or a colleague." : "When a colleague shares a sheet with you, it appears here.",
          mine && !IS_VIEWER ? '<a class="btn btn-primary" href="#/sheets">Go to Sheets</a>' : "")
      : `<table class="tbl"><thead><tr><th>Sheet</th><th>${mine ? "Shared with" : "Shared by"}</th>${mine ? "<th>Activity</th>" : ""}<th class="num">Rows</th><th>Shared</th><th>Stops working</th><th></th></tr></thead><tbody>
        ${items.map((s) => `<tr class="clickable" data-id="${s.id}"><td><div class="name-cell"><div class="file-ico">${icon("sheet")}</div><div><b>${esc(s.name)}</b><small>${esc(kindNote(s))}</small></div></div></td>
          <td>${mine ? (s.kind === "external" ? `<b>${esc(s.customer)}</b>` : esc([...s.recipients, ...(s.link_access ? ["anyone with the link"] : [])].join(", ") || "—")) : esc(s.owner)}</td>
          ${mine ? `<td>${activity(s)}</td>` : ""}
          <td class="num">${fmtNum(s.rows)}</td><td class="nowrap">${timeAgo(s.created)}</td><td class="nowrap" title="${esc(fmtDate(s.expires))}">${esc(expiresIn(s.expires))}</td>
          <td class="actions" data-stop><span class="menu-wrap">${mine ? `<button class="btn btn-sm" data-copy="${s.id}">${icon("link")}Copy link</button>${s.kind === "external" && s.has_passcode ? `<button class="btn btn-sm" data-pass="${s.id}">${icon("key")}Passcode</button>` : ""}<button class="btn btn-sm" data-send="${s.id}">${icon("send")}Send…</button><button class="btn btn-sm btn-danger" data-stopshare="${s.id}">Stop sharing</button>`
            : `<a class="btn btn-sm" href="#/shared/${s.id}">Open</a>`}</span></td></tr>`).join("")}</tbody></table>`;
  };
  const load = async () => {
    const r = await api(`/api/shares?box=${state.box}`);
    if (stale(tk)) return;
    items = r; render();
  };
  $$("#box button").forEach((b) => b.onclick = async () => {
    $$("#box button").forEach((x) => x.classList.toggle("active", x === b));
    state.box = b.dataset.b; $("#list").innerHTML = skeletonRows(4, 5);
    try { await load(); } catch (e) { toast(e.message, "err"); }
  });
  $("#list").addEventListener("click", async (e) => {
    const copy = e.target.closest("[data-copy]");
    if (copy) return toast((await copyText(linkFor(items.find((x) => x.id === copy.dataset.copy)))) ? "Link copied" : "Couldn't copy the link.", "ok");
    const pass = e.target.closest("[data-pass]");
    if (pass) { e.stopPropagation(); return showPasscode(items.find((x) => x.id === pass.dataset.pass)); }
    const send = e.target.closest("[data-send]");
    if (send) { e.stopPropagation(); return openMenu(send, shareChannels(items.find((x) => x.id === send.dataset.send))); }
    const stop = e.target.closest("[data-stopshare]");
    if (stop) {
      const s = items.find((x) => x.id === stop.dataset.stopshare);
      if (!await confirmDialog("Stop sharing?", `“${s.name}” will be deleted from the server and nobody will be able to open it any more.`, "Stop sharing")) return;
      try { await api(`/api/shares/${s.id}`, { method: "DELETE" }); toast("Sharing stopped", "ok"); load(); } catch (err) { toast(err.message, "err"); }
      return;
    }
    if (e.target.closest("[data-stop]")) return;
    const tr = e.target.closest("tr[data-id]");
    if (tr) location.hash = `#/shared/${tr.dataset.id}`;
  });
  await load();
}

async function viewSharedSheet(id) {
  const tk = routeToken();
  setCrumbs({ label: "Shared", href: "#/shared" }, "Shared sheet");
  const v = $("#view");
  let s;
  try { s = await api(`/api/shares/${id}`); }
  catch (e) {
    if (stale(tk)) return;
    if (e.status !== 404) throw e;
    v.innerHTML = `<div class="card">${emptyState("share", "This shared sheet isn't available", "It may have expired, been stopped by its owner, or not been shared with you.", '<a class="btn btn-primary" href="#/shared">See what is shared with you</a>')}</div>`;
    return;
  }
  if (stale(tk)) return;
  setCrumbs({ label: "Shared", href: "#/shared" }, s.name);
  const data = { columns: s.columns, rows: s.rows_data };
  v.innerHTML = `
    <div class="page-head"><div><div class="row-flex"><h1>${esc(s.name)}</h1><span class="badge accent plain">${s.kind === "external" ? "Customer link" : "Shared copy"}</span></div>
      <p>${rowsText(s.rows)} · ${s.kind === "external" ? `prepared for ${esc(s.customer)} · ${s.views || s.downloads ? `opened ${s.views}×, downloaded ${s.downloads}×` : "not opened yet"}` : s.mine ? "shared by you" : `shared by ${esc(s.owner)}`} · stops working ${esc(expiresIn(s.expires))}${s.allow_export ? "" : " · downloads are off"}</p></div>
      <div class="actions">
        ${s.allow_export ? `<button class="btn" id="save-copy">${icon("sheet")}Save a copy to my device</button><span class="menu-wrap"><button class="btn btn-primary" id="export">${icon("download")}Export</button></span>` : ""}
        ${s.mine && s.kind === "external" && s.has_passcode ? `<button class="btn" id="pass">${icon("key")}Passcode</button>` : ""}${s.mine && s.kind === "external" ? `<a class="btn" href="${customerUrl(s.id)}" target="_blank" rel="noopener">${icon("eye")}View as customer</a>` : ""}${s.mine ? `<span class="menu-wrap"><button class="btn" id="send">${icon("send")}Send link</button></span><button class="btn btn-danger" id="stop">${icon("trash")}Stop sharing</button>` : ""}</div></div>
    ${GRID_CARD}`;
  $("#export")?.addEventListener("click", (e) => { e.stopPropagation(); openMenu($("#export"), exportDataMenu(s.name, data.columns, data.rows)); });
  $("#save-copy")?.addEventListener("click", async () => {
    try {
      const meta = await Store.sheets.put({ id: newId(), name: s.name, source: "shared", created: nowIso() }, data);
      Store.persist();
      toast("Saved to your sheets", "ok", { label: "Open", run: () => { location.hash = `#/sheets/${meta.id}`; } });
    } catch (e) { toast(e.message, "err"); }
  });
  $("#pass")?.addEventListener("click", () => showPasscode(s));
  $("#send")?.addEventListener("click", (e) => { e.stopPropagation(); openMenu($("#send"), shareChannels(s)); });
  $("#stop")?.addEventListener("click", async () => {
    if (!await confirmDialog("Stop sharing?", "This copy will be deleted from the server and nobody will be able to open it any more.", "Stop sharing")) return;
    try { await api(`/api/shares/${id}`, { method: "DELETE" }); toast("Sharing stopped", "ok"); location.hash = "#/shared"; } catch (e) { toast(e.message, "err"); }
  });
  return mountGrid(data);
}

function rowDrawer(columns, row, n) {
  const ov = document.createElement("div");
  ov.className = "overlay drawer-overlay";
  ov.innerHTML = `<aside class="drawer" role="dialog" aria-modal="true" aria-label="Row ${n}">
    <div class="card-head"><h3>Row ${fmtNum(n)}</h3><button class="btn btn-ghost btn-icon btn-sm" data-x aria-label="Close">${icon("x")}</button></div>
    <dl>${columns.map((c) => {
      const s = row[c] === null || row[c] === undefined ? "" : String(row[c]);
      const val = !s ? '<span class="muted">—</span>' : /^https?:\/\/\S+$/.test(s) ? `<a href="${esc(s)}" target="_blank" rel="noopener noreferrer">${esc(s)}</a>` : esc(s);
      return `<dt>${esc(c)}</dt><dd>${val}</dd>`;
    }).join("")}</dl></aside>`;
  document.body.appendChild(ov);
  const close = () => { ov.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  ov.addEventListener("mousedown", (e) => { if (e.target === ov) close(); });
  $("[data-x]", ov).onclick = close;
  $("[data-x]", ov).focus();
}

/* ================================================================ MERGE & DEDUPE (PRD 5.9) */
async function viewMerge(preselected) {
  setCrumbs({ label: "Tools", href: "#/merge" }, "Merge & Dedupe");
  const v = $("#view");
  if (IS_VIEWER) return viewNotFound();
  const tk = routeToken();
  const sheets = await Store.sheets.list();
  if (stale(tk)) return;
  const st = { step: preselected.length ? 2 : 1, selected: new Set(preselected.filter((id) => sheets.some((s) => s.id === id))),
    keys: [], match: { trim: true, ignore_case: true, smart: true, ignore_punct: false }, keep: "first", fill_empty: true,
    save_removed: false, output_name: "", preview: null };
  if (!st.selected.size) st.step = 1;
  const columnsOf = () => { const cols = []; sheets.filter((s) => st.selected.has(s.id)).forEach((s) => s.columns.forEach((c) => { if (!cols.includes(c)) cols.push(c); })); return cols; };
  const suggestKeys = () => {
    const cols = columnsOf();
    const pick = cols.find((c) => /email/i.test(c)) || cols.find((c) => /phone/i.test(c)) || cols.find((c) => /^(website|source url)$/i.test(c));
    st.keys = pick ? [pick] : [];
  };
  if (st.step === 2) suggestKeys();

  const render = () => {
    const STEPS = ["Choose sheets", "Duplicate rules", "Preview & save"];
    v.innerHTML = `
      <div class="page-head"><div><h1>Merge &amp; Dedupe</h1><p>Combine sheets into one and remove duplicate rows. Your original sheets are never changed.</p></div></div>
      <div class="card">
        <div class="card-body" style="padding-bottom:0"><div class="steps">${STEPS.map((l, i) => `<div class="s ${i + 1 < st.step ? "did" : i + 1 === st.step ? "doing" : ""}"><span class="d">${i + 1 < st.step ? "✓" : i + 1}</span><span class="l">${l}</span></div>`).join("")}</div></div>
        <div id="step"></div>
      </div>`;
    const el = $("#step");
    if (st.step === 1) {
      el.innerHTML = !sheets.length ? `<div class="card-body">${emptyState("sheet", "No sheets to merge", "Run a collection or import a file first.", `<a class="btn btn-primary" href="#/sheets">Go to Sheets</a>`)}</div>` : `
        <div class="toolbar"><div class="search grow" style="max-width:340px">${icon("search")}<input id="mq" placeholder="Search sheets" aria-label="Search sheets"></div><span class="muted small" id="seln"></span></div>
        <div class="table-wrap" style="max-height:440px"><table class="tbl"><thead><tr><th style="width:36px"></th><th>Sheet</th><th class="num">Rows</th><th>Created</th></tr></thead><tbody>
          ${sheets.map((s) => `<tr class="clickable" data-id="${s.id}" data-name="${esc(s.name.toLowerCase())}"><td><input type="checkbox" ${st.selected.has(s.id) ? "checked" : ""} aria-label="Select ${esc(s.name)}"></td>
            <td><div class="name-cell"><div class="file-ico">${icon("sheet")}</div><div><b>${esc(s.name)}</b><small>${s.columns.length} columns</small></div></div></td>
            <td class="num">${fmtNum(s.rows)}</td><td>${timeAgo(s.created)}</td></tr>`).join("")}</tbody></table></div>
        <div class="pager"><span>Pick one sheet to remove its duplicates, or several to combine them. When rows match, the sheet you selected first wins.</span><button class="btn btn-primary" id="next1">Next ${icon("arrow")}</button></div>`;
      const sync = () => {
        const rows = sheets.filter((s) => st.selected.has(s.id)).reduce((a, s) => a + s.rows, 0);
        $("#seln") && ($("#seln").textContent = st.selected.size ? `${st.selected.size} selected · ${fmtNum(rows)} rows` : "Nothing selected");
        $("#next1") && ($("#next1").disabled = !st.selected.size);
      };
      el.addEventListener("click", (e) => {
        const tr = e.target.closest("tr[data-id]");
        if (!tr) return;
        const cb = $("input", tr);
        if (e.target !== cb) cb.checked = !cb.checked;
        cb.checked ? st.selected.add(tr.dataset.id) : st.selected.delete(tr.dataset.id);
        sync();
      });
      $("#mq")?.addEventListener("input", (e) => { const q = e.target.value.toLowerCase(); $$("tr[data-name]", el).forEach((tr) => tr.hidden = !tr.dataset.name.includes(q)); });
      $("#next1")?.addEventListener("click", () => { suggestKeys(); st.step = 2; render(); });
      sync();
    } else if (st.step === 2) {
      const cols = columnsOf();
      el.innerHTML = `
        <div class="form-section">
          <div class="section-title"><div class="step-num">A</div><div><h3>Which columns identify a duplicate?</h3><p>Rows with the same value in all chosen columns count as duplicates. Choose none to just combine the sheets.</p></div></div>
          <div class="section-body"><div class="cats" id="keys">${cols.map((c) => `<button type="button" class="cat ${st.keys.includes(c) ? "active" : ""}" data-k="${esc(c)}" aria-pressed="${st.keys.includes(c)}">${esc(c)}</button>`).join("")}</div></div>
        </div>
        <div class="form-section">
          <div class="section-title"><div class="step-num">B</div><div><h3>How strict is a match?</h3></div></div>
          <div class="section-body grid grid-2">
            ${[["trim", "Ignore extra spaces", "“ABC  School ” matches “ABC School”"], ["ignore_case", "Ignore upper/lower case", "INFO@A.COM matches info@a.com"],
               ["smart", "Smart match phones, emails & websites", "+91 98480 12345 matches 9848012345; www. and https:// are ignored"], ["ignore_punct", "Ignore punctuation", "A.B.C. School matches ABC School"]]
              .map(([k, l, s]) => `<label class="switch"><input type="checkbox" data-m="${k}" ${st.match[k] ? "checked" : ""}><span class="sw"></span><span><b>${l}</b><small>${s}</small></span></label>`).join("")}
          </div>
        </div>
        <div class="form-section">
          <div class="section-title"><div class="step-num">C</div><div><h3>When duplicates are found, keep…</h3></div></div>
          <div class="section-body">
            <div class="option-grid">${[["first", "The first row", "From the earliest sheet in your list"], ["last", "The last row", "From the latest sheet"], ["most_complete", "The most complete row", "The row with the fewest empty cells"]]
              .map(([k, l, s]) => `<label class="option radio"><input type="radio" name="keep" value="${k}" ${st.keep === k ? "checked" : ""}><span><b>${l}</b><small>${s}</small></span><span class="box"></span></label>`).join("")}</div>
            <label class="switch mt-16"><input type="checkbox" id="fill" ${st.fill_empty ? "checked" : ""}><span class="sw"></span><span><b>Fill empty cells from the removed duplicates</b><small>Keeps the most information, e.g. a phone found only in the duplicate.</small></span></label>
            <label class="switch mt-8"><input type="checkbox" id="saverm" ${st.save_removed ? "checked" : ""}><span class="sw"></span><span><b>Save removed duplicates as a separate sheet</b><small>So you can check what was removed.</small></span></label>
            <div class="field mt-16" style="max-width:420px"><label class="label" for="oname">Name of the new sheet</label><input class="input" id="oname" maxlength="60" placeholder="Merged" value="${esc(st.output_name)}"></div>
          </div>
        </div>
        <div class="pager"><button class="btn" id="back2">${icon("back")}Back</button><button class="btn btn-primary" id="next2">Preview ${icon("arrow")}</button></div>`;
      $("#keys").addEventListener("click", (e) => {
        const b = e.target.closest("[data-k]"); if (!b) return;
        const k = b.dataset.k; st.keys = st.keys.includes(k) ? st.keys.filter((x) => x !== k) : [...st.keys, k];
        b.classList.toggle("active"); b.setAttribute("aria-pressed", st.keys.includes(k));
      });
      $$("[data-m]", el).forEach((c) => c.onchange = () => { st.match[c.dataset.m] = c.checked; });
      $$("input[name=keep]", el).forEach((r) => r.onchange = () => { st.keep = r.value; });
      $("#fill").onchange = (e) => st.fill_empty = e.target.checked;
      $("#saverm").onchange = (e) => st.save_removed = e.target.checked;
      $("#oname").oninput = (e) => st.output_name = e.target.value;
      $("#back2").onclick = () => { st.step = 1; render(); };
      $("#next2").onclick = async () => {
        if (st.selected.size === 1 && !st.keys.length) return toast("Choose at least one column to find duplicates in a single sheet.", "err");
        const b = $("#next2"); b.disabled = true; b.innerHTML = '<span class="spinner"></span>Checking…';
        try {
          await new Promise((r) => setTimeout(r, 30));  // let the spinner paint before the heavy work
          const picked = await Promise.all([...st.selected].map(async (id) => {
            const m = sheets.find((x) => x.id === id), d = await Store.sheets.data(id);
            return { name: m.name, columns: d.columns, rows: d.rows };
          }));
          st.result = SheetOps.merge(picked, { keys: st.keys, match: st.match, keep: st.keep, fillEmpty: st.fill_empty });
          st.preview = SheetOps.mergeSummary(st.result);
          st.step = 3; render();
        }
        catch (e) { toast(e.message, "err"); b.disabled = false; b.innerHTML = `Preview ${icon("arrow")}`; }
      };
    } else {
      const p = st.preview;
      const cols = p.columns.filter((c) => c !== "Source Sheet").slice(0, 5);
      el.innerHTML = `<div class="card-body">
        <div class="grid grid-3">
          <div class="card kpi" style="box-shadow:none;background:var(--surface-2)"><small>Rows in</small><b>${fmtNum(p.rows_in)}</b><div class="sub">from ${st.selected.size} sheet${st.selected.size > 1 ? "s" : ""}</div></div>
          <div class="card kpi" style="box-shadow:none;background:var(--surface-2)"><small>Duplicates removed</small><b style="color:var(--red)">${fmtNum(p.removed)}</b><div class="sub">${st.keys.length ? `matched on ${esc(st.keys.join(" + "))}` : "no duplicate check"}</div></div>
          <div class="card kpi" style="box-shadow:none;background:var(--surface-2)"><small>Rows in new sheet</small><b style="color:var(--green)">${fmtNum(p.rows_out)}</b><div class="sub">${p.columns.length} columns</div></div>
        </div>
        ${p.sample_removed.length ? `<h3 style="font-size:14px;margin:24px 0 10px">Examples of removed rows</h3>
          <div class="table-wrap card" style="box-shadow:none"><table class="tbl grid-tbl"><thead><tr><th>#</th>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}<th>Why removed</th></tr></thead>
          <tbody>${p.sample_removed.map((r, i) => `<tr><td>${i + 1}</td>${cols.map((c) => cellHtml(r.row[c])).join("")}<td>${esc(r.why)}</td></tr>`).join("")}</tbody></table></div>`
          : `<div class="callout ok mt-16">${icon("check")}<div>No duplicates found${st.keys.length ? "" : " (no duplicate check chosen)"}. The sheets will simply be combined.</div></div>`}
      </div>
      <div class="pager"><button class="btn" id="back3">${icon("back")}Back</button><button class="btn btn-primary" id="save">${icon("check")}Save new sheet</button></div>`;
      $("#back3").onclick = () => { st.step = 2; render(); };
      $("#save").onclick = async () => {
        const b = $("#save"); b.disabled = true; b.innerHTML = '<span class="spinner"></span>Saving…';
        try {
          const name = stampName(st.output_name.trim() || "Merged");
          const meta = await Store.sheets.put({ id: newId(), name, source: "merge", created: nowIso() }, { columns: st.result.columns, rows: st.result.rows });
          if (st.save_removed && st.result.removedRows.length) {
            await Store.sheets.put({ id: newId(), name: name + " (removed duplicates)", source: "merge", created: nowIso() }, { columns: st.result.columns, rows: st.result.removedRows });
          }
          Store.persist();
          toast(`Saved “${name}” with ${fmtNum(meta.rows)} rows`, "ok");
          location.hash = `#/sheets/${meta.id}`;
        } catch (e) { toast(e.message, "err"); b.disabled = false; b.innerHTML = `${icon("check")}Save new sheet`; }
      };
    }
  };
  render();
}
/* ================================================================ SETTINGS */
const PROVIDER_ORDER = ["openai", "gemini", "anthropic", "openrouter", "groq", "custom"];
const PROVIDER_HELP = {
  anthropic: ["console.anthropic.com", "https://console.anthropic.com/settings/keys"],
  openai: ["platform.openai.com", "https://platform.openai.com/api-keys"],
  gemini: ["aistudio.google.com", "https://aistudio.google.com/app/apikey"],
  openrouter: ["openrouter.ai", "https://openrouter.ai/settings/keys"],
  groq: ["console.groq.com", "https://console.groq.com/keys"],
  custom: null,
};
async function viewSettings() {
  const tk = routeToken();
  setCrumbs("Settings");
  const v = $("#view");
  const s = await api("/api/settings");
  if (stale(tk)) return;
  const srcData = await api("/api/sources");
  const presetData = await api("/api/sources/presets");
  if (stale(tk)) return;
  const st = { provider: s.ai_provider, replacing: !s.ai_key_mask };
  v.innerHTML = `
    <div class="page-head"><div><h1>Settings</h1><p>Your AI provider, password and appearance.</p></div></div>
    <div class="stack" style="max-width:860px">
      <section class="card">
        <div class="card-head"><div><h3>${icon("sparkle")} AI provider</h3><p>Bring your own key. It's used only for your runs, never shared, and stored encrypted.</p></div><span id="ai-status"></span></div>
        <div class="card-body">
          <div class="field"><span class="label">Provider</span>
            <div class="provider-grid" id="providers">${PROVIDER_ORDER.filter((k) => CFG.providers[k]).map((k) => [k, CFG.providers[k]]).map(([k, p]) => `
              <label class="option radio"><input type="radio" name="provider" value="${k}" ${k === st.provider ? "checked" : ""}><span><b>${esc(p.label)}</b><small>${p.model ? `Default: ${esc(p.model)}` : "Any OpenAI-compatible API"}</small></span><span class="box"></span></label>`).join("")}</div></div>
          <div class="field" id="key-field"></div>
          <div class="grid grid-2">
            <div class="field"><label class="label" for="model">Model <span class="opt">(optional)</span></label><input class="input mono" id="model" value="${esc(s.ai_model)}" autocomplete="off"><div class="hint" id="model-hint"></div></div>
            <div class="field" id="url-field"><label class="label" for="base_url">API URL</label><input class="input mono" id="base_url" value="${esc(s.ai_base_url)}" placeholder="e.g. http://localhost:11434/v1" autocomplete="off"><div class="hint">For Ollama, LM Studio, Together, DeepSeek or any OpenAI-compatible service.</div></div>
          </div>
          <div class="row-flex"><button class="btn btn-primary" id="save-ai">Save</button><button class="btn" id="test-ai">${icon("check")}Test connection</button><span id="test-msg" class="small"></span></div>
        </div>
      </section>
      <section class="card">
        <div class="card-head"><div><h3>${icon("globe")} Directory sources</h3><p>Websites that list places, such as a college, school or hospital directory. Each one says where its lists are and how to read them - no code needed.</p></div><span class="badge plain">${srcData.sources.length} saved</span></div>
        <div class="card-body">
          ${srcData.sources.length ? `<ul class="summary-list" id="src-list">${srcData.sources.map((x) => `
            <li><span>${esc(x.name)}${x.category ? ` <small>· ${esc(x.category)}</small>` : ""}</span><b><small>${x.lists} list page${x.lists === 1 ? "" : "s"} · ${x.pages} page${x.pages === 1 ? "" : "s"} each${x.profile ? " · reads each entry's page" : ""}</small> <button class="btn btn-sm btn-danger" data-del="${x.id}">Remove</button></b></li>`).join("")}</ul>` : `<p class="small">No sources yet. Start from a preset below, or write your own.</p>`}
          <div class="field mt-24"><span class="label">Start from a preset <span class="opt">(copied into your sources, then you can change it)</span></span>
            <div class="row-flex" id="presets">${presetData.presets.map((p) => `<button class="btn btn-sm" data-preset="${esc(p.id)}" title="${esc(p.description || "")}">${icon("plus")}${esc(p.name)}</button>`).join("") || '<span class="small">No presets are installed.</span>'}</div>
          </div>
          <details class="mt-24" id="src-form-wrap"><summary><b>Write your own source</b></summary>
            <div class="grid grid-2 mt-24">
              <div class="field"><label class="label" for="src-name">Name</label><input class="input" id="src-name" placeholder="e.g. Hospitals - Pune"></div>
              <div class="field"><label class="label" for="src-cat">Category <span class="opt">(optional)</span></label><input class="input" id="src-cat" placeholder="e.g. Hospitals"></div>
            </div>
            <div class="field"><label class="label" for="src-urls">List page addresses <span class="opt">(one per line, up to 10)</span></label><textarea class="input mono" id="src-urls" rows="3" placeholder="https://example.com/hospitals/pune/"></textarea></div>
            <div class="grid grid-2">
              <div class="field"><label class="label" for="src-mode">How to read the list</label>
                <select class="input" id="src-mode"><option value="jsonld">Structured data (the list the page publishes for search engines)</option><option value="css">CSS selectors (for other pages)</option></select></div>
              <div class="field"><label class="label" for="src-pages">Pages per list</label><input class="input" id="src-pages" type="number" min="1" max="30" value="1"><div class="hint">Page 2, 3… are added as ?page=2, ?page=3…</div></div>
            </div>
            <div id="src-css" hidden>
              <div class="grid grid-2">
                <div class="field"><label class="label" for="src-item">Entry selector</label><input class="input mono" id="src-item" placeholder="e.g. div.hospital-card"></div>
                <div class="field"><label class="label" for="src-namesel">Name selector</label><input class="input mono" id="src-namesel" placeholder="e.g. h3"></div>
                <div class="field"><label class="label" for="src-link">Link selector <span class="opt">(optional)</span></label><input class="input mono" id="src-link" placeholder="e.g. a[href]"></div>
                <div class="field"><label class="label" for="src-addr">Address selector <span class="opt">(optional)</span></label><input class="input mono" id="src-addr" placeholder="e.g. .address"></div>
              </div>
            </div>
            <label class="row-flex mt-24"><input type="checkbox" id="src-profile"> Open each entry's own page for its phone, email and website</label>
            <div class="row-flex mt-24"><button class="btn" id="src-test">${icon("check")}Test on the first page</button><button class="btn btn-primary" id="src-save">Save source</button><span id="src-msg" class="small"></span></div>
            <div id="src-preview" class="mt-24"></div>
          </details>
        </div>
      </section>
      <section class="card">
        <div class="card-head"><div><h3>${icon("search")} Search connector: Brave Search</h3><p>Your own Brave Search key. Website lookups use it, so each person's searches run on their own quota. Stored encrypted, never shared.</p></div><span id="brave-status"></span></div>
        <div class="card-body">
          <div class="field" id="brave-field"></div>
          <div class="row-flex"><button class="btn btn-primary" id="save-brave">Save</button><button class="btn" id="test-brave">${icon("check")}Test key</button><span id="brave-msg" class="small"></span></div>
          <div class="hint">Get a free key at <a href="https://brave.com/search/api/" target="_blank" rel="noopener noreferrer">brave.com/search/api</a>. The free plan allows about one search per second and a monthly quota, so large runs take longer.</div>
        </div>
      </section>
      ${CFG.restricted ? `
      <section class="card">
        <div class="card-head"><div><h3>${icon("key")} Social logins <span class="badge cancelled plain">Restricted</span></h3><p>Enabled by your admin. Use only accounts your company owns and where the platform's terms allow it.</p></div></div>
        <div class="card-body">
          <div class="callout warn" style="margin-bottom:16px">${icon("alert")}<div>LinkedIn and Facebook can suspend accounts used for automated collection. Prefer official exports or APIs where possible.</div></div>
          ${[["li_at_cookie", "LinkedIn session cookie (li_at)", s.li_cookie_mask], ["fb_cookie", "Facebook cookies (c_user and xs)", s.fb_cookie_mask]].map(([k, l, mask]) => `
            <div class="field"><label class="label" for="${k}">${l}</label><input class="input mono" id="${k}" type="password" autocomplete="off" placeholder="${mask ? `Saved ${esc(mask)} - paste to replace` : "Paste the cookie value"}"></div>`).join("")}
          <button class="btn btn-primary" id="save-social">Save</button>
        </div>
      </section>` : ""}
      <section class="card">
        <div class="card-head"><div><h3>${icon("sheet")} Data on this device</h3><p>Your sheets and run history are saved in this browser. The server never stores them.</p></div></div>
        <div class="card-body">
          <div id="usage"><div class="skel" style="width:60%"></div></div>
          <div class="callout info mt-16">${icon("info")}<div>Sheets don't follow you to another computer or browser. Download a backup to move them or keep a safe copy.</div></div>
          <div class="row-flex mt-16">
            <button class="btn" id="backup">${icon("download")}Download backup</button>
            <button class="btn" id="restore">${icon("upload")}Restore from backup</button>
            <input type="file" id="restore-file" accept=".json,application/json" hidden>
            <button class="btn btn-danger" id="wipe">${icon("trash")}Delete all data on this device</button>
          </div>
        </div>
      </section>
      <section class="card">
        <div class="card-head"><div><h3>${icon("download")} Install the app</h3><p>Add Data Collector to your desktop or phone. It opens in its own window and works offline with your saved sheets.</p></div></div>
        <div class="card-body" id="install-body"></div>
      </section>
      <section class="card">
        <div class="card-head"><div><h3>${icon("key")} Password</h3><p>Change the password you use to sign in.</p></div></div>
        <div class="card-body"><div class="grid grid-3">
          <div class="field"><label class="label" for="pw-cur">Current password</label><input class="input" id="pw-cur" type="password" autocomplete="current-password"></div>
          <div class="field"><label class="label" for="pw-new">New password</label><input class="input" id="pw-new" type="password" autocomplete="new-password"><div class="hint">At least 8 characters.</div></div>
          <div class="field"><label class="label" for="pw-new2">Repeat new password</label><input class="input" id="pw-new2" type="password" autocomplete="new-password"></div>
        </div><button class="btn btn-primary" id="save-pw">Change password</button></div>
      </section>
      <section class="card">
        <div class="card-head"><div><h3>${icon("sun")} Appearance</h3><p>Choose light, dark, or follow your computer.</p></div>
          <div class="seg" id="theme">${[["system", "System"], ["light", "Light"], ["dark", "Dark"]].map(([k, l]) => `<button data-t="${k}">${l}</button>`).join("")}</div></div>
      </section>
    </div>`;

  const renderKey = () => {
    const help = PROVIDER_HELP[st.provider];
    const keyLabel = st.provider === "custom" ? "API key <span class=\"opt\">(if your service needs one)</span>" : "API key";
    $("#key-field").innerHTML = !st.replacing && s.ai_key_mask
      ? `<span class="label">${keyLabel}</span><div class="saved-key">${icon("key")}<span class="grow">${esc(s.ai_key_mask)}</span><button class="btn btn-sm" id="replace">Replace</button><button class="btn btn-sm btn-danger" id="remove">Remove</button></div>`
      : `<label class="label" for="key">${keyLabel}</label><input class="input mono" id="key" type="password" autocomplete="off" placeholder="Paste your API key">
         <div class="hint">${help ? `Get a key at <a href="${help[1]}" target="_blank" rel="noopener noreferrer">${help[0]}</a>. ` : ""}Your key is encrypted and only used when you add AI details to a run.</div>`;
    $("#replace")?.addEventListener("click", () => { st.replacing = true; renderKey(); $("#key").focus(); });
    $("#remove")?.addEventListener("click", async () => {
      if (!await confirmDialog("Remove your AI key?", "AI details will stop working until you add a key again.", "Remove")) return;
      await api("/api/settings", { method: "POST", body: { ai_api_key: "" } });
      s.ai_key_mask = ""; st.replacing = true; renderKey(); renderStatus(); toast("AI key removed", "ok");
    });
    const p = CFG.providers[st.provider];
    $("#model").placeholder = p.model || "e.g. llama3.1, deepseek-chat";
    $("#model-hint").textContent = p.model ? `Leave empty to use ${p.model}.` : "Required for this provider.";
    $("#url-field").hidden = st.provider !== "custom";
  };
  const renderStatus = () => {
    $("#ai-status").innerHTML = s.ai_key_mask || (s.ai_provider === "custom" && s.ai_base_url) ? '<span class="badge done">Ready</span>' : '<span class="badge plain">Not set up</span>';
  };
  $("#providers").addEventListener("change", (e) => { if (e.target.name === "provider") { st.provider = e.target.value; renderKey(); } });
  renderKey(); renderStatus();

  $("#save-ai").onclick = async () => {
    const body = { ai_provider: st.provider, ai_model: $("#model").value, ai_base_url: st.provider === "custom" ? $("#base_url").value : "" };
    const key = $("#key")?.value.trim();
    body.ai_api_key = key ? key : (s.ai_key_mask ? "__keep__" : "");
    if (!key && !s.ai_key_mask && st.provider !== "custom") return toast("Paste your API key first.", "err");
    if (st.provider === "custom" && !body.ai_base_url) return toast("Enter the API URL for your provider.", "err");
    try {
      await api("/api/settings", { method: "POST", body });
      Object.assign(s, await api("/api/settings"));
      st.replacing = !s.ai_key_mask; renderKey(); renderStatus();
      toast("AI settings saved", "ok", { label: "Test now", run: () => $("#test-ai").click() });
    } catch (e) { toast(e.message, "err"); }
  };
  $("#test-ai").onclick = async () => {
    const b = $("#test-ai"), msg = $("#test-msg");
    b.disabled = true; msg.innerHTML = '<span class="spinner"></span>';
    try {
      const r = await api("/api/settings/test", { method: "POST" });
      msg.innerHTML = r.ok ? `<span class="badge done">Connected</span>` : `<span class="badge error">${esc(r.error)}</span>`;
    } catch (e) { msg.innerHTML = `<span class="badge error">${esc(e.message)}</span>`; }
    b.disabled = false;
  };
  const srcBody = () => {
    const mode = $("#src-mode").value;
    const config = {
      list_urls: $("#src-urls").value.split("\n").map((x) => x.trim()).filter(Boolean),
      pages: +$("#src-pages").value || 1, page_param: "page", mode, profile: $("#src-profile").checked,
    };
    if (mode === "css") Object.assign(config, { item_selector: $("#src-item").value.trim(), name_selector: $("#src-namesel").value.trim(),
      link_selector: $("#src-link").value.trim(), address_selector: $("#src-addr").value.trim() });
    return { name: $("#src-name").value.trim(), category: $("#src-cat").value.trim(), config };
  };
  $("#src-mode").onchange = () => { $("#src-css").hidden = $("#src-mode").value !== "css"; };
  $("#src-test").onclick = async () => {
    const b = $("#src-test"), out = $("#src-preview");
    b.disabled = true; out.innerHTML = '<span class="spinner"></span> Reading the first page…';
    try {
      const r = await api("/api/sources/test", { method: "POST", body: { config: srcBody().config } });
      if (!r.ok) { out.innerHTML = `<span class="badge error">${esc(r.error)}</span>`; }
      else {
        out.innerHTML = `<p class="small"><b>${r.count}</b> entries found. First few:</p><ul class="summary-list">${r.sample.map((x) => `<li><span>${esc(x.name)}</span><b>${esc(x.address || "")}</b></li>`).join("")}</ul>`
          + (r.profile ? `<p class="small mt-24">Its own page gave: ${esc(["phones", "emails"].map((k) => (r.profile[k] || []).join(", ")).filter(Boolean).join(" · ") || "no phone or email")}${r.profile.website ? ` · website ${esc(r.profile.website)}` : ""}</p>` : "");
      }
    } catch (e) { out.innerHTML = `<span class="badge error">${esc(e.message)}</span>`; }
    b.disabled = false;
  };
  $("#src-save").onclick = async () => {
    const body = srcBody();
    try {
      await api("/api/sources", { method: "POST", body });
      toast("Source saved", "ok"); route();
    } catch (e) { $("#src-msg").innerHTML = `<span class="badge error">${esc(e.message)}</span>`; }
  };
  $$("[data-preset]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/api/sources/presets/${b.dataset.preset}`, { method: "POST" }); toast("Preset added to your sources", "ok"); route(); }
    catch (e) { toast(e.message, "err"); }
  }));
  $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!await confirmDialog("Remove this source?", "Runs already made from it are not affected.", "Remove")) return;
    try { await api(`/api/sources/${b.dataset.del}`, { method: "DELETE" }); route(); }
    catch (e) { toast(e.message, "err"); }
  }));
  const renderBrave = () => {
    $("#brave-status").innerHTML = s.brave_key_mask ? '<span class="badge done">Connected</span>' : '<span class="badge plain">Not set up</span>';
    $("#brave-field").innerHTML = s.brave_key_mask
      ? `<span class="label">Brave API key</span><div class="saved-key">${icon("key")}<span class="grow">${esc(s.brave_key_mask)}</span><button class="btn btn-sm btn-danger" id="remove-brave">Remove</button></div>`
      : `<label class="label" for="brave-key">Brave API key</label><input class="input mono" id="brave-key" type="password" autocomplete="off" placeholder="Paste your Brave Search API key">`;
    $("#remove-brave")?.addEventListener("click", async () => {
      if (!await confirmDialog("Remove your Brave key?", "Website lookups will fall back to the free search engines until you add a key again.", "Remove")) return;
      await api("/api/settings", { method: "POST", body: { brave_api_key: "" } });
      s.brave_key_mask = ""; renderBrave(); toast("Brave key removed", "ok");
    });
  };
  renderBrave();
  $("#save-brave").onclick = async () => {
    const key = $("#brave-key")?.value.trim();
    if (!key && !s.brave_key_mask) return toast("Paste your Brave key first.", "err");
    try {
      await api("/api/settings", { method: "POST", body: { brave_api_key: key ? key : "__keep__" } });
      Object.assign(s, await api("/api/settings"));
      renderBrave(); toast("Brave key saved", "ok", { label: "Test key", run: () => $("#test-brave").click() });
    } catch (e) { toast(e.message, "err"); }
  };
  $("#test-brave").onclick = async () => {
    const b = $("#test-brave"), msg = $("#brave-msg");
    b.disabled = true; msg.innerHTML = '<span class="spinner"></span>';
    try {
      const r = await api("/api/settings/brave/test", { method: "POST" });
      msg.innerHTML = r.ok ? '<span class="badge done">Working</span>' : `<span class="badge error">${esc(r.error)}</span>`;
    } catch (e) { msg.innerHTML = `<span class="badge error">${esc(e.message)}</span>`; }
    b.disabled = false;
  };
  $("#save-social")?.addEventListener("click", async () => {
    const body = {};
    ["li_at_cookie", "fb_cookie"].forEach((k) => { const val = $(`#${k}`).value.trim(); body[k] = val || "__keep__"; });
    try { await api("/api/settings", { method: "POST", body }); toast("Social logins saved", "ok"); route(); } catch (e) { toast(e.message, "err"); }
  });
  $("#save-pw").onclick = async () => {
    const cur = $("#pw-cur").value, nw = $("#pw-new").value;
    if (nw !== $("#pw-new2").value) return toast("The new passwords don't match.", "err");
    try {
      await api("/api/me/password", { method: "POST", body: { current: cur, new: nw } });
      ["#pw-cur", "#pw-new", "#pw-new2"].forEach((id) => $(id).value = "");
      toast("Password changed", "ok");
    } catch (e) { toast(e.message, "err"); }
  };
  const syncTheme = () => { const t = document.documentElement.dataset.theme || "system"; $$("#theme button").forEach((b) => b.classList.toggle("active", b.dataset.t === t)); };
  $$("#theme button").forEach((b) => b.onclick = () => { setTheme(b.dataset.t); syncTheme(); });
  syncTheme();

  /* data on this device */
  const paintUsage = async () => {
    try {
      const [u, shs, rns] = await Promise.all([Store.usage(), Store.sheets.list(), Store.runs.list()]);
      if (stale(tk)) return;
      $("#usage").innerHTML = `<ul class="summary-list">
        <li><span>Sheets</span><b>${fmtNum(shs.length)} (${fmtNum(shs.reduce((a, x) => a + (x.rows || 0), 0))} rows)</b></li>
        <li><span>Run history</span><b>${fmtNum(rns.length)} run${rns.length === 1 ? "" : "s"}</b></li>
        <li><span>Space used by this app</span><b>${fmtSize(u.used)}${u.quota ? ` of ${fmtSize(u.quota)} available` : ""}</b></li>
        <li><span>Protected from automatic cleanup</span><b>${u.persisted ? "Yes" : 'No &nbsp;<button class="btn btn-sm" id="protect">Protect my data</button>'}</b></li></ul>`;
      $("#protect")?.addEventListener("click", async () => {
        toast((await Store.persist()) ? "Your sheets are now protected from automatic cleanup." : "Your browser didn't allow it. Installing the app usually helps.", "");
        paintUsage();
      });
    } catch (e) { $("#usage").innerHTML = `<div class="callout err">${icon("alert")}<div>${esc(e.message)}</div></div>`; }
  };
  paintUsage();
  $("#backup").onclick = async () => {
    try {
      const all = await Store.exportAll();
      saveBlob(new Blob([JSON.stringify(all)], { type: "application/json" }), `onebridge-backup-${new Date().toISOString().slice(0, 10)}.json`);
      toast(`Backup of ${all.sheets.length} sheet${all.sheets.length === 1 ? "" : "s"} downloaded`, "ok");
    } catch (e) { toast(e.message, "err"); }
  };
  $("#restore").onclick = () => $("#restore-file").click();
  $("#restore-file").onchange = async (e) => {
    const f = e.target.files[0]; e.target.value = "";
    if (!f) return;
    try {
      const n = await Store.importAll(JSON.parse(await f.text()));
      toast(`Restored ${n} sheet${n === 1 ? "" : "s"}`, "ok"); paintUsage(); refreshNavCounts();
    } catch (err) { toast(err instanceof SyntaxError ? "That file isn't a valid backup." : err.message, "err"); }
  };
  $("#wipe").onclick = async () => {
    if (!await confirmDialog("Delete everything on this device?", "All your sheets and your run history on this device will be removed. Download a backup first if you may need them.", "Delete everything")) return;
    try { await Store.clearAll(); toast("Data on this device deleted", "ok"); paintUsage(); refreshNavCounts(); } catch (e) { toast(e.message, "err"); }
  };

  /* install */
  const paintInstall = () => {
    const el = $("#install-body");
    if (!el) return;
    const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
    el.innerHTML = isStandalone() ? `<div class="callout ok">${icon("check")}<div>You're using the installed app.</div></div>`
      : deferredInstall ? `<button class="btn btn-primary" id="install-now">${icon("download")}Install on this device</button>`
      : ios ? `<p class="muted" style="margin:0">On iPhone or iPad: tap the <b>Share</b> button in Safari, then choose <b>Add to Home Screen</b>.</p>`
      : `<p class="muted" style="margin:0">In Chrome or Edge, open the browser menu (⋮) and choose <b>Install Data Collector</b>, or click the install icon at the right end of the address bar. If you don't see it, this browser may not support installing apps.</p>`;
    $("#install-now")?.addEventListener("click", promptInstall);
  };
  paintInstall();
  window.addEventListener("install-state", paintInstall);
  return () => window.removeEventListener("install-state", paintInstall);
}

/* ================================================================ ADMIN */
function databaseNotice(db) {
  db = db || {};
  const label = { postgresql: "PostgreSQL", mysql: "MySQL", sqlite: "a SQLite file" }[db.kind] || db.kind || "the database";
  if (db.fallback) return `<div class="callout err">${icon("alert")}<div><b>The configured database could not be reached (${esc(db.error)}).</b> The app is using temporary storage instead, so new users and saved AI keys will be lost when the server restarts. Check <code>DATABASE_URL</code> on the server and whether the database has expired.</div></div>`;
  if (db.kind === "sqlite" && db.on_server) return `<div class="callout warn">${icon("alert")}<div><b>Users are saved in a temporary file on the server</b> and will be lost on every restart or update. Connect a PostgreSQL database by setting <code>DATABASE_URL</code> on the server.</div></div>`;
  return `<div class="callout ok">${icon("check")}<div class="grow"><b>Connected to ${esc(label)}.</b> User accounts and saved AI keys are stored here.</div></div>`;
}
async function viewAdmin() {
  setCrumbs("Admin");
  const v = $("#view");
  const tk = routeToken();
  const [users, settings, allShares] = await Promise.all([api("/api/admin/users"), api("/api/admin/settings"), api("/api/admin/shares")]);
  let shares = allShares;
  if (stale(tk)) return;
  const render = (list) => {
    v.innerHTML = `
      <div class="page-head"><div><h1>Admin</h1><p>Manage who can use the collector and what it may do.</p></div>
        <div class="actions"><button class="btn btn-primary" id="add-user">${icon("plus")}Add user</button></div></div>
      <div class="card">
        <div class="card-head"><div><h3>${icon("users")} Users</h3><p>Admins manage everything · Members run and manage their sheets · Viewers can only look and download.</p></div><span class="muted small">${list.length} user${list.length > 1 ? "s" : ""}</span></div>
        <div class="table-wrap"><table class="tbl"><thead><tr><th>User</th><th>Role</th><th>Status</th><th>Last sign-in</th><th>Created</th><th></th></tr></thead><tbody>
          ${list.map((u) => `<tr data-id="${u.id}"><td><div class="name-cell"><div class="avatar">${esc(u.username.slice(0, 2))}</div><div><b>${esc(u.username)}</b><small>${esc(u.email || "No email")}</small></div></div></td>
            <td><select class="input" style="height:34px;width:auto" data-role aria-label="Role for ${esc(u.username)}">${["admin", "member", "viewer"].map((r) => `<option value="${r}" ${u.role === r ? "selected" : ""}>${r[0].toUpperCase() + r.slice(1)}</option>`).join("")}</select></td>
            <td>${u.active ? '<span class="badge done">Active</span>' : '<span class="badge plain">Deactivated</span>'}</td>
            <td>${u.last_login ? timeAgo(u.last_login) : '<span class="muted">Never</span>'}</td><td>${fmtDate(u.created, false)}</td>
            <td class="actions"><span class="menu-wrap"><button class="btn btn-ghost btn-sm btn-icon" data-menu aria-label="More actions">${icon("dots")}</button></span></td></tr>`).join("")}
        </tbody></table></div>
      </div>
      <div class="card mt-16">
        <div class="card-head"><div><h3>${icon("share")} Data sent to customers</h3><p>Every customer link that is active right now: who sent it, to whom, and whether it was opened. Expired links are deleted automatically.</p></div></div>
        ${(() => {
          const out = shares.filter((x) => x.kind === "external");
          return !out.length ? emptyState("share", "Nothing is shared with customers right now", "Customer links appear here while they are active.")
            : `<div class="table-wrap"><table class="tbl"><thead><tr><th>Sheet</th><th>Customer</th><th>Sent by</th><th class="num">Rows</th><th>Activity</th><th>Stops working</th><th></th></tr></thead><tbody>
              ${out.map((x) => `<tr><td><div class="name-cell"><div class="file-ico">${icon("sheet")}</div><div><b>${esc(x.name)}</b><small>${x.has_passcode ? "Passcode" : "No passcode"} · sent ${timeAgo(x.created)}</small></div></div></td>
                <td><b>${esc(x.customer)}</b></td><td>${esc(x.owner)}</td><td class="num">${fmtNum(x.rows)}</td>
                <td>${!x.views && !x.downloads ? '<span class="muted">Not opened yet</span>' : `${x.views ? `Opened ${x.views}×` : ""}${x.views && x.downloads ? " · " : ""}${x.downloads ? `downloaded ${x.downloads}×` : ""}`}</td>
                <td class="nowrap" title="${esc(fmtDate(x.expires))}">${esc(expiresIn(x.expires))}</td>
                <td class="actions"><button class="btn btn-sm btn-danger" data-admin-stop="${x.id}">Stop</button></td></tr>`).join("")}</tbody></table></div>`;
        })()}
      </div>
      <div class="card mt-16">
        <div class="card-head"><div><h3>${icon("sheet")} Where data is stored</h3><p>User accounts and saved AI keys are in the database. Sheets and run history never leave each person's own device.</p></div></div>
        <div class="card-body">${databaseNotice(settings.database)}</div>
      </div>
      <div class="card mt-16">
        <div class="card-head"><div><h3>${icon("shield")} Connector policy</h3><p>Restricted connectors are off by default. Turn them on only with legal approval.</p></div></div>
        <div class="card-body">
          <label class="switch"><input type="checkbox" id="restricted" ${settings.allow_restricted ? "checked" : ""}><span class="sw"></span>
            <span><b>Allow logged-in LinkedIn / Facebook collection</b><small>When on, people can save session cookies in Settings and the collector opens those pages as them. When off, only public search results are used.</small></span></label>
          <div class="callout warn mt-16">${icon("alert")}<div>These platforms' terms forbid automated collection and can suspend the accounts used. Personal data is covered by India's DPDP Act. Turn this on only after legal review.</div></div>
        </div>
      </div>`;
    $("#add-user").onclick = () => addUser(list, render);
    $$("[data-admin-stop]", v).forEach((b) => b.onclick = async () => {
      const x = shares.find((y) => y.id === b.dataset.adminStop);
      if (!await confirmDialog("Stop this customer link?", `“${x.name}” for ${x.customer} will be deleted from the server and the link will stop working immediately.`, "Stop sharing")) return;
      try { await api(`/api/shares/${x.id}`, { method: "DELETE" }); shares = shares.filter((y) => y.id !== x.id); toast("Link stopped", "ok"); render(list); } catch (e) { toast(e.message, "err"); }
    });
    $$("[data-role]", v).forEach((sel) => sel.onchange = async () => {
      const id = sel.closest("tr").dataset.id;
      try { const u = await api(`/api/admin/users/${id}`, { method: "PATCH", body: { role: sel.value } }); Object.assign(list.find((x) => x.id == id), u); toast("Role updated", "ok"); }
      catch (e) { toast(e.message, "err"); render(list); }
    });
    $$("[data-menu]", v).forEach((b) => b.onclick = (e) => {
      e.stopPropagation();
      const u = list.find((x) => x.id == b.closest("tr").dataset.id);
      openMenu(b, [
        { label: "Reset password", icon: "key", run: () => modal({
          title: `Reset password for ${u.username}`, confirm: "Reset password",
          body: `<label class="label" for="np">New password</label><div class="input-row"><input class="input mono" id="np" value="${genPassword()}"><button class="btn" type="button" id="gen">New</button></div><div class="hint">Share it with ${esc(u.username)} privately. They can change it in Settings.</div>`,
          onOpen: (ov) => { $("#gen", ov).onclick = () => $("#np", ov).value = genPassword(); },
          onConfirm: async (ov) => { await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { password: $("#np", ov).value } }); toast("Password reset", "ok"); },
        }) },
        { label: u.active ? "Deactivate" : "Activate", icon: u.active ? "stop" : "check", danger: u.active, run: async () => {
          if (u.active && !await confirmDialog(`Deactivate ${u.username}?`, "They won't be able to sign in. Their sheets are kept.", "Deactivate")) return;
          try { Object.assign(u, await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { active: !u.active } })); render(list); toast(u.active ? "User activated" : "User deactivated", "ok"); }
          catch (err) { toast(err.message, "err"); }
        } },
      ]);
    });
    $("#restricted").onchange = async (e) => {
      const on = e.target.checked;
      if (on && !await modal({ title: "Allow restricted connectors?", text: "Only do this with legal approval. Accounts used for automated collection may be suspended by the platform.", confirm: "Allow", danger: true })) { e.target.checked = false; return; }
      try { await api("/api/admin/settings", { method: "POST", body: { allow_restricted: on } }); CFG.restricted = on; toast(on ? "Restricted connectors allowed" : "Restricted connectors turned off", "ok"); }
      catch (err) { toast(err.message, "err"); e.target.checked = !on; }
    };
  };
  render(users);
}
function genPassword() {
  const chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const a = new Uint32Array(12); crypto.getRandomValues(a);
  return [...a].map((n) => chars[n % chars.length]).join("");
}
function addUser(list, render) {
  return modal({
    title: "Add user", text: "They sign in with this username and password.", confirm: "Add user",
    body: `<div class="field"><label class="label" for="nu">Username</label><input class="input" id="nu" autocomplete="off" placeholder="e.g. ravi.k"></div>
      <div class="field"><label class="label" for="ne">Email <span class="opt">(optional)</span></label><input class="input" id="ne" type="email" autocomplete="off"></div>
      <div class="field"><label class="label" for="nr">Role</label><select class="input" id="nr"><option value="member">Member: runs collections, manages own sheets</option><option value="viewer">Viewer: can only view and download</option><option value="admin">Admin: everything, including users</option></select></div>
      <div class="field"><label class="label" for="np">Temporary password</label><div class="input-row"><input class="input mono" id="np" value="${genPassword()}"><button class="btn" type="button" id="gen">New</button></div><div class="hint">Share it privately. They can change it in Settings.</div></div>`,
    onOpen: (ov) => { $("#gen", ov).onclick = () => $("#np", ov).value = genPassword(); },
    onConfirm: async (ov) => {
      const u = await api("/api/admin/users", { method: "POST", body: { username: $("#nu", ov).value, email: $("#ne", ov).value, role: $("#nr", ov).value, password: $("#np", ov).value } });
      list.push(u); render(list); toast(`${u.username} added`, "ok");
    },
  });
}

/* ---------------------------------------------------------------- start */
(async function boot() {
  syncOffline(); syncInstallButton();
  $("#install-btn")?.addEventListener("click", promptInstall);
  // On sign-out, forget the cached signed-in page so the next person on this browser never sees it.
  document.querySelector(".sidebar-foot form")?.addEventListener("submit", () => {
    try { navigator.serviceWorker && navigator.serviceWorker.controller && navigator.serviceWorker.controller.postMessage("purge-pages"); } catch {}
  });
  await Store.init(CFG.user.id);
  await Tracker.resume();
  if (!location.hash) history.replaceState(null, "", "#/home");
  route();
  registerServiceWorker();
  setInterval(refreshNavCounts, 15000);
})();
