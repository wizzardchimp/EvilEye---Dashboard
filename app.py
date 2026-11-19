"""EvilEye - Dashboard backend.

Data refresh policy: there is NO background loop. Feeds are fetched only
  * when a signed-in user opens the dashboard and the stored data is stale
    (older than STALE_MINUTES), or
  * when the user presses Refresh (POST /api/refresh).
"""
import hmac
import json
import os
import re
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import breaches
from flask import Flask, jsonify, redirect, request, send_from_directory, session

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT = DATA_DIR / "snapshot.json"

PASSWORD = os.environ.get("EVILEYE_PASSWORD", "")
STALE_MINUTES = int(os.environ.get("STALE_MINUTES", "30"))
CH_KEY = os.environ.get("COMPANIES_HOUSE_KEY", "")
UA = {"User-Agent": "EvilEye-Dashboard/1.0 (research)"}

app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("RAILWAY_ENVIRONMENT")))

# ----------------------------------------------------------------- state
_lock = threading.Lock()
STATE = {"victims": [], "posts": [], "gangs": [], "intel": [], "last_refresh": {}, "errors": {}}
_refreshing = False
_geo_running = False
CASES = []                      # ICO cases (kept out of the main snapshot: it is large)
GEO = breaches.load_geo(DATA_DIR)  # organisation key -> placement record (cache on disk)
CASES_FILE = DATA_DIR / "ico_cases.json"
try:
    CASES = json.loads(CASES_FILE.read_text())
except Exception:
    pass


def _load_snapshot():
    try:
        STATE.update(json.loads(SNAPSHOT.read_text()))
    except Exception:
        pass


_load_snapshot()
for _v in STATE["victims"]:
    _v["activity"] = _sector(_v.get("activity"))


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _get(url, as_json=True):
    r = requests.get(url, headers=UA, timeout=40)
    r.raise_for_status()
    return r.json() if as_json else r.text


# ----------------------------------------------------------------- region
WM_TOWNS = ["Birmingham", "Coventry", "Wolverhampton", "Solihull", "Walsall", "Dudley", "West Bromwich",
            "Sutton Coldfield", "Stoke-on-Trent", "Telford", "Worcester", "Stafford", "Redditch",
            "Burton-on-Trent", "Burton upon Trent", "Pershore", "Lichfield", "Tamworth", "Nuneaton",
            "Leamington Spa", "Kidderminster", "Stourbridge", "Halesowen", "Smethwick", "Oldbury",
            "Cannock", "Shrewsbury", "Hereford", "Bromsgrove", "Stratford-upon-Avon", "Bilston",
            "Wednesbury", "Tipton", "Bridgnorth", "Evesham", "Droitwich", "Malvern", "Leek", "Stone"]
WM_COUNTIES = ["West Midlands", "Warwickshire", "Staffordshire", "Shropshire", "Worcestershire", "Herefordshire"]
# Stone and Leek are common words, so they only count next to a county name; see _wm_hits
_AMBIGUOUS = {"Stone", "Leek"}
POSTCODE = re.compile(r"\b(B|CV|DY|WS|WV|ST|TF|WR|HR)\d{1,2}[A-Z]?\s?\d[A-Z]{2}\b")
_TOWN_RE = {t: re.compile(r"\b" + re.escape(t) + r"\b", re.I) for t in WM_TOWNS}
_COUNTY_RE = {c: re.compile(r"\b" + re.escape(c) + r"\b", re.I) for c in WM_COUNTIES}


def _wm_hits(text):
    hits = []
    counties = [c for c, rx in _COUNTY_RE.items() if rx.search(text)]
    for t, rx in _TOWN_RE.items():
        if rx.search(text) and (t not in _AMBIGUOUS or counties):
            hits.append(t)
    hits += counties
    hits += sorted({m.group(0).replace("  ", " ") for m in POSTCODE.finditer(text.upper())})
    return list(dict.fromkeys(hits))


def _looks_british(v):
    """A town name is not enough: the record must look British and not overseas."""
    site = (v.get("website") or "").lower()
    if site.endswith(".uk") or ".uk/" in site:
        return True
    if v.get("country") == "GB":
        return True
    return False


def _sentence_with(text, hit):
    for s in re.split(r"(?<=[.;])\s+", text):
        if hit.lower() in s.lower():
            return s.strip()[:220]
    return ""


def _sector(a):
    """ransomware.live uses 'Not Found' (or nothing) when it has not classified a victim."""
    a = (a or "").strip()
    return "Unclassified" if a in ("", "Not Found", "Unknown") else a


