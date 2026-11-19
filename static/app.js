const $ = (sel) => document.querySelector(sel);
const fmtDate = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toUTCString().slice(5, 16);
};
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ------------------------------------------------------------------ tabs
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".panel").forEach((p) => {
      p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`);
    });
    refreshCurrent();
  });
});

let loadedTab = null;
function refreshCurrent() {
  const active = document.querySelector(".tab.active").dataset.tab;
  if (active === "overview") loadOverview();
  else if (active === "wmidlands") loadWm();
  else if (active === "victims") loadVictims();
  else if (active === "posts") loadPosts();
  else if (active === "gangs") loadGangs();
  else if (active === "alerts") loadAlerts();
}

async function api(path) {
  const r = await fetch(path);
  if (r.status === 401) {
    location.href = "/login";
    throw new Error("Unauthorized");
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function loadHealth() {
  try {
    const h = await api("/health");
    const errs = h.errors;
    const last = h.last_refresh?.rl_victims_gb || h.last_refresh?.rl_victims_gb;
    const el = $("#last-updated");
    if (last) {
      const when = new Date(last + (last.includes("Z") || /[+-]\d\d:\d\d$/.test(last) ? "" : "Z"));
      const mins = Math.max(0, Math.round((Date.now() - when.getTime()) / 60000));
      const ago = mins < 60 ? `${mins} min ago` : `${Math.round(mins / 60)}h ago`;
      el.innerHTML = h.refreshing
        ? `<span class="ok">refreshing…</span>`
        : `<span class="ok">synced ${ago}</span>`;
    } else if (errs) {
      el.innerHTML = `<span class="err">ingest error on first sync; retrying…</span>`;
    } else {
      el.innerHTML = `<span class="ok">connected</span>`;
    }
  } catch {
    $("#last-updated").innerHTML = `<span class="err">offline</span>`;
  }
}

// ------------------------------------------------------------------ overview
async function loadOverview() {
  const holder = $("#stat-cards");
  const [y, g, s] = ["chart-year", "chart-gangs", "chart-sectors"].map((i) => $(`#${i}`));
  y.innerHTML = g.innerHTML = s.innerHTML = '<div class="ldr">loading…</div>';
  try {
    const d = await api("/api/summary");
    holder.innerHTML = [
      ["UK victims", d.uk_total],
      ["West Midlands", d.wm_total, "hl"],
      ["GB attacks", d.attacks_total],
      ["Gangs tracked", d.gangs_total],
    ]
      .map(([label, num, cls]) => `<div class="stat ${cls || ""}"><div class="num">${num ?? 0}</div><div class="label">${label}</div></div>`)
      .join("");
    drawBars(y, d.by_year.map((r) => [r.year, r.count]));
    drawBars(g, d.top_gangs.map((r) => [r.group, r.count]));
    drawBars(s, d.top_sectors.map((r) => [r.sector, r.count]));
  } catch (e) {
    holder.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`;
  }
}

function drawBars(holder, rows) {
  const max = Math.max(1, ...rows.map((r) => r[1]));
  holder.innerHTML = rows.length
    ? rows
        .map(([k, v, share]) => `
        <div class="bar-row">
          <div class="bk" title="${esc(k)}">${esc(k)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, (v / max) * 100)}%"></div></div>
          <div class="bv">${v}${share != null ? ` <span class="fine">${share}%</span>` : ""}</div>
        </div>`)
        .join("")
    : '<div class="ldr">no data yet — still syncing</div>';
}

const modal = document.getElementById("modal");
let modalKind = "sector";

function openModal() {
  modal.hidden = false;
}

function closeModal() {
  modal.hidden = true;
  document.getElementById("modal-back").hidden = true;
  document.querySelectorAll(".more[data-detail]").forEach((b) => b.classList.remove("on"));
}

async function showBreakdown(kind) {
  modalKind = kind;
  openModal();
  document.getElementById("modal-back").hidden = true;
  document.getElementById("modal-title").textContent = "Loading…";
  document.getElementById("modal-explain").textContent = "";
  const body = document.getElementById("modal-body");
  body.innerHTML = '<div class="ldr">loading…</div>';
  const d = await api(`/api/summary/detail?kind=${kind}`);
  document.getElementById("modal-title").textContent = d.title;
  document.getElementById("modal-explain").textContent = "Select a row for a closer breakdown.";
  body.innerHTML = '<div class="bars"></div>';
  drawBars(body.querySelector(".bars"), d.items.map((r) => [r.label, r.count, r.share]));
  body.querySelectorAll(".bar-row").forEach((row, i) => {
    row.classList.add("pick");
    row.addEventListener("click", () => showSlice(kind, d.items[i].label));
  });
}

async function showSlice(kind, label) {
  openModal();
  document.getElementById("modal-back").hidden = false;
  document.getElementById("modal-title").textContent = label;
  document.getElementById("modal-explain").textContent = "Loading…";
  const body = document.getElementById("modal-body");
  body.innerHTML = '<div class="ldr">loading…</div>';
  const d = await api(`/api/summary/slice?kind=${encodeURIComponent(kind)}&label=${encodeURIComponent(label)}`);
  document.getElementById("modal-title").textContent = d.label;
  document.getElementById("modal-explain").textContent = `${d.explain} ${d.count} victims, ${d.share}% of the UK list.`;
  const bars = document.createElement("div");
  bars.className = "bars";
  body.innerHTML = `<p class="hint">${esc(d.split_title)}</p>`;
  body.appendChild(bars);
  drawBars(bars, d.split.map((r) => [r.label, r.count]));
  const list = document.createElement("div");
  list.className = "slice-list";
  list.innerHTML = d.victims.length
    ? d.victims.map((v) => `
      <div class="vc">
        <div class="name">${esc(v.post_title)}</div>
        <div class="meta"><span class="badge grp">${esc(v.group || "unknown")}</span> · ${esc(v.activity || "sector unknown")} · ${fmtDate(v.discovered)}</div>
        ${v.website ? `<div class="meta">${esc(v.website)}</div>` : ""}
      </div>`).join("")
    : '<div class="ldr">no victims in this slice</div>';
  body.appendChild(list);
}

document.getElementById("tab-overview").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-detail]");
  if (!btn) return;
  document.querySelectorAll(".more[data-detail]").forEach((b) => b.classList.toggle("on", b === btn));
  try {
    await showBreakdown(btn.dataset.detail);
  } catch (err) {
    openModal();
    document.getElementById("modal-title").textContent = "Breakdown";
    document.getElementById("modal-explain").textContent = "";
    document.getElementById("modal-body").innerHTML = `<div class="ldr">failed to load: ${esc(err.message)}</div>`;
  }
});

document.getElementById("modal-close").addEventListener("click", closeModal);
document.getElementById("modal-back").addEventListener("click", () => showBreakdown(modalKind));
modal.addEventListener("click", (e) => {
  if (e.target === modal) closeModal();
});

// ------------------------------------------------------------------ west midlands
async function loadWm() {
  const list = $("#wm-list");
  list.innerHTML = '<div class="ldr">loading…</div>';
  try {
    const d = await api("/api/victims?region=west_midlands&limit=200");
    list.innerHTML = d.items.length
      ? d.items
          .map((v) => `
        <div class="vc">
          <div class="name">${esc(v.post_title)}</div>
          <div class="meta"><span class="badge grp">${esc(v.group)}</span> · ${esc(v.activity || "sector unknown")}</div>
          ${v.location ? `<div class="meta">Registered office: ${esc(v.location)}</div>` : ""}
          ${v.maps_location ? `<div class="meta">Google Maps: ${esc(v.maps_location)}</div>` : ""}
          ${v.registered_name ? `<div class="meta">${esc(v.registered_name)}</div>` : ""}
          ${v.region_hits?.length ? `<div class="hits">Matched: ${v.region_hits.map((h) => `<span class="badge wm">${esc(h)}</span>`).join(" ")}</div>` : ""}
          <div class="meta">Discovered: ${fmtDate(v.discovered)}</div>
          <div class="links">
            ${v.website ? `<a href="https://${esc(v.website)}" target="_blank" rel="noopener">website</a>` : ""}
            ${v.post_url ? `<a href="${esc(v.post_url)}" target="_blank" rel="noopener" class="warn-link">leak site post</a>` : ""}
          </div>
        </div>`)
          .join("")
      : '<div class="ldr">no West Midlands matches found in current feed</div>';
  } catch (e) {
    list.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------ victims
async function loadVictims() {
  const tbody = $("#victim-rows");
  const q = $("#vq").value.trim();
  const group = $("#vgroup").value;
  const sector = $("#vsector").value;
  tbody.innerHTML = '<tr><td colspan="5" class="ldr">loading…</td></tr>';
  const params = new URLSearchParams({ limit: "200", offset: "0" });
  if (q) params.set("q", q);
  if (group) params.set("group", group);
  if (sector) params.set("sector", sector);
  try {
    const d = await api(`/api/victims?${params}`);
    $("#vcount").textContent = `${d.total} UK victims (showing first 200)`;
    fillGroupSelect(d.items);
    fillSectorSelect(d.items);
    tbody.innerHTML = d.items.length
      ? d.items
          .map(
            (v) => `
        <tr>
          <td>
            <strong>${esc(v.post_title)}</strong>
            ${v.website ? `<div class="meta"><a href="https://${esc(v.website)}" target="_blank" rel="noopener">${esc(v.website)}</a></div>` : ""}
          </td>
          <td><span class="badge grp">${esc(v.group)}</span></td>
          <td>${esc(v.activity || "—")}</td>
          <td>${fmtDate(v.discovered)}</td>
          <td>${v.region === "west_midlands" ? `<span class="badge wm">West Midlands</span>` : `<span class="badge uk">UK</span>`}</td>
        </tr>`
          )
          .join("")
      : '<tr><td colspan="5" class="ldr">no matches</td></tr>';
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="ldr">failed to load: ${esc(e.message)}</td></tr>`;
  }
}

