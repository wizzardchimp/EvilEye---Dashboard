const $ = (sel) => document.querySelector(sel);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDate = (iso) => (iso ? new Date(iso).toUTCString().slice(5, 16) : "—");
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—");
function ago(iso) {
  if (!iso) return "";
  const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  if (m < 60 * 24) return `${Math.round(m / 60)}h ago`;
  return `${Math.round(m / 1440)}d ago`;
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const sk = (n = 5) => Array.from({ length: n }, () => '<div class="sk"></div>').join("");
// only ever put web links into href attributes (a feed entry could carry javascript: or similar)
const safeHref = (u) => (/^https?:\/\//i.test(u || "") ? u : "");
const isDomain = (s) => /^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(\/.*)?$/i.test(s || "");

// ------------------------------------------------------------------ theme: dark (default) or inverted light, remembered per browser
const root = document.documentElement;
function setTheme(t) {
  if (t === "light") root.dataset.theme = "light"; else delete root.dataset.theme;
  try { localStorage.setItem("evileye-theme", t); } catch (e) { /* storage unavailable */ }
  const b = $("#theme");
  if (b) { b.setAttribute("aria-pressed", String(t === "light")); b.title = t === "light" ? "Switch back to the dark theme" : "Invert colour scheme (light theme)"; }
}
$("#theme").addEventListener("click", () => setTheme(root.dataset.theme === "light" ? "dark" : "light"));
setTheme(root.dataset.theme === "light" ? "light" : "dark");

let ME = null;       // { username, role, region, database, regions, prev_visit }
let region = "";     // selected ROCU region ("" = all UK); this is also the user's saved preference

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}), "X-Requested-With": "fetch" } };
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  if (r.status === 401) { location.href = "/login"; throw new Error("Unauthorized"); }
  let j = {};
  try { j = await r.json(); } catch (e) { /* no body */ }
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
// what you do is logged for the admin (tab views, searches, filters, copies); fire and forget
function track(event, detail) {
  fetch("/api/activity", { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: JSON.stringify({ event, detail: String(detail ?? "").slice(0, 300) }) }).catch(() => {});
}
const rq = (extra = {}) => { const p = new URLSearchParams(extra); if (region) p.set("rocu", region); return p; };

// ------------------------------------------------------------------ tabs, filter bar and shareable links
const TABS = { overview: loadOverview, victims: loadVictims, news: loadNews, gangs: loadGangs, alerts: loadAlerts, users: loadUsers, activity: loadActivity };
const REGION_TABS = ["overview", "victims", "gangs"];
const activeTab = () => document.querySelector(".tab.active").dataset.tab;
function showTab(name, fromClick) {
  const btn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (!btn || btn.hidden) return;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (fromClick) track("tab_view", name);
  updateFilterBar(); syncHash(); refreshCurrent();
}
document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => showTab(btn.dataset.tab, true)));
function refreshCurrent() { (TABS[activeTab()] || loadOverview)(); }
function updateFilterBar() {
  const show = region && REGION_TABS.includes(activeTab());
  $("#filterbar").hidden = !show;
  if (show) $("#fb-region").textContent = region === "Unplaced" ? "region unknown" : region;
}
function syncHash() {
  const p = new URLSearchParams({ tab: activeTab() });
  if (region) p.set("region", region);
  try { history.replaceState(null, "", "#" + p.toString()); } catch (e) { /* ignore */ }
}
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  return { tab: p.get("tab"), region: p.get("region") };
}
$("#fb-clear").addEventListener("click", () => pickRegion(""));
// a shared link pasted into an already-open tab only changes the hash: follow it (view only, the saved region is untouched)
window.addEventListener("hashchange", () => {
  if (!ME) return;
  const h = readHash();
  const r = h.region && ME.regions.includes(h.region) ? h.region : (ME.region || "");
  if (r !== region) { region = r; $("#region").value = r; applyRegionChange(false); }
  if (h.tab && h.tab !== activeTab()) { showTab(h.tab, false); track("tab_view", `${h.tab} (shared link)`); }
});

// ------------------------------------------------------------------ health / status
async function loadHealth() {
  try {
    const h = await api("/health");
    const last = h.last_refresh?.rl_victims_gb;
    const el = $("#last-updated");
    if (h.refreshing) el.innerHTML = '<span class="ok">refreshing…</span>';
    else if (last) el.innerHTML = `<span class="ok">synced ${ago(last)}${h.placing ? " · placing regions" : ""}</span>`;
    else if (h.errors) el.innerHTML = '<span class="err">ingest error on first sync; retrying…</span>';
    else el.innerHTML = '<span class="ok">connected</span>';
    return h;
  } catch (e) { $("#last-updated").innerHTML = '<span class="err">offline</span>'; return {}; }
}

// ------------------------------------------------------------------ region selector (the header filter)
function buildRegionSelect() {
  $("#region").innerHTML = '<option value="">All UK</option>' + ME.regions.map((r) => `<option>${esc(r)}</option>`).join("");
  $("#region").value = region;
}
function applyRegionChange(save) {
  ME.region = save ? region : ME.region;
  facetsFilled = false; vm.page = 0;
  updateFilterBar(); syncHash(); loadTicker(); refreshCurrent();
}
$("#region").addEventListener("change", async () => {
  region = $("#region").value;
  try { await api("/api/me/region", { method: "PUT", body: { region } }); } catch (e) { toast("Could not save your region: " + e.message); }
  applyRegionChange(true);
});
function pickRegion(r) { $("#region").value = r; $("#region").dispatchEvent(new Event("change")); }

// ------------------------------------------------------------------ victim label helpers
const placed = (v) => v.rocu && v.rocu !== "Unplaced" && v.rocu !== "Overseas / not UK";
const townLabel = (v) => (v.town && placed(v) ? ` <span class="town">(${esc(v.town)})</span>` : "");
const newBadge = (v) => (v.new ? ' <span class="badge new">NEW</span>' : "");

// ------------------------------------------------------------------ ticker (reads the server's cache; no external calls)
let tickerSeen = new Set();
const sectorShort = (s) => (s && s !== "Unclassified" ? s : "");
function tickerItem(x, showRegion, fresh) {
  const meta = [`<span class="badge grp">${esc(x.group)}</span>`, esc(sectorShort(x.sector)), ago(x.seen)].filter(Boolean).join(" · ");
  const reg = showRegion ? `<span class="badge ${x.rocu === "Unplaced" ? "src" : "uk"}">${esc(x.rocu === "Unplaced" ? "region unknown" : x.rocu)}</span>` : "";
  const t = x.town && placed(x) ? ` <span class="town">(${esc(x.town)})</span>` : "";
  return `<li class="${fresh ? "fresh" : ""}"><div class="tk-title">${esc(x.title)}${t} ${reg}${x.new ? ' <span class="badge new">NEW</span>' : ""}</div><div class="meta">${meta}</div></li>`;
}
async function loadTicker() {
  try {
    const d = await api("/api/ticker");
    const fresh = (x) => tickerSeen.size && !tickerSeen.has(x.title);
    $("#tk-uk").innerHTML = d.uk.length ? d.uk.map((x) => tickerItem(x, true, fresh(x))).join("") : '<li class="ldr">no data yet — still syncing</li>';
    $("#tk-region").textContent = d.region || "your region";
    $("#tk-reg").innerHTML = !d.region
      ? '<li class="ldr">Choose a region in the header to see its latest incidents here.</li>'
      : d.regional.length ? d.regional.map((x) => tickerItem(x, false, fresh(x))).join("") : `<li class="ldr">No placed incidents in ${esc(d.region)} yet.</li>`;
    tickerSeen = new Set([...d.uk, ...d.regional].map((x) => x.title));
    $("#tk-updated").textContent = d.updated ? `data ${ago(d.updated)}` : "";
  } catch (e) { /* keep what is on screen */ }
}
// re-read the server cache each minute while the tab is visible, so a refresh done by anyone shows up
setInterval(() => { if (document.visibilityState === "visible" && activeTab() === "overview") loadTicker(); }, 60000);

// ------------------------------------------------------------------ overview
function pickRegionTile(r) { pickRegion(r === region ? "" : r); }
$("#chart-regions").addEventListener("click", (e) => { const t = e.target.closest(".rtile"); if (t) pickRegionTile(t.dataset.region); });
$("#region-clear").addEventListener("click", () => pickRegion(""));
function balanceStrip() {
  const el = $("#chart-regions"), n = el.children.length;
  if (!n || !el.clientWidth) return;
  const fit = Math.max(1, Math.floor((el.clientWidth + 10) / 142));
  el.style.setProperty("--cols", Math.ceil(n / Math.ceil(n / fit)));       // 14 tiles -> 7 + 7, not 8 + 6
}
window.addEventListener("resize", debounce(balanceStrip, 150));