def _year(item):
    for k in ("published", "discovered", "attackdate"):
        val = item.get(k)
        if val:
            return str(val)[:4]
    return "unknown"


# ----------------------------------------------------------------- ingest
def _ingest():
    global _refreshing
    errors, stamps = {}, {}
    new = {"victims": STATE["victims"], "posts": STATE["posts"], "gangs": STATE["gangs"], "intel": STATE["intel"]}

    # ransomware.live: UK victims
    try:
        gb = _get("https://api.ransomware.live/v2/countryvictims/GB")
        victims = []
        for v in gb:
            text = " ".join(str(v.get(k) or "") for k in ("description", "website", "post_title"))
            hits = _wm_hits(text) if _looks_british(v) else []
            region = "west_midlands" if hits else "uk"
            loc = _sentence_with(v.get("description") or "", hits[0]) if hits else ""
            victims.append({
                "post_title": v.get("post_title") or "", "group": v.get("group_name") or "unknown",
                "activity": _sector(v.get("activity")), "discovered": v.get("published") or v.get("discovered"),
                "year": _year(v), "website": (v.get("website") or "").strip(),
                "post_url": v.get("post_url") or "", "region": region, "region_hits": hits,
                "location": loc, "registered_name": "", "maps_location": "", "country": "GB",
            })
        new["victims"] = victims
        stamps["rl_victims_gb"] = _now_iso()
    except Exception as e:
        errors["rl_victims_gb"] = str(e)

    # ransomware.live recent + RansomLook recent -> aggregated feed
    posts, press = [], []
    try:
        for v in _get("https://api.ransomware.live/v2/recentvictims"):
            gbv = v.get("country") == "GB"
            hits = _wm_hits(" ".join(str(v.get(k) or "") for k in ("description", "domain", "victim"))) if gbv else []
            posts.append({"title": v.get("victim") or "", "source": "ransomware.live", "group": v.get("group") or "",
                          "detail": (v.get("description") or "")[:200], "date": v.get("discovered"),
                          "url": v.get("url") or "", "region": "west_midlands" if hits else ""})
            if v.get("press"):
                press.append({"title": v.get("victim") or "", "source": "ransomware.live", "kind": "press",
                              "summary": (v.get("description") or "")[:260], "url": v.get("url") or "",
                              "published": v.get("discovered"), "extra": {}})
        stamps["rl_recent"] = _now_iso()
    except Exception as e:
        errors["rl_recent"] = str(e)
    rlook_groups = set()
    rlook_intel = []
    try:
        for v in _get("https://www.ransomlook.io/api/recent/100"):
            rlook_groups.add(v.get("group_name") or "")
            d = (v.get("discovered") or "").replace(" ", "T")
            posts.append({"title": v.get("post_title") or "", "source": "ransomlook", "group": v.get("group_name") or "",
                          "detail": (v.get("description") or "")[:200], "date": d, "url": v.get("link") or "", "region": ""})
            rlook_intel.append({"title": v.get("post_title") or "", "source": "ransomlook", "kind": "leak-post",
                                "summary": (v.get("description") or "")[:260], "url": v.get("link") or "",
                                "published": d, "extra": {}})
        stamps["ransomlook"] = _now_iso()
    except Exception as e:
        errors["ransomlook"] = str(e)
    if posts:
        posts.sort(key=lambda p: p.get("date") or "", reverse=True)
        new["posts"] = posts

    # gangs: ransomware.live groups + deepdarkCTI + RansomLook names
    uk_by_group = Counter((v["group"] or "").lower() for v in new["victims"])
    try:
        gangs = {}
        for g in _get("https://api.ransomware.live/v2/groups"):
            name = g.get("name") or ""
            gangs[name.lower()] = {
                "name": name, "altname": g.get("altname"), "description": g.get("description") or "",
                "leak_sites": [{"slug": l.get("slug"), "fqdn": l.get("fqdn"), "available": l.get("available")}
                               for l in (g.get("locations") or [])],
                "sources": ["ransomware.live"], "added_date": g.get("added_date")}
        try:
            md = _get("https://raw.githubusercontent.com/fastfire/deepdarkCTI/main/ransomware_gang.md", False)
            for line in md.splitlines():
                m = re.match(r"\|\[([^\]]+)\]\(([^)]+)\)\|(ONLINE|OFFLINE|VALID|[A-Z ]*)\|", line)
                if not m or "list" in m.group(1).lower():
                    continue
                name = re.sub(r"\s*\((Deep|Victims page)[^)]*\)", "", m.group(1)).strip()
                g = gangs.setdefault(name.lower(), {"name": name, "altname": None, "description": "", "leak_sites": [],
                                                   "sources": [], "added_date": None})
                if "deepdarkcti" not in g["sources"]:
                    g["sources"].append("deepdarkcti")
                g["leak_sites"].append({"slug": m.group(2), "fqdn": None, "status": m.group(3) or None})
            stamps["deepdarkcti"] = _now_iso()
        except Exception as e:
            errors["deepdarkcti"] = str(e)
        for n in rlook_groups:
            if n:
                g = gangs.setdefault(n.lower(), {"name": n, "altname": None, "description": "", "leak_sites": [],
                                                 "sources": [], "added_date": None})
                if "ransomlook" not in g["sources"]:
                    g["sources"].append("ransomlook")
        for k, g in gangs.items():
            g["activity"] = uk_by_group.get(k, 0)
        new["gangs"] = sorted(gangs.values(), key=lambda g: g["name"].lower())
        stamps["rl_groups"] = _now_iso()
    except Exception as e:
        errors["rl_groups"] = str(e)

    # alerts: NCSC + CISA KEV + RansomLook + press
    intel = list(press) + rlook_intel[:30]
    try:
        root = ET.fromstring(_get("https://www.ncsc.gov.uk/api/1/services/v1/all-rss-feed.xml", False))
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate")).isoformat()
            except Exception:
                pub = None
            kind = link.split("/")[3] if link.count("/") >= 4 else "ncsc"
            intel.append({"title": (it.findtext("title") or "").strip(), "source": "ncsc", "kind": kind,
                          "summary": (it.findtext("description") or "").strip(), "url": link, "published": pub, "extra": {}})
        stamps["ncsc"] = _now_iso()
    except Exception as e:
        errors["ncsc"] = str(e)
    try:
        kev = _get("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")
        vulns = sorted(kev.get("vulnerabilities", []), key=lambda x: x.get("dateAdded", ""), reverse=True)[:150]
        for x in vulns:
            intel.append({"title": x.get("vulnerabilityName") or x.get("cveID"), "source": "cisa", "kind": "KEV",
                          "summary": x.get("shortDescription") or "", "published": x.get("dateAdded"),
                          "url": "https://nvd.nist.gov/vuln/detail/" + (x.get("cveID") or ""),
                          "extra": {"cve": x.get("cveID"), "vendor": x.get("vendorProject")}})
        stamps["cisa_kev"] = _now_iso()
    except Exception as e:
        errors["cisa_kev"] = str(e)
    if intel:
        intel.sort(key=lambda i: str(i.get("published") or ""), reverse=True)
        new["intel"] = intel

    # ICO self-reported personal data breach cases (quarterly CSVs; each file is downloaded once)
    try:
        cases, _ = breaches.ingest(DATA_DIR)
        if cases:
            CASES[:] = cases
            CASES_FILE.write_text(json.dumps(cases))
        stamps["ico"] = _now_iso()
    except Exception as e:
        errors["ico"] = str(e)

    # optional enrichment: Companies House for West Midlands matches
    if CH_KEY:
        try:
            for v in new["victims"]:
                if v["region"] != "west_midlands" or v.get("registered_name"):
                    continue
                r = requests.get("https://api.company-information.service.gov.uk/search/companies",
                                 params={"q": v["post_title"], "items_per_page": 1}, auth=(CH_KEY, ""), timeout=20)
                if r.ok and r.json().get("items"):
                    it = r.json()["items"][0]
                    v["registered_name"] = it.get("title") or ""
                    v["location"] = it.get("address_snippet") or v["location"]
            stamps["companies_house"] = _now_iso()
        except Exception as e:
            errors["companies_house"] = str(e)

    with _lock:
        STATE.update(new)
        STATE["last_refresh"] = {**STATE.get("last_refresh", {}), **stamps}
        STATE["errors"] = errors
        try:
            SNAPSHOT.write_text(json.dumps({k: STATE[k] for k in ("victims", "posts", "gangs", "intel", "last_refresh", "errors")}))
        except Exception as e:
            STATE["errors"]["snapshot"] = str(e)
    _refreshing = False
    _geo()


