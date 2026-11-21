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
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// ------------------------------------------------------------------ theme: dark (default) or inverted light, remembered per browser
const root = document.documentElement;
function setTheme(t) {
  if (t === "light") root.dataset.theme = "light"; else delete root.dataset.theme;
  try { localStorage.setItem("evileye-theme", t); } catch (e) { /* storage unavailable */ }
  const b = document.getElementById("theme");
  if (b) {
    b.setAttribute("aria-pressed", String(t === "light"));
    b.title = t === "light" ? "Switch back to the dark theme" : "Invert colour scheme (light theme)";
  }
}
document.getElementById("theme").addEventListener("click", () => setTheme(root.dataset.theme === "light" ? "dark" : "light"));
setTheme(root.dataset.theme === "light" ? "light" : "dark");

let ME = null;       // { username, role, region, database, regions }
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
// what you did is logged for the admin (tab views, searches, filters, copies); fire and forget
function track(event, detail) {
  fetch("/api/activity", { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: JSON.stringify({ event, detail: String(detail ?? "").slice(0, 200) }) }).catch(() => {});
}
const rq = (extra = {}) => {
  const p = new URLSearchParams(extra);
  if (region) p.set("rocu", region);
  return p;
};

// ------------------------------------------------------------------ tabs
const TABS = { overview: loadOverview, victims: loadVictims, posts: loadPosts, gangs: loadGangs, alerts: loadAlerts, users: loadUsers, activity: loadActivity };
const activeTab = () => document.querySelector(".tab.active").dataset.tab;
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
    track("tab_view", btn.dataset.tab);
    refreshCurrent();
  });
});
function refreshCurrent() { (TABS[activeTab()] || loadOverview)(); }

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
  } catch (e) {
    $("#last-updated").innerHTML = '<span class="err">offline</span>';
    return {};
  }
}

// ------------------------------------------------------------------ region selector (the header filter)
function buildRegionSelect() {
  $("#region").innerHTML = '<option value="">All UK</option>' + ME.regions.map((r) => `<option>${esc(r)}</option>`).join("");
  $("#region").value = region;
}
$("#region").addEventListener("change", async () => {
  region = $("#region").value;
  try { await api("/api/me/region", { method: "PUT", body: { region } }); } catch (e) { toast("Could not save your region: " + e.message); }
  ME.region = region;
  loadTicker();
  refreshCurrent();
});