function drawTrend(holder, series, labelFn, curLast) {
  const max = Math.max(1, ...series.map((s) => s.count));
  holder.innerHTML = series.map((s, i) => `
    <div class="col${curLast && i === series.length - 1 ? " cur" : ""}" title="${esc(labelFn(s, true))}: ${s.count}">
      <span class="val">${s.count}</span><div class="bar" style="height:${Math.max(2, (s.count / max) * 82)}%"></div><span class="lab">${esc(labelFn(s, false))}</span>
    </div>`).join("");
}
const weekLabel = (s, long) => { const d = new Date(s.week + "T00:00:00Z"); return long ? `Week of ${d.toUTCString().slice(5, 11)}` : d.toUTCString().slice(5, 11).replace(/^0/, ""); };

async function loadOverview() {
  const holder = $("#stat-cards");
  const [y, g, s, rg] = ["chart-year", "chart-gangs", "chart-sectors", "chart-regions"].map((i) => $(`#${i}`));
  if (!holder.children.length) { y.innerHTML = g.innerHTML = s.innerHTML = sk(6); }
  loadNewsTop(); loadTrend();
  try {
    const d = await api(`/api/summary?${rq()}`);
    holder.innerHTML = [
      [region ? `${region === "Unplaced" ? "Unplaced" : region} victims` : "UK victims", d.uk_total, region ? "hl" : ""],
      ["UK victims (all regions)", d.uk_all, ""],
      ["Dark web news (7 days)", d.news_7d, ""],
      ["Gangs tracked", d.gangs_total, ""],
    ].map(([label, num, cls]) => `<div class="stat ${cls}"><div class="num">${num ?? 0}</div><div class="label">${label}</div></div>`).join("");
    drawBars(y, d.by_year.map((r) => [r.year, r.count]));
    drawBars(g, d.top_gangs.map((r) => [r.group, r.count]));
    drawBars(s, d.top_sectors.map((r) => [r.sector, r.count]));
    $("#sector-note").textContent = `${d.unclassified.count} victim${d.unclassified.count === 1 ? "" : "s"} (${d.unclassified.share}%) unclassified and not ranked. ${d.sector_source}`;
    const max = Math.max(1, ...d.by_region.map((r) => r.count));
    const tiles = d.by_region.filter((r) => r.count > 0);
    rg.innerHTML = tiles.map((r) => `
      <button type="button" class="rtile${r.region === region ? " active" : ""}${r.region === "Unplaced" || r.region === "Overseas / not UK" ? " dim" : ""}" data-region="${esc(r.region)}" aria-pressed="${r.region === region}" title="Filter the dashboard to ${esc(r.region)}">
        <span class="rn">${r.region === "Unplaced" ? "Region unknown" : esc(r.region)}</span>
        <span class="rc">${r.count}</span>
        <span class="rs">${(100 * r.count / d.uk_all).toFixed(1)}% of UK</span>
        <span class="rbar"><i style="width:${Math.max(3, (r.count / max) * 100)}%"></i></span>
      </button>`).join("") || '<div class="ldr">no data yet</div>';
    $("#region-clear").hidden = !region;
    balanceStrip();
    const p = d.placement, lk = p.lookup || {};
    let note = `<strong>${p.placed}</strong> of ${p.total} victims placed to a UK region (${p.from_text} from place names, ${p.from_postcode} postcodes, ${p.from_companies_house} Companies House, ${p.from_manual} set manually). ` +
      `${p.overseas} look overseas / not UK, ${p.unplaced} still unplaced.`;
    if (!p.api_enabled) note += " Companies House lookup is off: set COMPANIES_HOUSE_KEY.";
    else if (!lk.auth_error) {
      const bits = [];
      if (lk.pending) bits.push(`${lk.pending} waiting for a register lookup`);
      if (lk.no_match) bits.push(`${lk.no_match} with no Companies House match`);
      if (bits.length) note += ` Of the unplaced: ${bits.join(", ")}.`;
      if (lk.rate_limited) note += " Companies House asked us to slow down; the rest will be looked up after the next refresh.";
    }
    if (p.running) note += " <em>Looking up regions now…</em>";
    $("#region-note").innerHTML = note;
    const warn = $("#region-warn");
    warn.hidden = !(p.api_enabled && lk.auth_error);
    if (!warn.hidden) warn.textContent = "Companies House is rejecting the API key, so no register lookups are running. Victims whose description gives no location (for example “N/A”) cannot be placed until it is fixed." + (ME.role === "admin" ? " Check COMPANIES_HOUSE_KEY in Railway." : " Ask an admin to check it.");
    const nb = $("#newbar");
    if (d.new_total > 0 && ME.prev_visit) {
      nb.hidden = false;
      nb.innerHTML = `<span><strong>${d.new_total}</strong> new UK victim${d.new_total === 1 ? "" : "s"}${region ? ` in ${esc(region)}` : ""} since your last visit (${esc(fmtTime(ME.prev_visit))}).</span><button type="button" id="newbar-go">Show them</button>`;
      $("#newbar-go").addEventListener("click", () => { vm.newOnly = true; vm.page = 0; $("#vnew").checked = true; showTab("victims", true); });
    } else nb.hidden = true;
  } catch (e) { holder.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`; }
}
async function loadTrend() {
  try {
    const d = await api(`/api/trend?${rq({ weeks: "12" })}`);
    drawTrend($("#chart-trend"), d.series, weekLabel, true);
    $("#trend-meta").textContent = `avg ${d.average}/week${region ? ` · ${region}` : ""}`;
  } catch (e) { /* leave empty */ }
}
function newsItemHTML(n, withSummary) {
  const themes = (n.themes || []).slice(0, 3).map((t) => `<span class="badge src">${esc(t)}</span>`).join(" ");
  const groups = (n.groups || []).map((g) => `<span class="badge grp">${esc(g)}</span>`).join(" ");
  // the thumbnail is fetched, checked and shrunk by the server: your browser never contacts the news site
  const url = safeHref(n.url);
  const img = n.thumb && url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" tabindex="-1" aria-hidden="true"><img class="thumb" loading="lazy" decoding="async" width="132" height="88" alt="" src="/api/news/thumb/${esc(n.id)}"></a>` : "";
  return `<div class="news-item">${img}<div class="nbody">${url ? `<a class="nt" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>` : `<span class="nt">${esc(n.title)}</span>`}
    <div class="meta"><span class="badge uk">${esc(n.source_name)}</span> ${ago(n.published)} ${themes} ${groups}${n.uk ? ' <span class="badge wm">UK</span>' : ""}</div>
    ${withSummary && n.summary ? `<div class="sum">${esc(n.summary.slice(0, 200))}${n.summary.length > 200 ? "…" : ""}</div>` : ""}</div></div>`;
}
// thumbnails: on by default, switch off from either checkbox (remembered in this browser)
function setThumbs(on) {
  document.body.classList.toggle("no-thumbs", !on);
  document.querySelectorAll(".thumbs-toggle").forEach((c) => { c.checked = on; });
  try { localStorage.setItem("evileye-thumbs", on ? "1" : "0"); } catch (e) { /* storage unavailable */ }
}
document.querySelectorAll(".thumbs-toggle").forEach((c) => c.addEventListener("change", () => { setThumbs(c.checked); track("filter", `news thumbnails: ${c.checked ? "on" : "off"}`); }));
try { setThumbs(localStorage.getItem("evileye-thumbs") !== "0"); } catch (e) { setThumbs(true); }
// an image that fails to load (blocked, removed, not an image) just disappears rather than leaving a broken icon
document.addEventListener("error", (e) => { if (e.target && e.target.classList && e.target.classList.contains("thumb")) e.target.closest("a").remove(); }, true);
async function loadNewsTop() {
  const el = $("#news-top");
  if (!el.children.length) el.innerHTML = sk(3);
  try {
    const d = await api("/api/news/top");
    el.innerHTML = d.items.length ? d.items.map((n) => newsItemHTML(n, true)).join("") : '<div class="ldr">no dark web news yet — still syncing</div>';
  } catch (e) { el.innerHTML = `<div class="ldr">${esc(e.message)}</div>`; }
}
$("#news-all").addEventListener("click", () => showTab("news", true));

function drawBars(holder, rows) {
  const max = Math.max(1, ...rows.map((r) => r[1]));
  holder.innerHTML = rows.length
    ? rows.map(([k, v, share]) => `
        <div class="bar-row">
          <div class="bk" title="${esc(k)}">${esc(k)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, (v / max) * 100)}%"></div></div>
          <div class="bv">${v}${share != null ? ` <span class="fine">${share}%</span>` : ""}</div>
        </div>`).join("")
    : '<div class="ldr">no data for this selection</div>';
}

// ------------------------------------------------------------------ modal
const modal = $("#modal");
let modalKind = "sector";
let modalBackFn = null;      // set by drill-downs that need a custom Back action
function openModal() { modalBackFn = null; modal.hidden = false; }
function closeModal() { modal.hidden = true; $("#modal-back").hidden = true; document.querySelectorAll(".more[data-detail]").forEach((b) => b.classList.remove("on")); }
async function showBreakdown(kind) {
  modalKind = kind;
  openModal(); track("modal_open", `breakdown ${kind}`);
  $("#modal-back").hidden = true;
  $("#modal-title").textContent = "Loading…"; $("#modal-explain").textContent = "";
  $("#modal-body").innerHTML = sk(6);
  const d = await api(`/api/summary/detail?${rq({ kind })}`);
  $("#modal-title").textContent = d.title;
  $("#modal-explain").textContent = kind === "sector" ? "Sector as classified by ransomware.live. Select a row for a closer breakdown." : "Select a row for a closer breakdown.";
  $("#modal-body").innerHTML = '<div class="bars"></div>';
  drawBars($("#modal-body .bars"), d.items.map((r) => [r.label, r.count, r.share]));
  $("#modal-body").querySelectorAll(".bar-row").forEach((row, i) => { row.classList.add("pick"); row.addEventListener("click", () => showSlice(kind, d.items[i].label)); });
}
async function showSlice(kind, label) {
  openModal(); $("#modal-back").hidden = false;
  $("#modal-title").textContent = label; $("#modal-explain").textContent = "Loading…";
  const body = $("#modal-body"); body.innerHTML = sk(6);
  const d = await api(`/api/summary/slice?${rq({ kind, label })}`);
  $("#modal-title").textContent = d.label;
  $("#modal-explain").textContent = `${d.explain} ${d.count} victims, ${d.share}% of the current selection.`;
  body.innerHTML = `<p class="hint">${esc(d.split_title)}</p><div class="bars"></div><div class="slice-list"></div>`;
  drawBars(body.querySelector(".bars"), d.split.map((r) => [r.label, r.count]));
  const list = body.querySelector(".slice-list");
  list.innerHTML = d.victims.length ? d.victims.map((v, i) => `
      <div class="vc" data-i="${i}" style="cursor:pointer">
        <div class="name">${esc(v.post_title)}${townLabel(v)}${newBadge(v)}</div>
        <div class="meta"><span class="badge grp">${esc(v.group || "unknown")}</span> · ${esc(v.activity)} · ${fmtDate(v.discovered)} · ${esc(v.rocu === "Unplaced" ? "region unknown" : v.rocu)}</div>
      </div>`).join("") : '<div class="ldr">no victims in this slice</div>';
  list.querySelectorAll(".vc").forEach((el) => el.addEventListener("click", () => { closeModal(); openVictim(d.victims[+el.dataset.i]); }));
}
$("#tab-overview").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-detail]");
  if (!btn) return;
  document.querySelectorAll(".more[data-detail]").forEach((b) => b.classList.toggle("on", b === btn));
  try { await showBreakdown(btn.dataset.detail); }
  catch (err) { openModal(); $("#modal-title").textContent = "Breakdown"; $("#modal-body").innerHTML = `<div class="ldr">failed to load: ${esc(err.message)}</div>`; }
});
$("#modal-close").addEventListener("click", closeModal);
$("#modal-back").addEventListener("click", () => (modalBackFn ? modalBackFn() : showBreakdown(modalKind)));
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });

// ------------------------------------------------------------------ victims: sort, page, search, export
const PAGE = 50;
const vm = { q: "", group: "", sector: "", sort: "seen", dir: "desc", page: 0, newOnly: false };
let lastVictims = [], facetsFilled = false;
function victimParams(extra = {}) {
  const p = rq({ sort: vm.sort, dir: vm.dir, ...extra });
  if (vm.q) p.set("q", vm.q);
  if (vm.group) p.set("group", vm.group);
  if (vm.sector) p.set("sector", vm.sector);
  if (vm.newOnly) p.set("new", "1");
  return p;
}
const regionCell = (v) => v.rocu === "Unplaced" ? '<span class="badge src">region unknown</span>'
  : v.rocu === "Overseas / not UK" ? '<span class="badge off">Overseas / not UK</span>'
  : `<span class="badge uk">${esc(v.rocu)}</span><div class="meta">${esc(v.force_name)}${v.region_source === "manual" ? " · set manually" : ""}</div>`;
async function loadVictims() {
  const tbody = $("#victim-rows");
  if (!tbody.children.length || tbody.querySelector(".ldr")) tbody.innerHTML = `<tr><td colspan="5">${sk(6)}</td></tr>`;
  try {
    const d = await api(`/api/victims?${victimParams({ limit: String(PAGE), offset: String(vm.page * PAGE) })}`);
    lastVictims = d.items;
    fillFacets(d.facets);
    $("#vnew-wrap").hidden = !ME.prev_visit;
    $("#vnew-label").textContent = `New since your last visit (${d.new_total})`;
    $("#vcsv").href = `/api/victims.csv?${victimParams()}`;
    document.querySelectorAll("th.sortable").forEach((th) => {
      const on = th.dataset.sort === vm.sort;
      th.classList.toggle("sorted", on);
      th.querySelector(".arrow").textContent = on ? (vm.dir === "asc" ? "▲" : "▼") : "";
    });
    const pages = Math.max(1, Math.ceil(d.total / PAGE));
    $("#vcount").textContent = `${d.total} UK victims${region ? ` in ${region}` : ""}`;
    tbody.innerHTML = d.items.length ? d.items.map((v, i) => `
        <tr class="clickable" data-i="${i}" tabindex="0">
          <td><strong>${esc(v.post_title)}</strong>${townLabel(v)}${newBadge(v)}
            ${isDomain(v.website) ? `<div class="meta">${esc(v.website)}</div>` : ""}</td>
          <td><span class="badge grp">${esc(v.group)}</span></td>
          <td>${esc(v.activity || "—")}</td>
          <td>${fmtDate(v.seen || v.discovered)}</td>
          <td>${regionCell(v)}</td>
        </tr>`).join("") : '<tr><td colspan="5" class="ldr">no matches</td></tr>';
    $("#vpager").innerHTML = d.total ? `<span>Showing ${vm.page * PAGE + 1}–${vm.page * PAGE + d.items.length} of ${d.total}</span>
      <span class="btns"><button class="more" type="button" id="vprev" ${vm.page === 0 ? "disabled" : ""}>Previous</button>
      <span style="align-self:center">Page ${vm.page + 1} of ${pages}</span>
      <button class="more" type="button" id="vnext" ${vm.page + 1 >= pages ? "disabled" : ""}>Next</button></span>` : "";
    const go = (n) => () => { vm.page += n; loadVictims(); window.scrollTo({ top: $("#tab-victims").offsetTop - 60, behavior: "smooth" }); };
    $("#vprev")?.addEventListener("click", go(-1)); $("#vnext")?.addEventListener("click", go(1));
  } catch (e) { tbody.innerHTML = `<tr><td colspan="5" class="ldr">failed to load: ${esc(e.message)}</td></tr>`; }
}
function fillFacets(f) {
  if (facetsFilled || !f) return;
  $("#vgroup").innerHTML = '<option value="">All groups</option>' + f.groups.map((g) => `<option>${esc(g)}</option>`).join("");
  $("#vsector").innerHTML = '<option value="">All sectors</option>' + f.sectors.map((s) => `<option>${esc(s)}</option>`).join("");
  $("#vgroup").value = vm.group; $("#vsector").value = vm.sector;
  facetsFilled = true;
}
document.querySelectorAll("th.sortable").forEach((th) => th.addEventListener("click", () => {
  const k = th.dataset.sort;
  vm.dir = vm.sort === k ? (vm.dir === "asc" ? "desc" : "asc") : (k === "seen" ? "desc" : "asc");
  vm.sort = k; vm.page = 0; loadVictims(); track("filter", `victims sort: ${k} ${vm.dir}`);
}));
const openRow = (tr) => { if (tr && lastVictims[+tr.dataset.i]) openVictim(lastVictims[+tr.dataset.i]); };
$("#victim-rows").addEventListener("click", (e) => { if (!e.target.closest("a")) openRow(e.target.closest("tr.clickable")); });
$("#victim-rows").addEventListener("keydown", (e) => { if (e.key === "Enter") openRow(e.target.closest("tr.clickable")); });

// ------------------------------------------------------------------ victim details drawer
const drawer = $("#drawer"), backdrop = $("#drawer-backdrop");
function closeDrawer() { drawer.hidden = true; backdrop.hidden = true; }
const BASIS = { text: "a place name in its description", postcode: "a postcode in its description", "companies-house": "its Companies House registered office",
  manual: "set manually by an admin" };