def _geo():
    """Bounded placement of ICO organisations (resumes where the last refresh stopped)."""
    global _geo_running
    if _geo_running or not breaches.CH_ENABLED or not CASES:
        return
    _geo_running = True
    try:
        breaches.geo_pass(CASES, GEO, DATA_DIR)
    except Exception as e:
        STATE["errors"]["geo"] = str(e)
    finally:
        _geo_running = False


def start_refresh():
    """Start one refresh in a worker thread. Returns False if one is already running."""
    global _refreshing
    with _lock:
        if _refreshing:
            return False
        _refreshing = True
    threading.Thread(target=_ingest, daemon=True).start()
    return True


def is_stale():
    last = STATE.get("last_refresh", {}).get("rl_victims_gb")
    if not last:
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
    return age > STALE_MINUTES


# ----------------------------------------------------------------- auth
def authed():
    return session.get("ok") is True


@app.before_request
def guard():
    p = request.path
    if p in ("/login", "/healthz") or p.startswith("/static/") or p == "/favicon.ico":
        return None
    if not authed():
        if p.startswith("/api/") or p == "/health":
            return jsonify(error="unauthorized"), 401
        return redirect("/login")
    return None


@app.get("/login")
def login_page():
    return (BASE / "login.html").read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/login")
def login_post():
    pw = request.form.get("password", "")
    if PASSWORD and hmac.compare_digest(pw.encode(), PASSWORD.encode()):
        session.clear()
        session["ok"] = True
        return redirect("/")
    time.sleep(1)
    return redirect("/login?error=1")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/health")
