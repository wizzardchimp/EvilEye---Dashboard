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

// ------------------------------------------------------------------ theme: dark (default) or inverted light, remembered on this browser and on the account
const root = document.documentElement;
function setTheme(t) {
  if (t === "light") root.dataset.theme = "light"; else delete root.dataset.theme;
  try { localStorage.setItem("evileye-theme", t); } catch (e) { /* storage unavailable */ }
  const b = $("#theme");
  if (b) { b.setAttribute("aria-pressed", String(t === "light")); b.title = t === "light" ? "Switch back to the dark theme" : "Invert colour scheme (light theme)"; }
}
const curTheme = () => (root.dataset.theme === "light" ? "light" : "dark");
function saveTheme(t) {            // kept on the account so it follows the person to other devices (this browser has it already)
  if (typeof ME !== "undefined" && ME) { ME.theme = t; api("/api/me/theme", { method: "PUT", body: { theme: t } }).catch(() => { /* still remembered on this device */ }); }
}
$("#theme").addEventListener("click", () => { const t = curTheme() === "light" ? "dark" : "light"; setTheme(t); saveTheme(t); });
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
const TABS = { overview: loadOverview, victims: loadVictims, news: loadNews, gangs: loadGangs, alerts: loadAlerts, users: loadUsers, activity: loadActivity, settings: () => loadSettings() };
const REGION_TABS = ["overview", "victims", "gangs"];
const activeTab = () => document.querySelector(".tab.active").dataset.tab;
const tabsEl = $("#tabs");
function tabFade() {       // only on screens too narrow for every tab: fade the edge that has more tabs beyond it
  const max = tabsEl.scrollWidth - tabsEl.clientWidth;
  tabsEl.classList.toggle("fade-l", max > 2 && tabsEl.scrollLeft > 2);
  tabsEl.classList.toggle("fade-r", max > 2 && tabsEl.scrollLeft < max - 2);
}
tabsEl.addEventListener("scroll", tabFade, { passive: true });
window.addEventListener("resize", tabFade);
function showTab(name, fromClick) {
  const btn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (!btn || btn.hidden) return;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
  if (tabsEl.scrollWidth > tabsEl.clientWidth) btn.scrollIntoView({ inline: "center", block: "nearest" });
  tabFade();
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (fromClick) track("tab_view", name);
  updateFilterBar(); syncHash(); refreshCurrent();
}
document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => showTab(btn.dataset.tab, true)));
function refreshCurrent() { (TABS[activeTab()] || loadOverview)(); }
function updateFilterBar() {
  const show = region && REGION_TABS.includes(activeTab());
  document.body.classList.toggle("has-region", !!region);
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
  TK.reg.n = TK_FIRST; updateFilterBar(); syncHash(); loadTicker(); refreshCurrent();
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

// ------------------------------------------------------------------ flags (false positives) and allocations ("I am working on this")
const ICON_FLAG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 21V4"/><path d="M5 4h13l-2.5 4.5L18 13H5"/></svg>';
const ICON_PERSON = '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6.5 8-6.5s8 2.5 8 6.5"/></svg>';
const flagWhat = (f) => (f.kind === "not_uk" ? "not in the UK" : `not in ${f.region}`);
const flagText = (f) => `Flagged as a false positive by ${f.by} (${flagWhat(f)})${f.note ? `: "${f.note}"` : ""}, ${fmtTime(f.at)}`;
const allocText = (a) => `Allocated to ${a.initials}, set by ${a.by}, ${fmtTime(a.at)}`;
// ---- markers: Task (built in) and custom ones. A marker is live until it is completed.
const safeColour = (c) => (/^#[0-9a-f]{6}$/i.test(c || "") ? c : "#94a2b7");      // only ever a plain six-digit colour reaches a style attribute
const MK_STATUS = { allocated: "Allocated", started: "In progress", completed: "Completed" };
const ICON_PLAY = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor" aria-hidden="true"><path d="M7 4.5v15l12-7.5z"/></svg>';
const ICON_TICK = '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="4 12 10 18 20 6"/></svg>';
const markerText = (m) => (m.builtin ? allocText({ initials: m.initials, by: m.by, at: m.allocated_at }) : `${m.name}: allocated to ${m.initials}, set by ${m.by}, ${fmtTime(m.allocated_at)}`)
  + (m.started_at ? `, started ${fmtTime(m.started_at)}` : "") + (m.completed_at ? `, completed ${fmtTime(m.completed_at)}` : "") + ` (${MK_STATUS[m.status].toLowerCase()})`;
const mkShort = (n) => (n.length > 12 ? n.slice(0, 11) + "…" : n);        // the same shortening on every badge and in the preview
const mkBadge = (m) => `<span class="badge${m.builtin ? " alloc" : ""} mk ${m.status}" style="--mk:${safeColour(m.colour)}" title="${esc(markerText(m))}">${m.status === "completed" ? ICON_TICK : m.status === "started" ? ICON_PLAY : ICON_PERSON}${m.builtin ? "" : esc(mkShort(m.name)) + " "}${esc(m.initials)}</span>`;
const liveMarkers = (v) => (v.markers || []).filter((m) => m.status !== "completed");
const marks = (v, all) => [
  v.flag && v.flag.applied ? `<span class="badge fp" title="${esc(flagText(v.flag))}">false positive</span>` : "",
  ...(all ? v.markers || [] : liveMarkers(v)).map(mkBadge),
].filter(Boolean).join(" ");
const flagBtn = (ref, v) => (ME && ME.database
  ? `<button type="button" class="flagbtn${liveMarkers(v).length || (v.flag && v.flag.applied) ? " on" : ""}" data-flag="${ref}" aria-label="Flag or allocate ${esc(v.post_title || v.title)}" title="Flag or allocate">${ICON_FLAG}</button>` : "");
let MKTYPES = [];
async function loadMarkerTypes() { try { MKTYPES = (await api("/api/marker-types")).types; } catch (e) { /* the dialog falls back to Task only */ } }
let tkItems = { uk: [], reg: [] }, drawerVictim = null;            // what each flag button refers to
const refVictim = (ref) => { const [src, a, b] = ref.split(":"); return src === "tk" ? tkItems[a][+b] : src === "vt" ? lastVictims[+a] : drawerVictim; };

// ------------------------------------------------------------------ ticker (reads the server's cache; no external calls)
let tickerSeen = new Set();
const TK_FIRST = 5, TK_STEP = 25;
const TK = { uk: { n: TK_FIRST, total: 0 }, reg: { n: TK_FIRST, total: 0 } };      // how many each list shows / holds
const sectorShort = (s) => (s && s !== "Unclassified" ? s : "");
// the victim's own website as an https link, or "" when the source gave none (or something that is not a web address)
function siteHref(site) {
  const s = (site || "").trim().replace(/^https?:\/\//i, "");
  return isDomain(s) ? `https://${s}` : "";
}
function tickerItem(x, showRegion, fresh, list, i) {
  const meta = [`<span class="badge grp">${esc(x.group)}</span>`, esc(sectorShort(x.sector)), ago(x.seen)].filter(Boolean).join(" · ");
  const reg = showRegion ? `<span class="badge ${x.rocu === "Unplaced" ? "src" : "uk"}">${esc(x.rocu === "Unplaced" ? "region unknown" : x.rocu)}</span>` : "";
  const name = `<span class="tk-name">${esc(x.title)}</span>`;          // opens the breach summary; the company's site is linked from there
  const area = x.town && placed(x) ? `<span class="tk-area">${esc(x.town)}</span>` : "";
  const mk = marks(x), fb = flagBtn(`tk:${list}:${i}`, x);
  return `<li class="tk-open${fresh ? " fresh" : ""}" data-ti="${list}:${i}" tabindex="0"><div class="tk-main"><div class="tk-title">${name} ${reg}${x.new ? ' <span class="badge new">NEW</span>' : ""}${mk ? " " + mk : ""}</div><div class="meta">${meta}</div></div>${area || fb ? `<div class="tk-side">${area}${fb}</div>` : ""}</li>`;
}
function tickerFoot(which) {
  const s = TK[which], foot = $("#tkf-" + which), shown = Math.min(s.n, s.total), expanded = s.n > TK_FIRST;
  foot.hidden = s.total <= TK_FIRST;
  foot.querySelector(".meta").textContent = `Showing ${shown} of ${s.total.toLocaleString("en-GB")}`;
  const more = foot.querySelector('[data-act="more"]');
  more.hidden = shown >= s.total;
  more.textContent = expanded ? `Show ${Math.min(TK_STEP, s.total - shown)} more` : "View more";
  foot.querySelector('[data-act="less"]').hidden = !expanded;
  $("#tk-" + which).classList.toggle("expanded", expanded);
}
function setList(id, html) { const el = $(id), top = el.scrollTop; el.innerHTML = html; el.scrollTop = top; }      // keep the scroll position
async function loadTicker(expanding = false) {
  try {
    const d = await api(`/api/ticker?uk=${TK.uk.n}&reg=${TK.reg.n}`);
    // flash only genuinely new entries among the first few, not rows revealed by "View more"
    const fresh = (x, i) => !expanding && i < TK_FIRST && tickerSeen.size && !tickerSeen.has(x.title);
    TK.uk.total = d.uk_total; TK.reg.total = d.regional_total; tkItems = { uk: d.uk, reg: d.regional };
    setList("#tk-uk", d.uk.length ? d.uk.map((x, i) => tickerItem(x, true, fresh(x, i), "uk", i)).join("") : '<li class="ldr">no data yet — still syncing</li>');
    $("#tk-region").textContent = d.region || "your region";
    setList("#tk-reg", !d.region
      ? '<li class="ldr">Choose a region in the header to see its latest incidents here.</li>'
      : d.regional.length ? d.regional.map((x, i) => tickerItem(x, false, fresh(x, i), "reg", i)).join("") : `<li class="ldr">No placed incidents in ${esc(d.region)} yet.</li>`);
    tickerSeen = new Set([...d.uk, ...d.regional].map((x) => x.title));
    tickerFoot("uk"); tickerFoot("reg");
    $("#tk-updated").textContent = d.updated ? `data ${ago(d.updated)}` : "";
  } catch (e) { /* keep what is on screen */ }
}
async function openBreach(ref) {
  const [list, i] = ref.split(":"), x = tkItems[list] && tkItems[list][+i];
  if (!x) return;
  try { const v = await fetchVictim(x.title); if (v) openVictim(v); else toast("Could not find that breach any more."); }
  catch (e) { toast("Could not load the breach details: " + e.message); }
}
document.querySelector(".ticker").addEventListener("keydown", (e) => {
  const li = e.target.closest("li[data-ti]");
  if (li && e.target === li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openBreach(li.dataset.ti); }
});
document.querySelector(".ticker").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-ti]");
  if (li && !e.target.closest("a, button")) { openBreach(li.dataset.ti); return; }
  const b = e.target.closest("button[data-tk]");
  if (!b) return;
  const s = TK[b.dataset.tk];
  s.n = b.dataset.act === "less" ? TK_FIRST : Math.min(s.n <= TK_FIRST ? TK_STEP : s.n + TK_STEP, 1500);
  loadTicker(true);
});
// re-read the server cache each minute while the tab is visible, so a refresh done by anyone shows up
setInterval(() => { if (document.visibilityState === "visible" && activeTab() === "overview") { loadTicker(); loadLive(); } }, 60000);

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