function openVictim(v) {
  track("victim_view", v.post_title);
  $("#dr-title").innerHTML = `${esc(v.post_title)}${townLabel(v)}`;
  const basis = v.region_source === "text-overseas" ? "Marked overseas because its description says it is based abroad."
    : v.region_source ? `Placed using ${BASIS[v.region_source] || v.region_source}.`
    : (v.why || "Not placed yet: no postcode, place name or Companies House match.");
  const canEdit = ME.role === "admin" && ME.database;
  $("#dr-body").innerHTML = `
    <div>${newBadge(v).trim()} <span class="badge grp">${esc(v.group)}</span> <span class="badge src">${esc(v.activity)}</span> ${regionCell(v).split("<div")[0]}</div>
    <dl class="dl">
      <dt>First seen</dt><dd>${fmtTime(v.seen || v.discovered)}</dd>
      <dt>Region</dt><dd>${v.rocu === "Unplaced" ? "Unknown" : esc(v.rocu)}${v.force_name ? ` · ${esc(v.force_name)}` : ""}${v.town && placed(v) ? ` · ${esc(v.town)}` : ""}</dd>
      <dt>Basis</dt><dd>${esc(basis)}${v.region_source && v.why ? ` ${esc(v.why)}` : ""}${v.override_note ? ` Note: ${esc(v.override_note)}` : ""}</dd>
      ${v.registered_name ? `<dt>Registered as</dt><dd>${esc(v.registered_name)}</dd>` : ""}
      ${isDomain(v.website) ? `<dt>Website</dt><dd><a href="https://${esc(v.website)}" target="_blank" rel="noopener noreferrer">${esc(v.website)}</a></dd>` : ""}
      ${v.reposts ? `<dt>Re-posted</dt><dd>${v.reposts} more time${v.reposts === 1 ? "" : "s"} by the same group</dd>` : ""}
    </dl>
    ${v.description ? `<div><div class="sect">Description (from ransomware.live)</div><div class="desc">${esc(v.description)}</div></div>` : ""}
    ${v.post_url ? `<div><div class="sect">Leak-site post (copy only, never opened from here)</div><button type="button" class="copy" data-copy="${esc(v.post_url)}" data-gang="${esc(v.group)}" data-log="${esc("Victim " + v.post_title + " (" + v.group + "): " + v.post_url)}"><span class="addr">${esc(v.post_url)}</span><span class="cpy">copy</span></button></div>` : ""}
    ${canEdit ? `<div><div class="sect">Admin: set region</div>
      <form id="plform" class="form-row">
        <label class="field">Region<select id="pl-rocu" class="search">${ME.regions.filter((r) => r !== "Unplaced").map((r) => `<option ${r === v.rocu ? "selected" : ""}>${esc(r)}</option>`).join("")}</select></label>
        <label class="field">Town / district<input id="pl-town" class="search" maxlength="60" value="${esc(v.manual ? v.town : "")}" placeholder="optional"></label>
        <label class="field">Note<input id="pl-note" class="search" maxlength="200" value="${esc(v.override_note || "")}" placeholder="optional"></label>
        <button class="btn" type="submit">Save</button>
        ${v.manual ? '<button class="more danger" type="button" id="pl-clear">Remove manual placement</button>' : ""}
      </form><p id="pl-msg" class="hint"></p></div>` : ""}`;
  drawer.hidden = false; backdrop.hidden = false;
  const done = () => { closeDrawer(); loadVictims(); loadTicker(); toast("Region updated"); };
  $("#plform")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try { await api("/api/admin/placement", { method: "PUT", body: { title: v.post_title, rocu: $("#pl-rocu").value, town: $("#pl-town").value, note: $("#pl-note").value } }); done(); }
    catch (err) { $("#pl-msg").textContent = err.message; }
  });
  $("#pl-clear")?.addEventListener("click", async () => {
    try { await api(`/api/admin/placement?title=${encodeURIComponent(v.post_title)}`, { method: "DELETE" }); done(); }
    catch (err) { $("#pl-msg").textContent = err.message; }
  });
}
$("#dr-close").addEventListener("click", closeDrawer);
backdrop.addEventListener("click", closeDrawer);