def health():
    return jsonify(errors=STATE.get("errors") or None, last_refresh=STATE.get("last_refresh", {}),
                   refreshing=_refreshing, placing=_geo_running)


@app.post("/api/open")
def api_open():
    """Called once when the dashboard is opened: refresh only if the data is stale."""
    started = start_refresh() if is_stale() else False
    return jsonify(refreshing=_refreshing, started=started, stale=is_stale())


@app.post("/api/refresh")
def api_refresh():
    return jsonify(started=start_refresh(), refreshing=_refreshing), 202


# ----------------------------------------------------------------- queries
def _uk():
    return STATE["victims"]


def _share(n, total):
    return round(100 * n / total, 1) if total else 0


def _counts(key, label_default="Unknown"):
    return Counter((v.get(key) or label_default) for v in _uk())


@app.get("/api/summary")
def api_summary():
    v = _uk()
    years = Counter(x["year"] for x in v)
    return jsonify(
        uk_total=len(v), wm_total=sum(1 for x in v if x["region"] == "west_midlands"),
        attacks_total=len(STATE["posts"]) or len(v), gangs_total=len(STATE["gangs"]),
        by_year=[{"year": y, "count": n} for y, n in sorted(years.items())][-8:],
        top_gangs=[{"group": g, "count": n} for g, n in _counts("group").most_common(8)],
        top_sectors=[{"sector": s, "count": n} for s, n in _counts("activity").most_common(8)])


KINDS = {"year": ("year", "UK victims by year"), "gang": ("group", "UK victims by gang"),
         "sector": ("activity", "UK victims by sector")}


@app.get("/api/summary/detail")
def api_detail():
    kind = request.args.get("kind", "sector")
    key, title = KINDS.get(kind, KINDS["sector"])
    total = len(_uk())
    items = [{"label": k, "count": n, "share": _share(n, total)} for k, n in _counts(key).most_common(30)]
    return jsonify(title=title, items=items)


@app.get("/api/summary/slice")
def api_slice():
    kind = request.args.get("kind", "sector")
    label = request.args.get("label", "")
    key, _ = KINDS.get(kind, KINDS["sector"])
    rows = [v for v in _uk() if (v.get(key) or "Unknown") == label]
    other = "activity" if kind != "sector" else "group"
    split = Counter((v.get(other) or "Unknown") for v in rows).most_common(12)
    explain = {"year": f"Victims first published in {label}.", "gang": f"Victims claimed by {label}.",
               "sector": f"Victims in the {label} sector."}[kind if kind in KINDS else "sector"]
    rows.sort(key=lambda v: str(v.get("discovered") or ""), reverse=True)
    return jsonify(label=label, explain=explain, count=len(rows), share=_share(len(rows), len(_uk())),
                   split_title="By sector" if other == "activity" else "By gang",
                   split=[{"label": k, "count": n} for k, n in split],
                   victims=rows[:40])


def _int(name, default, hi=500):
    try:
        return max(0, min(int(request.args.get(name, default)), hi))
    except ValueError:
        return default


