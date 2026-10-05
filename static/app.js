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
const SOURCE_LABEL = { run: "Run", merge: "Merged", import: "Imported" };

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
function download(url) { const a = document.createElement("a"); a.href = url; a.download = ""; document.body.appendChild(a); a.click(); a.remove(); }
function exportMenu(sheetId) {
  return [
    { label: "Excel (.xlsx)", icon: "download", run: () => download(`/api/sheets/${sheetId}/export?format=xlsx`) },
    { label: "CSV (.csv)", icon: "download", run: () => download(`/api/sheets/${sheetId}/export?format=csv`) },
    { label: "JSON (.json)", icon: "download", run: () => download(`/api/sheets/${sheetId}/export?format=json`) },
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
  const add = (v) => { v = v.trim(); if (v && !items.includes(v)) { items.push(v); render(); } };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); add(input.value); input.value = ""; }
    else if (e.key === "Backspace" && !input.value && items.length) { items.pop(); render(); }
  });
  input.addEventListener("blur", () => { if (input.value.trim()) { add(input.value); input.value = ""; } });
  input.addEventListener("paste", (e) => {
    const text = e.clipboardData.getData("text");
    if (/\r?\n/.test(text)) { e.preventDefault(); text.split(/\r?\n/).forEach(add); }
  });
  box.addEventListener("click", () => input.focus());
  render(true);
  return { items, add, input, flush() { if (input.value.trim()) { add(input.value); input.value = ""; } } };
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
  $("#crumbs").innerHTML = parts.map((p, i) => i === parts.length - 1 ? `<b>${esc(p.label || p)}</b>`
    : `<a href="${p.href}">${esc(p.label)}</a><span>/</span>`).join("");
  document.title = `${parts[parts.length - 1].label || parts[parts.length - 1]} · OneBridge Data Collector`;
}

async function refreshNavCounts() {
  try {
    const d = await api("/api/dashboard");
    const run = $("#nav-running"), sh = $("#nav-sheets");
    run.hidden = !d.running; run.textContent = d.running;
    sh.hidden = !d.sheets_total; sh.textContent = d.sheets_total;
    return d;
  } catch { return null; }
}