async function loadOverview() {
  loadLive();
  const holder = $("#stat-cards");
  const [y, g, s, rg] = ["chart-year", "chart-gangs", "chart-sectors", "chart-regions"].map((i) => $(`#${i}`));
  if (!holder.children.length) { y.innerHTML = g.innerHTML = s.innerHTML = sk(6); }
  loadNewsTop();
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
        <span class="rs">${(100 * r.count / d.uk_all).toFixed(1)}%<span class="rs-of"> of UK</span></span>
        <span class="rbar"><i style="width:${Math.max(3, (r.count / max) * 100)}%"></i></span>
      </button>`).join("") || '<div class="ldr">no data yet</div>';
    $("#region-clear").hidden = !region;
    balanceStrip();
    const p = d.placement, lk = p.lookup || {};
    let note = `<strong>${p.placed}</strong> of ${p.total} victims placed to a UK region (${p.from_text} from place names, ${p.from_postcode} postcodes, ${p.from_companies_house} Companies House, ${p.from_manual} set manually). ` +
      `${p.overseas} look overseas / not UK, ${p.unplaced} still unplaced.`;
    if (p.flagged) note += ` ${p.flagged} flagged as false positive${p.flagged === 1 ? "" : "s"} by users.`;
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
const vm = { q: "", group: "", sector: "", mark: "", sort: "seen", dir: "desc", page: 0, newOnly: false };
let lastVictims = [], facetsFilled = false;
function victimParams(extra = {}) {
  const p = rq({ sort: vm.sort, dir: vm.dir, ...extra });
  if (vm.q) p.set("q", vm.q);
  if (vm.group) p.set("group", vm.group);
  if (vm.sector) p.set("sector", vm.sector);
  if (vm.mark) p.set("mark", vm.mark);
  if (vm.newOnly) p.set("new", "1");
  return p;
}
const regionCell = (v) => v.rocu === "Unplaced" ? '<span class="badge src">region unknown</span>'
  : v.rocu === "Overseas / not UK" ? '<span class="badge off">Overseas / not UK</span>'
  : `<span class="badge uk">${esc(v.rocu)}</span><div class="meta">${esc(v.force_name)}${v.region_source === "manual" ? " · set manually" : ""}</div>`;
async function loadVictims() {
  const tbody = $("#victim-rows");
  if (!tbody.children.length || tbody.querySelector(".ldr")) tbody.innerHTML = `<tr><td colspan="6">${sk(6)}</td></tr>`;
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
    $("#vmark").hidden = !ME.database;
    $("#vcount").textContent = `${d.total} ${vm.mark === "flagged" ? "false positives" : vm.mark ? "allocated victims" : "UK victims"}${region ? ` in ${region}` : ""}`;
    tbody.innerHTML = d.items.length ? d.items.map((v, i) => `
        <tr class="clickable" data-i="${i}" tabindex="0">
          <td><strong>${esc(v.post_title)}</strong>${townLabel(v)}${newBadge(v)}${marks(v) ? " " + marks(v) : ""}
            ${isDomain(v.website) ? `<div class="meta">${esc(v.website)}</div>` : ""}</td>
          <td><span class="badge grp">${esc(v.group)}</span></td>
          <td>${esc(v.activity || "—")}</td>
          <td>${fmtDate(v.seen || v.discovered)}</td>
          <td>${regionCell(v)}</td>
          <td class="r">${flagBtn("vt:" + i, v)}</td>
        </tr>`).join("") : '<tr><td colspan="6" class="ldr">no matches</td></tr>';
    $("#vpager").innerHTML = d.total ? `<span>Showing ${vm.page * PAGE + 1}–${vm.page * PAGE + d.items.length} of ${d.total}</span>
      <span class="btns"><button class="more" type="button" id="vprev" ${vm.page === 0 ? "disabled" : ""}>Previous</button>
      <span style="align-self:center">Page ${vm.page + 1} of ${pages}</span>
      <button class="more" type="button" id="vnext" ${vm.page + 1 >= pages ? "disabled" : ""}>Next</button></span>` : "";
    const go = (n) => () => { vm.page += n; loadVictims(); window.scrollTo({ top: $("#tab-victims").offsetTop - 60, behavior: "smooth" }); };
    $("#vprev")?.addEventListener("click", go(-1)); $("#vnext")?.addEventListener("click", go(1));
  } catch (e) { tbody.innerHTML = `<tr><td colspan="6" class="ldr">failed to load: ${esc(e.message)}</td></tr>`; }
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
$("#victim-rows").addEventListener("click", (e) => { if (!e.target.closest("a, button")) openRow(e.target.closest("tr.clickable")); });
$("#victim-rows").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.target.closest("button")) openRow(e.target.closest("tr.clickable")); });

// ------------------------------------------------------------------ victim details drawer
const drawer = $("#drawer"), backdrop = $("#drawer-backdrop");
function closeDrawer() { drawer.hidden = true; backdrop.hidden = true; }
const svgI = (d) => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const SEC_ICON = {
  details: svgI('<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/>'),
  company: svgI('<path d="M4 21V5a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v16"/><path d="M14 10h5a1 1 0 0 1 1 1v10"/><path d="M2 21h20"/><path d="M8 8h2M8 12h2M8 16h2"/>'),
  gang: svgI('<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18 14.3c2.3.7 3.5 2.6 3.5 5.7"/>'),
  sources: svgI('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
  region: svgI('<path d="M12 21s7-6.2 7-11.5A7 7 0 0 0 5 9.5C5 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>'),
  description: svgI('<path d="M4 6h16M4 10h16M4 14h10M4 18h13"/>'),
  admin: svgI('<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>'),
};
// One clearly bordered card per topic: an icon and a title on a tinted band, then the content.
const sect = (key, title, body, cls = "") => `<section class="dsec ${cls}" data-sec="${key}" aria-labelledby="ds-${key}">
    <header class="dsec-h"><span class="dsec-i">${SEC_ICON[key]}</span><h3 id="ds-${key}">${esc(title)}</h3></header>
    <div class="dsec-b">${body}</div></section>`;
// A label above its value; "wide" facts take the whole row.
const fact = (label, value, wide = false) => `<div class="fact${wide ? " wide" : ""}"><dt>${esc(label)}</dt><dd>${value}</dd></div>`;
const fmtPub = (iso) => (/T00:00:00/.test(iso || "") ? fmtDate(iso) : fmtTime(iso));      // posts often carry a date with no time
const srcRow = (badge, body, meta) => `<li><span class="badge src">${esc(badge)}</span><div class="br-sl">${body}${meta ? `<div class="meta">${meta}</div>` : ""}</div></li>`;
const outLink = (url, text) => `<a href="${esc(safeHref(url))}" target="_blank" rel="noopener noreferrer">${esc(text)} ↗</a>`;
function gangHtml(ctx) {
  if (!ctx) return '<div class="ldr">Loading gang details…</div>';
  const g = ctx.gang;
  if (!g) return '<div class="ldr">Could not load the gang details.</div>';
  const facts = [`${g.uk_victims} UK victim${g.uk_victims === 1 ? "" : "s"} on record here`, g.sites ? `${g.sites} known leak site${g.sites === 1 ? "" : "s"}${g.online ? ` (${g.online} online)` : ""}` : "", g.added ? `tracked since ${fmtDate(g.added)}` : ""].filter(Boolean).join(" · ");
  return `<div class="br-gang"><div class="br-gh"><span class="badge grp">${esc(g.name)}</span>${g.altname ? ` <span class="meta">also known as ${esc(g.altname)}</span>` : ""}</div>
    <div class="meta">${esc(facts)}</div>
    <p class="br-gd${g.description ? "" : " meta"}">${esc(g.description || "No description on file for this group.")}</p>
    <button type="button" class="more" data-gang-go="${esc(g.name)}">Show in Gang Catalog</button></div>`;
}
// A "Map" button after the registered office: it opens the address in the device's own maps app. Apple Maps on an iPhone, iPad or Mac;
// Google Maps everywhere else (on Android, and wherever the app is installed, that opens the Google Maps app, otherwise its web page).
const APPLE_MAPS = /iPhone|iPad|iPod|Macintosh/.test(navigator.userAgent);          // iPadOS and macOS both say "Macintosh"
const mapsHref = (addr) => (APPLE_MAPS ? `https://maps.apple.com/?q=${encodeURIComponent(addr)}` : `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(addr)}`);
const mapBtn = (addr) => `<a class="mapbtn" href="${esc(mapsHref(addr))}" target="_blank" rel="noopener noreferrer" aria-label="Open this address in Maps" title="${APPLE_MAPS ? "Open in Apple Maps" : "Open in Google Maps"}">${SEC_ICON.region}Map</a>`;
const CO_TYPE = { ltd: "Private limited company", plc: "Public limited company", llp: "Limited liability partnership", "private-unlimited": "Private unlimited company", "private-limited-guarant-nsc": "Private company limited by guarantee" };
const humanize = (s) => String(s || "").replace(/[-_]/g, " ").replace(/^./, (c) => c.toUpperCase());
function companyHtml(c) {
  if (!c) return '<div class="ldr">Looking up Companies House…</div>';
  const search = c.search_url ? ` ${outLink(c.search_url, "Search Companies House")}` : "";
  if (c.status !== "ok") {
    const msg = { no_match: "No company on the Companies House register matched this breach's name.", disabled: "Companies House lookup is not set up on this site (it needs a Companies House API key).",
      auth_error: "Companies House rejected this site's API key, so company details cannot be shown.", rate_limited: "Companies House is busy. Open this breach again in a minute." }[c.status] || "Could not load the Companies House details.";
    return `<div class="ldr">${esc(msg)}${search}</div>`;
  }
  const co = c.company, offs = c.officers;
  const dirs = offs === null ? '<span class="meta">Not available from Companies House.</span>'
    : !offs.length ? '<span class="meta">No current directors listed.</span>'
    : `<ul class="br-dir">${offs.slice(0, 12).map((o) => `<li>${esc(o.name)}${o.role !== "director" ? ` <span class="meta">${esc(humanize(o.role).toLowerCase())}</span>` : ""}${o.appointed ? ` <span class="meta">appointed ${esc(fmtDate(o.appointed))}</span>` : ""}</li>`).join("")}${offs.length > 12 ? `<li class="meta">and ${offs.length - 12} more, on the Companies House officers page</li>` : ""}</ul>`;
  return `<dl class="facts">
      ${fact("Registered as", `<strong>${esc(co.name)}</strong> <span class="meta">No. ${esc(co.number)}</span>`)}
      ${fact("Status", `${esc(humanize(co.status))}${co.type ? ` · ${esc(CO_TYPE[co.type] || humanize(co.type))}` : ""}${co.created ? ` <span class="meta">incorporated ${esc(fmtDate(co.created))}</span>` : ""}`)}
      ${fact("Registered office", co.address ? `${esc(co.address)} ${mapBtn(co.address)}` : "Not given", true)}
      ${fact("Directors", dirs, true)}</dl>
    <div class="br-cofoot">${outLink(c.url, "View on Companies House")} · ${outLink(c.officers_url, "All officers")}
      <div class="meta">According to Companies House, matched to this breach by ${c.matched_by === "name+domain" ? "company name and website" : "company name"}, checked ${esc(ago(c.fetched))}. Not the right company?${search}</div></div>`;
}
async function loadCompany(v) {
  let c;
  try { c = await api(`/api/victims/company?title=${encodeURIComponent(v.post_title)}`); } catch (e) { c = { status: "error" }; }
  if (drawer.hidden || !drawerVictim || drawerVictim.post_title !== v.post_title) return;
  if ($("#br-co")) $("#br-co").innerHTML = companyHtml(c);
}
const MENTION_KIND = { press: "Press mention", "leak-post": "Listed on RansomLook" };
function sourcesHtml(v, ctx) {
  const g = ctx && ctx.gang, rows = [], list = (ctx && ctx.mentions) || [];
  rows.push(srcRow("ransomware.live", `Victim tracker this dashboard reads the breach from${g && g.profile_url ? `<br>${outLink(g.profile_url, g.name + " on ransomware.live")}` : ""}`, `First picked up ${esc(fmtTime(v.seen || v.discovered))}`));
  if (g && g.ransomlook_url && !list.some((m) => m.kind === "leak-post")) rows.push(srcRow("RansomLook", `Also tracks this gang<br>${outLink(g.ransomlook_url, g.name + " on RansomLook")}`, ""));
  if (v.post_url) rows.push(srcRow("Leak site", `The gang's own post (the original)<button type="button" class="copy" data-copy="${esc(v.post_url)}" data-gang="${esc(v.group)}" data-log="${esc("Victim " + v.post_title + " (" + v.group + "): " + v.post_url)}"><span class="addr">${esc(v.post_url)}</span><span class="cpy">copy</span></button>`, `Posted ${esc(fmtPub(v.discovered))} · copy only, never opened from here`));
  if (!ctx) rows.push('<li class="ldr">Looking for other mentions…</li>');
  else list.forEach((m) => rows.push(srcRow(m.source || "Feed", outLink(m.url, m.title), `${esc(MENTION_KIND[m.kind] || "News or alert")} · ${esc(fmtDate(m.date))}`)));
  if (ctx && ctx.failed) rows.push('<li class="ldr">Could not load other mentions. Try opening this breach again.</li>');
  else if (ctx && !list.length) rows.push('<li class="ldr">No other mentions of this breach in the dashboard\'s news and alert feeds.</li>');
  return `<ul class="br-src">${rows.join("")}</ul>`;
}
async function loadBreachExtras(v) {
  let ctx;
  try { ctx = await api(`/api/victims/context?title=${encodeURIComponent(v.post_title)}`); } catch (e) { ctx = { gang: null, mentions: [], failed: true }; }
  if (drawer.hidden || !drawerVictim || drawerVictim.post_title !== v.post_title) return;      // the person has moved on
  if ($("#br-gang")) $("#br-gang").innerHTML = gangHtml(ctx);
  if ($("#br-src")) $("#br-src").innerHTML = sourcesHtml(v, ctx);
}
const BASIS = { text: "a place name in its description", postcode: "a postcode in its description", "companies-house": "its Companies House registered office",
  manual: "set manually by an admin" };
function openVictim(v, quiet = false) {
  if (!quiet) track("victim_view", v.post_title);
  const same = quiet && !drawer.hidden && drawerVictim && drawerVictim.post_title === v.post_title;      // a refresh of the breach already on screen
  const prev = same ? { scroll: $("#dr-body").scrollTop, co: $("#br-co")?.innerHTML, gang: $("#br-gang")?.innerHTML, src: $("#br-src")?.innerHTML } : {};
  drawerVictim = v;
  $("#dr-title").innerHTML = `${esc(v.post_title)}${townLabel(v)}`;
  const basis = v.region_source === "flag" ? v.why
    : v.region_source === "text-overseas" ? "Marked overseas because its description says it is based abroad."
    : v.region_source ? `Placed using ${BASIS[v.region_source] || v.region_source}.`
    : (v.why || "Not placed yet: no postcode, place name or Companies House match.");
  const canEdit = ME.role === "admin" && ME.database;
  const where = `${v.rocu === "Unplaced" ? "Unknown" : esc(v.rocu)}${v.force_name ? ` · ${esc(v.force_name)}` : ""}${v.town && placed(v) && v.town !== v.force_name ? ` · ${esc(v.town)}` : ""}`;
  $("#dr-body").innerHTML = `
    <div class="dsum">
      <div class="dsum-b">${newBadge(v).trim()} <span class="badge grp">${esc(v.group)}</span> <span class="badge src">${esc(v.activity)}</span> ${regionCell(v).split("<div")[0]}</div>
      ${ME.database ? `<div class="dr-actions">${marks(v)}<button class="more" type="button" data-flag="dr">${ICON_FLAG} Flag or allocate</button></div>` : ""}
    </div>
    ${sect("details", "Breach details", `<dl class="facts">
      ${fact("Original site", siteHref(v.website) ? outLink(siteHref(v.website), siteHref(v.website).slice(8)) : '<span class="meta">Not given in the post</span>')}
      ${fact("Published", `${esc(fmtPub(v.discovered))} <span class="meta">the date on the leak post</span>`)}
      ${fact("First seen", `${esc(fmtTime(v.seen || v.discovered))} <span class="meta">${esc(ago(v.seen || v.discovered))}, when the dashboard's feeds picked it up</span>`)}
      ${fact("Data", v.data ? `${esc(v.data)} <span class="meta">${v.data_basis === "stated" ? "as stated by the gang" : "quoted in the post text"}</span>` : '<span class="meta">Not stated in the post</span>')}
      ${v.ransom ? fact("Ransom", `${esc(v.ransom)} <span class="meta">as posted</span>`) : ""}
      ${v.reposts ? fact("Re-posted", `${v.reposts} more time${v.reposts === 1 ? "" : "s"} <span class="meta">by the same group</span>`) : ""}
    </dl>`)}
    ${sect("company", "Company (Companies House)", `<div id="br-co">${prev.co ?? companyHtml(null)}</div>`)}
    ${sect("gang", "Gang responsible", `<div id="br-gang">${prev.gang ?? gangHtml(null)}</div>`)}
    ${sect("sources", "Where this was sourced from", `<div id="br-src">${prev.src ?? sourcesHtml(v, null)}</div>`)}
    ${sect("region", "Region", `<dl class="facts">
      ${fact("Region", where)}
      ${v.registered_name ? fact("Registered as", esc(v.registered_name)) : ""}
      ${fact("Basis", `${esc(basis)}${v.region_source && v.region_source !== "flag" && v.why ? ` ${esc(v.why)}` : ""}${v.override_note ? ` Note: ${esc(v.override_note)}` : ""}`, true)}
    </dl>`)}
    ${v.description ? sect("description", "Description (from ransomware.live)", `<div class="desc">${esc(v.description)}</div>`) : ""}
    ${canEdit ? sect("admin", "Admin: set region", `<form id="plform" class="form-row">
        <label class="field">Region<select id="pl-rocu" class="search">${ME.regions.filter((r) => r !== "Unplaced").map((r) => `<option ${r === v.rocu ? "selected" : ""}>${esc(r)}</option>`).join("")}</select></label>
        <label class="field">Town / district<input id="pl-town" class="search" maxlength="60" value="${esc(v.manual ? v.town : "")}" placeholder="optional"></label>
        <label class="field">Note<input id="pl-note" class="search" maxlength="200" value="${esc(v.override_note || "")}" placeholder="optional"></label>
        <div class="fr-btns"><button class="btn" type="submit">Save</button>
        ${v.manual ? '<button class="more danger" type="button" id="pl-clear">Remove manual placement</button>' : ""}</div>
      </form><p id="pl-msg" class="hint"></p>`, "admin") : ""}`;
  drawer.hidden = false; backdrop.hidden = false;
  $("#dr-body").scrollTop = prev.scroll || 0;      // after showing it (a hidden box ignores this): a different breach starts at the top, a refresh after a flag keeps its place and its loaded sections
  loadBreachExtras(v); loadCompany(v);
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
const linkContext = (a) => a.closest(".ticker") ? "latest incidents" : a.closest(".drawer") ? "victim details" : a.closest(".news-item") ? "news" : a.closest("#tab-alerts") ? "alerts"
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

// ------------------------------------------------------------------ the flag / allocate dialog
const fdlg = $("#fdlg");
let fdVictim = null;
const closeFlag = () => { fdlg.hidden = true; fdVictim = null; };
const suggestInitials = (name) => { const p = (name || "").split(/[^A-Za-z]+/).filter(Boolean); return (p.length > 1 ? p.slice(0, 3).map((x) => x[0]).join("") : (p[0] || "").slice(0, 2)).toUpperCase(); };
async function fetchVictim(title) {                          // not region-filtered: a flagged victim may have left the region
  const d = await api(`/api/victims?${new URLSearchParams({ title, limit: "1" })}`);
  return d.items.find((x) => x.post_title === title) || null;
}
async function afterMark(title) {
  loadTicker(); loadLive(); refreshCurrent();
  if (!drawer.hidden && drawerVictim && drawerVictim.post_title === title) {
    try { const nv = await fetchVictim(title); if (nv) openVictim(nv, true); } catch (e) { /* leave the drawer as it is */ }
  }
}
async function undoFlag(title, quiet) {
  await api(`/api/victims/flag?title=${encodeURIComponent(title)}`, { method: "DELETE" });
  if (!quiet) toast("Flag removed"); afterMark(title);
}
const dtLocal = (iso) => { if (!iso) return ""; const d = new Date(iso), p = (n) => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; };
const dtIso = (v) => (v ? new Date(v).toISOString() : null);           // a datetime-local field is the browser's own clock
const canEditMarker = (m) => ME.role === "admin" || ME.username === m.by || (ME.initials || "") === m.initials;
function markerPanel(v, t, m) {              // the dates of one marker, and what can be done to it
  const can = canEditMarker(m), none = (x) => `<span class="meta">${x}</span>`;
  const acts = !can ? `<p class="hint" style="margin:8px 0 0">Only ${esc(m.initials)} (${esc(m.by)}) or an admin can change this.</p>`
    : `<div class="mk-actions">${m.status === "allocated" ? '<button type="button" class="btn" data-mk="start">Start now</button><button type="button" class="more" data-mk="complete">Complete now</button>'
      : m.status === "started" ? '<button type="button" class="btn" data-mk="complete">Complete now</button>' : '<button type="button" class="more" data-mk="reopen">Reopen</button>'}
      <button type="button" class="more" data-mk="edit" aria-expanded="false">Edit dates and initials</button>${t.builtin ? "" : '<button type="button" class="more danger" data-mk="remove">Remove</button>'}</div>
    <form class="mk-edit" hidden><div class="mk-grid">
      <label class="field">Initials<input class="search mk-e-ini" maxlength="4" value="${esc(m.initials)}" autocapitalize="characters" autocomplete="off" spellcheck="false"></label>
      <label class="field">Allocated<input type="datetime-local" class="search mk-e-alloc" required value="${dtLocal(m.allocated_at)}" max="${dtLocal(new Date().toISOString())}"></label>
      <label class="field">Started<input type="datetime-local" class="search mk-e-start" value="${dtLocal(m.started_at)}" max="${dtLocal(new Date().toISOString())}"></label>
      <label class="field">Completed<input type="datetime-local" class="search mk-e-done" value="${dtLocal(m.completed_at)}" max="${dtLocal(new Date().toISOString())}"></label></div>
      <p class="hint" style="margin:6px 0 10px">Leave Started or Completed empty if that has not happened yet.</p>
      <div class="fd-row"><button type="submit" class="btn">Save changes</button><button type="button" class="more" data-mk="edit-cancel">Cancel</button></div></form>`;
  return `<div class="mkp" data-m="${m.id}" data-t="${t.id}"><dl class="mk-dates">
    <div><dt>Allocated</dt><dd>${esc(m.initials)} <span class="meta">set by ${esc(m.by)}</span><br><span class="meta">${esc(fmtTime(m.allocated_at))}</span></dd></div>
    <div><dt>Started</dt><dd>${m.started_at ? esc(fmtTime(m.started_at)) : none("Not started")}</dd></div>
    <div><dt>Completed</dt><dd>${m.completed_at ? esc(fmtTime(m.completed_at)) : none("Not completed")}</dd></div></dl>${acts}</div>`;
}
function markerBlock(v, t, m) {              // one custom marker: allocate it, or see and move it along
  const initial = ME.initials || suggestInitials(ME.username), col = safeColour(t.colour);
  const form = (label) => `<div class="fd-row mk-new"><label class="field">Initials<input class="search fd-init mk-ini" maxlength="4" autocapitalize="characters" autocomplete="off" spellcheck="false" value="${esc(initial)}"></label>
    <label class="field">Allocated on (optional)<input type="datetime-local" class="search mk-when" max="${dtLocal(new Date().toISOString())}"></label>
    <button type="button" class="btn" data-mk="put" data-t="${t.id}">${label}</button></div>`;
  const state = m ? ` <span class="badge mk ${m.status}" style="--mk:${col}">${esc(MK_STATUS[m.status])} · ${esc(m.initials)}</span>` : ' <span class="meta">not set</span>';
  const body = !m ? form("Allocate")
    : markerPanel(v, t, m) + (m.status === "completed" ? `<p class="hint" style="margin:12px 0 6px">Start it again:</p>${form("Allocate again")}`
      : canEditMarker(m) ? "" : `<p class="hint" style="margin:12px 0 6px">Or take it over:</p>${form("Take over")}`);
  return `<details class="mkb" data-t="${t.id}" style="--mk:${col}" ${m && m.status !== "completed" ? "open" : ""}><summary><span class="mk-dot" aria-hidden="true"></span><strong>${esc(t.name)}</strong>${state}</summary>${body}</details>`;
}
function openFlag(v) {
  if (!v) return;
  fdVictim = v;
  const title = v.post_title || v.title, f = v.flag, a = v.alloc, isAdmin = ME.role === "admin";
  const region = v.rocu === "Unplaced" ? "region unknown" : v.rocu === "Overseas / not UK" ? "overseas / not UK" : v.rocu;
  $("#fd-sub").innerHTML = `<strong>${esc(title)}</strong> · ${esc(region)}`;
  const canUndo = f && (isAdmin || ME.username === f.by);
  const canRelease = a && (isAdmin || ME.username === a.by || (ME.initials || "") === a.initials);
  const status = [
    f ? `<div class="fd-status fp"><div class="fd-st-text"><strong>Flagged as a false positive</strong> by ${esc(f.by)}: ${esc(flagWhat(f))}${f.note ? ` &ldquo;${esc(f.note)}&rdquo;` : ""}. <span class="meta">${esc(fmtTime(f.at))}</span>${f.applied ? "" : `<br><span class="meta">Not in effect right now: ${v.manual ? "an admin set this region by hand" : "the automatic region has changed since"}.</span>`}${canUndo ? "" : `<br><span class="meta">Only the person who flagged it (${esc(f.by)}) or an admin can undo this flag.</span>`}</div>${canUndo ? '<button type="button" class="more" id="fd-unflag">Undo flag</button>' : ""}</div>` : "",
    a ? `<div class="fd-status al"><div class="fd-st-text"><strong>Allocated to ${esc(a.initials)}</strong>, set by ${esc(a.by)}. <span class="meta">${esc(fmtTime(a.at))}</span>${canRelease ? "" : `<br><span class="meta">Only the person who allocated it (${esc(a.by)}), ${esc(a.initials)} or an admin can release this.</span>`}</div>${canRelease ? '<button type="button" class="more" id="fd-release">Release</button>' : ""}</div>` : "",
  ].join("");
  const placedNow = placed(v), canFlag = !f && !v.manual && v.rocu !== "Overseas / not UK";
  const fpSection = canFlag ? `<div class="fd-sect"><h4>False positive</h4>
      <p class="hint">Tell everyone this is not where it appears. It is taken out of that list straight away, and you or an admin can undo it.</p>
      <fieldset class="fd-opts"><legend class="sr-only">Why is it a false positive?</legend>
        ${placedNow ? `<label class="fd-opt"><input type="radio" name="fp" value="not_region"><span><b>Not in ${esc(v.rocu)}</b><small>A UK victim, but not in this region.</small></span></label>` : ""}
        <label class="fd-opt"><input type="radio" name="fp" value="not_uk"><span><b>Not in the UK</b><small>Overseas, or not a UK victim at all.</small></span></label>
      </fieldset>
      <div class="fd-row"><label class="field grow">Note (optional)<input id="fd-note" class="search" maxlength="120" placeholder="e.g. head office is in Dublin"></label>
        <button type="button" class="btn" id="fd-flag" disabled>Flag as false positive</button></div></div>`
    : (!f && v.manual ? `<div class="fd-sect"><h4>False positive</h4><p class="hint">An admin set this victim's region by hand, so it can't be flagged. Ask an admin to change it.</p></div>` : "");
  const initial = ME.initials || suggestInitials(ME.username);
  const allocSection = `<div class="fd-sect"><h4>${a ? "Take over" : "Allocate"}</h4>
      <p class="hint">${a ? `Currently ${esc(a.initials)}. Put your own initials on it to take over.` : "Put your initials on this victim to show you are working on it."}</p>
      <div class="fd-row"><label class="field">Initials<input id="fd-init" class="search fd-init" maxlength="4" autocapitalize="characters" autocomplete="off" spellcheck="false" value="${esc(initial)}"></label>
        <label class="field">Allocated on (optional)<input id="fd-when" type="datetime-local" class="search" max="${dtLocal(new Date().toISOString())}"></label>
        <label class="chk"><input type="checkbox" id="fd-remember" ${ME.initials ? "" : "checked"}> Remember as mine</label>
        <button type="button" class="btn" id="fd-alloc">${a ? "Take over" : "Allocate"}</button></div></div>`;
  const taskT = MKTYPES.find((t) => t.builtin), taskM = taskT && (v.markers || []).find((m) => m.type_id === taskT.id);
  const progress = taskM ? `<div class="fd-sect" style="--mk:${safeColour(taskM.colour)}"><h4>Task progress <span class="badge mk ${taskM.status}" style="--mk:${safeColour(taskM.colour)}">${esc(MK_STATUS[taskM.status])}</span></h4>${markerPanel(v, taskT, taskM)}</div>` : "";
  const customs = MKTYPES.filter((t) => !t.builtin && (t.active || (v.markers || []).some((m) => m.type_id === t.id)));
  const others = customs.length ? `<div class="fd-sect"><h4>Other markers</h4><p class="hint">Each works like the Task: initials, then when it was allocated, started and completed. It shows in the live banner until it is completed.</p>${customs.map((t) => markerBlock(v, t, (v.markers || []).find((m) => m.type_id === t.id))).join("")}</div>` : "";
  $("#fd-body").innerHTML = `${status}${progress}${fpSection}${allocSection}${others}<p class="fd-msg" id="fd-msg" role="alert"></p>`;
  fdlg.hidden = false;
  const msg = (t) => { $("#fd-msg").textContent = t || ""; };
  const run = async (fn, ok) => { msg(""); try { await fn(); closeFlag(); afterMark(title); ok(); } catch (e) { msg(e.message); } };
  document.querySelectorAll('#fd-body input[name="fp"]').forEach((r) => r.addEventListener("change", () => { $("#fd-flag").disabled = false; msg(""); }));
  $("#fd-flag")?.addEventListener("click", () => {
    const kind = document.querySelector('#fd-body input[name="fp"]:checked')?.value;
    if (!kind) return;
    const note = $("#fd-note").value;
    run(() => api("/api/victims/flag", { method: "PUT", body: { title, kind, note } }),
      () => toast(`Flagged: ${kind === "not_uk" ? "not in the UK" : "not in " + v.rocu}.`, { label: "Undo", fn: () => undoFlag(title, true).then(() => toast("Flag removed")).catch((e) => toast(e.message)) }));
  });
  $("#fd-unflag")?.addEventListener("click", () => run(() => api(`/api/victims/flag?title=${encodeURIComponent(title)}`, { method: "DELETE" }), () => toast("Flag removed")));
  $("#fd-release")?.addEventListener("click", () => run(() => api(`/api/victims/alloc?title=${encodeURIComponent(title)}`, { method: "DELETE" }), () => toast("Released")));
  $("#fd-alloc").addEventListener("click", () => {
    const initials = $("#fd-init").value.trim().toUpperCase();
    if (!/^[A-Z]{2,4}$/.test(initials)) { msg("Initials must be 2 to 4 letters."); $("#fd-init").focus(); return; }
    const remember = $("#fd-remember").checked, when = dtIso($("#fd-when").value);
    run(async () => { await api("/api/victims/markers", { method: "PUT", body: { title, initials, remember, ...(when ? { allocated_at: when } : {}) } }); if (remember || !ME.initials) ME.initials = initials; },
      () => toast(`Allocated to ${initials}.`));
  });
  $("#fd-init").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#fd-alloc").click(); } });
  (document.querySelector('#fd-body input[name="fp"]') || $("#fd-init")).focus();
  track("modal_open", "flag or allocate");
}
// the Task progress and other-marker controls: one handler for the whole dialog, refreshing it in place
async function refreshFlagDialog(title) {
  const card = document.querySelector(".fd-card"), top = card ? card.scrollTop : 0;
  const nv = await fetchVictim(title);
  afterMark(title);
  if (nv) { openFlag(nv); if (card) card.scrollTop = top; }
}
$("#fd-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-mk]");
  if (!btn || !fdVictim) return;
  const title = fdVictim.post_title || fdVictim.title, panel = btn.closest(".mkp"), act = btn.dataset.mk;
  const msg = (t) => { $("#fd-msg").textContent = t || ""; };
  if (act === "edit") { const f = panel.querySelector(".mk-edit"); f.hidden = !f.hidden; btn.setAttribute("aria-expanded", String(!f.hidden)); if (!f.hidden) f.querySelector("input").focus(); return; }
  if (act === "edit-cancel") { const f = panel.querySelector(".mk-edit"); f.hidden = true; panel.querySelector('[data-mk="edit"]').setAttribute("aria-expanded", "false"); return; }
  msg("");
  btn.disabled = true;
  try {
    if (act === "put") {
      const box = btn.closest(".mk-new"), initials = box.querySelector(".mk-ini").value.trim().toUpperCase(), when = dtIso(box.querySelector(".mk-when").value);
      if (!/^[A-Z]{2,4}$/.test(initials)) { msg("Initials must be 2 to 4 letters."); box.querySelector(".mk-ini").focus(); return; }
      await api("/api/victims/markers", { method: "PUT", body: { title, type_id: +btn.dataset.t, initials, ...(when ? { allocated_at: when } : {}) } });
      toast("Marker set");
    } else {
      const id = panel.dataset.m;
      if (act === "remove") { if (!confirm("Remove this marker from the breach? Its dates are lost.")) return; await api(`/api/victims/markers/${id}`, { method: "DELETE" }); toast("Marker removed"); }
      else {
        const body = act === "start" ? { started_at: "now" } : act === "complete" ? { completed_at: "now" } : { completed_at: null };
        await api(`/api/victims/markers/${id}`, { method: "PATCH", body });
        toast(act === "start" ? "Started" : act === "complete" ? "Completed" : "Reopened");
      }
    }
    await refreshFlagDialog(title);
  } catch (err) { msg(err.message); } finally { btn.disabled = false; }
});
$("#fd-body").addEventListener("submit", async (e) => {
  const form = e.target.closest(".mk-edit");
  if (!form) return;
  e.preventDefault();
  const title = fdVictim.post_title || fdVictim.title, id = form.closest(".mkp").dataset.m, msg = (t) => { $("#fd-msg").textContent = t || ""; };
  const ini = form.querySelector(".mk-e-ini").value.trim().toUpperCase();
  if (!/^[A-Z]{2,4}$/.test(ini)) { msg("Initials must be 2 to 4 letters."); return; }
  try {
    await api(`/api/victims/markers/${id}`, { method: "PATCH", body: { initials: ini, allocated_at: dtIso(form.querySelector(".mk-e-alloc").value), started_at: dtIso(form.querySelector(".mk-e-start").value), completed_at: dtIso(form.querySelector(".mk-e-done").value) } });
    toast("Saved"); await refreshFlagDialog(title);
  } catch (err) { msg(err.message); }
});
$("#dr-body").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-gang-go]");
  if (!b) return;
  closeDrawer(); showTab("gangs", true);
  $("#gq").value = b.dataset.gangGo; $("#gq").dispatchEvent(new Event("input"));
});
document.addEventListener("click", (e) => { const b = e.target.closest("button[data-flag]"); if (b) { e.preventDefault(); openFlag(refVictim(b.dataset.flag)); } });
$("#fd-close").addEventListener("click", closeFlag);
fdlg.addEventListener("click", (e) => { if (e.target === fdlg) closeFlag(); });