@app.get("/api/victims")
def api_victims():
    q = request.args.get("q", "").lower()
    items = _uk()
    if request.args.get("region"):
        items = [v for v in items if v["region"] == request.args["region"]]
    if request.args.get("group"):
        items = [v for v in items if v["group"] == request.args["group"]]
    if request.args.get("sector"):
        items = [v for v in items if v["activity"] == request.args["sector"]]
    if q:
        items = [v for v in items if q in (v["post_title"] + " " + v["website"] + " " + v["group"]).lower()]
    items = sorted(items, key=lambda v: str(v.get("discovered") or ""), reverse=True)
    off, lim = _int("offset", 0, 10**6), _int("limit", 200)
    ico = {c["key"] for c in CASES if c["key"]}
    page = [dict(v, ico=breaches.norm(v["post_title"]) in ico) for v in items[off:off + lim]]
    facets = {"groups": sorted({v["group"] for v in _uk() if v["group"]}, key=str.lower),
              "sectors": sorted({v["activity"] for v in _uk() if v["activity"]})}
    return jsonify(total=len(items), items=page, facets=facets)


@app.get("/api/posts")
def api_posts():
    q = request.args.get("q", "").lower()
    items = STATE["posts"]
    if q:
        items = [p for p in items if q in (p["title"] + " " + p["group"] + " " + p["detail"]).lower()]
    return jsonify(total=len(items), items=items[:_int("limit", 250)])


@app.get("/api/gangs")
def api_gangs():
    q = request.args.get("q", "").lower()
    items = STATE["gangs"]
    if request.args.get("source"):
        items = [g for g in items if request.args["source"] in g["sources"]]
    if q:
        items = [g for g in items if q in (g["name"] + " " + (g["altname"] or "") + " " + g["description"]).lower()]
    if request.args.get("live") == "1":
        items = [g for g in items if any(s.get("available") is True or s.get("status") == "ONLINE" for s in g["leak_sites"])]
    if request.args.get("sort") == "active":
        items = sorted(items, key=lambda g: (-g["activity"], g["name"].lower()))
    return jsonify(total=len(items), items=items[:400])


@app.get("/api/intel")
def api_intel():
    q = request.args.get("q", "").lower()
    items = STATE["intel"]
    if request.args.get("source"):
        items = [i for i in items if i["source"] == request.args["source"]]
    if q:
        items = [i for i in items if q in json.dumps(i).lower()]
    return jsonify(total=len(items), items=items[:_int("limit", 120)], keys={"companies_house": bool(CH_KEY)})


# ----------------------------------------------------------------- ICO breaches
def _cases(ignore_sector=False):
    """Filtered ICO cases from query args: cyber=1, q, sector, subsector, rocu, force, quarter."""
    out = CASES
    if request.args.get("cyber") == "1":
        out = [c for c in out if c["cyber"]]
    for arg, key in (("sector", "sector"), ("subsector", "subsector"), ("quarter", "quarter")):
        if ignore_sector and arg != "quarter":
            continue
        if request.args.get(arg):
            out = [c for c in out if c[key] == request.args[arg]]
    q = request.args.get("q", "").lower()
    if q:
        out = [c for c in out if q in (c["org"] + " " + c["sector"] + " " + c["subsector"]).lower()]
    rocu, force = request.args.get("rocu"), request.args.get("force")
    if rocu or force:
        def hit(c):
            g = GEO.get(c["key"]) or {}
            if g.get("status") != "placed":
                return rocu == "Unplaced" and not force
            return (not rocu or g.get("rocu") == rocu) and (not force or g.get("force") == force)
        out = [c for c in out if hit(c)]
    return out


def _geo_of(c):
    g = GEO.get(c["key"]) or {}
    return g if g.get("status") == "placed" else {}


@app.get("/api/breaches/summary")
def api_breaches_summary():
    out = _cases()
    total = len(out)
    quarters = Counter(c["quarter"] for c in out)
    cyber_n = sum(1 for c in out if c["cyber"])
    rw = {breaches.norm(v["post_title"]) for v in STATE["victims"]}
    return jsonify(
        total=total, cyber=cyber_n, cyber_share=_share(cyber_n, total),
        orgs=len({c["key"] for c in out if c["key"]}),
        also_ransomware=len({c["key"] for c in out if c["key"] in rw}),
        by_quarter=[{"quarter": q, "count": n} for q, n in sorted(quarters.items())],
        top_sectors=[{"sector": s, "count": n} for s, n in Counter(c["sector"] for c in out).most_common(10)],
        decisions=[{"decision": d or "Unknown", "count": n} for d, n in Counter(c["decision"] for c in out).most_common(6)],
        cyber_flags=[{"flag": f, "count": n} for f, n in Counter(c["cyber_flag"] for c in _all_for_flags()).most_common()],
        source_note="Self-reported cases the ICO dealt with and did not refer for investigation.")