// ------------------------------------------------------------------ ticker (reads the server's cache; no external calls)
let tickerSeen = new Set();
const sectorShort = (s) => (s && s !== "Unclassified" ? s : "");
function tickerItem(x, showRegion, fresh) {
  const meta = [`<span class="badge grp">${esc(x.group)}</span>`, esc(sectorShort(x.sector)), ago(x.seen)].filter(Boolean).join(" · ");
  const reg = showRegion ? `<span class="badge ${x.rocu === "Unplaced" ? "src" : "uk"}">${esc(x.rocu === "Unplaced" ? "region unknown" : x.rocu)}</span>` : "";
  return `<li class="${fresh ? "fresh" : ""}"><div class="tk-title">${esc(x.title)} ${reg}</div><div class="meta">${meta}</div></li>`;
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
function pickRegion(r) {
  $("#region").value = r;
  $("#region").dispatchEvent(new Event("change"));
}
$("#chart-regions").addEventListener("click", (e) => {
  const t = e.target.closest(".rtile");
  if (t) pickRegion(t.dataset.region === region ? "" : t.dataset.region);   // click the selected tile again to clear
});
$("#region-clear").addEventListener("click", () => pickRegion(""));
// spread the tiles evenly over as few rows as fit (e.g. 14 tiles -> 7 + 7, not 8 + 6)
function balanceStrip() {
  const el = $("#chart-regions"), n = el.children.length;
  if (!n || !el.clientWidth) return;
  const fit = Math.max(1, Math.floor((el.clientWidth + 10) / 142));
  const rows = Math.ceil(n / fit);
  el.style.setProperty("--cols", Math.ceil(n / rows));
}
window.addEventListener("resize", debounce(balanceStrip, 150));
async function loadOverview() {
  const holder = $("#stat-cards");
  const [y, g, s, rg] = ["chart-year", "chart-gangs", "chart-sectors", "chart-regions"].map((i) => $(`#${i}`));
  y.innerHTML = g.innerHTML = s.innerHTML = '<div class="ldr">loading…</div>';
  try {
    const d = await api(`/api/summary?${rq()}`);
    holder.innerHTML = [
      [region ? `${region} victims` : "UK victims", d.uk_total, region ? "hl" : ""],
      ["UK victims (all regions)", d.uk_all, ""],
      ["Recent attacks tracked", d.attacks_total, ""],
      ["Gangs tracked", d.gangs_total, ""],
    ].map(([label, num, cls]) => `<div class="stat ${cls}"><div class="num">${num ?? 0}</div><div class="label">${label}</div></div>`).join("");
    drawBars(y, d.by_year.map((r) => [r.year, r.count]));
    drawBars(g, d.top_gangs.map((r) => [r.group, r.count]));
    drawBars(s, d.top_sectors.map((r) => [r.sector, r.count]));
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
    const p = d.placement;
    $("#region-note").innerHTML = `<strong>${p.placed}</strong> of ${p.total} victims placed to a UK region (${p.from_text} from place names, ${p.from_postcode} postcodes, ${p.from_companies_house} Companies House, ${p.from_manual} set manually). ` +
      `${p.overseas} look overseas / not UK, ${p.unplaced} still unplaced. ${p.api_enabled ? "" : "Companies House lookup is off: set COMPANIES_HOUSE_KEY. "}${p.running ? "<em>Looking up regions now…</em>" : ""}`;
  } catch (e) {
    holder.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`;
  }
}

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
const modal = document.getElementById("modal");
let modalKind = "sector";
function openModal() { modal.hidden = false; }
function closeModal() {
  modal.hidden = true;
  $("#modal-back").hidden = true;
  document.querySelectorAll(".more[data-detail]").forEach((b) => b.classList.remove("on"));
}
async function showBreakdown(kind) {
  modalKind = kind;
  openModal();
  track("modal_open", `breakdown ${kind}`);
  $("#modal-back").hidden = true;
  $("#modal-title").textContent = "Loading…";
  $("#modal-explain").textContent = "";
  $("#modal-body").innerHTML = '<div class="ldr">loading…</div>';
  const d = await api(`/api/summary/detail?${rq({ kind })}`);
  $("#modal-title").textContent = d.title;
  $("#modal-explain").textContent = "Select a row for a closer breakdown.";
  $("#modal-body").innerHTML = '<div class="bars"></div>';
  drawBars($("#modal-body .bars"), d.items.map((r) => [r.label, r.count, r.share]));
  $("#modal-body").querySelectorAll(".bar-row").forEach((row, i) => {
    row.classList.add("pick");
    row.addEventListener("click", () => showSlice(kind, d.items[i].label));
  });
}
async function showSlice(kind, label) {
  openModal();
  $("#modal-back").hidden = false;
  $("#modal-title").textContent = label;
  $("#modal-explain").textContent = "Loading…";
  const body = $("#modal-body");
  body.innerHTML = '<div class="ldr">loading…</div>';
  const d = await api(`/api/summary/slice?${rq({ kind, label })}`);
  $("#modal-title").textContent = d.label;
  $("#modal-explain").textContent = `${d.explain} ${d.count} victims, ${d.share}% of the current selection.`;
  body.innerHTML = `<p class="hint">${esc(d.split_title)}</p><div class="bars"></div><div class="slice-list"></div>`;
  drawBars(body.querySelector(".bars"), d.split.map((r) => [r.label, r.count]));
  body.querySelector(".slice-list").innerHTML = d.victims.length
    ? d.victims.map((v) => `
      <div class="vc">
        <div class="name">${esc(v.post_title)}</div>
        <div class="meta"><span class="badge grp">${esc(v.group || "unknown")}</span> · ${esc(v.activity)} · ${fmtDate(v.discovered)} · ${esc(v.rocu === "Unplaced" ? "region unknown" : v.rocu)}</div>
        ${v.website ? `<div class="meta">${esc(v.website)}</div>` : ""}
      </div>`).join("")
    : '<div class="ldr">no victims in this slice</div>';
}
$("#tab-overview").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-detail]");
  if (!btn) return;
  document.querySelectorAll(".more[data-detail]").forEach((b) => b.classList.toggle("on", b === btn));
  try { await showBreakdown(btn.dataset.detail); }
  catch (err) {
    openModal();
    $("#modal-title").textContent = "Breakdown";
    $("#modal-body").innerHTML = `<div class="ldr">failed to load: ${esc(err.message)}</div>`;
  }
});
$("#modal-close").addEventListener("click", closeModal);
$("#modal-back").addEventListener("click", () => showBreakdown(modalKind));
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) closeModal(); });

// ------------------------------------------------------------------ victims
let lastVictims = [];
const canEditRegion = () => ME && ME.role === "admin" && ME.database;
function regionBadge(v) {
  const edit = canEditRegion() ? `<div><button type="button" class="more small" data-edit="${esc(v.post_title)}">Edit region</button></div>` : "";
  const manual = v.region_source === "manual" ? ` · set manually${v.override_note ? `: ${esc(v.override_note)}` : ""}` : "";
  if (v.rocu === "Unplaced") return `<span class="badge src">region unknown</span>${edit}`;
  if (v.rocu === "Overseas / not UK") {
    return `<span class="badge off">Overseas / not UK</span><div class="meta">${v.region_source === "manual" ? `set manually${v.override_note ? `: ${esc(v.override_note)}` : ""}` : "probably based abroad"}</div>${edit}`;
  }
  return `<span class="badge uk">${esc(v.rocu)}</span><div class="meta">${esc(v.force_name)}${v.region_source && !manual ? ` · by ${esc(v.region_source.replace("-", " "))}` : ""}${manual}</div>${edit}`;
}
async function loadVictims() {
  const tbody = $("#victim-rows");
  const q = $("#vq").value.trim(), group = $("#vgroup").value, sector = $("#vsector").value;
  tbody.innerHTML = '<tr><td colspan="5" class="ldr">loading…</td></tr>';
  const extra = { limit: "200", offset: "0" };
  if (q) extra.q = q;
  if (group) extra.group = group;
  if (sector) extra.sector = sector;
  try {
    const d = await api(`/api/victims?${rq(extra)}`);
    $("#vcount").textContent = `${d.total} UK victims${region ? ` in ${region}` : ""} (showing first 200)`;
    lastVictims = d.items;
    fillFacets(d.facets);
    tbody.innerHTML = d.items.length
      ? d.items.map((v) => `
        <tr>
          <td><strong>${esc(v.post_title)}</strong>
            ${/^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(\/.*)?$/i.test(v.website || "") ? `<div class="meta"><a href="https://${esc(v.website)}" target="_blank" rel="noopener noreferrer">${esc(v.website)}</a></div>` : ""}
            ${v.registered_name ? `<div class="meta">${esc(v.registered_name)}</div>` : ""}</td>
          <td><span class="badge grp">${esc(v.group)}</span></td>
          <td>${esc(v.activity || "—")}</td>
          <td>${fmtDate(v.discovered)}</td>
          <td>${regionBadge(v)}</td>
        </tr>`).join("")
      : '<tr><td colspan="5" class="ldr">no matches</td></tr>';
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="ldr">failed to load: ${esc(e.message)}</td></tr>`;
  }
}
let facetsFilled = false;
function fillFacets(f) {
  // the lists come from the full UK dataset, not just the 200 rows on screen
  if (facetsFilled || !f) return;
  $("#vgroup").innerHTML = '<option value="">All groups</option>' + f.groups.map((g) => `<option>${esc(g)}</option>`).join("");
  $("#vsector").innerHTML = '<option value="">All sectors</option>' + f.sectors.map((s) => `<option>${esc(s)}</option>`).join("");
  facetsFilled = true;
}

$("#victim-rows").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-edit]");
  if (!b) return;
  const v = lastVictims.find((x) => x.post_title === b.dataset.edit);
  if (!v) return;
  openModal();
  $("#modal-back").hidden = true;
  $("#modal-title").textContent = `Region: ${v.post_title}`;
  $("#modal-explain").textContent = `Currently ${v.rocu === "Unplaced" ? "unplaced" : v.rocu}${v.region_source ? ` (by ${v.region_source.replace("-", " ")})` : ""}. A manual placement overrides every automatic method and survives refreshes.`;
  const opts = ME.regions.filter((r) => r !== "Unplaced");
  $("#modal-body").innerHTML = `<form id="plform" class="form-row">
      <label class="field">Region<select id="pl-rocu" class="search">${opts.map((r) => `<option ${r === v.rocu ? "selected" : ""}>${esc(r)}</option>`).join("")}</select></label>
      <label class="field">Note (optional)<input id="pl-note" class="search" maxlength="200" value="${esc(v.override_note || "")}" placeholder="e.g. HQ confirmed on company website"></label>
      <button class="btn" type="submit">Save</button>
      ${v.region_source === "manual" ? '<button class="more danger" type="button" id="pl-clear">Remove manual placement</button>' : ""}
    </form><p id="pl-msg" class="hint"></p>`;
  const done = () => { closeModal(); loadVictims(); loadTicker(); toast("Region updated"); };
  $("#plform").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try { await api("/api/admin/placement", { method: "PUT", body: { title: v.post_title, rocu: $("#pl-rocu").value, note: $("#pl-note").value } }); done(); }
    catch (err) { $("#pl-msg").textContent = err.message; }
  });
  const clr = $("#pl-clear");
  if (clr) clr.addEventListener("click", async () => {
    try { await api(`/api/admin/placement?title=${encodeURIComponent(v.post_title)}`, { method: "DELETE" }); done(); }
    catch (err) { $("#pl-msg").textContent = err.message; }
  });
});