// ------------------------------------------------------------------ click to copy
function toast(msg, action) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("on"); t.classList.toggle("act", !!action);
  if (action) {
    const b = document.createElement("button"); b.type = "button"; b.className = "toast-act"; b.textContent = action.label;
    b.addEventListener("click", () => { t.classList.remove("on"); action.fn(); });
    t.appendChild(b);
  }
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("on"), action ? 9000 : 2200);
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
  alerts_settings: "Switched alert emails on or off", alerts_group: "Changed a group's alerts", alerts_test: "Sent a test email", email_alerts: "Changed their own alert emails",
  screen_wake_on: "Screen wake on", screen_wake_off: "Screen wake off", screen_wake_failed: "Screen wake failed",
  password_change: "Changed own password", password_change_failed: "Password change failed",
  user_create: "Created a user", user_update: "Updated a user", user_delete: "Deleted a user",
  marker_set: "Put a marker on a breach", marker_update: "Updated a marker", marker_clear: "Removed a marker",
  marker_type_create: "Created a marker type", marker_type_update: "Changed a marker type", marker_type_delete: "Deleted a marker type",
  group_create: "Created a group", group_rename: "Renamed a group", group_delete: "Deleted a group", group_members: "Changed a group's members",
  placement_set: "Set a victim's region", placement_clear: "Cleared a manual region",
  victim_flag: "Flagged a false positive", victim_flag_clear: "Undid a false-positive flag", victim_alloc: "Allocated a victim", victim_alloc_clear: "Released a victim allocation",
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
ICONS.mail = '<rect x="3" y="5" width="18" height="14" rx="2"/><polyline points="3 7 12 13 21 7"/>';
let userCache = [], groupCache = [];
const uf = { group: "", sort: "name" };                   // the Users screen's view: which group to show, and the order
const tsOf = (x) => (x ? new Date(x).getTime() : -1);       // never = older than everything
const lastSignedIn = (u) => (u.last_login ? `<div>${esc(ago(u.last_login))}</div><div class="meta">${esc(fmtTime(u.last_login))}</div>` : '<span class="meta">Never signed in</span>');
const lastActive = (u) => {
  if (!u.last_activity) return '<span class="meta">No activity yet</span>';
  const now = Date.now() - tsOf(u.last_activity) < 5 * 60000;
  return `<div title="${esc(fmtTime(u.last_activity))}">${now ? '<span class="live-dot" role="img" aria-label="Active in the last 5 minutes" title="Active in the last 5 minutes"></span>' : ""}${esc(ago(u.last_activity))}</div>
    <div class="meta"><button type="button" class="link" data-act="events" data-id="${u.id}" title="See ${esc(u.username)}'s activity">${u.events} event${u.events === 1 ? "" : "s"}</button></div>`;
};
function groupCell(u) {
  if (!groupCache.length) return '<span class="meta">No groups yet</span>';
  const mine = groupCache.filter((g) => u.group_ids.includes(g.id));
  const free = groupCache.filter((g) => !u.group_ids.includes(g.id));
  const chips = mine.map((g) => `<span class="gchip">${esc(g.name)}<button type="button" data-act="ungroup" data-id="${u.id}" data-g="${g.id}" title="Take ${esc(u.username)} out of ${esc(g.name)}" aria-label="Remove ${esc(u.username)} from ${esc(g.name)}">&times;</button></span>`).join("");
  const add = free.length ? `<select class="search u-gadd" data-id="${u.id}" aria-label="Add ${esc(u.username)} to a group"><option value="">+ Add group</option>${free.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("")}</select>` : "";
  return `${chips ? `<div class="gchips">${chips}</div>` : ""}${add}`;
}
function renderGroups() {
  const list = $("#g-list");
  list.innerHTML = groupCache.length ? groupCache.map((g) => {
    const names = g.members.slice(0, 6).map((m) => esc(m.username)).join(", ") + (g.members.length > 6 ? ` and ${g.members.length - 6} more` : "");
    return `<div class="ugrp" data-g="${g.id}">
      <div class="ugrp-h"><button type="button" class="ugrp-name" data-act="gfilter" data-g="${g.id}" title="Show only the people in ${esc(g.name)}">${esc(g.name)}</button><span class="meta">${g.count} member${g.count === 1 ? "" : "s"}</span></div>
      <div class="ugrp-m ${g.count ? "" : "meta"}">${g.count ? names : "Nobody yet"}</div>
      <div class="ugrp-a"><button type="button" class="more" data-act="gmembers" data-g="${g.id}">Members</button><button type="button" class="more" data-act="grename" data-g="${g.id}">Rename</button><button type="button" class="more danger" data-act="gdelete" data-g="${g.id}" aria-label="Delete the group ${esc(g.name)}">Delete</button></div></div>`;
  }).join("") : '<p class="hint" style="margin:0">No groups yet. Create one above, then put people in it.</p>';
  const keep = uf.group;
  $("#u-fgroup").innerHTML = '<option value="">Everyone</option><option value="none">People in no group</option>' + groupCache.map((g) => `<option value="${g.id}">${esc(g.name)} (${g.count})</option>`).join("");
  if (keep && keep !== "none" && !groupCache.some((g) => String(g.id) === keep)) uf.group = "";
  $("#u-fgroup").value = uf.group;
}
function renderUsers() {
  const body = $("#user-rows");
  const regionOpts = (sel) => '<option value="">All UK</option>' + userRegions.map((r) => `<option ${r === sel ? "selected" : ""}>${esc(r)}</option>`).join("");
  let rows = userCache.slice();
  if (uf.group === "none") rows = rows.filter((u) => !u.group_ids.length);
  else if (uf.group) rows = rows.filter((u) => u.group_ids.includes(+uf.group));
  const key = { name: (u) => u.username.toLowerCase(), active: (u) => -tsOf(u.last_activity), "active-old": (u) => tsOf(u.last_activity), login: (u) => -tsOf(u.last_login), "login-old": (u) => tsOf(u.last_login) }[uf.sort];
  rows.sort((x, y) => (key(x) < key(y) ? -1 : key(x) > key(y) ? 1 : x.username.toLowerCase() < y.username.toLowerCase() ? -1 : 1));
  const g = uf.group && uf.group !== "none" ? groupCache.find((x) => String(x.id) === uf.group) : null;
  $("#u-count").textContent = rows.length === userCache.length ? `${rows.length} ${rows.length === 1 ? "person" : "people"}` : `${rows.length} of ${userCache.length} ${userCache.length === 1 ? "person" : "people"}${g ? ` in ${g.name}` : uf.group === "none" ? " in no group" : ""}`;
  body.innerHTML = rows.map((u) => `
      <tr data-id="${u.id}">
        <td class="who-cell"><div class="who"><span class="nm">${esc(u.username)}${u.username === ME.username ? ' <span class="badge src">you</span>' : ""}</span><span class="meta">created ${fmtDate(u.created_at)}${u.created_by ? ` by ${esc(u.created_by)}` : ""}</span><span class="meta u-mail${u.email ? "" : " none"}">${u.email ? `${esc(u.email)}${u.email_alerts ? "" : " · alert emails off"}` : "no email address"}</span></div></td>
        <td data-label="Role"><span class="badge ${u.role === "admin" ? "wm" : "uk"}">${esc(u.role)}</span></td>
        <td data-label="Groups" class="ugrp-cell">${groupCell(u)}</td>
        <td data-label="Region"><select class="search u-reg" data-id="${u.id}" aria-label="Region for ${esc(u.username)}">${regionOpts(u.region)}</select></td>
        <td data-label="Status"><div class="st">${u.active ? '<span class="badge on">active</span>' : '<span class="badge off">disabled</span>'}${u.must_change ? '<span class="badge wm" title="They must choose their own password when they next sign in">must set password</span>' : ""}</div></td>
        <td data-label="Last signed in">${lastSignedIn(u)}</td>
        <td data-label="Last active">${lastActive(u)}</td>
        <td class="actions r"><div class="row-actions">
          ${[["pw", "key", "Reset password", ""], ["mail", "mail", "Email address and alert emails", ""], ["role", "shield", u.role === "admin" ? "Make user (remove admin)" : "Make admin", ""],
             ["active", u.active ? "ban" : "ok", u.active ? "Disable sign-in" : "Enable sign-in", ""], ["del", "trash", "Delete user", " danger"]]
            .map(([act, ic, label, cls]) => `<button type="button" class="more icon-btn${cls}" data-act="${act}" data-id="${u.id}" title="${label}" aria-label="${label}: ${esc(u.username)}"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[ic]}</svg></button>`).join("")}
        </div></td>
      </tr>`).join("") || `<tr><td colspan="8" class="ldr">${uf.group ? "Nobody matches. Choose another group, or Everyone." : "No users."}</td></tr>`;
}
let userRegions = [];
async function loadUsers() {
  const body = $("#user-rows");
  if (!userCache.length) body.innerHTML = `<tr><td colspan="8">${sk(3)}</td></tr>`;
  try {
    const d = await api("/api/admin/users");
    userCache = d.users; groupCache = d.groups || []; userRegions = d.regions;
    if ($("#u-region").options.length <= 1) $("#u-region").innerHTML = '<option value="">All UK</option>' + d.regions.map((r) => `<option>${esc(r)}</option>`).join("");
    const me = userCache.find((u) => u.username === ME.username);
    if (me) ME.groups = groupCache.filter((g) => me.group_ids.includes(g.id)).map((g) => g.name);          // the account dialog shows these
    renderGroups(); renderUsers();
  } catch (e) { body.innerHTML = `<tr><td colspan="8" class="ldr">${esc(e.message)}</td></tr>`; }
}
const busy = async (form, fn) => {            // one request at a time per form: a double click must not send it twice
  const btn = form.querySelector("button[type=submit]");
  if (btn.disabled) return;
  btn.disabled = true;
  try { await fn(); } finally { btn.disabled = false; }
};
// ------------------------------------------------------------------ the live-tasks banner (Overview, above the two lists)
const lbOnly = () => { try { return localStorage.getItem("evileye-lb-region") === "1"; } catch (e) { return false; } };
async function loadLive() {
  const bar = $("#livebar");
  if (!ME || !ME.database) { bar.hidden = true; return; }
  const only = !!region && lbOnly();
  $("#lb-only-wrap").hidden = !region; $("#lb-region").textContent = region === "Unplaced" ? "region unknown" : region; $("#lb-only").checked = only;
  try {
    const d = await api(`/api/markers/live${only ? "?" + new URLSearchParams({ rocu: region }) : ""}`);
    bar.hidden = !d.total && !only;
    $("#lb-count").textContent = d.total ? `· ${d.total}` : "";
    const list = $("#lb-list");
    list.innerHTML = d.items.length ? d.items.map((x) => `<button type="button" class="lb-tile ${x.status}" role="listitem" data-title="${esc(x.title)}" style="--mk:${safeColour(x.colour)}" title="${esc(markerText(x))}">
        <span class="lb-name">${esc(x.title)}</span>
        <span class="lb-row"><span class="badge${x.builtin ? " alloc" : ""} mk ${x.status}" style="--mk:${safeColour(x.colour)}">${x.status === "started" ? ICON_PLAY : ICON_PERSON}${esc(x.name)}</span><b class="lb-ini">${esc(x.initials)}</b><span class="lb-st">${esc(MK_STATUS[x.status])}</span></span>
        <span class="lb-meta">${esc(x.rocu === "Unplaced" ? "region unknown" : x.rocu || "no longer in the feed")}${x.town && x.rocu && x.rocu !== "Unplaced" ? " · " + esc(x.town) : ""}</span>
        <span class="lb-meta">allocated ${esc(ago(x.allocated_at))}${x.started_at ? ` · started ${esc(ago(x.started_at))}` : ""}</span></button>`).join("")
      : `<p class="hint" style="margin:0">Nothing live in ${esc(region)} right now.</p>`;
    lbFade();
  } catch (e) { /* leave the banner as it was */ }
}
function lbFade() {                    // only when there are more tiles than fit: fade the edge that has more beyond it
  const el = $("#lb-list"), max = el.scrollWidth - el.clientWidth;
  el.classList.toggle("fade-l", max > 2 && el.scrollLeft > 2);
  el.classList.toggle("fade-r", max > 2 && el.scrollLeft < max - 2);
}
$("#lb-list").addEventListener("scroll", lbFade, { passive: true });
window.addEventListener("resize", lbFade);
$("#lb-only").addEventListener("change", () => { try { localStorage.setItem("evileye-lb-region", $("#lb-only").checked ? "1" : "0"); } catch (e) { /* ignore */ } loadLive(); });
$("#lb-list").addEventListener("click", async (e) => {
  const t = e.target.closest(".lb-tile");
  if (!t) return;
  try { const v = await fetchVictim(t.dataset.title); if (v) openVictim(v); else toast("Could not find that breach any more."); } catch (err) { toast("Could not load the breach details: " + err.message); }
});