const ROUTES = [
  [/^#\/home$/, () => viewHome()],
  [/^#\/new\/(web|places)$/, (m) => viewNewRun(m[1])],
  [/^#\/runs$/, () => viewRuns()],
  [/^#\/runs\/([\w-]+)$/, (m) => viewRunDetail(m[1])],
  [/^#\/sheets$/, () => viewSheets()],
  [/^#\/sheets\/([\w-]+)$/, (m) => viewSheet(m[1])],
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

  const [d, runs, sheets] = await Promise.all([api("/api/dashboard"), api("/api/runs"), api("/api/sheets")]);
  if (stale(tk)) return;
  $("#kpis").innerHTML = [
    ["runs", "Runs this week", fmtNum(d.runs_week), `${fmtNum(d.runs_total)} in total`],
    ["play", "Running now", fmtNum(d.running), d.running ? "Live progress in Runs" : "Nothing running"],
    ["sheet", "Sheets", fmtNum(d.sheets_total), "Saved results and merges"],
    ["rows", "Rows collected", fmtNum(d.rows_total), "Across all your sheets"],
  ].map(([ic, label, val, sub]) => `<div class="card kpi"><small><span class="kpi-ico">${icon(ic)}</span>${label}</small><b>${val}</b><div class="sub">${sub}</div></div>`).join("");
  if (!d.ai_ready && !IS_VIEWER) {
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
        <tr class="clickable" data-href="#/runs/${r.id}"><td><div class="name-cell"><div><b>${esc(r.name)}</b><small>${r.connector === "places" ? "Places" : "Web search"} · ${timeAgo(r.started)}</small></div></div></td>
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
const FIELD_INFO = {
  title: ["tag", "Page title / name"], emails: ["mail", "Email addresses"], phones: ["phone", "Phone & mobile numbers"],
  address: ["pin", "Postal address"], description: ["text", "Short description of the site"],
  social: ["link", "Facebook, Instagram, LinkedIn links"], contact_page: ["contact", "Link to the Contact page"],
  text_snippet: ["text", "First 500 characters of the page"],
};
const DEFAULT_FIELDS = ["title", "emails", "phones", "address"];
const PLATFORMS = [["web", "Websites", "globe"], ["linkedin.com", "LinkedIn", "link"], ["facebook.com", "Facebook", "link"], ["instagram.com", "Instagram", "link"], ["twitter.com", "X / Twitter", "link"]];
const CAT_GROUPS = [
  ["Religious", ["Hindu temples", "Churches", "Mosques", "Gurudwaras", "Buddhist / Jain temples", "All places of worship"]],
  ["Health", ["Hospitals", "Clinics & doctors", "Pharmacies"]],
  ["Education", ["Schools", "Colleges & universities"]],
  ["Food & stay", ["Restaurants", "Cafes", "Hotels"]],
  ["Business", ["Offices / companies", "IT companies", "Factories / industrial", "Supermarkets & shops", "Banks", "ATMs", "Petrol pumps"]],
  ["Public", ["Government offices", "Police stations", "Tourist attractions"]],
];

async function viewNewRun(mode) {
  if (IS_VIEWER) return viewNotFound();
  const tk = routeToken();
  setCrumbs({ label: "Runs", href: "#/runs" }, "New run");
  const v = $("#view");
  let settings = { ai_key_mask: "", ai_provider: "anthropic" };
  try { settings = await api("/api/settings"); } catch {}
  if (stale(tk)) return;
  const aiReady = !!settings.ai_key_mask || (settings.ai_provider === "custom" && settings.ai_base_url);
  const providerLabel = (CFG.providers[settings.ai_provider] || {}).label || "your AI provider";

  const aiBlock = (id) => `
    <div class="field" style="margin-top:20px">
      <label class="label">${icon("sparkle")} Other details with AI <span class="opt">(optional)</span></label>
      <div class="tags" id="${id}"></div>
      <div class="hint">${aiReady ? `Type any detail in your own words, such as <i>opening hours</i> or <i>services offered</i>. Uses your ${esc(providerLabel)} key.`
        : `Add your own AI key in <a href="#/settings">Settings</a> to fill any custom detail.`}</div>
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
        <div class="field mt-24"><label class="label">Search on</label>
          <div class="option-grid">${PLATFORMS.map(([val, label, ic]) => `
            <label class="option"><input type="checkbox" name="platform" value="${val}" ${val === "web" ? "checked" : ""}><span class="o-ico">${icon(ic)}</span><span><b>${label}</b><small>${val === "web" ? "All public websites" : "Public search results"}</small></span><span class="box"></span></label>`).join("")}
          </div>
          <div class="hint">${CFG.restricted ? "Logged-in collection for LinkedIn/Facebook is enabled by your admin and uses the cookies in your Settings."
            : "For social sites we collect only what appears in public search results (name, link, snippet). Their pages are not opened."}</div>
        </div>
      </div>
    </div>
    <div class="form-section">
      <div class="section-title"><div class="step-num">2</div><div><h3>Which details do you need?</h3><p>Each one becomes a column in your sheet.</p></div></div>
      <div class="section-body">
        <div class="option-grid">${Object.keys(FIELD_INFO).filter((k) => k in CFG.fields).map((k) => `
          <label class="option"><input type="checkbox" name="field" value="${k}" ${DEFAULT_FIELDS.includes(k) ? "checked" : ""}><span class="o-ico">${icon(FIELD_INFO[k][0])}</span><span><b>${esc(CFG.fields[k])}</b><small>${esc(FIELD_INFO[k][1])}</small></span><span class="box"></span></label>`).join("")}
        </div>
        ${aiBlock("custom")}
      </div>
    </div>
    <div class="form-section">
      <div class="section-title"><div class="step-num muted">3</div><div><h3>Options</h3><p>The defaults work well for most searches.</p></div></div>
      <div class="section-body">
        <div class="grid grid-2">
          <div class="field"><label class="label">Websites per search</label>
            <div class="range-row"><input type="range" id="max_results" min="5" max="200" step="5" value="30" aria-label="Websites per search"><output id="max_out">30</output></div>
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
        <div class="chips"><small>Try</small>${["Hyderabad, India", "Vijayawada, India", "Tirupati, India", "Chennai, India"].map((x) => `<button type="button" class="chip" data-l="${esc(x)}">${esc(x)}</button>`).join("")}</div>
      </div>
    </div>
    <div class="form-section">
      <div class="section-title"><div class="step-num muted">3</div><div><h3>Options</h3><p>Filter by name and add contact details from websites.</p></div></div>
      <div class="section-body">
        <div class="grid grid-2">
          <div class="field"><label class="label" for="name_filter">Name contains <span class="opt">(optional)</span></label><input class="input" id="name_filter" placeholder="e.g. Venkateswara, Apollo"></div>
          <div class="field"><label class="label">Max places per location</label><div class="range-row"><input type="range" id="max_places" min="10" max="500" step="10" value="300" aria-label="Max places per location"><output id="places_out">300</output></div></div>
        </div>
        <label class="switch"><input type="checkbox" id="enrich"><span class="sw"></span><span><b>Visit each place's website for emails &amp; phones</b><small>Slower, but finds contact details the map doesn't have.</small></span></label>
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
  let queries, locations, custom;
  const update = () => {
    const rows = [], checks = [];
    if (mode === "web") {
      const fields = $$("input[name=field]:checked", v).map((c) => CFG.fields[c.value]).concat(custom.items);
      const plats = $$("input[name=platform]:checked", v).map((c) => PLATFORMS.find((p) => p[0] === c.value)[1]);
      rows.push(["Searches", queries.items.length || "—"], ["Search on", plats.join(", ") || "—"],
        ["Websites", queries.items.length ? `up to ${fmtNum(queries.items.length * plats.length * +$("#max_results").value)}` : "—"],
        ["Country", CFG.regions[$("#region").value]], ["Columns", fields.length ? `${fields.length} details` : "—"]);
      checks.push([queries.items.length > 0, "At least one search"], [fields.length > 0, "At least one detail"], [plats.length > 0, "A place to search"]);
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
    custom = TagInput($("#custom"), { placeholder: aiReady ? "e.g. founder name, services offered" : "Add your AI key in Settings first", disabled: !aiReady, onChange: update });
    $("#max_results").oninput = () => { $("#max_out").textContent = $("#max_results").value; update(); };
    $$("#require button", v).forEach((b) => b.onclick = () => { $$("#require button", v).forEach((x) => x.classList.remove("active")); b.classList.add("active"); state.require = b.dataset.v; });
    $$("input[name=field], input[name=platform], #region", v).forEach((c) => c.addEventListener("change", update));
    setTimeout(() => queries.input.focus(), 50);
  } else {
    locations = TagInput($("#locations"), { placeholder: "e.g. Hyderabad, India", onChange: update });
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
    $("#enrich").onchange = update;
  }
  update();

  $("#start").onclick = async () => {
    custom.flush();
    const body = { mode, file_name: $("#file_name").value, custom_fields: custom.items.join(",") };
    if (mode === "web") {
      queries.flush();
      Object.assign(body, {
        queries: queries.items.join("\n"), max_results: $("#max_results").value, region: $("#region").value,
        require: state.require, follow_contact: $("#follow_contact").checked, one_per_site: $("#one_per_site").checked,
        fields: $$("input[name=field]:checked", v).map((c) => c.value), platforms: $$("input[name=platform]:checked", v).map((c) => c.value),
      });
      if (!queries.items.length) { toast("Add at least one search in step 1.", "err"); return queries.input.focus(); }
      if (!body.platforms.length) return toast("Choose at least one place to search.", "err");
      if (!body.fields.length && !custom.items.length) return toast("Pick at least one detail in step 2.", "err");
    } else {
      locations.flush();
      if (!state.category) { toast("Choose a category in step 1.", "err"); return $("#cat-filter").focus(); }
      Object.assign(body, { category: state.category, locations: locations.items.join("\n"), name_filter: $("#name_filter").value,
        max_results: $("#max_places").value, enrich: $("#enrich").checked });
      if (!locations.items.length) { toast("Add at least one location in step 2.", "err"); return locations.input.focus(); }
    }
    const btn = $("#start");
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Starting…';
    try {
      const r = await api("/api/jobs", { method: "POST", body });
      location.hash = `#/runs/${r.id}`;
    } catch (e) {
      toast(e.message, "err");
      btn.disabled = false; btn.innerHTML = `${icon("play")}Start run`;
    }
  };
}

/* ================================================================ RUNS */
async function viewRuns() {
  const tk = routeToken();
  setCrumbs("Runs");
  const v = $("#view");
  const state = { q: "", status: "all", all: false };
  v.innerHTML = `
    <div class="page-head"><div><h1>Runs</h1><p>Every collection you've started, with live status.</p></div>
      ${IS_VIEWER ? "" : `<div class="actions"><a class="btn btn-primary" href="#/new/web">${icon("plus")}New run</a></div>`}</div>
    <div class="card">
      <div class="toolbar">
        <div class="search grow" style="max-width:360px">${icon("search")}<input id="q" placeholder="Search runs" aria-label="Search runs"></div>
        <div class="seg" id="status">${[["all", "All"], ["running", "Running"], ["done", "Succeeded"], ["cancelled", "Cancelled"], ["error", "Failed"]].map(([k, l], i) => `<button data-s="${k}" class="${i ? "" : "active"}">${l}</button>`).join("")}</div>
        ${IS_ADMIN ? '<label class="switch" style="margin-left:auto"><input type="checkbox" id="everyone"><span class="sw"></span><span><b>Everyone</b></span></label>' : ""}
      </div>
      <div class="table-wrap" id="list">${skeletonRows(6, 6)}</div>
    </div>`;
  let runs = [], timer;
  const load = async () => { const r = await api(`/api/runs${state.all ? "?all=1" : ""}`); if (stale(tk)) return; runs = r; render(); };
  const render = () => {
    const list = runs.filter((r) => (state.status === "all" || r.status === state.status || (state.status === "running" && r.status === "queued"))
      && (!state.q || r.name.toLowerCase().includes(state.q) || (r.owner || "").toLowerCase().includes(state.q)));
    $("#list").innerHTML = !runs.length ? emptyState("runs", "No runs yet", "Start a run to collect data from the web.", IS_VIEWER ? "" : `<a class="btn btn-primary" href="#/new/web">${icon("plus")}New run</a>`)
      : !list.length ? emptyState("search", "No matching runs", "Try a different search or filter.")
      : `<table class="tbl"><thead><tr><th>Run</th><th>Status</th>${state.all ? "<th>Owner</th>" : ""}<th>Started</th><th class="num">Duration</th><th class="num">Rows</th><th></th></tr></thead><tbody>
        ${list.map((r) => `<tr class="clickable" data-id="${r.id}">
          <td><div class="name-cell"><div class="kpi-ico">${icon(r.connector === "places" ? "pin" : "globe")}</div><div><b>${esc(r.name)}</b><small>${runSubtitle(r)}</small></div></div></td>
          <td>${badge(r.status)}</td>${state.all ? `<td>${esc(r.owner)}</td>` : ""}
          <td class="nowrap" title="${esc(fmtDate(r.started))}">${timeAgo(r.started)}</td>
          <td class="num">${fmtDuration(r.duration)}</td><td class="num">${fmtNum(r.rows)}</td>
          <td class="actions"><span class="menu-wrap">${r.sheet_id ? `<a class="btn btn-sm" href="#/sheets/${r.sheet_id}" data-stop>Open sheet</a>` : ""}
            <button class="btn btn-ghost btn-sm btn-icon" data-menu="${r.id}" aria-label="More actions">${icon("dots")}</button></span></td></tr>`).join("")}
        </tbody></table>`;
  };
  $("#q").oninput = (e) => { state.q = e.target.value.trim().toLowerCase(); render(); };
  $$("#status button").forEach((b) => b.onclick = () => { $$("#status button").forEach((x) => x.classList.remove("active")); b.classList.add("active"); state.status = b.dataset.s; render(); });
  $("#everyone")?.addEventListener("change", (e) => { state.all = e.target.checked; load(); });
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
        { label: "Delete run", icon: "trash", danger: true, run: async () => {
          if (!await confirmDialog("Delete this run?", "The run history is removed. Its sheet stays in Sheets.")) return;
          try { await api(`/api/runs/${r.id}`, { method: "DELETE" }); toast("Run deleted", "ok"); load(); } catch (err) { toast(err.message, "err"); }
        } },
      ]);
    }
    const tr = e.target.closest("tr[data-id]");
    if (tr) location.hash = `#/runs/${tr.dataset.id}`;
  });
  await load();
  timer = setInterval(() => { if (runs.some((r) => r.status === "running" || r.status === "queued")) load().catch(() => {}); }, 3000);
  return () => clearInterval(timer);
}
function runSubtitle(r) {
  const s = r.spec || {};
  if (r.connector === "places") return `Places · ${esc(s.category || "")}${s.locations ? ` · ${esc(s.locations.slice(0, 2).join(", "))}` : ""}`;
  return `Web search${s.queries ? ` · ${s.queries.length} search${s.queries.length > 1 ? "es" : ""}` : ""}${s.region ? ` · ${esc(CFG.regions[s.region] || "")}` : ""}`;
}

/* ================================================================ RUN DETAIL */
async function viewRunDetail(id) {
  const tk = routeToken();
  setCrumbs({ label: "Runs", href: "#/runs" }, "Run");
  const v = $("#view");
  v.innerHTML = `<div class="card card-body"><div class="skel" style="width:40%;height:20px"></div><div class="skel mt-16"></div><div class="skel mt-8" style="width:70%"></div></div>`;
  let run = null;
  for (let i = 0; i < 6 && !run; i++) {  // a brand-new run takes a moment to be recorded
    try { run = await api(`/api/runs/${id}`); } catch (e) { if (e.status !== 404) throw e; await sleep(600); }
  }
  if (stale(tk)) return;
  if (!run) return viewNotFound();
  setCrumbs({ label: "Runs", href: "#/runs" }, run.name);
  v.innerHTML = `
    <div class="page-head"><div><div class="row-flex"><h1>${esc(run.name)}</h1><span id="badge">${badge(run.status)}</span></div>
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
      <div class="tabs" role="tablist"><button class="active" data-tab="results">Results</button><button data-tab="log">Log</button><button data-tab="input">Input</button></div>
      <div id="tab-results"></div>
      <pre class="log" id="tab-log" hidden></pre>
      <div id="tab-input" class="card-body" hidden></div>
    </div>`;
  $$(".tabs button", v).forEach((b) => b.onclick = () => {
    $$(".tabs button", v).forEach((x) => x.classList.toggle("active", x === b));
    ["results", "log", "input"].forEach((t) => $(`#tab-${t}`).hidden = t !== b.dataset.tab);
  });
  const spec = run.spec || {};
  $("#tab-input").innerHTML = `<ul class="summary-list">${Object.entries(spec).filter(([, val]) => val !== "" && val !== null && !(Array.isArray(val) && !val.length))
    .map(([k, val]) => `<li><span>${esc(k.replace(/_/g, " "))}</span><b>${esc(Array.isArray(val) ? val.join("; ") : typeof val === "boolean" ? (val ? "Yes" : "No") : val)}</b></li>`).join("")}</ul>`;

  let timer, stopped = false;
  const render = (j) => {
    const status = j ? j.status : run.status;
    const finished = !["queued", "running"].includes(status);
    $("#badge").innerHTML = badge(status);
    const isPlaces = (j?.mode || run.connector) === "places";
    const steps = isPlaces ? [["search", "Find places"], ["visit", "Check websites"], ["save", "Save sheet"], ["done", "Done"]]
                           : [["search", "Search"], ["visit", "Read websites"], ["save", "Save sheet"], ["done", "Done"]];
    const phase = j ? j.phase : "done";
    const cur = steps.findIndex((s) => s[0] === phase);
    $("#steps").innerHTML = steps.map(([, l], i) => {
      const cls = finished ? "did" : i < cur ? "did" : i === cur ? "doing" : "";
      return `<div class="s ${cls}"><span class="d">${cls === "did" ? "✓" : i + 1}</span><span class="l">${l}</span></div>`;
    }).join("");
    $("#spin").hidden = finished;
    $("#activity").textContent = j ? j.activity : finished ? "This run has finished." : "";
    const bar = $("#bar"), pct = finished ? 100 : j && j.total ? Math.round(100 * j.done / j.total) : 0;
    bar.classList.toggle("indeterminate", !finished && (!j || !j.total || j.phase === "search"));
    $("#bar > div").style.width = pct + "%";
    if (!finished && j && j.total && j.phase !== "search") {
      $("#meta-l").textContent = `${fmtNum(j.done)} of ${fmtNum(j.total)} ${isPlaces && j.phase !== "visit" ? "locations" : "websites"} · ${pct}%`;
      const left = j.done > 2 ? j.elapsed / j.done * (j.total - j.done) : null;
      $("#meta-r").textContent = left == null ? "Estimating time left…" : left < 60 ? "Less than a minute left" : `About ${Math.round(left / 60)} min left`;
    } else { $("#meta-l").textContent = finished ? "" : "Working…"; $("#meta-r").textContent = ""; }
    $("#s-rows").textContent = fmtNum(j ? j.count : run.rows);
    $("#s-email").textContent = j ? fmtNum(j.with_email) : "—";
    $("#s-phone").textContent = j ? fmtNum(j.with_phone) : "—";
    $("#s-time").textContent = fmtDuration(j ? j.elapsed : run.duration);
    const sheetId = (j && j.sheet_id) || run.sheet_id;
    $("#run-actions").innerHTML = (!finished ? `<button class="btn btn-danger" id="stop">${icon("stop")}Stop &amp; keep results</button>` : "")
      + (sheetId ? `<span class="menu-wrap"><button class="btn" id="dl">${icon("download")}Download</button></span><a class="btn btn-primary" href="#/sheets/${sheetId}">${icon("sheet")}Open sheet</a>` : "")
      + (finished && !IS_VIEWER ? `<a class="btn" href="#/new/${isPlaces ? "places" : "web"}">${icon("plus")}New run</a>` : "");
    $("#stop")?.addEventListener("click", async () => {
      $("#stop").disabled = true; $("#activity").textContent = "Stopping… finishing the pages already open.";
      try { await api(`/api/jobs/${id}/stop`, { method: "POST" }); } catch (e) { toast(e.message, "err"); }
    });
    $("#dl")?.addEventListener("click", (e) => { e.stopPropagation(); openMenu($("#dl"), exportMenu(sheetId)); });
    if (finished) {
      const err = (j && j.error) || run.error;
      const rows = j ? j.count : run.rows;
      $("#result").innerHTML = status === "error" ? `<div class="callout err mt-16">${icon("alert")}<div><b>The run stopped because of an error.</b> ${esc(err || "")} ${rows ? "The rows collected before the error were saved." : "Try again in a few minutes; if it keeps happening, share the Log tab with IT."}</div></div>`
        : rows ? `<div class="callout ok mt-16">${icon("check")}<div class="grow"><b>${fmtNum(rows)} rows saved to a sheet.</b> Open it to search, merge or download it.</div></div>`
        : `<div class="callout warn mt-16">${icon("info")}<div><b>No rows this time.</b> Try broader words, add the city or country, or choose “Everything” under Keep only rows that have.</div></div>`;
    }
    if (j) {
      $("#tab-log").textContent = j.log.join("\n") || "No log yet.";
      renderPreviewGrid($("#tab-results"), j.columns, j.preview.slice().reverse(), j.count, sheetId);
    } else {
      $("#tab-log").textContent = "The detailed log is only kept while the server is running this job.";
      $("#tab-results").innerHTML = sheetId ? `<div class="card-body">${emptyState("sheet", "Results are in the sheet", "Open the sheet to see every row.", `<a class="btn btn-primary" href="#/sheets/${sheetId}">Open sheet</a>`)}</div>`
        : `<div class="card-body">${emptyState("sheet", "No results", "This run didn't save any rows.")}</div>`;
    }
    return finished;
  };
  const poll = async () => {
    if (stopped) return;
    try {
      const j = await api(`/api/jobs/${id}`);
      if (render(j)) { refreshNavCounts(); run = await api(`/api/runs/${id}`).catch(() => run); return; }
    } catch (e) {
      if (e.status === 404) { run = await api(`/api/runs/${id}`); render(null); return; }
    }
    timer = setTimeout(poll, 1500);
  };
  if (run.live) poll(); else render(null);
  return () => { stopped = true; clearTimeout(timer); };
}

function renderPreviewGrid(el, columns, rows, total, sheetId) {
  if (!rows.length) {
    el.innerHTML = `<div class="card-body">${emptyState("rows", "No rows yet", "Rows appear here as they are collected.")}</div>`;
    return;
  }
  el.innerHTML = `<div class="grid-scroll" style="max-height:440px"><table class="tbl grid-tbl"><thead><tr><th>#</th>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r, i) => `<tr><td>${i + 1}</td>${columns.map((c) => cellHtml(r[c])).join("")}</tr>`).join("")}</tbody></table></div>
    <div class="pager"><span>Showing the latest ${fmtNum(rows.length)} of ${fmtNum(total)} rows</span>${sheetId ? `<a href="#/sheets/${sheetId}">Open full sheet →</a>` : "<span>The sheet is saved when the run finishes.</span>"}</div>`;
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
    <div class="page-head"><div><h1>Sheets</h1><p>Results from your runs, merges and imports.</p></div>
      ${IS_VIEWER ? "" : `<div class="actions"><button class="btn" id="import">${icon("upload")}Import file</button><a class="btn btn-primary" href="#/new/web">${icon("plus")}New run</a></div>`}</div>
    <input type="file" id="import-file" accept=".xlsx,.csv,.tsv,.json" hidden>
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
  const load = async () => { const r = await api("/api/sheets"); if (stale(tk)) return; sheets = r; state.selected = new Set([...state.selected].filter((id) => sheets.some((s) => s.id === id))); render(); };
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
          <td class="num">${fmtNum(s.rows)}</td><td class="num">${s.columns.length || "—"}</td><td class="num">${s.size_kb} KB</td>
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
    toast(`Importing ${f.name}…`);
    try { const r = await api("/api/sheets/import", { method: "POST", body: fd }); toast(`Imported ${fmtNum(r.rows)} rows`, "ok"); await load(); }
    catch (err) { toast(err.message, "err"); }
  };
  $("#q").oninput = (e) => { state.q = e.target.value.trim().toLowerCase(); render(); };
  $$("#source button").forEach((b) => b.onclick = () => { $$("#source button").forEach((x) => x.classList.remove("active")); b.classList.add("active"); state.source = b.dataset.s; render(); });
  $("#bulk-merge").onclick = () => { location.hash = `#/merge?ids=${[...state.selected].join(",")}`; };
  $("#bulk-del").onclick = async () => {
    const n = state.selected.size;
    if (!await confirmDialog(`Delete ${n} sheet${n > 1 ? "s" : ""}?`, "This can't be undone. Download anything you want to keep first.")) return;
    for (const id of state.selected) { try { await api(`/api/sheets/${id}`, { method: "DELETE" }); } catch (err) { toast(err.message, "err"); } }
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
        { label: "Merge & dedupe", icon: "merge", run: () => location.hash = `#/merge?ids=${s.id}` },
        "-",
        { label: "Delete", icon: "trash", danger: true, run: async () => {
          if (!await confirmDialog("Delete this sheet?", `“${s.name}” will be removed. This can't be undone.`)) return;
          try { await api(`/api/sheets/${s.id}`, { method: "DELETE" }); toast("Sheet deleted", "ok"); load(); } catch (err) { toast(err.message, "err"); }
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
    body: `<label class="label" for="rn">Name</label><input class="input" id="rn" maxlength="80" value="${esc(s.name.replace(/\.xlsx$/i, ""))}">`,
    onConfirm: async (ov) => { await api(`/api/sheets/${s.id}`, { method: "PATCH", body: { name: $("#rn", ov).value } }); toast("Sheet renamed", "ok"); after?.(); },
  });
}

/* ================================================================ SHEET VIEW (data grid) */
async function viewSheet(id) {
  const tk = routeToken();
  setCrumbs({ label: "Sheets", href: "#/sheets" }, "Sheet");
  const v = $("#view");
  const meta = (await api("/api/sheets")).find((s) => s.id === id);
  if (stale(tk)) return;
  if (!meta) return viewNotFound();
  setCrumbs({ label: "Sheets", href: "#/sheets" }, meta.name);
  const PAGE = 100;
  const state = { q: "", offset: 0, total: 0, columns: [], rows: [] };
  v.innerHTML = `
    <div class="page-head"><div><h1>${esc(meta.name)}</h1><p>${fmtNum(meta.rows)} rows · ${meta.columns.length} columns · ${SOURCE_LABEL[meta.source] || "Run"} · ${esc(fmtDate(meta.created))}</p></div>
      <div class="actions">${IS_VIEWER ? "" : `<button class="btn" id="rename">${icon("edit")}Rename</button><a class="btn" href="#/merge?ids=${id}">${icon("merge")}Dedupe / merge</a>`}
        <span class="menu-wrap"><button class="btn btn-primary" id="export">${icon("download")}Export</button></span></div></div>
    <div class="card">
      <div class="toolbar"><div class="search grow" style="max-width:420px">${icon("search")}<input id="q" placeholder="Search all columns" aria-label="Search all columns"></div>
        <span class="muted small" id="count"></span></div>
      <div class="grid-scroll" id="grid"><div class="card-body">${skeletonRows(8, 6)}</div></div>
      <div class="pager"><span id="range"></span><div class="row-flex"><button class="btn btn-sm" id="prev">Previous</button><button class="btn btn-sm" id="next">Next</button></div></div>
    </div>`;
  $("#export").onclick = (e) => { e.stopPropagation(); openMenu($("#export"), exportMenu(id)); };
  $("#rename")?.addEventListener("click", () => renameSheet(meta, () => route()));
  const load = async () => {
    const d = await api(`/api/sheets/${id}/rows?offset=${state.offset}&limit=${PAGE}&q=${encodeURIComponent(state.q)}`);
    if (stale(tk)) return;
    Object.assign(state, { total: d.total, columns: d.columns, rows: d.rows });
    $("#grid").innerHTML = !d.rows.length ? emptyState("search", state.q ? "No rows match" : "This sheet is empty", state.q ? "Try a different search." : "")
      : `<table class="tbl grid-tbl"><thead><tr><th>#</th>${d.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>
        ${d.rows.map((r, i) => `<tr class="clickable" data-i="${i}"><td>${state.offset + i + 1}</td>${d.columns.map((c) => cellHtml(r[c], state.q)).join("")}</tr>`).join("")}</tbody></table>`;
    $("#count").textContent = state.q ? `${fmtNum(d.total)} matching rows` : "";
    $("#range").textContent = d.total ? `Rows ${fmtNum(state.offset + 1)}–${fmtNum(Math.min(state.offset + PAGE, d.total))} of ${fmtNum(d.total)}` : "";
    $("#prev").disabled = state.offset === 0;
    $("#next").disabled = state.offset + PAGE >= d.total;
  };
  let deb;
  $("#q").oninput = (e) => { clearTimeout(deb); deb = setTimeout(() => { state.q = e.target.value.trim(); state.offset = 0; load(); }, 250); };
  $("#prev").onclick = () => { state.offset = Math.max(0, state.offset - PAGE); load(); $("#grid").scrollTop = 0; };
  $("#next").onclick = () => { state.offset += PAGE; load(); $("#grid").scrollTop = 0; };
  $("#grid").addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    const tr = e.target.closest("tr[data-i]");
    if (tr) rowDrawer(state.columns, state.rows[+tr.dataset.i], state.offset + +tr.dataset.i + 1);
  });
  await load();
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
  const sheets = await api("/api/sheets");
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
        <div class="pager"><span>Pick one sheet to remove its duplicates, or several to combine them. Earlier sheets win when rows match.</span><button class="btn btn-primary" id="next1">Next ${icon("arrow")}</button></div>`;
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
        try { st.preview = await api("/api/merge", { method: "POST", body: mergeBody(st, true) }); st.step = 3; render(); }
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
          const r = await api("/api/merge", { method: "POST", body: mergeBody(st, false) });
          toast(`Saved “${r.name}” with ${fmtNum(r.rows_out)} rows`, "ok");
          location.hash = `#/sheets/${r.id}`;
        } catch (e) { toast(e.message, "err"); b.disabled = false; b.innerHTML = `${icon("check")}Save new sheet`; }
      };
    }
  };
  render();
}
function mergeBody(st, preview) {
  return { sheets: [...st.selected], keys: st.keys, match: st.match, keep: st.keep, fill_empty: st.fill_empty,
    save_removed: st.save_removed, output_name: st.output_name || "Merged", preview };
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
}

/* ================================================================ ADMIN */
async function viewAdmin() {
  setCrumbs("Admin");
  const v = $("#view");
  const tk = routeToken();
  const [users, settings] = await Promise.all([api("/api/admin/users"), api("/api/admin/settings")]);
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
        <div class="card-head"><div><h3>${icon("shield")} Connector policy</h3><p>Restricted connectors are off by default. Turn them on only with legal approval.</p></div></div>
        <div class="card-body">
          <label class="switch"><input type="checkbox" id="restricted" ${settings.allow_restricted ? "checked" : ""}><span class="sw"></span>
            <span><b>Allow logged-in LinkedIn / Facebook collection</b><small>When on, people can save session cookies in Settings and the collector opens those pages as them. When off, only public search results are used.</small></span></label>
          <div class="callout warn mt-16">${icon("alert")}<div>These platforms' terms forbid automated collection and can suspend the accounts used. Personal data is covered by India's DPDP Act. Turn this on only after legal review.</div></div>
        </div>
      </div>`;
    $("#add-user").onclick = () => addUser(list, render);
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
if (!location.hash) history.replaceState(null, "", "#/home");
route();
setInterval(refreshNavCounts, 15000);