// ------------------------------------------------------------------ feed
async function loadPosts() {
  const list = $("#post-list");
  const q = $("#pq").value.trim();
  list.innerHTML = '<div class="ldr">loading…</div>';
  const extra = { limit: "250" };
  if (q) extra.q = q;
  try {
    const d = await api(`/api/posts?${rq(extra)}`);
    $("#pcount").textContent = `Aggregated feed (ransomware.live · RansomLook)${region ? ` — ${region} only` : ""} — showing ${d.items.length}`;
    list.innerHTML = d.items.length
      ? d.items.map((p) => `
        <div class="post">
          <div>
            <div class="ptitle">${esc(p.title)} ${p.rocu && p.rocu !== "Unplaced" ? `<span class="badge uk">${esc(p.rocu)}</span>` : ""}</div>
            <div class="pdetail"><span class="badge src">${esc(p.source)}</span> <span class="badge grp">${esc(p.group || "unknown")}</span> ${p.detail ? esc(p.detail) : ""}</div>
          </div>
          <div class="right"><div class="pdate">${fmtDate(p.date)}</div>${p.url ? `<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">view→</a>` : ""}</div>
        </div>`).join("")
      : `<div class="ldr">${region ? `no recent posts matched to ${esc(region)}` : "no posts"}</div>`;
  } catch (e) {
    list.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------ gangs
async function loadGangs() {
  const grid = $("#gang-grid");
  const q = $("#gq").value.trim(), source = $("#gsource").value, sort = $("#gsort").value;
  const live = document.querySelector('input[name="glive"]:checked')?.value === "1";
  grid.innerHTML = '<div class="ldr">loading…</div>';
  const extra = {};
  if (q) extra.q = q;
  if (source) extra.source = source;
  if (sort && sort !== "name") extra.sort = sort;
  if (live) extra.live = "1";
  try {
    const d = await api(`/api/gangs?${rq(extra)}`);
    $("#gcount").textContent = `${d.total} entries ${live ? "with a live leak site" : "in catalog"}${sort === "active" ? ` · ranked by ${region || "UK"} victim posts` : ""}`;
    grid.innerHTML = d.items.length
      ? d.items.map((g) => {
          const sites = (g.leak_sites || []).map((s) => {
            const ok = s.available === true || s.status === "ONLINE";
            const addr = s.slug || s.fqdn || "";
            return `<div class="site-line"><span class="dot ${s.available === false || s.status === "OFFLINE" ? "bad" : ok ? "ok" : "na"}"></span>${addr ? `<button type="button" class="copy" data-copy="${esc(addr)}" data-gang="${esc(g.name)}" title="Click to copy. Never opened from here."><span class="addr">${esc(addr)}</span><span class="cpy">copy</span></button>` : "site"}</div>`;
          });
          const meta = [g.added_date ? `tracked since ${esc(g.added_date)}` : "", g.activity ? `${g.activity} UK victim${g.activity === 1 ? "" : "s"}${region ? ` in ${esc(region)}` : ""}` : ""].filter(Boolean);
          return `
        <div class="gc">
          <div class="gname">${esc(g.name)} ${(g.sources || []).map((s) => `<span class="badge src">${esc(s)}</span>`).join(" ")}</div>
          ${g.altname ? `<div class="meta"><span class="badge uk">${esc(g.altname)}</span></div>` : ""}
          ${g.description ? `<div class="gdesc" title="${esc(g.description)}">${esc(g.description.slice(0, 180))}${g.description.length > 180 ? "…" : ""}</div>` : ""}
          ${sites.length ? `<div class="sites">${sites.join("")}</div>` : ""}
          <div class="meta">${meta.join(" · ")}</div>
        </div>`;
        }).join("")
      : '<div class="ldr">no gangs match these filters</div>';
  } catch (e) {
    grid.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------ alerts & news
async function loadAlerts() {
  const list = $("#alert-list");
  const q = $("#aq").value.trim(), source = $("#asource").value;
  list.innerHTML = '<div class="ldr">loading…</div>';
  const p = new URLSearchParams({ limit: "150" });
  if (q) p.set("q", q);
  if (source) p.set("source", source);
  try {
    const d = await api(`/api/intel?${p}`);
    $("#acount").textContent = `${d.total} items (national: not filtered by region)`;
    list.innerHTML = d.items.length
      ? d.items.map((item) => `
        <div class="post">
          <div>
            <div class="ptitle">${esc(item.title)}</div>
            <div class="pdetail"><span class="badge src">${esc(item.source)}</span> <span class="badge grp">${esc(item.kind)}</span>
              ${item.extra?.cve ? esc(item.extra.cve) + " " : ""}${item.extra?.vendor ? esc(item.extra.vendor) + " " : ""}${item.summary ? esc(item.summary.slice(0, 220)) : ""}</div>
          </div>
          <div class="right"><div class="pdate">${fmtDate(item.published)}</div>${item.url ? `<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">view→</a>` : ""}</div>
        </div>`).join("")
      : '<div class="ldr">no alerts yet — still syncing</div>';
  } catch (e) {
    list.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`;
  }
}

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
    toast("Copied to clipboard");
    track("copy_leak_site", b.dataset.gang || "");
    setTimeout(() => { b.classList.remove("done"); if (label) label.textContent = "copy"; }, 1400);
  } else toast("Copy failed: select the text manually");
});