def _all_for_flags():
    # flag breakdown ignores the cyber toggle so the split is always visible
    saved = request.args
    out = CASES
    q = saved.get("q", "").lower()
    if saved.get("sector"):
        out = [c for c in out if c["sector"] == saved["sector"]]
    if q:
        out = [c for c in out if q in (c["org"] + " " + c["sector"]).lower()]
    return out


@app.get("/api/breaches/regions")
def api_breaches_regions():
    out = _cases()
    total = len(out)
    by_rocu = defaultdict(lambda: Counter())
    placed = 0
    for c in out:
        g = _geo_of(c)
        if g:
            placed += 1
            by_rocu[g["rocu"]][g["force"]] += 1
    tried = sum(1 for c in out if (GEO.get(c["key"]) or {}).get("status"))
    rocus = []
    for r in list(breaches.ROCU_FORCES) + ["Scotland", "Northern Ireland"]:
        forces = by_rocu.get(r, Counter())
        n = sum(forces.values())
        rocus.append({"rocu": r, "count": n, "share": _share(n, total),
                      "forces": [{"force": f, "name": breaches.FORCE_NAMES.get(f, f.replace("-", " ").title()), "count": k}
                                 for f, k in forces.most_common()]})
    rocus.sort(key=lambda x: -x["count"])
    return jsonify(total=total, placed=placed, unplaced=total - placed,
                   coverage=_share(placed, total), looked_up=tried, enabled=breaches.CH_ENABLED,
                   placing=_geo_running, rocus=rocus,
                   note="Placed by matching the organisation name to a Companies House registered office. "
                        "Organisations that cannot be matched (many GP surgeries, schools, charities) stay unplaced.")


@app.get("/api/breaches/cases")
def api_breaches_cases():
    out = sorted(_cases(), key=lambda c: c["completed"] or "", reverse=True)
    rw = {breaches.norm(v["post_title"]): v["group"] for v in STATE["victims"]}
    off, lim = _int("offset", 0, 10**6), _int("limit", 100)
    items = []
    for c in out[off:off + lim]:
        g = _geo_of(c)
        items.append(dict(c, rocu=g.get("rocu"), force=g.get("force"),
                          force_name=breaches.FORCE_NAMES.get(g.get("force"), "") if g else "",
                          postcode=g.get("postcode"), ransomware_group=rw.get(c["key"])))
    return jsonify(total=len(out), items=items, sectors=sorted({c["sector"] for c in CASES}),
                   rocus=list(breaches.ROCU_FORCES) + ["Scotland", "Northern Ireland", "Unplaced"])


@app.get("/api/breaches/sectors")
def api_breaches_sectors():
    """All sectors with counts; with ?sector=X, the sub-sector breakdown of that sector."""
    sel = request.args.get("sector")
    out = _cases(ignore_sector=True)
    total = len(out)
    if sel:
        sub = [c for c in out if c["sector"] == sel]
        n = len(sub)
        items = Counter(c["subsector"] or "Not stated" for c in sub).most_common()
        return jsonify(sector=sel, total=n, share=_share(n, total),
                       items=[{"label": k, "count": v, "share": _share(v, n)} for k, v in items])
    return jsonify(total=total, items=[{"label": k, "count": v, "share": _share(v, total),
                                        "common": breaches.common_ico(k)}
                                       for k, v in Counter(c["sector"] for c in out).most_common()])


@app.get("/api/compare/sectors")
def api_compare_sectors():
    """ICO breach cases vs ransomware victims, both mapped to one approximate common sector list."""
    ico = Counter(breaches.common_ico(c["sector"]) for c in _cases(ignore_sector=True))
    rw = Counter(breaches.common_rl(v["activity"]) for v in _uk())
    ni, nr = sum(ico.values()), sum(rw.values())
    rows = [{"sector": k, "ico": ico.get(k, 0), "ico_share": _share(ico.get(k, 0), ni),
             "rw": rw.get(k, 0), "rw_share": _share(rw.get(k, 0), nr)} for k in set(ico) | set(rw)]
    rows.sort(key=lambda r: -(r["ico_share"] + r["rw_share"]))
    return jsonify(ico_total=ni, rw_total=nr, rows=rows,
                   note="Both lists are mapped to one common set of sectors. The mapping is approximate.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
