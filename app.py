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


def _load_snapshot():
    try:
        STATE.update(json.loads(SNAPSHOT.read_text()))
    except Exception:
        pass


_load_snapshot()


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
                "activity": v.get("activity") or "", "discovered": v.get("published") or v.get("discovered"),
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
                   refreshing=_refreshing)


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
    c = Counter((v.get(key) or label_default) for v in _uk())
    if key == "activity":
        c.pop("Not Found", None)
    return c


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
    return jsonify(total=len(items), items=items[off:off + lim])


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