// ------------------------------------------------------------------ admin: users
const genPassword = () => {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789-_";
  const a = new Uint32Array(18); crypto.getRandomValues(a);
  return [...a].map((n) => chars[n % chars.length]).join("");
};
let userCache = [];
async function loadUsers() {
  const body = $("#user-rows");
  body.innerHTML = '<tr><td colspan="7" class="ldr">loading…</td></tr>';
  try {
    const d = await api("/api/admin/users");
    userCache = d.users;
    const regionOpts = (sel) => '<option value="">All UK</option>' + d.regions.map((r) => `<option ${r === sel ? "selected" : ""}>${esc(r)}</option>`).join("");
    if ($("#u-region").options.length <= 1) $("#u-region").innerHTML = regionOpts("");
    body.innerHTML = d.users.map((u) => `
      <tr data-id="${u.id}">
        <td><strong>${esc(u.username)}</strong>${u.username === ME.username ? ' <span class="badge src">you</span>' : ""}<div class="meta">created ${fmtDate(u.created_at)}${u.created_by ? ` by ${esc(u.created_by)}` : ""}</div></td>
        <td><span class="badge ${u.role === "admin" ? "wm" : "uk"}">${esc(u.role)}</span></td>
        <td><select class="search u-reg" data-id="${u.id}" aria-label="Region for ${esc(u.username)}">${regionOpts(u.region)}</select></td>
        <td>${u.active ? '<span class="badge on">active</span>' : '<span class="badge off">disabled</span>'}</td>
        <td>${u.last_login ? fmtTime(u.last_login) : '<span class="meta">never</span>'}</td>
        <td>${u.events}</td>
        <td class="actions">
          <button class="more" data-act="pw" data-id="${u.id}">Reset password</button>
          <button class="more" data-act="role" data-id="${u.id}">${u.role === "admin" ? "Make user" : "Make admin"}</button>
          <button class="more" data-act="active" data-id="${u.id}">${u.active ? "Disable" : "Enable"}</button>
          <button class="more danger" data-act="del" data-id="${u.id}">Delete</button>
        </td>
      </tr>`).join("");
  } catch (e) {
    body.innerHTML = `<tr><td colspan="7" class="ldr">${esc(e.message)}</td></tr>`;
  }
}
$("#u-gen").addEventListener("click", () => { $("#u-pass").value = genPassword(); });
$("#uform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#u-msg");
  try {
    const name = $("#u-name").value.trim(), pw = $("#u-pass").value;
    await api("/api/admin/users", { method: "POST", body: { username: name, password: pw, role: $("#u-role").value, region: $("#u-region").value } });
    msg.innerHTML = `Created <strong>${esc(name)}</strong>. Give them this password now (it cannot be shown again): <code>${esc(pw)}</code>`;
    $("#u-name").value = ""; $("#u-pass").value = "";
    loadUsers();
  } catch (err) { msg.textContent = err.message; }
});
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
  else if (b.dataset.act === "del") {
    if (confirm(`Delete ${u.username}? Their past activity stays in the log.`)) {
      api(`/api/admin/users/${u.id}`, { method: "DELETE" }).then(loadUsers).catch((err) => toast(err.message));
    }
  } else if (b.dataset.act === "pw") passwordDialog(u);
});
function passwordDialog(u) {
  openModal();
  $("#modal-back").hidden = true;
  $("#modal-title").textContent = `Reset password: ${u.username}`;
  $("#modal-explain").textContent = "Sets a new password and signs them out everywhere. Tell them the new password yourself.";
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
  if (more !== true) { actOffset = 0; body.innerHTML = '<tr><td colspan="5" class="ldr">loading…</td></tr>'; }
  try {
    const d = await api(`/api/admin/activity?${actParams({ limit: "100", offset: String(actOffset) })}`);
    if ($("#act-user").options.length <= 1) {
      $("#act-user").innerHTML = '<option value="">All users</option>' + d.users.map((u) => `<option>${esc(u)}</option>`).join("");
      $("#act-event").innerHTML = '<option value="">All events</option>' + d.events.map((u) => `<option>${esc(u)}</option>`).join("");
    }
    const rows = d.items.map((r) => `<tr><td>${fmtTime(r.ts)}</td><td>${esc(r.username || "—")}</td><td><span class="badge ${/failed|delete|locked/.test(r.event + r.detail) ? "off" : "src"}">${esc(r.event)}</span></td><td>${esc(r.detail)}</td><td class="meta">${esc(r.ip)}</td></tr>`).join("");
    body.innerHTML = (more === true ? body.innerHTML : "") + (rows || '<tr><td colspan="5" class="ldr">no activity matches</td></tr>');
    actOffset += d.items.length;
    $("#act-count").textContent = `${d.total} events (showing ${actOffset})`;
    $("#act-more").hidden = actOffset >= d.total;
    $("#act-csv").href = `/api/admin/activity.csv?${actParams()}`;
  } catch (e) {
    body.innerHTML = `<tr><td colspan="5" class="ldr">${esc(e.message)}</td></tr>`;
  }
}
$("#act-refresh").addEventListener("click", () => loadActivity());
$("#act-more").addEventListener("click", () => loadActivity(true));
["act-user", "act-event"].forEach((id) => $(`#${id}`).addEventListener("change", () => loadActivity()));
$("#act-q").addEventListener("input", debounce(() => loadActivity(), 350));