// ------------------------------------------------------------------ dark web news page
const nm = { days: 7, q: "", source: "", theme: "", group: "", uk: "", offset: 0 };
let newsFilled = false;
async function loadNews(more) {
  const list = $("#news-list");
  if (more !== true) { nm.offset = 0; if (!list.children.length) list.innerHTML = sk(6); }
  const p = new URLSearchParams({ limit: "40", offset: String(nm.offset) });
  if (nm.q) p.set("q", nm.q); if (nm.source) p.set("source", nm.source); if (nm.theme) p.set("theme", nm.theme); if (nm.group) p.set("group", nm.group); if (nm.uk) p.set("uk", "1");
  try {
    const [d, s] = await Promise.all([api(`/api/news?${p}`), more === true ? Promise.resolve(null) : api(`/api/news/summary?days=${nm.days}`)]);
    if (!newsFilled) {
      $("#nsource").innerHTML = '<option value="">All sources</option>' + d.sources.map((x) => `<option value="${esc(x.id)}">${esc(x.name)} (${x.count})</option>`).join("");
      $("#ntheme").innerHTML = '<option value="">All themes</option>' + d.themes.map((t) => `<option>${esc(t)}</option>`).join("");
      newsFilled = true;
    }
    if (s) renderNewsSummary(s);
    renderNewsChips();
    document.querySelectorAll("#n-legend .lg, #n-donut circle[data-theme]").forEach((el) => { const on = el.dataset.theme === nm.theme; el.classList.toggle("on", on); });
    const html = d.items.map((n) => newsItemHTML(n, true)).join("");
    list.innerHTML = (more === true ? list.innerHTML : "") + (html || '<div class="ldr">no items match</div>');
    nm.offset += d.items.length;
    $("#ncount").textContent = `${d.total} item${d.total === 1 ? "" : "s"} (showing ${nm.offset})`;
    $("#n-more").hidden = nm.offset >= d.total;
  } catch (e) { list.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`; }
}
const THEME_COLOR = { "Ransomware": "var(--c1)", "Data breach / leak": "var(--c2)", "Access & credentials": "var(--c3)", "Malware": "var(--c5)",
  "Forums & marketplaces": "var(--c6)", "Law enforcement": "var(--c4)", "Vulnerabilities": "var(--c7)" };
const POINT_ICON = {
  volume: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  theme: '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/>',
  groups: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  claims: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
  uk: '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/>',
};
const HOT_POINTS = new Set(["groups", "uk"]);
function donut(themes, total) {
  const R = 44, C = 2 * Math.PI * R, gap = themes.length > 1 ? 2 : 0;
  const sum = themes.reduce((a, t) => a + t.count, 0) || 1;
  let off = 0;
  const seg = themes.map((t) => {
    const len = Math.max(0, (t.count / sum) * C - gap);
    const el = `<circle cx="60" cy="60" r="${R}" stroke="${THEME_COLOR[t.theme] || "var(--c7)"}" stroke-dasharray="${len} ${C - len}" stroke-dashoffset="${-off}" data-theme="${esc(t.theme)}" class="${nm.theme === t.theme ? "on" : ""}"><title>${esc(t.theme)}: ${t.count} items</title></circle>`;
    off += (t.count / sum) * C;
    return el;
  }).join("");
  $("#n-donut").innerHTML = `<circle class="track" cx="60" cy="60" r="${R}"/>${seg}`;
  $("#n-donut-total").textContent = total;
  $("#n-legend").innerHTML = themes.map((t) => `<button type="button" class="lg${nm.theme === t.theme ? " on" : ""}" data-theme="${esc(t.theme)}" title="Show only ${esc(t.theme)} items"><i style="background:${THEME_COLOR[t.theme] || "var(--c7)"}"></i><span>${esc(t.theme)}</span><b>${t.count}</b></button>`).join("")
    + (themes.length ? '<p class="hint small" style="margin:8px 0 0">An item can carry several themes.</p>' : "");
}
function renderNewsSummary(s) {
  $("#n-when").textContent = `last ${s.days} days`;
  const dl = $("#n-delta");
  dl.hidden = false;
  if (s.change == null) { dl.className = "delta flat"; dl.textContent = "no trend yet"; dl.title = "The feeds do not reach back far enough to compare with the previous period."; }
  else { const up = s.change >= 0; dl.className = `delta ${s.change === 0 ? "flat" : up ? "up" : "down"}`; dl.textContent = `${up ? "▲" : "▼"} ${Math.abs(s.change)}% vs previous ${s.days} days`; dl.title = `Compared across the ${s.comparable_sources} source(s) whose feeds reach back far enough.`; }
  $("#n-headline").textContent = s.headline;
  $("#n-points").innerHTML = s.points.map((p) => `<li><span class="ic${HOT_POINTS.has(p.k) ? " hot" : ""}"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${POINT_ICON[p.k] || ""}</svg></span><div><div class="lbl">${esc(p.label)}</div><div class="txt">${esc(p.text)}</div></div></li>`).join("");
  donut(s.themes, s.total);
  $("#n-note").textContent = s.note;
  $("#n-stats").innerHTML = [["Items tracked", s.total, ""], ["Sources reporting", s.sources.length, ""], ["Mention the UK", s.uk_count, "hl"],
    ["Ransomware groups named", s.groups.length, ""]].map(([l, n, c]) => `<div class="stat ${c}"><div class="num">${n}</div><div class="label">${l}</div></div>`).join("");
  drawBars($("#n-sources"), s.sources.map((x) => [x.source, x.count]));
  drawBars($("#n-groups"), s.groups.map((x) => [x.group, x.count]));
  $("#n-groups").querySelectorAll(".bar-row").forEach((row, i) => { row.classList.add("pick"); row.title = "Show only items naming this group"; row.addEventListener("click", () => setNewsFilter("group", nm.group === s.groups[i].group ? "" : s.groups[i].group)); });
  drawBars($("#n-claims"), s.claim_types.map((c) => [c.type, c.count]));
  $("#n-uk").innerHTML = s.uk_items.length ? s.uk_items.map((n) => newsItemHTML(n, false)).join("") : '<div class="ldr">No items mention the UK in this period.</div>';
}
// filters: clicking a theme (donut or legend) or a group filters the list below; chips show and clear them
function setNewsFilter(key, value) {
  nm[key] = value;
  if (key === "theme") $("#ntheme").value = value;
  loadNews(); track("filter", `news ${key}: ${value || "all"}`);
  $("#nq").scrollIntoView({ behavior: "smooth", block: "center" });
}
function renderNewsChips() {
  const chips = [["theme", nm.theme], ["group", nm.group]].filter(([, v]) => v);
  const el = $("#nfilters");
  el.hidden = !chips.length;
  el.innerHTML = chips.map(([k, v]) => `<span class="chip">${k === "theme" ? "Theme" : "Group"}: <strong>${esc(v)}</strong><button type="button" data-clear="${k}" aria-label="Remove ${k} filter">&times;</button></span>`).join("");
}
$("#nfilters").addEventListener("click", (e) => { const b = e.target.closest("[data-clear]"); if (b) setNewsFilter(b.dataset.clear, ""); });
$("#brief").addEventListener("click", (e) => {
  const t = e.target.closest("[data-theme]");
  if (t) setNewsFilter("theme", nm.theme === t.dataset.theme ? "" : t.dataset.theme);
});
$("#nperiod").addEventListener("change", () => { nm.days = +$("#nperiod").value; loadNews(); track("filter", `news period: ${nm.days}d`); });
$("#nsource").addEventListener("change", () => { nm.source = $("#nsource").value; loadNews(); track("filter", `news source: ${nm.source || "all"}`); });
$("#ntheme").addEventListener("change", () => { nm.theme = $("#ntheme").value; loadNews(); track("filter", `news theme: ${nm.theme || "all"}`); });
document.querySelectorAll('input[name="nuk"]').forEach((el) => el.addEventListener("change", () => { nm.uk = el.value; loadNews(); track("filter", `news UK only: ${el.value === "1"}`); }));
$("#n-more").addEventListener("click", () => loadNews(true));
$("#nrefresh").addEventListener("click", () => loadNews());

// ------------------------------------------------------------------ gangs
async function loadGangs() {
  const grid = $("#gang-grid");
  const q = $("#gq").value.trim(), source = $("#gsource").value, sort = $("#gsort").value;
  const live = document.querySelector('input[name="glive"]:checked')?.value === "1";
  if (!grid.children.length) grid.innerHTML = sk(6);
  const extra = {};
  if (q) extra.q = q; if (source) extra.source = source; if (sort && sort !== "name") extra.sort = sort; if (live) extra.live = "1";
  try {
    const d = await api(`/api/gangs?${rq(extra)}`);
    $("#gcount").textContent = `${d.total} entries ${live ? "with a live leak site" : "in catalog"}${sort === "active" ? ` · ranked by ${region || "UK"} victims` : ""}`;
    grid.innerHTML = d.items.length ? d.items.map((g) => {
      const sites = (g.leak_sites || []).map((s) => {
        const ok = s.available === true || s.status === "ONLINE";
        const addr = s.slug || s.fqdn || "";
        return `<div class="site-line"><span class="dot ${s.available === false || s.status === "OFFLINE" ? "bad" : ok ? "ok" : "na"}"></span>${addr ? `<button type="button" class="copy" data-copy="${esc(addr)}" data-gang="${esc(g.name)}" data-log="${esc("Gang catalog, " + g.name + ": " + addr)}" title="Click to copy. Never opened from here."><span class="addr">${esc(addr)}</span><span class="cpy">copy</span></button>` : "site"}</div>`;
      });
      const meta = [g.added_date ? `tracked since ${esc(g.added_date)}` : "", g.activity ? `${g.activity} UK victim${g.activity === 1 ? "" : "s"}${region ? ` in ${esc(region)}` : ""}` : ""].filter(Boolean);
      return `<div class="gc">
          <div class="gname">${esc(g.name)} ${(g.sources || []).map((s) => `<span class="badge src">${esc(s)}</span>`).join(" ")}</div>
          ${g.altname ? `<div class="meta"><span class="badge uk">${esc(g.altname)}</span></div>` : ""}
          ${g.description ? `<div class="gdesc" title="${esc(g.description)}">${esc(g.description.slice(0, 180))}${g.description.length > 180 ? "…" : ""}</div>` : ""}
          ${sites.length ? `<div class="sites">${sites.join("")}</div>` : ""}
          <div class="meta">${meta.join(" · ")}</div></div>`;
    }).join("") : '<div class="ldr">no gangs match these filters</div>';
  } catch (e) { grid.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------------ alerts
async function loadAlerts() {
  const list = $("#alert-list");
  const q = $("#aq").value.trim(), source = $("#asource").value;
  if (!list.children.length) list.innerHTML = sk(6);
  const p = new URLSearchParams({ limit: "150" });
  if (q) p.set("q", q); if (source) p.set("source", source);
  try {
    const d = await api(`/api/intel?${p}`);
    $("#acount").textContent = `${d.total} items (national: not filtered by region)`;
    list.innerHTML = d.items.length ? d.items.map((item) => `
        <div class="post">
          <div><div class="ptitle">${esc(item.title)}</div>
            <div class="pdetail"><span class="badge src">${esc(item.source)}</span> <span class="badge grp">${esc(item.kind)}</span>
              ${item.extra?.cve ? esc(item.extra.cve) + " " : ""}${item.extra?.vendor ? esc(item.extra.vendor) + " " : ""}${item.summary ? esc(item.summary.slice(0, 220)) : ""}</div></div>
          <div class="right"><div class="pdate">${fmtDate(item.published)}</div>${safeHref(item.url) ? `<a href="${esc(safeHref(item.url))}" target="_blank" rel="noopener noreferrer">view→</a>` : ""}</div>
        </div>`).join("") : '<div class="ldr">no alerts yet — still syncing</div>';
  } catch (e) { list.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------------ outbound link clicks are logged (where, and where to)
const linkContext = (a) => a.closest(".drawer") ? "victim details" : a.closest(".news-item") ? "news" : a.closest("#tab-alerts") ? "alerts"
  : a.closest("#tab-victims") ? "victims" : a.closest("#tab-news") ? "news page" : a.closest(".foot") ? "footer" : "page";
function logLink(e) {
  const a = e.target.closest && e.target.closest("a[href]");
  if (!a) return;
  let u; try { u = new URL(a.href, location.href); } catch (err) { return; }
  if (u.origin === location.origin || !/^https?:$/.test(u.protocol)) return;
  const label = (a.textContent || "").trim().replace(/\s+/g, " ").slice(0, 70);
  track("link_click", `${linkContext(a)}: ${label ? label + " → " : ""}${u.host}${u.pathname}`);      // query strings are not logged
}
document.addEventListener("click", logLink, true);
document.addEventListener("auxclick", logLink, true);       // middle-click opens a new tab too

// ------------------------------------------------------------------ click to copy
function toast(msg) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("on");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("on"), 2200);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall back */ }
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button.copy[data-copy]");
  if (!b) return;
  const ok = await copyText(b.dataset.copy);
  const label = b.querySelector(".cpy");
  if (ok) {
    b.classList.add("done"); if (label) label.textContent = "copied";
    toast("Copied to clipboard"); track("copy_leak_site", b.dataset.log || b.dataset.gang || b.dataset.copy);
    setTimeout(() => { b.classList.remove("done"); if (label) label.textContent = "copy"; }, 1400);
  } else toast("Copy failed: select the text manually");
});

// ------------------------------------------------------------------ event names shown to admins (the raw name stays as the tooltip and filter value)
const EVENT_LABEL = {
  login: "Signed in", login_failed: "Failed sign-in", logout: "Signed out", page_open: "Opened the dashboard", refresh: "Refreshed data",
  tab_view: "Viewed a page", search: "Searched", filter: "Changed a filter", region_change: "Changed region", modal_open: "Opened a breakdown",
  victim_view: "Opened victim details", copy_leak_site: "Copied a leak-site address", link_click: "Clicked an outside link", export: "Exported data",
  screen_wake_on: "Screen wake on", screen_wake_off: "Screen wake off", screen_wake_failed: "Screen wake failed",
  password_change: "Changed own password", password_change_failed: "Password change failed",
  user_create: "Created a user", user_update: "Updated a user", user_delete: "Deleted a user",
  placement_set: "Set a victim's region", placement_clear: "Cleared a manual region",
  onboarding_done: "Finished the welcome tour", onboarding_skipped: "Skipped the welcome tour",
};
const eventLabel = (e) => EVENT_LABEL[e] || (e ? e.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "");
const eventBadge = (r) => `<span class="badge ${/failed|delete|locked/.test(r.event + r.detail) ? "off" : "src"}" title="${esc(r.event)}">${esc(eventLabel(r.event))}</span>`;

// ------------------------------------------------------------------ admin: users
const genPassword = () => {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789-_";
  const a = new Uint32Array(18); crypto.getRandomValues(a);
  return [...a].map((n) => chars[n % chars.length]).join("");
};
const ICONS = {"key": "<path d=\"M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4\"/>", "shield": "<path d=\"M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z\"/>", "ban": "<circle cx=\"12\" cy=\"12\" r=\"10\"/><line x1=\"4.93\" y1=\"4.93\" x2=\"19.07\" y2=\"19.07\"/>", "ok": "<path d=\"M22 11.08V12a10 10 0 1 1-5.93-9.14\"/><polyline points=\"22 4 12 14.01 9 11.01\"/>", "trash": "<polyline points=\"3 6 5 6 21 6\"/><path d=\"M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2\"/>"};
let userCache = [];
async function loadUsers() {
  const body = $("#user-rows");
  body.innerHTML = `<tr><td colspan="7">${sk(3)}</td></tr>`;
  try {
    const d = await api("/api/admin/users");
    userCache = d.users;
    const regionOpts = (sel) => '<option value="">All UK</option>' + d.regions.map((r) => `<option ${r === sel ? "selected" : ""}>${esc(r)}</option>`).join("");
    if ($("#u-region").options.length <= 1) $("#u-region").innerHTML = regionOpts("");
    body.innerHTML = d.users.map((u) => `
      <tr data-id="${u.id}">
        <td class="who-cell"><div class="who"><span class="nm">${esc(u.username)}${u.username === ME.username ? ' <span class="badge src">you</span>' : ""}</span><span class="meta">created ${fmtDate(u.created_at)}${u.created_by ? ` by ${esc(u.created_by)}` : ""}</span></div></td>
        <td data-label="Role"><span class="badge ${u.role === "admin" ? "wm" : "uk"}">${esc(u.role)}</span></td>
        <td data-label="Region"><select class="search u-reg" data-id="${u.id}" aria-label="Region for ${esc(u.username)}">${regionOpts(u.region)}</select></td>
        <td data-label="Status"><div class="st">${u.active ? '<span class="badge on">active</span>' : '<span class="badge off">disabled</span>'}${u.must_change ? '<span class="badge wm" title="They must choose their own password when they next sign in">must set password</span>' : ""}</div></td>
        <td data-label="Last sign-in">${u.last_login ? fmtTime(u.last_login) : '<span class="meta">never</span>'}</td>
        <td data-label="Activity"><button type="button" class="link" data-act="events" data-id="${u.id}" title="See ${esc(u.username)}'s activity">${u.events} event${u.events === 1 ? "" : "s"}</button>
          <div class="meta">${u.last_activity ? `last ${ago(u.last_activity)}` : "none yet"}</div></td>
        <td class="actions r"><div class="row-actions">
          ${[["pw", "key", "Reset password", ""], ["role", "shield", u.role === "admin" ? "Make user (remove admin)" : "Make admin", ""],
             ["active", u.active ? "ban" : "ok", u.active ? "Disable sign-in" : "Enable sign-in", ""], ["del", "trash", "Delete user", " danger"]]
            .map(([act, ic, label, cls]) => `<button type="button" class="more icon-btn${cls}" data-act="${act}" data-id="${u.id}" title="${label}" aria-label="${label}: ${esc(u.username)}"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[ic]}</svg></button>`).join("")}
        </div></td>
      </tr>`).join("");
  } catch (e) { body.innerHTML = `<tr><td colspan="7" class="ldr">${esc(e.message)}</td></tr>`; }
}
$("#u-gen").addEventListener("click", () => { $("#u-pass").value = genPassword(); });
$("#uform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#u-msg");
  try {
    const name = $("#u-name").value.trim(), pw = $("#u-pass").value;
    await api("/api/admin/users", { method: "POST", body: { username: name, password: pw, role: $("#u-role").value, region: $("#u-region").value } });
    msg.hidden = false; msg.className = "callout";
    msg.innerHTML = `<span>Created <strong>${esc(name)}</strong>. Give them this temporary password now (it cannot be shown again):</span> <code>${esc(pw)}</code> <button type="button" class="more small" data-pw="${esc(pw)}">Copy</button>`;
    $("#u-name").value = ""; $("#u-pass").value = "";
    loadUsers();
  } catch (err) { msg.hidden = false; msg.className = "callout err"; msg.textContent = err.message; }
});
// copy a temporary password (deliberately not written to the activity log)
document.addEventListener("click", async (e) => { const b = e.target.closest("button[data-pw]"); if (b) toast((await copyText(b.dataset.pw)) ? "Password copied" : "Copy failed: select it manually"); });
$("#user-rows").addEventListener("change", async (e) => {
  const sel = e.target.closest(".u-reg");
  if (!sel) return;
  try { await api(`/api/admin/users/${sel.dataset.id}`, { method: "PATCH", body: { region: sel.value } }); toast("Region saved"); }
  catch (err) { toast(err.message); loadUsers(); }
});
$("#user-rows").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const u = userCache.find((x) => String(x.id) === b.dataset.id);
  const patch = (body) => api(`/api/admin/users/${u.id}`, { method: "PATCH", body }).then(loadUsers).catch((err) => toast(err.message));
  if (b.dataset.act === "role") patch({ role: u.role === "admin" ? "user" : "admin" });
  else if (b.dataset.act === "active") patch({ active: !u.active });
  else if (b.dataset.act === "events") showUserActivity(u);
  else if (b.dataset.act === "del") {
    if (confirm(`Delete ${u.username}? Their past activity stays in the log.`)) api(`/api/admin/users/${u.id}`, { method: "DELETE" }).then(loadUsers).catch((err) => toast(err.message));
  } else if (b.dataset.act === "pw") passwordDialog(u);
});
function passwordDialog(u) {
  openModal(); $("#modal-back").hidden = true;
  $("#modal-title").textContent = `Reset password: ${u.username}`;
  $("#modal-explain").textContent = "Sets a temporary password and signs them out everywhere. Tell them the password yourself: they will be asked to choose their own the next time they sign in.";
  $("#modal-body").innerHTML = `<form id="pwform" class="form-row"><label class="field">New password (10+ characters)
    <span class="pw"><input id="np" class="search" type="text" minlength="10" required autocomplete="new-password"><button type="button" class="more" id="npg">Generate</button></span></label>
    <button class="btn" type="submit">Save</button></form><p id="pwmsg" class="hint"></p>`;
  $("#npg").addEventListener("click", () => { $("#np").value = genPassword(); });
  $("#pwform").addEventListener("submit", async (e) => {
    e.preventDefault();
    const pw = $("#np").value;
    try {
      await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { password: pw } });
      $("#pwmsg").innerHTML = `Saved. New password for <strong>${esc(u.username)}</strong>: <code>${esc(pw)}</code>`;
      loadUsers();
    } catch (err) { $("#pwmsg").textContent = err.message; }
  });
}
// click a user's activity count to see what they have been doing
async function showUserActivity(u) {
  openModal(); $("#modal-back").hidden = true;
  $("#modal-title").textContent = `Activity: ${u.username}`;
  $("#modal-explain").textContent = `${u.events} event${u.events === 1 ? "" : "s"}${u.last_activity ? `, last ${ago(u.last_activity)}` : ""}. Newest first.`;
  $("#modal-body").innerHTML = `<div class="table-wrap"><table class="table" style="min-width:0"><thead><tr><th>Time</th><th>Event</th><th>Detail</th></tr></thead><tbody id="ua-rows"><tr><td colspan="3">${sk(4)}</td></tr></tbody></table></div>
    <p style="display:flex;gap:8px;margin-top:12px"><button id="ua-more" class="more" type="button" hidden>Load more</button><button id="ua-open" class="more" type="button">Open in Activity tab</button></p>`;
  let off = 0;
  const load = async () => {
    const d = await api(`/api/admin/activity?${new URLSearchParams({ user: u.username, limit: "40", offset: String(off) })}`);
    const rows = d.items.map((r) => `<tr><td>${fmtTime(r.ts)}</td><td>${eventBadge(r)}</td><td>${esc(r.detail)}</td></tr>`).join("");
    const body = $("#ua-rows");
    body.innerHTML = (off ? body.innerHTML : "") + (rows || '<tr><td colspan="3" class="ldr">no activity recorded</td></tr>');
    off += d.items.length; $("#ua-more").hidden = off >= d.total;
  };
  try { await load(); } catch (e) { $("#ua-rows").innerHTML = `<tr><td colspan="3" class="ldr">${esc(e.message)}</td></tr>`; }
  $("#ua-more").addEventListener("click", load);
  $("#ua-open").addEventListener("click", () => { closeModal(); showTab("activity", true); setTimeout(() => { $("#act-user").value = u.username; loadActivity(); }, 50); });
}

// ------------------------------------------------------------------ admin: activity
let actOffset = 0;
const actParams = (extra = {}) => {
  const p = new URLSearchParams(extra);
  if ($("#act-user").value) p.set("user", $("#act-user").value);
  if ($("#act-event").value) p.set("event", $("#act-event").value);
  if ($("#act-q").value.trim()) p.set("q", $("#act-q").value.trim());
  return p;
};
async function loadActivity(more) {
  const body = $("#act-rows");
  if (more !== true) { actOffset = 0; body.innerHTML = `<tr><td colspan="5">${sk(4)}</td></tr>`; }
  try {
    const d = await api(`/api/admin/activity?${actParams({ limit: "100", offset: String(actOffset) })}`);
    const keepU = $("#act-user").value, keepE = $("#act-event").value;
    if ($("#act-user").options.length <= 1 || $("#act-event").options.length - 1 !== d.events.length) {
      $("#act-user").innerHTML = '<option value="">All users</option>' + d.users.map((u) => `<option>${esc(u)}</option>`).join("");
      $("#act-event").innerHTML = '<option value="">All events</option>' + [...d.events].sort((a, b) => eventLabel(a).localeCompare(eventLabel(b))).map((u) => `<option value="${esc(u)}">${esc(eventLabel(u))}</option>`).join("");
      $("#act-user").value = keepU; $("#act-event").value = keepE;
    }
    const rows = d.items.map((r) => `<tr><td>${fmtTime(r.ts)}</td><td>${esc(r.username || "—")}</td><td>${eventBadge(r)}</td><td>${esc(r.detail)}</td><td class="meta">${esc(r.ip)}</td></tr>`).join("");
    body.innerHTML = (more === true ? body.innerHTML : "") + (rows || '<tr><td colspan="5" class="ldr">no activity matches</td></tr>');
    actOffset += d.items.length;
    $("#act-count").textContent = `${d.total} events (showing ${actOffset})`;
    $("#act-more").hidden = actOffset >= d.total;
    $("#act-csv").href = `/api/admin/activity.csv?${actParams()}`;
  } catch (e) { body.innerHTML = `<tr><td colspan="5" class="ldr">${esc(e.message)}</td></tr>`; }
}
$("#act-refresh").addEventListener("click", () => loadActivity());
$("#act-more").addEventListener("click", () => loadActivity(true));
["act-user", "act-event"].forEach((id) => $(`#${id}`).addEventListener("change", () => loadActivity()));
$("#act-q").addEventListener("input", debounce(() => loadActivity(), 350));

// ------------------------------------------------------------------ first sign-in: choose your own password (enforced by the server)
function strengthOf(pw) {
  let s = 0;
  if (pw.length >= 10) s++;
  if (pw.length >= 14) s++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
  if (/\d/.test(pw)) s++;
  if (/[^A-Za-z0-9]/.test(pw)) s++;
  return pw.length < 10 ? 0 : Math.min(4, Math.max(1, s - 0));
}
function showPwReq() {
  $("#pwreq").hidden = false; document.body.style.overflow = "hidden";
  $("#pr-user").value = ME.username;
  setTimeout(() => $("#pr-cur").focus(), 30);
}
$("#pr-new").addEventListener("input", () => {
  const pw = $("#pr-new").value, l = strengthOf(pw);
  $("#pr-meter").style.width = pw ? `${l === 0 ? 8 : l * 25}%` : "0"; $("#pr-meter").dataset.l = String(l);
  $("#pr-strength").textContent = !pw ? "" : l === 0 ? "Too short: at least 10 characters" : ["", "Weak", "Okay", "Good", "Strong"][l];
});
$("#prform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#pr-msg"), cur = $("#pr-cur").value, nw = $("#pr-new").value, rep = $("#pr-rep").value;
  const fail = (t) => { msg.hidden = false; msg.textContent = t; };
  msg.hidden = true;
  if (!cur) return fail("Enter the temporary password you just signed in with.");
  if (nw !== rep) return fail("The two new passwords do not match.");
  if (nw.length < 10) return fail("Your new password must be at least 10 characters.");
  try {
    await api("/api/me/password", { method: "POST", body: { current: cur, new: nw } });
    msg.hidden = true;
    location.reload();                    // the page restarts without the lock (and the browser can offer to save the new password)
  } catch (err) { fail(err.message); }
});