// ------------------------------------------------------------------ settings: markers (admin)
const MK_PALETTE = ["#e02a52", "#f08a3c", "#e6bf55", "#44c593", "#36c5d6", "#4f9dff", "#7c83ff", "#a98bff", "#ec6dbd", "#94a2b7"];
let mkSel = "#4f9dff";
function mkPreview() { const p = $("#mk-preview"); p.style.setProperty("--mk", safeColour(mkSel)); p.innerHTML = `${ICON_PLAY}${esc(mkShort($("#mk-name").value.trim() || "Marker"))} AB`; }
function mkPickColour(c) {
  mkSel = c.toLowerCase();
  document.querySelectorAll("#mk-swatches input").forEach((r) => { r.checked = r.value === mkSel; });
  if (/^#[0-9a-f]{6}$/.test(mkSel)) $("#mk-colour").value = mkSel;
  mkPreview();
}
$("#mk-swatches").innerHTML = MK_PALETTE.map((c) => `<label class="mk-sw" style="--mk:${c}" title="${c}"><input type="radio" name="mkc" value="${c}" aria-label="Colour ${c}"><span></span></label>`).join("");
$("#mk-swatches").addEventListener("change", (e) => { if (e.target.name === "mkc") mkPickColour(e.target.value); });
$("#mk-colour").addEventListener("input", () => mkPickColour($("#mk-colour").value));
$("#mk-name").addEventListener("input", mkPreview);
mkPickColour(mkSel);
const mkMsg = (t) => { const m = $("#mk-msg"); m.hidden = !t; m.textContent = t || ""; };
$("#mkform").addEventListener("submit", (e) => {
  e.preventDefault(); mkMsg("");
  busy(e.target, async () => {
    try { const r = await api("/api/admin/marker-types", { method: "POST", body: { name: $("#mk-name").value, colour: mkSel } }); toast(`Marker “${r.name}” added`); $("#mk-name").value = ""; mkPreview(); await loadSettings(); $("#mk-name").focus(); }
    catch (err) { mkMsg(err.message); }
  });
});
function renderMarkerTypes() {
  $("#mk-list").innerHTML = MKTYPES.map((t) => `<div class="mkrow${t.active ? "" : " retired"}" data-id="${t.id}" style="--mk:${safeColour(t.colour)}">
    <input type="color" class="mk-pick" value="${safeColour(t.colour)}" aria-label="Colour for ${esc(t.name)}" title="Change the colour">
    <div class="mkrow-main"><strong>${esc(t.name)}</strong>${t.builtin ? ' <span class="badge src">built in</span>' : ""}${t.active ? "" : ' <span class="badge off">retired</span>'}
      <span class="meta">${t.live} live · ${t.total} in total</span></div>
    <span class="badge mk started" style="--mk:${safeColour(t.colour)}" aria-hidden="true">${ICON_PLAY}${esc(t.name.length > 12 ? t.name.slice(0, 11) + "…" : t.name)} AB</span>
    <div class="mkrow-a"><button type="button" class="more" data-mkt="rename" data-id="${t.id}">Rename</button>${t.builtin ? "" : `<button type="button" class="more" data-mkt="${t.active ? "retire" : "use"}" data-id="${t.id}">${t.active ? "Retire" : "Use again"}</button><button type="button" class="more danger" data-mkt="delete" data-id="${t.id}" aria-label="Delete the marker ${esc(t.name)}">Delete</button>`}</div></div>`).join("");
}
async function loadSettings() { await loadMarkerTypes(); renderMarkerTypes(); loadEmailAlerts(); }
// ------------------------------------------------------------------ email alerts (Settings): per-group regions, the master switch, a test, the log
let EA = null;
function renderEaHead() {
  const d = EA, st = $("#ea-status");
  st.hidden = false; st.className = d.configured ? "callout" : "callout err";
  st.innerHTML = "<span>" + (d.configured
    ? `Email sending is set up: messages come from <strong>${esc(d.sender)}</strong> and link to <strong>${esc(d.public_url)}</strong>. Only breaches first seen in the last ${d.window_days} days are ever sent, and at most ${d.daily_cap} emails in 24 hours (${d.sent_today} so far).`
    : `<strong>Email sending is not set up yet.</strong> ${esc(d.reason)} <span class="meta">Add the settings in Railway (EMAIL_API_KEY, EMAIL_FROM, and PUBLIC_URL) and redeploy. The key itself is never shown here.</span>`) + "</span>";
  $("#ea-master").checked = d.enabled; $("#ea-master").disabled = !d.configured && !d.enabled;
  $("#ea-note").textContent = d.enabled
    ? "Alerts are on. They go out after the dashboard reads its feeds (when someone opens it and the data is over 30 minutes old, or someone presses Refresh), not on a timer. Nothing already on the dashboard is sent when a group is switched on."
    : "Alerts are off: nothing is sent, whatever the groups below say.";
  $("#ea-test").disabled = !d.configured;
  $("#ea-log").innerHTML = d.log.length ? d.log.map((r) => `<div class="ea-lr" role="listitem"><div class="ea-lh"><span class="meta">${esc(fmtTime(r.ts))}</span>${r.ok ? '<span class="badge on">sent</span>' : '<span class="badge off">problem</span>'}${r.kind === "test" ? "" : `<span class="meta">${r.to_count} ${r.to_count === 1 ? "person" : "people"}</span>`}</div>
      <div class="ea-lw">${r.kind === "test" ? "Test email" : esc(r.group_name || "Alert")}</div><div class="meta ea-ls">${esc(r.subject)}</div>${r.detail ? `<div class="meta ea-ls">${esc(r.detail)}</div>` : ""}</div>`).join("") : '<p class="hint" style="margin:0">Nothing sent yet.</p>';
}
function renderEaGroups() {
  const d = EA, box = $("#ea-groups");
  box.innerHTML = d.groups.length ? d.groups.map((g) => {
    const warn = g.enabled && !g.recipients;
    const who = g.members ? `${g.recipients} of ${g.members} ${g.members === 1 ? "member" : "members"} can be emailed.` : "Nobody is in this group yet (add people from the Users tab).";
    return `<div class="ea-g" data-g="${g.id}"><div class="ea-gh"><strong>${esc(g.name)}</strong><label class="chk"><input type="checkbox" class="ea-on" ${g.enabled ? "checked" : ""}> Email this group about new breaches</label></div>
      <fieldset class="ea-regions"><legend>Regions to watch</legend><div class="ea-grid">${d.regions.map((r) => `<label class="chk"><input type="checkbox" class="ea-reg" value="${esc(r)}" ${g.regions.includes(r) ? "checked" : ""}> ${esc(r)}</label>`).join("")}</div>
        <div class="ea-quick"><button type="button" class="more small" data-ea="all">All regions</button><button type="button" class="more small" data-ea="none">None</button></div></fieldset>
      <label class="chk"><input type="checkbox" class="ea-unp" ${g.include_unplaced ? "checked" : ""}> Also breaches whose region is not known yet</label>
      <p class="hint ea-who${warn ? " warn" : ""}">${esc(who)}${g.no_email.length ? ` No email address: ${esc(g.no_email.join(", "))}.` : ""}${g.opted_out.length ? ` Switched their own alerts off: ${esc(g.opted_out.join(", "))}.` : ""}${warn ? " Nobody can be emailed yet, so nothing would be sent." : ""}</p>
      <div class="fd-row"><button type="button" class="btn" data-ea="save">Save</button><span class="meta ea-saved" role="status"></span></div></div>`;
  }).join("") : '<p class="hint" style="margin:0">No groups yet. Make a group on the Users tab, put people in it, then choose its regions here.</p>';
}
async function loadEmailAlerts(headOnly) {
  try { EA = await api("/api/admin/alerts"); renderEaHead(); if (!headOnly) renderEaGroups(); }
  catch (e) { $("#ea-status").hidden = false; $("#ea-status").className = "callout err"; $("#ea-status").textContent = e.message; }
}
$("#ea-master").addEventListener("change", async (e) => {
  const on = e.target.checked;
  try { await api("/api/admin/alerts/settings", { method: "PUT", body: { enabled: on } }); toast(on ? "Alert emails on" : "Alert emails off"); }
  catch (err) { toast(err.message); }
  loadEmailAlerts(true);
});
$("#ea-groups").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-ea]");
  if (!b) return;
  const card = b.closest(".ea-g");
  if (b.dataset.ea === "all" || b.dataset.ea === "none") { card.querySelectorAll(".ea-reg").forEach((c) => { c.checked = b.dataset.ea === "all"; }); return; }
  const out = card.querySelector(".ea-saved"), g = EA.groups.find((x) => String(x.id) === card.dataset.g);
  const body = { enabled: card.querySelector(".ea-on").checked, regions: [...card.querySelectorAll(".ea-reg:checked")].map((c) => c.value), include_unplaced: card.querySelector(".ea-unp").checked };
  b.disabled = true; out.textContent = "";
  try {
    const r = await api(`/api/admin/groups/${card.dataset.g}/alerts`, { method: "PUT", body });
    toast(body.enabled ? `${g.name}: alerts on${r.already_listed ? `. ${r.already_listed} breach${r.already_listed === 1 ? "" : "es"} already on the dashboard will not be emailed` : ""}` : `${g.name}: alerts off`);
    await loadEmailAlerts(); const again = $(`#ea-groups .ea-g[data-g="${card.dataset.g}"] .ea-saved`); if (again) again.textContent = "Saved";
  } catch (err) { out.textContent = err.message; out.classList.add("err"); } finally { b.disabled = false; }
});
$("#ea-test").addEventListener("click", async () => {
  const b = $("#ea-test"), out = $("#ea-test-msg"); b.disabled = true; out.textContent = "Sending…";
  try { const r = await api("/api/admin/alerts/test", { method: "POST" }); out.textContent = r.ok ? "Sent. Check your inbox (and the spam folder, the first time)." : r.error; }
  catch (err) { out.textContent = err.message; }
  finally { b.disabled = false; loadEmailAlerts(true); }
});
$("#mk-list").addEventListener("change", async (e) => {
  const pick = e.target.closest(".mk-pick");
  if (!pick) return;
  try { await api(`/api/admin/marker-types/${pick.closest(".mkrow").dataset.id}`, { method: "PATCH", body: { colour: pick.value } }); toast("Colour saved"); } catch (err) { toast(err.message); }
  loadSettings();
});
$("#mk-list").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-mkt]");
  if (!b) return;
  const t = MKTYPES.find((x) => String(x.id) === b.dataset.id), act = b.dataset.mkt;
  const done = (m) => { toast(m); loadSettings(); };
  try {
    if (act === "rename") {
      openModal(); $("#modal-back").hidden = true;
      $("#modal-title").textContent = `Rename marker: ${t.name}`; $("#modal-explain").textContent = "It is renamed everywhere it is already used.";
      $("#modal-body").innerHTML = `<form id="mkrform" class="form-row"><label class="field">Marker name<input id="mkr-name" class="search" required maxlength="24" value="${esc(t.name)}" autocomplete="off"></label><button class="btn" type="submit">Save</button><button type="button" class="more" id="mkr-cancel">Cancel</button></form><p id="mkr-msg" class="hint" role="alert"></p>`;
      $("#mkr-name").focus(); $("#mkr-name").select();
      $("#mkr-cancel").addEventListener("click", closeModal);
      $("#mkrform").addEventListener("submit", (ev) => { ev.preventDefault(); busy(ev.target, async () => { try { await api(`/api/admin/marker-types/${t.id}`, { method: "PATCH", body: { name: $("#mkr-name").value } }); closeModal(); done("Renamed"); } catch (err) { $("#mkr-msg").textContent = err.message; } }); });
    } else if (act === "retire") { await api(`/api/admin/marker-types/${t.id}`, { method: "PATCH", body: { active: false } }); done(`${t.name} retired`); }
    else if (act === "use") { await api(`/api/admin/marker-types/${t.id}`, { method: "PATCH", body: { active: true } }); done(`${t.name} can be used again`); }
    else if (act === "delete" && confirm(`Delete the marker “${t.name}”? ${t.total ? "It is used on breaches, so this will be refused: retire it instead." : "Nothing carries it, so nothing else changes."}`)) { await api(`/api/admin/marker-types/${t.id}`, { method: "DELETE" }); done(`${t.name} deleted`); }
  } catch (err) { toast(err.message); }
});