// ------------------------------------------------------------------ account
$("#usermenu").addEventListener("click", () => {
  openModal();
  $("#modal-back").hidden = true;
  $("#modal-title").textContent = ME.username;
  $("#modal-explain").textContent = `${ME.role === "admin" ? "Administrator" : "User"} · ${ME.database ? "activity is logged" : "database not connected"}`;
  $("#modal-body").innerHTML = ME.database ? `<h3 class="sub">Change password</h3>
    <form id="mypw" class="form-row">
      <label class="field">Current password<input id="pw-cur" class="search" type="password" required autocomplete="current-password"></label>
      <label class="field">New password (10+ characters)<input id="pw-new" class="search" type="password" minlength="10" required autocomplete="new-password"></label>
      <label class="field">Repeat new password<input id="pw-rep" class="search" type="password" minlength="10" required autocomplete="new-password"></label>
      <button class="btn" type="submit">Change password</button></form><p id="mypw-msg" class="hint"></p>`
    : '<p class="hint">The user database is not connected, so accounts and passwords are unavailable. Set DATABASE_URL.</p>';
  const f = $("#mypw");
  if (f) f.addEventListener("submit", async (e) => {
    e.preventDefault();
    if ($("#pw-new").value !== $("#pw-rep").value) { $("#mypw-msg").textContent = "The new passwords do not match."; return; }
    try {
      await api("/api/me/password", { method: "POST", body: { current: $("#pw-cur").value, new: $("#pw-new").value } });
      $("#mypw-msg").textContent = "Password changed.";
      f.reset();
    } catch (err) { $("#mypw-msg").textContent = err.message; }
  });
});