// ------------------------------------------------------------------ welcome tour: what EvilEye is, then choose your ROCU region
const ob = { region: "", data: null, step: 1 };
const obEl = $("#onboard");
function showObStep(n) {
  ob.step = n;
  $("#ob-step1").hidden = n !== 1; $("#ob-step2").hidden = n !== 2;
  obEl.querySelector(".ob-card").classList.toggle("wide", n === 2);
  $("#ob-dot1").classList.toggle("on", n === 1); $("#ob-dot2").classList.toggle("on", n === 2);
  obEl.setAttribute("aria-labelledby", n === 1 ? "ob-title" : "ob-title2");
  if (n === 2) renderObRegions();
  setTimeout(() => (n === 1 ? $("#ob-next") : obEl.querySelector('.ob-tile[aria-checked="true"]') || $("#ob-go")).focus(), 30);
}
async function renderObRegions() {
  if (!ob.data) { try { ob.data = await api("/api/regions"); } catch (e) { ob.data = { regions: [], total: 0 }; } }
  const tiles = [{ name: "", label: "All UK", forces: ["The national picture: every region together"], count: ob.data.total }, ...ob.data.regions.map((r) => ({ ...r, label: r.name }))];
  $("#ob-regions").innerHTML = tiles.map((t) => `<button type="button" role="radio" class="ob-tile" aria-checked="${t.name === ob.region}" data-region="${esc(t.name)}" tabindex="${t.name === ob.region ? 0 : -1}">
      <span class="n">${esc(t.label)}${t.count ? `<em>${t.count} victim${t.count === 1 ? "" : "s"}</em>` : ""}</span><span class="f">${esc(t.forces.join(" · "))}</span></button>`).join("");
}
function selectObRegion(btn) {
  ob.region = btn.dataset.region;
  obEl.querySelectorAll(".ob-tile").forEach((b) => { const on = b === btn; b.setAttribute("aria-checked", String(on)); b.tabIndex = on ? 0 : -1; });
  btn.focus();
}
$("#ob-regions").addEventListener("click", (e) => { const b = e.target.closest(".ob-tile"); if (b) selectObRegion(b); });
$("#ob-regions").addEventListener("keydown", (e) => {          // arrow keys move through the options like a radio group
  const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
  if (!(e.key in keys)) return;
  const all = [...obEl.querySelectorAll(".ob-tile")], i = all.indexOf(document.activeElement);
  if (i < 0) return;
  e.preventDefault(); selectObRegion(all[(i + keys[e.key] + all.length) % all.length]);
});
function startOnboarding() {
  ob.region = region || "";
  obEl.hidden = false; document.body.style.overflow = "hidden";
  showObStep(1);
}
async function finishOnboarding(skipped) {
  try {
    if (!skipped && ob.region !== (ME.region || "")) {
      await api("/api/me/region", { method: "PUT", body: { region: ob.region } });
      region = ob.region; ME.region = ob.region; $("#region").value = region; applyRegionChange(true);
    }
    await api("/api/me/onboarded", { method: "POST", body: { skipped } }); ME.onboarded = true;
  } catch (e) { toast(e.message); }
  obEl.hidden = true; document.body.style.overflow = "";
  if (!skipped) showTab("overview", true);
}
$("#ob-next").addEventListener("click", () => showObStep(2));
$("#ob-back").addEventListener("click", () => showObStep(1));
$("#ob-go").addEventListener("click", () => finishOnboarding(false));
$("#ob-skip").addEventListener("click", () => finishOnboarding(true));
$("#ob-skip2").addEventListener("click", () => finishOnboarding(true));