// ------------------------------------------------------------------ groups: named sets of people
const gMsg = (text) => { const m = $("#g-msg"); m.hidden = !text; m.textContent = text || ""; };
$("#gform").addEventListener("submit", (e) => {
  e.preventDefault(); gMsg("");
  busy(e.target, async () => {
    try {
      const r = await api("/api/admin/groups", { method: "POST", body: { name: $("#g-name").value } });
      toast(`Group “${r.name}” created`); $("#g-name").value = ""; await loadUsers(); $("#g-name").focus();
    } catch (err) { gMsg(err.message); }
  });
});
const groupById = (id) => groupCache.find((g) => String(g.id) === String(id));
function renameDialog(g) {
  openModal(); $("#modal-back").hidden = true;
  $("#modal-title").textContent = `Rename group: ${g.name}`;
  $("#modal-explain").textContent = "Its members stay in it; only the name changes.";
  $("#modal-body").innerHTML = `<form id="grform" class="form-row"><label class="field">Group name<input id="gr-name" class="search" required maxlength="40" value="${esc(g.name)}" autocomplete="off"></label>
    <button class="btn" type="submit">Save</button><button type="button" class="more" id="gr-cancel">Cancel</button></form><p id="gr-msg" class="hint" role="alert"></p>`;
  $("#gr-name").focus(); $("#gr-name").select();
  $("#gr-cancel").addEventListener("click", closeModal);
  $("#grform").addEventListener("submit", (e) => {
    e.preventDefault();
    busy(e.target, async () => {
      try { const r = await api(`/api/admin/groups/${g.id}`, { method: "PATCH", body: { name: $("#gr-name").value } }); closeModal(); toast(`Renamed to “${r.name}”`); loadUsers(); }
      catch (err) { $("#gr-msg").textContent = err.message; }
    });
  });
}
function membersDialog(g) {
  openModal(); $("#modal-back").hidden = true;
  $("#modal-title").textContent = `Members: ${g.name}`;
  $("#modal-explain").textContent = "Tick everyone who should be in this group, then save. Anyone you untick is only taken out of the group; nobody is deleted.";
  const inG = new Set(g.members.map((m) => m.id));
  $("#modal-body").innerHTML = `<form id="gmform"><fieldset class="mem-list"><legend class="sr-only">People in ${esc(g.name)}</legend>${userCache.map((u) => `<label class="mem"><input type="checkbox" value="${u.id}" ${inG.has(u.id) ? "checked" : ""}><span class="mem-n">${esc(u.username)}${u.username === ME.username ? " (you)" : ""}</span><span class="badge ${u.role === "admin" ? "wm" : "uk"}">${esc(u.role)}</span>${u.active ? "" : '<span class="badge off">disabled</span>'}</label>`).join("")}</fieldset>
    <div class="mem-tools"><button type="button" class="link" id="gm-all">Tick everyone</button><button type="button" class="link" id="gm-none">Untick everyone</button><span class="meta" id="gm-count" aria-live="polite"></span></div>
    <div class="mem-actions"><button class="btn" type="submit">Save members</button><button type="button" class="more" id="gm-cancel">Cancel</button></div><p id="gm-msg" class="hint" role="alert"></p></form>`;
  const boxes = () => [...document.querySelectorAll("#gmform .mem input")];
  const count = () => { const n = boxes().filter((b) => b.checked).length; $("#gm-count").textContent = `${n} of ${userCache.length} ticked`; };
  count();
  $("#gmform").addEventListener("change", count);
  $("#gm-all").addEventListener("click", () => { boxes().forEach((b) => { b.checked = true; }); count(); });
  $("#gm-none").addEventListener("click", () => { boxes().forEach((b) => { b.checked = false; }); count(); });
  $("#gm-cancel").addEventListener("click", closeModal);
  $("#gmform").addEventListener("submit", (e) => {
    e.preventDefault();
    busy(e.target, async () => {
      try { const r = await api(`/api/admin/groups/${g.id}/members`, { method: "PUT", body: { user_ids: boxes().filter((b) => b.checked).map((b) => +b.value) } }); closeModal(); toast(`${g.name}: ${r.count} member${r.count === 1 ? "" : "s"}`); loadUsers(); }
      catch (err) { $("#gm-msg").textContent = err.message; }
    });
  });
}
$("#g-list").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const g = groupById(b.dataset.g);
  if (!g) return;
  if (b.dataset.act === "gmembers") membersDialog(g);
  else if (b.dataset.act === "grename") renameDialog(g);
  else if (b.dataset.act === "gfilter") { uf.group = String(g.id); $("#u-fgroup").value = uf.group; renderUsers(); $(".users").scrollIntoView({ behavior: "smooth", block: "start" }); }
  else if (b.dataset.act === "gdelete") {
    if (confirm(`Delete the group “${g.name}”? ${g.count ? `Its ${g.count} member${g.count === 1 ? "" : "s"} stay${g.count === 1 ? "s" : ""} on the dashboard; they are just taken out of the group.` : "It has no members."}`))
      api(`/api/admin/groups/${g.id}`, { method: "DELETE" }).then(() => { toast(`Group “${g.name}” deleted`); loadUsers(); }).catch((err) => toast(err.message));
  }
});
$("#u-fgroup").addEventListener("change", () => { uf.group = $("#u-fgroup").value; renderUsers(); });
$("#u-sort").addEventListener("change", () => { uf.sort = $("#u-sort").value; renderUsers(); });
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
  const gsel = e.target.closest(".u-gadd");
  if (gsel) {
    if (!gsel.value) return;
    const g = groupCache.find((x) => String(x.id) === gsel.value), u = userCache.find((x) => String(x.id) === gsel.dataset.id);
    try { await api(`/api/admin/groups/${gsel.value}/members/${gsel.dataset.id}`, { method: "PUT" }); toast(`${u.username} added to ${g.name}`); }
    catch (err) { toast(err.message); }
    loadUsers(); return;
  }
  const sel = e.target.closest(".u-reg");
  if (!sel) return;
  try { await api(`/api/admin/users/${sel.dataset.id}`, { method: "PATCH", body: { region: sel.value } }); toast("Region saved"); }
  catch (err) { toast(err.message); loadUsers(); }
});
$("#user-rows").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const u = userCache.find((x) => String(x.id) === b.dataset.id);
  if (b.dataset.act === "ungroup") {
    const g = groupCache.find((x) => String(x.id) === b.dataset.g);
    api(`/api/admin/groups/${g.id}/members/${u.id}`, { method: "DELETE" }).then(() => { toast(`${u.username} taken out of ${g.name}`); loadUsers(); }).catch((err) => toast(err.message));
    return;
  }
  const patch = (body) => api(`/api/admin/users/${u.id}`, { method: "PATCH", body }).then(loadUsers).catch((err) => toast(err.message));
  if (b.dataset.act === "role") patch({ role: u.role === "admin" ? "user" : "admin" });
  else if (b.dataset.act === "active") patch({ active: !u.active });
  else if (b.dataset.act === "events") showUserActivity(u);
  else if (b.dataset.act === "del") {
    if (confirm(`Delete ${u.username}? Their past activity stays in the log.`)) api(`/api/admin/users/${u.id}`, { method: "DELETE" }).then(loadUsers).catch((err) => toast(err.message));
  } else if (b.dataset.act === "pw") passwordDialog(u);
  else if (b.dataset.act === "mail") emailDialog(u);
});
function emailDialog(u) {
  openModal(); $("#modal-back").hidden = true;
  $("#modal-title").textContent = `Email: ${u.username}`;
  $("#modal-explain").textContent = "Where alert emails go for the groups they are in. Only an admin can set it. Leave it empty to remove it.";
  $("#modal-body").innerHTML = `<form id="emform" class="form-row"><label class="field">Email address<input id="em-addr" class="search" type="email" maxlength="254" value="${esc(u.email || "")}" autocomplete="off" placeholder="name@example.com"></label>
    <label class="chk"><input type="checkbox" id="em-on" ${u.email_alerts ? "checked" : ""}> Send them alert emails</label>
    <button class="btn" type="submit">Save</button><button type="button" class="more" id="em-cancel">Cancel</button></form><p id="em-msg" class="hint" role="alert"></p>`;
  $("#em-addr").focus();
  $("#em-cancel").addEventListener("click", closeModal);
  $("#emform").addEventListener("submit", (ev) => {
    ev.preventDefault();
    busy(ev.target, async () => {
      try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { email: $("#em-addr").value.trim(), email_alerts: $("#em-on").checked } }); closeModal(); toast(`${u.username}: saved`); loadUsers(); if (!$("#tab-settings").hidden && $("#ea-card")) loadEmailAlerts(); }
      catch (err) { $("#em-msg").textContent = err.message; }
    });
  });
}
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
const OB_STEPS = 4;      // 1 welcome, 2 light or dark, 3 the icons, 4 region
function showObStep(n) {
  ob.step = n;
  for (let i = 1; i <= OB_STEPS; i++) { $("#ob-step" + i).hidden = i !== n; $("#ob-dot" + i).classList.toggle("on", i === n); }
  obEl.querySelector(".ob-card").classList.toggle("wide", n >= 3);
  obEl.setAttribute("aria-labelledby", ["ob-title", "ob-title-theme", "ob-title-icons", "ob-title2"][n - 1]);
  if (n === 2) syncObTheme();
  if (n === 3) renderObIcons();
  if (n === 4) renderObRegions();
  setTimeout(() => ({ 1: $("#ob-next"), 2: obEl.querySelector('.ob-theme[aria-checked="true"]'), 3: $("#ob-next3"), 4: obEl.querySelector('.ob-tile[aria-checked="true"]') || $("#ob-go") }[n]).focus(), 30);
}
function syncObTheme() {
  obEl.querySelectorAll(".ob-theme").forEach((b) => { const on = b.dataset.pick === curTheme(); b.setAttribute("aria-checked", String(on)); b.tabIndex = on ? 0 : -1; });
}
function pickObTheme(btn) { setTheme(btn.dataset.pick); saveTheme(btn.dataset.pick); syncObTheme(); btn.focus(); }
$("#ob-themes").addEventListener("click", (e) => { const b = e.target.closest(".ob-theme"); if (b) pickObTheme(b); });
$("#ob-themes").addEventListener("keydown", (e) => {
  const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
  if (!(e.key in keys)) return;
  const all = [...obEl.querySelectorAll(".ob-theme")], i = all.indexOf(document.activeElement);
  if (i < 0) return;
  e.preventDefault(); pickObTheme(all[(i + keys[e.key] + all.length) % all.length]);
});
// the icon guide is built from the real buttons on the page, so it always shows exactly what is on screen
function renderObIcons() {
  const live = (sel) => `<span class="ob-ibtn">${document.querySelector(sel).innerHTML}</span>`;
  const groups = [
    ["In the header", [
      [live("#refresh-now"), "Refresh", "Fetches the latest feeds now. The dashboard also refreshes by itself whenever you open it."],
      [live("#theme"), "Light or dark", "Switches between the light and dark look. Your choice is kept on your account."],
      [live("#usermenu"), "Account", "Change your password, or show this welcome tour again."],
      [live('a[href="/logout"]'), "Sign out", "Ends your session. Always do this on a shared computer."],
    ]],
    ["On each breach", [
      [`<span class="ob-ibtn">${ICON_FLAG}</span>`, "Flag or allocate", "Mark a breach as a false positive (not in this region, or not in the UK), or put your initials on it to show you are working on it."],
      [`<span class="badge alloc">${ICON_PERSON}AB</span>`, "Initials badge", "Someone is working on this breach. The letters are their initials."],
      [`<span class="badge fp">false positive</span>`, "False positive", "A user flagged it as not belonging here, so it has moved out of the lists. It can be undone."],
      [`<span class="badge new">NEW</span>`, "New", "First seen since your last visit."],
      [`<span class="ob-arrow" aria-hidden="true">\u2197</span>`, "Outside link", "Opens another website in a new tab, such as a news story or Companies House."],
      [`<span class="ob-copy" aria-hidden="true">copy</span>`, "Copy", "Copies a dark web (leak-site) address to your clipboard. This dashboard never opens those addresses."],
    ]],
  ];
  $("#ob-icons").innerHTML = groups.map(([h, rows]) => `<section><h3>${h}</h3><ul class="ob-legend">${rows.map(([ic, name, desc]) => `<li><span class="ic">${ic}</span><div><strong>${name}</strong><span class="d">${desc}</span></div></li>`).join("")}</ul></section>`).join("");
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
$("#ob-back2").addEventListener("click", () => showObStep(1));
$("#ob-next2").addEventListener("click", () => showObStep(3));
$("#ob-back3").addEventListener("click", () => showObStep(2));
$("#ob-next3").addEventListener("click", () => showObStep(4));
$("#ob-back").addEventListener("click", () => showObStep(3));
$("#ob-go").addEventListener("click", () => finishOnboarding(false));
["#ob-skip", "#ob-skipT", "#ob-skipI", "#ob-skip2"].forEach((id) => $(id).addEventListener("click", () => finishOnboarding(true)));

// ------------------------------------------------------------------ account
$("#usermenu").addEventListener("click", () => {
  openModal(); $("#modal-back").hidden = true;
  $("#modal-title").textContent = ME.username;
  $("#modal-explain").textContent = `${ME.role === "admin" ? "Administrator" : "User"} · ${ME.database ? "activity is logged" : "database not connected"}${ME.groups && ME.groups.length ? ` · ${ME.groups.length === 1 ? "Group" : "Groups"}: ${ME.groups.join(", ")}` : ""}`;
  $("#modal-body").innerHTML = ME.database ? `<h3 class="sub">Change password</h3>
    <form id="mypw" class="form-row"><input class="sr-only-input" type="text" name="username" value="${esc(ME.username)}" autocomplete="username" tabindex="-1" aria-hidden="true">
      <label class="field">Current password<input id="pw-cur" class="search" type="password" required autocomplete="current-password"></label>
      <label class="field">New password (10+ characters)<input id="pw-new" class="search" type="password" minlength="10" required autocomplete="new-password"></label>
      <label class="field">Repeat new password<input id="pw-rep" class="search" type="password" minlength="10" required autocomplete="new-password"></label>
      <button class="btn" type="submit">Change password</button></form><p id="mypw-msg" class="hint"></p>`
    : '<p class="hint">The user database is not connected, so accounts and passwords are unavailable. Set DATABASE_URL.</p>';
  if (ME.database) {
    $("#modal-body").insertAdjacentHTML("beforeend", `<h3 class="sub" style="margin-top:18px">Email alerts</h3>${ME.email
      ? `<label class="chk"><input type="checkbox" id="my-ea" ${ME.email_alerts ? "checked" : ""}> Email me about new breaches in my groups</label><p class="hint" style="margin:6px 0 0">Sent to ${esc(ME.email)}. Which regions you hear about is set per group by an admin.</p>`
      : '<p class="hint" style="margin:0">Your account has no email address, so you get no alert emails. An admin can add one for you.</p>'}`);
    const ea = $("#my-ea");
    if (ea) ea.addEventListener("change", async () => { try { await api("/api/me/email-alerts", { method: "PUT", body: { on: ea.checked } }); ME.email_alerts = ea.checked; toast(ea.checked ? "Alert emails on" : "Alert emails off"); } catch (err) { ea.checked = !ea.checked; toast(err.message); } });
  }
  $("#modal-body").insertAdjacentHTML("beforeend", '<p style="margin-top:14px"><button type="button" class="more" id="show-welcome">Show the welcome tour (look, icon guide, region)</button></p>');
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
$("#vmark").addEventListener("change", () => { vm.mark = $("#vmark").value; vm.page = 0; loadVictims(); track("filter", `victims show: ${vm.mark || "all"}`); });
$("#vsector").addEventListener("change", () => { vm.sector = $("#vsector").value; vm.page = 0; loadVictims(); track("filter", `victims sector: ${vm.sector || "all"}`); });
$("#vnew").addEventListener("change", () => { vm.newOnly = $("#vnew").checked; vm.page = 0; loadVictims(); track("filter", `victims new only: ${vm.newOnly}`); });
[["gsource", "gangs source", loadGangs], ["gsort", "gangs sort", loadGangs], ["asource", "alerts source", loadAlerts]].forEach(([id, label, fn]) =>
  $(`#${id}`).addEventListener("change", () => { fn(); track("filter", `${label}: ${$(`#${id}`).value || "all"}`); }));
document.querySelectorAll('input[name="glive"]').forEach((el) => el.addEventListener("change", () => { loadGangs(); track("filter", `gangs live only: ${el.value === "1"}`); }));
$("#vrefresh").addEventListener("click", loadVictims);
$("#grefresh").addEventListener("click", loadGangs);
$("#arefresh").addEventListener("click", loadAlerts);

// ------------------------------------------------------------------ search: every cached breach, gang, news story and alert
// A type-ahead in the top bar. The server ranks (GET /api/search); picking a breach opens its details pane, picking anything else
// lands on the right tab already filtered to it.
const gs = { box: $("#gsearch"), input: $("#gs-input"), pop: $("#gs-pop"), list: $("#gs-list"), foot: $("#gs-foot"), clear: $("#gs-clear"),
  opts: [], active: -1, seq: 0, ctl: null, data: null, logT: 0 };
const GS_GROUPS = [
  { key: "breaches", kind: "breach", label: "Breaches", icon: SEC_ICON.company, tab: "UK Victims" },
  { key: "gangs", kind: "gang", label: "Gangs", icon: SEC_ICON.gang, tab: "Gang Catalog" },
  { key: "news", kind: "news", label: "Dark web news", icon: svgI('<path d="M4 5h13a1 1 0 0 1 1 1v13a2 2 0 0 0 2 2H6a2 2 0 0 1-2-2z"/><path d="M18 9h2a1 1 0 0 1 1 1v9a2 2 0 0 1-2 2"/><path d="M8 9h6M8 13h6M8 17h4"/>'), tab: "Dark Web News" },
  { key: "alerts", kind: "alert", label: "Alerts", icon: svgI('<path d="M12 3 2.5 20h19z"/><path d="M12 10v4.5"/><path d="M12 17.5v.01"/>'), tab: "Alerts" },
];
const gsToks = () => gs.input.value.trim().toLowerCase().split(/\s+/).filter(Boolean).slice(0, 6);
function gsHl(text, toks) {            // the typed words picked out in bold; everything is escaped first, so a feed title can never inject markup
  if (!toks.length) return esc(text);
  const re = new RegExp("(" + toks.slice().sort((a, b) => b.length - a.length).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "ig");
  return String(text ?? "").split(re).map((p, i) => (i % 2 ? `<mark>${esc(p)}</mark>` : esc(p))).join("");
}
function gsWhere(b) {
  if (b.rocu === "Unplaced") return "region unknown";
  if (b.rocu === "Overseas / not UK") return "overseas / not UK";
  return (b.town ? b.town + ", " : "") + b.rocu;
}
function gsRow(kind, it, toks, i) {
  let title, sub, tags = "";
  if (kind === "breach") {
    title = it.title; sub = [it.group, it.sector, gsWhere(it), ago(it.seen)].filter(Boolean).join(" · ");
    tags = (it.new ? '<span class="badge new">NEW</span>' : "") + (it.fp ? '<span class="badge fp">false positive</span>' : "") + (it.alloc ? `<span class="badge alloc">${ICON_PERSON}${esc(it.alloc)}</span>` : "")
      + (it.via ? '<span class="gs-via">in description</span>' : "");
  } else if (kind === "gang") {
    title = it.name; sub = `${it.altname ? "also known as " + it.altname + " · " : ""}${it.uk_victims} UK victim${it.uk_victims === 1 ? "" : "s"} on record`;
  } else if (kind === "news") {
    title = it.title; sub = [it.source, it.published ? fmtDate(it.published) : ""].filter(Boolean).join(" · ");
  } else {
    title = it.title; sub = [it.source, it.kind, it.published ? fmtDate(it.published) : ""].filter(Boolean).join(" · ");
  }
  return `<div class="gs-o" role="option" id="gs-o${i}" data-i="${i}" aria-selected="false"><span class="gs-main"><span class="gs-t">${gsHl(title, toks)}</span><span class="gs-s">${esc(sub)}</span></span>${tags ? `<span class="gs-tags">${tags}</span>` : ""}</div>`;
}
function gsOpen() { gs.pop.hidden = false; gs.input.setAttribute("aria-expanded", "true"); }
function gsClose() { gs.pop.hidden = true; gs.input.setAttribute("aria-expanded", "false"); gsActive(-1); }
function gsActive(i, scroll) {
  gs.active = i;
  gs.list.querySelectorAll(".gs-o").forEach((el, k) => { const on = k === i; el.classList.toggle("on", on); el.setAttribute("aria-selected", String(on)); if (on && scroll) el.scrollIntoView({ block: "nearest" }); });
  if (i >= 0) gs.input.setAttribute("aria-activedescendant", `gs-o${i}`); else gs.input.removeAttribute("aria-activedescendant");
}
function gsRender(d) {
  gs.opts = []; gs.active = -1;
  const toks = gsToks();
  let html = "", foot = "";
  if (!d || d.short) {
    html = '<div class="gs-empty"><strong>Search everything the dashboard has cached.</strong><br>Breaches, gangs, dark web news and alerts. Try a company name, a gang, a town or a website.</div>';
  } else if (d.error) {
    html = `<div class="gs-empty" role="alert">Search is unavailable right now: ${esc(d.error)}</div>`;
  } else {
    GS_GROUPS.forEach((g) => {
      const grp = d[g.key];
      if (!grp || !grp.items.length) return;
      html += `<div role="group" aria-labelledby="gs-h-${g.key}"><div class="gs-h" id="gs-h-${g.key}"><span class="gs-hi">${g.icon}</span>${g.label}<span class="gs-n">${grp.total > grp.items.length ? `${grp.items.length} of ${grp.total}` : grp.total}</span></div>`;
      grp.items.forEach((it) => { html += gsRow(g.kind, it, toks, gs.opts.length); gs.opts.push({ kind: g.kind, it, tab: g.tab }); });
      html += "</div>";
    });
    if (!gs.opts.length) html = `<div class="gs-empty">Nothing cached matches <strong>&ldquo;${esc(d.q)}&rdquo;</strong>.<br>Try fewer words, a company name, a gang, a town or a website.</div>`;
    const more = d.breaches.total - d.breaches.items.length;
    if (more > 0) foot += `<button type="button" class="gs-all" data-act="all">View all ${d.breaches.total} breaches in UK Victims</button>`;
  }
  gs.list.innerHTML = html;
  gs.list.scrollTop = 0;
  gs.foot.innerHTML = foot + '<span class="gs-keys">↑ ↓ to move · Enter to open · Esc to close</span>';
  gs.foot.hidden = false;
  gsOpen();
}
const gsRun = debounce(async () => {
  const q = gs.input.value.trim();
  if (q.length < 2) { gs.seq++; if (gs.ctl) gs.ctl.abort(); gs.box.classList.remove("busy"); gs.data = null; gsRender(null); return; }
  if (gs.ctl) gs.ctl.abort();
  const ctl = gs.ctl = new AbortController(), seq = ++gs.seq;
  gs.box.classList.add("busy");
  try {
    const d = await api(`/api/search?${new URLSearchParams({ q })}`, { signal: ctl.signal });
    if (seq !== gs.seq) return;
    gs.data = d; gsRender(d);
  } catch (e) {
    if (e.name === "AbortError" || seq !== gs.seq) return;
    gs.data = null; gsRender({ error: e.message });
  } finally { if (seq === gs.seq) gs.box.classList.remove("busy"); }
}, 150);
function gsLog(q, outcome) {          // one activity line per search: what was typed (after a pause) or what it led to
  clearTimeout(gs.logT);
  if (outcome) track("search", `all: ${q} → ${outcome}`);
  else if (q) gs.logT = setTimeout(() => track("search", `all: ${q}`), 900);
}
function gsPick(i) {
  const o = gs.opts[i];
  if (!o) return;
  const q = gs.input.value.trim();
  gsClose(); gs.input.blur();
  if (o.kind === "breach") {
    gsLog(q, `breach: ${o.it.title}`);
    fetchVictim(o.it.title).then((v) => (v ? openVictim(v) : toast("Could not find that breach any more.")))
      .catch((e) => toast("Could not load the breach details: " + e.message));
  } else if (o.kind === "gang") {
    gsLog(q, `gang: ${o.it.name}`);
    $("#gsource").value = ""; document.querySelector('input[name="glive"][value=""]').checked = true; $("#gq").value = o.it.name;
    showTab("gangs", true);
  } else if (o.kind === "news") {
    gsLog(q, `news: ${o.it.title}`);
    Object.assign(nm, { q: o.it.title, source: "", theme: "", group: "", uk: "" });
    $("#nq").value = o.it.title; $("#nsource").value = ""; $("#ntheme").value = ""; document.querySelector('input[name="nuk"][value=""]').checked = true;
    showTab("news", true);
  } else {
    gsLog(q, `alert: ${o.it.title}`);
    $("#asource").value = ""; $("#aq").value = o.it.title;
    showTab("alerts", true);
  }
}
function gsAll() {
  const q = gs.input.value.trim();
  gsLog(q, "all breaches");
  gsClose(); gs.input.blur();
  Object.assign(vm, { q, group: "", sector: "", mark: "", page: 0, newOnly: false });
  $("#vq").value = q; $("#vgroup").value = ""; $("#vsector").value = ""; $("#vmark").value = ""; $("#vnew").checked = false;
  if (region) { region = ""; $("#region").value = ""; applyRegionChange(false); }          // the search covered the whole UK: show it all (this view only, the saved region is untouched)
  showTab("victims", true);
}
gs.input.addEventListener("input", () => {
  gs.clear.hidden = !gs.input.value;
  const q = gs.input.value.trim();
  if (q.length >= 2) gsLog(q);
  gsRun();
});
gs.input.addEventListener("focus", () => {
  const q = gs.input.value.trim();
  if (q.length >= 2 && gs.data && gs.data.q === q) gsRender(gs.data);
  else if (q.length >= 2) gsRun();                  // the list opens when the results arrive
  else gsRender(null);
});
gs.input.addEventListener("keydown", (e) => {
  const n = gs.opts.length;
  if (e.key === "Escape") { if (!gs.pop.hidden) { e.preventDefault(); e.stopPropagation(); gsClose(); } return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (gs.pop.hidden) { gsRender(gs.data && !gs.data.short ? gs.data : null); return; }
    if (n) gsActive(e.key === "ArrowDown" ? (gs.active + 1) % n : (gs.active <= 0 ? n - 1 : gs.active - 1), true);
  } else if (e.key === "Home" && n && !gs.pop.hidden && gs.active >= 0) { e.preventDefault(); gsActive(0, true); }
  else if (e.key === "End" && n && !gs.pop.hidden && gs.active >= 0) { e.preventDefault(); gsActive(n - 1, true); }
  else if (e.key === "Enter") {
    e.preventDefault();
    if (gs.active >= 0) gsPick(gs.active);
    else if (n && gs.data && gs.data.q === gs.input.value.trim()) gsPick(0);       // Enter with nothing highlighted takes the best match
  }
});
gs.list.addEventListener("mousedown", (e) => e.preventDefault());            // keep the focus in the box so the click is not lost
gs.foot.addEventListener("mousedown", (e) => e.preventDefault());
gs.list.addEventListener("click", (e) => { const o = e.target.closest(".gs-o"); if (o) gsPick(+o.dataset.i); });
gs.list.addEventListener("mousemove", (e) => { const o = e.target.closest(".gs-o"); if (o && +o.dataset.i !== gs.active) gsActive(+o.dataset.i); });
gs.foot.addEventListener("click", (e) => { if (e.target.closest("[data-act=all]")) gsAll(); });
gs.clear.addEventListener("click", () => { gs.input.value = ""; gs.clear.hidden = true; gs.input.focus(); gsRun(); });
document.addEventListener("pointerdown", (e) => { gs.inside = gs.box.contains(e.target); if (!gs.pop.hidden && !gs.inside) gsClose(); });
document.addEventListener("pointerup", () => setTimeout(() => { gs.inside = false; if (!gs.pop.hidden && !gs.box.contains(document.activeElement)) gsClose(); }, 60));       // and if focus left while the pointer was down, close now
gs.box.addEventListener("focusout", () => setTimeout(() => { if (!gs.inside && !gs.box.contains(document.activeElement)) gsClose(); }, 0));      // Tab away closes it; a tap inside does not
const gsFocus = () => { if (window.scrollY > 0) window.scrollTo(0, 0); gs.input.focus({ preventScroll: true }); gs.input.select(); };       // the tab row scrolls away: come back to it (it would land under the pinned header)

// keyboard: "/" jumps to the search box of the current tab (or the top-bar search), Ctrl/Cmd+K to the top-bar search from anywhere, Esc closes dialogs
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if (!$("#fdlg").hidden) closeFlag(); else if (!drawer.hidden) closeDrawer(); else if (!modal.hidden) closeModal(); return; }
  if (!obEl.hidden) return;
  if ((e.key === "k" || e.key === "K") && (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey) { e.preventDefault(); gsFocus(); return; }
  if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || /^(input|textarea|select)$/i.test(e.target.tagName)) return;
  const box = { victims: "#vq", news: "#nq", gangs: "#gq", alerts: "#aq", activity: "#act-q" }[activeTab()];
  e.preventDefault();
  if (box) $(box).focus(); else gsFocus();
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

async function openFromLink() {                // a link in an alert email is /?open=<breach name>
  const want = new URLSearchParams(location.search).get("open");
  if (!want) return;
  history.replaceState(null, "", location.pathname + location.hash);
  try {
    const v = await fetchVictim(want.slice(0, 200));
    if (v) { openVictim(v); track("victim_view", `${v.post_title} (from an alert email)`); } else toast("That breach is no longer on the dashboard");
  } catch (e) { toast("Could not open that breach"); }
}
async function init() {
  try { ME = await api("/api/me"); } catch (e) { return; }
  if (ME.database) loadMarkerTypes();
  if (ME.theme && ME.theme !== curTheme()) setTheme(ME.theme);      // the look chosen on the account wins over this browser's last setting
  if (ME.must_change) { showPwReq(); return; }        // nothing else works until the password has been changed
  const h = readHash();
  region = ME.region || "";
  if (h.region && ME.regions.includes(h.region)) region = h.region;        // a shared link filters the view without changing your saved region
  buildRegionSelect();
  if (!ME.onboarded) startOnboarding();
  if (ME.role === "admin" && ME.database) document.querySelectorAll(".admin-only").forEach((el) => { el.hidden = false; });
  tabFade();
  $("#usermenu").title = `Account: ${ME.username}`;
  // record this visit first so "new since your last visit" is measured against the previous one
  let refreshing = false;
  try { const o = await api("/api/open", { method: "POST" }); ME.prev_visit = o.prev_visit; refreshing = o.refreshing; } catch (e) { /* offline: show cached data */ }
  openFromLink();
  if (h.tab && document.querySelector(`.tab[data-tab="${h.tab}"]`) && h.tab !== "overview") { showTab(h.tab, false); track("tab_view", `${h.tab} (shared link)`); } else { updateFilterBar(); syncHash(); loadOverview(); }
  loadHealth(); loadTicker();
  if (refreshing) await waitForRefresh();
  await loadHealth();
  refreshCurrent(); loadTicker(); trackPlacing();
}
init();