let groupSel = new Set(), sectorSel = new Set();
function fillGroupSelect(items) {
  items.forEach((i) => i.group && groupSel.add(i.group));
  if ($("#vgroup").options.length <= 1) {
    $("#vgroup").innerHTML = `<option value="">All groups</option>` + [...groupSel].sort().map((g) => `<option>${esc(g)}</option>`).join("");
  }
}
function fillSectorSelect(items) {
  items.forEach((i) => i.activity && i.activity !== "Not Found" && sectorSel.add(i.activity));
  if ($("#vsector").options.length <= 1) {
    $("#vsector").innerHTML = `<option value="">All sectors</option>` + [...sectorSel].sort().map((s) => `<option>${esc(s)}</option>`).join("");
  }
}

// ------------------------------------------------------------------ posts
async function loadPosts() {
  const list = $("#post-list");
  const q = $("#pq").value.trim();
  list.innerHTML = '<div class="ldr">loading…</div>';
  const params = new URLSearchParams({ limit: "250" });
  if (q) params.set("q", q);
  try {
    const d = await api(`/api/posts?${params}`);
    $("#pcount").textContent = `Aggregated feed (ransomware.live · ransomwatch · RansomLook) — showing ${d.items.length}`;
    list.innerHTML = d.items.length
      ? d.items
          .map(
            (p) => `
        <div class="post">
          <div>
            <div class="ptitle">${esc(p.title)} ${p.region === "west_midlands" ? `<span class="badge wm">WM</span>` : ""}</div>
            <div class="pdetail">
              <span class="badge src">${esc(p.source)}</span>
              <span class="badge grp">${esc(p.group || "unknown")}</span>
              ${p.detail ? esc(p.detail) : ""}
            </div>
          </div>
          <div class="right">
            <div class="pdate">${fmtDate(p.date)}</div>
            ${p.url ? `<a href="${esc(p.url)}" target="_blank" rel="noopener">view→</a>` : ""}
          </div>
        </div>`
          )
          .join("")
      : '<div class="ldr">no posts</div>';
  } catch (e) {
    list.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------ gangs
async function loadGangs() {
  const grid = $("#gang-grid");
  const q = $("#gq").value.trim();
  const source = $("#gsource").value;
  const sort = $("#gsort").value;
  const live = document.querySelector('input[name="glive"]:checked')?.value === "1";
  grid.innerHTML = '<div class="ldr">loading…</div>';
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (source) params.set("source", source);
  if (sort && sort !== "name") params.set("sort", sort);
  if (live) params.set("live", "1");
  try {
    const d = await api(`/api/gangs?${params}`);
    const rank = sort === "active" ? " · ranked by UK victim posts" : "";
    const scope = live ? "with a live leak site" : "in catalog";
    $("#gcount").textContent = `${d.total} entries ${scope} (gangs from ransomware.live + sources from deepdarkCTI)${rank}`;
    grid.innerHTML = d.items.length
      ? d.items
          .map((g) => {
            const sites = (g.leak_sites || []).map((s, i) => {
              const ok = s.available === true || s.status === "ONLINE";
              return `<div class="site-line"><span class="dot ${s.available === false || s.status === "OFFLINE" ? "bad" : ok ? "ok" : "na"}"></span>${s.slug ? `<a href="${esc(s.slug)}" target="_blank" rel="noopener">${esc(s.slug)}</a>` : esc(s.fqdn || "site")}</div>`;
            });
            const meta = [
              g.added_date ? `tracked since ${esc(g.added_date)}` : "",
              g.activity ? `${g.activity} UK victim${g.activity === 1 ? "" : "s"}` : "",
            ].filter(Boolean);
            return `
        <div class="gc">
          <div class="gname">${esc(g.name)} ${(g.sources || []).map((s) => `<span class="badge src">${esc(s)}</span>`).join(" ")}</div>
          ${g.altname ? `<div class="meta"><span class="badge uk">${esc(g.altname)}</span></div>` : ""}
          ${g.description ? `<div class="gdesc" title="${esc(g.description)}">${esc(g.description.slice(0, 180))}${g.description.length > 180 ? "…" : ""}</div>` : ""}
          ${sites.length ? `<div class="sites">${sites.join("")}</div>` : ""}
          <div class="meta">${meta.join(" · ")}</div>
        </div>`;
          })
          .join("")
      : '<div class="ldr">no gangs match these filters</div>';
  } catch (e) {
    grid.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------ wiring
["vq", "vgroup", "vsector"].forEach((id) => {
  const el = $(`#${id}`);
  el.addEventListener(el.tagName === "INPUT" ? "input" : "change", debounce(loadVictims, 300));
});
["pq"].forEach((id) => $(`#${id}`).addEventListener("input", debounce(loadPosts, 300)));
["gq"].forEach((id) => $(`#${id}`).addEventListener("input", debounce(loadGangs, 300)));
$("#gsource").addEventListener("change", loadGangs);
$("#gsort").addEventListener("change", loadGangs);
document.querySelectorAll('input[name="glive"]').forEach((el) => el.addEventListener("change", loadGangs));
$("#vrefresh").addEventListener("click", loadVictims);
$("#prefresh").addEventListener("click", loadPosts);
$("#grefresh").addEventListener("click", loadGangs);

async function loadAlerts() {
  const list = $("#alert-list");
  const q = $("#aq").value.trim();
  const source = $("#asource").value;
  list.innerHTML = '<div class="ldr">loading…</div>';
  const params = new URLSearchParams({ limit: "120" });
  if (q) params.set("q", q);
  if (source) params.set("source", source);
  try {
    const d = await api(`/api/intel?${params}`);
    const keys = d.keys || {};
    $("#acount").textContent = `${d.total} items. Companies House API ${keys.companies_house ? "on" : "off"}`;
    list.innerHTML = d.items.length
      ? d.items.map((item) => `
        <div class="post">
          <div>
            <div class="ptitle">${esc(item.title)}</div>
            <div class="pdetail">
              <span class="badge src">${esc(item.source)}</span>
              <span class="badge grp">${esc(item.kind)}</span>
              ${item.extra?.cve ? esc(item.extra.cve) + " " : ""}
              ${item.extra?.vendor ? esc(item.extra.vendor) + " " : ""}
              ${item.summary ? esc(item.summary.slice(0, 220)) : ""}
            </div>
          </div>
          <div class="right">
            <div class="pdate">${fmtDate(item.published)}</div>
            ${item.url ? `<a href="${esc(item.url)}" target="_blank" rel="noopener">view→</a>` : ""}
          </div>
        </div>`).join("")
      : '<div class="ldr">no alerts yet — still syncing</div>';
  } catch (e) {
    list.innerHTML = `<div class="ldr">failed to load: ${esc(e.message)}</div>`;
  }
}
["aq"].forEach((id) => $(`#${id}`).addEventListener("input", debounce(loadAlerts, 300)));
$("#asource").addEventListener("change", loadAlerts);
$("#arefresh").addEventListener("click", loadAlerts);

function debounce(fn, ms) {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

(function () {
  const btn = document.getElementById("awake");
  let lock = null;
  let want = localStorage.getItem("evileye-awake") === "1";

  function paint() {
    btn.classList.toggle("on", want && !!lock);
    btn.setAttribute("aria-pressed", want && lock ? "true" : "false");
    btn.querySelector(".awake-label").textContent = want && lock ? "Screen kept on" : "Keep screen on";
  }

  async function acquire() {
    if (!navigator.wakeLock) {
      btn.title = "This browser cannot keep the screen awake.";
      want = false;
      localStorage.removeItem("evileye-awake");
      paint();
      return;
    }
    try {
      lock = await navigator.wakeLock.request("screen");
      lock.addEventListener("release", () => {
        lock = null;
        paint();
      });
    } catch (err) {
      lock = null;
      btn.title = err.message || "Wake lock was denied.";
    }
    paint();
  }

  async function setAwake(on) {
    want = on;
    if (on) localStorage.setItem("evileye-awake", "1");
    else localStorage.removeItem("evileye-awake");
    if (!on && lock) {
      await lock.release();
      lock = null;
    }
    if (on) await acquire();
    else paint();
  }

  btn.addEventListener("click", () => setAwake(!(want && lock)));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && want) acquire();
  });
  if (want) acquire();
  else paint();
})();

document.getElementById("refresh-now").addEventListener("click", async () => {
  const btn = document.getElementById("refresh-now");
  btn.disabled = true;
  btn.textContent = "Refreshing";
  try {
    await fetch("/api/refresh", { method: "POST" });
  } catch (err) {
    btn.title = err.message || "Refresh failed";
  }
  await waitForRefresh();
  btn.disabled = false;
  btn.textContent = "Refresh";
  refreshCurrent();
});

// Refresh policy: the server fetches feeds only when the page is opened (if its data
// is stale) or when Refresh is pressed. Nothing polls in the background.
async function onOpen() {
  try {
    const r = await fetch("/api/open", { method: "POST" });
    if (r.status === 401) { location.href = "/login"; return; }
    const o = await r.json();
    if (o.refreshing) await waitForRefresh();
  } catch (e) { /* offline: show cached data */ }
  await loadHealth();
  refreshCurrent();
}
async function waitForRefresh() {
  // short-lived check, only while a refresh we asked for is running
  for (let i = 0; i < 40; i++) {
    await loadHealth();
    if (!$("#last-updated").textContent.includes("refreshing")) return;
    await new Promise((res) => setTimeout(res, 3000));
  }
}
loadHealth();
loadOverview();
onOpen();