// ------------------------------------------------------------------ account
$("#usermenu").addEventListener("click", () => {
  openModal(); $("#modal-back").hidden = true;
  $("#modal-title").textContent = ME.username;
  $("#modal-explain").textContent = `${ME.role === "admin" ? "Administrator" : "User"} · ${ME.database ? "activity is logged" : "database not connected"}`;
  $("#modal-body").innerHTML = ME.database ? `<h3 class="sub">Change password</h3>
    <form id="mypw" class="form-row"><input class="sr-only-input" type="text" name="username" value="${esc(ME.username)}" autocomplete="username" tabindex="-1" aria-hidden="true">
      <label class="field">Current password<input id="pw-cur" class="search" type="password" required autocomplete="current-password"></label>
      <label class="field">New password (10+ characters)<input id="pw-new" class="search" type="password" minlength="10" required autocomplete="new-password"></label>
      <label class="field">Repeat new password<input id="pw-rep" class="search" type="password" minlength="10" required autocomplete="new-password"></label>
      <button class="btn" type="submit">Change password</button></form><p id="mypw-msg" class="hint"></p>`
    : '<p class="hint">The user database is not connected, so accounts and passwords are unavailable. Set DATABASE_URL.</p>';
  $("#modal-body").insertAdjacentHTML("beforeend", '<p style="margin-top:14px"><button type="button" class="more" id="show-welcome">Show the welcome screen</button></p>');
  $("#show-welcome").addEventListener("click", () => { closeModal(); startOnboarding(); });
  const f = $("#mypw");
  if (f) f.addEventListener("submit", async (e) => {
    e.preventDefault();
    if ($("#pw-new").value !== $("#pw-rep").value) { $("#mypw-msg").textContent = "The new passwords do not match."; return; }
    try {
      await api("/api/me/password", { method: "POST", body: { current: $("#pw-cur").value, new: $("#pw-new").value } });
      $("#mypw-msg").textContent = "Password changed."; f.reset();
    } catch (err) { $("#mypw-msg").textContent = err.message; }
  });
});