// ------------------------------------------------------------------ search / filter wiring (with activity tracking)
const trackedSearch = (id, label, fn) => $(`#${id}`).addEventListener("input", debounce(() => {
  fn();
  const v = $(`#${id}`).value.trim();
  if (v) track("search", `${label}: ${v}`);
}, 400));
trackedSearch("vq", "victims", loadVictims);
trackedSearch("pq", "feed", loadPosts);
trackedSearch("gq", "gangs", loadGangs);
trackedSearch("aq", "alerts", loadAlerts);
[["vgroup", "victims group", loadVictims], ["vsector", "victims sector", loadVictims], ["gsource", "gangs source", loadGangs],
 ["gsort", "gangs sort", loadGangs], ["asource", "alerts source", loadAlerts]].forEach(([id, label, fn]) =>
  $(`#${id}`).addEventListener("change", () => { fn(); track("filter", `${label}: ${$(`#${id}`).value || "all"}`); }));
document.querySelectorAll('input[name="glive"]').forEach((el) => el.addEventListener("change", () => { loadGangs(); track("filter", `gangs live only: ${el.value === "1"}`); }));
$("#vrefresh").addEventListener("click", loadVictims);
$("#prefresh").addEventListener("click", loadPosts);
$("#grefresh").addEventListener("click", loadGangs);
$("#arefresh").addEventListener("click", loadAlerts);

// ------------------------------------------------------------------ keep-screen-on toggle
(function () {
  const btn = $("#awake");
  let lock = null;
  let want = false;
  try { want = localStorage.getItem("evileye-awake") === "1"; } catch (e) { /* storage unavailable */ }
  const store = (on) => { try { on ? localStorage.setItem("evileye-awake", "1") : localStorage.removeItem("evileye-awake"); } catch (e) { /* ignore */ } };
  function paint() {
    btn.classList.toggle("on", want && !!lock);
    btn.setAttribute("aria-pressed", want && lock ? "true" : "false");
    btn.querySelector(".awake-label").textContent = want && lock ? "Screen kept on" : "Keep screen on";
  }
  async function acquire() {
    if (!navigator.wakeLock) { btn.title = "This browser cannot keep the screen awake."; want = false; store(false); paint(); return; }
    try {
      lock = await navigator.wakeLock.request("screen");
      lock.addEventListener("release", () => { lock = null; paint(); });
    } catch (err) { lock = null; btn.title = err.message || "Wake lock was denied."; }
    paint();
  }
  async function setAwake(on) {
    want = on; store(on);
    if (!on && lock) { await lock.release(); lock = null; }
    if (on) await acquire(); else paint();
  }
  btn.addEventListener("click", () => setAwake(!(want && lock)));
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && want) acquire(); });
  if (want) acquire(); else paint();
})();

// ------------------------------------------------------------------ refresh policy
// External feeds are fetched by the server only when the page is opened (if stale) or Refresh is pressed.
async function waitForRefresh() {
  for (let i = 0; i < 40; i++) {
    const h = await loadHealth();
    if (!h.refreshing) return;
    await new Promise((res) => setTimeout(res, 3000));
  }
}
// Region lookups run on the server after an open or Refresh. While that one job runs we re-read the
// screen every 20s, then stop. No polling otherwise.
let tracking = false;
async function trackPlacing() {
  if (tracking) return;
  tracking = true;
  try {
    await new Promise((res) => setTimeout(res, 2000));
    for (let i = 0; i < 60; i++) {
      const h = await loadHealth();
      if (["overview", "victims", "posts"].includes(activeTab())) refreshCurrent();
      loadTicker();
      if (!h.placing) break;
      await new Promise((res) => setTimeout(res, 20000));
    }
  } finally { tracking = false; }
}
$("#refresh-now").addEventListener("click", async () => {
  const btn = $("#refresh-now");
  btn.disabled = true; btn.textContent = "Refreshing";
  try { await api("/api/refresh", { method: "POST" }); } catch (err) { btn.title = err.message || "Refresh failed"; }
  await waitForRefresh();
  btn.disabled = false; btn.textContent = "Refresh";
  refreshCurrent(); loadTicker(); trackPlacing();
});

async function init() {
  try { ME = await api("/api/me"); } catch (e) { return; }
  region = ME.region || "";
  buildRegionSelect();
  $("#usermenu").textContent = ME.username;
  if (ME.role === "admin" && ME.database) document.querySelectorAll(".admin-only").forEach((el) => { el.hidden = false; });
  loadHealth(); loadOverview(); loadTicker();
  try {
    const o = await api("/api/open", { method: "POST" });
    if (o.refreshing) await waitForRefresh();
  } catch (e) { /* offline: show cached data */ }
  await loadHealth();
  refreshCurrent(); loadTicker(); trackPlacing();
}
init();