// ------------------------------------------------------------------ search / filter wiring (with activity tracking)
const trackedSearch = (id, label, fn) => $(`#${id}`).addEventListener("input", debounce(() => {
  fn();
  const v = $(`#${id}`).value.trim();
  if (v) track("search", `${label}: ${v}`);
}, 400));
trackedSearch("vq", "victims", () => { vm.q = $("#vq").value.trim(); vm.page = 0; loadVictims(); });
trackedSearch("nq", "news", () => { nm.q = $("#nq").value.trim(); loadNews(); });
trackedSearch("gq", "gangs", loadGangs);
trackedSearch("aq", "alerts", loadAlerts);
$("#vgroup").addEventListener("change", () => { vm.group = $("#vgroup").value; vm.page = 0; loadVictims(); track("filter", `victims group: ${vm.group || "all"}`); });
$("#vsector").addEventListener("change", () => { vm.sector = $("#vsector").value; vm.page = 0; loadVictims(); track("filter", `victims sector: ${vm.sector || "all"}`); });
$("#vnew").addEventListener("change", () => { vm.newOnly = $("#vnew").checked; vm.page = 0; loadVictims(); track("filter", `victims new only: ${vm.newOnly}`); });
[["gsource", "gangs source", loadGangs], ["gsort", "gangs sort", loadGangs], ["asource", "alerts source", loadAlerts]].forEach(([id, label, fn]) =>
  $(`#${id}`).addEventListener("change", () => { fn(); track("filter", `${label}: ${$(`#${id}`).value || "all"}`); }));
document.querySelectorAll('input[name="glive"]').forEach((el) => el.addEventListener("change", () => { loadGangs(); track("filter", `gangs live only: ${el.value === "1"}`); }));
$("#vrefresh").addEventListener("click", loadVictims);
$("#grefresh").addEventListener("click", loadGangs);
$("#arefresh").addEventListener("click", loadAlerts);

// keyboard: "/" jumps to the search box of the current tab, Esc closes dialogs
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if (!drawer.hidden) closeDrawer(); else if (!modal.hidden) closeModal(); return; }
  if (!obEl.hidden) return;
  if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || /^(input|textarea|select)$/i.test(e.target.tagName)) return;
  const box = { victims: "#vq", news: "#nq", gangs: "#gq", alerts: "#aq", activity: "#act-q" }[activeTab()];
  if (box) { e.preventDefault(); $(box).focus(); }
});

// ------------------------------------------------------------------ keep-screen-on: the shield in the header is the switch
// Click the eye to keep the screen awake while this tab is visible; the eye turns green while it is on.
(function () {
  const btn = $("#awake"), fav = $("#favicon");
  let lock = null, want = false;
  try { want = localStorage.getItem("evileye-awake") === "1"; } catch (e) { /* storage unavailable */ }
  const store = (on) => { try { on ? localStorage.setItem("evileye-awake", "1") : localStorage.removeItem("evileye-awake"); } catch (e) { /* ignore */ } };
  function paint() {
    const on = want && !!lock;
    btn.setAttribute("aria-pressed", String(on));
    btn.setAttribute("aria-label", on ? "Screen is kept on: click to turn off" : "Keep screen on");
    if (fav) fav.href = on ? "/static/favicon-on.png" : "/static/favicon.png";
  }
  let failReason = "";
  async function acquire() {
    failReason = "";
    if (!navigator.wakeLock) { failReason = "browser does not support it"; want = false; store(false); paint(); toast("This browser cannot keep the screen awake."); return false; }
    try { lock = await navigator.wakeLock.request("screen"); lock.addEventListener("release", () => { lock = null; paint(); }); }
    catch (err) { lock = null; failReason = err.message || "denied by the browser"; toast(failReason); }
    paint(); return !!lock;
  }
  async function setAwake(on) {
    want = on; store(on);
    if (!on && lock) { await lock.release(); lock = null; }
    if (on) {
      if (await acquire()) { toast("Screen will stay on while this tab is visible"); track("screen_wake_on", "keep screen on"); }
      else track("screen_wake_failed", failReason);
    } else { paint(); toast("Screen wake off"); track("screen_wake_off", "screen wake released"); }
  }
  btn.addEventListener("click", () => setAwake(!(want && lock)));
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && want) acquire(); });
  if (want) acquire(); else paint();
})();

// home: the EvilEye text goes back to the Overview
$("#home").addEventListener("click", (e) => { e.preventDefault(); showTab("overview", true); window.scrollTo({ top: 0, behavior: "smooth" }); });

// ------------------------------------------------------------------ refresh policy
// External feeds are fetched by the server only when the page is opened (if stale) or Refresh is pressed.
async function waitForRefresh() {
  for (let i = 0; i < 40; i++) { const h = await loadHealth(); if (!h.refreshing) return; await new Promise((res) => setTimeout(res, 3000)); }
}
// Region lookups run on the server after an open or Refresh. While that one job runs we re-read the screen every
// 20s, then stop. No polling otherwise.
let tracking = false;
async function trackPlacing() {
  if (tracking) return;
  tracking = true;
  try {
    await new Promise((res) => setTimeout(res, 2000));
    for (let i = 0; i < 60; i++) {
      const h = await loadHealth();
      if (["overview", "victims"].includes(activeTab())) refreshCurrent();
      loadTicker();
      if (!h.placing) break;
      await new Promise((res) => setTimeout(res, 20000));
    }
  } finally { tracking = false; }
}
$("#refresh-now").addEventListener("click", async () => {
  const btn = $("#refresh-now");
  btn.disabled = true; btn.classList.add("spinning");
  try { await api("/api/refresh", { method: "POST" }); } catch (err) { toast(err.message || "Refresh failed"); }
  await waitForRefresh();
  btn.disabled = false; btn.classList.remove("spinning");
  refreshCurrent(); loadTicker(); trackPlacing();
});

async function init() {
  try { ME = await api("/api/me"); } catch (e) { return; }
  if (ME.must_change) { showPwReq(); return; }        // nothing else works until the password has been changed
  const h = readHash();
  region = ME.region || "";
  if (h.region && ME.regions.includes(h.region)) region = h.region;        // a shared link filters the view without changing your saved region
  buildRegionSelect();
  if (!ME.onboarded) startOnboarding();
  if (ME.role === "admin" && ME.database) document.querySelectorAll(".admin-only").forEach((el) => { el.hidden = false; });
  $("#usermenu").title = `Account: ${ME.username}`;
  // record this visit first so "new since your last visit" is measured against the previous one
  let refreshing = false;
  try { const o = await api("/api/open", { method: "POST" }); ME.prev_visit = o.prev_visit; refreshing = o.refreshing; } catch (e) { /* offline: show cached data */ }
  if (h.tab && document.querySelector(`.tab[data-tab="${h.tab}"]`) && h.tab !== "overview") { showTab(h.tab, false); track("tab_view", `${h.tab} (shared link)`); } else { updateFilterBar(); syncHash(); loadOverview(); }
  loadHealth(); loadTicker();
  if (refreshing) await waitForRefresh();
  await loadHealth();
  refreshCurrent(); loadTicker(); trackPlacing();
}
init();
